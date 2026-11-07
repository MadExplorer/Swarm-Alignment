"""Tests for upstream/agent_container.py (step 2 isolation: one agent container, one Linux user per agent).

Part 1 needs no Docker: a fake `docker` executable records the argv it was called with.
Part 2 runs against a real throwaway container (skipped when Docker or the test image is missing).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "upstream"))

from minisweagent.exceptions import Submitted  # noqa: E402
from agent_container import (AgentContainer, ContainerUserEnvironment, ContainerWorkspace,  # noqa: E402
                             home_for, user_for)
from cover_tasks import TaskManager  # noqa: E402

CRAFTED = [
    'echo "a;b" \'$(x)\' $(echo sub) `echo bt`; touch {marker}',
    "touch {marker}",
    "$(touch {marker})",
    "`touch {marker}`",
    "x' ; touch {marker} ; echo '",
    'x" ; touch {marker} ; echo "',
    "echo ok && touch {marker} || touch {marker}",
    "echo ok\ntouch {marker}",
    "-c 'touch {marker}'",
    "--rcfile {marker}",
    "echo ok > {marker}",
]


# ---- part 1: argv only (fake docker) ----------------------------------------------------------
@pytest.fixture
def fake_docker(tmp_path):
    """An executable that appends its argv (as JSON) to a log and prints 'fake-out'. No shell involved."""
    log = tmp_path / "argv.jsonl"
    exe = tmp_path / "docker"
    exe.write_text(f"#!{sys.executable}\n"
                   "import json, sys, time\n"
                   f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                   "if 'SLEEP' in sys.argv[-1]:\n    time.sleep(30)\n"
                   "print('fake-out')\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return str(exe), calls


@pytest.mark.parametrize("template", CRAFTED)
def test_crafted_command_is_one_argv_element(tmp_path, fake_docker, template):
    exe, calls = fake_docker
    marker = tmp_path / "HOST_MARKER"
    cmd = template.format(marker=marker)
    env = ContainerUserEnvironment(container="box", user="keter", executable=exe, timeout=10,
                                   env={"PAGER": "cat"})
    out = env.execute({"command": cmd})
    assert out["returncode"] == 0 and out["output"].strip() == "fake-out"
    [argv] = calls()
    assert argv == ["exec", "-u", "keter", "-w", "/home/keter", "-e", "HOME=/home/keter", "-e", "PAGER=cat",
                    "box", "bash", "-lc", "--", cmd]
    assert not marker.exists()          # nothing on the host interpreted the command


def test_no_shell_keyword_in_module():
    import ast
    tree = ast.parse((Path(__file__).resolve().parent.parent / "upstream" / "agent_container.py").read_text())
    shells = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "shell"]
    assert shells == []                  # no subprocess call in the module passes shell=... at all


def test_timeout_kills_user_processes_in_container(tmp_path, fake_docker):
    exe, calls = fake_docker
    env = ContainerUserEnvironment(container="box", user="cat", executable=exe, timeout=1)
    out = env.execute({"command": "SLEEP"})
    assert out["returncode"] == -1 and "timed out" in out["exception_info"]
    argvs = calls()
    assert argvs[-1] == ["exec", "-u", "cat", "box", "bash", "-c", "kill -9 -1"]  # as the agent's user, not root


def test_container_run_argv():
    box = AgentContainer(name="swarm-agents", image="agents:v1.1", executable="docker")
    argv = box.run_argv()
    assert argv[:2] == ["docker", "run"] and argv[-3:] == ["agents:v1.1", "sleep", "infinity"]
    assert argv[argv.index("--network") + 1] == "none"
    joined = " ".join(argv)
    for forbidden in ("-v ", "--volume", "--mount", "docker.sock", "--privileged", "--cap-add", "--device"):
        assert forbidden not in joined


def test_names_are_validated():
    assert user_for("Foxtrot") == "foxtrot" and home_for("cat") == "/home/cat"
    for bad in ("Fox trot", "../x", "Root;rm", ""):
        with pytest.raises(ValueError):
            user_for(bad)
    with pytest.raises(ValueError):
        AgentContainer(name="x; rm -rf /")
    with pytest.raises(ValueError):
        ContainerUserEnvironment(container="box", user="Bad User")


# ---- part 2: real container ------------------------------------------------------------------
TEST_IMAGE = os.environ.get("SWARM_TEST_IMAGE", "python:3.11-slim")


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", TEST_IMAGE], capture_output=True)
    return r.returncode == 0


needs_docker = pytest.mark.skipif(not _docker_ok(), reason=f"docker or image {TEST_IMAGE} not available")


@pytest.fixture(scope="module")
def box():
    b = AgentContainer(name=f"swarm-test-{uuid.uuid4().hex[:8]}", image=TEST_IMAGE)
    b.start()
    try:
        for agent in ("Alice", "Bob"):
            b.add_user(user_for(agent))
        b.verify_users()
        yield b
    finally:
        b.stop()


def env_for(box, agent, timeout=20):
    return ContainerUserEnvironment(container=box.name, user=user_for(agent), timeout=timeout)


@needs_docker
def test_real_config_and_users(box):
    info = box.verify_config()
    assert info["network"] == "none" and info["mounts"] == [] and info["privileged"] is False
    out = env_for(box, "Alice").execute({"command": "id -un; pwd; stat -c '%a %U' /home/alice"})
    assert out["output"].split("\n")[:3] == ["alice", "/home/alice", "700 alice"]


@needs_docker
def test_real_crafted_command_runs_only_inside(box, tmp_path):
    marker = tmp_path / "HOST_MARKER"
    cmd = f'echo "a;b" \'$(x)\' $(echo sub) `echo bt`; touch {marker}; echo after'
    out = env_for(box, "Alice").execute({"command": cmd})
    assert out["output"].startswith("a;b $(x) sub bt\n")    # interpreted by bash inside the container
    assert "after" in out["output"]
    assert not marker.exists()                               # the host path was never touched


@needs_docker
def test_real_leading_dash_is_a_command_not_an_option(box):
    out = env_for(box, "Alice").execute({"command": "-n echo x"})
    assert out["returncode"] == 127 and "-n: command not found" in out["output"]


@needs_docker
def test_real_homes_are_private(box):
    env_b = env_for(box, "Bob")
    assert env_b.execute({"command": "echo s3cret > /home/bob/note && cat /home/bob/note"})["output"] == "s3cret\n"
    env_a = env_for(box, "Alice")
    for cmd in ("ls /home/bob", "cat /home/bob/note", "touch /home/bob/x"):
        out = env_a.execute({"command": cmd})
        assert out["returncode"] != 0 and "Permission denied" in out["output"], cmd


@needs_docker
def test_real_no_network(box):
    # Some kernels (Docker Desktop) show idle tunnel devices in every namespace; what matters is: no routes.
    routes = env_for(box, "Alice").execute({"command": "tail -n +2 /proc/net/route"})
    assert routes["returncode"] == 0 and routes["output"].strip() == ""
    out = env_for(box, "Alice").execute({"command": "python3 -c \"import socket; "
                                                    "socket.create_connection(('1.1.1.1', 53), timeout=3)\""})
    assert out["returncode"] != 0 and "unreachable" in out["output"].lower()


@needs_docker
def test_real_timeout_kills_background_processes(box):
    env = env_for(box, "Alice", timeout=2)
    out = env.execute({"command": "sleep 300 & sleep 300"})
    assert out["returncode"] == -1 and "timed out" in out["exception_info"]
    count = ("python3 -c \"import os,pwd; u=pwd.getpwnam('alice').pw_uid; "
             "print(sum(1 for p in os.listdir('/proc') if p.isdigit() and os.stat('/proc/'+p).st_uid==u "
             "and open('/proc/'+p+'/comm').read().strip()=='sleep'))\"")
    assert env_for(box, "Bob").execute({"command": count})["output"].strip() == "0"


@needs_docker
def test_real_sentinel_still_raises_submitted(box):
    with pytest.raises(Submitted):
        env_for(box, "Alice").execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})


@needs_docker
def test_real_workspace_files_owned_by_agent_and_symlink_blocked(box, tmp_path):
    ws = ContainerWorkspace(box)
    d = ws.put_files("Alice", "task_r1_1", {"data.csv": b"a,b\n1,2\n"})
    assert d == "/home/alice/task_r1_1"
    out = env_for(box, "Alice").execute({"command": f"stat -c '%U %a' {d} {d}/data.csv; cat {d}/data.csv"})
    assert out["output"] == "alice 755\nalice 644\na,b\n1,2\n"
    assert ws.read_file("Alice", "task_r1_1", "data.csv", 100) == b"a,b\n1,2\n"
    assert ws.read_file("Alice", "task_r1_1", "data.csv", 3) == b"a,b"
    with pytest.raises(FileNotFoundError):
        ws.read_file("Alice", "task_r1_1", "answer.txt", 100)
    # Alice points her answer at Bob's file: the checker reads as Alice, so it cannot follow.
    env_for(box, "Bob").execute({"command": "echo bobs-data > /home/bob/secret"})
    env_for(box, "Alice").execute({"command": f"ln -s /home/bob/secret {d}/answer.txt"})
    with pytest.raises(FileNotFoundError):
        ws.read_file("Alice", "task_r1_1", "answer.txt", 100)


@needs_docker
def test_real_task_manager_round_trip(box, tmp_path):
    tm = TaskManager(["Alice", "Bob"], Path("/unused"), tmp_path / "run", run_seed=1, paired=False,
                     workspace=ContainerWorkspace(box), only=["log_stats"])
    texts = tm.start_round(1)
    assert "/home/alice/task_r1_1/" in texts["Alice"] and "/home/bob/task_r1_1/" in texts["Bob"]
    assert env_for(box, "Alice").execute({"command": "ls /home/alice/task_r1_1"})["output"].strip()
    assert "FAILED" in tm.on_done("Alice", "done")             # no answer yet
    env_for(box, "Bob").execute({"command": "echo 1 > /home/bob/task_r1_1/answer.txt"})
    tm.on_done("Bob", "done")
    checked = [json.loads(x) for x in (tmp_path / "run" / "tasks.jsonl").read_text().splitlines()
               if '"checked"' in x]
    assert [c["detail"] for c in checked][0] == "answer.txt not found"
    assert checked[1]["detail"] != "answer.txt not found"       # Bob's answer was read (and is wrong)


@needs_docker
def test_real_shared_tmp_is_recorded(box, tmp_path):
    env_for(box, "Alice").execute({"command": "echo hi > /tmp/from_alice; echo x > /dev/shm/shm_alice"})
    listing = box.shared_listing()
    owners = {(e["path"], e["owner"]) for e in listing}
    assert ("/tmp/from_alice", "alice") in owners and ("/dev/shm/shm_alice", "alice") in owners
    n = box.snapshot_shared(tmp_path / "shared.tar")
    assert n > 0 and b"from_alice" in (tmp_path / "shared.tar").read_bytes()


@needs_docker
def test_real_refuses_existing_container_name(box):
    with pytest.raises(RuntimeError, match="already exists"):
        AgentContainer(name=box.name, image=TEST_IMAGE).start()
