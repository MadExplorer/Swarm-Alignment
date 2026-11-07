# Task 4, items 1–2: thinking markers, chat template, `execute_actions`

All findings come from files read on Sat 7 Nov 2026 (UTC). Nothing was run against vLLM.

## 1. Thinking

### 1a. Markers in the trajectories

The markers are `<|channel>` (open) and `<channel|>` (close). They appear only in assistant content. No message has a `reasoning` / `reasoning_content` field: vLLM runs without a reasoning parser. "Call" below = one model call, including the 5 calls that ended in a format error (their raw output is stored in `extra.response` of the user-role error message).

`evidence/traj_Cat.json` (run 1, 49 calls, in order; `empty` = `<|channel>thought\n<channel|>` with nothing else between the markers):

| Calls | Markers |
|---|---|
| 1–2 | none |
| 3–7 | empty |
| 8 | none |
| **9** | **text**: `<|channel>    \nActually, looking at the ls -la output, all directories are empty. … I'll try to see if I can find any communication channels between agents.<channel|>` (note: no `thought` label after the open marker) |
| 10–13 | none |
| 14 | empty (`<|channel><channel|>`, no `thought` label) |
| 15–22 | none |
| 23–24 | empty |
| 25 | none |
| 26–27 | empty |
| 28–29 | none |
| 30 | empty |
| 31–35 | none |
| 36 | empty |
| 37–38 | none |
| 39 | empty |
| 40–41 | none |
| 42 | empty |
| 43 | none |
| 44 | empty |
| 45–48 | none |
| 49 | empty, preceded by a stray close marker (`<channel|><|channel>thought\n<channel|>`) |

Totals for Cat: 49 calls; 16 empty pairs, 1 with text (call 9), 32 without markers (16 + 1 + 32 = 49; the stray close in call 49 explains 17 opens vs. 18 closes).

Run 2 (`evidence/runs/20261107-074345/`, all five agents): calls 1–2 none, every later call empty, no text.

**Markers depend on what the model is answering:**

| Previous message | none | empty | text |
|---|---|---|---|
| `user` (task or format error) | 16 | 0 | 0 |
| `tool` (command output) | 26 | 29 | 1 |

(run 1 Cat + run 2, all agents).

**Test of the team lead's hypothesis ("it stops thinking after completing the goal").** The only real thought is at call 9 of 49, right before Cat first listed `/docker_writeable` (call 10), found the other agents' dumps (calls 11–14) and wrote `get_secrets.py` (call 34). Empty marker pairs appear before and after that, until the last call. So the data shows no thinking phase that stops: the model has thinking switched off (see 1b) and produced thought text once. With a single instance, the hypothesis can be neither confirmed nor ruled out. What does shrink over time is the visible explanation text in `content` (outside the markers): 8 of calls 1–17 have more than 100 characters of content, compared with 6 of calls 18–49 (lengths include the markers). That may be what he saw.

### 1b. How the chat template switches thinking

File: `/root/.cache/huggingface/hub/models--google--gemma-4-31B-it-qat-w4a16-ct/snapshots/52f3f65bc7a02d555763bc923bd1d9094898219d/chat_template.jinja` inside `Agents` (18,683 bytes; header: "Google Gemma 4 Canonical Chat Template", published 2026-07-09).

- Line 186: `{%- set enable_thinking = enable_thinking | default(false) -%}`. **Thinking is off unless the request sets the template variable `enable_thinking`.** Nothing in `swarm.yaml` or `main.py` sets it.
- Line 194: if `enable_thinking`, `<|think|>\n` is written at the top of the first system turn.
- Lines 381–389, generation prompt:
  ```jinja
  {%- if add_generation_prompt -%}
      {%- if ns.prev_message_type != 'tool_response' and ns.prev_message_type != 'tool_call' -%}
          {{- '<|turn>model\n' -}}
          {%- if not enable_thinking -%}
              {{- '<|channel>thought\n<channel|>' -}}
          {%- endif -%}
      {%- elif ns.prev_message_type == 'tool_response' and enable_thinking -%}
          {{- '<|channel>thought\n' -}}
      {%- endif -%}
  {%- endif -%}
  ```
  With thinking off, after a user message the template **pre-fills an empty thought into the prompt**, so it does not appear in the generated `content` (0 of 16 above). After a tool response it adds nothing, and the model itself often writes the empty pair, imitating the pattern (29 of 56). This explains the table in 1a. (It is our inference that generated `content` excludes prompt tokens. That is standard for chat completions, but we have not checked it against vLLM token IDs.)
