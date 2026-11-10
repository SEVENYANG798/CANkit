"""Disk cache for parsed ELF SymbolStore (skip re-parse when file unchanged)."""

from __future__ import annotations

import hashlib
import logging
import pickle
from pathlib import Path

from xcp_pcan.symbols.types import SymbolStore

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".xcp_pcan" / "elf_cache"
_CACHE_VERSION = 1


def _file_fingerprint(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    resolved = str(path.resolve())
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
    return resolved, int(mtime_ns), int(st.st_size)


def cache_key(path: Path) -> str:
    resolved, mtime_ns, size = _file_fingerprint(path)
    raw = f"v{_CACHE_VERSION}|{resolved}|{mtime_ns}|{size}".encode()
    return hashlib.sha1(raw).hexdigest()


def cache_path_for(path: Path) -> Path:
    return _CACHE_DIR / f"{cache_key(path)}.pkl"


def try_load(path: Path) -> SymbolStore | None:
    """Return cached store if fingerprint still matches; else None."""
    try:
        if not path.is_file():
            return None
        cpath = cache_path_for(path)
        if not cpath.is_file():
            return None
        with cpath.open("rb") as fh:
            payload = pickle.load(fh)
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != _CACHE_VERSION:
            return None
        fp = _file_fingerprint(path)
        if payload.get("fingerprint") != fp:
            return None
        store = payload.get("store")
        if not isinstance(store, SymbolStore):
            return None
        return store
    except Exception:  # noqa: BLE001
        logger.debug("ELF cache miss/read failed for %s", path, exc_info=True)
        return None


def save(path: Path, store: SymbolStore) -> None:
    """Write store to cache keyed by current file fingerprint."""
    try:
        if not path.is_file():
            return
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cpath = cache_path_for(path)
        payload = {
            "version": _CACHE_VERSION,
            "fingerprint": _file_fingerprint(path),
            "store": store,
        }
        tmp = cpath.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(cpath)
        logger.info("ELF cache saved: %s (%d symbols)", path.name, len(store.variables))
    except Exception:  # noqa: BLE001
        logger.warning("ELF cache write failed for %s", path, exc_info=True)
