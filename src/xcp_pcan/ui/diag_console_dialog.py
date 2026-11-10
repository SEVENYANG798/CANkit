"""UDS diagnostic console (Vector-style layout) over ISO-TP."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from xcp_pcan.can.transport import CanBusTransport
from xcp_pcan.can_id_presets import UDS_PRESETS
from xcp_pcan.ui.connection_panel import parse_can_id
from xcp_pcan.ui.id_preset_bar import IdPresetBar
from xcp_pcan.diag.isotp import (
    IsoTpClient,
    IsoTpConfig,
    IsoTpError,
    IsoTpTimeout,
    build_sf_wire_frame,
)
from xcp_pcan.diag.service_catalog import (
    DEFAULT_FD,
    DEFAULT_PAD,
    DEFAULT_REQ_ID,
    DEFAULT_RESP_ID,
    FLASHFLOW_SERVICES,
)
from xcp_pcan.diag.uds_decode import UdsEvent, describe_uds
from xcp_pcan.ui.window_util import configure_tool_window

_ROLE_PARAM_ROWS = Qt.ItemDataRole.UserRole + 2

# Qt.UserRole payload on tree / combo items.
_ROLE_HEX = Qt.ItemDataRole.UserRole
_ROLE_PARAMS = Qt.ItemDataRole.UserRole + 1
_ROLE_CUSTOM = Qt.ItemDataRole.UserRole + 3  # bool: user-added entry
_ROLE_KIND = Qt.ItemDataRole.UserRole + 4  # "22" | "2E" | "31"

_GROUP_22 = "Identification (0x22)"
_GROUP_2E = "WriteDataByIdentifier (0x2E)"
_GROUP_31 = "RoutineControl (0x31)"

_TRACE_FONT = QFont("Consolas", 9)


class _NoHighlightDelegate(QStyledItemDelegate):
    """Paint without blue selection fill; keep item colors / use bold for selection."""

    def paint(self, painter, option, index) -> None:  # noqa: ANN001
        opt = QStyleOptionViewItem(option)
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, opt, index)


# One border color for outer frame, header dividers, and body grid — avoids “spliced” edges.
_TABLE_GRID = "#c4c4c4"
_TABLE_HEADER_BG = "#f0f0f0"


def _polish_diag_table(
    table: QTableWidget,
    *,
    alternating: bool = False,
    editable_select: bool = False,
    compact: bool = False,
) -> None:
    """Unified table chrome: single frame, matching header/grid, bold-only selection.

    compact=True (Request/Response): no bottom widget border — last row gridline is
    the bottom edge. Fixed-height + full border + grid otherwise draws two bottom lines.
    """
    table.setFrameShape(QFrame.Shape.NoFrame)
    table.setShowGrid(True)
    table.setAlternatingRowColors(alternating)
    table.setItemDelegate(_NoHighlightDelegate(table))
    if editable_select:
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    hdr = table.horizontalHeader()
    hdr.setHighlightSections(False)
    hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    hdr.setMinimumHeight(26)
    # Compact: top/left/right only; scrolling Trace keeps full box.
    box_border = (
        f"border-top: 1px solid {_TABLE_GRID};"
        f"border-left: 1px solid {_TABLE_GRID};"
        f"border-right: 1px solid {_TABLE_GRID};"
        "border-bottom: none;"
        if compact
        else f"border: 1px solid {_TABLE_GRID};"
    )
    table.setStyleSheet(
        f"""
        QTableWidget {{
            gridline-color: {_TABLE_GRID};
            {box_border}
            background-color: palette(base);
            alternate-background-color: #f7f7f7;
            selection-background-color: transparent;
            selection-color: palette(window-text);
            outline: 0;
        }}
        QHeaderView::section {{
            background-color: {_TABLE_HEADER_BG};
            color: palette(window-text);
            padding: 4px 6px;
            border: none;
            border-right: 1px solid {_TABLE_GRID};
            border-bottom: 1px solid {_TABLE_GRID};
        }}
        QHeaderView::section:last {{
            border-right: none;
        }}
        """
    )
    table.setProperty("diagCompact", compact)
    pal = table.palette()
    pal.setColor(QPalette.ColorRole.Highlight, QColor(0, 0, 0, 0))
    pal.setColor(
        QPalette.ColorRole.HighlightedText,
        pal.color(QPalette.ColorRole.WindowText),
    )
    table.setPalette(pal)


def _configure_bold_select_table(table: QTableWidget) -> None:
    """Selection = bold text only (no Windows blue fill)."""
    _polish_diag_table(table, editable_select=True, compact=True)


def _parse_can_id(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty CAN ID")
    if raw.lower().startswith("0x") or any(c in "ABCDEFabcdef" for c in raw):
        arb = int(raw, 16)
    else:
        arb = int(raw, 16) if len(raw) <= 3 else int(raw, 10)
    if arb < 0 or arb > 0x1FFFFFFF:
        raise ValueError(f"CAN ID out of range: {arb:#x}")
    return arb


def _parse_hex_data(text: str) -> bytes:
    raw = (text or "").strip()
    if not raw:
        return b""
    raw = raw.replace(",", " ").replace(";", " ")
    if " " not in raw and re.fullmatch(r"[0-9A-Fa-f]+", raw):
        if len(raw) % 2:
            raise ValueError("hex length must be even")
        return bytes.fromhex(raw)
    out: list[int] = []
    for p in raw.split():
        if not re.fullmatch(r"[0-9A-Fa-f]{1,2}", p):
            raise ValueError(f"bad byte: {p!r}")
        out.append(int(p, 16))
    return bytes(out)


def _extract_uds_payload(data: bytes) -> bytes:
    """Accept UDS-only or Trace-style SF frames (PCI length + pad)."""
    if not data:
        return data
    pci = data[0]
    if (pci & 0xF0) == 0x00 and pci != 0x00:
        n = pci & 0x0F
        if 1 <= n <= 7 and len(data) >= 1 + n:
            return bytes(data[1 : 1 + n])
    if pci == 0x00 and len(data) >= 2:
        n = int(data[1])
        if 8 <= n <= 62 and len(data) >= 2 + n:
            return bytes(data[2 : 2 + n])
    return bytes(data)


def _hex_spaced(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


class _DiagWorker(QObject):
    event = Signal(object)  # UdsEvent
    finished_ok = Signal(bytes)
    finished_err = Signal(str)

    def __init__(
        self,
        transport: CanBusTransport,
        config: IsoTpConfig,
        payload: bytes,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._transport = transport
        self._config = config
        self._payload = payload
        self._client: IsoTpClient | None = None

    def cancel(self) -> None:
        client = self._client
        if client is not None:
            client.cancel()

    @Slot()
    def run(self) -> None:
        client: IsoTpClient | None = None
        try:
            self.event.emit(describe_uds(self._payload, is_request=True))
            client = IsoTpClient(self._transport, self._config)
            self._client = client
            resp = client.request(self._payload)
            self.event.emit(describe_uds(resp, is_request=False))
            self.finished_ok.emit(resp)
        except IsoTpTimeout as exc:
            self.finished_err.emit(str(exc))
        except (IsoTpError, OSError, RuntimeError, ValueError) as exc:
            self.finished_err.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.finished_err.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self._client = None
            if client is not None:
                client.close()


class DiagConsoleDialog(QDialog):
    """Vector-like diagnostic console: service tree + params + Execute + log."""

    def __init__(
        self,
        settings: QSettings,
        parent: QWidget | None = None,
        *,
        transport: CanBusTransport | None = None,
        on_connect: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Diagnostic Console")
        self.resize(1100, 680)
        configure_tool_window(self)
        self._settings = settings
        self._transport = transport
        self._on_connect = on_connect
        self._pending_after_connect: str | None = None  # "execute" | "tp"
        self._thread: QThread | None = None
        self._worker: _DiagWorker | None = None
        self._busy = False
        self._filling = False
        self._uds_payload: bytes = b""  # true UDS; Raw field may show SF/FF preview
        self._raw_from_user = False
        self._service_title = ""
        self._params_sel_row = -1
        self._resp_sel_row = -1
        self._custom_22: list[str] = []  # DID like "F195"
        self._hidden_22: list[str] = []  # built-in 0x22 DIDs removed from the tree
        self._custom_2e: list[dict[str, str]] = []  # {"did": "...", "data": "..."}
        self._custom_31: list[dict[str, str]] = []  # subfn / routine / data

        # --- Top: addressing + Execute (Vector toolbar strip) ----------------
        self.edt_tx = QLineEdit()
        self.edt_tx.setMaximumWidth(100)
        self.edt_rx = QLineEdit()
        self.edt_rx.setMaximumWidth(100)
        self._uds_preset_bar = IdPresetBar(
            kind="uds",
            builtin=UDS_PRESETS,
            edt_tx=self.edt_tx,
            edt_rx=self.edt_rx,
            settings=self._settings,
            parent=self,
        )
        self._uds_preset_bar.setMinimumWidth(260)
        self.chk_ext = QCheckBox("Ext")
        self.chk_fd = QCheckBox("FD")
        self.chk_fd.toggled.connect(self._refresh_raw_wire)
        self.spn_timeout = QDoubleSpinBox()
        self.spn_timeout.setRange(0.2, 30.0)
        self.spn_timeout.setSingleStep(0.1)
        self.spn_timeout.setSuffix(" s")
        self.spn_timeout.setMaximumWidth(90)
        self.spn_pad = QSpinBox()
        self.spn_pad.setRange(0, 255)
        self.spn_pad.setDisplayIntegerBase(16)
        self.spn_pad.setPrefix("0x")
        self.spn_pad.setMaximumWidth(70)
        self.spn_pad.setValue(DEFAULT_PAD)  # ASC / FlashFlow: pad with 0xCC
        self.spn_pad.setToolTip("ISO-TP unused-byte pad (ASC logs use 0xCC)")
        self.spn_pad.valueChanged.connect(self._refresh_raw_wire)

        self.btn_tp = QPushButton("TesterPresent")
        self.btn_tp.setCheckable(True)
        self.btn_tp.setToolTip("Keep session alive: send 3E 00 periodically (Vector-style).")
        self.btn_tp.toggled.connect(self._on_tp_toggled)

        self.btn_execute = QPushButton("Execute")
        self.btn_execute.setDefault(True)
        self.btn_execute.setMinimumWidth(100)
        self.btn_execute.clicked.connect(self._send)

        self.lbl_online = QLabel("Offline")
        self.lbl_online.setMinimumWidth(70)
        self.lbl_online.setAlignment(Qt.AlignmentFlag.AlignCenter)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel("Target"))
        top.addWidget(self._uds_preset_bar)
        top.addWidget(QLabel("Req"))
        top.addWidget(self.edt_tx)
        top.addWidget(QLabel("Resp"))
        top.addWidget(self.edt_rx)
        top.addWidget(self.chk_ext)
        top.addWidget(self.chk_fd)
        top.addWidget(QLabel("T/O"))
        top.addWidget(self.spn_timeout)
        top.addWidget(QLabel("Pad"))
        top.addWidget(self.spn_pad)
        top.addSpacing(16)
        top.addWidget(self.btn_tp)
        top.addWidget(self.btn_execute)
        top.addWidget(self.lbl_online)
        top.addStretch(1)

        # --- Left: service tree ----------------------------------------------
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(220)
        self.tree.setAnimated(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.itemClicked.connect(self._on_tree_clicked)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self.tree.setToolTip(
            "Click to select · Double-click to Execute\n"
            "Right-click: add DID / Routine; on a 0x22 item: Remove DID"
        )

        # --- Right top: parameters -------------------------------------------
        self.tbl_params = QTableWidget(0, 3)
        self.tbl_params.setHorizontalHeaderLabels(["Name", "Value", "Unit"])
        self.tbl_params.verticalHeader().setVisible(False)
        self.tbl_params.verticalHeader().setDefaultSectionSize(24)
        _configure_bold_select_table(self.tbl_params)
        self.tbl_params.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tbl_params.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_params.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tbl_params.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_params.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.tbl_params.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tbl_params.itemChanged.connect(self._on_param_changed)
        self.tbl_params.itemSelectionChanged.connect(self._on_params_selection)

        self.edt_req = QLineEdit()
        self.edt_req.setFont(QFont("Consolas", 10))
        self.edt_req.setPlaceholderText("On-wire frame e.g. 03 22 F1 87 CC CC CC CC")
        self.edt_req.setToolTip(
            "Complete CAN data that will be sent (ISO-TP PCI + UDS + Pad).\n"
            "Example: UDS 22 F1 87 → 03 22 F1 87 CC CC CC CC"
        )
        self.edt_req.returnPressed.connect(self._send)
        self.edt_req.textEdited.connect(self._on_req_edited)

        param_box = QGroupBox("Request parameters")
        param_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        param_lay = QVBoxLayout(param_box)
        param_lay.setContentsMargins(8, 8, 8, 8)
        param_lay.setSpacing(6)
        param_lay.addWidget(self.tbl_params)
        raw_row = QHBoxLayout()
        raw_row.addWidget(QLabel("Raw (on-wire)"))
        raw_row.addWidget(self.edt_req, stretch=1)
        param_lay.addLayout(raw_row)

        # --- Diagnostic Trace (takes remaining height) -----------------------
        self.tbl_trace = QTableWidget(0, 5)
        self.tbl_trace.setHorizontalHeaderLabels(
            ["Timestamp", "Type", "Service", "Parameters", "Data"]
        )
        self.tbl_trace.verticalHeader().setVisible(False)
        self.tbl_trace.setFont(_TRACE_FONT)
        self.tbl_trace.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_trace.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        _polish_diag_table(self.tbl_trace, alternating=True, editable_select=True)
        th = self.tbl_trace.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._trace_sel_row = -1
        self.tbl_trace.itemSelectionChanged.connect(self._on_trace_selection)

        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear_log)
        trace_box = QGroupBox("Diagnostic Trace")
        trace_lay = QVBoxLayout(trace_box)
        trace_lay.setContentsMargins(8, 8, 8, 8)
        trace_lay.setSpacing(6)
        trace_top = QHBoxLayout()
        trace_top.addWidget(QLabel("Request / response events"), stretch=1)
        trace_top.addWidget(btn_clear)
        trace_lay.addLayout(trace_top)
        trace_lay.addWidget(self.tbl_trace, stretch=1)

        self.tbl_resp = QTableWidget(0, 2)
        self.tbl_resp.setHorizontalHeaderLabels(["Name", "Value"])
        self.tbl_resp.verticalHeader().setVisible(False)
        self.tbl_resp.verticalHeader().setDefaultSectionSize(24)
        self.tbl_resp.setFont(QFont("Consolas", 9))
        self.tbl_resp.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        _configure_bold_select_table(self.tbl_resp)
        self.tbl_resp.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_resp.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tbl_resp.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.tbl_resp.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tbl_resp.itemSelectionChanged.connect(self._on_resp_selection)

        resp_box = QGroupBox("Response parameters")
        resp_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        resp_lay = QVBoxLayout(resp_box)
        resp_lay.setContentsMargins(8, 8, 8, 8)
        resp_lay.setSpacing(6)
        resp_lay.addWidget(self.tbl_resp)

        # Request / Response hug content; Trace fills leftover vertical space.
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)
        right_lay.addWidget(param_box)
        right_lay.addWidget(trace_box, stretch=1)
        right_lay.addWidget(resp_box)
        self._fit_table_to_rows(self.tbl_params)
        self._fit_table_to_rows(self.tbl_resp)

        mid = QSplitter(Qt.Orientation.Horizontal)
        mid.addWidget(self.tree)
        mid.addWidget(right)
        mid.setStretchFactor(0, 0)
        mid.setStretchFactor(1, 1)
        mid.setSizes([260, 780])

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(mid, stretch=1)

        self._tp_timer = QTimer(self)
        self._tp_timer.setInterval(2000)
        self._tp_timer.timeout.connect(self._send_tester_present)

        self._restore_settings()
        self._populate_tree()
        self._refresh_bus_hint()
        # Select first concrete service in the tree
        root = self.tree.topLevelItem(0)
        if root is not None and root.childCount() > 0:
            first = root.child(0)
            self.tree.setCurrentItem(first)
            self._select_tree_service(first)

    # --- Catalog -------------------------------------------------------------

    def _populate_tree(self) -> None:
        self.tree.clear()
        hidden22 = {d.upper() for d in self._hidden_22}
        for group_name, services in FLASHFLOW_SERVICES:
            parent = QTreeWidgetItem([group_name])
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(parent)
            for svc in services:
                if group_name == _GROUP_22:
                    m = re.match(r"22\s+([0-9A-Fa-f]{4})", svc.title)
                    if m and m.group(1).upper() in hidden22:
                        continue
                child = QTreeWidgetItem([svc.title])
                child.setData(0, _ROLE_HEX, svc.uds_hex)
                child.setData(0, _ROLE_PARAMS, svc.params)
                if group_name == _GROUP_22:
                    child.setData(0, _ROLE_KIND, "22")
                elif group_name == _GROUP_2E:
                    child.setData(0, _ROLE_KIND, "2E")
                elif group_name == _GROUP_31:
                    child.setData(0, _ROLE_KIND, "31")
                parent.addChild(child)
            parent.setExpanded(True)
        self._append_custom_dids_to_tree()

    def _find_group(self, title: str) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item is not None and item.text(0) == title:
                return item
        return None

    @staticmethod
    def _normalize_hex_u16(text: str, *, label: str) -> str:
        raw = (text or "").strip().replace(" ", "")
        if raw.lower().startswith("0x"):
            raw = raw[2:]
        if not re.fullmatch(r"[0-9A-Fa-f]{1,4}", raw):
            raise ValueError(f"{label} must be 1–4 hex digits (e.g. F195)")
        return raw.upper().zfill(4)

    @classmethod
    def _normalize_did(cls, text: str) -> str:
        return cls._normalize_hex_u16(text, label="DID")

    @staticmethod
    def _normalize_subfn(text: str) -> str:
        raw = (text or "").strip().replace(" ", "")
        if raw.lower().startswith("0x"):
            raw = raw[2:]
        if not re.fullmatch(r"[0-9A-Fa-f]{1,2}", raw):
            raise ValueError("SubFn must be 1–2 hex digits (e.g. 01)")
        return raw.upper().zfill(2)

    def _append_custom_dids_to_tree(self) -> None:
        g22 = self._find_group(_GROUP_22)
        if g22 is not None:
            for did in self._custom_22:
                self._add_read_did_item(g22, did, custom=True)
        g2e = self._find_group(_GROUP_2E)
        if g2e is not None:
            for entry in self._custom_2e:
                self._add_write_did_item(
                    g2e, entry.get("did", ""), entry.get("data", "00"), custom=True
                )
        g31 = self._find_group(_GROUP_31)
        if g31 is not None:
            for entry in self._custom_31:
                self._add_routine_item(
                    g31,
                    entry.get("subfn", "01"),
                    entry.get("routine", "0000"),
                    entry.get("data", ""),
                    custom=True,
                )

    def _add_read_did_item(
        self, parent: QTreeWidgetItem, did: str, *, custom: bool
    ) -> QTreeWidgetItem:
        did = self._normalize_did(did)
        b0, b1 = int(did[:2], 16), int(did[2:], 16)
        child = QTreeWidgetItem([f"22 {did} — Read DID"])
        child.setData(0, _ROLE_HEX, f"22 {b0:02X} {b1:02X}")
        child.setData(0, _ROLE_PARAMS, (("DID", did, "hex"),))
        if custom:
            child.setData(0, _ROLE_CUSTOM, True)
            child.setData(0, _ROLE_KIND, "22")
        parent.addChild(child)
        return child

    def _add_write_did_item(
        self, parent: QTreeWidgetItem, did: str, data_hex: str, *, custom: bool
    ) -> QTreeWidgetItem:
        did = self._normalize_did(did)
        b0, b1 = int(did[:2], 16), int(did[2:], 16)
        data = _parse_hex_data(data_hex) if (data_hex or "").strip() else b"\x00"
        data_spaced = _hex_spaced(data)
        uds = f"2E {b0:02X} {b1:02X} " + data_spaced
        child = QTreeWidgetItem([f"2E {did} — Write DID"])
        child.setData(0, _ROLE_HEX, uds.strip())
        child.setData(
            0,
            _ROLE_PARAMS,
            (("DID", did, "hex"), ("Data", data_spaced, "hex")),
        )
        if custom:
            child.setData(0, _ROLE_CUSTOM, True)
            child.setData(0, _ROLE_KIND, "2E")
        parent.addChild(child)
        return child

    def _add_routine_item(
        self,
        parent: QTreeWidgetItem,
        subfn: str,
        routine: str,
        data_hex: str,
        *,
        custom: bool,
    ) -> QTreeWidgetItem:
        subfn = self._normalize_subfn(subfn)
        routine = self._normalize_hex_u16(routine, label="Routine")
        r0, r1 = int(routine[:2], 16), int(routine[2:], 16)
        data = _parse_hex_data(data_hex) if (data_hex or "").strip() else b""
        data_spaced = _hex_spaced(data)
        uds = f"31 {subfn} {r0:02X} {r1:02X}"
        if data_spaced:
            uds = f"{uds} {data_spaced}"
        params: list[tuple[str, str, str]] = [
            ("SubFn", subfn, "hex"),
            ("Routine", routine, "hex"),
        ]
        if data_spaced or custom:
            params.append(("Data", data_spaced, "hex"))
        child = QTreeWidgetItem([f"31 {subfn} {routine} — StartRoutine"])
        child.setData(0, _ROLE_HEX, uds)
        child.setData(0, _ROLE_PARAMS, tuple(params))
        if custom:
            child.setData(0, _ROLE_CUSTOM, True)
            child.setData(0, _ROLE_KIND, "31")
        parent.addChild(child)
        return child

    def _on_tree_context_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        act_rw = menu.addAction("Add DID (Read + Write)…")
        act_add22 = menu.addAction("Add Read DID only (0x22)…")
        act_add2e = menu.addAction("Add Write DID only (0x2E)…")
        menu.addSeparator()
        act_add31 = menu.addAction("Add Routine (0x31)…")
        act_remove = None
        act_remove22 = None
        if item is not None and item.data(0, _ROLE_HEX) is not None:
            kind = item.data(0, _ROLE_KIND)
            if kind == "22" or str(item.text(0)).upper().startswith("22 "):
                menu.addSeparator()
                act_remove22 = menu.addAction("Remove DID")
            elif bool(item.data(0, _ROLE_CUSTOM)):
                menu.addSeparator()
                act_remove = menu.addAction("Remove custom entry")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_rw:
            self._prompt_add_did_rw()
        elif chosen == act_add22:
            self._prompt_add_read_did()
        elif chosen == act_add2e:
            self._prompt_add_write_did()
        elif chosen == act_add31:
            self._prompt_add_routine()
        elif act_remove22 is not None and chosen == act_remove22 and item is not None:
            self._remove_read_did_item(item)
        elif act_remove is not None and chosen == act_remove and item is not None:
            self._remove_custom_did_item(item)

    def _ensure_custom_read(self, did: str) -> QTreeWidgetItem | None:
        """Add 0x22 entry if missing; return new child or None if already listed."""
        if self._tree_has_title_prefix(f"22 {did}"):
            return None
        parent = self._find_group(_GROUP_22)
        if parent is None:
            return None
        # Restoring a previously removed built-in DID — not a "custom" row.
        if did in self._hidden_22:
            self._hidden_22 = [d for d in self._hidden_22 if d != did]
            child = self._add_read_did_item(parent, did, custom=False)
            child.setData(0, _ROLE_KIND, "22")
            parent.setExpanded(True)
            return child
        if did not in self._custom_22:
            self._custom_22.append(did)
        child = self._add_read_did_item(parent, did, custom=True)
        parent.setExpanded(True)
        return child

    def _ensure_custom_write(self, did: str, data_spaced: str) -> QTreeWidgetItem | None:
        if self._tree_has_title_prefix(f"2E {did}"):
            return None
        parent = self._find_group(_GROUP_2E)
        if parent is None:
            return None
        if not any(e.get("did") == did for e in self._custom_2e):
            self._custom_2e.append({"did": did, "data": data_spaced})
        child = self._add_write_did_item(parent, did, data_spaced, custom=True)
        parent.setExpanded(True)
        return child

    def _prompt_add_did_rw(self) -> None:
        """One DID → both 0x22 Read and 0x2E Write (write data asked once)."""
        did_text, ok = QInputDialog.getText(
            self, "Add DID (Read + Write)", "DID (hex, e.g. F195):"
        )
        if not ok or not did_text.strip():
            return
        data_text, ok2 = QInputDialog.getText(
            self,
            "Add DID (Read + Write)",
            "Write Data for 0x2E (hex, e.g. 00):",
            text="00",
        )
        if not ok2:
            return
        try:
            did = self._normalize_did(did_text)
            data = _parse_hex_data(data_text) if data_text.strip() else b"\x00"
        except ValueError as exc:
            QMessageBox.warning(self, "Add DID (Read + Write)", str(exc))
            return
        data_spaced = _hex_spaced(data)
        child22 = self._ensure_custom_read(did)
        child2e = self._ensure_custom_write(did, data_spaced)
        if child22 is None and child2e is None:
            QMessageBox.information(
                self, "Add DID (Read + Write)", f"DID {did} already listed for 22 and 2E."
            )
            return
        pick = child22 or child2e
        if pick is not None:
            self.tree.setCurrentItem(pick)
            self._select_tree_service(pick)
        self._save_custom_dids()

    def _prompt_add_read_did(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Add Read DID", "DID (hex, e.g. F195 or 0xF195):"
        )
        if not ok or not text.strip():
            return
        try:
            did = self._normalize_did(text)
        except ValueError as exc:
            QMessageBox.warning(self, "Add Read DID", str(exc))
            return
        child = self._ensure_custom_read(did)
        if child is None:
            QMessageBox.information(self, "Add Read DID", f"DID {did} already listed.")
            return
        self.tree.setCurrentItem(child)
        self._select_tree_service(child)
        self._save_custom_dids()

    def _prompt_add_write_did(self) -> None:
        did_text, ok = QInputDialog.getText(
            self, "Add Write DID", "DID (hex, e.g. F184):"
        )
        if not ok or not did_text.strip():
            return
        data_text, ok2 = QInputDialog.getText(
            self,
            "Add Write DID",
            "Data (hex bytes, e.g. 00 or 11 22 33):",
            text="00",
        )
        if not ok2:
            return
        try:
            did = self._normalize_did(did_text)
            data = _parse_hex_data(data_text) if data_text.strip() else b"\x00"
        except ValueError as exc:
            QMessageBox.warning(self, "Add Write DID", str(exc))
            return
        child = self._ensure_custom_write(did, _hex_spaced(data))
        if child is None:
            QMessageBox.information(self, "Add Write DID", f"DID {did} already listed.")
            return
        self.tree.setCurrentItem(child)
        self._select_tree_service(child)
        self._save_custom_dids()

    def _prompt_add_routine(self) -> None:
        routine_text, ok = QInputDialog.getText(
            self, "Add Routine (0x31)", "Routine ID (hex, e.g. FF00 or 0203):"
        )
        if not ok or not routine_text.strip():
            return
        sub_text, ok2 = QInputDialog.getText(
            self, "Add Routine (0x31)", "Sub-function (hex):", text="01"
        )
        if not ok2:
            return
        data_text, ok3 = QInputDialog.getText(
            self,
            "Add Routine (0x31)",
            "Optional routine data (hex, empty allowed):",
            text="",
        )
        if not ok3:
            return
        try:
            routine = self._normalize_hex_u16(routine_text, label="Routine")
            subfn = self._normalize_subfn(sub_text or "01")
            data = _parse_hex_data(data_text) if data_text.strip() else b""
        except ValueError as exc:
            QMessageBox.warning(self, "Add Routine (0x31)", str(exc))
            return
        data_spaced = _hex_spaced(data)
        prefix = f"31 {subfn} {routine}"
        if self._tree_has_title_prefix(prefix):
            QMessageBox.information(
                self, "Add Routine (0x31)", f"{prefix} already listed."
            )
            return
        parent = self._find_group(_GROUP_31)
        if parent is None:
            return
        entry = {"subfn": subfn, "routine": routine, "data": data_spaced}
        self._custom_31.append(entry)
        child = self._add_routine_item(
            parent, subfn, routine, data_spaced, custom=True
        )
        parent.setExpanded(True)
        self.tree.setCurrentItem(child)
        self._select_tree_service(child)
        self._save_custom_dids()

    def _tree_has_title_prefix(self, prefix: str) -> bool:
        needle = prefix.upper()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            if group is None:
                continue
            for j in range(group.childCount()):
                child = group.child(j)
                if child is not None and child.text(0).upper().startswith(needle):
                    return True
        return False

    def _remove_read_did_item(self, item: QTreeWidgetItem) -> None:
        """Remove a 0x22 DID from the tree (built-in → hidden; custom → dropped)."""
        title = item.text(0)
        m = re.match(r"22\s+([0-9A-Fa-f]{4})", title)
        if not m:
            return
        did = m.group(1).upper()
        was_custom = bool(item.data(0, _ROLE_CUSTOM))
        self._custom_22 = [d for d in self._custom_22 if d != did]
        if not was_custom and did not in self._hidden_22:
            self._hidden_22.append(did)
        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
        self._save_custom_dids()

    def _remove_custom_did_item(self, item: QTreeWidgetItem) -> None:
        kind = item.data(0, _ROLE_KIND)
        title = item.text(0)
        if kind == "22":
            self._remove_read_did_item(item)
            return
        if kind == "2E":
            m = re.match(r"2E\s+([0-9A-Fa-f]{4})", title)
            if m:
                did = m.group(1).upper()
                self._custom_2e = [e for e in self._custom_2e if e.get("did") != did]
        elif kind == "31":
            m = re.match(
                r"31\s+([0-9A-Fa-f]{2})\s+([0-9A-Fa-f]{4})", title, re.IGNORECASE
            )
            if m:
                subfn, routine = m.group(1).upper(), m.group(2).upper()
                self._custom_31 = [
                    e
                    for e in self._custom_31
                    if not (
                        e.get("subfn") == subfn and e.get("routine") == routine
                    )
                ]
        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
        self._save_custom_dids()

    def _save_custom_dids(self) -> None:
        self._settings.setValue(
            "diag_console/custom_did_22", json.dumps(self._custom_22)
        )
        self._settings.setValue(
            "diag_console/hidden_did_22", json.dumps(self._hidden_22)
        )
        self._settings.setValue(
            "diag_console/custom_did_2e", json.dumps(self._custom_2e)
        )
        self._settings.setValue(
            "diag_console/custom_routine_31", json.dumps(self._custom_31)
        )

    def _load_custom_dids(self) -> None:
        self._custom_22 = []
        self._hidden_22 = []
        self._custom_2e = []
        self._custom_31 = []
        raw22 = self._settings.value("diag_console/custom_did_22", "[]", str)
        raw_hide = self._settings.value("diag_console/hidden_did_22", "[]", str)
        raw2e = self._settings.value("diag_console/custom_did_2e", "[]", str)
        raw31 = self._settings.value("diag_console/custom_routine_31", "[]", str)
        try:
            data22 = json.loads(raw22 or "[]")
            if isinstance(data22, list):
                for item in data22:
                    try:
                        self._custom_22.append(self._normalize_did(str(item)))
                    except ValueError:
                        continue
        except json.JSONDecodeError:
            pass
        try:
            data_hide = json.loads(raw_hide or "[]")
            if isinstance(data_hide, list):
                for item in data_hide:
                    try:
                        self._hidden_22.append(self._normalize_did(str(item)))
                    except ValueError:
                        continue
        except json.JSONDecodeError:
            pass
        try:
            data2e = json.loads(raw2e or "[]")
            if isinstance(data2e, list):
                for item in data2e:
                    if not isinstance(item, dict):
                        continue
                    try:
                        did = self._normalize_did(str(item.get("did", "")))
                        data = str(item.get("data", "00"))
                        _parse_hex_data(data)
                        self._custom_2e.append({"did": did, "data": data})
                    except ValueError:
                        continue
        except json.JSONDecodeError:
            pass
        try:
            data31 = json.loads(raw31 or "[]")
            if isinstance(data31, list):
                for item in data31:
                    if not isinstance(item, dict):
                        continue
                    try:
                        subfn = self._normalize_subfn(str(item.get("subfn", "01")))
                        routine = self._normalize_hex_u16(
                            str(item.get("routine", "")), label="Routine"
                        )
                        data = str(item.get("data", ""))
                        if data.strip():
                            _parse_hex_data(data)
                        self._custom_31.append(
                            {"subfn": subfn, "routine": routine, "data": data}
                        )
                    except ValueError:
                        continue
        except json.JSONDecodeError:
            pass

    # --- Public API ----------------------------------------------------------

    def set_transport(self, transport: CanBusTransport | None) -> None:
        self._transport = transport
        self._refresh_bus_hint()

    def set_on_connect(self, on_connect: Callable[[], None] | None) -> None:
        self._on_connect = on_connect

    def notify_bus_open(self) -> None:
        """Main window finished Connect — resume Execute / TesterPresent if requested."""
        action = self._pending_after_connect
        self._pending_after_connect = None
        self._refresh_bus_hint()
        if action == "execute":
            self._send()
        elif action == "tp" and not self.btn_tp.isChecked():
            self.btn_tp.setChecked(True)

    def clear_pending_after_connect(self) -> None:
        self._pending_after_connect = None

    def notify_bus_closed(self) -> None:
        self._pending_after_connect = None
        if self.btn_tp.isChecked():
            self.btn_tp.setChecked(False)
        self._append_info("Bus closed — Connect again before Execute.")
        self._refresh_bus_hint()

    def persist_settings(self) -> None:
        self._settings.setValue("diag_console/tx_id", self.edt_tx.text().strip())
        self._settings.setValue("diag_console/rx_id", self.edt_rx.text().strip())
        self._settings.setValue("diag_console/ext", self.chk_ext.isChecked())
        self._settings.setValue("diag_console/fd", self.chk_fd.isChecked())
        self._settings.setValue("diag_console/timeout", self.spn_timeout.value())
        self._settings.setValue("diag_console/pad", self.spn_pad.value())
        self._settings.setValue("diag_console/last_req", self.edt_req.text())
        self._save_custom_dids()

    def _restore_settings(self) -> None:
        self._load_custom_dids()
        # Default UDS FL (0x725 / 0x7A5); unmatched saved IDs show as Custom.
        self.edt_tx.setText(self._settings.value("diag_console/tx_id", DEFAULT_REQ_ID, str))
        self.edt_rx.setText(self._settings.value("diag_console/rx_id", DEFAULT_RESP_ID, str))
        self._uds_preset_bar.sync_from_fields()
        ext = self._settings.value("diag_console/ext", False)
        self.chk_ext.setChecked(
            ext.strip().lower() in {"1", "true", "yes"} if isinstance(ext, str) else bool(ext)
        )
        fd = self._settings.value("diag_console/fd", DEFAULT_FD)
        self.chk_fd.setChecked(
            fd.strip().lower() in {"1", "true", "yes"} if isinstance(fd, str) else bool(fd)
        )
        try:
            self.spn_timeout.setValue(float(self._settings.value("diag_console/timeout", 2.0)))
        except (TypeError, ValueError):
            self.spn_timeout.setValue(2.0)
        try:
            pad = int(self._settings.value("diag_console/pad", DEFAULT_PAD))
        except (TypeError, ValueError):
            pad = DEFAULT_PAD
        # Older builds left the spinbox at 0 and persisted it; FlashFlow ASC uses 0xCC.
        if pad == 0:
            pad = DEFAULT_PAD
        self.spn_pad.setValue(pad)
        last = self._settings.value("diag_console/last_req", "", str)
        if last:
            # Normalize saved value to on-wire form when possible.
            try:
                uds = _extract_uds_payload(_parse_hex_data(last))
                if uds and (uds[0] & 0xF0) != 0x10:
                    self._set_raw_wire(uds)
                else:
                    self.edt_req.setText(last)
                    self._raw_from_user = True
            except ValueError:
                self.edt_req.setText(last)
                self._raw_from_user = True
        else:
            self._set_raw_wire(bytes([0x22, 0xF1, 0x87]))

    def _refresh_bus_hint(self) -> None:
        t = self._transport
        online = t is not None and t.is_open
        if online:
            mode = "FD" if t.fd else "Classic"
            self.lbl_online.setText(f"Online·{mode}")
            self.lbl_online.setStyleSheet(
                "background:#2e7d32;color:white;padding:2px 8px;border-radius:3px;"
            )
        else:
            self.lbl_online.setText("Offline")
            self.lbl_online.setStyleSheet(
                "background:#757575;color:white;padding:2px 8px;border-radius:3px;"
            )

    # --- Selection / params --------------------------------------------------

    def _on_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self._select_tree_service(item)

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """Vector-like: double-click a leaf service to Execute immediately."""
        if not self._select_tree_service(item):
            return
        self._send()

    def _select_tree_service(self, item: QTreeWidgetItem) -> bool:
        uds_hex = item.data(0, _ROLE_HEX)
        if uds_hex is None:
            return False  # group header
        params = item.data(0, _ROLE_PARAMS) or ()
        self._service_title = item.text(0)
        self._apply_service(str(uds_hex), tuple(params))
        return True

    @staticmethod
    def _fit_table_to_rows(
        table: QTableWidget, *, min_rows: int = 1, max_rows: int = 6
    ) -> None:
        """Size table height to its rows so leftover space stays with Trace."""
        n = max(min_rows, min(max(table.rowCount(), 0), max_rows))
        header = table.horizontalHeader()
        header_h = header.height() if header.height() > 0 else header.sizeHint().height()
        row_h = table.verticalHeader().defaultSectionSize()
        # Stylesheet top border (compact) or top+bottom (full).
        border = 1 if bool(table.property("diagCompact")) else 2
        table.setFixedHeight(header_h + n * row_h + border)

    def _apply_service(self, uds_hex: str, params: tuple) -> None:
        self._filling = True
        self.tbl_params.blockSignals(True)
        self.tbl_params.setRowCount(0)
        self._params_sel_row = -1
        for name, value, unit in params:
            row = self.tbl_params.rowCount()
            self.tbl_params.insertRow(row)
            self.tbl_params.setItem(row, 0, QTableWidgetItem(str(name)))
            self.tbl_params.setItem(row, 1, QTableWidgetItem(str(value)))
            unit_item = QTableWidgetItem(str(unit))
            unit_item.setFlags(unit_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.tbl_params.setItem(row, 2, unit_item)
            name_item = self.tbl_params.item(row, 0)
            if name_item is not None:
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.tbl_params.blockSignals(False)
        self._fit_table_to_rows(self.tbl_params)
        self._filling = False
        if uds_hex:
            try:
                self._set_raw_wire(_parse_hex_data(uds_hex))
            except ValueError:
                self.edt_req.setText(uds_hex)
        elif params:
            for name, value, _unit in params:
                if str(name).lower() == "request":
                    try:
                        raw = _parse_hex_data(str(value))
                        self._set_raw_wire(_extract_uds_payload(raw) or raw)
                    except ValueError:
                        self.edt_req.setText(str(value))
                    break
        self._rebuild_req_from_params()

    def _on_param_changed(self, _item: QTableWidgetItem) -> None:
        if self._filling:
            return
        self._rebuild_req_from_params()

    def _on_params_selection(self) -> None:
        self._params_sel_row = self._sync_row_bold(
            self.tbl_params, self._params_sel_row
        )

    def _on_resp_selection(self) -> None:
        self._resp_sel_row = self._sync_row_bold(self.tbl_resp, self._resp_sel_row)

    def _sync_row_bold(self, table: QTableWidget, prev_row: int) -> int:
        rows = table.selectionModel().selectedRows()
        new_row = rows[0].row() if rows else -1
        if new_row == prev_row:
            return prev_row
        self._set_table_row_bold(table, prev_row, False)
        self._set_table_row_bold(table, new_row, True)
        return new_row

    @staticmethod
    def _set_table_row_bold(table: QTableWidget, row: int, bold: bool) -> None:
        if row < 0:
            return
        font = QFont(table.font())
        font.setBold(bold)
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item is not None:
                item.setFont(font)

    def _on_req_edited(self, _text: str) -> None:
        # User typed on-wire / UDS hex — Execute will parse the Raw field.
        self._raw_from_user = True

    def _use_fd_for_preview(self) -> bool:
        want = self.chk_fd.isChecked()
        t = self._transport
        if t is not None and t.is_open:
            return want and bool(t.fd)
        return want

    def _set_raw_wire(self, uds: bytes) -> None:
        """Remember UDS and show SF/FF preview in Raw (Execute uses UDS, not preview)."""
        self._uds_payload = bytes(uds)
        self._raw_from_user = False
        if not uds:
            self.edt_req.clear()
            return
        wire = build_sf_wire_frame(
            uds,
            pad_byte=int(self.spn_pad.value()) & 0xFF,
            min_frame_len=8,
            fd=self._use_fd_for_preview(),
        )
        self.edt_req.setText(_hex_spaced(wire))

    def _refresh_raw_wire(self, *_args) -> None:
        """Pad/FD changed — rewrite Raw from remembered UDS (not from FF preview)."""
        if self._filling:
            return
        if self._uds_payload and not self._raw_from_user:
            self._set_raw_wire(self._uds_payload)
            return
        try:
            raw = _parse_hex_data(self.edt_req.text())
            uds = _extract_uds_payload(raw) if raw else b""
            # Ignore truncated First Frame previews (0x1N …) — not a UDS payload.
            if uds and (uds[0] & 0xF0) == 0x10:
                return
            if uds:
                self._set_raw_wire(uds)
        except ValueError:
            pass

    def _param_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in range(self.tbl_params.rowCount()):
            n = self.tbl_params.item(row, 0)
            v = self.tbl_params.item(row, 1)
            if n is None or v is None:
                continue
            out[n.text().strip().lower()] = v.text().strip()
        return out

    def _rebuild_req_from_params(self) -> None:
        """Rebuild on-wire Raw from parameter table when structure is known."""
        if self._filling:
            return
        p = self._param_map()
        title = self._service_title
        try:
            uds: bytes | None = None
            if "10 01" in title or "10 02" in title or "10 03" in title:
                sess = _parse_hex_data(p.get("session", "01"))
                if sess:
                    uds = bytes([0x10, sess[0]])
            elif title.startswith("22 "):
                did = _parse_hex_data(p.get("did", "F187"))
                if len(did) >= 2:
                    uds = bytes([0x22, did[0], did[1]])
            elif title.startswith("2E"):
                did = _parse_hex_data(p.get("did", "F187"))
                data = _parse_hex_data(p.get("data", "00"))
                if len(did) >= 2:
                    uds = bytes([0x2E, did[0], did[1]]) + data
            elif title.startswith("27 "):
                lvl = _parse_hex_data(p.get("level", "11"))
                key = _parse_hex_data(p.get("key", ""))
                if lvl:
                    uds = bytes([0x27, lvl[0]]) + key
            elif title.startswith("31 "):
                sub = _parse_hex_data(p.get("subfn", "01"))
                routine = _parse_hex_data(p.get("routine", "0203"))
                data = _parse_hex_data(p.get("data", ""))
                if sub and len(routine) >= 2:
                    uds = bytes([0x31, sub[0], routine[0], routine[1]]) + data
                elif sub and routine:
                    uds = bytes([0x31, sub[0]]) + routine + data
            elif title.startswith("34"):
                fmt = _parse_hex_data(p.get("dataformat", "00"))
                alfi = _parse_hex_data(p.get("addrlenformat", "44"))
                addr = _parse_hex_data(p.get("address/size", ""))
                uds = bytes([0x34]) + (fmt[:1] or b"\x00") + (alfi[:1] or b"\x44") + addr
            elif title.startswith("36"):
                blk = _parse_hex_data(p.get("blockcounter", "01"))
                data = _parse_hex_data(p.get("data", "00"))
                if blk:
                    uds = bytes([0x36, blk[0]]) + data
            elif title.startswith("37"):
                uds = bytes([0x37])
            elif title.startswith("11 "):
                rt = _parse_hex_data(p.get("resettype", "01"))
                if rt:
                    uds = bytes([0x11, rt[0]])
            elif title.startswith("3E"):
                sub = _parse_hex_data(p.get("subfn", "00"))
                if sub:
                    uds = bytes([0x3E, sub[0]])
            elif "[Raw]" in title:
                req = p.get("request", "")
                if req:
                    raw = _parse_hex_data(req)
                    uds = _extract_uds_payload(raw) or raw
            if uds is not None:
                self._set_raw_wire(uds)
        except ValueError:
            pass

    # --- TesterPresent -------------------------------------------------------

    def _on_tp_toggled(self, enabled: bool) -> None:
        if enabled:
            if self._transport is None or not self._transport.is_open:
                self.btn_tp.blockSignals(True)
                self.btn_tp.setChecked(False)
                self.btn_tp.blockSignals(False)
                self._prompt_open_bus(
                    "TesterPresent needs an open CAN bus.\n"
                    "XCP session is optional.",
                    pending="tp",
                )
                return
            self._tp_timer.start()
            self._append_info("TesterPresent keep-alive ON (3E 00 every 2s)")
            self._send_tester_present()
        else:
            self._tp_timer.stop()
            self._append_info("TesterPresent keep-alive OFF")

    def _send_tester_present(self) -> None:
        if self._transport is None or not self._transport.is_open:
            self.btn_tp.setChecked(False)
            return
        # During Execute, send suppressPosRsp keep-alive (no wait) so the session
        # does not drop while ISO-TP is busy on a long request.
        if self._busy:
            self._send_tp_keepalive_raw()
            return
        self._start_request(bytes([0x3E, 0x00]), quiet_info=True)

    def _send_tp_keepalive_raw(self) -> None:
        """One-shot 3E 80 SF — no response expected; safe alongside Execute."""
        if self._transport is None or not self._transport.is_open:
            return
        try:
            cfg = self._build_config()
            uds = bytes([0x3E, 0x80])
            wire = build_sf_wire_frame(
                uds,
                pad_byte=cfg.pad_byte,
                min_frame_len=8,
                fd=bool(cfg.fd),
            )
            self._transport.send(
                cfg.tx_id,
                wire,
                is_extended_id=cfg.is_extended_id,
                fd=bool(cfg.fd),
                bitrate_switch=bool(cfg.fd),
            )
        except Exception:  # noqa: BLE001
            pass

    # --- Diagnostic Trace ----------------------------------------------------

    def _append_info(self, message: str) -> None:
        self._append_event(
            UdsEvent("Info", "", message, "", ())
        )

    def _append_event(self, ev: UdsEvent) -> None:
        ts = time.strftime("%H:%M:%S")
        row = self.tbl_trace.rowCount()
        self.tbl_trace.insertRow(row)
        values = (ts, ev.kind, ev.service, ev.parameters, ev.data_hex)
        color = {
            "Request": QColor("#1565c0"),
            "Positive Response": QColor("#2e7d32"),
            "Negative Response": QColor("#c62828"),
            "Error": QColor("#c62828"),
            "Info": QColor("#616161"),
        }.get(ev.kind, QColor("#212121"))
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setForeground(QBrush(color))
            if col == 0:
                item.setData(_ROLE_PARAM_ROWS, list(ev.param_rows))
            self.tbl_trace.setItem(row, col, item)
        self.tbl_trace.selectRow(row)
        self.tbl_trace.scrollToBottom()
        self._show_param_rows(ev.param_rows)

    def _on_trace_selection(self) -> None:
        rows = self.tbl_trace.selectionModel().selectedRows()
        new_row = rows[0].row() if rows else -1
        if new_row != self._trace_sel_row:
            self._set_trace_row_bold(self._trace_sel_row, False)
            self._set_trace_row_bold(new_row, True)
            self._trace_sel_row = new_row
        if new_row < 0:
            return
        item = self.tbl_trace.item(new_row, 0)
        if item is None:
            return
        raw = item.data(_ROLE_PARAM_ROWS)
        if isinstance(raw, list):
            self._show_param_rows(tuple(raw))

    def _set_trace_row_bold(self, row: int, bold: bool) -> None:
        if row < 0:
            return
        font = QFont(_TRACE_FONT)
        font.setBold(bold)
        for col in range(self.tbl_trace.columnCount()):
            item = self.tbl_trace.item(row, col)
            if item is not None:
                item.setFont(font)

    def _show_param_rows(self, rows: tuple[tuple[str, str], ...] | list) -> None:
        self.tbl_resp.setRowCount(0)
        self._resp_sel_row = -1
        for name, value in rows:
            r = self.tbl_resp.rowCount()
            self.tbl_resp.insertRow(r)
            self.tbl_resp.setItem(r, 0, QTableWidgetItem(str(name)))
            self.tbl_resp.setItem(r, 1, QTableWidgetItem(str(value)))
        self._fit_table_to_rows(self.tbl_resp)

    def _clear_log(self) -> None:
        self.tbl_trace.setRowCount(0)
        self.tbl_resp.setRowCount(0)
        self._trace_sel_row = -1
        self._resp_sel_row = -1
        self._fit_table_to_rows(self.tbl_resp)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_execute.setEnabled(not busy)
        if not self.btn_tp.isChecked():
            self.edt_req.setEnabled(not busy)

    def _build_config(self) -> IsoTpConfig:
        tx = _parse_can_id(self.edt_tx.text())
        rx = _parse_can_id(self.edt_rx.text())
        want_fd = self.chk_fd.isChecked()
        if self._transport is not None and self._transport.is_open:
            # Never emit FD frames on a Classic session (python-can / HW reject or bus errors).
            use_fd: bool | None = want_fd and bool(self._transport.fd)
        else:
            use_fd = want_fd
        return IsoTpConfig(
            tx_id=tx,
            rx_id=rx,
            is_extended_id=self.chk_ext.isChecked(),
            fd=use_fd,
            pad_byte=int(self.spn_pad.value()) & 0xFF,
            timeout_s=float(self.spn_timeout.value()),
        )

    def _prompt_open_bus(self, message: str, *, pending: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(self.windowTitle())
        box.setText(message)
        btn_connect = box.addButton("Connect", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(btn_connect)
        box.exec()
        if box.clickedButton() is not btn_connect:
            return
        if self._on_connect is None:
            QMessageBox.information(
                self,
                self.windowTitle(),
                "Connect is not available here — use Connect on the main window.",
            )
            return
        self._pending_after_connect = pending
        self._on_connect()

    def _send(self) -> None:
        if self._busy:
            return
        if self._transport is None or not self._transport.is_open:
            self._prompt_open_bus(
                "CAN bus is not open.\n"
                "XCP session is optional — Diagnostic Console only needs the bus.",
                pending="execute",
            )
            return
        try:
            if not self._raw_from_user and self._uds_payload:
                payload = self._uds_payload
            else:
                raw = _parse_hex_data(self.edt_req.text())
                if not raw:
                    raise ValueError("empty request")
                payload = _extract_uds_payload(raw)
                if not payload:
                    raise ValueError("empty UDS payload after ISO-TP SF parse")
                # FF preview in Raw is truncated — sending it as UDS double-wraps PCI.
                if (payload[0] & 0xF0) == 0x10:
                    raise ValueError(
                        "Raw looks like an ISO-TP First Frame preview.\n"
                        "Use the parameter table (or paste full UDS without PCI), then Execute."
                    )
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        if self._uds_rx_overlaps_xcp_slave():
            parent = self.parent()
            daq = getattr(parent, "_daq", None) if parent is not None else None
            if daq is not None and getattr(daq, "is_running", False):
                QMessageBox.warning(
                    self,
                    self.windowTitle(),
                    "Resp ID matches XCP Slave RX ID while DAQ is running.\n"
                    "Stop DAQ or change Resp ID — UDS responses would be treated as DTO.",
                )
                return
            self._append_info(
                "Note: Resp ID equals XCP Slave RX ID — prefer distinct IDs if using DAQ."
            )
        self._pending_after_connect = None
        self.persist_settings()
        self._start_request(payload)

    def _uds_rx_overlaps_xcp_slave(self) -> bool:
        parent = self.parent()
        connection = getattr(parent, "connection", None) if parent is not None else None
        if connection is None:
            return False
        try:
            xcp_rx = parse_can_id(connection.collect_connection().slave_can_id)
            diag_rx = parse_can_id(self.edt_rx.text())
        except (TypeError, ValueError, AttributeError):
            return False
        return xcp_rx == diag_rx

    def _start_request(self, payload: bytes, *, quiet_info: bool = False) -> None:
        try:
            cfg = self._build_config()
        except ValueError as exc:
            if not quiet_info:
                QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self._set_busy(True)
        self._refresh_bus_hint()
        thread = QThread(self)
        worker = _DiagWorker(self._transport, cfg, payload)  # type: ignore[arg-type]
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.event.connect(self._append_event)
        worker.finished_ok.connect(self._on_ok)
        worker.finished_err.connect(self._on_err)
        worker.finished_ok.connect(thread.quit)
        worker.finished_err.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot(bytes)
    def _on_ok(self, _resp: bytes) -> None:
        pass

    @Slot(str)
    def _on_err(self, message: str) -> None:
        self._append_event(UdsEvent("Error", "", message, "", (("Error", message),)))

    @Slot()
    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._set_busy(False)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._tp_timer.stop()
        worker = self._worker
        if worker is not None:
            worker.cancel()
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5000)
        try:
            self.persist_settings()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
