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
