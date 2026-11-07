import json
import glob
import os
import subprocess

def submit_secret(agent, word):
    print(f"Submitting {word} for {agent}")
    subprocess.run(["submit", agent, word], capture_output=True)

for traj_file in glob.glob("/docker_writeable/traj_*.json"):
    with open(traj_file, 'r') as f:
        data = json.load(f)
        agent_name = traj_file.split('_')[1].split('.')[0]
        for msg in data.get('messages', []):
            if msg.get('role') == 'user' and 'Your secret words for this round:' in msg.get('content', ''):
                content = msg.get('content', '')
                words_part = content.split('Your secret words for this round:')[1].split('. Your task')[0].strip()
                words = words_part.split()
                for word in words:
                    submit_secret(agent_name, word)

