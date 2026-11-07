
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import get_config_from_spec
#from minisweagent.environments.docker import DockerEnvironment
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel
from discrete_agent import discrete_agent

cfg = get_config_from_spec("mini")
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

    env = LocalEnvironment(cwd="/agents/" + i, timeout=60)

    agent = discrete_agent(model, env, **{**agent_cfg, "cost_limit": 0})
    models.append(model)
    envs.append(env)
    agents.append(agent)

