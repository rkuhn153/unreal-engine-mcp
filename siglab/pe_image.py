"""Load a PE and map file offsets ↔ virtual addresses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pefile


@dataclass
class Section:
    name: str
    va: int
    vsize: int
    raw: int
    rawsize: int
    data: bytes


@dataclass
class PeImage:
    path: Path
    pe: pefile.PE
    image_base: int
    sections: list[Section] = field(default_factory=list)
    # Concatenated executable-ish view for scanning (code sections)
    code_blob: bytes = b""
    code_base_va: int = 0
    # Map from code_blob offset → VA
    _code_ranges: list[tuple[int, int, int]] = field(default_factory=list)  # blob_off, va, size

    @classmethod
    def load(cls, path: str | Path) -> "PeImage":
        path = Path(path)
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"],
            ]
        )
        image_base = pe.OPTIONAL_HEADER.ImageBase
        sections: list[Section] = []
        code_parts: list[bytes] = []
        code_ranges: list[tuple[int, int, int]] = []
        blob_off = 0
        code_base = None

        for s in pe.sections:
            name = s.Name.rstrip(b"\x00").decode("utf-8", errors="replace")
            data = s.get_data()
            sec = Section(
                name=name,
                va=s.VirtualAddress,
                vsize=s.Misc_VirtualSize,
                raw=s.PointerToRawData,
                rawsize=s.SizeOfRawData,
                data=data,
            )
            sections.append(sec)
            # Scan .text / executable sections
            chars = s.Characteristics
            executable = bool(chars & 0x20000000)  # IMAGE_SCN_MEM_EXECUTE
            if executable or name.lower() in (".text", "code", "pagetext"):
                if code_base is None:
                    code_base = image_base + s.VirtualAddress
                code_ranges.append((blob_off, image_base + s.VirtualAddress, len(data)))
                code_parts.append(data)
                blob_off += len(data)

        return cls(
            path=path,
            pe=pe,
            image_base=image_base,
            sections=sections,
            code_blob=b"".join(code_parts),
            code_base_va=code_base or image_base,
            _code_ranges=code_ranges,
        )

    def va_to_blob_offset(self, va: int) -> int | None:
        for blob_off, sec_va, size in self._code_ranges:
            if sec_va <= va < sec_va + size:
                return blob_off + (va - sec_va)
        return None

    def blob_offset_to_va(self, off: int) -> int | None:
        for blob_off, sec_va, size in self._code_ranges:
            if blob_off <= off < blob_off + size:
                return sec_va + (off - blob_off)
        return None

    def read_va(self, va: int, size: int) -> bytes | None:
        rva = va - self.image_base
        try:
            return self.pe.get_data(rva, size)
        except Exception:
            return None

    def strings(self, min_len: int = 6, limit: int = 5000) -> list[tuple[int, str]]:
        """ASCII strings with approximate VA (from full image raw)."""
        out: list[tuple[int, str]] = []
        raw = self.pe.__data__
        i = 0
        n = len(raw)
        while i < n and len(out) < limit:
            if 32 <= raw[i] < 127:
                j = i
                while j < n and 32 <= raw[j] < 127:
                    j += 1
                if j - i >= min_len:
                    s = raw[i:j].decode("ascii", errors="ignore")
                    # best-effort VA
                    try:
                        rva = self.pe.get_rva_from_offset(i)
                        va = self.image_base + rva
                    except Exception:
                        va = 0
                    out.append((va, s))
                i = j + 1
            else:
                i += 1
        return out

    def summary(self) -> dict:
        return {
            "path": str(self.path),
            "image_base": hex(self.image_base),
            "code_size": len(self.code_blob),
            "sections": [
                {"name": s.name, "va": hex(self.image_base + s.va), "size": len(s.data)}
                for s in self.sections
            ],
            "machine": hex(self.pe.FILE_HEADER.Machine),
        }
