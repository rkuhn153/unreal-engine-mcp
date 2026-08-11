import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "translator"))
from bridge_client import BridgeClient, BridgeError

IPC = Path(
    r"D:\SteamLibrary\steamapps\common"
    r"\SpongeBob SquarePants Battle for Bikini Bottom - Rehydrated"
    r"\Pineapple\Binaries\Win64\UnrealEngineMCP_IPC"
)
OUT = Path(__file__).resolve().parents[1] / "spongebob_properties.json"


def main() -> int:
    c = BridgeClient(ipc_dir=IPC, timeout_seconds=45)
    print("alive", c.is_alive(12), "hb_age_check")
    if not c.heartbeat_path.is_file():
        print("no heartbeat file")
        return 1

    # Wait for fresh heartbeat (pump alive)
    for i in range(60):
        if c.is_alive(8):
            break
        print(f"waiting for fresh heartbeat {i}...")
        time.sleep(1)
    else:
        print("bridge pump not alive — game may be hung; restart game")
        return 1

    fo = c.call("find_objects", {"class": "BP_Character_Spongebob_C", "limit": 3}, timeout=20)
    objs = fo.get("objects") or []
    if not objs:
        print("no BP_Character_Spongebob_C in world")
        return 2
    fn = objs[0].get("full_name")
    cs = objs[0].get("class_short") or "BP_Character_Spongebob_C"
    print("PAWN", fn)

    all_props: list[dict] = []
    offset = 0
    page_size = 50
    for page in range(20):
        try:
            r = c.call(
                "get_properties",
                {
                    "class": cs,
                    "full_name": fn,
                    "limit": page_size,
                    "offset": offset,
                    "max_depth": 5,
                    "include_values": False,
                },
                timeout=35,
            )
        except BridgeError as exc:
            print(f"page {page} error: {exc}")
            # fallback class-only once
            try:
                r = c.call(
                    "get_properties",
                    {
                        "class": cs,
                        "limit": page_size,
                        "offset": offset,
                        "max_depth": 5,
                    },
                    timeout=35,
                )
            except BridgeError as exc2:
                print(f"fallback failed: {exc2}")
                break

        if not r.get("ok"):
            print("FAIL", r)
            # if full_name resolve fails, class-only already tried above
            break

        props = r.get("properties") or []
        print(
            f"page {page} offset={offset} n={len(props)} "
            f"truncated={r.get('truncated')} next={r.get('next_offset')}"
        )
        all_props.extend(props)
        if not props or not r.get("truncated"):
            break
        offset = int(r.get("next_offset") or (offset + len(props)))
        time.sleep(0.35)

    seen: set[str] = set()
    unique: list[dict] = []
    for p in all_props:
        name = p.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(p)

    OUT.write_text(
        json.dumps(
            {
                "object": {"full_name": fn, "class_short": cs},
                "count": len(unique),
                "properties": unique,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("TOTAL", len(unique))
    print("WROTE", OUT)
    for i, p in enumerate(unique, 1):
        print(f"{i:3d}. {p.get('name')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
