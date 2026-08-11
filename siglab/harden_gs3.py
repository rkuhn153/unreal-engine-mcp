"""One-shot: recover GS3 signature AOBs from UE4SS.log + PE, export bulletproof set."""

from __future__ import annotations

import re
import struct
from pathlib import Path

from siglab.aob import Pattern, scan
from siglab.disasm import disassemble, prologue_aob
from siglab.export_ue4ss import write_signature
from siglab.pe_image import PeImage

EXE = Path(r"D:\Program Files\EpicGames\GoatSimulator3\Goat2\Binaries\Win64\Goat2-Win64-Shipping.exe")
LOG = Path(r"D:\Program Files\EpicGames\GoatSimulator3\Goat2\Binaries\Win64\UE4SS.log")
OUT = Path(__file__).resolve().parents[1] / "tools" / "game-profiles" / "GoatSimulator3" / "UE4SS_Signatures"


def read_log(path: Path) -> tuple[int | None, dict[str, int]]:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or b"\x00U\x00E" in data[:40]:
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    m = re.search(r"MainExe @ (0x[0-9a-fA-F]+)", text)
    base = int(m.group(1), 16) if m else None
    found: dict[str, int] = {}
    for name, addr in re.findall(
        r"Found\s+(.+?):\s*(0x[0-9a-fA-F]+)\s*$", text, re.I | re.M
    ):
        found[name.strip()] = int(addr, 16)
    return base, found


def find_lea_to_target(img: PeImage, target_va: int, max_hits: int = 32) -> list[tuple[int, bytes]]:
    """Find lea/mov reg, [rip+disp] that resolve to target_va."""
    prefixes = [
        bytes.fromhex("488D0D"),  # lea rcx
        bytes.fromhex("488D05"),  # lea rax
        bytes.fromhex("488D15"),  # lea rdx
        bytes.fromhex("488D1D"),  # lea rbx
        bytes.fromhex("488D3D"),  # lea rdi
        bytes.fromhex("488D35"),  # lea rsi
        bytes.fromhex("4C8D05"),  # lea r8
        bytes.fromhex("4C8D0D"),  # lea r9
        bytes.fromhex("4C8D15"),  # lea r10
        bytes.fromhex("4C8D1D"),  # lea r11
        bytes.fromhex("488B05"),  # mov rax,[rip]
        bytes.fromhex("488B0D"),  # mov rcx,[rip]
        bytes.fromhex("488B15"),  # mov rdx,[rip]
        bytes.fromhex("488B3D"),  # mov rdi,[rip]
        bytes.fromhex("4C8B05"),  # mov r8
        bytes.fromhex("4C8B0D"),  # mov r9
    ]
    blob = img.code_blob
    hits: list[tuple[int, bytes]] = []
    for pref in prefixes:
        start = 0
        while True:
            j = blob.find(pref, start)
            if j < 0:
                break
            if j + 7 > len(blob):
                break
            disp = struct.unpack_from("<i", blob, j + 3)[0]
            va = img.blob_offset_to_va(j)
            if va is not None:
                next_ip = va + 7
                if next_ip + disp == target_va:
                    # capture surrounding bytes for AOB (include a bit of context)
                    lo = max(0, j - 0)
                    hi = min(len(blob), j + 16)
                    hits.append((va, blob[lo:hi]))
                    if len(hits) >= max_hits:
                        return hits
            start = j + 1
    return hits


def aob_from_bytes(data: bytes, wildcard_rel32_at: int | None = 3) -> str:
    """Hex AOB; wildcard 4-byte disp at offset (default after 3-byte lea/mov prefix)."""
    parts: list[str] = []
    for i, b in enumerate(data):
        if wildcard_rel32_at is not None and wildcard_rel32_at <= i < wildcard_rel32_at + 4:
            parts.append("??")
        else:
            parts.append(f"{b:02X}")
    return " ".join(parts)


def unique_aob_for_site(img: PeImage, site_va: int, min_len: int = 8, max_len: int = 28) -> str | None:
    """Grow prologue from site_va until unique in code blob."""
    off = img.va_to_blob_offset(site_va)
    if off is None:
        return None
    for length in range(min_len, max_len + 1):
        chunk = img.code_blob[off : off + length]
        if len(chunk) < length:
            break
        # wildcard RIP disp if this looks like lea/mov [rip+disp] at start
        if length >= 7 and chunk[0] in (0x48, 0x4C) and chunk[1] in (0x8D, 0x8B) and chunk[2] in (
            0x05,
            0x0D,
            0x15,
            0x1D,
            0x25,
            0x2D,
            0x35,
            0x3D,
        ):
            aob = aob_from_bytes(chunk, 3)
        else:
            aob = " ".join(f"{b:02X}" for b in chunk)
        try:
            pat = Pattern.parse(aob)
        except ValueError:
            continue
        hits = scan(img.code_blob, pat, max_hits=8)
        if len(hits) == 1:
            return aob
    # fallback strict 16 bytes
    chunk = img.code_blob[off : off + 16]
    return " ".join(f"{b:02X}" for b in chunk)


