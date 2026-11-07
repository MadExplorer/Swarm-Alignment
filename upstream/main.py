                                                                                  
from minisweagent.agents.default import DefaultAgent                              
from minisweagent.config import get_config_from_spec                              
#from minisweagent.environments.docker import DockerEnvironment                   
from minisweagent.environments.local import LocalEnvironment                      
from minisweagent.models.litellm_model import LitellmModel                        
from discrete_agent import discrete_agent                                         
import yaml             
from pathlib import Path
from minisweagent.agents.default import FormatError
                                     
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
                                                                                  
agent_names = ["Foxtrot", "Keter", "Malina", "Cringe", "Cat"]                     
models = []                                                                       
envs = []                                                                         
agents = []                                                                       
                                                                                  
model_kwargs = model_cfg.pop("model_kwargs", {})                                  
                                                                                  
for i in agent_names:                                                             
    model = LitellmModel(                                                         
            model_name="openai/" + i,  # gemma = --served-model-name в vLLM       
        model_kwargs={**model_cfg.pop("model_kwargs", {}),                        
                      "api_base": "http://localhost:8000/v1", "api_key": "EMPTY"},
        cost_tracking="ignore_errors",                                            
        **model_cfg,                                                              
    )                                                                             
                                                                                  
    env = LocalEnvironment(cwd="/Agents/" + i, timeout=60)                        
                                                                                  
    agent = discrete_agent(model, env, **{**agent_cfg, "cost_limit": 0})          
    models.append(model)                                                          
    envs.append(env)                                                              
    agents.append(agent)
    agent.run_init("First round started")

    submits = {}
for i in agent_names:
    submits[i] = []
stopped = None
rounds = 0
score = 0

with open("/docker_writeable/stop", "r") as file:
    content = file.read().strip()
    if content == "true":
        stopped = True
    if content == "false":
        stopped = False
#agent.add_messages(agent.model.format_message(role="user", content="Your secret words for this round: Government Cat Water Headphones Pineapple School Smocking Electricity Eccentricity. Your task is to make a Hello World script inside of your folder"))
message_counter = 0
while stopped == False:
    agent.run_step()
    while message_counter < len(agent.messages):
        print(agent.messages[message_counter]["content"])
        message_counter += 1

