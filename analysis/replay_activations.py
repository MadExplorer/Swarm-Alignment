"""Replay stored vLLM token IDs through the model with transformers to extract residual-stream activations.

Analysis only; never run on the server (it loads the full model on a GPU). vLLM 0.33 exposes no server option
for per-token hidden states (only pooled embeddings, and only for a pooling runner), so activations are taken
OFFLINE here by replaying the EXACT token IDs vLLM produced (never re-tokenized text), one forward pass per point.

Two input modes (same output):
    run folders     python replay_activations.py --runs RUN [RUN ...] --layers 12,18,24 --out acts
                    replays every MESSAGE point (send/broadcast) that has token IDs; needs load_run.py.
    endpoint JSON   python replay_activations.py --tokens-json FILE [FILE ...] --layers 12,18,24 --out acts
                    replays every RECORD (= one model call) in exports of the read-only endpoint's
                    /runs/<run>/agents/<name>/tokens?full=1 (records with prompt_token_ids + completion).
                    Run and agent come from the file name, or write RUN/AGENT=FILE. Needs no run folder,
                    no load_run.py, and never our vLLM: any machine with a big enough GPU.
    common options  [--agents Keter,Malina] [--rounds 1,2] [--records 3,7] [--keep-last-prompt 256] [--pool]
                    [--device cuda | --device-map auto [--max-gpu-memory 28GiB] [--max-cpu-memory 70GiB]]
                    [--dry-run]

Output (one file pair in --out):
    activations.safetensors (or .npz)  tensors, one per (point or record, layer, span), float32 [n_tokens, hidden]
    index.json                         every tensor's key -> {run, agent, record, round, point_id, source, layer,
                                       span, token_positions, token_ids}, so each vector maps back to its token.

Model: google/gemma-4-31B-it-qat-w4a16-ct @ 52f3f65bc7a02d555763bc923bd1d9094898219d (compressed-tensors, w4a16;
needs the `compressed-tensors` package). See docs/replay-activations.md for GPU requirements.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))      # load_run.py (run-folder mode only) is imported lazily: not needed for --tokens-json

MODEL_ID = "google/gemma-4-31B-it-qat-w4a16-ct"
REVISION = "52f3f65bc7a02d555763bc923bd1d9094898219d"
GEMMA_HIDDEN = 5376                # text_config.hidden_size of this model (for --dry-run size estimates)
AGENT_NAMES = ("Foxtrot", "Keter", "Malina", "Cringe", "Cat")
RUN_NAME_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Za-z0-9]{0,7}-)?\d{8}-\d{6}(-[a-z]+)?(?![0-9])")


# ---- model loading (heavy deps imported lazily, so the module imports without torch) -----------------------
def max_memory_map(max_gpu: Optional[str], max_cpu: Optional[str], n_gpus: int) -> Optional[dict]:
    """accelerate's max_memory: the same cap for every visible GPU (0..n_gpus-1) and one for the CPU."""
    if not max_gpu and not max_cpu:
        return None
    out = {i: max_gpu for i in range(n_gpus)} if max_gpu else {}
    if max_cpu:
        out["cpu"] = max_cpu
    return out


