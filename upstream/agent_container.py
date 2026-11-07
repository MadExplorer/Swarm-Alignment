"""Isolation for the team lead's step 2: all agents share ONE container, each as its own Linux user;
the harness runs on the host, outside that container.

Proposed by Ivan for the team lead's sandbox (not his code). Design: design/isolation-1page.md.

- AgentContainer starts the agent container (no network, no host folders, no docker.sock, not
  privileged, `sleep infinity`), creates one user per agent with a home folder chmod 700, and checks
  this setup with `docker inspect` before any agent runs.
- ContainerUserEnvironment is a LocalEnvironment whose commands run as
  ["docker", "exec", "-u", user, "-w", home, container, "bash", "-lc", "--", command].
  The command is one argv element: the host never runs a shell (no shell=True anywhere in this file).
- ContainerWorkspace puts task files into the agent's home (as that user, via tar on stdin) and reads
  answers back as that user, so a symlink in an agent's home cannot reach another agent's files.

Not closed (see the Limitations section of the design): /tmp, /var/tmp, /run/lock, /dev/shm are
shared between users; processes are visible in /proc; background processes can talk over unix sockets.
shared_listing() / snapshot_shared() record what was there, for an offline scan.
"""
from __future__ import annotations

import io
import json
import os
import re
import signal
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any, Optional

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig

DOCKER = os.environ.get("SWARM_DOCKER", "docker")
SHARED_DIRS = ["/tmp", "/var/tmp", "/run/lock", "/dev/shm"]
_USER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")  # docker container / task folder / file names


def user_for(agent_name: str) -> str:
    user = agent_name.lower()
    if not _USER_RE.match(user):
        raise ValueError(f"agent name {agent_name!r} does not give a valid Linux user name")
    return user


def home_for(user: str) -> str:
    return f"/home/{user}"


def _check_name(name: str, what: str) -> str:
    if not _NAME_RE.match(name) or ".." in name:
        raise ValueError(f"invalid {what}: {name!r}")
    return name


