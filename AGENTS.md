# UnrealEngineMCP — agent rules (3.3.0-beta.1)

When working in this repo or driving the live Unreal bridge, follow:

**`MASTER_PROMPT.md`** (canonical long-form steering for any AI)

**Versioning:** [Semantic Versioning 2.0.0](https://semver.org/). Current package/protocol: **`3.3.0-beta.1`** (beta = usable on known titles, not a stability/API lock). Optional FatalGuard: **`3.1.0-beta.1`**.

Stack layers: **UE4SS** (inject + sig scan) → **Lua mod** `bridge/UnrealEngineMCP` (reflection + recoverable pump) → **FatalGuard** (optional native) → **FastMCP** `translator/unreal_mcp_server.py` (file IPC).  
Protocol reports as **`3.3.0-beta.1`**; pump reports **`recoverable`**. MCP server id: **`unreal-engine-mcp`**.

---

## Mindset

1. **User launches the game** — agents do **not** kill/restart unless the user explicitly asks.
2. **Ping first** — no mutations on a dead/stale bridge. Check `process_alive.json` (FatalGuard) if heartbeat stalls.
3. **Multi-game:** if IPC might be wrong, `list_unreal_games` → `select_unreal_game` (by `index`, `name`, or `ipc_dir`) before work.
4. **Reflection only** — no gameplay offset tables; UE4SS owns bootstrap. Hard titles need `tools/game-profiles/<Game>/` + **`siglab/`** / `unreal-siglab` MCP (static PE/AOB/log).
5. **Discover → catalog names → read specific values → small write → verify.**
6. **`get_properties` / `list_functions` = names** (page `offset`; default `bp_only=true`; set `bp_only=false` for Engine layers). **`get_property` = values** (BP then bounded engine). **`set_property`** BP-first; engine writes need `allow_engine=true`.
7. **One `set_property` at a time**, then read back (unless user wants batch and pump is healthy).
8. **Gentle boot** — wait ~45s / `ready: true` after launch. Heavy cmds before ready are rejected or risky.
9. **Recoverable pump** — single `ExecuteWithDelay` chain only (dual chains = UE4SS crash [#1180](https://github.com/UE4SS-RE/RE-UE4SS/issues/1180)). Never Actor `ReceiveTick` / `Tick`. Busy watchdog clears stuck cmds.
10. **Revive mid-game** (preferred order):
    1. `revive_unreal_bridge` (writes `revive.flag`, clears stale request/response)
    2. **FatalGuard 3.1.0-beta.1+** auto-kicks synthetic Ctrl+F9 on `revive.flag` / stale heartbeat
    3. Manual **Ctrl+F9** / **Ctrl+F10** / **Ctrl+F8** if native autokick missing
    4. Full restart only if process is gone (`process_alive` stale)
11. **No anti-cheat bypass / online-unfair guidance.**
12. **Heavy tools allowed but time-budgeted** — `sample_uobjects` modest `limit` (≤20–50); expect `truncated` / `job_id` → `poll_job`.
13. **Object identity is session-scoped** — re-discover after map load / revive.
14. **Honesty** — report protocol, `pump`, `ready`, `game_state_suspect`. Swallowed fatals may leave corrupt state → suggest save reload.

---

## Property & call safety

| Rule | Detail |
|------|--------|
| Names vs values | Catalog with `get_properties`; never treat it as a bulk value dump |
| TArray | `{__type:"Array", count, items:[…]}` with UObject names — not fake Vectors |
| Gear / inventory | Prefer **`get_gear_loadout`** (merges EquippedGear + GearSlots). Default **light** (`deep=false`) — heavy probes can dirty soft refs / saves. Never trust EquippedGear alone after wardrobe changes |
| HUD | May be display-only (BFBB `CntShiny`) → maps / `execute_console_command` for real state |
| Null refs | Unpossessed `Controller` / `PlayerState` → clean `NullObject` (do not force-serialize) |
| Invalid UObjects | **Never** `GetFullName` / path on `IsValid()==false` (native AV) |
| `call_function` | Prefer `describe_function` / `has_function` / `dry_run=true` first. **0-arg is not safe.** Default: 0-arg only unless `allow_args=true`. Arity mismatch **refused** unless `force=true`. Bridge + translator refuse **Debug** / **Server** / **Multicast** / **Client** RPCs unless `force=true`. Never call `*Debug*` for “read” dumps |
| Native asserts | Lua `pcall` does **not** catch Fatals — prechecks + FatalGuard matter |

---

## Tool reminder

| Goal | Tool |
|------|------|
| Alive? | `ping_unreal_bridge` / `get_bridge_status` / `get_runtime_capabilities` |
| IPC + process | `get_ipc_path` (heartbeat + `process_alive`) |
| Wrong game? | `list_unreal_games` → `select_unreal_game` |
| Pump dead (game open)? | `revive_unreal_bridge` (+ Ctrl+F9/F10/F8 fallback) |
| Who am I? | `get_player` |
| Discovery | `find_objects` / `search_objects` (default class **Pawn**) / `list_actors` / `get_object` |
| List fields / funcs | `get_properties` / `list_functions` (names, paged; `bp_only`) |
| Read / write field | `get_property` / `set_property` |
| Gear loadout | `get_gear_loadout` (`deep=true` only if needed) |
| Function sig | `describe_function` / `has_function` |
| Call | `call_function` (`dry_run`, `allow_args`, `force`) |
| Heavy sample | `sample_uobjects` (budgeted) → `poll_job` |
| Map entry | `get_map_entry` / `set_map_entry` |
| Console cheat | `execute_console_command` |

Engine short names to try: `PlayerController`, `Pawn`, `Character`, `PlayerCameraManager`, `GameModeBase`, `GameStateBase`, `PlayerState`, `HUD`, `WorldSettings`. Game BP classes look like `BP_Something_C` — discover them; do not invent them.

---

## Failure playbook

| Symptom | Action |
|---------|--------|
| No heartbeat | Game running? UE4SS + UnrealEngineMCP in `mods.txt`? Check `ue4ss/UE4SS.log` |
| Stale heartbeat, process alive | `revive_unreal_bridge`; else Ctrl+F9/F10/F8; check FatalGuard 3.1.0-beta.1+ loaded |
| `process_alive` stale | Process likely gone — user relaunches |
| Timeout / stuck busy | Stop bulk; wait watchdog; revive; do not spam dumps |
| empty `find_objects` | Wrong short name, still booting, or not constructed — retry after menu/level |
| `set_property` fails | Not reflected, wrong type, read-only, or need `allow_engine=true` / a UFunction |
| Works in menu, not in level | Re-discover after LoadMap; full_names/addresses change |
| `game_state_suspect` | After swallow/revive — reload save if behavior is weird |
| GS3 / hard titles | Use `tools/game-profiles/…`; `no_hooks.txt` skips Lua RegisterHook; avoid stock tick hooks |

---

## FatalGuard (optional native)

**3.1.0-beta.1** (`native/FatalGuard/`): main-exe-only `ExitProcess`/`abort` hooks after ~45s delay; writes `process_alive.json` (`version` **310**, `version_semver` **3.1.0-beta.1**); watches **`revive.flag`** + stale heartbeat and **auto-sends Ctrl+F9** so MCP revive works mid-game without user input.

| Env | Effect |
|-----|--------|
| `FATALGUARD=0` | Disable |
| `FATALGUARD_DELAY_MS` | Boot delay before hooks |
| `FATALGUARD_AUTOKICK=0` | No synthetic Ctrl+F9 |
| `FATALGUARD_MSGBOX=1` | Also swallow fatal MessageBox on main exe |

Build: `native/FatalGuard/Build-FatalGuard.ps1`. **Do not** enable for GS3 smoke tests. State may still be corrupt after a swallow.

`native/SafeReflect/` is a placeholder for future SEH wrappers; crash boundary today is Lua prechecks + FatalGuard.

---

## Install, profiles, static sigs

| Path | Role |
|------|------|
| `scripts/Install-ToGame.ps1 -GameDir "..." [-Profile Name] [-DownloadUE4SS]` | Install UE4SS + mod + IPC |
| `tools/game-profiles/BFBB`, `DodoPeak`, `GoatSimulator3` | Per-game UE4SS settings / signatures / hooks |
| `siglab/` or MCP **`unreal-siglab`** | Static PE/AOB/disasm/UE4SS log recover — not live reflection |
| IPC folder | `…\Win64\UnrealEngineMCP_IPC\` (`heartbeat.json`, `request.*`, `response.*`, `revive.flag`, `process_alive.json`) |

Live acceptance (user-launched game): `python tests/v3_tool_matrix.py --ipc-dir "…\UnrealEngineMCP_IPC"`.  
Offline: `pytest tests/test_bridge_client.py -q`. Smoke: `python tests/smoke_live.py --ipc-dir "…"`.

Default autotest target on this machine: **SpongeBob BFBB Rehydrated** (`Pineapple\Binaries\Win64\UnrealEngineMCP_IPC`). Retarget with `--ipc-dir`, `UNREAL_MCP_IPC_DIR`, or `select_unreal_game`.

---

## Workflow (one-liner)

```text
list/select game → ping → (revive if stale) → get_player
  → find/search → get_properties (names) → get_property
  → set_property | describe_function → call_function
  → verify; budgeted sample/poll_job only when needed
```

**One-liner paste:** User launches game; select correct IPC; ping first; revive mid-game via `revive_unreal_bridge` not relaunch; discover by reflection; names then values; describe before argful calls; budgeted heavy tools OK; one set then verify; no custom offsets; no anti-cheat bypass.
