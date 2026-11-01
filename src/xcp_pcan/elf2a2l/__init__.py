"""ELF globals → A2L (ported from the toa2l project)."""

from xcp_pcan.elf2a2l.a2l_gen import (
    BUNDLED_TEMPLATES,
    DEFAULT_CAN_ID_MASTER,
    DEFAULT_CAN_ID_SLAVE,
    bundled_template_path,
    default_template_path,
    fill_template,
    normalize_can_id,
    parse_template_can_ids,
)
from xcp_pcan.elf2a2l.a2l_update import load_a2l, save_a2l
from xcp_pcan.elf2a2l.elf_extract import extract_variables

__all__ = [
    "BUNDLED_TEMPLATES",
    "DEFAULT_CAN_ID_MASTER",
    "DEFAULT_CAN_ID_SLAVE",
    "bundled_template_path",
    "default_template_path",
    "extract_variables",
    "fill_template",
    "load_a2l",
    "normalize_can_id",
    "parse_template_can_ids",
    "save_a2l",
]
