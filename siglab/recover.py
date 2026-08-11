"""
Bulletproof-ish signature recovery from a successful UE4SS.log + shipping PE.

When PatternSleuth already Found symbols once, we:
  1) Map runtime addresses → preferred ImageBase VAs via MainExe
  2) For globals: find lea/mov [rip+disp] xrefs → unique AOB + lea_rip resolve
  3) For functions: grow unique prologue AOB at the VA
  4) Export UE4SS_Signatures/*.lua
  5) Self-test uniqueness in this PE

Does not replace deep RE when log has zero Found — but closes the loop for
"scan worked once, freeze the AOBs" and fills corpus misses.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from siglab.aob import Pattern, make_unique_aob, scan
from siglab.export_ue4ss import write_signature
from siglab.log_parse import parse_ue4ss_log_file
from siglab.pe_image import PeImage

# log name substring -> (ue4ss file stem, resolve mode)
SYMBOL_MAP: list[tuple[str, str, str]] = [
    ("guobjectarray", "GUObjectArray", "lea_rip"),
    ("gmalloc", "GMalloc", "lea_rip"),
    ("staticconstructobject", "StaticConstructObject", "direct"),
    ("fname::tostring", "FName_ToString", "direct"),
    ("fname::fname", "FName_Constructor", "direct"),
    ("ftext::ftext", "FText_Constructor", "direct"),
    ("fuobjecthashtables", "GUObjectHashTables", "direct"),
    ("guobjecthashtables", "GUObjectHashTables", "direct"),
    ("gnatives", "GNatives", "lea_rip"),
    ("gameenginetick", "GameEngineTick", "direct"),
    ("consolemanagersingleton", "ConsoleManager", "direct"),
    ("consolemanager", "ConsoleManager", "direct"),
]


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def parse_log_addresses(log_path: Path) -> tuple[int | None, dict[str, int]]:
    text = _read_text(log_path)
    m = re.search(r"MainExe @ (0x[0-9a-fA-F]+)", text)
    base = int(m.group(1), 16) if m else None
    found: dict[str, int] = {}
    for name, addr in re.findall(
        r"Found\s+(.+?):\s*(0x[0-9a-fA-F]+)\s*$", text, re.I | re.M
    ):
        found[name.strip()] = int(addr, 16)
    return base, found


def preferred_vas(image_base: int, main_exe: int, found: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, rt in found.items():
        out[name] = image_base + (rt - main_exe)
    return out


def find_rip_loads(img: PeImage, target_va: int, max_hits: int = 64) -> list[int]:
    prefixes = [
        bytes.fromhex(p)
        for p in (
            "488D0D",
            "488D05",
            "488D15",
            "488D1D",
            "488D3D",
            "488D35",
            "4C8D05",
            "4C8D0D",
            "4C8D15",
            "4C8D1D",
            "488B05",
            "488B0D",
            "488B15",
            "488B1D",
            "488B3D",
            "4C8B05",
            "4C8B0D",
        )
    ]
    blob = img.code_blob
    hits: list[int] = []
    for pref in prefixes:
        start = 0
        while True:
            j = blob.find(pref, start)
            if j < 0:
                break
            if j + 7 <= len(blob):
                disp = struct.unpack_from("<i", blob, j + 3)[0]
                va = img.blob_offset_to_va(j)
                if va is not None and va + 7 + disp == target_va:
                    hits.append(va)
                    if len(hits) >= max_hits:
                        return hits
            start = j + 1
    return hits


def unique_aob_at(img: PeImage, site_va: int, min_len: int = 10, max_len: int = 48) -> str | None:
    """Build a unique (or best-effort) AOB at site_va using disasm wildcards + grow."""
    off = img.va_to_blob_offset(site_va)
    if off is None:
        return None
    r = make_unique_aob(
        img.code_blob,
        off,
        base_va=site_va,
        min_len=min_len,
        max_len=max_len,
        prefer_wildcard=True,
    )
    return r.get("aob")


def map_symbol(log_name: str) -> tuple[str, str] | None:
    low = log_name.lower().replace(" ", "")
    for key, file_stem, resolve in SYMBOL_MAP:
        if key in low:
            return file_stem, resolve
    # fallback filename
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", log_name).strip("_")
    return safe, "direct"


def recover_from_log(
    exe_path: str | Path,
    log_path: str | Path,
    out_dir: str | Path,
) -> dict:
    exe_path, log_path, out_dir = Path(exe_path), Path(log_path), Path(out_dir)
    img = PeImage.load(exe_path)
    main_exe, found = parse_log_addresses(log_path)
    if not main_exe or not found:
        return {
            "ok": False,
            "error": "need MainExe base and at least one Found line in UE4SS.log",
            "main_exe": hex(main_exe) if main_exe else None,
            "found": list(found.keys()),
        }

    pref = preferred_vas(img.image_base, main_exe, found)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    errors: list[str] = []

    for log_name, va in pref.items():
        mapped = map_symbol(log_name)
        if not mapped:
            continue
        file_stem, resolve = mapped
        try:
            if resolve == "lea_rip":
                sites = find_rip_loads(img, va)
                if not sites:
                    # Fall back to direct AOB at the global's address region (rare)
                    errors.append(f"{log_name}: no RIP load sites for {hex(va)}")
                    aob = unique_aob_at(img, va)
                    if aob:
                        path = write_signature(out_dir, file_stem, aob, resolve="direct")
                        n = len(scan(img.code_blob, Pattern.parse(aob), max_hits=8))
                        written.append(
                            {
                                "log_name": log_name,
                                "file": str(path),
                                "target_va": hex(va),
                                "aob": aob,
                                "resolve": "direct",
                                "hits": n,
                                "ok": n == 1,
                                "note": "fallback direct; no lea site",
                            }
                        )
                    continue
                # Prefer a site that yields a unique AOB
                best_site = None
                best_aob = None
                best_n = 99
                for site in sites[:12]:
                    aob = unique_aob_at(img, site)
                    if not aob:
                        continue
                    n = len(scan(img.code_blob, Pattern.parse(aob), max_hits=8))
                    if n < best_n:
                        best_n, best_site, best_aob = n, site, aob
                    if n == 1:
                        break
                if not best_aob or best_site is None:
                    errors.append(f"{log_name}: could not build AOB at any of {len(sites)} sites")
                    continue
                path = write_signature(out_dir, file_stem, best_aob, resolve="lea_rip")
                written.append(
                    {
                        "log_name": log_name,
                        "file": str(path),
                        "target_va": hex(va),
                        "site_va": hex(best_site),
                        "aob": best_aob,
                        "resolve": "lea_rip",
                        "hits": best_n,
                        "ok": best_n == 1,
                        "sites_considered": min(len(sites), 12),
                    }
                )
            else:
                aob = unique_aob_at(img, va)
                if not aob:
                    errors.append(f"{log_name}: could not build AOB at {hex(va)}")
                    continue
                path = write_signature(out_dir, file_stem, aob, resolve="direct")
                n = len(scan(img.code_blob, Pattern.parse(aob), max_hits=8))
                written.append(
                    {
                        "log_name": log_name,
                        "file": str(path),
                        "target_va": hex(va),
                        "aob": aob,
                        "resolve": "direct",
                        "hits": n,
                        "ok": n == 1,
                    }
                )
        except Exception as e:
            errors.append(f"{log_name}: {e}")

    # Multi-hit failures from the same log (e.g. ConsoleManager with 2 candidates)
    from siglab.log_parse import parse_ue4ss_log_file as _parse

    sr = _parse(log_path)
    for name, addrs in sr.multi_hit.items():
        mapped = map_symbol(name)
        if not mapped:
            continue
        file_stem, resolve = mapped
        if any(w.get("file", "").endswith(file_stem + ".lua") for w in written):
            continue
        # Convert runtime addresses using MainExe slide
        cands: list[int] = []
        for a in addrs:
            try:
                rt = int(a, 16)
                cands.append(img.image_base + (rt - main_exe))
            except ValueError:
                continue
        picked = None
        for va in cands:
            aob = unique_aob_at(img, va)
            if not aob:
                continue
            n = len(scan(img.code_blob, Pattern.parse(aob), max_hits=8))
            if n == 1:
                picked = (va, aob, n, resolve)
                break
            if picked is None or n < picked[2]:
                picked = (va, aob, n, resolve)
        if not picked:
            errors.append(f"{name}: could not disambiguate multi-hit {addrs}")
            continue
        va, aob, n, resolve = picked
        # Globals often need lea_rip if this looks like a data VA outside code — keep direct for functions
        path = write_signature(out_dir, file_stem, aob, resolve="direct")
        written.append(
            {
                "log_name": name,
                "file": str(path),
                "target_va": hex(va),
                "aob": aob,
                "resolve": "direct",
                "hits": n,
                "ok": n == 1,
                "note": "disambiguated multi-hit from log",
                "candidates": [hex(c) for c in cands],
            }
        )

    all_ok = all(w.get("ok") for w in written) and len(written) > 0
    return {
        "ok": all_ok,
        "main_exe": hex(main_exe),
        "image_base": hex(img.image_base),
        "found_in_log": list(found.keys()),
        "multi_hit_in_log": {k: v for k, v in sr.multi_hit.items()},
        "written": written,
        "errors": errors,
        "out_dir": str(out_dir),
        "bulletproof": all_ok and not errors,
    }
