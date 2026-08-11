# SafeReflect (v3)

Full in-process SEH wrappers around `ProcessEvent` require UE headers / UE4SS C++ SDK.

**v3 ships the practical layer that works without a full RE-UE4SS C++ toolchain:**

| Layer | Role |
|-------|------|
| **Lua bridge v3** | Single-tick pump, signature describe, arg refuse, jobbed sample, map entry API |
| **FatalGuard 3.1.0-beta.1** | Delayed selective IAT + process_alive.json + revive autokick |
| **This folder** | Placeholder for future UE4SS C++ SafeReflect.dll when SDK is vendored |

## Future C++ work

When `RE-UE4SS` is available as a local build dependency:

1. `safe_get_property` / `safe_set_property` with `__try/__except`
2. Param-blob `ProcessEvent` from `UFunction` children
3. Chunked `GUObjectArray` iterator with per-object SEH

Until then, Lua prechecks + FatalGuard 3.1.0-beta.1 are the crash boundary.

Build FatalGuard: `..\FatalGuard\Build-FatalGuard.ps1`
