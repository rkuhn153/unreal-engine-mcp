"""High-level signature lab operations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from siglab.aob import Pattern, scan
from siglab.corpus import all_corpus
from siglab.disasm import disassemble, prologue_aob
from siglab.export_ue4ss import write_signature
from siglab.log_parse import parse_ue4ss_log_file, result_summary
from siglab.pe_image import PeImage


@dataclass
class Hit:
    symbol: str
    aob: str
    resolve: str
    note: str
    count: int
    vas: list[str] = field(default_factory=list)
    disasm_preview: list[str] = field(default_factory=list)


def load_image(path: str | Path) -> PeImage:
    return PeImage.load(path)


def analyze_image(path: str | Path) -> dict:
    img = PeImage.load(path)
    return img.summary()


def scan_aob(path: str | Path, pattern: str, max_hits: int = 32) -> dict:
    img = PeImage.load(path)
    pat = Pattern.parse(pattern)
    offs = scan(img.code_blob, pat, max_hits=max_hits)
    hits = []
    for off in offs:
        va = img.blob_offset_to_va(off)
        if va is None:
            continue
        window = img.code_blob[off : off + 64]
        insns = disassemble(window, va, count=8)
        hits.append(
            {
                "va": hex(va),
                "blob_offset": off,
                "disasm": [str(i) for i in insns],
            }
        )
    return {
        "pattern": pat.raw,
        "hit_count": len(hits),
        "unique": len(hits) == 1,
        "hits": hits,
    }


def disasm_at(path: str | Path, va_hex: str, count: int = 24) -> dict:
    img = PeImage.load(path)
    va = int(va_hex, 16) if isinstance(va_hex, str) else int(va_hex)
    data = img.read_va(va, 128)
    if not data:
        return {"error": f"cannot read VA {hex(va)}"}
    insns = disassemble(data, va, count=count)
    aob = prologue_aob(data, max_len=20)
    return {
        "va": hex(va),
        "aob_from_prologue": aob,
        "instructions": [
            {
                "address": hex(i.address),
                "bytes": i.bytes_hex,
                "text": f"{i.mnemonic} {i.op_str}",
            }
            for i in insns
        ],
    }


def run_corpus(path: str | Path, max_hits: int = 16) -> dict:
    img = PeImage.load(path)
    results: list[Hit] = []
    for symbol, aob, resolve, note in all_corpus():
        try:
            pat = Pattern.parse(aob)
        except ValueError:
            continue
        offs = scan(img.code_blob, pat, max_hits=max_hits)
        vas: list[str] = []
        preview: list[str] = []
        for off in offs[:3]:
            va = img.blob_offset_to_va(off)
            if va is None:
                continue
            vas.append(hex(va))
            window = img.code_blob[off : off + 48]
            preview.extend(str(i) for i in disassemble(window, va, count=3))
        results.append(
            Hit(
                symbol=symbol,
                aob=aob,
                resolve=resolve,
                note=note,
                count=len(offs),
                vas=vas,
                disasm_preview=preview[:6],
            )
        )

    unique = [asdict(h) for h in results if h.count == 1]
    multi = [asdict(h) for h in results if h.count > 1]
    miss = [asdict(h) for h in results if h.count == 0]
    return {
        "image": img.summary(),
        "unique_hits": unique,
        "multi_hits": multi,
        "misses": miss,
        "stats": {
            "unique": len(unique),
            "multi": len(multi),
            "miss": len(miss),
            "corpus_size": len(results),
        },
    }


def parse_log(path: str | Path) -> dict:
    return result_summary(parse_ue4ss_log_file(path))


def export_unique_from_corpus(
    exe_path: str | Path,
    out_dir: str | Path,
) -> dict:
    """Scan corpus; write UE4SS_Signatures for unique hits only."""
    report = run_corpus(exe_path)
    written: list[str] = []
    for h in report["unique_hits"]:
        p = write_signature(
            out_dir,
            h["symbol"],
            h["aob"],
            resolve=h["resolve"],
        )
        written.append(str(p))
    return {"written": written, "count": len(written), "report_stats": report["stats"]}


def suggest_aob_from_va(path: str | Path, va_hex: str, length: int = 20) -> dict:
    """User found a VA in their RE tool — make an AOB + disasm."""
    d = disasm_at(path, va_hex, count=12)
    if "error" in d:
        return d
    img = PeImage.load(path)
    va = int(va_hex, 16)
    data = img.read_va(va, 64) or b""
    return {
        **d,
        "suggested_aob": prologue_aob(data, max_len=length),
        "suggested_aob_strict": " ".join(f"{b:02X}" for b in data[:length]),
        "export_hint": "Use export_signature with resolve=direct or lea_rip if LEA [rip+disp]",
    }


def export_signature(
    out_dir: str | Path,
    name: str,
    aob: str,
    resolve: str = "direct",
) -> dict:
    p = write_signature(out_dir, name, aob, resolve=resolve)
    return {"path": str(p), "name": name, "aob": aob, "resolve": resolve}


def full_report(exe: str | Path, log: str | Path | None = None) -> dict:
    out: dict = {"exe": analyze_image(exe), "corpus": run_corpus(exe)}
    if log and Path(log).is_file():
        out["ue4ss_log"] = parse_log(log)
    return out


def save_report(report: dict, path: str | Path) -> str:
    path = Path(path)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return str(path)


def recover_signatures_from_log(
    exe_path: str | Path,
    log_path: str | Path,
    out_dir: str | Path,
) -> dict:
    """Freeze UE4SS Found addresses into unique AOBs + Lua exports."""
    from siglab.recover import recover_from_log

    return recover_from_log(exe_path, log_path, out_dir)


def solve_signatures(
    exe_path: str | Path,
    out_dir: str | Path,
    log_path: str | Path | None = None,
) -> dict:
    """
    Best-effort export of all required UE4SS signatures:
    log recovery + multi-hit disambiguation + corpus + auto-tighten.
    """
    from siglab.solve import solve

    return solve(exe_path, out_dir, log_path=log_path)
