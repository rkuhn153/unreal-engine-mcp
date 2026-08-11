---
name: unreal-engine-mcp
description: >-
  Live Unreal Engine runtime MCP via UE4SS + UnrealEngineMCP (file IPC). Use when
  modding/inspecting a running UE4/UE5 game, UnrealEngineMCP, UE4SS bridge, UObject
  get/set/call, revive pump, multi-game IPC select, gear loadout, or when the user
  mentions Unreal mod helper / unreal-engine-mcp tools. Not for static AOB work
  alone — that is unreal-siglab / siglab.
---

# Unreal Engine MCP (Live Runtime Bridge) — 3.3.0-beta.1

MCP server name: **`unreal-engine-mcp`**.

| Doc | Path |
|-----|------|
| Full steering | `./MASTER_PROMPT.md` |
| Short rules | `…\UnrealEngineMCP\AGENTS.md` |
| Static sigs | MCP **`unreal-siglab`** / repo `siglab/` |

Protocol **`3.3.0-beta.1`**, pump **`recoverable`**. Reflection only — UE4SS owns offsets.

## Always first

1. Wrong game? `list_unreal_games` → `select_unreal_game` (`index` / `name` / `ipc_dir`)
2. `ping_unreal_bridge` or `get_bridge_status`
3. Heartbeat fresh + (after boot) `ready` if present
4. Only then discover / mutate

**User launches the game.** Do not kill/relaunch unless they ask.

### Stale pump (game still open)

1. **`revive_unreal_bridge`** (writes `revive.flag`, clears stale IPC)
2. FatalGuard **3.1.0-beta.1+** may auto-kick synthetic Ctrl+F9
3. Manual **Ctrl+F9** / **Ctrl+F10** / **Ctrl+F8**
4. If `process_alive` stale → process gone; user relaunches

Do not spam bulk tools on a dead pump.

## Mental model

- **UE4SS** owns signature scan / engine roots.
- **Single-tick pump only** (no dual delay — UE4SS #1180). Never Actor ReceiveTick/Tick.
- Transport: `Win64/UnrealEngineMCP_IPC/` (`heartbeat`, `request.*`, `response.*`, `revive.flag`, `process_alive.json`).
- **Gentle boot** ~45s / `ready: true`.
- Object identity is **session-scoped** — re-discover after map load / revive.
- Hard titles: `tools/game-profiles/<Game>/`, `no_hooks.txt` (GS3), siglab for AOBs.

## Property mindset

```text
get_properties / list_functions  →  NAMES only (page offset; bp_only=false for Engine)
get_property                     →  ONE value (TArray → {__type:Array, items:[…]})
set_property                     →  ONE write (allow_engine=true for engine fields), then verify
get_gear_loadout                 →  gear merge (deep=false default; saves/soft-ref risk if deep)
describe_function / has_function / dry_run  →  before call_function with args
```

- Prefer known gameplay fields; HUD may be display-only (BFBB shinies → map / `execute_console_command`).
- Invalid UObject: never GetFullName when `IsValid()==false`.
- `call_function`: **0-arg is not safe**. Default 0-arg only unless `allow_args=true`. Arity refuse unless `force=true`. Refuses Debug/Server/Multicast/Client unless `force=true`. Never `*Debug*` for “reads”.
- `sample_uobjects` budgeted — prefer `find_objects` when class is known; `poll_job` for job ids.

## Workflow

```text
list/select game → ping → (revive if stale) → get_player
  → find/search/list_actors → get_properties (names)
  → get_property / get_gear_loadout / get_map_entry
  → set_property | describe_function → call_function
  → verify
```

## Tool map (quick)

| Goal | Tool |
|------|------|
| Alive / status | `ping_unreal_bridge`, `get_bridge_status`, `get_ipc_path` |
| Multi-game | `list_unreal_games`, `select_unreal_game` |
| Revive | `revive_unreal_bridge` |
| Player | `get_player` |
| Discover | `find_objects`, `search_objects` (default Pawn), `list_actors`, `get_object` |
| Names | `get_properties`, `list_functions` |
| R/W | `get_property`, `set_property`, `get_map_entry`, `set_map_entry` |
| Gear | `get_gear_loadout` |
| Call | `describe_function`, `has_function`, `call_function` |
| Heavy | `sample_uobjects` → `poll_job` |
| Console | `execute_console_command` |

## Failure cheatsheet

| Issue | Action |
|-------|--------|
| No heartbeat | UE4SS + mod in mods.txt? `UE4SS.log` |
| Stale HB, process alive | `revive_unreal_bridge`; Ctrl+F9/F10/F8; FatalGuard 3.1.0-beta.1+? |
| process_alive stale | User relaunch |
| Timeout / busy | Stop bulk; revive; smaller pages |
| empty find | Wrong class / still booting / re-discover after map |
| set_property fail | Type / RO / `allow_engine=true` / use UFunction |
| game_state_suspect | Suggest save reload |
| GS3 | Profile + `no_hooks.txt`; no FatalGuard for smoke |

## Response style

Concrete class names, full_names, values. Small experiments. One diagnostic step when blocked. Never claim native assembly patches for reflected sets.

