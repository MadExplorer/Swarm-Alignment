from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.default import FormatError
from minisweagent.agents.default import InterruptAgentFlow

class discrete_agent(DefaultAgent):
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
