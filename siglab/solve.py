"""
One-shot solver: export as many unique UE4SS_Signatures as possible.

Pipeline:
  1) If UE4SS.log present → recover Found + disambiguate multi-hit
  2) Run expanded corpus; unique hits export; multi-hits auto-tighten
  3) Report still-missing REQUIRED_SIGS
"""

from __future__ import annotations

from pathlib import Path

from siglab.aob import Pattern, make_unique_aob, scan
from siglab.corpus import all_corpus
from siglab.export_ue4ss import REQUIRED_SIGS, write_signature
from siglab.log_parse import parse_ue4ss_log_file
from siglab.pe_image import PeImage
from siglab.recover import recover_from_log


def _stem_written(out_dir: Path) -> set[str]:
    if not out_dir.is_dir():
        return set()
    return {p.stem for p in out_dir.glob("*.lua")}


def solve(
    exe_path: str | Path,
    out_dir: str | Path,
    log_path: str | Path | None = None,
) -> dict:
    exe_path, out_dir = Path(exe_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    img = PeImage.load(exe_path)

    steps: list[dict] = []
    written_files: list[str] = []

    # --- 1) log recovery ---
    if log_path and Path(log_path).is_file():
        rec = recover_from_log(exe_path, log_path, out_dir)
        steps.append({"step": "recover_from_log", "result": rec})
        for w in rec.get("written") or []:
            if w.get("file"):
                written_files.append(w["file"])
    else:
        steps.append({"step": "recover_from_log", "skipped": True, "reason": "no log"})

    have = _stem_written(out_dir)

    # --- 2) corpus + tighten ---
    corpus_unique = 0
    corpus_tightened = 0
    corpus_miss = 0
    corpus_still_multi = 0
    by_symbol: dict[str, list[dict]] = {}

    for symbol, aob, resolve, note in all_corpus():
        if symbol in have:
            continue
        try:
            pat = Pattern.parse(aob)
        except ValueError:
            continue
        offs = scan(img.code_blob, pat, max_hits=16)
        entry = {
            "symbol": symbol,
            "seed_aob": aob,
            "resolve": resolve,
            "note": note,
            "seed_hits": len(offs),
        }
        if len(offs) == 0:
            corpus_miss += 1
            entry["status"] = "miss"
            by_symbol.setdefault(symbol, []).append(entry)
            continue
        if len(offs) == 1:
            path = write_signature(out_dir, symbol, aob, resolve=resolve)
            # verify unique after write (seed already unique)
            n = len(scan(img.code_blob, Pattern.parse(aob), max_hits=4))
            # still grow a tighter unique if seed is short/fragile
            off = offs[0]
            va = img.blob_offset_to_va(off) or 0
            grown = make_unique_aob(img.code_blob, off, base_va=va)
            use_aob = aob
            if grown.get("unique") and grown.get("aob"):
                use_aob = grown["aob"]
                path = write_signature(out_dir, symbol, use_aob, resolve=resolve)
                n = grown["hits"]
            written_files.append(str(path))
            have.add(symbol)
            corpus_unique += 1
            entry.update({"status": "unique", "aob": use_aob, "hits": n, "file": str(path)})
            by_symbol.setdefault(symbol, []).append(entry)
            continue

        # multi-hit → tighten each site with correct VA
        best_unique = None
        best_any = None
        for off in offs[:8]:
            va = img.blob_offset_to_va(off) or 0
            grown = make_unique_aob(img.code_blob, off, base_va=va)
            grown = {**grown, "site_offset": off, "site_va": hex(va) if va else None}
            if best_any is None or (grown.get("hits", 99) < best_any.get("hits", 99)):
                best_any = grown
            if grown.get("unique"):
                best_unique = grown
                break
        if best_unique and best_unique.get("aob"):
            path = write_signature(out_dir, symbol, best_unique["aob"], resolve=resolve)
            written_files.append(str(path))
            have.add(symbol)
            corpus_tightened += 1
            entry.update(
                {
                    "status": "tightened",
                    "aob": best_unique["aob"],
                    "hits": 1,
                    "file": str(path),
                    "method": best_unique.get("method"),
                    "site_va": best_unique.get("site_va"),
                }
            )
        else:
            corpus_still_multi += 1
            entry.update(
                {
                    "status": "multi",
                    "best_hits": (best_any or {}).get("hits"),
                    "best_aob": (best_any or {}).get("aob"),
                }
            )
        by_symbol.setdefault(symbol, []).append(entry)

    steps.append(
        {
            "step": "corpus_and_tighten",
            "unique_exports": corpus_unique,
            "tightened_exports": corpus_tightened,
            "misses": corpus_miss,
            "still_multi": corpus_still_multi,
            "by_symbol": {k: v for k, v in by_symbol.items()},
        }
    )

    have = _stem_written(out_dir)
    missing = [s for s in REQUIRED_SIGS if s not in have]

    # Optional log-driven failed list
    failed_from_log: list[str] = []
    if log_path and Path(log_path).is_file():
        sr = parse_ue4ss_log_file(log_path)
        failed_from_log = list(sr.failed)

    return {
        "ok": len(missing) == 0,
        "exe": str(exe_path),
        "out_dir": str(out_dir),
        "exported_stems": sorted(have),
        "exported_count": len(have),
        "required": REQUIRED_SIGS,
        "missing_required": missing,
        "failed_in_log": failed_from_log,
        "written_files": written_files,
        "steps": steps,
        "hint": (
            "All REQUIRED_SIGS exported — drop out_dir next to UE4SS.dll as UE4SS_Signatures."
            if not missing
            else "Still missing required symbols; need RE VA → suggest, or MemberVariableLayout "
            "if scan already Found most (MCD-style)."
        ),
    }
