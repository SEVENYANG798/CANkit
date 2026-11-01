"""ELF loader tests."""

from pathlib import Path

from xcp_pcan.symbols.elf_loader import load_elf

FIX_DIR = Path(__file__).parent / "fixtures"
ELF = FIX_DIR / "sample.elf"


def _ensure_sample_elf() -> None:
    if ELF.exists():
        return
    import runpy

    runpy.run_path(str(FIX_DIR / "make_sample_elf.py"), run_name="__main__")


def test_load_elf_symbol():
    _ensure_sample_elf()
    store = load_elf(ELF)
    names = {v.name for v in store.variables}
    assert "EngineSpeed" in names
    var = store.by_name()["EngineSpeed"]
    assert var.address == 0x20000000
    assert var.size == 4
    assert var.source == "elf"
