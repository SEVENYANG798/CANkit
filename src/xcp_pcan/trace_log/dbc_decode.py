"""DBC-backed CAN frame decode (cantools)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xcp_pcan.trace_log.types import TraceFrame

_EXT_FLAG = 0x80000000
_ID_MASK = 0x1FFFFFFF


@dataclass(frozen=True, slots=True)
class DecodedSignal:
    name: str
    value: Any
    unit: str = ""

    def format_value(self) -> str:
        val = self.value
        if isinstance(val, float):
            text = f"{val:.6g}"
        else:
            text = str(val)
        return f"{text} {self.unit}".rstrip()


@dataclass(frozen=True, slots=True)
class DecodedMessage:
    name: str
    frame_id: int
    signals: tuple[DecodedSignal, ...]
    error: str = ""


@dataclass(frozen=True, slots=True)
class SignalInfo:
    """One DBC signal entry for pickers."""

    message: str
    frame_id: int
    signal: str
    unit: str = ""


@dataclass(frozen=True, slots=True)
class SignalSample:
    """One decoded sample along a Trace for signal-plot / table."""

    timestamp: float
    frame_index: int
    value: Any
    numeric: float | None
    text: str


class DbcDatabase:
    """Thin wrapper so the UI does not import cantools directly everywhere."""

    def __init__(self, path: str | Path) -> None:
        import cantools

        self.path = Path(path)
        self._db = cantools.database.load_file(str(self.path))
        self.message_count = len(self._db.messages)
        # Map both raw DBC frame_id and 29-bit arbitration id → message.
        self._by_id: dict[int, Any] = {}
        for message in self._db.messages:
            fid = int(message.frame_id)
            self._by_id[fid] = message
            self._by_id[fid & _ID_MASK] = message
            if getattr(message, "is_extended_frame", False) or fid > 0x7FF:
                self._by_id[fid | _EXT_FLAG] = message

    def _message_for(self, frame_id: int):
        msg = self._by_id.get(frame_id)
        if msg is not None:
            return msg
        msg = self._by_id.get(frame_id & _ID_MASK)
        if msg is not None:
            return msg
        try:
            return self._db.get_message_by_frame_id(frame_id)
        except KeyError:
            try:
                return self._db.get_message_by_frame_id(frame_id | _EXT_FLAG)
            except KeyError:
                try:
                    return self._db.get_message_by_frame_id(frame_id & _ID_MASK)
                except KeyError as exc:
                    raise KeyError(frame_id) from exc

    def message_name(self, frame_id: int) -> str | None:
        try:
            return self._message_for(frame_id).name
        except KeyError:
            return None

    def message_names(self) -> dict[int, str]:
        """Arbitration-id → name (29-bit key so trace frames match)."""
        out: dict[int, str] = {}
        for message in self._db.messages:
            fid = int(message.frame_id)
            out[fid & _ID_MASK] = message.name
            out[fid] = message.name
        return out

    def decode(self, frame: TraceFrame) -> DecodedMessage | None:
        try:
            message = self._message_for(frame.arbitration_id)
        except KeyError:
            return None
        try:
            raw = message.decode(frame.data, decode_choices=True, scaling=True)
        except Exception as exc:  # noqa: BLE001 — show decode errors in UI
            return DecodedMessage(
                name=message.name,
                frame_id=frame.arbitration_id,
                signals=(),
                error=str(exc),
            )
        signals: list[DecodedSignal] = []
        by_name = {sig.name: sig for sig in message.signals}
        for name, value in raw.items():
            unit = ""
            sig = by_name.get(name)
            if sig is not None and sig.unit:
                unit = str(sig.unit)
            signals.append(DecodedSignal(name=name, value=value, unit=unit))
        return DecodedMessage(
            name=message.name,
            frame_id=frame.arbitration_id,
            signals=tuple(signals),
        )

    def format_detail(self, frame: TraceFrame) -> str | None:
        decoded = self.decode(frame)
        if decoded is None:
            return None
        lines = [f"DBC: {decoded.name}"]
        if decoded.error:
            lines.append(f"  decode error: {decoded.error}")
            return "\n".join(lines)
        if not decoded.signals:
            lines.append("  (no signals)")
            return "\n".join(lines)
        width = max(len(s.name) for s in decoded.signals)
        for sig in decoded.signals:
            lines.append(f"  {sig.name:<{width}}  {sig.format_value()}")
        return "\n".join(lines)

    def list_messages(self) -> list[tuple[str, int]]:
        """``(message_name, frame_id)`` sorted by CAN-ID ascending."""
        out: list[tuple[str, int]] = []
        for message in self._db.messages:
            fid = int(message.frame_id) & _ID_MASK
            out.append((message.name, fid))
        out.sort(key=lambda item: (item[1], item[0].lower()))
        return out

    def list_signals(self) -> list[SignalInfo]:
        """Flat Message/Signal list for UI pickers (CAN-ID, then signal name)."""
        out: list[SignalInfo] = []
        for message in self._db.messages:
            fid = int(message.frame_id) & _ID_MASK
            for sig in message.signals:
                unit = str(sig.unit) if sig.unit else ""
                out.append(
                    SignalInfo(
                        message=message.name,
                        frame_id=fid,
                        signal=sig.name,
                        unit=unit,
                    )
                )
        out.sort(key=lambda s: (s.frame_id, s.message.lower(), s.signal.lower()))
        return out

    def extract_signal_series(
        self,
        frames: list[TraceFrame],
        *,
        frame_id: int,
        signal: str,
    ) -> tuple[str, str, list[SignalSample]]:
        """Decode ``signal`` on matching Trace frames (offline plot / table).

        Returns ``(message_name, unit, samples)``. Non-matching / error frames
        and decode failures are skipped.
        """
        try:
            message = self._message_for(frame_id)
        except KeyError as exc:
            raise KeyError(f"no DBC message for ID 0x{frame_id:X}") from exc
        unit = ""
        for sig in message.signals:
            if sig.name == signal:
                unit = str(sig.unit) if sig.unit else ""
                break
        else:
            raise KeyError(f"signal {signal!r} not in {message.name}")

        want = int(frame_id) & _ID_MASK
        samples: list[SignalSample] = []
        for fr in frames:
            if fr.is_error:
                continue
            if (int(fr.arbitration_id) & _ID_MASK) != want:
                continue
            try:
                # Physical values (no named choices) — suited to plot + period analysis.
                decoded = message.decode(
                    fr.data, decode_choices=False, scaling=True
                )
            except Exception:  # noqa: BLE001 — skip undecodable frames
                continue
            if signal not in decoded:
                continue
            raw_val = decoded[signal]
            numeric: float | None
            try:
                numeric = float(raw_val)
            except (TypeError, ValueError):
                numeric = None
            if isinstance(raw_val, float):
                text = f"{raw_val:.6g}"
            else:
                text = str(raw_val)
            samples.append(
                SignalSample(
                    timestamp=float(fr.timestamp),
                    frame_index=int(fr.index),
                    value=raw_val,
                    numeric=numeric,
                    text=text,
                )
            )
        return message.name, unit, samples
