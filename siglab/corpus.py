"""
Seed AOB corpus from known working custom game configs + common prologues.

These are *candidates* to try on a new binary — not guaranteed hits.
"""

from __future__ import annotations

from pathlib import Path

# (symbol_name, aob, resolve_mode, note)
CORPUS: list[tuple[str, str, str, str]] = [
    # From custom configs in tools/UE4SS/custom_game_configs
    (
        "FName_Constructor",
        "48 89 5C 24 10 48 89 74 24 18 57 48 81 EC 40 04 00 00",
        "direct",
        "Ghostwire/KH3-style FName ctor prologue",
    ),
    (
        "FName_Constructor",
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 8B FA 48 8B D9",
        "direct",
        "alternate FName ctor (shorter stack frame)",
    ),
    (
        "GUObjectArray",
        "48 8D ?? ?? ?? ?? ?? 44 8B 84 24 90 00 00 00 8B 94 24 98 00 00 00 E8 ?? ?? ?? ?? E8",
        "lea_rip",
        "Like a Dragon Ishin GUObjectArray lea",
    ),
    (
        "GUObjectArray",
        "48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 48 8B D8 48 85 C0",
        "lea_rip",
        "common GUObjectArray lea + call pattern",
    ),
    (
        "GMalloc",
        "48 8B 0D ?? ?? ?? ?? 48 85 C9 74 ?? 48 8B 01 FF 50",
        "lea_rip",
        "GMalloc load + null check + vcall",
    ),
    (
        "StaticConstructObject",
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 50 8B 41",
        "direct",
        "common StaticConstructObject-ish prologue (may multi-hit)",
    ),
    (
        "StaticConstructObject",
        "48 89 5C 24 10 48 89 74 24 18 55 57 41 54 41 56",
        "direct",
        "StaticConstructObject push-heavy prologue variant",
    ),
    (
        "FName_ToString",
        "48 89 5C 24 08 57 48 83 EC 20 8B 01",
        "direct",
        "common FName::ToString prologue variant",
    ),
    (
        "FName_ToString",
        "48 89 5C 24 10 48 89 74 24 18 57 48 83 EC 20 8B 19",
        "direct",
        "FName::ToString alternate",
    ),
    (
        "FText_Constructor",
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 48 8B F2 48 8B F9",
        "direct",
        "FText ctor-ish prologue",
    ),
    (
        "GUObjectHashTables",
        "48 83 EC 28 48 8B 0D ?? ?? ?? ?? 48 85 C9 75 ?? 48 8D 0D",
        "direct",
        "FUObjectHashTables::Get lazy-init style",
    ),
    (
        "GUObjectHashTables",
        "40 53 48 83 EC 20 48 8B D9 48 8B 0D ?? ?? ?? ?? 48 85 C9",
        "direct",
        "FUObjectHashTables::Get alternate",
    ),
    (
        "GNatives",
        "48 8D 0D ?? ?? ?? ?? 48 89 05 ?? ?? ?? ?? 48 8D 05",
        "lea_rip",
        "GNatives table lea pattern",
    ),
    (
        "ConsoleManager",
        "48 89 5C 24 08 57 48 83 EC 20 48 8B D9 E8 ?? ?? ?? ?? 48 8B F8",
        "direct",
        "IConsoleManager accessor-ish",
    ),
    (
        "ConsoleManager",
        "48 83 EC 28 48 8B 0D ?? ?? ?? ?? 48 85 C9 75 ?? E8",
        "direct",
        "ConsoleManager singleton lazy get",
    ),
    (
        "GameEngineTick",
        "48 89 5C 24 10 48 89 74 24 18 55 57 41 56 48 8D 6C 24 ?? 48 81 EC",
        "direct",
        "UGameEngine::Tick-like frame prologue",
    ),
]


def load_corpus_from_configs(root: str | Path | None = None) -> list[tuple[str, str, str, str]]:
    """Parse Register() returns from custom_game_configs UE4SS_Signatures."""
    if root is None:
        root = Path(__file__).resolve().parents[1] / "tools" / "UE4SS" / "custom_game_configs"
    root = Path(root)
    extra: list[tuple[str, str, str, str]] = []
    if not root.is_dir():
        return extra
    for lua in root.rglob("*.lua"):
        try:
            text = lua.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "function Register" not in text:
            continue
        # extract return "..."
        import re

        m = re.search(r'return\s+"([^"]+)"', text)
        if not m:
            continue
        aob = m.group(1)
        name = lua.stem
        resolve = "lea_rip" if "DerefToInt32" in text or "LeaInstr" in text else "direct"
        game = lua.parent.parent.name if lua.parent.name == "UE4SS_Signatures" else "?"
        extra.append((name, aob, resolve, f"from custom config: {game}"))
    return extra


def all_corpus() -> list[tuple[str, str, str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str, str]] = []
    for row in CORPUS + load_corpus_from_configs():
        key = (row[0], row[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
