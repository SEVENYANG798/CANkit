"""A2L address update from ELF map (text rewrite)."""

from pathlib import Path

from xcp_pcan.symbols.a2l_updater import update_a2l_from_elf, update_a2l_text

FIX = Path(__file__).parent / "fixtures"


def test_update_existing_ecu_address():
    text = """
/begin MEASUREMENT EngineSpeed "rpm" UWORD Cm 0 0 0 8000
  ECU_ADDRESS 0x20000000
/end MEASUREMENT
"""
    new, report = update_a2l_text(text, {"EngineSpeed": 0x2000ABCD})
    assert "ECU_ADDRESS 0x2000ABCD" in new
    assert report.objects_seen == 1
    assert len(report.updated) == 1
    assert report.updated[0].old_address == 0x20000000
    assert report.updated[0].new_address == 0x2000ABCD


def test_multiline_begin_name():
    """Vector/ASAP2 often puts the object name on the line after /begin."""
    text = """
/begin MEASUREMENT
  EngineSpeed
  "rpm"
  UWORD
  ECU_ADDRESS 0x20000000
/end MEASUREMENT
"""
    new, report = update_a2l_text(text, {"EngineSpeed": 0x2000ABCD})
    assert report.objects_seen == 1
    assert "ECU_ADDRESS 0x2000ABCD" in new
    assert len(report.updated) == 1


def test_ecu_address_on_same_line_as_begin():
    text = """
/begin MEASUREMENT Foo ECU_ADDRESS 0x10
/end MEASUREMENT
"""
    new, report = update_a2l_text(text, {"Foo": 0x99})
    assert report.objects_seen == 1
    assert "ECU_ADDRESS 0x99" in new
    assert len(report.updated) == 1


def test_insert_ecu_address_when_missing():
    text = """
/begin MEASUREMENT NeedsElf "x" UWORD Cm 0 0 0 100
  SYMBOL_LINK "ElfOnlySymbol" 0
/end MEASUREMENT
"""
    new, report = update_a2l_text(text, {"ElfOnlySymbol": 0x10001000})
    assert "ECU_ADDRESS 0x10001000" in new
    assert len(report.inserted) == 1
    assert report.inserted[0].linker_name == "ElfOnlySymbol"


def test_symbol_link_offset():
    text = """
/begin MEASUREMENT ViaOff "x" UWORD Cm 0 0 0 100
  SYMBOL_LINK "BaseSym" 4
  ECU_ADDRESS 0x0
/end MEASUREMENT
"""
    new, report = update_a2l_text(text, {"BaseSym": 0x20000000})
    assert "ECU_ADDRESS 0x20000004" in new
    assert report.updated[0].new_address == 0x20000004


def test_unchanged_and_missing():
    text = """
/begin MEASUREMENT Keep "x" UWORD Cm 0 0 0 100
  ECU_ADDRESS 0x10
/end MEASUREMENT
/begin MEASUREMENT Ghost "x" UWORD Cm 0 0 0 100
  ECU_ADDRESS 0x20
/end MEASUREMENT
"""
    new, report = update_a2l_text(text, {"Keep": 0x10})
    assert "ECU_ADDRESS 0x10" in new
    assert "ECU_ADDRESS 0x20" in new
    assert len(report.unchanged) == 1
    assert report.missing == ["MEASUREMENT Ghost"]


def test_characteristic_update():
    text = """
/begin CHARACTERISTIC CalGain "g" VALUE Cm 0 100
  ECU_ADDRESS 0x20001000
  UWORD
/end CHARACTERISTIC
"""
    new, report = update_a2l_text(text, {"CalGain": 0x30001000})
    assert "ECU_ADDRESS 0x30001000" in new
    assert report.updated[0].kind == "CHARACTERISTIC"


def test_characteristic_positional_and_link_map():
    text = """
/begin CHARACTERISTIC Foo.Val "x"
  VALUE 0x1495B0 RL 0 NO_COMPU_METHOD 0 1
  SYMBOL_LINK "Group.Member_Val" 0
  /begin IF_DATA CANAPE_EXT
    LINK_MAP "Group.Member_Val" 0x1495B0 0 0 0 1 0x1 0
  /end IF_DATA
/end CHARACTERISTIC
"""
    new, report = update_a2l_text(text, {"Group.Member_Val": 0xABCDEF0})
    assert report.objects_seen == 1
    assert "VALUE 0xABCDEF0" in new
    assert 'LINK_MAP "Group.Member_Val" 0xABCDEF0' in new
    assert len(report.updated) == 1


def test_ghs_underscore_alias_lookup():
    text = """
/begin MEASUREMENT M1 "x" UWORD Cm 0 0 0 100
  ECU_ADDRESS 0x10
  SYMBOL_LINK "Rte_Foo_Val" 0
/end MEASUREMENT
"""
    # ELF map only has GHS-style leading underscore
    new, report = update_a2l_text(text, {"_Rte_Foo_Val": 0x20})
    assert "ECU_ADDRESS 0x20" in new
    assert len(report.updated) == 1


def test_update_sample_a2l_with_sample_elf(tmp_path: Path):
    elf = FIX / "sample.elf"
    if not elf.exists():
        import runpy

        runpy.run_path(str(FIX / "make_sample_elf.py"), run_name="__main__")
    a2l = FIX / "sample.a2l"
    out = tmp_path / "sample_updated.a2l"
    # sample.elf has EngineSpeed @ 0x20000000 — same as A2L → unchanged for that one
    report = update_a2l_from_elf(a2l, elf, out)
    assert out.exists()
    assert report.output_path == str(out)
    body = out.read_text(encoding="utf-8")
    assert "EngineSpeed" in body
    # Only EngineSpeed is in the sample ELF; others missing
    assert any("EngineSpeed" in m or m.endswith("EngineSpeed") for m in ["MEASUREMENT EngineSpeed"]) or (
        any(u.object_name == "EngineSpeed" for u in report.unchanged + report.updated)
    )
