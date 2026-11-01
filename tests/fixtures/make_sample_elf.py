"""Generate a minimal ELF32 with one global object symbol for tests."""

from __future__ import annotations

import struct
from pathlib import Path

# ELF32 little-endian layout (minimal):
# ehdr + .data (4 bytes) + .shstrtab + .strtab + .symtab + section headers

OUT = Path(__file__).with_name("sample.elf")


def main() -> None:
    # Section names
    shstr = b"\x00.data\x00.shstrtab\x00.strtab\x00.symtab\x00"
    # Symbol names: empty + EngineSpeed
    strtab = b"\x00EngineSpeed\x00"

    data_section = struct.pack("<I", 0x12345678)  # 4 bytes payload

    # Symbol: undefined dummy + EngineSpeed OBJECT
    # Elf32_Sym: name(4) value(4) size(4) info(1) other(1) shndx(2)
    null_sym = struct.pack("<IIIBBH", 0, 0, 0, 0, 0, 0)
    # STT_OBJECT=1, STB_GLOBAL=1 → info = (1<<4)|1 = 0x11
    obj_sym = struct.pack("<IIIBBH", 1, 0x20000000, 4, 0x11, 0, 1)  # shndx=1 (.data)
    symtab = null_sym + obj_sym

    # Layout offsets after ELF header (52 bytes)
    ehdr_size = 52
    off_data = ehdr_size
    off_shstr = off_data + len(data_section)
    off_strtab = off_shstr + len(shstr)
    off_symtab = off_strtab + len(strtab)
    off_shdr = off_symtab + len(symtab)

    # Section header table: NULL, .data, .shstrtab, .strtab, .symtab
    def shdr(name_off, sh_type, flags, addr, offset, size, link, info, align, entsize):
        return struct.pack(
            "<IIIIIIIIII",
            name_off,
            sh_type,
            flags,
            addr,
            offset,
            size,
            link,
            info,
            align,
            entsize,
        )

    SHT_NULL, SHT_PROGBITS, SHT_STRTAB, SHT_SYMTAB = 0, 1, 3, 2
    SHF_ALLOC_WRITE = 0x3

    # name offsets in shstr
    # \0 .data \0 .shstrtab \0 .strtab \0 .symtab \0
    # 0, 1, 7, 17, 25
    sections = b"".join(
        [
            shdr(0, SHT_NULL, 0, 0, 0, 0, 0, 0, 0, 0),
            shdr(1, SHT_PROGBITS, SHF_ALLOC_WRITE, 0x20000000, off_data, len(data_section), 0, 0, 4, 0),
            shdr(7, SHT_STRTAB, 0, 0, off_shstr, len(shstr), 0, 0, 1, 0),
            shdr(17, SHT_STRTAB, 0, 0, off_strtab, len(strtab), 0, 0, 1, 0),
            shdr(25, SHT_SYMTAB, 0, 0, off_symtab, len(symtab), 3, 1, 4, 16),  # link=.strtab, info=1st global
        ]
    )

    e_shnum = 5
    e_shstrndx = 2
    ehdr = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01" + b"\x00" * 9,  # e_ident
        2,  # ET_EXEC
        3,  # EM_386
        1,  # version
        0x20000000,  # entry
        0,  # phoff
        off_shdr,  # shoff
        0,  # flags
        ehdr_size,  # ehsize
        0,  # phentsize
        0,  # phnum
        40,  # shentsize
        e_shnum,
        e_shstrndx,
    )
    assert len(ehdr) == 52

    blob = ehdr + data_section + shstr + strtab + symtab + sections
    OUT.write_bytes(blob)
    print(f"Wrote {OUT} ({len(blob)} bytes)")


if __name__ == "__main__":
    main()
