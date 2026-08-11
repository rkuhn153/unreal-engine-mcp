"""Offline unit tests for bridge client (no pytest tmp_path — local basetemp)."""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "translator"))

from bridge_client import BridgeClient, BridgeError  # noqa: E402

BASE = ROOT / ".test_tmp"


def _fresh(name: str) -> Path:
    p = BASE / name
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_call_roundtrip() -> None:
    tmp = _fresh("roundtrip")
    client = BridgeClient(ipc_dir=tmp, timeout_seconds=2.0, poll_interval=0.02)
    (tmp / "heartbeat.json").write_text(
        json.dumps({"ok": True, "ts": time.time()}), encoding="utf-8"
    )

    def responder() -> None:
        deadline = time.time() + 2
        while time.time() < deadline:
            if (tmp / "request.flag").is_file():
                req = json.loads((tmp / "request.json").read_text(encoding="utf-8"))
                (tmp / "request.flag").unlink(missing_ok=True)
                resp = {"id": req["id"], "ok": True, "pong": True, "cmd": req["cmd"]}
                (tmp / "response.json").write_text(json.dumps(resp), encoding="utf-8")
                (tmp / "response.flag").write_text("1", encoding="utf-8")
                return
            time.sleep(0.01)

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    out = client.call("ping")
    t.join(timeout=2)
    assert out["ok"] is True
    assert out["pong"] is True


def test_timeout_without_bridge() -> None:
    tmp = _fresh("timeout")
    client = BridgeClient(ipc_dir=tmp, timeout_seconds=0.3, poll_interval=0.05)
    try:
        client.call("ping")
        raise AssertionError("expected BridgeError")
    except BridgeError:
        pass


def test_call_ok_raises_on_error() -> None:
    tmp = _fresh("call_ok")
    client = BridgeClient(ipc_dir=tmp, timeout_seconds=2.0, poll_interval=0.02)
    (tmp / "heartbeat.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

    def responder() -> None:
        deadline = time.time() + 2
        while time.time() < deadline:
            if (tmp / "request.flag").is_file():
                req = json.loads((tmp / "request.json").read_text(encoding="utf-8"))
                (tmp / "request.flag").unlink(missing_ok=True)
                resp = {"id": req["id"], "ok": False, "error": "nope"}
                (tmp / "response.json").write_text(json.dumps(resp), encoding="utf-8")
                (tmp / "response.flag").write_text("1", encoding="utf-8")
                return
            time.sleep(0.01)

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    try:
        client.call_ok("status")
        raise AssertionError("expected BridgeError")
    except BridgeError as exc:
        assert "nope" in str(exc)
    t.join(timeout=2)


def main() -> int:
    failed = 0
    for name, fn in [
        ("test_call_roundtrip", test_call_roundtrip),
        ("test_timeout_without_bridge", test_timeout_without_bridge),
        ("test_call_ok_raises_on_error", test_call_ok_raises_on_error),
    ]:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
