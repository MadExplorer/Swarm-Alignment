# Activation replay kit (Gemma 4 31B, swarm sandbox)

Extracts residual-stream activations by replaying the **exact token IDs** our vLLM produced, with one transformers forward pass per model call. It runs on **any machine with a big enough GPU**. It **never needs our vLLM, our server or any connection to the sandbox**: your endpoint exports contain everything. The only download is the public model from Hugging Face (about 22 GB).

| File | What |
|---|---|
| `replay_activations.py` | the script (standard library + torch/transformers; no other repo files needed in `--tokens-json` mode) |
| `requirements-replay.txt` | pinned versions, with what is verified and what is not |
| `replay-activations.md` | full documentation: modes, output format, GPU memory, caveats |

## 1. Install

```bash
python3 -m venv venv && . venv/bin/activate          # Python 3.11 or 3.12
pip install -r requirements-replay.txt
# if `nvidia-smi` shows CUDA Version below 13.0, use the CUDA 12.6 build of torch instead:
#   pip install torch==2.14.0 --index-url https://download.pytorch.org/whl/cu126
```

## 2. Your files

Your files are the endpoint exports of `/runs/<run>/agents/<name>/tokens?full=1` (round 1, five agents, collusive and honest runs). Each is a JSON array of records with `prompt_token_ids` and `completion`. The script needs to know the run and the agent of each file:
- **Name contains both** (agent name in any case; run name in the file name or its folder), e.g. `20261108-145133-collusive__Keter__round1.json` or `20261108-145133-collusive/keter.json`: pass the files as they are.
- **Otherwise**, label each file: `--tokens-json <RUN>/<AGENT>=<file>`, e.g. `20261108-145133-collusive/Keter=keter_r1.json`.

## 3. Run (RTX 5090 32 GB + ~82 GiB free RAM: GPU + CPU offload)

The model in bf16 (~62.5 GB) does not fit on a 32 GB GPU, so use `--device-map auto` with memory caps:
- the weights are dequantized to bf16 **on the CPU at load time** (~62.5 GB RAM);
- accelerate then puts as many decoder layers on the GPU as fit under `--max-gpu-memory`, and keeps the rest in RAM, moving each of them to the GPU for its part of every forward pass.

Leave the GPU some headroom for activations (28 of 32 GB). Do not count on the 8 GiB swap.

The script runs the decoder only, with no LM head, so no full-vocabulary logits are computed. Those caused the CUDA out-of-memory error on an 8,055-token record. Memory still grows with the prompt length (attention and the per-layer activations of the whole sequence). For very long records, lower `--max-gpu-memory` (e.g. `24GiB`): more layers then stay in CPU RAM and the GPU keeps more headroom. Check `longest_sequence` in the dry run.

```bash
# 1) dry run: reads all files, lists records, prints the plan; no torch, no GPU
python replay_activations.py --tokens-json exports/*.json --layers 20,40 --keep-last-prompt 128 \
    --out acts --dry-run
# 2) smoke test: 2 records of one agent (loads the model once, ~10-20 min before the first forward)
python replay_activations.py --tokens-json exports/*.json --agents Keter --records 1,2 --layers 20,40 \
    --keep-last-prompt 128 --device-map auto --max-gpu-memory 28GiB --max-cpu-memory 70GiB --out acts-smoke
# 3) everything in one invocation (the model is loaded once):
python replay_activations.py --tokens-json exports/*.json --layers 20,40 --keep-last-prompt 128 \
    --device-map auto --max-gpu-memory 28GiB --max-cpu-memory 70GiB --out acts
```

- The script prints the device map (how many modules are on `0` and on `cpu`) after loading.
- If `approx_output_bytes` in the dry run is above ~5 GB, split the work by run (`exports/<RUN>__*.json`, one `--out` per run). The activations are kept in RAM next to the model until the end.
- `--layers`: decoder layers 0–59; `20,40` is a small example choice.
- `--keep-last-prompt 128` keeps the last 128 prompt tokens as well as the completion.

**Rough time (a guess, not measured):**
- Loading plus CPU dequantization: about 10–20 min.
- Each forward pass moves the CPU-resident part of the weights, about 30 GB, over PCIe (a few seconds), plus compute over the whole sequence (up to about 10–20k tokens). That is roughly **5–15 s per record**.
- Round 1 has about 20–40 records per agent, so 5 agents × your runs (`forward_passes` in the dry run) is a few hundred records: **on the order of 0.5–1.5 h**.
- Time the smoke test and scale by `total_tokens_fed`.

## 4. Output

`acts/<RUN>/activations.safetensors` holds float32 tensors `[n_tokens, 5376]`, and `[5376]` for pooled ones. `acts/<RUN>/index.json` maps every tensor key to `run`, `agent`, `record`, `round`, `span` (`completion` / `prompt_tail`), `layer`, `token_positions` (in prompt + completion) and `token_ids`. So each vector maps back to (run, agent, record, token position). There's an example loader in `replay-activations.md`.

## 5. GPU memory: why offload

- **The 4-bit model cannot stay compressed during the forward pass** with transformers 5.20.2 + compressed-tensors 0.17.0. `run_compressed` is deprecated and only means `dequantize=False`, under which compressed-tensors decompresses the whole model on the first forward pass. `CompressedLinear` raises "no longer supported", and the only kernel path (`use_optimized_inference`) covers W8A8 FP8. Checked in the installed source.
- **In bf16 the model is 31.27 B parameters, about 62.5 GB** (estimate from the parameter count, not measured). So you need one 80 GB GPU, or a smaller GPU with CPU offload (section 3).
- **Not confirmed:** we could **not confirm** that compressed-tensors' first-forward decompression works on accelerate-offloaded modules. Its `decompress_module` reads `module._parameters` directly, which accelerate replaces with `meta` tensors on offloaded modules. compressed-tensors has its own conversion (`load_offloaded_model` / `from_accelerate`), but transformers does not use it. The script therefore **does not rely on it**: it dequantizes on the CPU at load time and only then lets accelerate place and offload the plain bf16 model.
- **Why not plain `from_pretrained(device_map="auto")`:** transformers would size the map from the compressed parameters (about 22 GB), put everything on the GPU and then decompress there.

## 6. What is verified

**Verified:**
- The token IDs in endpoint exports are identical to the run-folder mode, checked on a real evidence run.
- Both modes give identical activations on a tiny model.
- The real Gemma 4 config builds with these versions, and the 60 hook targets are found.

**Not verified:** loading the real weights and running a forward pass on a GPU (we have not done it); the offload path with the real model (tested only on a tiny model on CPU, where it gives the same activations as without offload).

Activations are close to, but not bit-identical with, vLLM's internal ones: same weights and token IDs, but different kernels.
