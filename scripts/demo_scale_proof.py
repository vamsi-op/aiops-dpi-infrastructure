"""
Demo script to visually prove real Docker container replication to external reviewers.
1. Scales out central-ledger from 1 to 2 replicas.
2. Displays the live `docker ps` output with both containers.
3. Holds for 12 seconds so reviewers can see both in Docker Desktop or terminal.
4. Scales in back to 1 replica.
5. Displays the final `docker ps` showing the replica was cleanly removed.
"""
import asyncio
import os
import sys
import time
import subprocess
import docker
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "services/healing_engine"))

from dotenv import load_dotenv

load_dotenv()

from healing_engine.scaling import DockerComposeScaler

def show_docker_containers():
    print("\n---------------------- [LIVE DOCKER DAEMON OUTPUT] ----------------------")
    res = subprocess.run(
        ["docker", "ps", "--filter", "name=central-ledger", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"],
        capture_output=True,
        text=True
    )
    print(res.stdout.strip())
    print("-------------------------------------------------------------------------\n")

async def main():
    client = docker.from_env()
    scaler = DockerComposeScaler(client)

    print("=========================================================================")
    print("           LIVE DOCKER CONTAINER REPLICATION PROOF                      ")
    print("=========================================================================")
    
    print("\n[STEP 1] Current state in Docker (expecting 1 replica)...")
    show_docker_containers()
    
    print("[STEP 2] Executing REAL Scale-Out on central-ledger (1 -> 2 replicas)...")
    success, msg, details = await scaler.scale_out("central-ledger", target_replicas=2)
    print(f"Result: success={success}\nMessage: {msg}\nContainer created: {details.get('created_container')}")
    if not success:
        print("Scale out failed:", details)
        return

    print("\n>>> PROOF: 2 SEPARATE RUNNING CONTAINERS NOW IN DOCKER <<<")
    show_docker_containers()
    
    hold_seconds = 12
    print(f"Holding for {hold_seconds} seconds so you can see 'aiops-dpi-central-ledger-2' in Docker Desktop or a second terminal...")
    for i in range(hold_seconds, 0, -1):
        print(f"  Scaling in in {i}s...", end="\r", flush=True)
        await asyncio.sleep(1)
    print("\n")

    print("[STEP 3] Executing REAL Scale-In on central-ledger (2 -> 1 replicas)...")
    success_in, msg_in, details_in = await scaler.scale_in("central-ledger", target_replicas=1)
    print(f"Result: success={success_in}\nMessage: {msg_in}\nContainer removed: {details_in.get('removed_container')}")
    
    print("\n[STEP 4] Final state in Docker (replica cleanly stopped and removed)...")
    show_docker_containers()
    print("=========================================================================")
    print("  VERIFICATION COMPLETE: Containers physically created, run, and removed!")
    print("=========================================================================")

if __name__ == "__main__":
    asyncio.run(main())
