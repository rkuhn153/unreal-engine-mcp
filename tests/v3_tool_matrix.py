#!/usr/bin/env python3
"""v3 acceptance suite — run against a USER-launched game with bridge ready.

  python tests/v3_tool_matrix.py --ipc-dir "D:\\...\\UnrealEngineMCP_IPC"

Does NOT launch or kill the game. Exits nonzero if any tool hard-fails or
heartbeat dies mid-run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "translator"))

from bridge_client import BridgeClient, BridgeError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--ipc-dir",
        default=r"D:\SteamLibrary\steamapps\common\SpongeBob SquarePants Battle for Bikini Bottom - Rehydrated\Pineapple\Binaries\Win64\UnrealEngineMCP_IPC",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    c = BridgeClient(Path(args.ipc_dir), timeout_seconds=args.timeout)
    if not c.is_alive(max_age_seconds=8):
        print("FAIL: bridge heartbeat stale — user must launch game and wait for ready")
        return 2

    hb0 = c.read_heartbeat()
    print("heartbeat0", json.dumps({k: hb0.get(k) for k in ("protocol", "ready", "pump", "busy")}, default=str))

    results: list[tuple[str, bool, str]] = []

    def step(name: str, fn) -> None:
        if not c.is_alive(10):
            results.append((name, False, "heartbeat dead before call"))
            return
        try:
            out = fn()
            ok = bool(out.get("ok", True)) if isinstance(out, dict) else True
            results.append((name, ok, json.dumps(out, default=str)[:200]))
            print(("OK " if ok else "ERR"), name)
        except Exception as e:
            results.append((name, False, str(e)))
            print("EXC", name, e)

    step("ping", lambda: c.call("ping"))
    step("status", lambda: c.call("status"))
    step("get_player", lambda: c.call("get_player"))
    step(
        "find_objects_PC",
        lambda: c.call("find_objects", {"class": "PlayerController", "limit": 5}),
    )
    step(
        "search_objects",
        lambda: c.call("search_objects", {"query": "Sponge", "class": "Pawn", "limit": 5}),
    )
    step("list_actors", lambda: c.call("list_actors", {"limit": 10, "name_contains": ""}))

    player = {}
    try:
        player = c.call("get_player")
    except BridgeError:
        pass
    pawn_fn = (player.get("pawn") or {}).get("full_name") if isinstance(player, dict) else None
    if pawn_fn:
        step(
            "get_properties",
            lambda: c.call(
                "get_properties",
                {"full_name": pawn_fn, "limit": 20, "offset": 0, "bp_only": True},
            ),
        )
        step(
            "list_functions",
            lambda: c.call(
                "list_functions",
                {"full_name": pawn_fn, "limit": 15, "bp_only": True, "max_depth": 2},
            ),
        )
        step(
            "has_function_K2_GetActorLocation",
            lambda: c.call(
                "has_function",
                {
                    "full_name": pawn_fn,
                    "function_name": "K2_GetActorLocation",
                    "dry_run": True,
                },
            ),
        )
        step(
            "describe_function",
            lambda: c.call(
                "describe_function",
                {"full_name": pawn_fn, "function_name": "K2_GetActorLocation"},
            ),
        )
    else:
        results.append(("pawn_tools", False, "no pawn full_name (menu?)"))
        print("SKIP pawn tools — not in world?")

    step(
        "sample_uobjects",
        lambda: c.call("sample_uobjects", {"limit": 8}),
    )
    step(
        "execute_console_stat",
        lambda: c.call("execute_console", {"command": "stat unit"}),
    )

    # Heartbeat still fresh?
    time.sleep(1)
    alive = c.is_alive(5)
    results.append(("heartbeat_after", alive, "alive" if alive else "dead"))
    print("heartbeat_after", alive)

    failed = [r for r in results if not r[1]]
    print("---")
    print(f"passed {len(results) - len(failed)}/{len(results)}")
    for name, ok, detail in failed:
        print("FAIL", name, detail[:160])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
