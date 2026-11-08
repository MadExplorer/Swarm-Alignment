# Extracting activations (`analysis/replay_activations.py`)

Residual-stream activations for the agents, taken **offline** by replaying the exact token IDs vLLM produced.

## Where it runs: any machine with a big enough GPU, never our vLLM

Replay is a plain transformers forward pass over stored token IDs. It **never needs our vLLM, our server, or network access to the sandbox**. Copy the token exports (or run folders) to any machine with a large enough GPU (see "GPU requirements"), install the requirements, and run it there. The only download is the model itself from Hugging Face (`google/gemma-4-31B-it-qat-w4a16-ct` at the pinned revision, public and ungated, about 22 GB).

Do **not** run it on the sandbox server: it would load the full model onto the GPUs vLLM is using.

## Why offline

vLLM 0.33.0's OpenAI server has **no option to return per-token hidden states or residual-stream activations** for a generate model. The chat/completions API returns token IDs and logprobs only (we already capture the token IDs with `return_token_ids`). The only hidden-state-adjacent server feature is the pooling / embeddings API (`/v1/embeddings`, `/pooling`, `token_embed`). It (1) requires the server to run as a *pooling* runner, which means a restart that we will not do, and (2) returns only a final pooled or token embedding, not arbitrary intermediate layers. `output_hidden_states` exists only inside a few multimodal model implementations in vLLM, not as a server option. So activations are extracted here, by one forward pass through the model with transformers over the **stored** token IDs, never re-tokenized text.

## Two input modes, same output

| Mode | Input | What is replayed | Needs |
|---|---|---|---|
| `--tokens-json FILE ...` | Exports of the read-only endpoint's `/runs/<run>/agents/<name>/tokens?full=1` (optionally `&round=N`): a JSON array of records with `record`, `round`, `prompt_len`, `prompt_token_ids` (the full prompt) and `completion` | **every record**, i.e. every model call of that agent (one forward pass each) | only this script |
| `--runs RUN ...` | run folders | every **message point** (`send`/`broadcast`) that has token IDs, via `load_run`'s `Run.point_tokens()`; also locates the message text inside the completion (`message` span) | `load_run.py` next to the script |

- **Run and agent of an export** are taken from the file name: the agent name (Foxtrot, Keter, Malina, Cringe, Cat; any case) must be in the file name, and the run name (e.g. `20261108-145133-collusive`, `A-20261108-154508-collusive`) in the file name or a parent folder. Examples: `20261108-145133-collusive__Keter__round1.json`, `20261108-145133-collusive/keter.json`. Otherwise label it: `--tokens-json 20261108-145133-collusive/Keter=keter.json`.
- Records the endpoint could not reconstruct (`prompt_token_ids: null`, or a length that does not match `prompt_len`) are skipped and listed.
- The token IDs are identical in both modes: `tests/test_replay_activations.py` exports round 1 of an evidence run through the real endpoint code and checks every message point's prompt and completion against the run-folder mode, and that both modes give identical activations (tiny model).

## What it does

- Loads `google/gemma-4-31B-it-qat-w4a16-ct` at revision `52f3f65bc7a02d555763bc923bd1d9094898219d` with transformers (needs **`compressed-tensors`** for the w4a16 weights). Eval mode, `no_grad`, **no generation**.
- **One forward pass** over `prompt + completion` per record or point, batch size 1, the stored IDs fed verbatim. Only the decoder runs (`model.base_model`, i.e. `Gemma4Model`), not the LM head, so no full-vocabulary logits are computed. The activations are identical to the full forward (tested).
- Forward hooks on the chosen decoder layers capture each layer's **residual stream** (its raw output, pre the final norm). Gemma 4 31B has **60 decoder layers (indices 0–59), hidden size 5376**; the hook target is `model.language_model.layers` of `Gemma4ForConditionalGeneration` (checked on the real config, meta device). Kept spans:
  - **completion**: the model's output tokens (the tool call);
  - **message** (run-folder mode only): the message text inside the tool call, found by decoding the completion tokens and string-matching (best-effort; skipped if not found);
  - **prompt_tail** (`--keep-last-prompt N`): the last N prompt tokens.
- `--pool` also saves a **mean-pooled** vector per span.
- `--dry-run` reads the inputs and prints the plan (forward passes, longest sequence, rows and approximate output bytes) without torch or a model.

## Output

Two files in `--out`:
- `activations.safetensors` (or `activations.npz` if safetensors is absent): one float32 tensor per `(record or point, layer, span)`, shape `[n_tokens, hidden]` (and `[hidden]` for pooled).
- `index.json`: `model`, `revision`, `layers`, and for every tensor `key`, `run`, `agent`, `record`, `round`, `point_id` (null in `--tokens-json` mode), `source` (`tokens-json` / `run-folder`), `layer`, `span`, `pooled`, `token_positions` (absolute positions in `prompt + completion`) and `token_ids` (the stored IDs at those positions). So **every vector maps back to (run, agent, record, token position)**.

