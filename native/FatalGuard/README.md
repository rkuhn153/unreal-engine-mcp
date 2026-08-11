# FatalGuard 3.1.0-beta.1

SemVer 2.0.0 pre-release (**beta** — usable, not a stability lock). Survives Unreal
**ExitProcess / abort** after a Fatal so the process can keep running (state may
still be corrupt). Designed to **not** boot-crash BFBB.

**3.1.0-beta.1:** mid-game MCP revive without user Ctrl+F9 or map reload — watches
`UnrealEngineMCP_IPC/revive.flag` and stale `heartbeat.json`, then synthesizes
Ctrl+F9 so UnrealEngineMCP’s keybind runs `force_revive` + an immediate pump.

`process_alive.json` reports `version` (compact int **310**) and `version_semver`
(`3.1.0-beta.1`).

## What was wrong before

- Hooked **every** module’s IAT (d3d9, XInput, …) → instant `0xC0000005`
- Broken UE4SS `start_mod` export ABI
- MessageBox hooks on system DLLs

## Design

| Rule | Detail |
|------|--------|
| Main exe only | IAT patch **only** `Pineapple-Win64-Shipping.exe` |
| Minimal hooks | `ExitProcess` + optional `abort` |
| Delayed | Default **45s** after load (`FATALGUARD_DELAY_MS`) |
| Alive file | `UnrealEngineMCP_IPC/process_alive.json` every 1s |
| UE4SS | Opaque `start_mod` / `uninstall_mod` stubs |

## Install

```powershell
.\Build-FatalGuard.ps1
# or disable:
.\Build-FatalGuard.ps1 -Disable
```

## Env

| Env | Effect |
|-----|--------|
| `FATALGUARD=0` | No hooks |
| `FATALGUARD_DELAY_MS=60000` | Wait longer before hooks |
| `FATALGUARD_MSGBOX=1` | Also swallow fatal-style MessageBoxW **on main exe only** |
| `FATALGUARD_ABORT=0` | Don’t hook abort |
| `FATALGUARD_AUTOKICK=0` | Disable synthetic Ctrl+F9 on revive.flag / stale heartbeat |

## Limits

- Does **not** fix access violations mid-instruction (would need VEH continue — unsafe)
- After a swallowed Fatal, reload a save if the game acts weird
- User launches the game; do not agent-relaunch for testing
