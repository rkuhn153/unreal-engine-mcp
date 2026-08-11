"""File-IPC client for the UnrealEngineMCP UE4SS Lua bridge."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BridgeError(RuntimeError):
    """Raised when the in-game Unreal bridge cannot complete a request."""


@dataclass
class BridgeClient:
    """Talks to UnrealEngineMCP_IPC next to the shipping Win64 binary."""

    ipc_dir: Path
    timeout_seconds: float = 15.0
    poll_interval: float = 0.05

    def __post_init__(self) -> None:
        self.ipc_dir = Path(self.ipc_dir)
        # Do not force-create IPC on non-game paths; retarget/select will mkdir when needed.
        if self.ipc_dir.parent.is_dir() and self.ipc_dir.name == "UnrealEngineMCP_IPC":
            try:
                self.ipc_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def retarget(self, ipc_dir: Path | str, *, ensure_dir: bool = True) -> Path:
        """Point this client at another game's UnrealEngineMCP_IPC folder."""
        self.ipc_dir = Path(ipc_dir).expanduser().resolve()
        if ensure_dir:
            try:
                self.ipc_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        return self.ipc_dir

    @property
    def request_path(self) -> Path:
        return self.ipc_dir / "request.json"

    @property
    def request_flag(self) -> Path:
        return self.ipc_dir / "request.flag"

    @property
    def response_path(self) -> Path:
        return self.ipc_dir / "response.json"

    @property
    def response_flag(self) -> Path:
        return self.ipc_dir / "response.flag"

    @property
    def heartbeat_path(self) -> Path:
        return self.ipc_dir / "heartbeat.json"

    def is_alive(self, max_age_seconds: float = 5.0) -> bool:
        if not self.heartbeat_path.is_file():
            return False
        try:
            age = time.time() - self.heartbeat_path.stat().st_mtime
            if age > max_age_seconds:
                return False
            data = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
            return bool(data.get("ok", True))
        except (OSError, json.JSONDecodeError):
            return False

    def read_heartbeat(self) -> dict[str, Any]:
        if not self.heartbeat_path.is_file():
            raise BridgeError(
                "No heartbeat from UnrealEngineMCP bridge. "
                "Is the game running with UE4SS and the UnrealEngineMCP mod enabled?"
            )
        try:
            return json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(f"Failed to read heartbeat: {exc}") from exc

    def _clear_response(self) -> None:
        for path in (self.response_flag, self.response_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _abandon_request(self) -> None:
        """Clear request + response after client-side timeout so the pump is not left busy."""
        for path in (
            self.request_flag,
            self.request_path,
            self.response_flag,
            self.response_path,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def call(
        self,
        cmd: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        timeout = self.timeout_seconds if timeout is None else timeout
        # Require a recent heartbeat so we do not queue work against a dead pump.
        # Brief wait: map loads re-arm the delay chain and can leave a short gap.
        alive_deadline = time.time() + min(6.0, timeout)
        while not self.is_alive(max_age_seconds=8.0) and time.time() < alive_deadline:
            time.sleep(0.2)
        if not self.is_alive(max_age_seconds=8.0):
            if not self.heartbeat_path.is_file():
                raise BridgeError(
                    "Cannot reach UnrealEngineMCP bridge (no heartbeat). "
                    "Launch the game with UE4SS + UnrealEngineMCP installed."
                )
            raise BridgeError(
                "UnrealEngineMCP heartbeat is stale (bridge pump not running). "
                "Press Ctrl+F9 in-game to revive the pump, finish loading a level, "
                "or check ue4ss/UE4SS.log for mod errors."
            )

        req_id = str(uuid.uuid4())
        payload = {
            "id": req_id,
            "cmd": cmd,
            "params": params or {},
            "ts": time.time(),
        }

        self._clear_response()
        # Write request body first, then flag (bridge waits on flag).
        self.request_path.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        self.request_flag.write_text("1", encoding="utf-8")

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.response_flag.is_file():
                try:
                    raw = self.response_path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                except (OSError, json.JSONDecodeError) as exc:
                    time.sleep(self.poll_interval)
                    continue
                finally:
                    # Always attempt cleanup once flag appears and JSON is readable.
                    pass

                try:
                    self.response_flag.unlink(missing_ok=True)
                except OSError:
                    pass

                if not isinstance(data, dict):
                    raise BridgeError("Bridge returned non-object JSON.")
                if data.get("id") not in (None, req_id):
                    # Stale response from a previous call; wait for ours.
                    self._clear_response()
                    time.sleep(self.poll_interval)
                    continue
                return data

            time.sleep(self.poll_interval)

        # Critical: drop in-flight request so a recovering game does not execute a
        # timed-out cmd later while the client already moved on (stuck busy / BFBB).
        self._abandon_request()
        raise BridgeError(
            f"Bridge timed out after {timeout:.1f}s waiting for command '{cmd}'. "
            "Game may be paused, stuck on a menu, or the mod is not polling. "
            "Stale request flag cleared; try again or Ctrl+F9 if heartbeat is dead."
        )

    def call_ok(self, cmd: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = self.call(cmd, params, **kwargs)
        if data.get("ok") is False:
            raise BridgeError(data.get("error") or f"Command '{cmd}' failed.")
        return data
