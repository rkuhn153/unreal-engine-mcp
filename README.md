# unreal-engine-mcp

**Runtime** Unreal Engine bridge for AI agents via **[UE4SS](https://github.com/UE4SS-RE/RE-UE4SS)** + file IPC + Python MCP.

Inspect and mutate **shipped** UE4/UE5 games (UObject get/set/call) — not the Unreal **Editor** MCP.

| Version | [SemVer](https://semver.org/) in `VERSION` — currently **`3.3.0-beta.1`** |
|---------|--------------------------------------------------------------------------|
| Status | **Experimental beta.** Usable on known titles; games can still crash or hang on bad access. |

## What it is

| Layer | Role |
|--------|------|
| **UE4SS** | Inject + signature-scan engine roots (download separately) |
| **`bridge/UnrealEngineMCP`** | Lua mod: single-tick IPC pump, budgeted reflection |
| **`native/FatalGuard`** | Optional: delayed exit swallow + revive helpers |
| **`translator/`** | Python FastMCP tools for Cursor / Claude / Grok |
| **`siglab/`** | Optional static AOB / signature tooling (separate from live bridge) |

**No hardcoded gameplay offset tables.** UE4SS owns bootstrap; this MCP is reflection over UObjects.

### vs Editor Unreal MCP

| | Official / Editor MCP | **This** |
|--|----------------------|----------|
| Target | Unreal Editor project | **Shipped** game + UE4SS |
| Use case | Build levels, assets | Live mod / inspect running game |
| Inject | N/A | UE4SS in `Win64` |

## Honesty (read this)

Unreal runtime modding is **less stable** than Unity + BepInEx:

- Bad property/function calls can **crash or freeze** the game.
- **Revive** (`revive_unreal_bridge` / Ctrl+F9) is best-effort — not a guarantee.
- The **MCP host** should stay up; you get timeouts / `bridge_dead` when the game dies.
- **You** launch the game. Agents should not auto-kill/relaunch unless you ask.

Prefer **read** tools first; treat **writes** as unsafe.

## Related projects

| Repo | Role |
|------|------|
| [bepinex-mcp](https://github.com/rkuhn153/bepinex-mcp) | Live Unity + BepInEx |
| [gamecode-rag](https://github.com/rkuhn153/gamecode-rag) | Mono C# search |
| [il2cpp-decompiler](https://github.com/rkuhn153/il2cpp-decompiler) | IL2CPP static decompile |
| [flash-mod-bridge](https://github.com/rkuhn153/flash-mod-bridge) | Flash / Ruffle live bridge |
| *This* | Unreal **runtime** (UE4SS) live bridge |

## Layout

```text
unreal-engine-mcp/
  bridge/UnrealEngineMCP/scripts/   # UE4SS Lua mod
  translator/                       # FastMCP server
  scripts/Install-ToGame.ps1        # Install UE4SS + mod into a game
  scripts/Download-UE4SS.ps1
  native/FatalGuard/                # Optional native helper (source)
  siglab/                           # Optional AOB tooling
  tests/                            # Offline + live smoke
  skills/unreal-engine-mcp/         # Agent skill
  MASTER_PROMPT.md                  # Full agent steering
  AGENTS.md
```

## Install into a game

```powershell
cd unreal-engine-mcp

.\scripts\Install-ToGame.ps1 `
  -GameDir "D:\SteamLibrary\steamapps\common\YourUnrealGame" `
  -DownloadUE4SS
```

This copies UE4SS into the game’s `Win64` folder, installs `Mods\UnrealEngineMCP`, enables it in `mods.txt`, and creates `UnrealEngineMCP_IPC\`.

Launch the game once. Confirm UE4SS console shows something like `[UnrealEngineMCP] bridge started`.

Some games need UE4SS signature profiles or `no_hooks.txt` (see `tools/game-profiles` on a full checkout if you keep local profiles, or UE4SS docs).

## Run the MCP server

```powershell
cd translator
pip install -r requirements.txt

python unreal_mcp_server.py `
  --ipc-dir "D:\path\to\Game\Binaries\Win64\UnrealEngineMCP_IPC"
```

Or set env `UNREAL_MCP_IPC_DIR` to that folder.

### MCP client config

```json
{
  "mcpServers": {
    "unreal-engine-mcp": {
      "command": "C:/path/to/python.exe",
      "args": [
        "C:/path/to/unreal-engine-mcp/translator/unreal_mcp_server.py",
        "--ipc-dir",
        "D:/path/to/Game/Binaries/Win64/UnrealEngineMCP_IPC"
      ]
    }
  }
}
```

Restart the AI client after config changes.

### Agent skill

```powershell
Copy-Item ".\skills\unreal-engine-mcp" "$env:USERPROFILE\.cursor\skills\unreal-engine-mcp" -Recurse -Force
```

Or paste [`MASTER_PROMPT.md`](MASTER_PROMPT.md) as a system prompt.

## Tool flow

```text
list_unreal_games / select_unreal_game   (if multi-game)
  → ping_unreal_bridge / get_bridge_status
  → list_actors / find_objects / get_player
  → get_properties / get_property
  → set_property / call_function   (after describe_function)
```

If the pump goes stale while the game is still open: **`revive_unreal_bridge`**, then in-game **Ctrl+F9** (or FatalGuard autokick if installed).

## IPC

Next to the shipping exe (`*-Win64-Shipping.exe`):

```text
UnrealEngineMCP_IPC/
  heartbeat.json
  request.json / request.flag
  response.json / response.flag
  revive.flag          (optional)
  process_alive.json   (optional, FatalGuard)
```

## Tests

```powershell
# Offline
pip install pytest
pytest tests/test_bridge_client.py -q

# Live (game running with mod loaded)
python tests/smoke_live.py --ipc-dir "...\Win64\UnrealEngineMCP_IPC"
```

## Design notes

- **Single-tick IPC pump** only — avoids UE4SS dual-`ExecuteWithDelay` crash ([#1180](https://github.com/UE4SS-RE/RE-UE4SS/issues/1180)).
- Object identities are **session-scoped** — rediscover after map load / revive.
- File IPC is simple and portable; not as fast as an in-process HTTP bridge.
- Anti-cheat / blocked injectors still won’t load UE4SS.

## License

MIT — see [LICENSE](LICENSE).  
**UE4SS** is separate software — download from [UE4SS-RE/RE-UE4SS](https://github.com/UE4SS-RE/RE-UE4SS); not redistributed in this repo by default.