def load_model(model_id: str = MODEL_ID, revision: Optional[str] = REVISION, device: str = "cuda",
               dtype: str = "auto", device_map: Optional[str] = None, max_gpu_memory: Optional[str] = None,
               max_cpu_memory: Optional[str] = None):
    """Load the model and tokenizer for a forward pass only (eval, no grad, no generation). `model_id` may be a
    local path (tests use a tiny model), in which case pass revision=None.

    device_map="auto" (needs `accelerate`): GPU + CPU offload, for GPUs smaller than the bf16 model. The model is
    first loaded on the CPU with the compressed-tensors weights DEQUANTIZED to bf16 at load time
    (CompressedTensorsConfig(dequantize=True); ~62.5 GB RAM for Gemma 4 31B), then accelerate computes the device
    map from the bf16 sizes within --max-gpu-memory / --max-cpu-memory and dispatches it (layers that do not fit
    on the GPU stay in CPU RAM and are moved to the GPU for their part of each forward pass). Why not
    from_pretrained(device_map="auto") directly: transformers sizes the map from the COMPRESSED parameters
    (quantizers/base.py param_element_size = param.element_size()), so everything would be put on the GPU and
    then decompressed there on the first forward pass (compressed-tensors' decompress hook), far over 32 GB."""
    import torch
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
    try:
        tok = AutoTokenizer.from_pretrained(model_id, revision=revision)
    except (OSError, ValueError) as e:      # a bare test model has no tokenizer; only the message span needs it
        print(f"warning: no tokenizer ({type(e).__name__}); message spans are skipped")
        tok = None
    torch_dtype = dtype if dtype == "auto" else getattr(torch, dtype)
    if not device_map:
        model = AutoModelForCausalLM.from_pretrained(model_id, revision=revision, dtype=torch_dtype)
        model.to(device)
        model.eval()
        return model, tok
    from accelerate import dispatch_model, infer_auto_device_map
    kw = {}
    qc = getattr(AutoConfig.from_pretrained(model_id, revision=revision), "quantization_config", None)
    quant_method = qc.get("quant_method") if isinstance(qc, dict) else getattr(qc, "quant_method", None)
    if str(quant_method) == "compressed-tensors":
        from transformers import CompressedTensorsConfig
        kw["quantization_config"] = CompressedTensorsConfig(dequantize=True)   # bf16 at load time, on the CPU
    model = AutoModelForCausalLM.from_pretrained(model_id, revision=revision, dtype=torch_dtype, **kw)
    model.eval()
    max_memory = max_memory_map(max_gpu_memory, max_cpu_memory, torch.cuda.device_count())
    dmap = infer_auto_device_map(model, max_memory=max_memory,
                                 no_split_module_classes=list(getattr(model, "_no_split_modules", None) or []))
    if "disk" in set(dmap.values()):
        raise SystemExit("the model does not fit into --max-gpu-memory + --max-cpu-memory (accelerate wants disk "
                         "offload); raise the caps")
    model = dispatch_model(model, device_map=dmap)
    per_device = {}
    for dev in dmap.values():
        per_device[str(dev)] = per_device.get(str(dev), 0) + 1
    print(f"device map ({len(dmap)} entries): {per_device}  max_memory={max_memory}")
    return model, tok


def decoder_layers(model):
    """The list of text decoder blocks (residual-stream carriers). Tries the common layouts, including the
    nested text decoder of a multimodal model (Gemma may wrap the language model)."""
    candidates = (
        "model.layers", "model.model.layers",                       # Llama / Gemma text models
        "model.language_model.layers", "model.language_model.model.layers",   # multimodal wrappers
        "language_model.model.layers", "language_model.layers",
        "model.text_model.layers", "transformer.h", "gpt_neox.layers",
    )
    for path in candidates:
        obj = model
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            continue
        try:
            if len(obj) > 0:
                return obj
        except TypeError:
            continue
    raise AttributeError("could not find the decoder layer list on this model (checked: " + ", ".join(candidates) + ")")


# ---- one point -------------------------------------------------------------------------------------------
@dataclass
class SpanSpec:
    name: str            # "completion", "message", "prompt_tail"
    start: int           # absolute token position in prompt+completion (inclusive)
    end: int             # exclusive


@dataclass
class PointActivations:
    point_id: str
    run: str
    agent: str
    record: int
    seq_len: int
    token_ids: list            # the full prompt+completion IDs (as fed)
    spans: list                # list[SpanSpec]
    layer_acts: dict = field(default_factory=dict)   # (layer, span_name) -> tensor [n_tokens, hidden] (cpu)
    pooled: dict = field(default_factory=dict)        # (layer, span_name) -> tensor [hidden] (cpu), if --pool


