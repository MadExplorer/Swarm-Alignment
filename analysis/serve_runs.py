"""Read-only HTTP endpoint over the run folders (the team lead's items 3 and 6: read the agents' output).

Written by Ivan (with Claude) for the team; not the team lead's harness. Python standard library only.

    python serve_runs.py --root /root/repro/host-runs --port 8765          # on the server, in tmux
    ssh -L 8765:127.0.0.1:8765 tl                                           # on your machine
    curl localhost:8765/runs

Routes (GET only; JSON unless format=text):
    /                                             this list
    /runs                                         runs with condition, status, rounds, score
    /runs/<run>/messages                          messages.jsonl (sent, delivered, rejected, undelivered)
    /runs/<run>/submits                           submits.jsonl
    /runs/<run>/monitor                           monitor.jsonl (role "game_rule" = verbatim-word monitor)
    /runs/<run>/agents/<name>/round/<N>           <name>.r<N>.traj.json (or <name>.traj.json without per-round files)
                                   ?format=text   readable transcript; &max=<chars> caps each tool output (default 2000)
    /runs/<run>/agents/<name>/tokens              <name>.tokens.jsonl (delta-encoded; see README-run-folder.md)

Safety: binds to loopback only (refuses anything else); serves only files under --root whose names match the
run-folder format; run and agent names are checked against fixed patterns and the run's own run_meta.json; the
resolved path must stay inside --root (no traversal, no symlinks out); nothing from the data is ever executed,
only parsed as JSON. The agents cannot reach it: their container has no network (--network none).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

RUN_RE = re.compile(r"\d{8}-\d{6}(-[a-z]+)?\Z")
AGENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
LIVE_SECONDS = 600            # no run_end.json and a file changed within this time: "running"
DEFAULT_TOOL_CAP = 2000

ROUTES = {
    "/runs": "runs with condition, status, rounds, score",
    "/runs/<run>/messages": "messages.jsonl",
    "/runs/<run>/submits": "submits.jsonl",
    "/runs/<run>/monitor": "monitor.jsonl",
    "/runs/<run>/agents/<name>/round/<N>?format=json|text[&max=2000]": "one agent's trajectory for round N",
    "/runs/<run>/agents/<name>/tokens": "token-ID records (delta-encoded)",
}


class NotFound(Exception):
    pass


def read_jsonl(path: Path) -> list:
    """Parse a JSONL file that may be appended to right now: an incomplete last line is skipped."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class Runs:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)

    def _inside(self, p: Path) -> Path:
        """Resolve and require the result to be a regular file or folder inside the root."""
        r = p.resolve()
        if r != self.root and self.root not in r.parents:
            raise NotFound("outside the run root")
        return r

    def run_dir(self, run: str) -> Path:
        if not RUN_RE.match(run):
            raise NotFound(f"no such run: {run!r}")
        d = self._inside(self.root / run)
        if not d.is_dir() or d.parent != self.root:
            raise NotFound(f"no such run: {run!r}")
        return d

    def file(self, run: str, name: str) -> Path:
        d = self.run_dir(run)
        f = self._inside(d / name)
        if f.parent != d or not f.is_file():
            raise NotFound(f"{name} not found in {run} (yet)")
        return f

    def agents(self, run: str) -> list:
        meta = read_json(self.run_dir(run) / "run_meta.json") or {}
        return [a for a in meta.get("agents", []) if isinstance(a, str) and AGENT_RE.match(a)]

    def check_agent(self, run: str, name: str) -> None:
        if not AGENT_RE.match(name) or name not in self.agents(run):
            raise NotFound(f"no agent {name!r} in {run}")

    def list_runs(self) -> list:
        out = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.is_symlink() or not RUN_RE.match(d.name):
                continue
            meta = read_json(d / "run_meta.json") or {}
            end = read_json(d / "run_end.json")
            newest = max((f.stat().st_mtime for f in d.iterdir() if f.is_file()), default=0)
            status = "finished" if end else ("running" if time.time() - newest < LIVE_SECONDS else "incomplete")
            trajs = [f.name for f in d.glob("*.traj.json")]
            rounds = max((int(m.group(1)) for f in trajs if (m := re.search(r"\.r(\d+)\.traj\.json\Z", f))), default=None)
            out.append({"run": d.name, "condition": meta.get("condition") or "none", "status": status,
                        "block": meta.get("block"), "started": meta.get("started"),
                        "ended": (end or {}).get("ended"), "rounds": (end or {}).get("rounds") or rounds,
                        "score": (end or {}).get("score"), "agents": meta.get("agents"),
                        "round_seconds": meta.get("round_seconds"),
                        "monitors": [m.get("name") for m in meta.get("monitors") or []],
                        "last_write": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest)) if newest else None})
        return out

    def trajectory(self, run: str, name: str, round_no: int) -> dict:
        self.check_agent(run, name)
        try:
            return read_json(self.file(run, f"{name}.r{round_no}.traj.json")) or {}
        except NotFound:
            if round_no == 1:                     # runs without the per-round reset have one file per agent
                return read_json(self.file(run, f"{name}.traj.json")) or {}
            raise


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return "" if content is None else json.dumps(content)


