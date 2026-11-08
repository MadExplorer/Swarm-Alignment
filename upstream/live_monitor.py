"""A team member's research monitor, run LIVE in the harness (SWARM_LIVE_MONITOR=<file>), observe only.

Written by Ivan (with Claude) for the team; the same monitor file also runs offline (tools/swarm-eval,
analysis/monitor_template.py). Standard library only.

The monitor file defines its monitor in one of two ways (checked in this order):
    MONITOR = MyMonitor            # a class (instantiated with no arguments) or an instance
    class Monitor: ...             # a class named exactly "Monitor"
It needs `name`, `version` and `review(point, context) -> {"score": float, "flag": bool (optional), "reason": str}`.

Live it is wrapped as a research monitor (role "research"):
- verdicts are written to monitor.jsonl; in the word-judge model they never change the score, and only game-rule
  words ever block a message (variant C), so it can neither penalise nor block;
- it never sees a submit (Router._run_monitors refuses submit input before any monitor is called);
- `point` and `context` have the same fields as offline (no words, no condition, no run name);
- an exception inside review() is logged as an error verdict and the game goes on;
- needs_tokens monitors cannot run live (the router has no token IDs): they are refused.
"""
from __future__ import annotations

import hashlib
import importlib.util
import time
from pathlib import Path

from monitors import Verdict

RESERVED_NAMES = {"keyword-baseline", "word-judge", "verbatim-words"}


def load_monitor(path: str):
    """Import the monitor file and return an instance (see the module docstring for the two accepted forms)."""
    p = Path(path)
    if not p.is_file() or p.suffix != ".py":
        raise ValueError(f"monitor file not found or not a .py file: {path}")
    spec = importlib.util.spec_from_file_location(f"swarm_monitor_{p.stem}", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    obj = getattr(mod, "MONITOR", None) or getattr(mod, "Monitor", None)
    if obj is None:
        classes = [n for n, v in vars(mod).items() if isinstance(v, type) and hasattr(v, "review")]
        raise ValueError(f"{path}: define MONITOR = <class or instance> or a class named Monitor "
                         f"(classes with review(): {classes})")
    mon = obj() if isinstance(obj, type) else obj
    for attr in ("name", "version", "review"):
        if not hasattr(mon, attr):
            raise ValueError(f"{path}: the monitor has no {attr!r}")
    return mon


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LiveResearchMonitor:
    """Adapter: the offline interface inside the harness's Router (review(message, ctx) -> Verdict)."""
    role = "research"
    observe_only = True      # Router: logged to monitor.jsonl, never passed to scoring (on_verdicts) or blocking

    def __init__(self, inner, path: str = ""):
        if getattr(inner, "needs_tokens", False):
            raise ValueError("a needs_tokens monitor cannot run live (no token IDs in the router); run it offline")
        if getattr(inner, "role", "research") != "research":
            raise ValueError("a live team monitor must be a research monitor (role 'research')")
        if inner.name in RESERVED_NAMES:
            raise ValueError(f"monitor name {inner.name!r} is taken by a harness monitor")
        self.inner = inner
        self.name = str(inner.name)
        self.version = str(inner.version)
        self.threshold = float(getattr(inner, "threshold", 0.5))
        self.path = path

    def review(self, message: dict, ctx: dict) -> Verdict:
        shared = message.get("kind") == "shared_file"
        point = {"id": ("f" if shared else "m") + str(message["id"]), "kind": "shared_file" if shared else "message",
                 "round": ctx.get("round"), "time": time.time(), "sender": message["sender"],
                 "recipients": list(message.get("recipients") or []), "text": message.get("text", "")}
        earlier = [{"id": f"m{e['id']}", "kind": "message", "sender": e["sender"],
                    "recipients": list(e.get("recipients") or []), "text": e.get("text", "")}
                   for e in ctx.get("earlier_messages") or []]
        v = self.inner.review(point, {"earlier_points": earlier})
        score = float(v["score"])
        flag = v.get("flag")
        return Verdict(bool(flag) if flag is not None else score >= self.threshold, score, str(v.get("reason", ""))[:500])


def live_monitor_from_env(path: str) -> tuple[LiveResearchMonitor, dict]:
    mon = LiveResearchMonitor(load_monitor(path), path)
    return mon, {"path": str(path), "sha256": file_sha256(path), "name": mon.name, "version": mon.version}
