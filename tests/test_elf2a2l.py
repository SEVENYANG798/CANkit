"""ELF → A2L generation (bundled template)."""

from pathlib import Path

import pytest

from xcp_pcan.elf2a2l.a2l_gen import (
    BUNDLED_TEMPLATES,
    bundled_template_path,
    fill_template,
    parse_template_can_ids,
)
from xcp_pcan.elf2a2l.a2l_update import load_a2l
from xcp_pcan.elf2a2l.elf_extract import extract_variables

FIX = Path(__file__).parent / "fixtures"
TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "xcp_pcan" / "elf2a2l" / "templates"


def _sample_vars():
    elf = FIX / "sample.elf"
    if not elf.exists():
        import runpy

        runpy.run_path(str(FIX / "make_sample_elf.py"), run_name="__main__")
    return extract_variables(str(elf), with_types=False)


def test_fill_template_from_sample_elf(tmp_path: Path):
    variables, _typed, sections = _sample_vars()
    assert any(v.name == "EngineSpeed" for v in variables)
    tpl = TPL_DIR / "xcp_fr.a2l"
    text = fill_template(
        load_a2l(tpl),
        variables,
        sections,
        elf_path=str(FIX / "sample.elf"),
        can_id_master="0x667",
        can_id_slave="0x7E1",
    )
    assert "EngineSpeed" in text
    assert "ECU_ADDRESS" in text
    assert "0x667" in text
    out = tmp_path / "out.a2l"
    out.write_text(text, encoding="utf-8")
    assert out.stat().st_size > 1000


@pytest.mark.parametrize("filename,master,slave", [
    (fname, m, s) for _label, fname, m, s in BUNDLED_TEMPLATES
])
def test_n82_templates_fill(filename: str, master: str, slave: str, tmp_path: Path):
    path = bundled_template_path(filename)
    assert path.is_file()
    raw = load_a2l(path)
    parsed_m, parsed_s = parse_template_can_ids(raw)
    assert parsed_m == master
    assert parsed_s == slave
    variables, _typed, sections = _sample_vars()
    text = fill_template(raw, variables, sections, elf_path=str(FIX / "sample.elf"))
    assert "EngineSpeed" in text
    assert "@@ELF_MEASUREMENTS@@" not in text
    kept_m, kept_s = parse_template_can_ids(text)
    assert kept_m == master
    assert kept_s == slave
    out = tmp_path / filename
    out.write_text(text, encoding="utf-8")
    assert out.stat().st_size > 1000
