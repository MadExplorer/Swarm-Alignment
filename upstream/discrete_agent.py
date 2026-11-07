from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.default import FormatError
from minisweagent.agents.default import InterruptAgentFlow
from minisweagent.exceptions import Submitted

# Stored command output per observation (the model itself sees at most 10k characters,
# see observation_template in swarm.yaml). Full outputs made trajectories grow ~45 MB in 50 s.
RAW_OUTPUT_CAP = 20_000

class discrete_agent(DefaultAgent):
    def __init__(self, *args, agent_name: str = "", router=None, token_log=None, **kwargs):
        # Harness-only arguments are taken out before mini-swe-agent's AgentConfig sees the rest.
        self.agent_name = agent_name  # set by the harness, never parsed from model output
        self.router = router
        self.token_log = token_log
        self._last_prompt_ids = []
        self._token_records = 0
        super().__init__(*args, **kwargs)

    def run_step(self):
        try:
            self.step()
            self.n_consecutive_format_errors = 0  # reset on any clean step
        except FormatError as e:
            # The call was billed before parsing failed, so query() never got to charge it.
            self.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
            self.n_consecutive_format_errors += 1
            if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                pass
                #self.add_messages(
                #    *e.messages,
                #    {
                #        "role": "exit",
                #        "content": "RepeatedFormatError",
                #        "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
                #     },
                #)
            else:
                pass
                self.add_messages(*e.messages)
        except InterruptAgentFlow as e:
            pass
            self.add_messages(*e.messages)
        except Exception as e:
            self.handle_uncaught_exception(e)
            raise
        finally:
            self.save(self.config.output_path)


    def run_init(self, task: str = "", **kwargs):
        ### First part of the division of original run() method
        ### Initialising agent with task, befor consecutively making steps with run_step()
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
            )


    def execute_actions(self, message: dict) -> list[dict]:
        """Like DefaultAgent.execute_actions, but harness commands (send, broadcast, submit, done) are handled by
        the router and never reach the shell, and printing COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT counts as `done`
        instead of ending the agent for good."""
        outputs = []
        for action in message.get("extra", {}).get("actions", []):
            output = None
            if self.router is not None:
                output = self.router.handle(self.agent_name, action.get("command", ""),
                                            self.n_calls, action.get("tool_call_id", ""))
            if output is None:
                try:
                    output = self.env.execute(action)
                except Submitted:
                    if self.router is None:
                        raise
                    output = self.router.done_from_output(self.agent_name)
            outputs.append(output)
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def add_messages(self, *messages: dict) -> list[dict]:
        for m in messages:
            self._move_token_ids(m)
            self._cap_raw_output(m)
        return super().add_messages(*messages)

    def _move_token_ids(self, message: dict) -> None:
        """Move vLLM's token IDs (return_token_ids) from the stored response into an append-only sidecar file.
        Each record stores the prompt as (length of the prefix shared with the previous prompt, new suffix),
        so the file grows linearly instead of repeating the whole prompt on every call."""
        resp = (message.get("extra") or {}).get("response")
        if self.token_log is None or not isinstance(resp, dict):
            return
        prompt = resp.pop("prompt_token_ids", None)
        completion = None
        for choice in (resp.get("choices") or [])[:1]:
            completion = choice.pop("token_ids", None)
            psf = choice.get("provider_specific_fields") or {}
            completion = psf.pop("token_ids", None) if completion is None else completion
        if prompt is None and completion is None:
            return
        prompt = list(prompt or [])
        k = 0
        for a, b in zip(self._last_prompt_ids, prompt):
            if a != b:
                break
            k += 1
        self._token_records += 1
        self.token_log.write({
            "record": self._token_records, "message_index": len(self.messages), "role": message.get("role"),
            "response_id": resp.get("id"), "prompt_len": len(prompt), "prefix_len": k,
            "prompt_suffix": prompt[k:], "completion": completion,
        })
        self._last_prompt_ids = prompt
        message["extra"]["token_ids_record"] = self._token_records

    @staticmethod
    def _cap_raw_output(message: dict) -> None:
        extra = message.get("extra") or {}
        raw = extra.get("raw_output")
        if isinstance(raw, str) and len(raw) > RAW_OUTPUT_CAP:
            half = RAW_OUTPUT_CAP // 2
            extra["raw_output"] = (raw[:half] + f"\n[... {len(raw) - RAW_OUTPUT_CAP} characters not stored by the harness ...]\n"
                                   + raw[-half:])
            extra["raw_output_len"] = len(raw)
