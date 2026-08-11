"""Array-of-bytes pattern compile and scan."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Pattern:
    raw: str
    needle: bytes
    mask: bytes  # 0xFF = fixed, 0x00 = wildcard

    @classmethod
    def parse(cls, text: str) -> "Pattern":
        """Parse '48 8B ?? 90' style patterns (space-separated)."""
        text = text.strip()
        if not text:
            raise ValueError("empty pattern")
        # normalize continuous hex with ?? into tokens
        if " " not in text and ("?" in text or len(text) >= 2):
            s = text
            parts: list[str] = []
            i = 0
            while i < len(s):
                if s[i] == "?":
                    parts.append("??")
                    i += 2 if i + 1 < len(s) and s[i + 1] == "?" else 1
                else:
                    if i + 1 >= len(s):
                        raise ValueError(f"odd hex length in {text!r}")
                    parts.append(s[i : i + 2])
                    i += 2
        else:
            parts = text.split()

        needle = bytearray()
        mask = bytearray()
        for p in parts:
            p = p.strip()
            if p in ("?", "??", "**", "*"):
                needle.append(0)
                mask.append(0)
            else:
                needle.append(int(p, 16))
                mask.append(0xFF)
        if not needle:
            raise ValueError(f"could not parse pattern: {text!r}")
        return cls(raw=" ".join(f"{b:02X}" if m else "??" for b, m in zip(needle, mask)), needle=bytes(needle), mask=bytes(mask))


def scan(blob: bytes, pattern: Pattern, max_hits: int = 64) -> list[int]:
    """Return offsets in blob matching pattern."""
    n = pattern.needle
    m = pattern.mask
    plen = len(n)
    if plen == 0 or plen > len(blob):
        return []
    hits: list[int] = []
    limit = len(blob) - plen + 1
    first = n[0]
    first_masked = m[0] != 0
    for i in range(limit):
        if first_masked and blob[i] != first:
            continue
        ok = True
        for j in range(plen):
            if m[j] and blob[i + j] != n[j]:
                ok = False
                break
        if ok:
            hits.append(i)
            if len(hits) >= max_hits:
                break
    return hits


def scan_pattern_str(blob: bytes, pattern: str, max_hits: int = 64) -> list[int]:
    return scan(blob, Pattern.parse(pattern), max_hits=max_hits)


def wildcard_mask_for_bytes(data: bytes, base_va: int = 0) -> bytes:
    """
    Build a mask for `data` (0xFF=fixed, 0x00=wildcard) using Capstone.
    Wildcards: E8/E9 rel32, RIP-relative LEA/MOV disp32, and similar.
    Falls back to fixed bytes if Capstone unavailable or decode fails.
    """
    mask = bytearray([0xFF] * len(data))
    try:
        from capstone import CS_ARCH_X86, CS_MODE_64, Cs
    except ImportError:
        return bytes(mask)

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = False
    for insn in md.disasm(data, base_va):
        off = insn.address - base_va
        raw = insn.bytes
        end = off + len(raw)
        if end > len(data):
            break
        b = list(raw)
        # call/jmp rel32
        if len(b) >= 5 and b[0] in (0xE8, 0xE9):
            for i in range(1, 5):
                mask[off + i] = 0
        # REX + lea/mov r64, [rip+disp32]  (48/4C 8D/8B xx disp32)
        elif (
            len(b) >= 7
            and b[0] in (0x48, 0x4C, 0x4D, 0x49)
            and b[1] in (0x8D, 0x8B)
            and (b[2] & 0xC7) == 0x05  # mod=00 rm=101 → rip+disp32 form (common)
        ):
            for i in range(3, 7):
                mask[off + i] = 0
        # 8B 05 / 8D 05 without REX (rare in x64 but exists)
        elif len(b) >= 6 and b[0] in (0x8B, 0x8D) and (b[1] & 0xC7) == 0x05:
            for i in range(2, 6):
                mask[off + i] = 0
    return bytes(mask)


def bytes_to_aob(data: bytes, mask: bytes | None = None) -> str:
    parts: list[str] = []
    for i, b in enumerate(data):
        if mask is not None and i < len(mask) and mask[i] == 0:
            parts.append("??")
        else:
            parts.append(f"{b:02X}")
    return " ".join(parts)


def make_unique_aob(
    blob: bytes,
    offset: int,
    *,
    base_va: int = 0,
    min_len: int = 10,
    max_len: int = 48,
    prefer_wildcard: bool = True,
) -> dict:
    """
    Grow a window at `offset` until the (optionally wildcarded) AOB is unique
    in `blob`. Also tries a few bytes of preamble if pure grow fails.

    Returns dict: aob, hits, unique, length, method
    """
    if offset < 0 or offset >= len(blob):
        return {"aob": None, "hits": 0, "unique": False, "error": "bad offset"}

    def try_window(start: int, length: int) -> tuple[str, int] | None:
        if start < 0 or start + length > len(blob):
            return None
        chunk = blob[start : start + length]
        va = base_va + (start - offset) if base_va else 0
        if prefer_wildcard:
            mask = wildcard_mask_for_bytes(chunk, va)
            aob = bytes_to_aob(chunk, mask)
        else:
            aob = bytes_to_aob(chunk, None)
        n = len(scan(blob, Pattern.parse(aob), max_hits=8))
        return aob, n

    best: dict | None = None
    for length in range(min_len, max_len + 1):
        got = try_window(offset, length)
        if not got:
            break
        aob, n = got
        if best is None or n < best["hits"] or (n == best["hits"] and length < best["length"]):
            best = {"aob": aob, "hits": n, "unique": n == 1, "length": length, "method": "grow"}
        if n == 1:
            return best

    # Preamble: include up to 16 bytes before the site (helps multi-hit prologues)
    for back in range(1, 17):
        start = offset - back
        if start < 0:
            break
        for length in range(min_len + back, max_len + back + 1):
            got = try_window(start, length)
            if not got:
                break
            aob, n = got
            if best is None or n < best["hits"]:
                best = {
                    "aob": aob,
                    "hits": n,
                    "unique": n == 1,
                    "length": length,
                    "method": f"preamble_{back}",
                }
            if n == 1:
                return best

    # Strict (no wildcards) last resort grow
    for length in range(min_len, max_len + 1):
        chunk = blob[offset : offset + length]
        if len(chunk) < length:
            break
        aob = bytes_to_aob(chunk, None)
        n = len(scan(blob, Pattern.parse(aob), max_hits=8))
        if best is None or n < best["hits"]:
            best = {
                "aob": aob,
                "hits": n,
                "unique": n == 1,
                "length": length,
                "method": "strict_grow",
            }
        if n == 1:
            return best

    if best is None:
        chunk = blob[offset : offset + min_len]
        return {
            "aob": bytes_to_aob(chunk),
            "hits": -1,
            "unique": False,
            "length": len(chunk),
            "method": "fallback",
        }
    return best


def tighten_at_hits(
    blob: bytes,
    seed_aob: str,
    *,
    base_va_for_offset0: int = 0,
    max_sites: int = 8,
) -> dict:
    """
    For a multi-hit seed AOB, try make_unique_aob at each hit site.
    Returns first unique success, else best fewest-hits attempt.
    """
    pat = Pattern.parse(seed_aob)
    offs = scan(blob, pat, max_hits=max_sites)
    attempts: list[dict] = []
    for off in offs:
        r = make_unique_aob(
            blob,
            off,
            base_va=base_va_for_offset0 + off if base_va_for_offset0 else 0,
        )
        r = {**r, "site_offset": off}
        attempts.append(r)
        if r.get("unique"):
            return {"ok": True, "chosen": r, "sites_tried": len(attempts), "attempts": attempts}
    attempts.sort(key=lambda x: (x.get("hits") if x.get("hits", 99) >= 0 else 99))
    return {
        "ok": False,
        "chosen": attempts[0] if attempts else None,
        "sites_tried": len(attempts),
        "attempts": attempts,
    }
