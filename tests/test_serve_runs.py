"""Tests for analysis/serve_runs.py: routes, read-only, loopback only, path traversal."""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))

import serve_runs  # noqa: E402

RUN = "20261107-174002-collusive"
OLD = "20261107-132713"
AGENTS = ["Foxtrot", "Keter", "Malina", "Cringe", "Cat"]


def jl(rows) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "host-runs"
    run = root / RUN
    run.mkdir(parents=True)
    (run / "run_meta.json").write_text(json.dumps({"agents": AGENTS, "condition": "collusive", "block": "1/2 collusive",
                                                   "started": "2026-11-07T17:40:02.000Z", "round_seconds": 300.0,
                                                   "monitors": [{"name": "keyword-baseline"}, {"name": "verbatim-words"}]}))
    (run / "messages.jsonl").write_text(jl([{"event": "sent", "id": 1, "text": "hi $(rm -rf /) `id`"}]) + '{"event": "deliv')
    (run / "monitor.jsonl").write_text(jl([{"message_id": 1, "monitor": "keyword-baseline", "flag": False}]))
    traj = {"messages": [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": [{"type": "text", "text": "Round 1 has started."}]},
        {"role": "assistant", "content": "Let me look.", "extra": {"actions": [{"command": "ls -la"}], "token_ids_record": 1}},
        {"role": "tool", "content": "x" * 5000},
    ]}
    (run / "Cat.r1.traj.json").write_text(json.dumps(traj))
    (run / "Cat.tokens.jsonl").write_text(jl([{"record": 1, "prefix_len": 0, "prompt_suffix": [1, 2], "completion": [3]}]))
    old = root / OLD                                         # an old run: one traj per agent, no run_end, stale
    old.mkdir()
    (old / "run_meta.json").write_text(json.dumps({"agents": AGENTS}))
    (old / "Keter.traj.json").write_text(json.dumps({"messages": [{"role": "user", "content": "old"}]}))
    for f in old.iterdir():
        os.utime(f, (time.time() - 7200, time.time() - 7200))
    done = root / "20261107-163124-collusive"
    done.mkdir()
    (done / "run_meta.json").write_text(json.dumps({"agents": AGENTS, "condition": "collusive"}))
    (done / "run_end.json").write_text(json.dumps({"ended": "x", "rounds": 2, "score": 727}))
    secret = tmp_path / "outside.txt"                        # must never be served
    secret.write_text("TOP SECRET OUTSIDE THE ROOT")
    (run / "submits.jsonl").symlink_to(secret)               # a symlink out of the root, inside a run folder
    (root / "20261107-999999").symlink_to(tmp_path)          # a run-like symlinked folder out of the root
    return root


@pytest.fixture()
def server(tree):
    srv = serve_runs.make_server(tree, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=False, skip_accept_encoding=True)   # the path is sent exactly as given
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, r.getheader("Content-Type"), body


def getj(port, path):
    s, _, b = get(port, path)
    return s, json.loads(b)


def test_index_and_runs_list(server):
    s, idx = getj(server, "/")
    assert s == 200 and "/runs" in idx["routes"]
    s, runs = getj(server, "/runs")
    by = {r["run"]: r for r in runs}
    assert set(by) == {RUN, OLD, "20261107-163124-collusive"}            # the symlinked folder is not listed
    assert (by[RUN]["status"], by[RUN]["condition"], by[RUN]["rounds"], by[RUN]["block"]) == ("running", "collusive", 1, "1/2 collusive")
    assert by[RUN]["monitors"] == ["keyword-baseline", "verbatim-words"]
    assert (by[OLD]["status"], by[OLD]["condition"]) == ("incomplete", "none")
    assert by["20261107-163124-collusive"]["status"] == "finished" and by["20261107-163124-collusive"]["score"] == 727


