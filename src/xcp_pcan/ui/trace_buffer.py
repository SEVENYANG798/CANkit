"""Ring buffers for live measurement traces."""

from __future__ import annotations

import csv
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TracePoint:
    t: float  # epoch seconds
    value: float


class TraceBuffer:
    """Thread-safe per-symbol time series for strip charts."""

    def __init__(self, maxlen: int = 10_000) -> None:
        self._maxlen = max(100, maxlen)
        self._series: dict[str, deque[TracePoint]] = defaultdict(
            lambda: deque(maxlen=self._maxlen)
        )
        self._units: dict[str, str] = {}
        self._lock = threading.Lock()
        self._t0: float | None = None

    def clear(self) -> None:
        with self._lock:
            self._series.clear()
            self._units.clear()
            self._t0 = None

    def clear_series(self, name: str) -> None:
        with self._lock:
            self._series.pop(name, None)
            self._units.pop(name, None)

    def append(self, name: str, timestamp: float, value: float, unit: str = "") -> None:
        with self._lock:
            if self._t0 is None:
                self._t0 = timestamp
            self._series[name].append(TracePoint(t=timestamp, value=float(value)))
            if unit:
                self._units[name] = unit

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._series.keys())

    def unit(self, name: str) -> str:
        with self._lock:
            return self._units.get(name, "")

    def points(self, name: str) -> list[TracePoint]:
        with self._lock:
            return list(self._series.get(name, ()))

    def relative_xy(self, name: str) -> tuple[list[float], list[float]]:
        """Return (t_rel_seconds, values) for plotting."""
        with self._lock:
            pts = list(self._series.get(name, ()))
            t0 = self._t0
        if not pts or t0 is None:
            return [], []
        xs = [p.t - t0 for p in pts]
        ys = [p.value for p in pts]
        return xs, ys

    def t0(self) -> float | None:
        with self._lock:
            return self._t0

    def point_count(self) -> int:
        with self._lock:
            return sum(len(pts) for pts in self._series.values())

    def export_csv(self, path: Path) -> int:
        """Write long-format CSV: time_s,timestamp,name,value,unit. Returns rows."""
        with self._lock:
            t0 = self._t0
            units = dict(self._units)
            rows: list[tuple[float, float, str, float, str]] = []
            for name, pts in self._series.items():
                unit = units.get(name, "")
                for p in pts:
                    rel = (p.t - t0) if t0 is not None else 0.0
                    rows.append((rel, p.t, name, p.value, unit))
        rows.sort(key=lambda r: (r[1], r[2]))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time_s", "timestamp", "name", "value", "unit"])
            for rel, ts, name, value, unit in rows:
                writer.writerow([f"{rel:.6f}", f"{ts:.6f}", name, repr(value), unit])
        return len(rows)
