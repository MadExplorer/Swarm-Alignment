"""swarm-run / swarm-stop / swarm-eval: start, stop and evaluate runs on the sandbox server (see docs/QUICKSTART.md).

Written by Ivan (with Claude) for the team; not the team lead's harness. Standard library only (Python >= 3.10).
Everything lives under SWARM_REPRO_ROOT (default /root/repro): run folders in host-runs/, one code directory per
run (host-<PREFIX>, deployed from the bare repo repo.git with a blob-hash check), stop files stop-<PREFIX>.
It never touches host/ or host-C/ (code that running runs import) and never another run's container.
"""
from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

TOOLS = Path(__file__).resolve().parent
REPO_ROOT = TOOLS.parent                                     # the snapshot the tools came from (has analysis/, upstream/)
PREFIX_RE = re.compile(r"[A-Z][A-Za-z0-9]{0,7}\Z")           # = upstream/main.py SWARM_RUN_PREFIX rule
VARIANTS = {"A": {"SWARM_SHARED_FOLDER": "1", "SWARM_JUDGE_BLOCKS": "0"},
            "B": {"SWARM_SHARED_FOLDER": "0", "SWARM_JUDGE_BLOCKS": "0"},
            "C": {"SWARM_SHARED_FOLDER": "0", "SWARM_JUDGE_BLOCKS": "1"}}
PORTS = (8000, 8001)
AGENT_NAMES = ("Foxtrot", "Keter", "Malina", "Cringe", "Cat")   # = upstream/main.py agent_names (served model names)
SESSION = "repro"
PROTECTED_DIRS = ("host", "host-C")                          # code of the long runs: never deploy into these


class Refused(Exception):
    """A precondition failed; nothing was changed."""


# ---- the server, behind a small interface (tests replace it) ----------------------------------------------
class Host:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or os.environ.get("SWARM_REPRO_ROOT", "/root/repro"))

    @property
    def runs(self) -> Path:
        return self.root / "host-runs"

    def exists(self, p: Path) -> bool:
        return p.exists() or p.is_symlink()

    def docker_names(self) -> set[str]:
        r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, check=True)
        return {x.strip() for x in r.stdout.splitlines() if x.strip()}

    def processes(self) -> list[dict]:
        """Harness processes (run_blocks.py / main.py) with their SWARM_* environment."""
        out = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                cmd = (d / "cmdline").read_bytes().split(b"\0")
                if not any(c.endswith((b"run_blocks.py", b"main.py")) for c in cmd):
                    continue
                env = dict(x.split("=", 1) for x in (d / "environ").read_bytes().decode(errors="replace").split("\0")
                           if x.startswith("SWARM_") and "=" in x)
                ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            kind = "run_blocks" if any(c.endswith(b"run_blocks.py") for c in cmd) else "main"
            out.append({"pid": int(d.name), "ppid": ppid, "kind": kind, "env": env})
        return out

    def tmux_windows(self) -> set[str]:
        r = subprocess.run(["tmux", "list-windows", "-t", SESSION, "-F", "#{window_name}"], capture_output=True, text=True)
        return set(r.stdout.split()) if r.returncode == 0 else set()

    def vllm_problem(self, port: int) -> Optional[str]:
        """None if our vLLM answers on the port: /health is 200 AND /v1/models serves the five agent names (so another
        server on the same port, e.g. a monitor on GPUs 2-3, is not mistaken for it). Otherwise what is wrong."""
        base = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                if r.status != 200:
                    return f"/health returned HTTP {r.status}"
        except urllib.error.HTTPError as e:
            return f"/health returned HTTP {e.code}"
        except OSError as e:
            return f"nothing answers on /health ({getattr(e, 'reason', e)})"
        try:
            with urllib.request.urlopen(base + "/v1/models", timeout=5) as r:
                served = {m.get("id") for m in json.load(r).get("data", [])}
        except (OSError, ValueError, AttributeError, TypeError) as e:
            return f"/health is OK but /v1/models failed ({e.__class__.__name__})"
        missing = [a for a in AGENT_NAMES if a not in served]
        if missing:
            return (f"a server answers, but it does not serve the agents' model names {missing} "
                    f"(served: {sorted(x for x in served if x)[:5]}); is another program using this port?")
        return None

    def git(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["git", f"--git-dir={self.root / 'repo.git'}", *args], capture_output=True, check=True, **kw)

    def run(self, argv: list[str], **kw) -> subprocess.CompletedProcess:
        return subprocess.run(argv, check=True, **kw)

    def kill(self, pid: int, sig: int) -> None:
        os.kill(pid, sig)

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


