"""Tests for analysis/replay_activations.py: span logic (pure), and alignment on a tiny random model
(index token positions match the stored token IDs; a layer hook captures that layer's residual stream)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
sys.path.insert(0, str(ROOT / "tests"))

import replay_activations as ra  # noqa: E402


# ---- pure span logic (no torch) --------------------------------------------------------------------------
class FakeTok:
    """A toy tokenizer: each id decodes to a fixed string, concatenated."""
    VOCAB = {10: "ab", 11: "c", 12: "de", 13: "f", 14: "g"}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(self.VOCAB[i] for i in ids)


def test_message_span_locates_the_tokens():
    comp = [10, 11, 12, 13, 14]            # decodes to "ab c de f g" -> "abcdefg"
    sp = ra.message_span(FakeTok(), comp, "cde", prompt_len=100)
    assert sp is not None and sp.name == "message"
    assert (sp.start, sp.end) == (101, 103)         # tokens 1,2 ("c","de") -> "cde", offset by prompt_len
    assert ra.message_span(FakeTok(), comp, "zzz", prompt_len=100) is None       # not present -> None
    assert ra.message_span(FakeTok(), comp, "", prompt_len=100) is None


def test_spans_for_completion_and_prompt_tail():
    spans = ra.spans_for("m1", prompt_ids=list(range(10)), completion_ids=[100, 101, 102],
                         tok=None, message_text="", keep_last_prompt=4)
    by = {s.name: (s.start, s.end) for s in spans}
    assert by["completion"] == (10, 13)
    assert by["prompt_tail"] == (6, 10)              # last 4 of 10 prompt tokens
    assert "message" not in by                        # no tokenizer -> no message span


# ---- tiny-model alignment (torch + transformers) ---------------------------------------------------------
@pytest.fixture(scope="module")
def tiny():
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    from transformers import LlamaConfig, LlamaForCausalLM
    cfg = LlamaConfig(vocab_size=64, hidden_size=16, intermediate_size=32, num_hidden_layers=3,
                      num_attention_heads=2, num_key_value_heads=2, max_position_embeddings=128)
    torch.manual_seed(0)
    model = LlamaForCausalLM(cfg).eval()
    return model, torch, transformers


def test_hook_captures_the_layer_residual_stream(tiny):
    model, torch, _ = tiny
    prompt, completion = [5, 6, 7, 8], [9, 10]
    n_layers = model.config.num_hidden_layers
    layers = list(range(n_layers))                                # includes the last layer
    spans = ra.spans_for("m1", prompt, completion, tok=None, keep_last_prompt=2)
    res = ra.replay_point(model, prompt, completion, layers, spans, pool=True, device="cpu")
    ids = prompt + completion
    with torch.no_grad():
        ref = model(torch.tensor([ids]), output_hidden_states=True, use_cache=False).hidden_states
    for li in layers:
        comp = res["acts"][(li, "completion")]
        assert comp.shape == (len(completion), 16)
        assert torch.allclose(res["pooled"][(li, "completion")], comp.mean(dim=0), atol=1e-5)  # mean pool
        if li < n_layers - 1:
            # the hook captures the raw residual stream == hidden_states[li+1] for every layer EXCEPT the last,
            # where transformers' output_hidden_states applies the final norm. The hook gives the pre-norm stream.
            assert torch.allclose(comp, ref[li + 1][0, len(prompt):].to(torch.float32), atol=1e-4)
        else:
            assert not torch.allclose(comp, ref[li + 1][0, len(prompt):].to(torch.float32), atol=1e-4)
    assert res["acts"][(0, "prompt_tail")].shape == (2, 16)


def _make_run(tmp_path, prompt, completion):
    from test_load_run import RunBuilder
    b = RunBuilder(tmp_path / "20261108-000000-collusive")
    b.msg(1, "A", ["B"], "hello world payload")
    p = b.write()
    # link the sent message to a token record via the trajectory
    msgs = [json.loads(x) for x in (p / "messages.jsonl").read_text().splitlines()]
    out = []
    for m in msgs:
        out.append({**m, "tool_call_id": "call-1"} if m.get("event") == "sent" else m)
    (p / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in out))
    traj = {"messages": [{"role": "assistant", "content": "",
                          "extra": {"actions": [{"tool_call_id": "call-1"}], "token_ids_record": 1}}]}
    (p / "A.r1.traj.json").write_text(json.dumps(traj))
    rec = {"record": 1, "round": 1, "prompt_len": len(prompt), "prefix_len": 0,
           "prompt_suffix": prompt, "completion": completion}
    (p / "A.tokens.jsonl").write_text(json.dumps(rec) + "\n")
    return p


def test_run_replay_index_positions_match_stored_token_ids(tmp_path, tiny):
    from load_run import load_run
    model, torch, _ = tiny
    prompt, completion = [5, 6, 7, 8, 9], [10, 11, 12]
    run = load_run(str(_make_run(tmp_path, prompt, completion)))
    # sanity: point_tokens gives back exactly the stored IDs
    assert run.point_tokens("m1") == {"record": 1, "prompt": prompt, "completion": completion}
    out = tmp_path / "acts"
    res = ra.run_replay([run], layers=[0, 1], out_dir=out, model=model, tok=None,
                        keep_last_prompt=2, pool=False, device="cpu", save=True)
    ids = prompt + completion
    # every index entry's token_ids equal the stored sequence at its positions
    for e in res["index"]:
        assert e["token_ids"] == [ids[i] for i in e["token_positions"]], e
        assert len(e["token_positions"]) == res["_tensors"][e["key"]].shape[0]
    # completion span covers exactly the completion positions
    comp = [e for e in res["index"] if e["span"] == "completion" and e["layer"] == 0][0]
    assert comp["token_positions"] == [5, 6, 7] and comp["token_ids"] == completion
    tail = [e for e in res["index"] if e["span"] == "prompt_tail" and e["layer"] == 0][0]
    assert tail["token_positions"] == [3, 4] and tail["token_ids"] == [8, 9]
    # the index file and a tensor store were written
    assert (out / "index.json").exists()
    written = json.loads((out / "index.json").read_text())
    assert written["model"] == ra.MODEL_ID and written["revision"] == ra.REVISION
    assert (out / "activations.safetensors").exists() or (out / "activations.npz").exists()


def test_real_gemma_config_has_the_hook_targets():
    """Instantiate the real model config with EMPTY weights (no GPU, no download of weights) and confirm the
    script's hook target (decoder_layers) exists and its length matches num_hidden_layers. Skips if the config
    is not available (no network / gated repo / transformers too old). Set SWARM_GEMMA_CONFIG to a local dir or
    config.json to run it offline (e.g. a copy of the HF-cache config)."""
    import os
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    accelerate = pytest.importorskip("accelerate")
    from transformers import AutoConfig, AutoModelForCausalLM
    import torch.nn as nn
    src = os.environ.get("SWARM_GEMMA_CONFIG")
    try:
        config = AutoConfig.from_pretrained(src) if src else AutoConfig.from_pretrained(ra.MODEL_ID, revision=ra.REVISION)
    except Exception as e:                                           # no network, gated, or model_type unknown
        pytest.skip(f"Gemma 4 config not available: {type(e).__name__}: {e}")
    with accelerate.init_empty_weights():                            # structure only, on the meta device
        model = AutoModelForCausalLM.from_config(config)
    layers = ra.decoder_layers(model)                               # the script's hook target must exist
    n = getattr(config, "num_hidden_layers", None) or getattr(getattr(config, "text_config", None),
                                                               "num_hidden_layers", None)
    assert n and len(layers) == n, f"{len(layers)} layer modules vs num_hidden_layers {n}"
    assert all(isinstance(blk, nn.Module) for blk in layers)
    h = layers[0].register_forward_hook(lambda m, i, o: None)        # a hook attaches without error
    h.remove()
    assert type(model.base_model).__name__ == "Gemma4Model"          # run_decoder() calls this: no LM head


# ---- endpoint-JSON input mode (--tokens-json) ------------------------------------------------------------
def test_parse_tokens_spec_from_file_name_or_label():
    assert ra.parse_tokens_spec("exports/20261108-145133-collusive__Keter__round1.json") == (
        "exports/20261108-145133-collusive__Keter__round1.json", "20261108-145133-collusive", "Keter")
    assert ra.parse_tokens_spec("A-20261108-154508-collusive_cat_r1.json")[1:] == ("A-20261108-154508-collusive", "Cat")
    assert ra.parse_tokens_spec("20261108-150210-honest/malina.json")[1:] == ("20261108-150210-honest", "Malina")
    assert ra.parse_tokens_spec("R/Keter=whatever.json") == ("whatever.json", "R", "Keter")
    for bad in ("keter.json", "20261108-145133-collusive.json", "20261108-145133-collusive_Cat_Keter.json"):
        with pytest.raises(ValueError):
            ra.parse_tokens_spec(bad)


def test_load_tokens_json_skips_unreplayable_records_and_filters(tmp_path):
    recs = [{"record": 1, "round": 1, "prompt_len": 3, "prompt_token_ids": [1, 2, 3], "completion": [4, 5]},
            {"record": 2, "round": 1, "prompt_len": 4, "prompt_token_ids": None, "reconstruction_error": "got 2"},
            {"record": 3, "round": 1, "prompt_len": 9, "prompt_token_ids": [1, 2], "completion": [6]},
            {"record": 4, "round": 2, "prompt_len": 2, "prompt_token_ids": [7, 8], "completion": None}]
    f = tmp_path / "x.json"
    f.write_text(json.dumps(recs))
    items, skipped = ra.load_tokens_json(f, "R", "Cat")
    assert [(i["record"], i["prompt"], i["completion"]) for i in items] == [(1, [1, 2, 3], [4, 5]), (4, [7, 8], [])]
    assert [s[0] for s in skipped] == [2, 3]
    assert all(i["source"] == "tokens-json" and i["point_id"] is None and (i["run"], i["agent"]) == ("R", "Cat")
               for i in items)
    assert [i["record"] for i in ra.load_tokens_json(f, "R", "Cat", rounds={2})[0]] == [4]
    assert [i["record"] for i in ra.load_tokens_json(f, "R", "Cat", records={1})[0]] == [1]
    f.write_text(json.dumps({"records": recs[:1]}))
    assert len(ra.load_tokens_json(f, "R", "Cat")[0]) == 1
    f.write_text(json.dumps({"not": "records"}))
    with pytest.raises(ValueError):
        ra.load_tokens_json(f, "R", "Cat")


EVID_RUN = ROOT / "evidence" / "runs" / "20261108-145133-collusive"


def _export_round1(tmp_path, run_dir):
    """Endpoint exports exactly as a teammate gets them: GET /runs/<run>/agents/<name>/tokens?full=1&round=1."""
    import threading
    import urllib.request
    import serve_runs
    srv = serve_runs.make_server(run_dir.parent, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    files = []
    try:
        for agent in ra.AGENT_NAMES:
            url = f"http://127.0.0.1:{srv.server_address[1]}/runs/{run_dir.name}/agents/{agent}/tokens?full=1&round=1"
            with urllib.request.urlopen(url, timeout=60) as r:
                f = tmp_path / f"{run_dir.name}__{agent}__round1.json"
                f.write_bytes(r.read())
                files.append(f)
    finally:
        srv.shutdown()
        srv.server_close()
    return files


@pytest.mark.skipif(not EVID_RUN.exists(), reason="evidence run not present (kept out of git)")
def test_endpoint_export_token_ids_equal_run_folder_mode(tmp_path):
    from load_run import load_run
    files = _export_round1(tmp_path, EVID_RUN)
    from_json = {}
    for f in files:
        path, run, agent = ra.parse_tokens_spec(str(f))
        items, skipped = ra.load_tokens_json(path, run, agent)
        assert not skipped and items, f
        assert all(i["round"] == 1 for i in items)
        from_json.update({(i["agent"], i["record"]): i for i in items})
    from_run = ra.items_from_runs([load_run(EVID_RUN)], rounds={1})
    assert from_run, "the evidence run has message points with token IDs in round 1"
    for it in from_run:                     # every message point's model call is in the export, token for token
        j = from_json[(it["agent"], it["record"])]
        assert (j["prompt"], j["completion"]) == (it["prompt"], it["completion"]), (it["agent"], it["record"])
        assert j["run"] == it["run"] == EVID_RUN.name
    assert len(from_json) > len(from_run)   # the export also has the calls that sent no message


@pytest.mark.skipif(not EVID_RUN.exists(), reason="evidence run not present (kept out of git)")
def test_endpoint_export_and_run_folder_give_identical_activations(tmp_path):
    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from transformers import LlamaConfig, LlamaForCausalLM
    from load_run import load_run
    from_run = sorted(ra.items_from_runs([load_run(EVID_RUN)], rounds={1}),
                      key=lambda i: len(i["prompt"]) + len(i["completion"]))[:2]     # the two shortest
    longest = max(len(i["prompt"]) + len(i["completion"]) for i in from_run)
    cfg = LlamaConfig(vocab_size=262144, hidden_size=16, intermediate_size=32, num_hidden_layers=2,
                      num_attention_heads=2, num_key_value_heads=2, max_position_embeddings=max(4096, longest + 1))
    torch.manual_seed(0)
    model = LlamaForCausalLM(cfg).eval()
    files = _export_round1(tmp_path, EVID_RUN)
    from_json = []
    for f in files:
        path, run, agent = ra.parse_tokens_spec(str(f))
        want = {i["record"] for i in from_run if i["agent"] == agent}
        if want:
            from_json += ra.load_tokens_json(path, run, agent, records=want)[0]
    assert len(from_json) == len(from_run)
    kw = dict(layers=[0, 1], model=model, tok=None, keep_last_prompt=8, device="cpu", save=False)
    a = ra.replay_items(from_run, out_dir=tmp_path / "a", **kw)
    b = ra.replay_items(from_json, out_dir=tmp_path / "b", **kw)
    ia = {(e["agent"], e["record"], e["layer"], e["span"]): e for e in a["index"]}
    ib = {(e["agent"], e["record"], e["layer"], e["span"]): e for e in b["index"]}
    assert set(ia) == set(ib) and ia
    for k in ia:
        assert ia[k]["token_ids"] == ib[k]["token_ids"] and ia[k]["token_positions"] == ib[k]["token_positions"]
        assert torch.equal(a["_tensors"][ia[k]["key"]], b["_tensors"][ib[k]["key"]]), k
        assert (ia[k]["source"], ib[k]["source"]) == ("run-folder", "tokens-json")


def test_dry_run_reads_exports_without_torch(tmp_path, capsys):
    f = tmp_path / "20261108-145133-collusive__Keter__round1.json"
    f.write_text(json.dumps([{"record": 1, "round": 1, "prompt_len": 3, "prompt_token_ids": [1, 2, 3],
                              "completion": [4, 5]}]))
    ra.main(["--tokens-json", str(f), "--layers", "10,20", "--keep-last-prompt", "2", "--out", str(tmp_path / "o"),
             "--dry-run"])
    out = capsys.readouterr().out
    plan = json.loads(out.split("plan:", 1)[1])
    assert plan["forward_passes"] == 1 and plan["longest_sequence"] == 5
    assert plan["stored_rows"] == (2 + 2) * 2 and plan["approx_output_bytes"] == 8 * ra.GEMMA_HIDDEN * 4
    assert not (tmp_path / "o").exists()                       # dry run writes nothing


# ---- GPU + CPU offload (--device-map auto with memory caps) ----------------------------------------------
def test_max_memory_map():
    assert ra.max_memory_map("28GiB", "70GiB", 1) == {0: "28GiB", "cpu": "70GiB"}
    assert ra.max_memory_map("20GiB", None, 2) == {0: "20GiB", 1: "20GiB"}
    assert ra.max_memory_map(None, "70GiB", 0) == {"cpu": "70GiB"}
    assert ra.max_memory_map(None, None, 1) is None


def test_memory_caps_need_device_map_auto(tmp_path):
    f = tmp_path / "20261108-145133-collusive__Keter.json"
    f.write_text("[]")
    with pytest.raises(SystemExit):
        ra.main(["--tokens-json", str(f), "--layers", "1", "--out", str(tmp_path / "o"), "--max-gpu-memory", "28GiB"])


def test_offloaded_load_matches_plain_model(tmp_path, tiny):
    """load_model(device_map="auto", caps) on a saved tiny model: dispatched by accelerate within max_memory, and
    the replayed residual stream equals the same model without offloading (CPU-only here; on a GPU machine the
    layers that do not fit stay on the CPU)."""
    model, torch, _ = tiny
    pytest.importorskip("accelerate")
    model.save_pretrained(tmp_path / "tinymodel")
    off, tok = ra.load_model(str(tmp_path / "tinymodel"), revision=None, device_map="auto",
                             max_cpu_memory="2GiB", dtype="float32")
    assert tok is None and getattr(off, "hf_device_map", None)
    prompt, completion = [5, 6, 7, 8], [9, 10]
    spans = ra.spans_for("x", prompt, completion, keep_last_prompt=2)
    a = ra.replay_point(model, prompt, completion, [0, 2], spans, device="cpu")["acts"]
    b = ra.replay_point(off, prompt, completion, [0, 2], spans, device="auto")["acts"]
    assert a.keys() == b.keys() and all(torch.allclose(a[k], b[k], atol=1e-6) for k in a)


def test_decoder_only_forward_gives_identical_activations_and_skips_the_lm_head(tiny):
    """Default path (base model, no LM head) vs the old full CausalLM forward: identical hook outputs, and the LM
    head (full-vocabulary logits, the OOM on long records) is never called."""
    model, torch, _ = tiny
    calls = []
    h = model.lm_head.register_forward_hook(lambda m, i, o: calls.append(o.shape))
    prompt, completion = list(range(3, 40)), [41, 42, 43]
    layers = list(range(model.config.num_hidden_layers))
    spans = ra.spans_for("x", prompt, completion, keep_last_prompt=5)
    try:
        new = ra.replay_point(model, prompt, completion, layers, spans, device="cpu")["acts"]
        assert calls == []                                        # no logits computed
        old = ra.replay_point(model, prompt, completion, layers, spans, device="cpu", full_forward=True)["acts"]
        assert len(calls) == 1                                    # the old path does run the LM head
    finally:
        h.remove()
    assert new.keys() == old.keys() and all(torch.equal(new[k], old[k]) for k in new)
