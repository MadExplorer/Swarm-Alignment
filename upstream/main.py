                                                                                  
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

agent_names = ["Foxtrot", "Keter", "Malina", "Cringe", "Cat"]                     
models = []                                                                       
envs = []                                                                         
agents = []                                                                       
                                                                                  
model_kwargs = model_cfg.pop("model_kwargs", {})                                  
                                                                                  
for i in agent_names:                                                             
    model = LitellmModel(                                                         
            model_name="openai/" + i,  # gemma = --served-model-name в vLLM       
        model_kwargs={**model_kwargs,
                      "api_base": "http://localhost:8000/v1", "api_key": "EMPTY"},
        cost_tracking="ignore_errors",                                            
        **model_cfg,                                                              
    )                                                                             
                                                                                  
    env = LocalEnvironment(cwd="/Agents/" + i, timeout=60)                        
                                                                                  
    agent = discrete_agent(model, env, **{**agent_cfg, "cost_limit": 0,
                                          "output_path": RUN_DIR / f"{i}.traj.json"})
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


print(f"Saving trajectories to {RUN_DIR}")
paused_notice_shown = False
while True:
    if stop_requested():
        if not paused_notice_shown:
            print(f"Paused: {STOP_FILE} does not contain 'false'. Waiting...")
            paused_notice_shown = True
        time.sleep(2)
        continue
    paused_notice_shown = False

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
        try:
            agent.run_step()
        except Exception:
            print(f"[{name}] step failed, this agent stops; the others continue:")
            traceback.print_exc()
        print_new_messages(name, agent)