def main() -> None:
    print("Loading", EXE)
    img = PeImage.load(EXE)
    base, found = read_log(LOG) if LOG.is_file() else (None, {})
    print("log MainExe", hex(base) if base else None)
    print("log found symbols:", list(found.keys()))

    preferred: dict[str, int] = {}
    if base and found:
        for name, rt in found.items():
            rva = rt - base
            preferred[name] = img.image_base + rva
            print(f"  {name}: preferred VA {hex(preferred[name])} (rva {hex(rva)})")

    OUT.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    # Map log names -> UE4SS signature file names + resolve style
    export_plan: list[tuple[str, str, str]] = []  # log_name, file_name, resolve

    if "GUObjectArray" in preferred:
        target = preferred["GUObjectArray"]
        sites = find_lea_to_target(img, target)
        print(f"GUObjectArray xref sites: {len(sites)}")
        if sites:
            site_va, raw = sites[0]
            aob = unique_aob_for_site(img, site_va) or aob_from_bytes(raw[:12], 3)
            print("  site", hex(site_va), "aob", aob)
            ins = disassemble(img.code_blob[img.va_to_blob_offset(site_va) or 0 :][:48], site_va, 4)
            for i in ins:
                print("   ", i)
            p = write_signature(OUT, "GUObjectArray", aob, resolve="lea_rip")
            written.append(str(p))
            export_plan.append(("GUObjectArray", "GUObjectArray", "lea_rip"))

    if "GMalloc" in preferred:
        target = preferred["GMalloc"]
        sites = find_lea_to_target(img, target)
        print(f"GMalloc xref sites: {len(sites)}")
        if sites:
            site_va, raw = sites[0]
            aob = unique_aob_for_site(img, site_va) or aob_from_bytes(raw[:12], 3)
            # GMalloc often mov reg,[rip+disp] to pointer — still lea_rip style resolve
            resolve = "lea_rip"
            print("  site", hex(site_va), "aob", aob)
            p = write_signature(OUT, "GMalloc", aob, resolve=resolve)
            written.append(str(p))

    # Function symbols: use preferred VA as function start if log points at function
    func_map = {
        "StaticConstructObject_Internal": ("StaticConstructObject", "direct"),
        "FName::ToString": ("FName_ToString", "direct"),
        "FName::FName(wchar_t*)": ("FName_Constructor", "direct"),
        "FName::FName": ("FName_Constructor", "direct"),
        "FUObjectHashTables::Get()": ("GUObjectHashTables", "direct"),
        "GNatives": ("GNatives", "lea_rip"),
        "GameEngineTick": ("GameEngineTick", "direct"),
    }
    for log_name, (file_name, resolve) in func_map.items():
        # fuzzy match log keys
        key = None
        for k in preferred:
            if log_name.lower() in k.lower() or k.lower() in log_name.lower():
                key = k
                break
        if not key:
            continue
        va = preferred[key]
        # For lea_rip globals we already handled some; functions use direct
        if resolve == "lea_rip" and file_name in ("GMalloc", "GUObjectArray"):
            continue
        if resolve == "lea_rip":
            sites = find_lea_to_target(img, va)
            if not sites:
                print(f"skip {key}: no lea sites")
                continue
            site_va = sites[0][0]
            aob = unique_aob_for_site(img, site_va) or ""
            p = write_signature(OUT, file_name, aob, resolve="lea_rip")
        else:
            aob = unique_aob_for_site(img, va)
            if not aob:
                print(f"skip {key}: no aob")
                continue
            # verify unique
            hits = scan(img.code_blob, Pattern.parse(aob), max_hits=8)
            print(f"{key} @{hex(va)} aob={aob} hits={len(hits)}")
            if len(hits) != 1:
                # extend
                aob2 = unique_aob_for_site(img, va, min_len=12, max_len=40)
                if aob2:
                    aob = aob2
                    hits = scan(img.code_blob, Pattern.parse(aob), max_hits=8)
                    print(f"  extended hits={len(hits)} aob={aob}")
            p = write_signature(OUT, file_name, aob, resolve="direct")
        written.append(str(p))

    # Also try FName from corpus unique if not in log
    print("\nWritten", len(written), "files to", OUT)
    for w in written:
        print(" ", w)

    # Self-test: re-scan all written AOBs for uniqueness
    print("\n=== uniqueness self-test ===")
    for lua in sorted(OUT.glob("*.lua")):
        text = lua.read_text(encoding="utf-8")
        m = re.search(r'return\s+"([^"]+)"', text)
        if not m:
            continue
        aob = m.group(1)
        hits = scan(img.code_blob, Pattern.parse(aob), max_hits=8)
        status = "OK" if len(hits) == 1 else f"BAD hits={len(hits)}"
        print(f"  {lua.name}: {status}")


if __name__ == "__main__":
    main()
