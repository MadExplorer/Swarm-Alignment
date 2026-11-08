# Extracting activations (`analysis/replay_activations.py`)

Residual-stream activations for the agents, taken **offline** by replaying the exact token IDs vLLM produced.

## Why offline

vLLM 0.33.0's OpenAI server has **no option to return per-token hidden states or residual-stream activations** for a generate model. The chat/completions API returns token IDs and logprobs only (we already capture the token IDs with `return_token_ids`). The only hidden-state-adjacent server feature is the pooling / embeddings API (`/v1/embeddings`, `/pooling`, `token_embed`), which (1) requires the server to run as a *pooling* runner — a restart, which we will not do — and (2) returns only a final pooled or token embedding, not arbitrary intermediate layers. `output_hidden_states` exists only inside a few multimodal model implementations in vLLM, not as a server option. So activations are extracted here, by one forward pass through the model with transformers over the **stored** token IDs, never re-tokenized text.

## What it does

- Loads `google/gemma-4-31B-it-qat-w4a16-ct` at revision `52f3f65bc7a02d555763bc923bd1d9094898219d` with transformers (needs the **`compressed-tensors`** package for the w4a16 weights). Eval mode, `no_grad`, **no generation**.
- For each selected point (message), `load_run`'s `Run.point_tokens()` gives the record's `prompt` and `completion` token IDs. The module runs **one forward pass** over `prompt + completion`, batch size 1.
- Forward hooks on the decoder layers you choose capture each layer's **residual stream** (its raw output, pre the final norm). For every point it keeps the activations of:
  - the **completion** span (the model's output tokens, which contain the tool call);
  - the **message text** span inside the tool call, located by decoding the completion tokens and string-matching (best-effort; skipped if not found);
  - optionally a **prompt tail** (`--keep-last-prompt N`), to bound memory on ~20k-token prompts.
- With `--pool`, it also saves a **mean-pooled** vector per span.

## Output

Two files in `--out`:
- `activations.safetensors` (or `activations.npz` if safetensors is absent) — one tensor per `(point, layer, span)`, shape `[n_tokens, hidden]` (and `[hidden]` for pooled).
- `index.json` — for every tensor: `run`, `agent`, `record`, `point_id`, `layer`, `span`, `token_positions` (absolute positions in `prompt + completion`), and `token_ids` (the stored IDs at those positions). So **every vector maps back to its token**; `tests/test_replay_activations.py` checks this alignment on a tiny model.

## Running it

```bash
pip install torch transformers compressed-tensors safetensors
cd analysis
python replay_activations.py --runs ../evidence/runs/20261107-174002-collusive \
    --layers 12,18,24 --out acts --pool
# narrow the work:
python replay_activations.py --runs RUN --layers 18 --agents Keter,Malina --rounds 2,3 \
    --keep-last-prompt 256 --out acts --device cuda
```

Load a tensor and its tokens:

```python
import json, safetensors.torch as st
idx = {e["key"]: e for e in json.load(open("acts/index.json"))["index"]}
t = st.load_file("acts/activations.safetensors")
key = next(k for k, e in idx.items() if e["span"] == "completion" and e["layer"] == 18)
vecs, e = t[key], idx[key]            # vecs[i] is the layer-18 residual stream at token e["token_ids"][i]
```

## GPU requirements

- The model is 31B parameters at **w4a16** (4-bit weights): roughly **16–18 GB** of weights, plus activations/KV for the forward pass.
- **Estimate, not measured:** a single **RTX 4090 (24 GB)** should fit the weights and a ~20k-token forward at batch 1, but we have **not** benchmarked peak memory for this model at that length — treat it as a starting guess, not a guarantee. **Start with short prompts** (a few hundred tokens) or `--keep-last-prompt`, watch `nvidia-smi`, and scale up; a long forward may need more headroom (or a second GPU) than one 4090. Use a small `--layers` set to bound what is stored (the forward still processes the full sequence, but only the kept spans are moved to CPU).
- `--device cpu` works for small inputs and the tests, but a full 20k-token forward on CPU is very slow.
- **Do not run this on the harness server** — it would load the full model onto the GPUs that vLLM is using and compete with the running sandbox. Run it on a separate box, or when vLLM is not in use, with the team lead's agreement.

## Notes

- The replay uses the stored token IDs verbatim; it never re-tokenizes text, so the activations match what vLLM actually ran (the chat template and quantized weights are the same).
- For the **last** layer, the hook's residual stream is the pre-final-norm hidden state, which differs from transformers' `output_hidden_states[-1]` (post-norm). Use the hook output for a consistent residual-stream definition across layers.
