#!/usr/bin/env python3
"""Unreal Engine MCP server (v1) — FastMCP translator for the UE4SS Lua bridge."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from bridge_client import BridgeClient, BridgeError
from game_discover import discover_unreal_games, pick_default_ipc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("unreal-engine-mcp")

parser = argparse.ArgumentParser(description="UnrealEngineMCP translator")
parser.add_argument(
    "--ipc-dir",
    default=os.environ.get("UNREAL_MCP_IPC_DIR", ""),
    help="Path to UnrealEngineMCP_IPC (optional; auto-detects live games if empty).",
)
parser.add_argument(
    "--game-dir",
    default=os.environ.get("UNREAL_MCP_GAME_DIR", ""),
    help="Optional game root; used to auto-find Win64/UnrealEngineMCP_IPC.",
)
parser.add_argument(
    "--timeout",
    type=float,
    default=float(os.environ.get("UNREAL_MCP_TIMEOUT", "25")),
    help="Bridge call timeout seconds (v3 default 25 for budgeted heavy tools).",
)
parser.add_argument(
    "--transport",
    choices=("stdio", "sse"),
    default=None,
    help="MCP transport (stdio required when launched by Cursor/Grok).",
)
args, unknown = parser.parse_known_args()
sys.argv = [sys.argv[0]] + unknown
TRANSPORT = args.transport or "stdio"


IPC_DIR = pick_default_ipc(args.ipc_dir, args.game_dir)
bridge = BridgeClient(ipc_dir=IPC_DIR, timeout_seconds=args.timeout)
# Cache last discovery for select-by-index
_last_discovery: list[dict[str, Any]] = []
mcp = FastMCP("unreal-engine-mcp")


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _run(cmd: str, params: dict[str, Any] | None = None) -> str:
    try:
        result = bridge.call(cmd, params)
        return _fmt(result)
    except BridgeError as exc:
        logger.error("%s", exc)
        return f"❌ Error: {exc}"


def _object_params(
    full_name: str = "",
    address: str = "",
    class_name: str = "",
    **extra: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = dict(extra)
    if full_name.strip():
        params["full_name"] = full_name.strip()
    if address.strip():
        params["address"] = address.strip()
        # Address-only lookups: enable scan (bridge also auto-scans with a cap).
        params.setdefault("allow_address_scan", True)
    if class_name.strip():
        params["class"] = class_name.strip()
    return params


@mcp.tool()
async def ping_unreal_bridge() -> str:
    """Checks bridge health (file IPC heartbeat + ping command)."""
    try:
        hb = bridge.read_heartbeat()
        pong = bridge.call("ping")
        return _fmt({"heartbeat": hb, "ping": pong, "ipc_dir": str(bridge.ipc_dir)})
    except BridgeError as exc:
        return f"❌ Error: {exc}"


@mcp.tool()
async def get_bridge_status() -> str:
    """Reports UE4SS/mod status, Unreal version, IPC path, and supported commands.

    Offsets/engine roots are owned by UE4SS signature scan — not this MCP.
    """
    return _run("status")


@mcp.tool()
async def get_runtime_capabilities() -> str:
    """Same as get_bridge_status (alias for Unity-MCP parity)."""
    return _run("capabilities")


@mcp.tool()
async def list_actors(name_contains: str = "", limit: int = 50) -> str:
    """Lists AActor instances currently loaded (bounded). Uses reflection, not offsets."""
    if not 1 <= limit <= 500:
        return "❌ Error: limit must be 1-500."
    return _run(
        "list_actors",
        {"name_contains": name_contains, "limit": limit},
    )


@mcp.tool()
async def find_objects(
    class_name: str = "",
    name_contains: str = "",
    limit: int = 50,
) -> str:
    """Finds loaded UObject instances of a short class name (e.g. PlayerController, Pawn, Character)."""
    if not class_name.strip():
        return "❌ Error: class_name is required (short name, e.g. 'PlayerController')."
    if not 1 <= limit <= 500:
        return "❌ Error: limit must be 1-500."
    return _run(
        "find_objects",
        {
            "class": class_name.strip(),
            "name_contains": name_contains,
            "limit": limit,
        },
    )


@mcp.tool()
async def search_objects(
    query: str = "",
    class_name: str = "Pawn",
    limit: int = 30,
) -> str:
    """Searches objects of a class whose full name contains query (case-insensitive).

    Default class is Pawn (safer than Actor). Prefer a specific Blueprint class
    when possible. Actor-wide searches are hard-capped on the bridge.
    """
    if not query.strip():
        return "❌ Error: query is required."
    if not 1 <= limit <= 80:
        return "❌ Error: limit must be 1-80."
    return _run(
        "search_objects",
        {"query": query.strip(), "class": class_name.strip() or "Pawn", "limit": limit},
    )


@mcp.tool()
async def get_object(
    full_name: str = "",
    address: str = "",
    class_name: str = "",
) -> str:
    """Resolves one object by full_name, address (0x...), or first instance of class_name."""
    params = _object_params(full_name, address, class_name)
    if not params:
        return "❌ Error: provide full_name, address, or class_name."
    return _run("get_object", params)


@mcp.tool()
async def get_properties(
    full_name: str = "",
    address: str = "",
    class_name: str = "",
    limit: int = 60,
    offset: int = 0,
    max_depth: int = 3,
    bp_only: bool = True,
) -> str:
    """Lists reflected property NAMES on an object (time-budgeted, pageable).

    Does NOT read every value. Use get_property for specific values.
    Page with offset if truncated=true.
    bp_only=True (default): Blueprint/game fields first.
    bp_only=False: include Engine Character/Pawn/Actor layers (page carefully).
    """
    if not 1 <= limit <= 120:
        return "❌ Error: limit must be 1-120 (safer cap)."
    if offset < 0:
        return "❌ Error: offset must be nonnegative."
    if not 1 <= max_depth <= 8:
        return "❌ Error: max_depth must be 1-8."
    params = _object_params(
        full_name,
        address,
        class_name,
        limit=limit,
        offset=offset,
        max_depth=max_depth,
        bp_only=bp_only,
        include_values=False,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("get_properties", params)


@mcp.tool()
async def get_property(
    property_name: str = "",
    full_name: str = "",
    address: str = "",
    class_name: str = "",
    allow_engine: bool = True,
) -> str:
    """Reads one reflected property (BP then bounded Engine supers by default).

    Prefer names from get_properties. allow_engine=False limits scan to game/BP.
    TArray properties serialize as {__type: Array, count, items: [...]} with UObject
    names (not fake Vectors). Prefer this over call_function for gear/inventory.
    """
    if not property_name.strip():
        return "❌ Error: property_name is required."
    params = _object_params(
        full_name,
        address,
        class_name,
        property=property_name.strip(),
        allow_engine=allow_engine,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("get_property", params)


@mcp.tool()
async def get_gear_loadout(
    full_name: str = "",
    address: str = "",
    class_name: str = "",
    deep: bool = False,
) -> str:
    """Read goat gear loadout (property reads only — no call_function).

    EquippedGear alone is incomplete after wardrobe changes. Merges
    EquippedGear + GearSlots.

    Default is **light** mode (index walk, no ForEach/:get on soft arrays) because
    heavy reflection can leave soft refs in a state that does not save on exit.
    Set deep=true only if you need aggressive struct field probing (may affect save).

    If no object is specified, uses the local player's GoatGearManager.
    """
    params = _object_params(full_name, address, class_name, deep=deep)
    return _run("get_gear_loadout", params)


@mcp.tool()
async def set_property(
    property_name: str = "",
    value_json: str = "",
    full_name: str = "",
    address: str = "",
    class_name: str = "",
    allow_engine: bool = False,
) -> str:
    """Sets one reflected property. value_json is JSON (number, bool, string, or {X,Y,Z}).

    Engine-layer writes require allow_engine=true (or force via bridge). Prefer
    names discovered via get_properties first.
    """
    if not property_name.strip():
        return "❌ Error: property_name is required."
    if value_json == "":
        return "❌ Error: value_json is required (JSON value)."
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        return f"❌ Error: value_json is invalid JSON: {exc}"
    params = _object_params(
        full_name,
        address,
        class_name,
        property=property_name.strip(),
        value=value,
        allow_engine=allow_engine,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("set_property", params)


@mcp.tool()
async def list_functions(
    full_name: str = "",
    address: str = "",
    class_name: str = "",
    limit: int = 30,
    offset: int = 0,
    max_depth: int = 2,
    bp_only: bool = True,
) -> str:
    """Lists reflected UFunctions (time-budgeted, pageable).

    bp_only=True: Blueprint/game first. bp_only=False: include Engine layers;
    page with offset when truncated.
    """
    if not 1 <= limit <= 80:
        return "❌ Error: limit must be 1-80."
    if offset < 0:
        return "❌ Error: offset must be nonnegative."
    if not 1 <= max_depth <= 8:
        return "❌ Error: max_depth must be 1-8."
    params = _object_params(
        full_name,
        address,
        class_name,
        limit=limit,
        offset=offset,
        max_depth=max_depth,
        bp_only=bp_only,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("list_functions", params)


@mcp.tool()
async def call_function(
    function_name: str = "",
    args_json: str = "[]",
    full_name: str = "",
    address: str = "",
    class_name: str = "",
    dry_run: bool = False,
    allow_args: bool = False,
    force: bool = False,
) -> str:
    """Calls a UFunction with safety prechecks (unlike raw Unity, misses can FATAL).

    Safety (default):
    - Resolves the UFunction on the class first; **missing names refuse without ProcessEvent**
    - Refuses **Debug** / **Server** / **Multicast** / **Client** RPCs, ExecuteUbergraph,
      delegates, lifecycle (unless force=true). **0-arg is not safe** — Debug dumps can FATAL.
    - **0-argument calls only** unless allow_args=true (wrong arity is the main native crash)
    Prefer get_property / array reads over call_function for inventory/gear.

    dry_run=true: only check that the function exists (no call).
    pcall cannot catch native Unreal asserts — prechecks are the real protection.
    """
    if not function_name.strip():
        return "❌ Error: function_name is required."
    try:
        args = json.loads(args_json) if args_json.strip() else []
    except json.JSONDecodeError as exc:
        return f"❌ Error: args_json is invalid JSON: {exc}"
    if not isinstance(args, list):
        return "❌ Error: args_json must be a JSON array."
    fn_l = function_name.strip().lower()
    if not force and not dry_run:
        if "debug" in fn_l:
            return (
                "❌ Error: refused *Debug* call_function (native dump/assert risk). "
                "Prefer get_property on arrays (EquippedGear etc.). force=true only if you accept crash risk."
            )
        if fn_l.startswith("server") or fn_l.startswith("multicast") or (
            fn_l.startswith("client") and fn_l != "client"
        ):
            return (
                "❌ Error: refused Server/Multicast/Client RPC. "
                "Use force=true only if you accept crash risk."
            )
    if args and not allow_args and not force and not dry_run:
        return (
            "❌ Error: refused args for safety. Unreal wrong-arity ProcessEvent can FATAL "
            "(Unity would just throw). Re-call with allow_args=true only if signature is known, "
            "or use dry_run=true / empty args_json=[] first."
        )
    params = _object_params(
        full_name,
        address,
        class_name,
        function_name=function_name.strip(),
        args=args,
        dry_run=dry_run,
        allow_args=allow_args,
        force=force,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("call_function", params)


@mcp.tool()
async def has_function(
    function_name: str = "",
    full_name: str = "",
    address: str = "",
    class_name: str = "",
) -> str:
    """Check whether a UFunction exists on an object (no ProcessEvent / no side effects)."""
    if not function_name.strip():
        return "❌ Error: function_name is required."
    params = _object_params(
        full_name,
        address,
        class_name,
        function_name=function_name.strip(),
        dry_run=True,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("has_function", params)


@mcp.tool()
async def get_player() -> str:
    """Returns player controller, pawn, camera manager, and pawn world location when available."""
    return _run("get_player")


@mcp.tool()
async def execute_console_command(command: str = "") -> str:
    """Runs a player console command via APlayerController::ConsoleCommand.

    Prefer this for known game cheats that write real state (e.g. BFBB
    ``PINE_SetShinyAmount 999999``), rather than HUD-only properties.
    Still can crash bad commands — use known-safe names.
    """
    if not command or not str(command).strip():
        return "❌ Error: command is required (e.g. PINE_SetShinyAmount 999999)"
    return _run("execute_console", {"command": str(command).strip()})


@mcp.tool()
async def sample_uobjects(
    limit: int = 10,
    name_contains: str = "",
    class_contains: str = "",
    allow_dangerous: bool = True,
) -> str:
    """Samples GUObjectArray (v3: time-budgeted / jobbed; prefer small limits).

    Prefer find_objects/search_objects when you know the class. allow_dangerous
    kept for compatibility (default true in v3 under budgets).
    """
    if not 1 <= limit <= 100:
        return "❌ Error: limit must be 1-100."
    return _run(
        "sample_uobjects",
        {
            "limit": limit,
            "name_contains": name_contains,
            "class_contains": class_contains,
            "allow_dangerous": allow_dangerous,
        },
    )


@mcp.tool()
async def describe_function(
    function_name: str = "",
    full_name: str = "",
    address: str = "",
    class_name: str = "",
) -> str:
    """Describes a UFunction signature via reflection (no ProcessEvent)."""
    if not function_name.strip():
        return "❌ Error: function_name is required."
    params = _object_params(
        full_name, address, class_name, function_name=function_name.strip()
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("describe_function", params)


@mcp.tool()
async def get_map_entry(
    map_property: str = "",
    key: str = "",
    full_name: str = "",
    address: str = "",
    class_name: str = "",
) -> str:
    """Reads one key from a reflected TMap/table property (e.g. VariableData)."""
    if not map_property.strip() or key == "":
        return "❌ Error: map_property and key are required."
    params = _object_params(
        full_name,
        address,
        class_name,
        map=map_property.strip(),
        key=key,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("get_map_entry", params)


@mcp.tool()
async def set_map_entry(
    map_property: str = "",
    key: str = "",
    value_json: str = "",
    full_name: str = "",
    address: str = "",
    class_name: str = "",
) -> str:
    """Writes one key on a reflected TMap/table when UE4SS exposes it as a table."""
    if not map_property.strip() or key == "" or value_json == "":
        return "❌ Error: map_property, key, and value_json are required."
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError as exc:
        return f"❌ Error: value_json invalid: {exc}"
    params = _object_params(
        full_name,
        address,
        class_name,
        map=map_property.strip(),
        key=key,
        value=value,
    )
    if not any(k in params for k in ("full_name", "address", "class")):
        return "❌ Error: provide full_name, address, or class_name."
    return _run("set_map_entry", params)


@mcp.tool()
async def poll_job(job_id: str = "") -> str:
    """Poll a multi-tick bridge job (sample_uobjects job_id, etc.)."""
    if not job_id.strip():
        return "❌ Error: job_id is required."
    return _run("poll_job", {"job_id": job_id.strip()})


@mcp.tool()
async def revive_unreal_bridge() -> str:
    """Try to revive a dead UnrealEngineMCP pump after a soft Fatal (game still open).

    Mid-game, no map change required. Clears stale IPC and writes revive.flag.

    With FatalGuard 3.1.0-beta.1+ auto-kick: native watches revive.flag / stale
    heartbeat and synthesizes Ctrl+F9 so Lua force_revive runs without user input.
    Lua then does an immediate pump_once even if ExecuteWithDelay died.

    Waits up to ~3s for heartbeat to refresh. Manual Ctrl+F9/F10/F8 is only a
    fallback if FatalGuard is missing or autokick is disabled.
    """
    ipc = bridge.ipc_dir
    actions: list[str] = []

    # Clear stale IPC so a hung request does not re-fire.
    for name in ("request.flag", "request.json", "response.flag", "response.json"):
        p = ipc / name
        try:
            if p.is_file():
                p.unlink()
                actions.append(f"cleared {name}")
        except OSError as exc:
            actions.append(f"could not clear {name}: {exc}")

    # Signal Lua (+ FatalGuard auto-kick). Keep flag until pump consumes it.
    try:
        ipc.mkdir(parents=True, exist_ok=True)
        (ipc / "revive.flag").write_text(
            f"revive requested at {time.time()}\n",
            encoding="utf-8",
        )
        actions.append("wrote revive.flag")
    except OSError as exc:
        actions.append(f"could not write revive.flag: {exc}")

    # Wait for native kick + Lua force_revive + immediate pump (up to ~3s)
    alive = False
    for _ in range(6):
        time.sleep(0.5)
        alive = bridge.is_alive(max_age_seconds=3.0)
        if alive:
            actions.append("heartbeat recovered")
            break

    hb = None
    if bridge.heartbeat_path.is_file():
        try:
            age = time.time() - bridge.heartbeat_path.stat().st_mtime
            hb = {
                "age_sec": age,
                "data": json.loads(bridge.heartbeat_path.read_text(encoding="utf-8")),
            }
        except (OSError, json.JSONDecodeError):
            hb = None

    process_alive = None
    pa = ipc / "process_alive.json"
    if pa.is_file():
        try:
            process_alive = {
                "age_sec": time.time() - pa.stat().st_mtime,
                "data": json.loads(pa.read_text(encoding="utf-8")),
            }
        except (OSError, json.JSONDecodeError):
            process_alive = None

    user_action = None
    if not alive:
        pa_fresh = bool(process_alive and process_alive.get("age_sec", 99) < 3.0)
        kicks = None
        if process_alive and isinstance(process_alive.get("data"), dict):
            kicks = process_alive["data"].get("kicks")
        if pa_fresh:
            user_action = (
                "process_alive is fresh but heartbeat is still stale. "
                "Need FatalGuard 3.1.0-beta.1+ (autokick) and UnrealEngineMCP 3.3.0-beta.1+ "
                "(immediate pump). Restart the game once to load those builds. "
                "Fallback: focus the game and press Ctrl+F9 once."
            )
            if kicks is not None:
                user_action += f" process_alive.kicks={kicks}."
        else:
            user_action = (
                "Game process may be gone (process_alive stale). "
                "Relaunch the game, then ping again."
            )

    return _fmt(
        {
            "ok": alive,
            "ipc_dir": str(ipc),
            "alive": alive,
            "actions": actions,
            "heartbeat": hb,
            "process_alive": process_alive,
            "user_action": user_action,
            "keys": ["Ctrl+F9", "Ctrl+F10", "Ctrl+F8"],
            "note": (
                "Mid-game path: revive.flag → FatalGuard auto-kick → Lua force_revive "
                "+ immediate pump. No map change / no user key when FatalGuard "
                "3.1.0-beta.1+ is loaded."
            ),
        }
    )


@mcp.tool()
async def get_ipc_path() -> str:
    """Returns the IPC directory this translator is currently targeting."""
    alive = bridge.is_alive()
    hb = None
    process_alive = None
    if bridge.heartbeat_path.is_file():
        try:
            hb = json.loads(bridge.heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            hb = None
    pa = bridge.ipc_dir / "process_alive.json"
    if pa.is_file():
        try:
            process_alive = json.loads(pa.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            process_alive = None
    return _fmt(
        {
            "ipc_dir": str(bridge.ipc_dir),
            "alive": alive,
            "heartbeat": hb,
            "process_alive": process_alive,
            "protocol_hint": "3.3.0-beta.1",
            "auto_detect": not bool(args.ipc_dir.strip() or os.environ.get("UNREAL_MCP_IPC_DIR", "").strip()),
        }
    )


@mcp.tool()
async def list_unreal_games(alive_only: bool = False) -> str:
    """Discover Unreal games with UnrealEngineMCP installed and/or a live heartbeat.

    Scans running processes (Shipping/Win64 + mod) and common Steam/Epic roots
    for UnrealEngineMCP_IPC/heartbeat.json. Use select_unreal_game to switch.
    """
    global _last_discovery
    try:
        games = discover_unreal_games()
    except Exception as exc:  # noqa: BLE001 — surface discovery errors to agent
        return _fmt({"ok": False, "error": f"discovery failed: {exc}"})

    rows = [g.to_dict() for g in games]
    if alive_only:
        rows = [r for r in rows if r.get("alive")]
    # Attach stable indices for select_unreal_game
    for i, row in enumerate(rows):
        row["index"] = i
    _last_discovery = rows

    try:
        current = str(bridge.ipc_dir.resolve())
    except OSError:
        current = str(bridge.ipc_dir)
    for row in rows:
        try:
            row["selected"] = str(Path(row["ipc_dir"]).resolve()) == current
        except OSError:
            row["selected"] = row["ipc_dir"] == current

    return _fmt(
        {
            "ok": True,
            "count": len(rows),
            "current_ipc_dir": str(bridge.ipc_dir),
            "current_alive": bridge.is_alive(),
            "games": rows,
            "hint": "Call select_unreal_game(index=N) or select_unreal_game(name='Pineapple') to switch.",
        }
    )


@mcp.tool()
async def select_unreal_game(
    index: int = -1,
    name: str = "",
    ipc_dir: str = "",
    prefer_alive: bool = True,
) -> str:
    """Switch the translator to a discovered (or explicit) UnrealEngineMCP IPC target.

    Prefer list_unreal_games first, then pass index= or name= (substring match on
    game_name / process_name / exe path). ipc_dir= sets an absolute path directly.
    """
    global _last_discovery, IPC_DIR

    target_ipc: Path | None = None
    matched: dict[str, Any] | None = None

    if ipc_dir.strip():
        target_ipc = Path(ipc_dir).expanduser().resolve()
        matched = {"game_name": target_ipc.parent.name, "ipc_dir": str(target_ipc), "source": "explicit"}
    else:
        games = _last_discovery
        if not games:
            games = [g.to_dict() for g in discover_unreal_games()]
            for i, row in enumerate(games):
                row["index"] = i
            _last_discovery = games

        if prefer_alive:
            pool = [g for g in games if g.get("alive")] or games
        else:
            pool = games

        if index >= 0:
            # index refers to last list_unreal_games order (full list, not alive-only filter)
            if index >= len(games):
                return _fmt({"ok": False, "error": f"index {index} out of range (count={len(games)})"})
            matched = games[index]
            target_ipc = Path(matched["ipc_dir"])
        elif name.strip():
            q = name.strip().lower()
            hits = []
            for g in pool:
                blob = " ".join(
                    str(x or "")
                    for x in (
                        g.get("game_name"),
                        g.get("process_name"),
                        g.get("exe_path"),
                        g.get("ipc_dir"),
                    )
                ).lower()
                if q in blob:
                    hits.append(g)
            if not hits:
                return _fmt(
                    {
                        "ok": False,
                        "error": f"no game matched name={name!r}",
                        "available": [
                            {"index": g.get("index"), "game_name": g.get("game_name"), "alive": g.get("alive")}
                            for g in games
                        ],
                    }
                )
            # Prefer alive among hits
            hits.sort(key=lambda g: (not g.get("alive"), g.get("game_name") or ""))
            matched = hits[0]
            target_ipc = Path(matched["ipc_dir"])
        else:
            return _fmt(
                {
                    "ok": False,
                    "error": "provide index=, name=, or ipc_dir=",
                    "hint": "list_unreal_games() then select_unreal_game(index=0)",
                }
            )

    assert target_ipc is not None
    new_dir = bridge.retarget(target_ipc, ensure_dir=True)
    IPC_DIR = new_dir
    alive = bridge.is_alive()
    hb = None
    if bridge.heartbeat_path.is_file():
        try:
            hb = json.loads(bridge.heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            hb = None

    logger.info("Selected Unreal IPC: %s alive=%s", new_dir, alive)
    return _fmt(
        {
            "ok": True,
            "selected": matched,
            "ipc_dir": str(new_dir),
            "alive": alive,
            "heartbeat": hb,
            "note": None
            if alive
            else "IPC selected but heartbeat is not live — is the game running with UnrealEngineMCP enabled?",
        }
    )


def main() -> None:
    logger.info("UnrealEngineMCP translator starting; IPC=%s", bridge.ipc_dir)
    if TRANSPORT == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
