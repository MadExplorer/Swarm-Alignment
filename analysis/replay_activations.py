"""Replay stored vLLM token IDs through the model with transformers to extract residual-stream activations.

Analysis only; never run on the server (it loads the full model on a GPU). vLLM 0.33 exposes no server option
for per-token hidden states (only pooled embeddings, and only for a pooling runner), so activations are taken
OFFLINE here by replaying the EXACT token IDs vLLM produced (never re-tokenized text), one forward pass per point.

    python replay_activations.py --runs RUN [RUN ...] --layers 12,18,24 --out acts \
        [--agents Keter,Malina] [--rounds 2,3] [--keep-last-prompt 256] [--pool] [--device cuda]

Output (one file pair in --out):
    activations.safetensors (or .npz)  tensors, one per (point, layer, span)
    index.json                         every tensor's key -> {run, agent, record, point_id, layer, span,
                                       token_positions, token_ids}, so each vector maps back to its token.

Model: google/gemma-4-31B-it-qat-w4a16-ct @ 52f3f65bc7a02d555763bc923bd1d9094898219d (compressed-tensors, w4a16;
needs the `compressed-tensors` package). See docs/replay-activations.md for GPU requirements.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from load_run import Run, load_run  # noqa: E402

MODEL_ID = "google/gemma-4-31B-it-qat-w4a16-ct"
REVISION = "52f3f65bc7a02d555763bc923bd1d9094898219d"


# ---- model loading (heavy deps imported lazily, so the module imports without torch) -----------------------
def load_model(model_id: str = MODEL_ID, revision: Optional[str] = REVISION, device: str = "cuda",
               dtype: str = "auto"):
    """Load the model and tokenizer for a forward pass only (eval, no grad, no generation). `model_id` may be a
    local path (tests use a tiny model), in which case pass revision=None."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model_id, revision=revision)
    torch_dtype = dtype if dtype == "auto" else getattr(torch, dtype)
    model = AutoModelForCausalLM.from_pretrained(model_id, revision=revision, torch_dtype=torch_dtype)
    model.to(device)
    model.eval()
    return model, tok


def decoder_layers(model):
    """The list of decoder blocks (residual-stream carriers). Gemma/Llama: model.model.layers."""
    for path in ("model.layers", "model.model.layers", "transformer.h", "gpt_neox.layers"):
        obj = model
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    raise AttributeError("could not find the decoder layer list on this model")


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


# ---- driver over run folders ------------------------------------------------------------------------------
def _select_points(run: Run, agents: Optional[set], rounds: Optional[set]):
    for p in run.points:
        if p.kind != "message":
            continue
        if agents and p.sender not in agents:
            continue
        if rounds and p.round not in rounds:
            continue
        if run.point_tokens(p.id) is not None:
            yield p


def run_replay(runs: list, layers: list, out_dir: Path, model=None, tok=None, agents: Optional[set] = None,
               rounds: Optional[set] = None, keep_last_prompt: Optional[int] = None, pool: bool = False,
               device: str = "cuda", save: bool = True) -> dict:
    """Replay the selected points of each run and write activations + index. Returns the index dict. `model`
    and `tok` are injected (tests pass a tiny model); if None they are loaded with load_model()."""
    if model is None:
        model, tok = load_model(device=device)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tensors: dict = {}
    index: list = []
    for run in runs:
        for p in _select_points(run, agents, rounds):
            rec = run.point_tokens(p.id)
            prompt_ids, completion_ids = rec["prompt"], rec["completion"] or []
            spans = spans_for(p.id, prompt_ids, completion_ids, tok, p.text, keep_last_prompt)
            res = replay_point(model, prompt_ids, completion_ids, layers, spans, pool=pool, device=device)
            ids = list(prompt_ids) + list(completion_ids)
            for sp in spans:
                positions = list(range(sp.start, sp.end))
                for li in layers:
                    key = f"{run.path.name}__{p.sender}__rec{rec['record']}__{p.id}__L{li}__{sp.name}"
                    tensors[key] = res["acts"][(li, sp.name)]
                    entry = {"key": key, "run": run.path.name, "agent": p.sender, "record": rec["record"],
                             "point_id": p.id, "layer": li, "span": sp.name, "pooled": False,
                             "token_positions": positions, "token_ids": [ids[i] for i in positions]}
                    index.append(entry)
                    if pool:
                        pkey = key + "__mean"
                        tensors[pkey] = res["pooled"][(li, sp.name)]
                        index.append({**entry, "key": pkey, "pooled": True,
                                      "token_positions": positions, "token_ids": [ids[i] for i in positions]})
    result = {"model": MODEL_ID, "revision": REVISION, "layers": layers, "index": index}
    if save:
        _save_tensors(tensors, out_dir)
        (out_dir / "index.json").write_text(json.dumps(result, indent=1))
    result["_tensors"] = tensors
    return result


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
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--layers", required=True, help="comma-separated decoder layer indices, e.g. 12,18,24")
    ap.add_argument("--out", required=True)
    ap.add_argument("--agents", help="comma-separated agent names to include")
    ap.add_argument("--rounds", help="comma-separated round numbers to include")
    ap.add_argument("--keep-last-prompt", type=int, default=None, help="keep only the last N prompt tokens")
    ap.add_argument("--pool", action="store_true", help="also save a mean-pooled vector per span")
    ap.add_argument("--device", default="cuda")
    a = ap.parse_args(argv)
    runs = [load_run(p) for p in a.runs]
    res = run_replay(runs, [int(x) for x in a.layers.split(",")], Path(a.out),
                     agents=(set(a.agents.split(",")) if a.agents else None),
                     rounds=_ints(a.rounds), keep_last_prompt=a.keep_last_prompt, pool=a.pool, device=a.device)
    print(f"wrote {len(res['index'])} vectors from {len(runs)} run(s) to {a.out}")


if __name__ == "__main__":
    main()
