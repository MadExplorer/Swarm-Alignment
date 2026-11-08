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
    /runs/<run>/scores                            score per round and agent (tasks, words, wrong submits, detections
                                                  by monitor and role), recomputed and checked against the harness
    /runs/<run>/agents/<name>/round/<N>           <name>.r<N>.traj.json (or <name>.traj.json without per-round files)
                                   ?format=text   readable transcript; &max=<chars> caps each tool output (default 2000)
    /runs/<run>/agents/<name>/tokens              <name>.tokens.jsonl (delta-encoded; see README-run-folder.md)
                                   ?full=1        + prompt_token_ids: the full prompt of each record, reconstructed
                                   ?decode=1      + prompt_text, completion_text, decoded by vLLM's POST /detokenize
                                                    (special tokens kept); &record=<k> or &round=<N> to select records

Safety: binds to loopback only (refuses anything else); serves only files under --root whose names match the
run-folder format; run and agent names are checked against fixed patterns and the run's own run_meta.json; the
resolved path must stay inside --root (no traversal, no symlinks out); nothing from the data is ever executed,
only parsed as JSON. The agents cannot reach it: their container has no network (--network none).
The only outgoing traffic is decode=1: token IDs to vLLM's /detokenize on loopback (no generation); --vllm none
switches that off.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from load_run import score_breakdown

RUN_RE = re.compile(r"([A-Z][A-Za-z0-9]{0,7}-)?\d{8}-\d{6}(-[a-z]+)?\Z")   # optional variant prefix (SWARM_RUN_PREFIX)
AGENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
LIVE_SECONDS = 600            # no run_end.json and a file changed within this time: "running"
DEFAULT_TOOL_CAP = 2000
DEFAULT_VLLM = "http://127.0.0.1:8000"
DECODE_CACHE = 512            # decoded texts kept in memory (records of finished calls never change)

ROUTES = {
    "/runs": "runs with condition, status, rounds, score",
    "/runs/<run>/messages": "messages.jsonl",
    "/runs/<run>/submits": "submits.jsonl",
    "/runs/<run>/monitor": "monitor.jsonl",
    "/runs/<run>/scores": "score breakdown per round and agent, checked against the harness's score",
    "/runs/<run>/agents/<name>/round/<N>?format=json|text[&max=2000]": "one agent's trajectory for round N",
    "/runs/<run>/agents/<name>/tokens[?full=1][&decode=1][&record=<k>|&round=<N>]":
        "token-ID records (delta-encoded); full=1 adds prompt_token_ids, decode=1 adds prompt_text and completion_text",
}


class NotFound(Exception):
    pass


class Unavailable(Exception):
    """vLLM's /detokenize failed or is switched off (HTTP 502/503)."""


