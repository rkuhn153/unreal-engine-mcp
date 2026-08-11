# unreal-engine-mcp

Live **Unreal Engine (shipped game)** bridge for AI agents and tools via **MCP (Model Context Protocol)**.

Inspect the running game, read and write **UObject** properties, call **UFunctions**, sample objects, run console commands, and revive a stalled IPC pump — without writing a one-off UE4SS script for every tweak.

This is **runtime** modding through **[UE4SS](https://github.com/UE4SS-RE/RE-UE4SS)**. It is **not** Epic’s Unreal **Editor** MCP (levels/assets while developing).

| Piece | Path | Role |
|--------|------|------|
| UE4SS | (download) | Inject + AOB-scan engine roots into the game process |
| Lua bridge | `bridge/UnrealEngineMCP/` | In-game IPC pump + reflection (`main.lua`) |
| MCP server | `translator/unreal_mcp_server.py` | FastMCP tools agents call |
| FatalGuard | `native/FatalGuard/` | Optional native helper (revive / exit swallow) |
| Siglab | `siglab/` | Optional static AOB tooling (separate MCP) |

**Protocol / package version:** **`3.3.0-beta.1`** ([SemVer](https://semver.org/) — see `VERSION`). **Beta:** usable on known titles; not a stability or API freeze.

**No hardcoded gameplay offsets.** UE4SS owns bootstrap. This stack is **reflection** over UObjects.

## What you can do

| Area | Capabilities |
|------|----------------|
| **Discover** | List actors, find/search by class or name, resolve objects, sample UObjects |
| **Read / write** | Property names + values; gear-oriented loadout helper; set one property at a time |
| **Call** | List / describe / dry-run-ish checks, then `call_function` with arity guards |
| **Player** | Controller, pawn, location via `get_player` |
| **Console / map** | `execute_console_command`; map entry get/set helpers |
| **Multi-game** | Discover live IPC folders; select which game the MCP talks to |
| **Resilience** | Heartbeat status, `revive_unreal_bridge`, optional FatalGuard |

**Not supported:** replacing a full C++ mod SDK, bypassing anti-cheat, or guaranteeing survival after bad native/reflection access. Prefer the game’s own functions when possible.

**Always start with** `ping_unreal_bridge` or `get_bridge_status` / `get_runtime_capabilities` so you know the pump is alive before bulk discovery or writes.

## Honesty (runtime Unreal is harder than Unity)

Compared to [bepinex-mcp](https://github.com/rkuhn153/bepinex-mcp):

| | Unity + BepInEx | This (UE4SS) |
|--|-----------------|--------------|
| Stability | Generally good | **Game-dependent**; hangs/crashes happen |
| Injector | BepInEx | UE4SS (signatures may need care per title) |
| Bridge death | Restart plugin / game | Revive best-effort; sometimes full relaunch |
| Agent rule | — | **You** launch the game; agents should not auto-kill/relaunch |

- Prefer **read** tools first; treat **set_property** / **call_function** as unsafe.
- Object identities are **session-scoped** — rediscover after map load or revive.
- Single-tick IPC pump only (avoids UE4SS dual-delay crash [#1180](https://github.com/UE4SS-RE/RE-UE4SS/issues/1180)).

## MCP tools

Names as exposed by `translator/unreal_mcp_server.py`:

### Connection & multi-game
- `ping_unreal_bridge` — heartbeat + ping
- `get_bridge_status` / `get_runtime_capabilities` — UE4SS / engine / feature surface
- `get_ipc_path` — which IPC directory is active
- `list_unreal_games` — find games with live or known IPC folders
- `select_unreal_game` — switch target (`index` / `name` / `ipc_dir`)
- `revive_unreal_bridge` — write revive flag / clear stale IPC (then Ctrl+F9 if needed)
- `poll_job` — poll long-running bridge jobs when used

### Objects & actors
- `list_actors` — bound list of actors (optional name filter)
- `find_objects` — `FindAllOf`-style by short class name
- `search_objects` — name filter within a class
- `get_object` — resolve by full name / address / class
- `sample_uobjects` — budgeted `ForEachUObject` sample
- `get_player` — local player controller / pawn / location

### Properties & functions
- `get_properties` — **names** (paged; optional BP-only filter)
- `get_property` / `set_property` — one field (TArray may return structured items)
- `get_gear_loadout` — merged gear-oriented read helper
- `list_functions` / `describe_function` / `has_function`
- `call_function` — invoke a `UFunction` (refuses bad arity when known)

### Console & map
- `execute_console_command`
- `get_map_entry` / `set_map_entry`

### Property mindset (agents)

```text
get_properties / list_functions     → NAMES (page carefully)
get_property                        → ONE value
set_property                        → ONE write, then verify with get_property
describe_function / has_function    → before call_function with args
```

Prefer known gameplay fields. Some HUD/UI values are display-only; progression may need console or map tools depending on the game.

## Requirements

- **Windows x64** (UE4SS + shipping games)
- **Python 3.10+** (3.11/3.13 fine) for the MCP translator
- A **UE4/UE5** game that can load **[UE4SS](https://github.com/UE4SS-RE/RE-UE4SS)**
- PowerShell for install scripts (optional but recommended)

UE4SS is **not** vendored in this repo by default — `Download-UE4SS.ps1` / `-DownloadUE4SS` fetch it.

## Compatibility

| | |
|--|--|
| **Target** | Shipped Unreal games (`*-Win64-Shipping.exe` or similar under `Binaries\Win64`) |
| **Engine** | UE4 / UE5 as supported by your UE4SS build |
| **Not for** | Unreal Editor project automation (use Epic / editor MCP stacks) |
| **Hard titles** | May need UE4SS signature overrides, `no_hooks.txt`, or delayed inject — same class of issues as any UE4SS mod |

Success depends on UE4SS attaching cleanly. Anti-cheat or blocked injectors fail the same way as on other modded titles.

## Quick start

### 1. Install into a game

```powershell
cd unreal-engine-mcp   # this repo

.\scripts\Install-ToGame.ps1 `
  -GameDir "D:\SteamLibrary\steamapps\common\YourUnrealGame" `
  -DownloadUE4SS
```

This will:

1. Locate `Win64` / shipping binary under the game dir  
2. Install UE4SS (unless `-SkipUE4SS`)  
3. Copy `Mods\UnrealEngineMCP` from `bridge/`  
4. Enable the mod in `mods.txt`  
5. Create `UnrealEngineMCP_IPC\` next to the shipping exe  

### 2. Launch the game yourself

Start the game once. In the UE4SS console, look for a line like:

```text
[UnrealEngineMCP] bridge started
```

Do **not** expect the MCP process to start the game for you.

### 3. Run the MCP server

```powershell
cd translator
pip install -r requirements.txt

python unreal_mcp_server.py `
  --ipc-dir "D:\path\to\Game\Binaries\Win64\UnrealEngineMCP_IPC"
```

Or set environment variable:

```text
UNREAL_MCP_IPC_DIR=D:\path\to\Game\Binaries\Win64\UnrealEngineMCP_IPC
```

### 4. Wire an MCP client

**Cursor** (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "unreal-engine-mcp": {
      "command": "C:/Python313/python.exe",
      "args": [
        "C:/path/to/unreal-engine-mcp/translator/unreal_mcp_server.py",
        "--ipc-dir",
        "D:/path/to/Game/Binaries/Win64/UnrealEngineMCP_IPC"
      ]
    }
  }
}
```

**Grok Build** (`~/.grok/config.toml`):

```toml
[mcp_servers.unreal-engine-mcp]
command = 'C:\Python313\python.exe'
args = [
  'C:\path\to\unreal-engine-mcp\translator\unreal_mcp_server.py',
  '--ipc-dir',
  'D:\path\to\Game\Binaries\Win64\UnrealEngineMCP_IPC',
]
enabled = true
```

Restart the client (or reload MCP) after config changes.

### 5. First agent session

```text
ping_unreal_bridge
  → get_bridge_status / get_runtime_capabilities
  → get_player / list_actors / find_objects
  → get_properties → get_property
  → describe_function → call_function   (only when needed)
```

If the heartbeat goes stale while the game is still open:

1. `revive_unreal_bridge`  
2. In-game **Ctrl+F9** (re-arm pump) if needed  
3. Optional FatalGuard may assist; otherwise restart the game  

## IPC layout

Next to the shipping executable:

```text
UnrealEngineMCP_IPC/
  heartbeat.json      # Lua ~1 Hz
  request.json        # Python command
  request.flag
  response.json       # Lua result
  response.flag
  revive.flag         # optional revive request
  process_alive.json  # optional FatalGuard
```

Request shape:

```json
{ "id": "uuid", "cmd": "list_actors", "params": { "limit": 20 } }
```

## AI skills

Agent skill for Cursor / Grok-style clients:

| Skill | Role |
|--------|------|
| [`skills/unreal-engine-mcp`](skills/unreal-engine-mcp/SKILL.md) | Live bridge tools, revive order, property habits |

```powershell
Copy-Item ".\skills\unreal-engine-mcp" "$env:USERPROFILE\.cursor\skills\unreal-engine-mcp" -Recurse -Force
```

Full portable steering: [`MASTER_PROMPT.md`](MASTER_PROMPT.md). Short rules: [`AGENTS.md`](AGENTS.md).

## Related projects (same suite)

| Need | Repo |
|------|------|
| Live **Unreal runtime** (this) | [unreal-engine-mcp](https://github.com/rkuhn153/unreal-engine-mcp) |
| Live **Unity** get/set/patch | [bepinex-mcp](https://github.com/rkuhn153/bepinex-mcp) |
| Mono C# search | [gamecode-rag](https://github.com/rkuhn153/gamecode-rag) |
| IL2CPP static decompile | [il2cpp-decompiler](https://github.com/rkuhn153/il2cpp-decompiler) |
| Flash / Ruffle live bridge | [flash-mod-bridge](https://github.com/rkuhn153/flash-mod-bridge) |

## Tests

```powershell
# Offline IPC client unit tests
pip install pytest
pytest tests/test_bridge_client.py -q

# Live (game running with mod loaded)
python tests/smoke_live.py --ipc-dir "...\Win64\UnrealEngineMCP_IPC"
```

## Layout

```text
unreal-engine-mcp/
  bridge/UnrealEngineMCP/   # UE4SS Lua mod
  translator/               # FastMCP server + BridgeClient
  scripts/                  # Install-ToGame, Download-UE4SS
  native/FatalGuard/        # Optional native helper (source)
  siglab/                   # Optional static AOB / sig tooling
  tests/
  skills/unreal-engine-mcp/
  MASTER_PROMPT.md
  AGENTS.md
  VERSION
  README.md
  LICENSE
```

## License

MIT — see [LICENSE](LICENSE).

**UE4SS** is separate software under its own license — get it from [UE4SS-RE/RE-UE4SS](https://github.com/UE4SS-RE/RE-UE4SS). This repo does not redistribute UE4SS binaries by default.
