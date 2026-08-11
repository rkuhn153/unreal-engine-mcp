"""Discover running / live UnrealEngineMCP IPC targets on this machine."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SEARCH_ROOTS = [
    Path(r"D:\SteamLibrary\steamapps\common"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common"),
    Path(r"C:\Program Files\Steam\steamapps\common"),
    Path(r"D:\Program Files\EpicGames"),
    Path(r"C:\Program Files\Epic Games"),
    Path(r"D:\Epic Games"),
    Path(r"C:\Games"),
    Path(r"D:\Games"),
]


@dataclass
class UnrealGameTarget:
    """One Unreal game with (or ready for) UnrealEngineMCP IPC."""

    game_name: str
    ipc_dir: Path
    exe_path: Path | None = None
    pid: int | None = None
    process_name: str | None = None
    heartbeat_age_sec: float | None = None
    heartbeat: dict[str, Any] | None = None
    alive: bool = False
    has_mod: bool = False
    source: str = ""  # process | heartbeat_scan

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_name": self.game_name,
            "ipc_dir": str(self.ipc_dir),
            "exe_path": str(self.exe_path) if self.exe_path else None,
            "pid": self.pid,
            "process_name": self.process_name,
            "alive": self.alive,
            "heartbeat_age_sec": self.heartbeat_age_sec,
            "has_mod": self.has_mod,
            "source": self.source,
            "protocol": (self.heartbeat or {}).get("protocol"),
            "mod": (self.heartbeat or {}).get("mod"),
            "ready": (self.heartbeat or {}).get("ready"),
        }


def _read_heartbeat(ipc_dir: Path, max_age: float = 8.0) -> tuple[bool, float | None, dict[str, Any] | None]:
    hb_path = ipc_dir / "heartbeat.json"
    if not hb_path.is_file():
        return False, None, None
    try:
        age = time.time() - hb_path.stat().st_mtime
        data = json.loads(hb_path.read_text(encoding="utf-8"))
        alive = age <= max_age and bool(data.get("ok", True))
        return alive, age, data
    except (OSError, json.JSONDecodeError, TypeError):
        return False, None, None


def _game_name_from_paths(exe: Path | None, ipc_dir: Path) -> str:
    if exe is not None:
        # .../GameName/Binaries/Win64/Foo-Win64-Shipping.exe
        # or .../GameName/Foo/Binaries/Win64/...
        parts = exe.parts
        for i, p in enumerate(parts):
            if p.lower() == "binaries" and i > 0:
                return parts[i - 1]
        return exe.stem.replace("-Win64-Shipping", "").replace("Win64-Shipping", "")
    # .../Win64/UnrealEngineMCP_IPC
    parent = ipc_dir.parent  # Win64
    if parent.name.lower() == "win64" and parent.parent.name.lower() == "binaries":
        return parent.parent.parent.name
    return ipc_dir.parent.name


def _shipping_exes(win64: Path) -> list[Path]:
    return list(win64.glob("*-Win64-Shipping.exe")) + list(win64.glob("*Win64-Shipping.exe"))


def _win64_has_mod(win64: Path) -> bool:
    """True only for real UnrealEngineMCP / UE4SS installs — not bare Windows dwmapi.dll."""
    return (
        (win64 / "Mods" / "UnrealEngineMCP").is_dir()
        or (win64 / "ue4ss" / "Mods" / "UnrealEngineMCP").is_dir()
        or (win64 / "UE4SS.dll").is_file()
        or (win64 / "ue4ss" / "UE4SS.dll").is_file()
    )


def _iter_running_exes() -> list[tuple[int, str, Path]]:
    """Return (pid, name, exe_path) for processes with an executable path."""
    # Prefer psutil if present; else PowerShell CIM.
    try:
        import psutil  # type: ignore

        out: list[tuple[int, str, Path]] = []
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                exe = proc.info.get("exe")
                if not exe:
                    continue
                path = Path(exe)
                if path.is_file():
                    out.append((int(proc.info["pid"]), str(proc.info.get("name") or path.name), path))
            except (psutil.Error, OSError, TypeError, ValueError):
                continue
        return out
    except ImportError:
        pass

    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath } | "
        "Select-Object ProcessId, Name, ExecutablePath | "
        "ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError, TimeoutError):
        return []

    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for row in data:
        try:
            exe = row.get("ExecutablePath") or ""
            if not exe:
                continue
            path = Path(exe)
            if not path.is_file():
                continue
            out.append((int(row["ProcessId"]), str(row.get("Name") or path.name), path))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _looks_like_unreal_win64(win64: Path) -> bool:
    if not win64.is_dir():
        return False
    shipping = _shipping_exes(win64)
    has_mod = _win64_has_mod(win64)
    has_ipc = (win64 / "UnrealEngineMCP_IPC").is_dir()
    has_ue4ss_dir = (win64 / "ue4ss").is_dir()
    # Require shipping binary and (mod or ipc or ue4ss) — never match System32 via dwmapi alone
    if shipping and (has_mod or has_ipc or has_ue4ss_dir):
        return True
    if has_mod and has_ipc:
        return True
    return False


def discover_from_processes(max_hb_age: float = 8.0) -> list[UnrealGameTarget]:
    found: dict[str, UnrealGameTarget] = {}
    for pid, name, exe in _iter_running_exes():
        # Skip obvious non-game system paths
        exe_s = str(exe).lower()
        if "\\windows\\" in exe_s or exe_s.startswith("c:\\windows"):
            continue

        win64 = exe.parent
        # Shipping often lives in Binaries/Win64
        if not _looks_like_unreal_win64(win64):
            alt = win64 / "Binaries" / "Win64"
            if _looks_like_unreal_win64(alt):
                win64 = alt
            else:
                # Running shipping exe whose Win64 has live IPC even if mod folder renamed
                ipc_try = win64 / "UnrealEngineMCP_IPC"
                alive_try, _, _ = _read_heartbeat(ipc_try, max_hb_age)
                if not (alive_try and _shipping_exes(win64)):
                    continue

        ipc = win64 / "UnrealEngineMCP_IPC"
        alive, age, hb = _read_heartbeat(ipc, max_hb_age)
        has_mod = _win64_has_mod(win64)
        if not alive and not has_mod:
            continue

        key = str(ipc.resolve()) if ipc.exists() else str(win64.resolve())
        tgt = UnrealGameTarget(
            game_name=_game_name_from_paths(exe, ipc),
            ipc_dir=ipc,
            exe_path=exe,
            pid=pid,
            process_name=name,
            heartbeat_age_sec=age,
            heartbeat=hb,
            alive=alive,
            has_mod=has_mod,
            source="process",
        )
        prev = found.get(key)
        if prev is None or (tgt.alive and not prev.alive):
            found[key] = tgt
    return list(found.values())


def discover_from_heartbeat_scan(
    roots: list[Path] | None = None,
    max_hb_age: float = 8.0,
) -> list[UnrealGameTarget]:
    """Find IPC dirs with recent heartbeats under common install roots."""
    roots = roots or DEFAULT_SEARCH_ROOTS
    extra = os.environ.get("UNREAL_MCP_SEARCH_ROOTS", "").strip()
    if extra:
        roots = list(roots) + [Path(p.strip()) for p in extra.split(";") if p.strip()]

    found: list[UnrealGameTarget] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        # Cap depth: game folders only then known layout
        try:
            # Prefer shallow patterns first
            patterns = [
                root.glob("*/Binaries/Win64/UnrealEngineMCP_IPC/heartbeat.json"),
                root.glob("*/*/Binaries/Win64/UnrealEngineMCP_IPC/heartbeat.json"),
                root.glob("*/*/*/Binaries/Win64/UnrealEngineMCP_IPC/heartbeat.json"),
            ]
            hb_files: list[Path] = []
            for it in patterns:
                hb_files.extend(it)
        except OSError:
            continue

        for hb in hb_files:
            ipc = hb.parent
            key = str(ipc.resolve())
            if key in seen:
                continue
            seen.add(key)
            alive, age, data = _read_heartbeat(ipc, max_hb_age)
            if not alive:
                continue
            win64 = ipc.parent
            found.append(
                UnrealGameTarget(
                    game_name=_game_name_from_paths(None, ipc),
                    ipc_dir=ipc,
                    exe_path=None,
                    pid=None,
                    process_name=None,
                    heartbeat_age_sec=age,
                    heartbeat=data,
                    alive=True,
                    has_mod=_win64_has_mod(win64),
                    source="heartbeat_scan",
                )
            )
    return found


def discover_unreal_games(max_hb_age: float = 8.0) -> list[UnrealGameTarget]:
    """Merge process + heartbeat discovery; alive first, then by name."""
    by_ipc: dict[str, UnrealGameTarget] = {}
    for tgt in discover_from_processes(max_hb_age) + discover_from_heartbeat_scan(max_hb_age=max_hb_age):
        key = str(tgt.ipc_dir.resolve()) if tgt.ipc_dir.exists() else str(tgt.ipc_dir)
        prev = by_ipc.get(key)
        if prev is None:
            by_ipc[key] = tgt
            continue
        # Merge: keep process info if scan found heartbeat
        if tgt.alive and not prev.alive:
            by_ipc[key] = tgt
        elif tgt.pid and not prev.pid:
            prev.pid = tgt.pid
            prev.process_name = tgt.process_name
            prev.exe_path = tgt.exe_path or prev.exe_path
            prev.source = "process+scan"
        elif tgt.alive and prev.alive and tgt.heartbeat_age_sec is not None:
            if prev.heartbeat_age_sec is None or tgt.heartbeat_age_sec < prev.heartbeat_age_sec:
                # fresher hb, keep process fields
                tgt.pid = tgt.pid or prev.pid
                tgt.process_name = tgt.process_name or prev.process_name
                tgt.exe_path = tgt.exe_path or prev.exe_path
                by_ipc[key] = tgt

    games = list(by_ipc.values())
    games.sort(key=lambda g: (not g.alive, g.game_name.lower(), str(g.ipc_dir)))
    return games


def pick_default_ipc(
    explicit: str = "",
    game_dir: str = "",
    max_hb_age: float = 8.0,
) -> Path:
    """Resolve startup IPC: explicit > env > best live game > legacy fallbacks."""
    if explicit.strip():
        return Path(explicit).expanduser().resolve()

    env = os.environ.get("UNREAL_MCP_IPC_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()

    live = [g for g in discover_unreal_games(max_hb_age) if g.alive]
    if live:
        return live[0].ipc_dir.resolve()

    # Optional single game dir walk
    if game_dir.strip():
        root = Path(game_dir).expanduser().resolve()
        if root.is_dir():
            for p in root.rglob("UnrealEngineMCP_IPC"):
                if p.is_dir() and (p / "heartbeat.json").is_file():
                    return p.resolve()
            for win64 in root.rglob("Win64"):
                if win64.is_dir() and _looks_like_unreal_win64(win64):
                    return (win64 / "UnrealEngineMCP_IPC").resolve()

    # Empty placeholder next to translator (will fail alive checks until select)
    return (Path.cwd() / "UnrealEngineMCP_IPC").resolve()