def message_span(tok, completion_ids: list, message_text: str, prompt_len: int) -> Optional[SpanSpec]:
    """Best-effort token span of the message text inside the completion (the tool-call command). Returns None
    if it cannot be located. Decodes completion tokens cumulatively (no re-tokenization of the message) and
    finds the smallest token range whose decoded text contains `message_text`."""
    if not message_text:
        return None
    target = message_text.strip()
    # cumulative decode boundaries: text[:i] is the decode of completion_ids[:i]
    texts = [tok.decode(completion_ids[:i], skip_special_tokens=False) for i in range(len(completion_ids) + 1)]
    full = texts[-1]
    pos = full.find(target)
    if pos < 0:
        return None
    end_char = pos + len(target)
    # first token index whose cumulative text reaches past pos (start) and past end_char (end)
    start_tok = next((i for i in range(len(completion_ids) + 1) if len(texts[i]) > pos), 0)
    start_tok = max(0, start_tok - 1)
    end_tok = next((i for i in range(len(completion_ids) + 1) if len(texts[i]) >= end_char), len(completion_ids))
    if end_tok <= start_tok:
        return None
    return SpanSpec("message", prompt_len + start_tok, prompt_len + end_tok)


def spans_for(point_id: str, prompt_ids: list, completion_ids: list, tok=None, message_text: str = "",
              keep_last_prompt: Optional[int] = None) -> list:
    """The token spans whose activations are kept: the completion, the message text inside it (if found), and
    optionally a tail of the prompt (keep_last_prompt). Positions are absolute in prompt+completion."""
    p = len(prompt_ids)
    total = p + len(completion_ids)
    spans = [SpanSpec("completion", p, total)]
    if tok is not None and message_text:
        ms = message_span(tok, completion_ids, message_text, p)
        if ms is not None:
            spans.append(ms)
    if keep_last_prompt is not None and keep_last_prompt > 0 and p > 0:
        spans.append(SpanSpec("prompt_tail", max(0, p - keep_last_prompt), p))
    return spans


def replay_point(model, prompt_ids: list, completion_ids: list, layers: list, spans: list,
                 pool: bool = False, device: str = "cuda") -> dict:
    """One forward pass over prompt+completion (batch 1, no grad). Forward hooks on `layers` capture each
    layer's residual-stream output; for every span we keep that span's token rows. Returns
    {(layer, span_name): tensor[n_tokens, hidden]} and, if pool, {(layer, span_name): tensor[hidden]} (mean).
    The token IDs are fed EXACTLY as stored (never re-tokenized). The hook captures the RAW residual stream at
    each layer's output (pre the model's final norm); for the last layer this differs from transformers'
    output_hidden_states[-1], which is post-norm."""
    import torch
    ids = list(prompt_ids) + list(completion_ids)
    if device in (None, "auto"):                       # --device-map auto: inputs go where the embeddings are
        device = model.get_input_embeddings().weight.device
    input_ids = torch.tensor([ids], dtype=torch.long, device=device)
    blocks = decoder_layers(model)
    captured: dict = {}
    handles = []

    def hook_for(layer_idx):
        def hook(_module, _inp, out):
            h = out[0] if isinstance(out, (tuple, list)) else out      # [1, seq, hidden]
            captured[layer_idx] = h.detach()
        return hook

    for li in layers:
        handles.append(blocks[li].register_forward_hook(hook_for(li)))
    try:
        with torch.no_grad():
            model(input_ids=input_ids, use_cache=False)
    finally:
        for h in handles:
            h.remove()

    acts, pooled = {}, {}
    for li in layers:
        h = captured[li][0]                                            # [seq, hidden]
        for sp in spans:
            rows = h[sp.start:sp.end].to("cpu", dtype=torch.float32).contiguous()
            acts[(li, sp.name)] = rows
            if pool:
                pooled[(li, sp.name)] = rows.mean(dim=0) if rows.shape[0] else rows.new_zeros(h.shape[-1])
    return {"acts": acts, "pooled": pooled}


