from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "translator"))
from bridge_client import BridgeClient

ipc = Path(
    r"D:\SteamLibrary\steamapps\common\SpongeBob SquarePants Battle for Bikini Bottom - Rehydrated"
    r"\Pineapple\Binaries\Win64\UnrealEngineMCP_IPC"
)
c = BridgeClient(ipc, timeout_seconds=30)

player = c.call("get_player")
if not isinstance(player, dict):
    player = json.loads(player)
full = (player.get("pawn") or {}).get("full_name")
if not full:
    print("No pawn — load into world first")
    sys.exit(2)
print("PAWN", full)

props: list[str] = []
offset = 0
for _ in range(50):
    r = c.call(
        "get_properties",
        {
            "full_name": full,
            "limit": 100,
            "offset": offset,
            "max_depth": 6,
            "bp_only": False,
            "include_values": False,
        },
    )
    if not isinstance(r, dict):
        r = json.loads(r)
    batch = r.get("properties") or []
    for p in batch:
        name = p.get("name") if isinstance(p, dict) else p
        if name and name not in props:
            props.append(name)
    print(
        f"props off={offset} got={len(batch)} trunc={r.get('truncated')} "
        f"budget={r.get('budget_hit')} unique={len(props)}"
    )
    if not batch or not r.get("truncated"):
        break
    offset = int(r.get("next_offset") or (offset + len(batch)))

funcs: list[str] = []
# BP-first full page, then engine-inclusive
for bp_only in (True, False):
    offset = 0
    for _ in range(80):
        r = c.call(
            "list_functions",
            {
                "full_name": full,
                "limit": 50,
                "offset": offset,
                "max_depth": 6 if bp_only else 4,
                "bp_only": bp_only,
            },
        )
        if not isinstance(r, dict):
            r = json.loads(r)
        batch = r.get("functions") or []
        new = 0
        for f in batch:
            name = f.get("name") if isinstance(f, dict) else f
            if name and name not in funcs:
                funcs.append(name)
                new += 1
        print(
            f"funcs bp_only={bp_only} off={offset} got={len(batch)} new={new} "
            f"trunc={r.get('truncated')} budget={r.get('budget_hit')} unique={len(funcs)} "
            f"has_offset={r.get('offset') is not None}"
        )
        if not batch or not r.get("truncated"):
            break
        # if offset ignored (old lua), break to avoid infinite loop
        if r.get("offset") is None and offset > 0:
            print("offset not supported on live bridge")
            break
        next_off = r.get("next_offset")
        if next_off is None:
            next_off = offset + len(batch)
        if int(next_off) <= offset:
            break
        offset = int(next_off)

out = Path(__file__).resolve().parents[1] / "spongebob_reflection_dump.json"
data = {
    "object": full,
    "class": "BP_Character_Spongebob_C",
    "property_count": len(props),
    "function_count": len(funcs),
    "properties": props,
    "functions": funcs,
}
out.write_text(json.dumps(data, indent=2), encoding="utf-8")
print("WROTE", out)
print("PROP_COUNT", len(props))
print("FUNC_COUNT", len(funcs))