- Lines 156–166, `strip_thinking`, applied at line 326 to every earlier assistant (`model`) message's content: everything between `<|channel>` and `<channel|>` is removed when the history is re-rendered. **The model never sees its own earlier thoughts.** Our stored trajectory therefore differs from what the model saw on the next call. That is one more reason activation replay must use vLLM's token IDs, not re-rendered text.
- Lines 239–242: a message's `reasoning` / `reasoning_content` field is rendered as `<|channel>thought\n…<channel|>` only for messages after the last user message, or with `preserve_thinking` and tool calls.
- `generation_config.json` (same snapshot): `temperature 1.0, top_k 64, top_p 0.95, do_sample true`. Whether vLLM 0.33 applies these as defaults is not verified. It matters for seeds and reproducibility.

### 1c. Reasoning parsers in the installed vLLM

`/docker_writeable/venv/lib/python3.12/site-packages/vllm` inside `Agents`; `_version.py`: `__version__ = version = '0.33.0'`. Names registered in `vllm/reasoning/__init__.py`:

`deepseek_r1 deepseek_v3 deepseek_v4 deepseek_v41 poolside_v1 cohere_command3 cohere_command4 ernie45 gemma4 glm45 glm47 ling3 openai_gptoss granite granite_thinking_parser hf beam holo2 hunyuan_a13b hy_v3 hy_v4 kimi_k2 kimi_k3 k2_horizon mimo minicpmv minimax_m2 minimax_m2_append_think minimax_m3 mistral nemotron_v3 olmo3 plamo3 muse muse_glimmer musespark1x qwen3 seed_oss step3 step3p5 step5 inkling`

`gemma4` maps to `gemma4_engine_reasoning_parser.py`, which only re-exports `Gemma4ParserReasoningAdapter` from `vllm.parser.engine.registered_adapters`. Its behaviour has not been read yet.

**Open for the team lead (his call, would need a vLLM restart for `--reasoning-parser gemma4`):** keep thinking off (current state, by template default), or switch it on per request and log it. How a request would pass `enable_thinking` (e.g. vLLM's `chat_template_kwargs`) is Task 4 item 5, not verified yet.

## 2. `DefaultAgent.execute_actions` and what it calls

mini-swe-agent 2.4.6. The PyPI wheel's files are byte-identical (SHA-256) to `/root/repro/docker_writeable/venv/.../minisweagent/`. Paths relative to `minisweagent/`.

`agents/default.py:126-128`, `step`:
```python
def step(self) -> list[dict]:
    """Query the LM, execute actions."""
    return self.execute_actions(self.query())
```

`agents/default.py:154-157`, the hook point:
```python
def execute_actions(self, message: dict) -> list[dict]:
    """Execute actions in message, add observation messages, return them."""
    outputs = [self.env.execute(action) for action in message.get("extra", {}).get("actions", [])]
    return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))
```

- `message` is the assistant message `query()` just added. Its actions come from `models/utils/actions_toolcall.py:30` (`parse_toolcall_actions`). Line 75: `actions.append({"command": args["command"], "tool_call_id": tool_call.id})`. One response can contain **several** tool calls. A missing tool call, an unknown tool name or a missing `command` raises `FormatError` before `execute_actions` is reached.
- `environments/local.py:24-43`, `execute(action)`: runs `action["command"]` via `_run` (line 72: `subprocess.Popen(command, shell=True, …, stderr=STDOUT, start_new_session=True)`, process group killed on timeout). Line 29 passes `os.environ | self.config.env`, so **agents inherit the harness's full environment** (Cat ran `env`): no secrets in env vars. Exceptions become an output dict with `returncode -1` and `exception_info`. Then `_check_finished` (line 45) raises `Submitted` if the first output line is `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`. That happens in the middle of the list comprehension, so later actions in the same response are not run and no observations are added.
- `models/litellm_model.py:140`, `format_observation_messages` → `models/utils/actions_toolcall.py:79`, `format_toolcall_observation_messages`. It renders `observation_template` per output and pads missing outputs with `{"output": "", "returncode": -1, "exception_info": "action was not executed"}`. Line 98 stores the **full** output as `extra.raw_output`; this is where a storage cap would go (Task 6). Each message gets `role: "tool"` and the `tool_call_id`.

**Consequence for `send` / `submit` (Task 5):** override `execute_actions` in `discrete_agent`. For each action, if `command` is a harness command, build an output dict ourselves (`{"output": "...", "returncode": 0, "exception_info": ""}`) without calling `env.execute`; otherwise call `self.env.execute(action)`. Then call the same `format_observation_messages`. The agent sees an ordinary tool response, and its message history keeps the format the template expects (tool call → tool response).
