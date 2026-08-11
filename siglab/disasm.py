"""Capstone x64 disassembly helpers."""

from __future__ import annotations

from dataclasses import dataclass

from capstone import CS_ARCH_X86, CS_MODE_64, Cs


@dataclass
class Insn:
    address: int
    size: int
    mnemonic: str
    op_str: str
    bytes_hex: str

    def __str__(self) -> str:
        return f"0x{self.address:X}: {self.mnemonic} {self.op_str}"


_md: Cs | None = None


def _engine() -> Cs:
    global _md
    if _md is None:
        _md = Cs(CS_ARCH_X86, CS_MODE_64)
        _md.detail = False
    return _md


def disassemble(data: bytes, base_va: int, count: int = 32) -> list[Insn]:
    """Disassemble up to count instructions starting at base_va."""
    md = _engine()
    out: list[Insn] = []
    for i in md.disasm(data, base_va):
        out.append(
            Insn(
                address=i.address,
                size=i.size,
                mnemonic=i.mnemonic,
                op_str=i.op_str,
                bytes_hex=i.bytes.hex(" "),
            )
        )
        if len(out) >= count:
            break
    return out


def disassemble_window(data: bytes, base_va: int, before: int = 0, after: int = 16) -> list[Insn]:
    """Disassemble from base_va for `after` instructions (before requires prior bytes)."""
    return disassemble(data, base_va, count=after)


def bytes_to_aob(data: bytes, wildcard_mask: bytes | None = None) -> str:
    parts: list[str] = []
    for i, b in enumerate(data):
        if wildcard_mask and i < len(wildcard_mask) and wildcard_mask[i] == 0:
            parts.append("??")
        else:
            parts.append(f"{b:02X}")
    return " ".join(parts)


def prologue_aob(data: bytes, max_len: int = 16, rip_wildcard: bool = True) -> str:
    """
    Build an AOB from instruction stream; optionally wildcard RIP-relative disp32
    (last 4 bytes of instructions that look like rel32 — heuristic).
    """
    md = _engine()
    parts: list[str] = []
    total = 0
    for insn in md.disasm(data, 0):
        raw = insn.bytes
        # Heuristic: E8/E9 rel32 or opcode with modrm RIP-relative (mod=00 rm=101)
        hex_parts: list[str] = []
        b = list(raw)
        if rip_wildcard and len(b) >= 5 and b[0] in (0xE8, 0xE9):
            hex_parts = [f"{b[0]:02X}", "??", "??", "??", "??"]
        elif rip_wildcard and len(b) >= 7 and b[0] in (0x48, 0x4C) and b[1] == 0x8D:
            # lea r64, [rip+disp32]  common 48 8D 05 xx xx xx xx
            hex_parts = [f"{x:02X}" for x in b[:3]] + ["??", "??", "??", "??"]
            if len(b) > 7:
                hex_parts += [f"{x:02X}" for x in b[7:]]
        else:
            hex_parts = [f"{x:02X}" for x in b]
        parts.extend(hex_parts)
        total += len(b)
        if total >= max_len:
            break
    # `parts` is one token per byte (or ??); cap to max_len tokens
    return " ".join(parts[:max_len])