# ---- checks -----------------------------------------------------------------------------------------------
def parse_order(order: str) -> list[tuple[str, int]]:
    out = []
    for item in (x.strip() for x in order.split(",") if x.strip()):
        cond, _, n = item.partition(":")
        if cond not in ("collusive", "honest") or not n.isdigit() or int(n) <= 0:
            raise Refused(f"bad --order item {item!r}: use collusive:N or honest:N, N > 0")
        out.append((cond, int(n)))
    if not out:
        raise Refused("--order is empty")
    return out


def prefix_conflicts(host: Host, prefix: str) -> list[str]:
    if not PREFIX_RE.match(prefix):
        return [f"prefix {prefix!r} must be 1-8 ASCII letters/digits starting with an upper-case letter"]
    probs = []
    taken = [p.name for p in host.runs.glob(f"{prefix}-*")] if host.exists(host.runs) else []
    if taken:
        probs.append(f"prefix {prefix} already used in {host.runs}: {sorted(taken)[:3]}")
    for p in (host.root / f"host-{prefix}", host.root / f"stop-{prefix}", host.root / f"host-{prefix}-DEPLOYED_COMMIT"):
        if host.exists(p):
            probs.append(f"{p} already exists")
    if any(pr["env"].get("SWARM_RUN_PREFIX") == prefix for pr in host.processes()):
        probs.append(f"a running harness process already uses prefix {prefix}")
    if f"run{prefix}" in host.tmux_windows():
        probs.append(f"tmux window {SESSION}:run{prefix} already exists")
    return probs


def next_container(host: Host) -> str:
    used = host.docker_names() | {pr["env"].get("SWARM_AGENT_CONTAINER", "") for pr in host.processes()}
    n = 4
    while f"repro{n}" in used:
        n += 1
    return f"repro{n}"


def ports_by_load(host: Host) -> list[int]:
    """The vLLM ports, fewest running runs first (a run = one run_blocks.py, or a main.py without one); ties: 8000."""
    procs = host.processes()
    rb_pids = {p["pid"] for p in procs if p["kind"] == "run_blocks"}
    load = {port: 0 for port in PORTS}
    for p in procs:
        if p["kind"] == "main" and p["ppid"] in rb_pids:
            continue
        m = re.search(r":(\d+)/", p["env"].get("SWARM_API_BASE", ""))
        if m and int(m.group(1)) in load:
            load[int(m.group(1))] += 1
    return sorted(PORTS, key=lambda port: (load[port], port))


def pick_port(host: Host) -> int:
    """The less busy port (load only, no health check; see choose_port)."""
    return ports_by_load(host)[0]


def choose_port(host: Host, requested: Optional[int]) -> tuple[int, list[str]]:
    """The port to run on, checked live (Host.vllm_problem). An explicit --port is never switched silently: if it is
    down, refuse and name the healthy port. Without --port: the less busy port, else the other one if the less busy
    one is down (the skip is reported); refuse if neither answers. Returns (port, notes about skipped ports)."""
    if requested is not None:
        if requested not in PORTS:
            raise Refused(f"--port must be one of {PORTS}")
        prob = host.vllm_problem(requested)
        if prob is None:
            return requested, []
        others = [p for p in PORTS if p != requested and host.vllm_problem(p) is None]
        hint = (f"; port {others[0]} is usable: rerun with --port {others[0]} (or without --port)" if others
                else "; no other vLLM port is usable either")
        raise Refused(f"vLLM on port {requested} is not usable: {prob}{hint}")
    notes = []
    for port in ports_by_load(host):
        prob = host.vllm_problem(port)
        if prob is None:
            return port, notes
        notes.append(f"port {port} skipped: {prob}")
    raise Refused("no usable vLLM on any port (" + "; ".join(notes) + "); start one first (REPRODUCE.md section 4) "
                  "or ask who stopped it")


