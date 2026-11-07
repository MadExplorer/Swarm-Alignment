# Isolation: one agent container, one Linux user per agent (1 page)

*Follows the team lead's decision (Discord, Sat 15:28 his time): all agents share ONE container and the harness runs outside it. Separate Linux users per agent were Ivan's proposal to him, with no objection. Code: branch `isolation` (67da88f, on top of `overnight`). Tested locally only; nothing has been run on the server.*

**Why.** In both test runs, agents read other agents' trajectories within 3–4 commands: Cat, then Keter and Malina grepping `runs/` for "secret". As root next to the harness they could also read its memory via `/proc`. So no secrets until this is in place.

**Layout.**

```
host (his server)
├── vLLM in his container Agents   → unchanged, reached at 127.0.0.1:8000 (published on the host)
├── harness: python main.py        → host process, own venv (Python 3.10, see below)
│     runs/, logs, secrets, answers, stop file, swarm.yaml: host only
└── ONE agent container            → image agents:v1.1, started by the harness
      docker run -d --rm --name <name> --network none --init --security-opt no-new-privileges
                 agents:v1.1 sleep infinity
      no host folders, no docker.sock, not privileged
      users foxtrot keter malina cringe cat, homes /home/<user> chmod 700
```

**How it works (`upstream/agent_container.py`).**
- `AgentContainer.start()`:
  - refuses if a container with that name already exists (it never removes something it did not start);
  - starts the container, then checks with `docker inspect`: network `none`, no mounts or binds, not privileged, no added capabilities or devices. On any problem it stops the container and the harness does not start.
- **Users:** `useradd --create-home` and `chmod 700` for each agent. Then `verify_users()` checks every pair: no user can list another user's home, and no user is root.
- `ContainerUserEnvironment`, a subclass of `LocalEnvironment`, runs every command as:
  ```
  ["docker","exec","-u",user,"-w",home,"-e","HOME=…",container,"bash","-lc","--",command]
  ```
  - `subprocess` gets an argument list; no `shell=` keyword appears anywhere in the module (a test checks this).
  - The `--` matters: without it, `bash -lc '-n …'` treats the command as an option. mini-swe-agent's own environments have this edge case.
  - It keeps `LocalEnvironment`'s output format, timeout and sentinel check.
  - **On timeout** it also runs `kill -9 -1` as that user inside the container. Killing the `docker exec` client alone leaves the command running, and that's true of mini-swe-agent's `DockerEnvironment` too.
- **Task files.** `ContainerWorkspace` copies them in as a tar on stdin, extracted *by the agent's user* (`docker exec -i -u <user> … tar -x`), so the agent owns them. Answers are read back as that user (`head -c`), so a symlink to another home fails like any other read.
- **`main.py` settings.**
  - `SWARM_ISOLATION=container` turns this on (default `none` = today's setup). `SWARM_RUN_DIR` is then required.
  - `SWARM_CONFIG`, `SWARM_STOP_FILE` and `SWARM_API_BASE` set the paths and the vLLM address.
  - `SWARM_AGENT_CONTAINER` (default `swarm-agents`) and `SWARM_AGENT_IMAGE` (default `agents:v1.1`) set the container.
  - The container stops at exit (`--rm` removes it).
- **Not used:** mini-swe-agent's `DockerEnvironment`. It always starts its own container in `__init__`, has no user option, and its cleanup calls `shell=True` on the host.

**Image `agents:v1.1` (read-only check via `Agents`).** Debian 13, bash 5.2.37, `python3` 3.12.16 with sqlite 3.46.1, and `useradd`, `tar`, `timeout`, `pkill`, `setpriv` all present. `sqlite3` CLI and `jq` absent. The image's default user is root, and the agents get no harness code: nothing is mounted.

**Host venv.**
- **His `/agents_programm/venv` is not usable on the host.** It was created inside the container (`python3 -m venv /docker_writeable/venv`), its `python3` links to `/usr/local/bin/python3`, which the host lacks, and `pip`'s shebang is `/docker_writeable/...`.
- **The host has Python 3.10.12.** His 3.12 pins don't resolve on 3.10 (`platformdirs==4.14.5` needs ≥3.11).
- **Tested set:** `deploy/requirements-host-py310.txt`, with 17 packages differing from his pins, including `pandas` 2.3.3 vs 3.1.1. `mini-swe-agent` 2.4.6 and `litellm` 1.108.1 are unchanged. All 178 tests pass on CPython 3.10.20.

**Tests (all local).** 26 for isolation (`tests/test_isolation.py`) and 11 for scoring; 178 in total with the earlier ones.
- **Without Docker,** a fake `docker` program records its argv. 11 crafted commands (quotes, `;`, `&&`/`||`, `$(…)`, backticks, newline, a leading `-`, redirection to a host path) each arrive as exactly one argv element, and the host marker file is never created.
- **With a real throwaway container** (`python:3.11-slim`, also Debian 13):
  - commands run as the user in their home;
  - other homes give "Permission denied" for list, read and write;
  - there's no route out;
  - a timed-out `sleep 300 & sleep 300` leaves no process behind;
  - the sentinel still raises;
  - task files are owned by the agent, and the symlink to another home is blocked;
  - `TaskManager` works end to end;
  - an existing container name is refused.
- **Smoke run of `main.py`** in container mode against a fake OpenAI server: 2 agents, 2 rounds, through litellm. Everything above held, and the container was gone after Ctrl-C.

**Limitations (for the report).**
- **Shared places:** `/tmp`, `/var/tmp`, `/run/lock` and `/dev/shm` are writable by every agent. By decision they stay open. The harness records their listing (owner, size, mtime) at each round end in `shared_dirs.jsonl`, plus a tar at exit, for an offline scan.
- **Process list:** `/proc/<pid>/cmdline` of other users is visible, so a running command line is a (short-lived) channel.
- **Background processes** started with `&`/`nohup` can stay alive and talk over unix sockets (including abstract ones, which `--network none` doesn't block).
- **Timing and load:** CPU and timing covert channels are theoretical.
- **One kernel** is shared with the host, as for any container.

Options we haven't built (his call): `--pids-limit`/`--memory`/`--cpus`, killing each user's leftover processes after every step, and closing `/tmp` (`TMPDIR=$HOME/tmp` plus `chmod 1770`).

**Next steps (each needs Ivan's OK; nothing on the server yet).**
1. **Repro test on the server.**
   - Host venv `/root/repro/venv-host` from `deploy/requirements-host-py310.txt`. That downloads from PyPI on his host.
   - Run dir, stop file and `swarm.yaml` under `/root/repro`.
   - Agent container named `repro`, once the old `repro` container is gone, so hard rule 2 holds as written.
   - Short run, 2 rounds.
2. **Deploy to `/agents_programm`** only after it's tested and after a timestamped backup: `cp -a /agents_programm /root/backup/agents_programm-<UTC timestamp>`, plus his `/docker_writeable/swarm.yaml` and `stop`.

**Estimate.** Repro test about 1 h (venv 15 min, run and checks 45 min); deploy 15 min after his OK.