# ---- inputs: run folders (message points) or endpoint token exports (records) ----------------------------
def _select_points(run, agents: Optional[set], rounds: Optional[set]):
    for p in run.points:
        if p.kind != "message":
            continue
        if agents and p.sender not in agents:
            continue
        if rounds and p.round not in rounds:
            continue
        if run.point_tokens(p.id) is not None:
            yield p


def items_from_runs(runs: list, agents: Optional[set] = None, rounds: Optional[set] = None,
                    records: Optional[set] = None) -> list:
    """Run-folder mode: one item per message point with token IDs (load_run's Run.point_tokens)."""
    items = []
    for run in runs:
        for p in _select_points(run, agents, rounds):
            rec = run.point_tokens(p.id)
            if records and rec["record"] not in records:
                continue
            items.append({"source": "run-folder", "run": run.path.name, "agent": p.sender, "record": rec["record"],
                          "round": p.round, "point_id": p.id, "message_text": p.text,
                          "prompt": list(rec["prompt"]), "completion": list(rec["completion"] or [])})
    return items


def parse_tokens_spec(spec: str) -> tuple:
    """'RUN/AGENT=PATH' or 'PATH'. Without a label, the agent (Foxtrot, Keter, Malina, Cringe, Cat; any case) must
    appear in the file name and the run (e.g. 20261108-145133-collusive or A-20261108-154508-collusive) in the file
    name or a parent folder: 20261108-145133-collusive__Keter__round1.json or 20261108-145133-collusive/keter.json."""
    label, sep, path = spec.partition("=")
    if sep and "/" in label:
        run, _, agent = label.partition("/")
        if not run or not agent:
            raise ValueError(f"bad label in {spec!r}: use RUN/AGENT=PATH")
        return path, run, agent
    name = Path(spec).name
    m = RUN_NAME_RE.search(name)
    words = {w.casefold() for w in re.split(r"[^A-Za-z0-9]+", name.replace(m.group(0), " ") if m else name)}
    if not m:                                 # the run may be the folder: <run>/<agent>.json
        m = next((x for x in (RUN_NAME_RE.search(d) for d in reversed(Path(spec).parent.parts)) if x), None)
    found = [a for a in AGENT_NAMES if a.casefold() in words]
    if not m or len(found) != 1:
        raise ValueError(f"cannot tell the run and agent from the file name {name!r} "
                         f"(run found: {m.group(0) if m else None}, agents found: {found}); pass RUN/AGENT={spec}")
    return spec, m.group(0), found[0]


