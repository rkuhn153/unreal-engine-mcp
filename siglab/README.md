# unreal-siglab — UE4SS signature finder

Static lab for **finding / scoring / exporting** UE4SS AOBs.  
Does **not** need the game running (except when you validate with a real `UE4SS.log`).

Uses **pefile** + **Capstone** (disassembly). Not Ghidra — focused on signature work.

## Known games that struggle with signatures

| Game | Issue |
|------|--------|
| **Goat Simulator 3** | Customized UE 4.27; CFBNA detours fail; experimental UE4SS crashes; needs settings + layouts ([RE-UE4SS #1186](https://github.com/UE4SS-RE/RE-UE4SS/issues/1186), Nexus GOAT Patch). **We hit this in this repo.** |
| **Avowed** | Pattern scan fails for required sigs ([#1207](https://github.com/UE4SS-RE/RE-UE4SS/issues/1207), customized engine). |
| **FF7 Remake, KH3, Ghostwire, …** | Need `UE4SS_Signatures` in UE4SS `zCustomGameConfigs`. |
| **Like a Dragon Ishin!** | Custom `GUObjectArray.lua` lea resolve. |

“Easy” titles (stock-ish UE, e.g. many older games / BFBB for us) often work with default scan.

## CLI

From repo root:

```powershell
# PE summary
python -m siglab.cli info "D:\path\Game-Win64-Shipping.exe"

# Scan AOB
python -m siglab.cli scan "D:\path\Game-Win64-Shipping.exe" "48 8B 05 ?? ?? ?? ??"

# Disassemble at VA (from your RE session)
python -m siglab.cli disasm "D:\path\Game-Win64-Shipping.exe" 0x140001000

# Run seed corpus (custom configs + common prologues)
python -m siglab.cli corpus "D:\path\Game-Win64-Shipping.exe" -o report.json

# Parse UE4SS.log
python -m siglab.cli log "D:\path\ue4ss\UE4SS.log"

# Suggest AOB from a VA you found
python -m siglab.cli suggest "D:\path\Game-Win64-Shipping.exe" 0x141234567

# Export unique corpus hits as UE4SS_Signatures/*.lua
python -m siglab.cli export-corpus "D:\path\Game-Win64-Shipping.exe" "D:\path\ue4ss\UE4SS_Signatures"
```

## MCP server

```toml
[mcp_servers.unreal-siglab]
command = 'C:\Python313\python.exe'
args = [
  'C:\Users\ryan\OneDrive\Desktop\Game Modding\UnrealEngineMCP\siglab\mcp_server.py',
]
```

Tools: `pe_info`, `scan_aob`, `disassemble_va`, `run_signature_corpus`, `parse_ue4ss_log`,
`suggest_aob_from_va`, `export_ue4ss_signature`, `export_unique_corpus_signatures`,
`full_signature_report`, `known_hard_games`.

## Workflow

### Preferred one-shot (maximize unique AOBs)

```powershell
python -m siglab.cli solve "Game-Win64-Shipping.exe" "ue4ss\UE4SS_Signatures" --log "UE4SS.log"
```

This will:
1. **Recover** every `Found` address from the log into unique AOBs (RIP-load sites for globals)
2. **Disambiguate** multi-hit failures (e.g. ConsoleManager with 2 candidates)
3. Run the **expanded corpus** and **auto-tighten** multi-hit seeds (grow + disasm wildcards until 1 hit)
4. Report any still-**missing** `REQUIRED_SIGS`

MCP: `solve_signatures`.

### Manual steps

1. Install UE4SS, launch game once (or fail).
2. `parse_ue4ss_log` → list **Failed** symbols.
3. `run_signature_corpus` on shipping exe → unique hits.
4. Or recover only:
   ```powershell
   python -m siglab.cli recover "Game-Win64-Shipping.exe" "UE4SS.log" "ue4ss\UE4SS_Signatures"
   ```
5. Cold misses: RE VA → `suggest` → `export`.
6. Freeze into `tools/game-profiles/<Game>/`.

## Deps

```
pefile
capstone
mcp[cli]   # for MCP server only
```

## Limits

- Does not invent correct `OnMatchFound` for every pattern (you pick `direct` vs `lea_rip`).
- **Unique AOB ≠ correct semantics** — still validate in UE4SS.log after boot.
- **MemberVariableLayout / VTableLayout** (MCD, Goat3) are **not** AOBs; solve cannot invent those.
- Live inject validation still needs a real game boot + log.
- “All the time” means maximize export rate, not guarantee 9/9 on every fork.
