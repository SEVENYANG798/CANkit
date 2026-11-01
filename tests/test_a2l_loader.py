"""A2L loader tests."""

from pathlib import Path

from xcp_pcan.symbols.a2l_loader import load_a2l
from xcp_pcan.symbols.types import DataType, physical_value

FIX = Path(__file__).parent / "fixtures" / "sample.a2l"


def test_load_measurements():
    store = load_a2l(FIX)
    names = {v.name for v in store.variables}
    assert "EngineSpeed" in names
    assert "CoolantTemp" in names
    assert "FlagByte" in names
    assert "CalGain" in names
    assert "CalTempOffset" in names


def test_characteristic_writable():
    from xcp_pcan.symbols.types import SymbolKind

    store = load_a2l(FIX)
    cal = store.by_name()["CalGain"]
    assert cal.writable is True
    assert cal.kind == SymbolKind.CHARACTERISTIC
    assert cal.characteristic_type == "VALUE"
    assert cal.address == 0x20001000
    assert store.by_name()["EngineSpeed"].writable is False


def test_val_blk_not_writable_and_record_layout_datatype():
    from xcp_pcan.symbols.types import DataType, SymbolKind

    store = load_a2l(FIX)
    blk = store.by_name()["CalTable"]
    assert blk.kind == SymbolKind.CHARACTERISTIC
    assert blk.characteristic_type == "VAL_BLK"
    assert blk.writable is False
    assert blk.datatype == DataType.UBYTE

    flag = store.by_name()["CalFlag"]
    assert flag.writable is True
    assert flag.datatype == DataType.UBYTE
    assert flag.size == 1



def test_addresses_and_types():
    store = load_a2l(FIX)
    by = store.by_name()
    assert by["EngineSpeed"].address == 0x20000000
    assert by["EngineSpeed"].datatype == DataType.UWORD
    assert by["CoolantTemp"].address == 0x20000004
    assert by["CoolantTemp"].datatype == DataType.SWORD
    assert by["FlagByte"].size == 1


def test_linear_conversion():
    store = load_a2l(FIX)
    cm = store.by_name()["CoolantTemp"].conversion
    assert cm.is_identity is False
    assert cm.factor == 0.1
    assert cm.unit == "degC"
    assert physical_value(250, cm) == 25.0


def test_rat_func_linear_case():
    store = load_a2l(FIX)
    cm = store.by_name()["EngineSpeed"].conversion
    assert cm.factor == 0.25
    assert physical_value(4000, cm) == 1000.0


def test_search():
    store = load_a2l(FIX)
    hits = store.search("temp")
    names = {v.name for v in hits}
    assert "CoolantTemp" in names
    assert "CalTempOffset" in names



def test_search_paste_list():
    store = load_a2l(FIX)
    hits = store.search("EngineSpeed\nCoolantTemp\nMissingVar")
    assert [v.name for v in hits] == ["EngineSpeed", "CoolantTemp"]
    assert store.missing_names("EngineSpeed\nCoolantTemp\nMissingVar") == ["MissingVar"]


def test_search_comma_list():
    store = load_a2l(FIX)
    hits = store.search("FlagByte, EngineSpeed")
    assert [v.name for v in hits] == ["FlagByte", "EngineSpeed"]
