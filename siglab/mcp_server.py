#!/usr/bin/env python3
"""
unreal-siglab MCP — PE disassembly, AOB scan, UE4SS log parse, signature export.

Does NOT require the game to be running (static). Use unreal-engine-mcp for live work.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP

from siglab import engine

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("unreal-siglab")

mcp = FastMCP("unreal-siglab")


def _j(obj: dict) -> str:
    return json.dumps(obj, indent=2)


@mcp.tool()
async def pe_info(exe_path: str) -> str:
    """Summarize a Win64 shipping PE (sections, image base, code size)."""
    try:
        return _j(engine.analyze_image(exe_path))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def scan_aob(exe_path: str, pattern: str, max_hits: int = 32) -> str:
    """Scan .text for an AOB pattern (hex bytes, ?? wildcards). Returns VAs + disasm."""
    try:
        return _j(engine.scan_aob(exe_path, pattern, max_hits=max_hits))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def disassemble_va(exe_path: str, va: str, count: int = 24) -> str:
    """Disassemble at a virtual address (0x...). Also suggests a prologue AOB."""
    try:
        return _j(engine.disasm_at(exe_path, va, count=count))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def run_signature_corpus(exe_path: str) -> str:
    """
    Try known UE4SS-related AOB seeds (custom game configs + common prologues).
    Reports unique / multi-hit / miss — unique hits are export candidates.
    """
    try:
        return _j(engine.run_corpus(exe_path))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def parse_ue4ss_log(log_path: str) -> str:
    """Parse UE4SS.log for Found / Failed / multi-hit signature scan results."""
    try:
        return _j(engine.parse_log(log_path))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def suggest_aob_from_va(exe_path: str, va: str, length: int = 20) -> str:
    """You found a VA in RE — produce AOB candidates + disassembly window."""
    try:
        return _j(engine.suggest_aob_from_va(exe_path, va, length=length))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def export_ue4ss_signature(
    out_dir: str,
    name: str,
    aob: str,
    resolve: str = "direct",
) -> str:
    """
    Write UE4SS_Signatures/<name>.lua.
    resolve: 'direct' (match = target) or 'lea_rip' (resolve LEA [rip+disp32]).
    """
    try:
        return _j(engine.export_signature(out_dir, name, aob, resolve=resolve))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def export_unique_corpus_signatures(exe_path: str, out_dir: str) -> str:
    """Run corpus; write only unique (single-hit) signatures as UE4SS Lua files."""
    try:
        return _j(engine.export_unique_from_corpus(exe_path, out_dir))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def full_signature_report(exe_path: str, log_path: str = "") -> str:
    """PE info + corpus scan + optional UE4SS.log parse in one report."""
    try:
        return _j(engine.full_report(exe_path, log_path or None))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def recover_signatures_from_log(
    exe_path: str,
    log_path: str,
    out_dir: str,
) -> str:
    """
    Bulletproof recovery: use a UE4SS.log that already Found symbols + the shipping exe
    to build unique AOBs and write UE4SS_Signatures/*.lua (self-tested for 1 hit).
    Best path when scan worked once and you want frozen sigs for misses/portability.
    """
    try:
        return _j(engine.recover_signatures_from_log(exe_path, log_path, out_dir))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def solve_signatures(exe_path: str, out_dir: str, log_path: str = "") -> str:
    """
    One-shot maximizer: recover from UE4SS.log (Found + multi-hit), run expanded corpus,
    auto-tighten multi-hit AOBs to unique patterns, write UE4SS_Signatures/*.lua.
    Returns missing REQUIRED_SIGS if any remain.
    """
    try:
        return _j(engine.solve_signatures(exe_path, out_dir, log_path or None))
    except Exception as e:
        return f"❌ Error: {e}"


@mcp.tool()
async def known_hard_games() -> str:
    """List known UE games that struggle with default UE4SS signatures / hooks."""
    return _j(
        {
            "games": [
                {
                    "name": "Goat Simulator 3",
                    "why": "Customized 4.27; CallFunctionByNameWithArguments often unavailable; "
                    "experimental UE4SS can crash; needs engine override + hook policy; "
                    "community GOAT Patch (MemberVariableLayout/VTableLayout).",
                    "refs": ["RE-UE4SS #1186", "Nexus UE4SS GOAT Patch", "this repo tools/game-profiles/GoatSimulator3"],
                },
                {
                    "name": "Minecraft Dungeons",
                    "why": "UE 4.22.3 Mojang fork. PS scan finds most symbols but fails "
                    "GUObjectHashTables + ConsoleManager multi-hit; crash often needs "
                    "MemberVariableLayout (not only AOBs). Retail Shipping harder than debug.",
                    "refs": ["RE-UE4SS #1211", "RE-UE4SS #1219"],
                },
                {
                    "name": "Avowed",
                    "why": "experimental-latest pattern scan fails for required signatures (customized engine).",
                    "refs": ["RE-UE4SS #1207"],
                },
                {
                    "name": "Final Fantasy 7 Remake / KH3 / Ghostwire Tokyo / etc.",
                    "why": "Ship with custom UE4SS_Signatures in zCustomGameConfigs (FName, StaticConstructObject, …).",
                    "refs": ["tools/UE4SS/custom_game_configs/"],
                },
                {
                    "name": "S.T.A.L.K.E.R. 2 / similar",
                    "why": "Often needs custom FName / scan work; large customized UE5 titles.",
                    "refs": ["UE4SS docs + community configs"],
                },
            ],
            "note": "Hard ≠ impossible. Prefer `solve_signatures` then layout inis if still crashing.",
        }
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