All tensors are kept in RAM until the end, then written once; `--dry-run` prints the size (`approx_output_bytes`). Run one run (or one agent) per `--out` folder if that is large.

## Running it

```bash
pip install -r requirements-replay.txt
# endpoint exports (one file per run and agent):
python replay_activations.py --tokens-json exports/*.json --layers 15,30,45 --keep-last-prompt 256 --pool \
    --out acts --dry-run                 # check files, sizes; no GPU
python replay_activations.py --tokens-json exports/*.json --layers 15,30,45 --keep-last-prompt 256 --pool \
    --out acts --device cuda
# narrow the work: --agents Keter,Malina --rounds 1 --records 3,7
# run folders (needs load_run.py):
python replay_activations.py --runs RUN_DIR --layers 18 --rounds 2,3 --keep-last-prompt 256 --out acts
```

Load a tensor and its tokens:

```python
import json, safetensors.torch as st
idx = {e["key"]: e for e in json.load(open("acts/index.json"))["index"]}
t = st.load_file("acts/activations.safetensors")
key = next(k for k, e in idx.items() if e["span"] == "completion" and e["layer"] == 30 and not e["pooled"])
vecs, e = t[key], idx[key]            # vecs[i] is the layer-30 residual stream at token e["token_ids"][i]
```

## GPU requirements

Facts (checked in the installed source and on the model's config, not on a GPU run):
- The checkpoint is **22 GB** on disk (`model.safetensors`, compressed-tensors `pack-quantized` w4a16, plus bf16 embeddings and the vision tower).
- The model has **31.27 B parameters** (`Gemma4ForConditionalGeneration`; 30.70 B in the language model).
- With transformers 5.20.2 and compressed-tensors 0.17.0, a compressed checkpoint loads compressed, but compressed-tensors registers a hook that **decompresses the whole model to bf16 on the first forward pass** (`ModelCompressor.decompress_model`) and keeps it that way. There is no 4-bit kernel path in transformers for this format.

So, an **estimate (not measured)**: about **62.5 GB for the weights** in bf16, plus the forward pass.
- **Not supported: keeping the model compressed.** transformers 5.20.2 + compressed-tensors 0.17.0 have no compressed forward for W4A16:
  - `run_compressed` is deprecated and only sets `dequantize=False`;
  - `apply_quantization_config`'s `run_compressed` argument is unused;
  - `CompressedLinear` raises "no longer supported";
  - `use_optimized_inference` covers only W8A8 FP8.
- **One 80 GB GPU** (A100 80GB, H100) is the simple case.
- **A smaller GPU plus CPU RAM:** `--device-map auto --max-gpu-memory 28GiB --max-cpu-memory 70GiB` (needs `accelerate`). The script loads the model on the CPU with the weights **dequantized to bf16 at load time** (`CompressedTensorsConfig(dequantize=True)`, about 62.5 GB RAM). Then `accelerate.infer_auto_device_map` sizes the map from the bf16 parameters within the caps, and `dispatch_model` keeps the layers that do not fit on the GPU in RAM, moving them to the GPU for each forward pass.
  - It does not use `from_pretrained(device_map="auto")`, because transformers sizes that map from the *compressed* parameters (`quantizers/base.py`: `param_element_size` returns `param.element_size()`, and the compressed-tensors quantizer does not override it). That would put everything on the GPU and then decompress there on the first forward pass.
  - We could **not confirm** that compressed-tensors' first-forward decompression works on accelerate-offloaded modules (`decompress_module` reads `module._parameters`, which are `meta` tensors under accelerate offload; compressed-tensors' own `load_offloaded_model`/`from_accelerate` conversion is not used by transformers). So the script avoids it.
  - Tested on a tiny model on CPU (same activations as without offload); **not run with the real model**.
- **The forward pass:** one sequence of up to ~20k tokens (check `longest_sequence` with `--dry-run`). A residual-stream tensor is 20k × 5376 × 2 bytes ≈ 0.2 GB per layer, plus larger transient MLP buffers; attention uses PyTorch SDPA. Leave a few GB of GPU headroom (hence 28 of 32 GB) and start with a few records (`--records`).
- **CPU RAM:** about 62.5 GB for the bf16 model in offload mode (22 GB otherwise), plus the kept activations (`approx_output_bytes`).
- `--device cpu` works for the tests and tiny inputs; a 31B forward over thousands of tokens on CPU is impractically slow (and needs ~65 GB RAM).

## Notes

- The replay uses the stored token IDs verbatim; it never re-tokenizes text, so the model sees exactly the tokens vLLM ran on.
- The numbers are close to, but **not bit-identical with**, vLLM's internal activations. vLLM ran the same quantized weights with its own w4a16 kernels and tensor parallelism (TP=4 on RTX 4090 for runs up to 20261108-043719, TP=2 on RTX 5090 after that); here the weights are dequantized to bf16 and run with standard PyTorch kernels.
- For the **last** layer, the hook's residual stream is the pre-final-norm hidden state, which differs from transformers' `output_hidden_states[-1]` (post-norm). Use the hook output for a consistent residual-stream definition across layers.
