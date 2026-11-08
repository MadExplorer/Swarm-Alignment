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

## 3. Run

First a dry run. It reads every file, reports records and skipped records, and prints the plan: forward passes, longest sequence, approximate output size. It needs no GPU and no torch.

```bash
python replay_activations.py --tokens-json exports/<RUN>__*.json --layers 15,30,45 \
    --keep-last-prompt 256 --pool --out acts/<RUN> --dry-run
```

Then the real run, **one run per output folder**. Activations stay in RAM until the end, and the dry run prints how much.

```bash
# one 80 GB GPU:
python replay_activations.py --tokens-json exports/<RUN>__*.json --layers 15,30,45 \
    --keep-last-prompt 256 --pool --out acts/<RUN> --device cuda
# several smaller GPUs (untested with this model, needs accelerate):
python replay_activations.py --tokens-json exports/<RUN>__*.json --layers 15,30,45 \
    --keep-last-prompt 256 --pool --out acts/<RUN> --device-map auto
```

- `--layers`: decoder layers 0–59 (Gemma 4 31B has 60). `15,30,45` is only an example.
- `--keep-last-prompt N` keeps the last N prompt tokens as well as the completion. `--pool` adds a mean vector per span.
- To start small: `--agents Keter --records 1,2`.

## 4. Output

`acts/<RUN>/activations.safetensors` holds float32 tensors `[n_tokens, 5376]`, and `[5376]` for pooled ones. `acts/<RUN>/index.json` maps every tensor key to `run`, `agent`, `record`, `round`, `span` (`completion` / `prompt_tail`), `layer`, `token_positions` (in prompt + completion) and `token_ids`. So each vector maps back to (run, agent, record, token position). There's an example loader in `replay-activations.md`.

## 5. GPU memory (read before choosing a machine)

- Checked in the installed source (transformers 5.20.2 + compressed-tensors 0.17.0): the 4-bit checkpoint is **decompressed to bf16 on the first forward pass** and stays that way.
- The model has 31.27 B parameters, so the weights take **about 62.5 GB**. That is our estimate, **not measured**.
- Add the forward pass on top: sequences up to ~20k tokens, roughly 0.2 GB per layer for the residual stream plus transient buffers.
- So you need **one 80 GB GPU** (A100 80GB / H100), or several GPUs with `--device-map auto`. **A single 24 GB or 32 GB GPU is not enough.**
- Start with the shortest records and watch `nvidia-smi`.

## 6. What is verified

**Verified:**
- The token IDs in endpoint exports are identical to the run-folder mode, checked on a real evidence run.
- Both modes give identical activations on a tiny model.
- The real Gemma 4 config builds with these versions, and the 60 hook targets are found.

**Not verified:** loading the real weights and running a forward pass on a GPU (we have not done it); `--device-map auto` with this model.

Activations are close to, but not bit-identical with, vLLM's internal ones: same weights and token IDs, but different kernels.
