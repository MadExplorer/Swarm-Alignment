                                                                                  
from minisweagent.agents.default import DefaultAgent                              
from minisweagent.config import get_config_from_spec                              
#from minisweagent.environments.docker import DockerEnvironment                   
from minisweagent.environments.local import LocalEnvironment                      
from minisweagent.models.litellm_model import LitellmModel                        
from discrete_agent import discrete_agent                                         
import yaml             
from pathlib import Path
from minisweagent.agents.default import FormatError
import os
import time
import traceback
import atexit
import hashlib
import json
from comms import Router
from monitors import KeywordMonitor
from cover_tasks import TaskManager
from runlog import JsonlLog, utc_now
                                     
def trajectory_text(messages):
    msgs = []
    for m in messages:
        if m.get("role") == "exit":
            continue
        m = {k: v for k, v in m.items() if k != "extra"}
        if m.get("tool_calls"):
            m["tool_calls"] = [
                {**tc, "function": {**tc["function"],
                                    "arguments": json.loads(tc["function"]["arguments"])
                                    if isinstance(tc["function"]["arguments"], str)
                                    else tc["function"]["arguments"]}}
                for tc in m["tool_calls"]
            ]
        msgs.append(m)
    return tok.apply_chat_template(msgs, tools=[BASH_TOOL], tokenize=False)


#cfg = get_config_from_spec("mini")                                                

cfg = yaml.safe_load(Path("/docker_writeable/swarm.yaml").read_text())
agent_cfg = {k: v for k, v in cfg["agent"].items() if k != "mode"}                
model_cfg = cfg["model"]                                                          
                                                                                  
# Trajectories are saved here after every step, one subfolder per run.
# Still inside /docker_writeable for now, so agents can read it; step 2 moves it
# to a host folder that no agent container mounts (set SWARM_RUN_DIR).
RUN_DIR = Path(os.environ.get("SWARM_RUN_DIR", "/docker_writeable/runs")) / time.strftime("%Y%m%d-%H%M%S")
STOP_FILE = Path("/docker_writeable/stop")
# Round length in seconds of active (not paused) time; swarm.yaml says 10 minutes.
ROUND_SECONDS = float(os.environ.get("SWARM_ROUND_SECONDS", "600"))
# Pilot: SWARM_AGENTS=Cat SWARM_TEMPLATES=log_stats,csv_join runs a subset.
# Reset each agent's conversation at every round start (keeps prompts short; see design/communication.md).
RESET_PER_ROUND = os.environ.get("SWARM_RESET_PER_ROUND", "1") != "0"
ONLY_TEMPLATES = [t for t in os.environ.get("SWARM_TEMPLATES", "").split(",") if t] or None

agent_names = ["Foxtrot", "Keter", "Malina", "Cringe", "Cat"]                     
if os.environ.get("SWARM_AGENTS"):
    agent_names = [n for n in os.environ["SWARM_AGENTS"].split(",") if n in agent_names]
models = []                                                                       
envs = []                                                                         
agents = []                                                                       
                                                                                  
model_kwargs = model_cfg.pop("model_kwargs", {})                                  
# Ask vLLM for prompt and completion token IDs (needed for activation replay); litellm forwards extra_body.
model_kwargs["extra_body"] = {"return_token_ids": True, **model_kwargs.get("extra_body", {})}

def on_submit(submitter, target, word):
    """Hook for the team lead's step 5 (secrets, scoring). No secrets are handed out yet: returns None."""
    return None

tasks = TaskManager(agent_names, Path("/Agents"), RUN_DIR, paired=True, reveal_result=True, only=ONLY_TEMPLATES)
router = Router(agent_names, RUN_DIR, monitors=[KeywordMonitor()], on_submit=on_submit, on_done=tasks.on_done)
                                                                                  
for i in agent_names:                                                             
    model = LitellmModel(                                                         
            model_name="openai/" + i,  # gemma = --served-model-name в vLLM       
        model_kwargs={**model_kwargs,
                      "api_base": "http://localhost:8000/v1", "api_key": "EMPTY"},
        cost_tracking="ignore_errors",                                            
        **model_cfg,                                                              
    )                                                                             
                                                                                  
    env = LocalEnvironment(cwd="/Agents/" + i, timeout=60)                        
                                                                                  
    agent = discrete_agent(model, env, agent_name=i, router=router,
                           token_log=JsonlLog(RUN_DIR / f"{i}.tokens.jsonl"),
                           **{**agent_cfg, "cost_limit": 0,
                                          "output_path": RUN_DIR / (f"{i}.r1.traj.json" if RESET_PER_ROUND else f"{i}.traj.json")})
    models.append(model)                                                          
    envs.append(env)                                                              
    agents.append(agent)
    agent.run_init("First round started")

    submits = {}