class AgentContainer:
    """The one agent container. The harness owns it: starts it, adds users, stops it at exit."""

    def __init__(self, name: str = "swarm-agents", image: str = "agents:v1.1", executable: str = DOCKER,
                 extra_run_args: Optional[list[str]] = None):
        self.name = _check_name(name, "container name")
        self.image = image
        self.executable = executable
        self.extra_run_args = list(extra_run_args or [])
        self.users: list[str] = []

    # ---- argv builders (pure, unit-tested) -----------------------------------------------
    def run_argv(self) -> list[str]:
        return [self.executable, "run", "-d", "--rm", "--name", self.name,
                "--network", "none",            # no network at all (only lo)
                "--init",                       # PID 1 reaps the zombies of finished docker exec children
                "--security-opt", "no-new-privileges",  # setuid binaries cannot raise an agent to root
                *self.extra_run_args,
                self.image, "sleep", "infinity"]

    def exec_argv(self, args: list[str], user: str = "root", workdir: Optional[str] = None,
                  stdin: bool = False, env: Optional[dict[str, str]] = None) -> list[str]:
        argv = [self.executable, "exec"]
        if stdin:
            argv.append("-i")
        argv += ["-u", user]
        if workdir:
            argv += ["-w", workdir]
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        return argv + [self.name, *args]

    # ---- lifecycle -----------------------------------------------------------------------
    def _run(self, argv: list[str], timeout: float = 60, input: Optional[bytes] = None,
             check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(argv, input=input, capture_output=True, timeout=timeout, check=check)

    def exists(self) -> bool:
        r = self._run([self.executable, "inspect", "--type", "container", self.name], check=False)
        return r.returncode == 0

    def start(self) -> None:
        if self.exists():
            # Never remove a container we did not start: the operator decides.
            raise RuntimeError(f"a container named {self.name!r} already exists; remove it or set "
                               f"SWARM_AGENT_CONTAINER to another name")
        self._run(self.run_argv(), timeout=120)
        self.verify_config()

    def stop(self) -> None:
        """Stop the container (--rm removes it). Safe to call twice."""
        self._run([self.executable, "stop", "-t", "2", self.name], timeout=60, check=False)

    def verify_config(self) -> dict:
        """Refuse to go on unless the running container really has no network, mounts, or privileges."""
        r = self._run([self.executable, "inspect", "--type", "container", self.name])
        info = json.loads(r.stdout)[0]
        hc = info["HostConfig"]
        problems = []
        if hc.get("NetworkMode") != "none":
            problems.append(f"NetworkMode={hc.get('NetworkMode')!r}")
        if info.get("Mounts"):
            problems.append(f"Mounts={[m.get('Source') or m.get('Name') for m in info['Mounts']]}")
        if hc.get("Binds"):
            problems.append(f"Binds={hc['Binds']}")
        if hc.get("Privileged"):
            problems.append("Privileged=true")
        if hc.get("CapAdd"):
            problems.append(f"CapAdd={hc['CapAdd']}")
        if hc.get("Devices"):
            problems.append(f"Devices={hc['Devices']}")
        if problems:
            self.stop()
            raise RuntimeError("agent container is not isolated: " + "; ".join(problems))
        return {"network": hc.get("NetworkMode"), "mounts": [], "privileged": False,
                "image": info.get("Config", {}).get("Image"), "id": info.get("Id", "")[:12]}

    def add_user(self, user: str) -> None:
        if not _USER_RE.match(user):
            raise ValueError(f"invalid user name {user!r}")
        home = home_for(user)
        self._run(self.exec_argv(["useradd", "--create-home", "--home-dir", home, "--shell", "/bin/bash",
                                  "--user-group", user]))
        self._run(self.exec_argv(["chmod", "700", home]))
        self.users.append(user)

    def verify_users(self) -> None:
        """Each user must be unable to list another user's home (checked for every pair)."""
        for a in self.users:
            for b in self.users:
                if a == b:
                    continue
                r = self._run(self.exec_argv(["ls", home_for(b)], user=a), check=False)
                if r.returncode == 0:
                    raise RuntimeError(f"isolation check failed: {a} can list {home_for(b)}")
            r = self._run(self.exec_argv(["id", "-u"], user=a))
            if r.stdout.strip() == b"0":
                raise RuntimeError(f"isolation check failed: {a} runs as root")

    # ---- shared places (not closed; recorded for the offline scan) ----------------------
    def shared_listing(self) -> list[dict]:
        """Every file in the shared world-writable places: owner, size, mtime, path."""
        r = self._run(self.exec_argv(["find", *SHARED_DIRS, "-xdev", "-mindepth", "1",
                                      "-printf", r"%u\t%s\t%T@\t%y\t%p\n"]), check=False)
        out = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t", 4)
            if len(parts) == 5:
                out.append({"owner": parts[0], "size": int(parts[1]), "mtime": float(parts[2]),
                            "type": parts[3], "path": parts[4]})
        return out

    def snapshot_shared(self, dest: Path, max_bytes: int = 50_000_000) -> int:
        """Tar of the shared places, saved on the host (capped). Returns the number of bytes written."""
        r = self._run(self.exec_argv(["tar", "-c", "-f", "-", "--ignore-failed-read", *SHARED_DIRS]),
                      timeout=120, check=False)
        data = r.stdout[:max_bytes]
        Path(dest).write_bytes(data)
        return len(data)


class ContainerWorkspace:
    """Task folders inside the agent container, in each agent's home, owned by that agent's user."""

    def __init__(self, box: AgentContainer):
        self.box = box

    def task_dir(self, agent: str, dirname: str) -> str:
        return f"{home_for(user_for(agent))}/{_check_name(dirname, 'folder name')}"

    def put_files(self, agent: str, dirname: str, files: dict[str, bytes]) -> str:
        user = user_for(agent)
        _check_name(dirname, "folder name")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            d = tarfile.TarInfo(dirname)
            d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
            tar.addfile(d)
            for fname, data in files.items():
                ti = tarfile.TarInfo(f"{dirname}/{_check_name(fname, 'file name')}")
                ti.size, ti.mode, ti.mtime = len(data), 0o644, int(time.time())
                tar.addfile(ti, io.BytesIO(data))
        # Extracted by the agent's own user (not root): the files are owned by that user.
        self.box._run(self.box.exec_argv(["tar", "-x", "-f", "-", "--no-same-owner", "--no-same-permissions"],
                                         user=user, workdir=home_for(user), stdin=True),
                      input=buf.getvalue(), timeout=120)
        return self.task_dir(agent, dirname)

    def read_file(self, agent: str, dirname: str, fname: str, max_bytes: int) -> bytes:
        """Read as the agent's user: whatever that user cannot read (another home), the checker cannot either."""
        user = user_for(agent)
        path = f"{self.task_dir(agent, dirname)}/{_check_name(fname, 'file name')}"
        r = self.box._run(self.box.exec_argv(["head", "-c", str(max_bytes), "--", path], user=user), check=False)
        if r.returncode != 0:
            raise FileNotFoundError(r.stderr.decode("utf-8", "replace").strip() or path)
        return r.stdout


class ContainerUserEnvironmentConfig(LocalEnvironmentConfig):
    container: str
    user: str
    executable: str = DOCKER
    interpreter: list[str] = ["bash", "-lc", "--"]  # "--": a command starting with "-" is not a bash option


class ContainerUserEnvironment(LocalEnvironment):
    """mini-swe-agent environment: every command runs in the shared agent container as the agent's user.

    Same contract as LocalEnvironment.execute (output dict, timeout handling, the sentinel check), but the
    host only ever runs `docker exec` with an argument list."""

    def __init__(self, **kwargs):
        super().__init__(config_class=ContainerUserEnvironmentConfig, **kwargs)
        if not _USER_RE.match(self.config.user):
            raise ValueError(f"invalid user name {self.config.user!r}")
        if not self.config.cwd:
            self.config.cwd = home_for(self.config.user)

    def argv(self, command: str, cwd: str = "") -> list[str]:
        c = self.config
        argv = [c.executable, "exec", "-u", c.user, "-w", cwd or c.cwd, "-e", f"HOME={home_for(c.user)}"]
        for k, v in c.env.items():
            argv += ["-e", f"{k}={v}"]
        return argv + [c.container, *c.interpreter, command]

    def kill_user_processes(self) -> None:
        """After a timeout: kill everything this user still runs in the container (only its own processes)."""
        c = self.config
        subprocess.run([c.executable, "exec", "-u", c.user, c.container, "bash", "-c", "kill -9 -1"],
                       capture_output=True, timeout=30, check=False)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        timeout = timeout or self.config.timeout
        try:
            stdout, returncode = self._run(self.argv(command, cwd), timeout)
            output = {"output": stdout, "returncode": returncode, "exception_info": ""}
        except Exception as e:  # same shape as LocalEnvironment
            raw_output = getattr(e, "output", None)
            raw_output = (raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes)
                          else (raw_output or ""))
            output = {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        self._check_finished(output)
        return output

    def _run(self, argv: list[str], timeout: float) -> tuple[str, int]:
        process = subprocess.Popen(argv, text=True, encoding="utf-8", errors="replace",
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=os.name == "posix")
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Killing the docker client does not stop the command inside the container: kill it there too.
            try:
                os.killpg(process.pid, signal.SIGKILL) if os.name == "posix" else process.kill()
            except ProcessLookupError:
                pass
            self.kill_user_processes()
            stdout, _ = process.communicate()
            raise subprocess.TimeoutExpired(argv[-1], timeout, output=stdout)
        return stdout, process.returncode

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        # Like LocalEnvironment, minus the host's environment variables (they describe the host, not the agent).
        import platform
        from minisweagent.utils.serialize import recursive_merge
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), kwargs)
