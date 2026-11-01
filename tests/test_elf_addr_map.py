"""ELF address-map name normalization helpers."""

from xcp_pcan.symbols.elf_addr_map import normalize_symbol_name


def test_normalize_ghs_suffix_and_underscore():
    assert (
        normalize_symbol_name("_Rte_Foo_GROUP..D.3A.5Cpath...")
        == "Rte_Foo_GROUP"
    )
    assert normalize_symbol_name("Rte_Foo") == "Rte_Foo"