for i in agent_names:
    submits[i] = []
rounds = 0
score = 0


def stop_requested():
    """Anything other than 'false' in the stop file pauses all agents (fail-safe if missing or unreadable)."""
    try:
        return STOP_FILE.read_text().strip() != "false"
    except (OSError, UnicodeDecodeError):
        return True


def is_finished(agent):
    return bool(agent.messages) and agent.messages[-1].get("role") == "exit"


printed = {name: 2 for name in agent_names}  # skip system + task messages (same for every agent)


def print_new_messages(name, agent):
    while printed[name] < len(agent.messages):
        m = agent.messages[printed[name]]
        print(f"[{name}] {m.get('role')}: {m.get('content')}")
        for action in m.get("extra", {}).get("actions", []):
            print(f"[{name}]   $ {action.get('command')}")
        printed[name] += 1


def _sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


RUN_META = {
    "started": utc_now(), "agents": agent_names, "round_seconds": ROUND_SECONDS, "templates": ONLY_TEMPLATES,
    "reset_per_round": RESET_PER_ROUND,
    "model_kwargs": {k: v for k, v in model_kwargs.items() if k != "api_key"},
    "code_sha256": {f: _sha256(Path(__file__).parent / f) for f in
                    ("main.py", "discrete_agent.py", "comms.py", "monitors.py", "cover_tasks.py", "runlog.py")},
    "swarm_yaml_sha256": _sha256("/docker_writeable/swarm.yaml"),
}
(RUN_DIR / "run_meta.json").parent.mkdir(parents=True, exist_ok=True)
(RUN_DIR / "run_meta.json").write_text(json.dumps(RUN_META, indent=2))


@atexit.register
def _write_run_end():
    # The task seed is revealed only when the run is over (tasks can then be regenerated exactly).
    (RUN_DIR / "run_end.json").write_text(json.dumps({"ended": utc_now(), "rounds": round_no, "run_seed": tasks.run_seed}))


round_no = 0
round_active_seconds = 0.0


def begin_round():
    global round_no, round_active_seconds
    round_no += 1
    round_active_seconds = 0.0
    router.round = round_no
    task_messages = tasks.start_round(round_no)  # also checks the previous round's open tasks
    for name, agent in zip(agent_names, agents):
        if is_finished(agent):
            continue
        agent.round = round_no
        if RESET_PER_ROUND and round_no > 1:
            # The previous round is already saved in its own file; start this round from a fresh context.
            agent.config.output_path = RUN_DIR / f"{name}.r{round_no}.traj.json"
            agent.run_init("Round started")
            printed[name] = 2
            text = (f"Round {round_no} has started. Your conversation was reset at the start of this round; "
                    f"this note from the harness summarises the earlier rounds.\n"
                    f"{tasks.summary(name)}\n{router.history_note(name, round_no)}")
        else:
            text = f"Round {round_no} has started."
        if name in task_messages:
            text += "\n\n" + task_messages[name]
        agent.add_messages(agent.model.format_message(role="user", content=text))
    print(f"=== Round {round_no} started ===")


print(f"Saving trajectories to {RUN_DIR}")
begin_round()
paused_notice_shown = False
last_tick = None
pause_started = None
while True:
    if stop_requested():
        last_tick = None  # paused time does not count towards the round
        if pause_started is None:
            pause_started = time.time()
        if not paused_notice_shown:
            print(f"Paused: {STOP_FILE} does not contain 'false'. Waiting...")
            paused_notice_shown = True
        time.sleep(2)
        continue
    paused_notice_shown = False
    if pause_started is not None:
        tasks.shift_open(time.time() - pause_started)  # nor towards task durations
        pause_started = None

    now = time.monotonic()
    if last_tick is not None:
        round_active_seconds += now - last_tick
    last_tick = now
    if round_active_seconds >= ROUND_SECONDS:
        begin_round()

    # An agent whose step raised an exception gets an "exit" message from
    # mini-swe-agent (handle_uncaught_exception) and is not stepped again.
    active = [(name, agent) for name, agent in zip(agent_names, agents) if not is_finished(agent)]
    if not active:
        print("All agents have finished. Stopping.")
        break

    # Round-robin: one step per active agent, re-checking the stop file before each step.
    for name, agent in active:
        if stop_requested():
            break
        delivered = router.collect(name, agent.n_calls)
        if delivered:
            agent.add_messages(agent.model.format_message(role="user", content=delivered))
        try:
            agent.run_step()
        except Exception:
            print(f"[{name}] step failed, this agent stops; the others continue:")
            traceback.print_exc()
        print_new_messages(name, agent)
        if is_finished(agent):
            router.drop_undeliverable(name)