def transcript(traj: dict, run: str, name: str, round_no: int, cap: int = DEFAULT_TOOL_CAP) -> str:
    """Readable transcript: what the model saw (role, content), the commands it ran, tool outputs capped."""
    lines = [f"# {run} / {name} / round {round_no}", ""]
    for i, m in enumerate(traj.get("messages") or []):
        role = m.get("role", "?")
        extra = m.get("extra") or {}
        lines.append(f"--- [{i}] {role}" + (f"  (token record {extra['token_ids_record']})"
                                             if extra.get("token_ids_record") else ""))
        text = _text(m.get("content"))
        if role == "tool" and len(text) > cap:
            text = text[:cap] + f"\n[... {len(text) - cap} more characters; ?max= to change ...]"
        if text.strip():
            lines.append(text.rstrip())
        for a in extra.get("actions") or []:
            lines.append("$ " + str(a.get("command", "")).rstrip())
        lines.append("")
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    runs: Runs
    server_version = "swarm-runs/1"

    def log_message(self, fmt, *args):  # one short line per request on stderr
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=1).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            raw_parts = url.path.split("/")
            parts = [unquote(p) for p in raw_parts if p]
            if any(p in ("..", ".") or "/" in p or "\\" in p or "\x00" in p for p in parts):
                raise NotFound("bad path")
            q = parse_qs(url.query)
            self._route(parts, q)
        except NotFound as e:
            self._json({"error": str(e)}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # never leak a traceback with paths; log it on the server side only
            sys.stderr.write(f"error on {self.path!r}: {type(e).__name__}: {e}\n")
            self._json({"error": "internal error"}, 500)

    def _route(self, parts: list, q: dict) -> None:
        r = self.runs
        if not parts:
            return self._json({"routes": ROUTES})
        if parts == ["runs"]:
            return self._json(r.list_runs())
        if len(parts) == 3 and parts[0] == "runs" and parts[2] in ("messages", "submits", "monitor"):
            f = {"messages": "messages.jsonl", "submits": "submits.jsonl", "monitor": "monitor.jsonl"}[parts[2]]
            try:
                return self._json(read_jsonl(r.file(parts[1], f)))
            except NotFound:
                r.run_dir(parts[1])           # unknown run -> 404; known run without the file yet -> []
                return self._json([])
        if len(parts) == 5 and parts[0] == "runs" and parts[2] == "agents" and parts[4] == "tokens":
            r.check_agent(parts[1], parts[3])
            return self._json(read_jsonl(r.file(parts[1], f"{parts[3]}.tokens.jsonl")))
        if len(parts) == 6 and parts[0] == "runs" and parts[2] == "agents" and parts[4] == "round":
            if not parts[5].isdigit() or not 0 < int(parts[5]) < 10000:
                raise NotFound("round must be a positive number")
            n = int(parts[5])
            traj = r.trajectory(parts[1], parts[3], n)
            fmt = (q.get("format") or ["json"])[0]
            if fmt == "text":
                cap = (q.get("max") or [str(DEFAULT_TOOL_CAP)])[0]
                cap = int(cap) if cap.isdigit() else DEFAULT_TOOL_CAP
                body = transcript(traj, parts[1], parts[3], n, cap).encode("utf-8", "replace")
                return self._send(200, body, "text/plain; charset=utf-8")
            if fmt != "json":
                raise NotFound("format must be json or text")
            return self._json(traj)
        raise NotFound("no such route; GET / lists the routes")

    def _not_allowed(self):
        self._json({"error": "read-only: only GET"}, 405)

    do_POST = do_PUT = do_DELETE = do_PATCH = _not_allowed


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if not ipaddress.ip_address(host).is_loopback:
        raise SystemExit(f"refusing to bind to {host}: loopback only (teammates use ssh -L)")
    handler = type("BoundHandler", (Handler,), {"runs": Runs(Path(root))})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Read-only endpoint over the run folders (loopback only).")
    ap.add_argument("--root", required=True, help="folder with the run folders, e.g. /root/repro/host-runs")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    srv = make_server(Path(a.root), a.host, a.port)
    print(f"serving {Path(a.root).resolve()} read-only on http://{a.host}:{a.port}/  (Ctrl-C stops)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
