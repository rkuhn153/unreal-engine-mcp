"""Parse UE4SS.log for Found / Failed signature lines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScanResult:
    found: dict[str, str] = field(default_factory=dict)  # name -> address hex
    failed: list[str] = field(default_factory=list)
    multi_hit: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    raw_hits: list[str] = field(default_factory=list)


# Names may contain '::' (FName::ToString) — stop before ': 0x'
_FOUND = re.compile(
    r"(?:\[PS\]\s+)?Found\s+(.+?):\s*(0x[0-9A-Fa-f]+)\s*$",
    re.I,
)
_FAILED = re.compile(
    r"(?:\[PS\]\s+)?Failed to find\s+(.+?)(?:\s*:|\s*$)",
    re.I,
)
_MULTI = re.compile(
    r"Failed to find\s+([^:]+):\s*.*found\s+(\d+)\s+unique values\s*\[([^\]]+)\]",
    re.I,
)
_ENGINE = re.compile(r"Using engine version:\s*([\d.]+)", re.I)


def parse_ue4ss_log(text: str) -> ScanResult:
    r = ScanResult()
    for line in text.splitlines():
        m = _ENGINE.search(line)
        if m:
            r.notes.append(f"engine_version={m.group(1)}")
        m = _MULTI.search(line)
        if m:
            name = m.group(1).strip()
            vals = [v.strip() for v in m.group(3).split(",")]
            r.multi_hit[name] = vals
            r.failed.append(name)
            r.raw_hits.append(line.strip())
            continue
        m = _FOUND.search(line)
        if m:
            r.found[m.group(1).strip()] = m.group(2)
            r.raw_hits.append(line.strip())
            continue
        m = _FAILED.search(line)
        if m:
            name = m.group(1).strip()
            # strip trailing noise
            name = name.split(":")[0].strip()
            if name not in r.failed:
                r.failed.append(name)
            r.raw_hits.append(line.strip())
    return r


def parse_ue4ss_log_file(path: str | Path) -> ScanResult:
    p = Path(path)
    # UE4SS logs are sometimes UTF-16
    data = p.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
    return parse_ue4ss_log(text)


def result_summary(r: ScanResult) -> dict:
    return {
        "found": r.found,
        "failed": r.failed,
        "multi_hit": r.multi_hit,
        "notes": r.notes,
        "found_count": len(r.found),
        "failed_count": len(r.failed),
    }
