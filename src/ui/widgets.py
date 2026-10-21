"""작은 공용 위젯: 패널 틀, 상태 칩, 흐름 배치(줄바꿈), 버튼 만들기."""
from __future__ import annotations

from PyQt5.QtCore import QPoint, QRect, QSize, Qt
from PyQt5.QtGui import QFont, QPainter, QPen
from PyQt5.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from . import theme


def button(text: str, kind: str | None = None, tip: str = '', parent: QWidget | None = None) -> QPushButton:
    b = QPushButton(text, parent)
    if kind:
        b.setProperty('kind', kind)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setAutoDefault(False)
    return b


def latin_input(w: QWidget) -> QWidget:
    """코드·로트·박스 ID 처럼 영문·숫자만 받는 칸: 한글 입력기를 끈다 (바코드 리더가 'ㅔㅊ9030' 을 치지 않게)."""
    w.setAttribute(Qt.WA_InputMethodEnabled, False)
    w.setInputMethodHints(Qt.ImhLatinOnly | Qt.ImhNoPredictiveText)
    return w


def mouse_only(*widgets: QWidget) -> None:
    """마우스로만 쓰는 버튼: 눌러도 키보드 포커스를 가져가지 않는다.
    (포커스가 버튼에 남으면 바코드 리더가 보내는 공백이 그 버튼을 눌러 버린다)"""
    for w in widgets:
        w.setFocusPolicy(Qt.NoFocus)


def scan_key_text(ev) -> str:
    """바코드 리더(키보드처럼 입력)가 보낸 글자 키면 그 글자, 아니면 ''.
    Ctrl/Alt 조합·공백으로 시작·제어 문자는 제외 (공백·Enter 는 이미 스캔 칸에 포커스가 간 뒤에 온다)."""
    if ev.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
        return ''
    text = ev.text()
    if not text or not text.isprintable() or text[0].isspace():
        return ''
    return text


def label(text: str = '', name: str | None = None, parent: QWidget | None = None) -> QLabel:
    lb = QLabel(text, parent)
    if name:
        lb.setObjectName(name)
    return lb


class Panel(QFrame):
    """흰 바탕 + 1px 테두리 + 둥근 모서리. title 이 있으면 위에 제목 줄."""

    def __init__(self, title: str = '', parent: QWidget | None = None, margins=(12, 10, 12, 10), spacing: int = 8):
        super().__init__(parent)
        self.setObjectName('Panel')
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(*margins)
        self.outer.setSpacing(spacing)
        self.title_row: QHBoxLayout | None = None
        self.title_label: QLabel | None = None
        if title:
            self.title_row = QHBoxLayout()
            self.title_row.setSpacing(8)
            self.title_label = label(title, 'PanelTitle')
            self.title_row.addWidget(self.title_label)
            self.title_row.addStretch(1)
            self.outer.addLayout(self.title_row)

    def add_title_widget(self, w: QWidget) -> None:
        if self.title_row is not None:
            self.title_row.addWidget(w)


class Chip(QWidget):
    """둥근 상태 표시 (글자 + 색). 색이 짙으면 흰 글자."""

    def __init__(self, text: str = '', color: str = theme.TEXT_2, parent: QWidget | None = None, filled: bool = True):
        super().__init__(parent)
        self._text, self._color, self._filled = text, color, filled
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        f.setBold(True)
        self.setFont(f)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set(self, text: str, color: str, filled: bool = True) -> None:
        if (text, color, filled) != (self._text, self._color, self._filled):
            self._text, self._color, self._filled = text, color, filled
            self.updateGeometry()
            self.update()

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._text) + 20, fm.height() + 6)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _ev) -> None:
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        c = theme.qc(self._color)
        if self._filled:
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            fg = theme.text_on(c)
        else:
            p.setPen(QPen(c, 1.2))
            p.setBrush(theme.mix(c, '#ffffff', 0.88))
            fg = theme.mix(c, theme.TEXT, 0.35)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(fg)
        p.drawText(r, Qt.AlignCenter, self._text)


class ElidedLabel(QLabel):
    """폭이 모자라면 끝을 '…' 로 줄이는 한 줄 글자 (잘린 글자 대신). 전체 글은 툴팁."""

    def __init__(self, text: str = '', name: str | None = None, parent: QWidget | None = None):
        super().__init__(text, parent)
        if name:
            self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setTextFormat(Qt.PlainText)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text if text else '')

    def minimumSizeHint(self) -> QSize:
        return QSize(10, super().minimumSizeHint().height())

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        r = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, max(0, r.width()))
        self.style().drawItemText(p, r, int(self.alignment()), self.palette(), self.isEnabled(), text,
                                  self.foregroundRole())


class Swatch(QWidget):
    """작은 색 견본 사각형."""

    def __init__(self, color: str = '#cccccc', size: int = 14, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(theme.mix(self._color, '#000000', 0.25), 1))
        p.setBrush(theme.qc(self._color))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 3, 3)


def _explicitly_hidden(item) -> bool:
    w = item.widget()
    return w is not None and w.testAttribute(Qt.WA_WState_ExplicitShowHide) and w.isHidden()


class FlowLayout(QLayout):
    """줄이 차면 다음 줄로 넘기는 배치 (범례 칩용)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)
        self.invalidate()          # 다시 배치 (안 하면 새 칩이 (0,0)에 겹친다)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        if not 0 <= i < len(self._items):
            return None
        item = self._items.pop(i)
        self.invalidate()
        return item

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        s = QSize()
        for it in self._items:
            if _explicitly_hidden(it):
                continue
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            if _explicitly_hidden(it):
                continue
            hint = it.sizeHint()
            nx = x + hint.width() + self._sp
            if nx - self._sp > r.right() + 1 and line_h > 0:
                x, y = r.x(), y + line_h + self._sp
                nx = x + hint.width() + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f'color: {theme.BORDER}; background: {theme.BORDER}; max-height: 1px; border: none;')
    return f


def clear_layout(layout: QLayout) -> None:
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            clear_layout(it.layout())


def colored_dot_html(color: str) -> str:
    return f'<span style="color:{color}; font-size:11pt;">●</span>'