def read_jsonl(path: Path) -> list:
    """Parse a JSONL file that may be appended to right now: an incomplete last line is skipped."""
    out = []
    # Split on "\n" only: str.splitlines() also breaks at \x85,  ,  , which json.dumps leaves unescaped
    # inside strings (raw bytes of binary files in shared_events.jsonl); those records would be silently dropped.
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
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
            out.append({"run": d.name, "variant": meta.get("variant"), "shared_folder": meta.get("shared_folder"),
                        "api_base": meta.get("api_base"), "deployed_commit": meta.get("deployed_commit"),
                        "condition": meta.get("condition") or "none", "status": status,
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


def full_prompts(records: list) -> list:
    """Records with "prompt_token_ids" added. The prompt of record k is the prompt of record k-1 (same agent, same
    file, across rounds; the previous prompt only, not its completion) cut to prefix_len, plus prompt_suffix.
    Record 1 has prefix_len 0. A length that does not match prompt_len gives null and "reconstruction_error"."""
    out, prev = [], []
    for rec in records:
        prompt = prev[: int(rec.get("prefix_len") or 0)] + list(rec.get("prompt_suffix") or [])
        r = dict(rec)
        if len(prompt) == rec.get("prompt_len"):
            r["prompt_token_ids"] = prompt
        else:
            r["prompt_token_ids"] = None
            r["reconstruction_error"] = f"got {len(prompt)} tokens, prompt_len is {rec.get('prompt_len')}"
        out.append(r)
        prev = prompt
    return out


class Detokenizer:
    """Client for vLLM's POST /detokenize (vLLM 0.33: tokenizer.decode(ids) with the default
    skip_special_tokens=False, so special tokens stay visible). Loopback only; no generation."""

    def __init__(self, base_url: str, timeout: float = 60.0):
        host = urlsplit(base_url).hostname or ""
        try:
            loop = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loop = False
        if urlsplit(base_url).scheme != "http" or not loop:
            raise SystemExit(f"refusing vLLM url {base_url}: http on a loopback address only")
        self.url = base_url.rstrip("/") + "/detokenize"
        self.timeout = timeout
        self.cache: OrderedDict = OrderedDict()
        self.lock = threading.Lock()              # the server is threaded

    def decode(self, ids: list, key: Optional[tuple] = None, model: Optional[str] = None) -> str:
        if not ids:
            return ""
        with self.lock:
            if key is not None and key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        body = json.dumps({"model": model, "tokens": [int(i) for i in ids]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = json.loads(resp.read())["prompt"]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            raise Unavailable(f"vLLM /detokenize failed: {type(e).__name__}: {e}") from None
        if key is not None:
            with self.lock:
                self.cache[key] = text
                while len(self.cache) > DECODE_CACHE:
                    self.cache.popitem(last=False)
        return text


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
    detok: Optional[Detokenizer] = None
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
        except Unavailable as e:
            self._json({"error": str(e)}, 503 if self.detok is None else 502)
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
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "scores":
            return self._json(self._scores(parts[1]))
        if len(parts) == 5 and parts[0] == "runs" and parts[2] == "agents" and parts[4] == "tokens":
            r.check_agent(parts[1], parts[3])
            return self._json(self._tokens(parts[1], parts[3], q))
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

    def _scores(self, run: str) -> dict:
        self.runs.run_dir(run)                                   # unknown run -> 404

        def rows(name):
            try:
                return read_jsonl(self.runs.file(run, name))
            except NotFound:
                return []
        try:
            end = read_json(self.runs.file(run, "run_end.json")) or {}
        except NotFound:
            end = {}
        return score_breakdown(rows("submits.jsonl"), rows("tasks.jsonl"), rows("monitor.jsonl"),
                               rows("messages.jsonl"), rows("shared_events.jsonl"), rows("score.jsonl"), end)

    def _tokens(self, run: str, name: str, q: dict) -> list:
        records = read_jsonl(self.runs.file(run, f"{name}.tokens.jsonl"))
        flag = lambda k: (q.get(k) or ["0"])[0] in ("1", "true", "yes")          # noqa: E731
        full, decode = flag("full"), flag("decode")
        sel = {}
        for k in ("record", "round"):
            if k in q:
                v = q[k][0]
                if not v.isdigit():
                    raise NotFound(f"{k} must be a number")
                sel[k] = int(v)
        if not (full or decode):
            return [x for x in records if all(x.get(k) == v for k, v in sel.items())]
        if decode and self.detok is None:
            raise Unavailable("decoding is switched off (--vllm none)")
        out = []
        for rec in full_prompts(records):                    # reconstruct over all records, then select
            if not all(rec.get(k) == v for k, v in sel.items()):
                continue
            if decode:
                key = (run, name, rec.get("record"))
                rec["prompt_text"] = (None if rec["prompt_token_ids"] is None else
                                      self.detok.decode(rec["prompt_token_ids"], key + ("prompt",), name))
                rec["completion_text"] = self.detok.decode(rec.get("completion") or [], key + ("completion",), name)
            if not full:
                del rec["prompt_token_ids"]
            out.append(rec)
        return out

    def _not_allowed(self):
        self._json({"error": "read-only: only GET"}, 405)

    do_POST = do_PUT = do_DELETE = do_PATCH = _not_allowed


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8765,
                vllm_url: Optional[str] = None) -> ThreadingHTTPServer:
    if not ipaddress.ip_address(host).is_loopback:
        raise SystemExit(f"refusing to bind to {host}: loopback only (teammates use ssh -L)")
    detok = Detokenizer(vllm_url) if vllm_url else None
    handler = type("BoundHandler", (Handler,), {"runs": Runs(Path(root)), "detok": detok})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Read-only endpoint over the run folders (loopback only).")
    ap.add_argument("--root", required=True, help="folder with the run folders, e.g. /root/repro/host-runs")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--vllm", default=DEFAULT_VLLM, help="vLLM base url for decode=1 (loopback only), or 'none'")
    a = ap.parse_args(argv)
    srv = make_server(Path(a.root), a.host, a.port, None if a.vllm == "none" else a.vllm)
    print(f"serving {Path(a.root).resolve()} read-only on http://{a.host}:{a.port}/  (Ctrl-C stops)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