def load_tokens_json(path, run: str, agent: str, rounds: Optional[set] = None, records: Optional[set] = None):
    """Endpoint-export mode: the JSON array from /runs/<run>/agents/<agent>/tokens?full=1 (a dict with a "records"
    list also works). One item per record with prompt_token_ids. Returns (items, skipped) where skipped lists
    (record, reason) for records that cannot be replayed (prompt not reconstructed, length mismatch)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("records")
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected the endpoint's JSON array of token records")
    items, skipped = [], []
    for rec in data:
        if not isinstance(rec, dict) or "record" not in rec:
            raise ValueError(f"{path}: not a token record: {str(rec)[:80]}")
        if rounds and rec.get("round") not in rounds:
            continue
        if records and rec["record"] not in records:
            continue
        prompt, completion = rec.get("prompt_token_ids"), rec.get("completion") or []
        if prompt is None:
            skipped.append((rec["record"], rec.get("reconstruction_error") or "no prompt_token_ids (export with ?full=1)"))
            continue
        if rec.get("prompt_len") is not None and len(prompt) != rec["prompt_len"]:
            skipped.append((rec["record"], f"prompt has {len(prompt)} tokens, prompt_len is {rec['prompt_len']}"))
            continue
        if not all(isinstance(t, int) for t in prompt) or not all(isinstance(t, int) for t in completion):
            raise ValueError(f"{path}: record {rec['record']}: token IDs must be integers")
        items.append({"source": "tokens-json", "run": run, "agent": agent, "record": rec["record"],
                      "round": rec.get("round"), "point_id": None, "message_text": "",
                      "prompt": list(prompt), "completion": list(completion)})
    return items, skipped


# ---- replay -----------------------------------------------------------------------------------------------
def replay_items(items: list, layers: list, out_dir: Path, model=None, tok=None,
                 keep_last_prompt: Optional[int] = None, pool: bool = False, device: str = "cuda",
                 device_map: Optional[str] = None, max_gpu_memory: Optional[str] = None,
                 max_cpu_memory: Optional[str] = None, save: bool = True) -> dict:
    """Replay each item (one forward pass) and write activations + index. Returns the index dict. `model` and
    `tok` are injected (tests pass a tiny model); if None they are loaded with load_model()."""
    if model is None:
        model, tok = load_model(device=device, device_map=device_map, max_gpu_memory=max_gpu_memory,
                                max_cpu_memory=max_cpu_memory)
        if device_map:
            device = "auto"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tensors: dict = {}
    index: list = []
    for it in items:
        prompt_ids, completion_ids = it["prompt"], it["completion"]
        spans = spans_for(it["point_id"] or f"rec{it['record']}", prompt_ids, completion_ids, tok,
                          it["message_text"], keep_last_prompt)
        res = replay_point(model, prompt_ids, completion_ids, layers, spans, pool=pool, device=device)
        ids = list(prompt_ids) + list(completion_ids)
        for sp in spans:
            positions = list(range(sp.start, sp.end))
            for li in layers:
                key = (f"{it['run']}__{it['agent']}__rec{it['record']}"
                       + (f"__{it['point_id']}" if it["point_id"] else "") + f"__L{li}__{sp.name}")
                tensors[key] = res["acts"][(li, sp.name)]
                entry = {"key": key, "run": it["run"], "agent": it["agent"], "record": it["record"],
                         "round": it["round"], "point_id": it["point_id"], "source": it["source"],
                         "layer": li, "span": sp.name, "pooled": False,
                         "token_positions": positions, "token_ids": [ids[i] for i in positions]}
                index.append(entry)
                if pool:
                    pkey = key + "__mean"
                    tensors[pkey] = res["pooled"][(li, sp.name)]
                    index.append({**entry, "key": pkey, "pooled": True})
    result = {"model": MODEL_ID, "revision": REVISION, "layers": layers, "index": index}
    if save:
        _save_tensors(tensors, out_dir)
        (out_dir / "index.json").write_text(json.dumps(result, indent=1))
    result["_tensors"] = tensors
    return result


def run_replay(runs: list, layers: list, out_dir: Path, model=None, tok=None, agents: Optional[set] = None,
               rounds: Optional[set] = None, keep_last_prompt: Optional[int] = None, pool: bool = False,
               device: str = "cuda", save: bool = True) -> dict:
    """Run-folder mode (kept for compatibility): replay the message points of each load_run Run."""
    return replay_items(items_from_runs(runs, agents, rounds), layers, out_dir, model=model, tok=tok,
                        keep_last_prompt=keep_last_prompt, pool=pool, device=device, save=save)


def plan(items: list, layers: list, keep_last_prompt: Optional[int], pool: bool, hidden: int = GEMMA_HIDDEN) -> dict:
    """What a replay would do, without torch: forward passes, longest sequence, stored rows and bytes (float32)."""
    rows = 0
    for it in items:
        p, c = len(it["prompt"]), len(it["completion"])
        rows += c + (min(keep_last_prompt, p) if keep_last_prompt else 0)
    rows *= len(layers)
    nbytes = rows * hidden * 4 + (len(items) * len(layers) * 2 * hidden * 4 if pool else 0)
    return {"forward_passes": len(items),
            "longest_sequence": max((len(i["prompt"]) + len(i["completion"]) for i in items), default=0),
            "total_tokens_fed": sum(len(i["prompt"]) + len(i["completion"]) for i in items),
            "stored_rows": rows, "approx_output_bytes": nbytes}


def _save_tensors(tensors: dict, out_dir: Path) -> str:
    """safetensors if available (preferred), else .npz. Returns the file written."""
    try:
        from safetensors.torch import save_file
        path = out_dir / "activations.safetensors"
        save_file({k: v.contiguous() for k, v in tensors.items()}, str(path))
        return str(path)
    except Exception:
        import numpy as np
        path = out_dir / "activations.npz"
        np.savez(str(path), **{k: v.cpu().numpy() for k, v in tensors.items()})
        return str(path)


def _ints(s: Optional[str]):
    return {int(x) for x in s.split(",") if x.strip()} if s else None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Replay stored token IDs to extract residual-stream activations.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--runs", nargs="+", help="run folders (message points; needs load_run.py)")
    src.add_argument("--tokens-json", nargs="+", metavar="[RUN/AGENT=]FILE",
                     help="endpoint exports of /runs/<run>/agents/<name>/tokens?full=1 (every record)")
    ap.add_argument("--layers", required=True, help="comma-separated decoder layer indices (0-59 for Gemma 4 31B)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--agents", help="comma-separated agent names to include")
    ap.add_argument("--rounds", help="comma-separated round numbers to include")
    ap.add_argument("--records", help="comma-separated record numbers to include")
    ap.add_argument("--keep-last-prompt", type=int, default=None, help="also keep the last N prompt tokens")
    ap.add_argument("--pool", action="store_true", help="also save a mean-pooled vector per span")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--device-map", choices=["auto"],
                    help="GPU(s) + CPU offload (needs accelerate): bf16 weights placed within the caps below")
    ap.add_argument("--max-gpu-memory", help="with --device-map auto: cap per GPU, e.g. 28GiB")
    ap.add_argument("--max-cpu-memory", help="with --device-map auto: cap for CPU RAM, e.g. 70GiB")
    ap.add_argument("--dry-run", action="store_true", help="read the inputs and print the plan; no model, no torch")
    a = ap.parse_args(argv)
    if (a.max_gpu_memory or a.max_cpu_memory) and a.device_map != "auto":
        ap.error("--max-gpu-memory / --max-cpu-memory need --device-map auto")
    layers = [int(x) for x in a.layers.split(",")]
    agents, rounds, records = (set(a.agents.split(",")) if a.agents else None), _ints(a.rounds), _ints(a.records)
    if a.runs:
        from load_run import load_run
        items = items_from_runs([load_run(p) for p in a.runs], agents, rounds, records)
    else:
        items = []
        for spec in a.tokens_json:
            path, run, agent = parse_tokens_spec(spec)
            if agents and agent not in agents:
                continue
            got, skipped = load_tokens_json(path, run, agent, rounds, records)
            print(f"{path}: run {run}, agent {agent}: {len(got)} records to replay"
                  + (f", {len(skipped)} skipped: {skipped[:3]}" if skipped else ""))
            items += got
    pl = plan(items, layers, a.keep_last_prompt, a.pool)
    print("plan:", json.dumps(pl))
    if a.dry_run:
        return
    res = replay_items(items, layers, Path(a.out), keep_last_prompt=a.keep_last_prompt, pool=a.pool,
                       device=a.device, device_map=a.device_map, max_gpu_memory=a.max_gpu_memory,
                       max_cpu_memory=a.max_cpu_memory)
    print(f"wrote {len(res['index'])} vectors from {len(items)} forward passes to {a.out}")


if __name__ == "__main__":
    main()
