"""Seed & Key providers for XCP UNLOCK (Python plugin or Windows DLL)."""

from __future__ import annotations

import importlib.util
import logging
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Typical Vector / CANape SeedNKey export:
# DWORD XCP_ComputeKeyFromSeed(BYTE resource, BYTE seedLen, BYTE* seed,
#                              BYTE* keyLen, BYTE* key);
DEFAULT_DLL_SYMBOL = "XCP_ComputeKeyFromSeed"


class SeedKeyError(RuntimeError):
    """Raised when seed→key computation or plugin load fails."""


class SeedKeyProvider(ABC):
    @abstractmethod
    def compute_key(self, resource: int, seed: bytes) -> bytes:
        """Return the unlock key for ``resource`` given the slave ``seed``."""


class CallableSeedKeyProvider(SeedKeyProvider):
    def __init__(self, fn: Callable[[int, bytes], bytes], *, name: str = "callable") -> None:
        self._fn = fn
        self.name = name

    def compute_key(self, resource: int, seed: bytes) -> bytes:
        key = self._fn(int(resource), bytes(seed))
        if not isinstance(key, (bytes, bytearray)):
            raise SeedKeyError(f"{self.name}: compute_key must return bytes")
        return bytes(key)


class EchoSeedKeyProvider(SeedKeyProvider):
    """Debug provider: key == seed (only for open/sim slaves)."""

    name = "echo"

    def compute_key(self, resource: int, seed: bytes) -> bytes:
        _ = resource
        return bytes(seed)


class DllSeedKeyProvider(SeedKeyProvider):
    """Load a Win32 Seed&Key DLL (ctypes).

    Expected C prototype (CANape / Vector style)::

        DWORD XCP_ComputeKeyFromSeed(
            BYTE resource,
            BYTE seedLength,
            BYTE* seed,
            BYTE* keyLength,   /* in: max, out: actual */
            BYTE* key
        );
        /* return 0 on success */
    """

    def __init__(self, path: str | Path, *, symbol: str = DEFAULT_DLL_SYMBOL) -> None:
        if sys.platform != "win32":
            raise SeedKeyError("DLL Seed&Key is only supported on Windows")
        import ctypes
        from ctypes import POINTER, byref, c_ubyte, c_uint32

        path = Path(path)
        if not path.is_file():
            raise SeedKeyError(f"Seed&Key DLL not found: {path}")
        self.path = path
        self.symbol = symbol
        self.name = f"dll:{path.name}:{symbol}"
        try:
            self._dll = ctypes.WinDLL(str(path))
        except OSError as exc:
            raise SeedKeyError(f"Failed to load Seed&Key DLL: {exc}") from exc
        try:
            func = getattr(self._dll, symbol)
        except AttributeError as exc:
            raise SeedKeyError(f"DLL export '{symbol}' not found in {path.name}") from exc
        func.argtypes = [c_ubyte, c_ubyte, POINTER(c_ubyte), POINTER(c_ubyte), POINTER(c_ubyte)]
        func.restype = c_uint32
        self._func = func
        self._c_ubyte = c_ubyte
        self._byref = byref
        self._POINTER = POINTER

    def compute_key(self, resource: int, seed: bytes) -> bytes:
        if len(seed) > 255:
            raise SeedKeyError("Seed longer than 255 bytes is not supported by DLL API")
        seed_arr = (self._c_ubyte * len(seed)).from_buffer_copy(seed)
        key_buf = (self._c_ubyte * 255)()
        key_len = self._c_ubyte(255)
        rc = int(
            self._func(
                self._c_ubyte(resource & 0xFF),
                self._c_ubyte(len(seed)),
                seed_arr,
                self._byref(key_len),
                key_buf,
            )
        )
        if rc != 0:
            raise SeedKeyError(f"{self.name} returned error code {rc}")
        n = int(key_len.value)
        if n <= 0 or n > 255:
            raise SeedKeyError(f"{self.name} returned invalid key length {n}")
        return bytes(key_buf[:n])


def load_python_seedkey(path: str | Path) -> SeedKeyProvider:
    """Load a ``.py`` plugin that defines ``compute_key(resource, seed) -> bytes``."""
    path = Path(path)
    if not path.is_file():
        raise SeedKeyError(f"Seed&Key Python plugin not found: {path}")
    spec = importlib.util.spec_from_file_location("xcp_pcan_seedkey_plugin", path)
    if spec is None or spec.loader is None:
        raise SeedKeyError(f"Cannot import Seed&Key plugin: {path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        raise SeedKeyError(f"Seed&Key plugin failed to load: {exc}") from exc
    fn = getattr(mod, "compute_key", None)
    if not callable(fn):
        raise SeedKeyError(f"{path.name} must define compute_key(resource, seed) -> bytes")
    return CallableSeedKeyProvider(fn, name=f"py:{path.name}")


def load_seedkey(path: str | Path, *, dll_symbol: str = DEFAULT_DLL_SYMBOL) -> SeedKeyProvider:
    """Auto-detect provider from file suffix (``.py`` / ``.dll`` / ``.echo``)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".py":
        return load_python_seedkey(path)
    if suffix in {".dll", ".so"}:
        return DllSeedKeyProvider(path, symbol=dll_symbol)
    if path.name.lower() in {"echo", "identity"} or suffix == ".echo":
        return EchoSeedKeyProvider()
    raise SeedKeyError(f"Unsupported Seed&Key path (use .py or .dll): {path}")