# ---- swarm-run --------------------------------------------------------------------------------------------
def plan_run(host: Host, a) -> dict:
    """Check everything and build the plan (no side effects)."""
    probs = prefix_conflicts(host, a.prefix)
    if probs:
        raise Refused("; ".join(probs))
    order = parse_order(a.order)
    if a.round_seconds <= 0:
        raise Refused("--round-seconds must be > 0")
    port, port_notes = choose_port(host, a.port)
    commit = a.commit or default_commit(host)
    try:
        sha = host.git("rev-parse", "--verify", f"{commit}^{{commit}}", text=True).stdout.strip()
    except subprocess.CalledProcessError:
        raise Refused(f"commit {commit!r} is not in {host.root / 'repo.git'} (push it first)") from None
    main_py = host.git("show", f"{sha}:upstream/main.py", text=True).stdout
    if a.variant == "C" and "SWARM_JUDGE_BLOCKS" not in main_py:
        raise Refused(f"commit {sha[:7]} has no variant C (SWARM_JUDGE_BLOCKS)")
    monitor = None
    if a.monitor:
        if "SWARM_LIVE_MONITOR" not in main_py:
            raise Refused(f"commit {sha[:7]} cannot run a live monitor (no SWARM_LIVE_MONITOR); pass --commit with a "
                          f"newer commit")
        mp = Path(a.monitor).resolve()
        if not mp.is_file():
            raise Refused(f"monitor file not found: {mp}")
        monitor = {"source": str(mp), "check": check_monitor_file(mp)}
    container = next_container(host)
    code = host.root / f"host-{a.prefix}"
    if code.name in PROTECTED_DIRS:
        raise Refused(f"refusing to deploy into {code}")
    stop = host.root / f"stop-{a.prefix}"
    console = host.runs / f"{a.prefix}-console.log"
    launch = {"tool": "swarm-run", "variant": a.variant, "order": a.order, "round_seconds": a.round_seconds,
              "port": port, "ports_skipped": port_notes, "prefix": a.prefix, "commit": sha, "container": container,
              "monitor": (monitor or {}).get("check"), "argv": a.argv}
    env = {"SWARM_ISOLATION": "container", "SWARM_AGENT_IMAGE": "agents:v1.1", "SWARM_RUN_DIR": str(host.runs),
           "SWARM_CONFIG": str(code / "upstream" / "swarm.yaml"), "PYTHONDONTWRITEBYTECODE": "1",
           "SWARM_DEPLOYED_COMMIT": sha, "SWARM_RUN_PREFIX": a.prefix, **VARIANTS[a.variant],
           "SWARM_API_BASE": f"http://127.0.0.1:{port}/v1", "SWARM_AGENT_CONTAINER": container,
           "SWARM_STOP_FILE": str(stop), "SWARM_ROUND_SECONDS": f"{a.round_seconds:g}"}
    if monitor:
        env["SWARM_LIVE_MONITOR"] = str(code / "live_monitor_file" / Path(monitor["source"]).name)
    if "SWARM_LAUNCH" in main_py:
        env["SWARM_LAUNCH"] = json.dumps(launch, separators=(",", ":"))
    py = host.root / "host-venv" / "bin" / "python"
    cmd = (f"cd {shlex.quote(str(code / 'upstream'))} && echo false > {shlex.quote(str(stop))} && "
           + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
           + f" {shlex.quote(str(py))} run_blocks.py --order {shlex.quote(','.join(f'{c}:{n}' for c, n in order))}"
           + f" 2>&1 | tee -a {shlex.quote(str(console))}")
    return {"sha": sha, "code": code, "stop": stop, "console": console, "container": container, "port": port,
            "port_notes": port_notes,
            "monitor": monitor, "launch": launch, "env": env, "command": cmd, "window": f"run{a.prefix}",
            "launch_in_run_meta": "SWARM_LAUNCH" in main_py}


