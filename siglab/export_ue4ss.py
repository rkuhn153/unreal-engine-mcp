"""Export UE4SS_Signatures Lua stubs."""

from __future__ import annotations

from pathlib import Path


# What OnMatchFound must return — docs.ue4ss.com fixing-compatibility-problems
REQUIRED_SIGS = [
    "GUObjectArray",
    "FName_ToString",
    "FName_Constructor",
    "FText_Constructor",
    "StaticConstructObject",
    "GMalloc",
    "GUObjectHashTables",
    "GNatives",
    "ConsoleManager",
]


def lua_direct_match(aob: str) -> str:
    """Simple signature: match address is the target."""
    return f"""function Register()
    return "{aob}"
end

function OnMatchFound(MatchAddress)
    return MatchAddress
end
"""


def lua_lea_rip(aob: str, lea_len: int = 7) -> str:
    """Resolve RIP-relative LEA (common for GUObjectArray)."""
    return f"""function Register()
    return "{aob}"
end

function OnMatchFound(MatchAddress)
    local LeaInstr = MatchAddress
    local NextInstr = LeaInstr + 0x{lea_len:X}
    local Offset = LeaInstr + 0x3
    local AddressLoaded = NextInstr + DerefToInt32(Offset)
    return AddressLoaded
end
"""


def write_signature(
    out_dir: str | Path,
    name: str,
    aob: str,
    *,
    resolve: str = "direct",
) -> Path:
    """
    Write UE4SS_Signatures/<name>.lua
    resolve: 'direct' | 'lea_rip'
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # normalize filename
    fname = name.replace("::", "_").replace(" ", "_")
    if not fname.endswith(".lua"):
        # map common PS names to UE4SS file names
        mapping = {
            "FName::ToString": "FName_ToString",
            "FName::FName(wchar_t*)": "FName_Constructor",
            "FName::FName": "FName_Constructor",
            "StaticConstructObject_Internal": "StaticConstructObject",
            "FUObjectHashTables::Get()": "GUObjectHashTables",
        }
        fname = mapping.get(name, fname)
        if not fname.endswith(".lua"):
            fname = fname + ".lua"
    path = out_dir / fname
    body = lua_lea_rip(aob) if resolve == "lea_rip" else lua_direct_match(aob)
    path.write_text(body, encoding="utf-8")
    return path
