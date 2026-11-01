"""Simple collapsible section for packing advanced options."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QStyle,
    QStyleOptionGroupBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class CollapsibleSection(QWidget):
    """Header + body that can expand/collapse.

    framed=True draws a native group-box wireframe (same as Symbols), with the
    title sitting on the top border. Click the title to toggle. No checkable
    tool button, so the expanded state does not pick up a pressed-button shadow.
    """

    toggled = Signal(bool)

    def __init__(
        self,
        title: str = "高级配置",
        *,
        expanded: bool = False,
        indent: int = 8,
        framed: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._framed = framed
        self._indent = indent
        self._expanded = expanded

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        if framed:
            self._frame = QGroupBox(self._frame_title(expanded))
            self._frame.installEventFilter(self)
            layout.addWidget(self._frame)
            host = QVBoxLayout(self._frame)
            self._btn = None
        else:
            self._frame = None
            self._btn = QToolButton()
            self._btn.setText(title)
            self._btn.setCheckable(True)
            self._btn.setChecked(expanded)
            self._btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._btn.setArrowType(
                Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
            )
            self._btn.setAutoRaise(True)
            # Checked state must stay flat — the default pressed look reads as a shadow.
            self._btn.setStyleSheet(
                "QToolButton {"
                " font-weight: 600;"
                " border: none;"
                " background: transparent;"
                " padding: 2px 4px;"
                "}"
                "QToolButton:hover { background: transparent; }"
                "QToolButton:checked { background: transparent; border: none; }"
                "QToolButton:pressed { background: transparent; border: none; }"
            )
            self._btn.toggled.connect(self._on_toggled)
            layout.addWidget(self._btn)
            host = layout

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(self._indent, 0, 0, 0)
        self._body_layout.setSpacing(6)
        self._body.setVisible(expanded)
        host.addWidget(self._body)

    @property
    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    def add_widget(self, widget: QWidget) -> None:
        self._body_layout.addWidget(widget)

    def set_expanded(self, expanded: bool) -> None:
        if self._btn is not None:
            self._btn.setChecked(expanded)
            return
        if expanded == self._expanded:
            return
        self._apply_expanded(expanded)
        self.toggled.emit(expanded)

    def is_expanded(self) -> bool:
        if self._btn is not None:
            return self._btn.isChecked()
        return self._expanded

    def set_title(self, title: str) -> None:
        self._title = title
        if self._btn is not None:
            self._btn.setText(title)
        elif self._frame is not None:
            self._frame.setTitle(self._frame_title(self._expanded))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            watched is self._frame
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and self._title_hit(event.position().toPoint())
        ):
            self.set_expanded(not self._expanded)
            return True
        return super().eventFilter(watched, event)

    def _title_hit(self, pos) -> bool:
        if self._frame is None:
            return False
        opt = QStyleOptionGroupBox()
        self._frame.initStyleOption(opt)
        label = self._frame.style().subControlRect(
            QStyle.ComplexControl.CC_GroupBox,
            opt,
            QStyle.SubControl.SC_GroupBoxLabel,
            self._frame,
        )
        return label.adjusted(-4, -6, 6, 6).contains(pos)

    def _frame_title(self, expanded: bool) -> str:
        arrow = "▼" if expanded else "▶"
        return f"{arrow}  {self._title}"

    def _apply_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._body.setVisible(expanded)
        if self._frame is not None:
            self._frame.setTitle(self._frame_title(expanded))

    def _on_toggled(self, expanded: bool) -> None:
        self._expanded = expanded
        self._body.setVisible(expanded)
        if self._btn is not None:
            self._btn.setArrowType(
                Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
            )
        self.toggled.emit(expanded)
