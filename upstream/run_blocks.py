"""Run experiment blocks back to back: by default a collusive block, then an honest block.

Proposed by Ivan for the team lead's sandbox (not his code).

Each block is one `python main.py` (not -i) with SWARM_CONDITION, SWARM_MAX_ROUNDS, a NEW random SWARM_SEED
(new tasks and new secrets per block) and SWARM_BLOCK set; main.py writes its own run folder
(<timestamp>-<condition>) and records condition, block and max rounds in run_meta.json. Every other setting
(SWARM_ISOLATION=container, SWARM_RUN_DIR, SWARM_ROUND_SECONDS, ...) is taken from the environment.

    python run_blocks.py --rounds 6                        # collusive, then honest
    python run_blocks.py --rounds 2 --order collusive      # one block

Ctrl-C reaches the running block too (same terminal); this script then waits for that block's own cleanup
(shared dirs recorded, container removed, run_end.json) and starts no further block. It never kills a block.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def new_seed() -> int:
    return int.from_bytes(os.urandom(8), "big")


def block_env(base: dict, condition: str, rounds: int, seed: int, label: str) -> dict:
    return {**base, "SWARM_CONDITION": condition, "SWARM_MAX_ROUNDS": str(rounds),
            "SWARM_SEED": str(seed), "SWARM_BLOCK": label}


def main(argv=None, runner=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rounds", type=int, required=True, help="rounds per block (> 0)")
    ap.add_argument("--order", default="collusive,honest", help="comma-separated conditions")
    args = ap.parse_args(argv)
    order = [c for c in args.order.split(",") if c]
    if args.rounds <= 0 or not order or any(c not in ("collusive", "honest") for c in order):
        ap.error("--rounds must be > 0 and --order a list of collusive/honest")
    if os.environ.get("SWARM_ISOLATION") != "container":
        ap.error("set SWARM_ISOLATION=container (conditions need secrets, secrets need isolation)")
    runner = runner or run_block
    seeds: set[int] = set()
    for k, condition in enumerate(order, 1):
        seed = new_seed()
        while seed in seeds:
            seed = new_seed()
        seeds.add(seed)
        label = f"{k}/{len(order)} {condition}"
        print(f"=== block {label}: {args.rounds} rounds ===", flush=True)
        rc = runner(block_env(dict(os.environ), condition, args.rounds, seed, label))
        if rc != 0:
            print(f"=== block {label} ended with exit code {rc}; no further blocks ===", flush=True)
            return rc
    return 0


def run_block(env: dict) -> int:
    p = subprocess.Popen([sys.executable, str(HERE / "main.py")], cwd=str(HERE), env=env)
    while True:
        try:
            return p.wait()
        except KeyboardInterrupt:
            # The block got the same Ctrl-C and cleans up itself; wait for it, then stop.
            print("Ctrl-C: waiting for the block to clean up ...", flush=True)
            p.wait()
            return 130


if __name__ == "__main__":
    sys.exit(main())
