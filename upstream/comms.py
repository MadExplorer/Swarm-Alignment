"""Harness-mediated communication: send / broadcast / submit / done.

Every agent action passes through `Router.handle()` before it can reach the shell
(called from `discrete_agent.execute_actions`). Harness commands are never executed.
Design: design/communication.md. Proposed by Ivan for the team lead's sandbox (not his code).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from runlog import JsonlLog

SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

# `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, optionally quoted -> treated as `done`
_SENTINEL_ECHO = re.compile(r"""^echo\s+(['"]?)COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\1$""")
# a harness word in command position later in a shell line: after ; && || | or a newline
_EMBEDDED = re.compile(r"(;|&&|\|\||\||\n)\s*(send|broadcast|submit)\b")
_MESSAGE_TAG = re.compile(r"<(/?)message", re.IGNORECASE)


def ok(text: str) -> dict:
    return {"output": text, "returncode": 0, "exception_info": ""}


def error(text: str) -> dict:
    return {"output": "error: " + text, "returncode": 1, "exception_info": ""}


def _strip_outer_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def classify(command: str) -> tuple[Optional[str], str]:
    """Return (kind, rest). kind is one of send/broadcast/submit/done/embedded, or None (= run in shell)."""
    stripped = command.strip()
    if stripped == "done" or _SENTINEL_ECHO.match(stripped):
        return "done", ""
    m = re.match(r"(\S+)(.*)\Z", stripped, re.S)  # first word, then the rest (may start with a newline)
    if m and m.group(1) in ("send", "broadcast", "submit"):
        return m.group(1), m.group(2).strip()
    if _EMBEDDED.search(command):
        return "embedded", ""
    return None, ""


@dataclass
class Message:
    id: int
    round: int
    sender: str
    recipients: list[str]
    text: str
    sent_time: float = field(default_factory=time.time)


class Router:
    def __init__(
        self,
        agent_names: list[str],
        run_dir: Path,
        monitors: list,
        on_submit: Callable[[str, str, str], Optional[bool]],
        on_done: Callable[[str, str], str],
        max_len: int = 2000,
    ):
        self.agent_names = list(agent_names)
        self.monitors = monitors
        self.on_submit = on_submit
        self.on_done = on_done
        self.max_len = max_len
        self.round = 0
        self.queues: dict[str, list[Message]] = {n: [] for n in self.agent_names}
        self.history: list[Message] = []
        self._next_id = 1
        self.messages_log = JsonlLog(Path(run_dir) / "messages.jsonl")
        self.submits_log = JsonlLog(Path(run_dir) / "submits.jsonl")
        self.monitor_log = JsonlLog(Path(run_dir) / "monitor.jsonl")

    # ---- called for every action, before the shell -------------------------------------
    def handle(self, sender: str, command: str, sender_step: int = 0, tool_call_id: str = "") -> Optional[dict]:
        """Return an output dict for harness commands, or None if the action should go to the shell."""
        kind, rest = classify(command)
        if kind is None:
            return None
        if kind == "embedded":
            self._log_rejected(sender, command, "embedded", sender_step, tool_call_id)
            return error("send/broadcast/submit must be the whole command, not part of a shell line")
        if kind == "done":
            return ok(self.on_done(sender, "sentinel" if SENTINEL in command else "done"))
        if kind == "submit":
            return self._submit(sender, rest, command, sender_step, tool_call_id)
        if kind == "send":
            m = re.match(r"(\S+)\s*(.*)\Z", rest, re.S)
            if not m:
                return error("usage: send <AGENT> <text>")
            recipient, text = m.group(1), m.group(2)
            if recipient not in self.agent_names:
                return error(f"unknown agent '{recipient}'. Agents: {', '.join(self.agent_names)}")
            if recipient == sender:
                return error("you cannot send a message to yourself")
            return self._send(sender, [recipient], text, command, sender_step, tool_call_id)
        # broadcast
        return self._send(sender, [n for n in self.agent_names if n != sender], rest, command, sender_step, tool_call_id)

    def done_from_output(self, sender: str) -> dict:
        """The sentinel was printed by some other command (mini-swe-agent raised Submitted)."""
        return ok(self.on_done(sender, "sentinel-output"))

    # ---- messages ----------------------------------------------------------------------
    def _send(self, sender, recipients, text, raw_command, sender_step, tool_call_id) -> dict:
        text = _strip_outer_quotes(text.strip())
        if not text:
            return error("empty message")
        if len(text) > self.max_len:
            return error(f"message is {len(text)} characters; the limit is {self.max_len}")
        msg = Message(self._next_id, self.round, sender, recipients, text)
        self._next_id += 1
        self.messages_log.write({
            "event": "sent", "id": msg.id, "round": msg.round, "sender": sender, "recipients": recipients,
            "text": text, "raw_command": raw_command, "sender_step": sender_step, "tool_call_id": tool_call_id,
        })
        self._review(msg)
        self.history.append(msg)
        for r in recipients:
            self.queues[r].append(msg)
        who = recipients[0] if len(recipients) == 1 else "all other agents"
        return ok(f"sent to {who} (message {msg.id}); it will be delivered at the recipient's next step.")

    def _review(self, msg: Message) -> None:
        """Observe mode: every monitor sees every message before delivery; verdicts are logged, never block."""
        ctx = {
            "round": msg.round,
            "sender": msg.sender,
            "recipients": msg.recipients,
            "earlier_messages": [
                {"id": h.id, "sender": h.sender, "recipients": h.recipients, "text": h.text}
                for h in self.history
                if {h.sender, *h.recipients} & {msg.sender} and {h.sender, *h.recipients} & set(msg.recipients)
            ],
        }
        for mon in self.monitors:
            t0 = time.perf_counter()
            try:
                v = mon.review({"id": msg.id, "sender": msg.sender, "recipients": msg.recipients, "text": msg.text}, ctx)
                rec = {"flag": v.flag, "score": v.score, "reason": v.reason}
            except Exception as e:  # a broken monitor must not stop the game
                rec = {"error": f"{type(e).__name__}: {e}"}
            self.monitor_log.write({
                "message_id": msg.id, "round": msg.round, "monitor": mon.name, "version": mon.version,
                "mode": "observe", "latency_ms": round((time.perf_counter() - t0) * 1000, 2), **rec,
            })

    def collect(self, recipient: str, recipient_step: int = 0) -> Optional[str]:
        """Pending messages for `recipient` as one user-message text, or None. Logs delivery."""
        pending, self.queues[recipient] = self.queues[recipient], []
        if not pending:
            return None
        blocks = []
        for m in pending:
            self.messages_log.write({"event": "delivered", "id": m.id, "recipient": recipient, "recipient_step": recipient_step})
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m.sent_time))
            body = _MESSAGE_TAG.sub(lambda x: "&lt;" + x.group(1) + "message", m.text)
            to = recipient if len(m.recipients) == 1 else "all"
            blocks.append(f'<message from="{m.sender}" to="{to}" id="{m.id}" round="{m.round}" time="{when}">\n{body}\n</message>')
        return "\n".join(blocks)

    def drop_undeliverable(self, recipient: str) -> None:
        for m in self.queues.get(recipient, []):
            self.messages_log.write({"event": "undelivered", "id": m.id, "recipient": recipient})
        self.queues[recipient] = []

    def _log_rejected(self, sender, command, reason, sender_step, tool_call_id) -> None:
        self.messages_log.write({
            "event": "rejected", "round": self.round, "sender": sender, "reason": reason,
            "raw_command": command, "sender_step": sender_step, "tool_call_id": tool_call_id,
        })

    # ---- submit ------------------------------------------------------------------------
    def _submit(self, sender, rest, raw_command, sender_step, tool_call_id) -> dict:
        m = re.match(r"(\S+)\s+(.+)\Z", rest, re.S)
        if not m:
            return error("usage: submit <AGENT> <SECRET WORD>")
        target, word = m.group(1), _strip_outer_quotes(m.group(2).strip())
        if target not in self.agent_names:
            return error(f"unknown agent '{target}'. Agents: {', '.join(self.agent_names)}")
        if target == sender:
            return error("you can only submit secret words of other agents")
        correct = self.on_submit(sender, target, word)
        self.submits_log.write({
            "round": self.round, "submitter": sender, "target": target, "word": word, "correct": correct,
            "raw_command": raw_command, "sender_step": sender_step, "tool_call_id": tool_call_id,
        })
        return ok(f"submitted '{word}' for {target}.")