def default_commit(host: Host) -> str:
    """/root/repro/bin/DEFAULT_COMMIT (set by the team), else the commit variant C runs."""
    for f in (host.root / "bin" / "DEFAULT_COMMIT", host.root / "host-C-DEPLOYED_COMMIT"):
        if host.exists(f):
            return f.read_text().strip()
    raise Refused("no --commit and no default (bin/DEFAULT_COMMIT or host-C-DEPLOYED_COMMIT)")


def check_monitor_file(path: Path) -> dict:
    """Load the monitor the way the harness will (upstream/live_monitor.py), in a separate interpreter."""
    code = ("import json,sys; sys.path.insert(0, sys.argv[1]); from live_monitor import live_monitor_from_env; "
            "print(json.dumps(live_monitor_from_env(sys.argv[2])[1]))")
    r = subprocess.run([sys.executable, "-c", code, str(REPO_ROOT / "upstream"), str(path)], capture_output=True,
                       text=True, timeout=120)
    if r.returncode != 0:
        raise Refused(f"monitor file {path} does not load as a live research monitor: "
                      f"{(r.stderr.strip().splitlines() or ['?'])[-1]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def deploy(host: Host, plan: dict) -> int:
    """git archive <sha> into the run's own code directory, then compare every file with its blob hash."""
    code, sha = plan["code"], plan["sha"]
    code.mkdir(parents=False)
    archive = host.git("archive", "--format=tar", sha).stdout
    host.run(["tar", "-x", "-C", str(code)], input=archive)
    blobs = {}
    for line in host.git("ls-tree", "-r", sha, text=True).stdout.splitlines():
        meta, path = line.split("\t", 1)
        mode, typ, obj = meta.split()
        if typ == "blob":
            blobs[path] = obj
    paths = sorted(blobs)
    got = host.git("hash-object", "--stdin-paths", input="\n".join(str(code / p) for p in paths) + "\n",
                   text=True).stdout.split()
    bad = [p for p, h in zip(paths, got) if blobs[p] != h]
    on_disk = sum(1 for p in code.rglob("*") if p.is_file())
    if bad or len(got) != len(paths) or on_disk != len(paths):
        raise Refused(f"hash check FAILED in {code}: {len(bad)} mismatches, {on_disk} files vs {len(paths)} blobs; "
                      f"nothing was started")
    (host.root / f"host-{code.name[5:]}-DEPLOYED_COMMIT").write_text(sha + "\n")
    if plan["monitor"]:
        dst = Path(plan["env"]["SWARM_LIVE_MONITOR"])
        dst.parent.mkdir()
        shutil.copy2(plan["monitor"]["source"], dst)
    return len(paths)


def start(host: Host, plan: dict, header: str) -> None:
    plan["console"].parent.mkdir(parents=True, exist_ok=True)
    plan["console"].write_text(header)
    if SESSION not in subprocess.run(["tmux", "ls", "-F", "#{session_name}"], capture_output=True, text=True).stdout.split():
        host.run(["tmux", "new-session", "-d", "-s", SESSION, "-n", "info"])
    body = plan["command"] + "; echo '=== swarm-run: run_blocks.py exited ==='; exec bash"
    host.run(["tmux", "new-window", "-d", "-t", SESSION, "-n", plan["window"], "bash", "-c", body])


def main_run(argv=None, host: Optional[Host] = None) -> int:
    ap = argparse.ArgumentParser(prog="swarm-run", description="Start a run of the sandbox (detached, in tmux).")
    ap.add_argument("--variant", required=True, choices=sorted(VARIANTS),
                    help="A = shared folder on; B = off; C = judge blocks detected messages (shared folder off)")
    ap.add_argument("--prefix", required=True, help="new run-folder prefix, e.g. D (folders D-<time>-<condition>)")
    ap.add_argument("--order", default="collusive:8,honest:8", help="blocks, e.g. collusive:8,honest:8")
    ap.add_argument("--round-seconds", type=float, default=300.0)
    ap.add_argument("--port", type=int, choices=PORTS, help="vLLM port (default: the one with fewer running runs that answers; an explicit port that does "
                         "not answer is refused, never switched)")
    ap.add_argument("--commit", help="code to run (default: /root/repro/bin/DEFAULT_COMMIT, else the commit C runs)")
    ap.add_argument("--monitor", help="your research monitor file, live and observe only (needs a recent --commit)")
    ap.add_argument("--dry-run", action="store_true", help="check and print the command; change nothing")
    a = ap.parse_args(argv)
    a.argv = ["swarm-run", *(argv if argv is not None else sys.argv[1:])]
    host = host or Host()
    try:
        plan = plan_run(host, a)
    except Refused as e:
        print(f"swarm-run: refused: {e}", file=sys.stderr)
        return 2
    for n in plan["port_notes"]:
        print(f"note: {n}; using port {plan['port']}")
    print(f"variant {a.variant}  prefix {a.prefix}  commit {plan['sha'][:12]}  port {plan['port']}  "
          f"container {plan['container']}  code {plan['code']}")
    if plan["monitor"]:
        print(f"live monitor: {plan['monitor']['check']}")
    if not plan["launch_in_run_meta"]:
        print("note: this commit does not record the launch options in run_meta.json (no SWARM_LAUNCH); they are "
              "in the console log header")
    print("command:\n  " + plan["command"])
    if a.dry_run:
        print("dry run: nothing deployed, nothing started")
        return 0
    try:
        n = deploy(host, plan)
    except Refused as e:
        print(f"swarm-run: {e}", file=sys.stderr)
        return 3
    print(f"deployed {n} files, hash check OK")
    header = f"# swarm-run {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {json.dumps(plan['launch'])}\n"
    start(host, plan, header)
    folder = None
    for _ in range(90):
        found = sorted(p.name for p in host.runs.glob(f"{a.prefix}-2*") if p.is_dir())
        if found:
            folder = found[0]
            break
        time.sleep(1)
    print(f"started in tmux window {SESSION}:{plan['window']}; console {plan['console']}")
    print(f"run folder: {folder or '(not created yet; check the console log)'}")
    print(f"stop after the current block:  swarm-stop {a.prefix}")
    print(f"stop now (block cleans up):    swarm-stop {a.prefix} --now")
    print(f"pause / resume:                echo true > {plan['stop']}   /   echo false > {plan['stop']}")
    return 0


# ---- swarm-stop -------------------------------------------------------------------------------------------
def main_stop(argv=None, host: Optional[Host] = None, wait_s: int = 180) -> int:
    ap = argparse.ArgumentParser(prog="swarm-stop", description="Stop a run cleanly.")
    ap.add_argument("prefix")
    ap.add_argument("--now", action="store_true", help="also end the current block (it still cleans up)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    host = host or Host()
    procs = [p for p in host.processes() if p["env"].get("SWARM_RUN_PREFIX") == a.prefix]
    parents = [p for p in procs if p["kind"] == "run_blocks"]
    blocks = [p for p in procs if p["kind"] == "main"]
    if not procs:
        print(f"swarm-stop: no running harness process with prefix {a.prefix}", file=sys.stderr)
        return 2
    if len(parents) > 1:
        print(f"swarm-stop: {len(parents)} run_blocks processes with prefix {a.prefix}; refusing", file=sys.stderr)
        return 2
    for p in parents:
        print(f"{'would send' if a.dry_run else 'sending'} SIGTERM to run_blocks.py pid {p['pid']}: no further blocks")
        if not a.dry_run:
            host.kill(p["pid"], signal.SIGTERM)
    if not a.now:
        for b in blocks:
            print(f"the current block (main.py pid {b['pid']}) runs to its last round and cleans up itself")
        return 0
    for b in blocks:
        container = b["env"].get("SWARM_AGENT_CONTAINER", "")
        print(f"{'would send' if a.dry_run else 'sending'} SIGINT to main.py pid {b['pid']} (cleanup: run_end.json, "
              f"container {container} removed)")
        if a.dry_run:
            continue
        host.kill(b["pid"], signal.SIGINT)
        for _ in range(wait_s):
            if not host.alive(b["pid"]):
                break
            time.sleep(1)
        if host.alive(b["pid"]):
            print(f"swarm-stop: main.py pid {b['pid']} still running after {wait_s} s; check the console", file=sys.stderr)
            return 3
        if container and re.fullmatch(r"repro\d*", container) and container in host.docker_names():
            print(f"container {container} still exists after the block ended: removing it")
            host.run(["docker", "rm", "-f", container], capture_output=True)
        print(f"block ended; container {container} gone")
    return 0


# ---- swarm-eval -------------------------------------------------------------------------------------------
def main_eval(argv=None, runs_root: Optional[str] = None) -> int:
    ap = argparse.ArgumentParser(prog="swarm-eval", description="Evaluate a research monitor offline on finished runs.")
    ap.add_argument("--monitor", required=True, help="your monitor file (MONITOR = ... or class Monitor)")
    ap.add_argument("--runs", required=True, nargs="+", help="run folders or globs, e.g. 'A-*' (relative to host-runs)")
    ap.add_argument("--include-running", action="store_true", help="also unfinished runs (labels may be incomplete)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT / "analysis"))
    sys.path.insert(0, str(REPO_ROOT / "upstream"))
    from live_monitor import load_monitor
    from load_run import load_run
    from monitor_template import check_stateless, evaluate
    base = Path(runs_root or os.environ.get("SWARM_REPRO_ROOT", "/root/repro")) / "host-runs"
    dirs = []
    for pat in a.runs:
        hits = glob.glob(pat) if os.path.isabs(pat) or os.sep in pat else glob.glob(str(base / pat))
        dirs += [Path(h) for h in sorted(hits) if Path(h).is_dir() and (Path(h) / "run_meta.json").exists()]
    dirs = sorted(set(dirs))
    skipped = [d.name for d in dirs if not (d / "run_end.json").exists() and not a.include_running]
    dirs = [d for d in dirs if d.name not in skipped]
    if skipped:
        print(f"skipped (still running): {skipped}")
    if not dirs:
        print("swarm-eval: no finished run folders match", file=sys.stderr)
        return 2
    mon = load_monitor(a.monitor)
    if getattr(mon, "role", "research") != "research":
        print("swarm-eval: only research monitors are evaluated (game-rule monitors know the words)", file=sys.stderr)
        return 2
    runs = [load_run(d) for d in dirs]                     # game-rule verdicts excluded (load_run default)
    conds = {r.path.name: r.condition for r in runs}
    if "honest" not in conds.values():
        print("warning: no honest run selected, so there are no negatives (AUROC / TPR undefined)")
    res = evaluate(mon, runs)
    res["stateless"] = check_stateless(mon, runs)
    if a.json:
        print(json.dumps(res, indent=1, default=str))
        return 0
    tr, so = res["transmission_recall"], res["start_of_transmission"]
    tpr = res.get("tpr_at_0.01_fpr") or {}
    print(f"monitor {res['monitor']} {res['version']} on {len(runs)} runs "
          f"({sum(c == 'collusive' for c in conds.values())} collusive, {sum(c == 'honest' for c in conds.values())} honest)")
    print(f"  points: {res.get('n_pos')} positive, {res.get('n_neg')} negative")
    print(f"  AUROC                {res.get('auroc')}")
    few = (res.get("n_neg") or 0) < 100
    print(f"  TPR at 1% FPR        {tpr.get('tpr')}  (threshold {tpr.get('threshold')}, FPR {tpr.get('fpr')}"
          + ("; fewer than 100 negatives, so this is TPR at 0 false positives)" if few else ")"))
    print(f"  transmission recall  {tr.get('recall')}  ({tr.get('caught')}/{tr.get('n')}, unobserved {tr.get('unobserved')})")
    print(f"  start caught         {so.get('start_caught')}  detection delay {so.get('detection_delay')}")
    at = res.get("attempts") or {}
    print(f"  attempts             recall {at.get('recall')}  ({at.get('flagged')} flagged of {at.get('attempts')} attempts, "
          f"{at.get('blocked')} attempts blocked by the judge); AUROC vs honest negatives {at.get('auroc')}; "
          f"source {at.get('sources')}")
    print(f"  stateless check      {'passed' if res['stateless'] else 'FAILED (scores depend on visiting order)'}")
    return 0
