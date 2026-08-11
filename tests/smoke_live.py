#!/usr/bin/env python3
"""Live smoke test against a running game + UnrealEngineMCP bridge."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "translator"))

from bridge_client import BridgeClient, BridgeError  # noqa: E402


DEFAULT_IPC = Path(
    r"D:\SteamLibrary\steamapps\common"
    r"\SpongeBob SquarePants Battle for Bikini Bottom - Rehydrated"
    r"\Pineapple\Binaries\Win64\UnrealEngineMCP_IPC"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipc-dir", type=Path, default=DEFAULT_IPC)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--wait-heartbeat", type=float, default=120.0)
    args = parser.parse_args()

    ipc = args.ipc_dir
    print(f"IPC dir: {ipc}")
    client = BridgeClient(ipc_dir=ipc, timeout_seconds=args.timeout)

    deadline = time.time() + args.wait_heartbeat
    print("Waiting for heartbeat...")
    while time.time() < deadline:
        if client.is_alive(max_age_seconds=15.0):
            print("Heartbeat OK:", json.dumps(client.read_heartbeat(), indent=2))
            break
        time.sleep(0.5)
    else:
        print("FAIL: no heartbeat. Is the game running with UE4SS + UnrealEngineMCP?")
        return 1

    steps = [
        ("ping", {}),
        ("status", {}),
        ("get_player", {}),
        ("list_actors", {"limit": 15}),
        ("find_objects", {"class": "PlayerController", "limit": 5}),
    ]

    failed = 0
    for cmd, params in steps:
        print(f"\n=== {cmd} {params} ===")
        try:
            result = client.call(cmd, params, timeout=args.timeout)
            print(json.dumps(result, indent=2, default=str)[:4000])
            if result.get("ok") is False:
                failed += 1
        except BridgeError as exc:
            print(f"FAIL: {exc}")
            failed += 1

    # Optional property read on player controller / pawn if present
    try:
        player = client.call("get_player")
        target = (player or {}).get("controller") or (player or {}).get("pawn") or {}
        full = target.get("full_name")
        class_short = target.get("class_short") or ""
        if full:
            print(f"\n=== get_properties {target.get('name')} ===")
            props = client.call(
                "get_properties",
                {
                    "full_name": full,
                    "class": class_short,
                    "limit": 40,
                },
                timeout=args.timeout,
            )
            print(json.dumps(props, indent=2, default=str)[:4000])
            if props.get("ok"):
                print("\n=== list_functions (sample) ===")
                funcs = client.call(
                    "list_functions",
                    {"full_name": full, "class": class_short, "limit": 20},
                    timeout=args.timeout,
                )
                print(json.dumps(funcs, indent=2, default=str)[:3000])
        else:
            print("property probe skipped (no controller/pawn yet — still on boot?)")
    except BridgeError as exc:
        print(f"property probe skipped/failed: {exc}")

    if failed:
        print(f"\nSMOKE FAILED ({failed} step(s))")
        return 2
    print("\nSMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
