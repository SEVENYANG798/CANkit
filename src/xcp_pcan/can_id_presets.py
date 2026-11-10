"""Built-in + user-saved XCP / UDS CAN ID presets."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QSettings

CUSTOM_KEY = "custom"
USER_KEY_PREFIX = "user_"

# Match MainWindow / __main__ organization so presets survive restarts.
SETTINGS_ORG = "CANkit"
SETTINGS_APP = "CANkit"
_SETTINGS_XCP = "can_id_presets/xcp_user"
_SETTINGS_UDS = "can_id_presets/uds_user"


@dataclass(frozen=True, slots=True)
class CanIdPreset:
    key: str
    name: str
    tx: str  # e.g. "0x725"
    rx: str


# UDS Req (tester→ECU) / Resp (ECU→tester)
UDS_PRESETS: tuple[CanIdPreset, ...] = (
    CanIdPreset("uds_fl", "FL", "0x725", "0x7A5"),
    CanIdPreset("uds_fr", "FR", "0x729", "0x7A9"),
    CanIdPreset("uds_fr_dev", "FR_DEV", "0x7E0", "0x7E8"),
    CanIdPreset("uds_fr_erev", "FR_EREV", "0x7E4", "0x7EC"),
)

# XCP Master TX / Slave RX
XCP_PRESETS: tuple[CanIdPreset, ...] = (
    CanIdPreset("xcp_viu_fl", "VIU_FL", "0x6A8", "0x6A9"),
    CanIdPreset("xcp_viu_fr", "VIU_FR", "0x6A6", "0x6A7"),
    CanIdPreset("xcp_viu_r", "VIU_R", "0x6AA", "0x6AB"),
)


def _id_hex(text: str) -> str:
    """Normalize to 0x-prefixed uppercase hex: 6AA → 0x6AA."""
    value = normalize_can_id_int(text)
    if value is None:
        raw = (text or "").strip()
        return raw if raw.lower().startswith("0x") else f"0x{raw.upper()}"
    return f"0x{value:X}"


def preset_label(preset: CanIdPreset) -> str:
    return f"{preset.name} Tx:{_id_hex(preset.tx)}/Rx:{_id_hex(preset.rx)}"


def is_user_preset_key(key: str) -> bool:
    return str(key).startswith(USER_KEY_PREFIX)


def normalize_can_id_int(text: str) -> int | None:
    raw = (text or "").strip().lower()
    if not raw:
        return None
    try:
        if raw.startswith("0x"):
            return int(raw, 16)
        if any(c in "abcdef" for c in raw):
            return int(raw, 16)
        # Bare digits: accept hex if looks like CAN ID hex (common in this tool)
        return int(raw, 16) if len(raw) <= 3 else int(raw, 10)
    except ValueError:
        return None


def format_can_id(value: int) -> str:
    return f"0x{value:X}"


def match_preset_key(
    presets: Sequence[CanIdPreset],
    tx_text: str,
    rx_text: str,
) -> str:
    """Return preset key if TX/RX match (numeric), else Custom."""
    tx = normalize_can_id_int(tx_text)
    rx = normalize_can_id_int(rx_text)
    if tx is None or rx is None:
        return CUSTOM_KEY
    for preset in presets:
        pt = normalize_can_id_int(preset.tx)
        pr = normalize_can_id_int(preset.rx)
        if pt == tx and pr == rx:
            return preset.key
    return CUSTOM_KEY


def find_preset(
    presets: Sequence[CanIdPreset],
    key: str,
) -> CanIdPreset | None:
    for preset in presets:
        if preset.key == key:
            return preset
    return None


def app_settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def _settings_key(kind: str) -> str:
    if kind == "xcp":
        return _SETTINGS_XCP
    if kind == "uds":
        return _SETTINGS_UDS
    raise ValueError(f"unknown preset kind: {kind!r}")


def load_user_presets(kind: str, settings: QSettings | None = None) -> list[CanIdPreset]:
    store = settings or app_settings()
    raw = store.value(_settings_key(kind), "", str)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[CanIdPreset] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        tx = str(item.get("tx", "")).strip()
        rx = str(item.get("rx", "")).strip()
        key = str(item.get("key", "")).strip()
        if not name or normalize_can_id_int(tx) is None or normalize_can_id_int(rx) is None:
            continue
        if not key.startswith(USER_KEY_PREFIX):
            key = _make_user_key(kind, name, existing={p.key for p in out})
        tx_i = normalize_can_id_int(tx)
        rx_i = normalize_can_id_int(rx)
        assert tx_i is not None and rx_i is not None
        out.append(
            CanIdPreset(
                key=key,
                name=name,
                tx=format_can_id(tx_i),
                rx=format_can_id(rx_i),
            )
        )
    return out


def save_user_presets(
    kind: str,
    presets: Sequence[CanIdPreset],
    settings: QSettings | None = None,
) -> None:
    store = settings or app_settings()
    payload = [
        {"key": p.key, "name": p.name, "tx": p.tx, "rx": p.rx}
        for p in presets
        if is_user_preset_key(p.key)
    ]
    store.setValue(_settings_key(kind), json.dumps(payload, ensure_ascii=False))


def _make_user_key(kind: str, name: str, *, existing: set[str]) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip()).strip("_").lower() or "label"
    base = f"{USER_KEY_PREFIX}{kind}_{slug}"
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def add_user_preset(
    kind: str,
    name: str,
    tx_text: str,
    rx_text: str,
    settings: QSettings | None = None,
) -> CanIdPreset:
    """Add or update a user preset by name; persist and return it."""
    name = name.strip()
    if not name:
        raise ValueError("Label name is empty")
    tx_i = normalize_can_id_int(tx_text)
    rx_i = normalize_can_id_int(rx_text)
    if tx_i is None or rx_i is None:
        raise ValueError("TX/RX must be valid CAN IDs")
    tx = format_can_id(tx_i)
    rx = format_can_id(rx_i)
    users = load_user_presets(kind, settings)
    # Same name → update IDs in place.
    for i, preset in enumerate(users):
        if preset.name.casefold() == name.casefold():
            updated = CanIdPreset(key=preset.key, name=name, tx=tx, rx=rx)
            users[i] = updated
            save_user_presets(kind, users, settings)
            return updated
    key = _make_user_key(kind, name, existing={p.key for p in users})
    created = CanIdPreset(key=key, name=name, tx=tx, rx=rx)
    users.append(created)
    save_user_presets(kind, users, settings)
    return created


def remove_user_preset(
    kind: str,
    key: str,
    settings: QSettings | None = None,
) -> bool:
    if not is_user_preset_key(key):
        return False
    users = load_user_presets(kind, settings)
    kept = [p for p in users if p.key != key]
    if len(kept) == len(users):
        return False
    save_user_presets(kind, kept, settings)
    return True


def merged_presets(
    kind: str,
    builtin: Sequence[CanIdPreset],
    settings: QSettings | None = None,
) -> list[CanIdPreset]:
    return list(builtin) + load_user_presets(kind, settings)
