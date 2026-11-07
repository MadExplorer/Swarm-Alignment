# Minimal isolation without restarting vLLM: proposal (1 page)

*For the team lead's OK. Nothing of this has been run. Isolation is his step 2; this is the smallest version we could run for tonight, if he agrees.*

**Why now.**
- In both test runs, agents read other agents' trajectories within 3–4 commands (Cat; then Keter and Malina grepped `runs/` for "secret").
- As root in the shared container they can also read the harness's memory through `/proc`.
- So **no secrets can be handed out until each agent is isolated**. Without secrets there is no collusion signal in the data.

**Layout.**

```
host (his server)
├── vLLM in his container Agents        → unchanged, reached at 127.0.0.1:8000 (already published on the host)
├── harness: python main.py             → host process, venv under /root/repro/venv-host
│     runs/, stop file, secrets, task answers: only on the host (/root/repro/runs, /root/repro/stop)
└── repro-Foxtrot … repro-Cat           → one container per agent, image agents:v1.1
      --rm --network none, no host folders mounted, work dir /work
```

**How (mini-swe-agent's `DockerEnvironment`, read in 2.4.6).**
- One environment per agent: `image="agents:v1.1"`, `cwd="/work"`.
  - `run_args=["--rm", "--network", "none", "--pids-limit", "512", "--memory", "4g", "--cpus", "2", "--security-opt", "no-new-privileges", "--label", "repro=1"]`. Setting `run_args` replaces the default `["--rm"]`, so it is listed again.
  - `container_timeout="24h"`: the default `"2h"` would stop the agents in the middle of the night.
- **Naming.** `DockerEnvironment` names containers `minisweagent-<hex>` itself. A 10-line subclass overrides `_start_container` to use `repro-<agent>`, so every container of ours is recognisable and can be cleaned up with `docker rm -f repro-<agent>`.
- **What reaches the agent.** It gets only what we pass in `env` / `forward_env`, not the harness's environment (unlike `LocalEnvironment`). Each command runs as `docker exec … bash -lc <command>`.
- **Task files.** They are copied in with `docker cp` at round start; answers are read back with `docker exec cat` (a small file-I/O adapter in `TaskManager`).
- **Checked in the image `agents:v1.1`.**
  - Present: `python3` 3.12 with the `sqlite3` module.
  - Absent: the `sqlite3` CLI and `jq`.
  - The image contains no code of ours: `/agents_programm` is no longer mounted, so agents cannot read the harness.
- **Code changes.**
  - `main.py`: `DockerEnvironment` instead of `LocalEnvironment`; run dir, stop file and `swarm.yaml` paths from env vars; `docker rm -f repro-*` at exit.
  - `cover_tasks.py`: the file adapter.
  - `discrete_agent.py`, `comms.py`, `monitors.py`: unchanged.

**What this does not fix.**
- Covert channels through shared host resources (timing, CPU load): theoretical, noted as a limitation.
- Agents still run as root inside their own container.
- A command that times out may keep running inside the container: `subprocess` kills only the `docker exec` client. Not verified.

**Verification before any secret is handed out.** From inside one agent container we check that:
- `ls /docker_writeable /agents_programm` fails;
- `cat /proc/1/cmdline` shows only `sleep`;
- network access fails;
- `ls /` shows no other agent's files.

Only then would his step 5 hand out secrets.

**Host facts (read-only, Sat 08:5x UTC).**
- Python 3.10.12 with `venv`; mini-swe-agent and litellm both allow `>=3.10`.
- Whether our 3.12 pins (`constraints.txt`) all install on 3.10 is **untested**. Fallback: a standalone Python 3.12 via `uv`, installed under `/root/repro` (downloads from the internet on his host).
- Docker 28.1.1, 122 CPUs, 197 GB RAM; GPUs are not needed for the agent containers.

**What we need from the team lead.**
1. OK to run a harness process and five containers `repro-<agent>` on his server. They would write only under `/root/repro`, use no GPUs, and send no traffic except to `127.0.0.1:8000`.
2. OK to send the overnight run's load to his vLLM (5 agents, about one call every 3 s each; Phase B: 21 calls per agent in 70 s).
3. Whether this becomes his step 2, or stays our parallel setup.
4. Secrets and scoring (his step 5) only after verification.

**Estimate.**
- Code and offline tests: about 2–3 h (subclass 0.5 h, file adapter 0.75 h, main/config 0.5 h, tests 0.5–1 h).
- Host venv: 0.5 h (plus 0.25 h if the 3.12 fallback is needed).
- Test in repro: 0.75 h.
- **Total about 3.5–4.5 h** after his OK. Started by ~14:00 UTC, it is ready well before the overnight run.
