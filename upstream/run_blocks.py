"""Run experiment blocks back to back: by default a collusive block, then an honest block.

Proposed by Ivan for the team lead's sandbox (not his code).

Each block is one `python main.py` (not -i) with SWARM_CONDITION, SWARM_MAX_ROUNDS, a NEW random SWARM_SEED
(new tasks and new secrets per block) and SWARM_BLOCK set; main.py writes its own run folder
(<timestamp>-<condition>) and records condition, block and max rounds in run_meta.json. Every other setting
(SWARM_ISOLATION=container, SWARM_RUN_DIR, SWARM_ROUND_SECONDS, ...) is taken from the environment.

    python run_blocks.py --rounds 6                        # collusive, then honest, 6 rounds each
    python run_blocks.py --rounds 2 --order collusive      # one block
    python run_blocks.py --order collusive:18,honest:9     # rounds per block

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
    ap.add_argument("--rounds", type=int, help="rounds per block (> 0), for blocks without their own count")
    ap.add_argument("--order", default="collusive,honest",
                    help="comma-separated conditions, each optionally with its own round count: collusive:18,honest:9")
    args = ap.parse_args(argv)
    order = []
    for item in (x.strip() for x in args.order.split(",") if x.strip()):
        cond, _, n = item.partition(":")
        if cond not in ("collusive", "honest"):
            ap.error(f"unknown condition {cond!r} in --order (collusive or honest)")
        try:
            rounds = int(n) if n else args.rounds
        except ValueError:
            ap.error(f"bad round count in {item!r}")
        if rounds is None or rounds <= 0:
            ap.error(f"block {item!r} needs a round count > 0 (--rounds N or {cond}:N)")
        order.append((cond, rounds))
    if not order:
        ap.error("--order is empty")
    if os.environ.get("SWARM_ISOLATION") != "container":
        ap.error("set SWARM_ISOLATION=container (conditions need secrets, secrets need isolation)")
    runner = runner or run_block
    seeds: set[int] = set()
    for k, (condition, rounds) in enumerate(order, 1):
        seed = new_seed()
        while seed in seeds:
            seed = new_seed()
        seeds.add(seed)
        label = f"{k}/{len(order)} {condition}"
        print(f"=== block {label}: {rounds} rounds ===", flush=True)
        rc = runner(block_env(dict(os.environ), condition, rounds, seed, label))
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
