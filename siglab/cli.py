#!/usr/bin/env python3
"""CLI for unreal signature lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# allow `python -m siglab.cli` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siglab import engine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="UE4SS signature lab (PE + Capstone AOB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="PE summary")
    s.add_argument("exe")

    s = sub.add_parser("scan", help="Scan AOB in .text")
    s.add_argument("exe")
    s.add_argument("pattern", help='e.g. "48 8B 05 ?? ?? ?? ??"')

    s = sub.add_parser("disasm", help="Disassemble at VA")
    s.add_argument("exe")
    s.add_argument("va", help="0x... virtual address")
    s.add_argument("-n", type=int, default=24)

    s = sub.add_parser("corpus", help="Run seed AOB corpus against exe")
    s.add_argument("exe")
    s.add_argument("-o", "--out", help="write JSON report")

    s = sub.add_parser("log", help="Parse UE4SS.log Found/Failed")
    s.add_argument("log")

    s = sub.add_parser("suggest", help="AOB from known VA (from your RE)")
    s.add_argument("exe")
    s.add_argument("va")

    s = sub.add_parser("export-corpus", help="Write unique corpus hits as UE4SS_Signatures")
    s.add_argument("exe")
    s.add_argument("out_dir")

    s = sub.add_parser("export", help="Write one signature lua")
    s.add_argument("out_dir")
    s.add_argument("name")
    s.add_argument("aob")
    s.add_argument("--resolve", default="direct", choices=["direct", "lea_rip"])

    s = sub.add_parser("report", help="Full corpus + optional log")
    s.add_argument("exe")
    s.add_argument("--log", default="")
    s.add_argument("-o", "--out", default="")

    s = sub.add_parser(
        "recover",
        help="From successful UE4SS.log + exe: unique AOBs → UE4SS_Signatures",
    )
    s.add_argument("exe")
    s.add_argument("log")
    s.add_argument("out_dir")

    s = sub.add_parser(
        "solve",
        help="One-shot: recover log + corpus + auto-tighten multi-hits → UE4SS_Signatures",
    )
    s.add_argument("exe")
    s.add_argument("out_dir")
    s.add_argument("--log", default="", help="optional UE4SS.log (Found + multi-hit)")

    args = p.parse_args(argv)

    def dump(obj: dict) -> None:
        print(json.dumps(obj, indent=2))

    if args.cmd == "info":
        dump(engine.analyze_image(args.exe))
    elif args.cmd == "scan":
        dump(engine.scan_aob(args.exe, args.pattern))
    elif args.cmd == "disasm":
        dump(engine.disasm_at(args.exe, args.va, count=args.n))
    elif args.cmd == "corpus":
        r = engine.run_corpus(args.exe)
        if args.out:
            engine.save_report(r, args.out)
        dump(r)
    elif args.cmd == "log":
        dump(engine.parse_log(args.log))
    elif args.cmd == "suggest":
        dump(engine.suggest_aob_from_va(args.exe, args.va))
    elif args.cmd == "export-corpus":
        dump(engine.export_unique_from_corpus(args.exe, args.out_dir))
    elif args.cmd == "export":
        dump(engine.export_signature(args.out_dir, args.name, args.aob, resolve=args.resolve))
    elif args.cmd == "report":
        r = engine.full_report(args.exe, args.log or None)
        if args.out:
            engine.save_report(r, args.out)
        dump(r)
    elif args.cmd == "recover":
        dump(engine.recover_signatures_from_log(args.exe, args.log, args.out_dir))
    elif args.cmd == "solve":
        dump(engine.solve_signatures(args.exe, args.out_dir, args.log or None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