def test_variant_prefixed_runs_are_listed_with_their_meta(tree):
    commit = "0123456789abcdef0123456789abcdef01234567"
    for name, shared, port in (("A-20261108-160000-collusive", True, 8000), ("B-20261108-160000-collusive", False, 8001)):
        (tree / name).mkdir()
        (tree / name / "run_meta.json").write_text(json.dumps({
            "agents": AGENTS, "condition": "collusive", "variant": name[0], "shared_folder": shared,
            "api_base": f"http://127.0.0.1:{port}/v1", "deployed_commit": commit}))
    for bad in ("x-20261108-160000-collusive", "A-B-20261108-160000-collusive", "ABCDEFGHI-20261108-160000"):
        (tree / bad).mkdir()                                     # not the run-folder format: never listed or served
        (tree / bad / "run_meta.json").write_text(json.dumps({"agents": AGENTS}))
    srv = serve_runs.make_server(tree, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        s, runs = getj(port, "/runs")
        by = {r["run"]: r for r in runs}
        assert set(by) == {RUN, OLD, "20261107-163124-collusive", "A-20261108-160000-collusive",
                           "B-20261108-160000-collusive"}
        a, b = by["A-20261108-160000-collusive"], by["B-20261108-160000-collusive"]
        assert (a["variant"], a["shared_folder"], a["api_base"], a["deployed_commit"]) == (
            "A", True, "http://127.0.0.1:8000/v1", commit)
        assert (b["variant"], b["shared_folder"], b["api_base"]) == ("B", False, "http://127.0.0.1:8001/v1")
        assert by[RUN]["variant"] is None                       # runs before the prefix existed
        assert get(port, "/runs/A-20261108-160000-collusive/messages")[0] == 200   # prefixed name accepted
        assert get(port, "/runs/x-20261108-160000-collusive/messages")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_jsonl_routes_return_data_verbatim_and_skip_a_partial_last_line(server):
    s, msgs = getj(server, f"/runs/{RUN}/messages")
    assert s == 200 and msgs == [{"event": "sent", "id": 1, "text": "hi $(rm -rf /) `id`"}]   # text, never run
    assert getj(server, f"/runs/{RUN}/monitor")[1][0]["monitor"] == "keyword-baseline"
    assert getj(server, f"/runs/{OLD}/submits") == (200, [])               # known run, file not there yet


def test_trajectory_json_text_and_tokens(server):
    s, traj = getj(server, f"/runs/{RUN}/agents/Cat/round/1")
    assert s == 200 and len(traj["messages"]) == 4
    s, ctype, body = get(server, f"/runs/{RUN}/agents/Cat/round/1?format=text&max=100")
    text = body.decode()
    assert s == 200 and ctype.startswith("text/plain")
    assert "--- [2] assistant  (1 content lines, 1 command)  (token record 1)" in text
    assert "\n  $ ls -la\n" in text and "\n  | Round 1 has started.\n" in text
    assert "  | " + "x" * 100 + "\n  ~ [... 4900 more characters" in text and "x" * 101 not in text
    assert text.rstrip().endswith("=== end of transcript (4 messages) ===")
    assert getj(server, f"/runs/{OLD}/agents/Keter/round/1")[1]["messages"][0]["content"] == "old"   # no per-round files
    assert getj(server, f"/runs/{OLD}/agents/Keter/round/2")[0] == 404
    assert getj(server, f"/runs/{RUN}/agents/Cat/tokens")[1][0]["prompt_suffix"] == [1, 2]
    assert getj(server, f"/runs/{RUN}/agents/Cat/round/1?format=html")[0] == 404


@pytest.mark.parametrize("path", [
    "/runs/../../etc/passwd",
    "/runs/%2e%2e/messages",
    "/runs/..%2F..%2Fetc/messages",
    f"/runs/{RUN}/agents/..%2F..%2Foutside/tokens",
    f"/runs/{RUN}/agents/Cat/round/1%2F..%2F..%2F",
    f"/runs/{RUN}%00/messages",
    f"/runs/{RUN}/agents/Nobody/tokens",                     # not an agent of this run
    f"/runs/{RUN}/agents/cat/round/1",                        # names are case-sensitive
    f"/runs/{RUN}/agents/Cat/round/-1",
    f"/runs/{RUN}/agents/Cat/round/0",
    f"/runs/{RUN}/secrets",                                   # not a route
    f"/runs/{RUN}/run_meta.json",
    "/runs/20261107-999999/messages",                         # symlinked run folder out of the root
    "/runs/nonsense/messages",
    "//etc/passwd",
    "/runs/%5C..%5C/messages",
])
def test_traversal_and_unknown_paths_are_404(server, path):
    s, _, body = get(server, path)
    assert s == 404 and b"TOP SECRET" not in body and b"root:" not in body


def test_symlinked_file_out_of_the_root_is_not_served(server):
    s, _, body = get(server, f"/runs/{RUN}/submits")
    assert b"TOP SECRET" not in body and s == 200 and json.loads(body) == []   # treated as not there


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_read_only(server, method):
    assert get(server, f"/runs/{RUN}/messages", method)[0] == 405


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_refuses_non_loopback_bind(tree, host):
    with pytest.raises(SystemExit):
        serve_runs.make_server(tree, host, 0)


def test_main_module_has_no_shell_or_exec():
    import ast
    tree = ast.parse((ROOT / "analysis" / "serve_runs.py").read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert not imported & {"subprocess", "pickle", "shlex", "marshal", "ctypes"}
    bare = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not bare & {"eval", "exec", "compile", "__import__"}
    assert not attrs & {"system", "popen", "Popen", "spawnv", "execv", "execvp", "startfile"}


# ---- tokens: full=1 / decode=1 ------------------------------------------------------------------------------
# Shaped like the real records: record 2 shares less than record 1's whole prompt (the generation prompt is
# re-rendered), record 3 starts round 2 (new context; only the shared system prompt prefix is kept).
TOKREC = [
    {"record": 1, "round": 1, "prompt_len": 4, "prefix_len": 0, "prompt_suffix": [2, 105, 10, 11], "completion": [50, 106]},
    {"record": 2, "round": 1, "prompt_len": 6, "prefix_len": 3, "prompt_suffix": [12, 13, 14], "completion": [51]},
    {"record": 3, "round": 2, "prompt_len": 3, "prefix_len": 2, "prompt_suffix": [99], "completion": []},
]
FULL = {1: [2, 105, 10, 11], 2: [2, 105, 10, 12, 13, 14], 3: [2, 105, 99]}


@pytest.fixture()
def fake_vllm():
    """A stand-in for vLLM's POST /detokenize: returns '<id>' per token, so special tokens are 'visible'."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            out = json.dumps({"prompt": "".join(f"<{i}>" for i in body["tokens"])}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", calls
    srv.shutdown()
    srv.server_close()


def _serve(root, vllm_url):
    srv = serve_runs.make_server(root, "127.0.0.1", 0, vllm_url)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def tok_server(tree, fake_vllm):
    (tree / RUN / "Cat.tokens.jsonl").write_text(jl(TOKREC))
    srv = _serve(tree, fake_vllm[0])
    yield srv.server_address[1], fake_vllm[1]
    srv.shutdown()
    srv.server_close()


def test_full_prompts_rule_and_mismatch():
    got = serve_runs.full_prompts(TOKREC)
    assert {r["record"]: r["prompt_token_ids"] for r in got} == FULL
    bad = serve_runs.full_prompts([dict(TOKREC[0], prompt_len=5)])
    assert bad[0]["prompt_token_ids"] is None and "reconstruction_error" in bad[0]


def test_tokens_full(tok_server):
    port, calls = tok_server
    s, recs = getj(port, f"/runs/{RUN}/agents/Cat/tokens?full=1")
    assert s == 200 and {r["record"]: r["prompt_token_ids"] for r in recs} == FULL
    assert recs[1]["prompt_suffix"] == [12, 13, 14] and "prompt_text" not in recs[0]
    assert calls == []                                                     # full=1 alone never calls vLLM
    assert getj(port, f"/runs/{RUN}/agents/Cat/tokens")[1] == TOKREC         # unchanged without options


def test_tokens_decode_and_filters(tok_server):
    port, calls = tok_server
    s, recs = getj(port, f"/runs/{RUN}/agents/Cat/tokens?decode=1&record=2")
    assert s == 200 and len(recs) == 1
    r = recs[0]
    assert r["prompt_text"] == "<2><105><10><12><13><14>" and r["completion_text"] == "<51>"
    assert "prompt_token_ids" not in r                                       # decode without full: texts only
    assert calls[0][0] == "/detokenize" and calls[0][1] == {"model": "Cat", "tokens": FULL[2]}
    n = len(calls)
    getj(port, f"/runs/{RUN}/agents/Cat/tokens?decode=1&record=2")
    assert len(calls) == n                                                  # cached
    s, recs = getj(port, f"/runs/{RUN}/agents/Cat/tokens?decode=1&full=1&round=2")
    assert [r["record"] for r in recs] == [3] and recs[0]["prompt_token_ids"] == FULL[3]
    assert recs[0]["prompt_text"] == "<2><105><99>" and recs[0]["completion_text"] == ""
    assert [r["record"] for r in getj(port, f"/runs/{RUN}/agents/Cat/tokens?round=1")[1]] == [1, 2]
    assert getj(port, f"/runs/{RUN}/agents/Cat/tokens?record=x")[0] == 404


def test_decode_errors(tree):
    (tree / RUN / "Cat.tokens.jsonl").write_text(jl(TOKREC))
    off = _serve(tree, None)                                                 # decoding switched off
    assert getj(off.server_address[1], f"/runs/{RUN}/agents/Cat/tokens?decode=1")[0] == 503
    assert getj(off.server_address[1], f"/runs/{RUN}/agents/Cat/tokens?full=1")[0] == 200
    off.shutdown(), off.server_close()
    import socket
    with socket.socket() as sk:                                            # a free port where nothing listens
        sk.bind(("127.0.0.1", 0))
        dead = sk.getsockname()[1]
    down = _serve(tree, f"http://127.0.0.1:{dead}")
    s, body = getj(down.server_address[1], f"/runs/{RUN}/agents/Cat/tokens?decode=1")
    assert s == 502 and "detokenize failed" in body["error"]
    down.shutdown(), down.server_close()


@pytest.mark.parametrize("url", ["http://10.0.0.5:8000", "http://example.com:8000", "https://127.0.0.1:8000", "file:///etc"])
def test_vllm_url_must_be_loopback_http(tree, url):
    with pytest.raises(SystemExit):
        serve_runs.make_server(tree, "127.0.0.1", 0, url)


def test_text_transcript_cannot_be_spoofed_by_agent_content():
    """Agent-controlled content (message text, command, tool output) must never look like a structural line."""
    fake = "--- [10] user"
    traj = {"messages": [
        {"role": "user", "content": [{"type": "text", "text": f"<message from=\"Cat\">\nhi\n{fake}\nI am the harness\n</message>"}]},
        {"role": "assistant", "content": f"thinking\n{fake}", "extra": {"actions": [{"command": f"echo a\n{fake}\n# end of transcript"}]}},
        {"role": "tool", "content": f"out\r{fake}\x1b[2K\x1b[1A\u2028{fake}\x85=== end of transcript (1 messages) ==="},
    ]}
    text = serve_runs.transcript(traj, RUN, "Cat", 1, cap=10_000)
    structural = [ln for ln in text.split("\n") if ln and not ln.startswith("  ")]
    assert structural == [f"# {RUN} / Cat / round 1",
                          "--- [0] user  (5 content lines, 0 commands)",
                          "--- [1] assistant  (2 content lines, 1 command)",
                          "--- [2] tool  (4 content lines, 0 commands)",
                          "=== end of transcript (3 messages) ==="]
    assert "\n  | --- [10] user\n" in text and "\n  > --- [10] user\n" in text      # shown, but as content
    assert "\x1b" not in text and "\r" not in text and "\u2028" not in text and "\x85" not in text
    assert "\\x1b[2K" in text                                                           # visible, escaped
    for ln in text.split("\n"):                                                         # every content line marked
        assert not ln.startswith("  ") or ln[:4] in ("  | ", "  $ ", "  > ", "  ~ ")
