"""가벼운 표 모델 — 행 단위 갱신(바뀐 행만 dataChanged, 새 행은 앞에 끼움)으로 선택·스크롤을 지킨다."""
from __future__ import annotations

from dataclasses import dataclass, field

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter
from PyQt5.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from . import theme


TABLE_PT = 9.0


@dataclass
class Row:
    key: object
    cells: list[str]
    fg: dict[int, str] = field(default_factory=dict)        # 열 → 글자색
    swatch: dict[int, str] = field(default_factory=dict)    # 열 → 색 견본 (decoration)
    bold: set[int] = field(default_factory=set)
    bg: str | None = None                                    # 행 바탕 강조
    tip: str = ''
    data: object = None                                      # 아무 값 (선택 행 처리용)

    def same(self, other: 'Row') -> bool:
        return (self.cells == other.cells and self.fg == other.fg and self.swatch == other.swatch
                and self.bg == other.bg and self.bold == other.bold and self.tip == other.tip)


class RowTableModel(QAbstractTableModel):
    def __init__(self, headers: list[str], align: dict[int, int] | None = None, parent=None):
        super().__init__(parent)
        self.headers = list(headers)
        self.align = dict(align or {})
        self.rows: list[Row] = []
        self._bold = QFont()
        self._bold.setBold(True)
        self._brush: dict[str, QBrush] = {}

    # ---- Qt
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(self.headers):
            return self.headers[section]
        if orientation == Qt.Horizontal and role == Qt.TextAlignmentRole:
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def _b(self, color: str) -> QBrush:
        b = self._brush.get(color)
        if b is None:
            b = self._brush[color] = QBrush(QColor(color))
        return b

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if r >= len(self.rows):
            return None
        row = self.rows[r]
        if role == Qt.DisplayRole:
            return row.cells[c] if c < len(row.cells) else ''
        if role == Qt.ForegroundRole and c in row.fg:
            return self._b(row.fg[c])
        if role == Qt.BackgroundRole and row.bg:
            return self._b(row.bg)
        if role == Qt.DecorationRole and c in row.swatch:
            return QColor(row.swatch[c])
        if role == Qt.FontRole and c in row.bold:
            return self._bold
        if role == Qt.ToolTipRole:
            if row.tip:
                return row.tip
            text = row.cells[c] if c < len(row.cells) else ''
            return text if len(text) > 18 else None
        if role == Qt.TextAlignmentRole:
            return int(self.align.get(c, Qt.AlignLeft) | Qt.AlignVCenter)
        return None

    # ---- 갱신
    def set_rows(self, rows: list[Row]) -> None:
        """키 순서가 같으면 바뀐 행만 갱신, 다르면 다시 채운다 (보기 쪽에서 선택을 키로 되살린다)."""
        if [r.key for r in rows] == [r.key for r in self.rows]:
            last = len(self.headers) - 1
            for i, (old, new) in enumerate(zip(self.rows, rows)):
                if not old.same(new):
                    self.rows[i] = new
                    self.dataChanged.emit(self.index(i, 0), self.index(i, last))
                else:
                    self.rows[i] = new
            return
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def prepend(self, rows: list[Row], cap: int | None = None) -> None:
        if rows:
            self.beginInsertRows(QModelIndex(), 0, len(rows) - 1)
            self.rows[0:0] = rows
            self.endInsertRows()
        if cap is not None and len(self.rows) > cap:
            self.beginRemoveRows(QModelIndex(), cap, len(self.rows) - 1)
            del self.rows[cap:]
            self.endRemoveRows()

    def row_at(self, r: int) -> Row | None:
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def find(self, key) -> int:
        for i, r in enumerate(self.rows):
            if r.key == key:
                return i
        return -1


class RowTable(QTableView):
    """한 줄 선택, 편집 없음, 줄무늬, 내용 열 늘림. 다시 채워도 선택한 키는 되살린다."""

    def __init__(self, headers: list[str], widths: list, stretch: int, align: dict[int, int] | None = None,
                 parent=None, min_stretch: int = 120):
        super().__init__(parent)
        self._stretch_col = stretch
        self._min_stretch = min_stretch
        self.model_ = RowTableModel(headers, align, self)
        self.setModel(self.model_)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideRight)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(max(24, self.fontMetrics().height() + 9))
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        h = self.horizontalHeader()
        h.setHighlightSections(False)
        h.setMinimumSectionSize(36)
        h.setStretchLastSection(False)
        f = QFont(self.font())
        f.setPointSizeF(TABLE_PT)          # 표는 9pt (좁은 오른쪽 열에 열이 다 들어가게)
        self.setFont(f)
        self.verticalHeader().setDefaultSectionSize(max(22, QFontMetrics(f).height() + 9))
        bold = QFont(f)
        bold.setBold(True)
        fm = QFontMetrics(bold)   # 머리글·굵은 칸 기준
        for i, w in enumerate(widths):
            if i == stretch:
                h.setSectionResizeMode(i, QHeaderView.Stretch)
            else:
                h.setSectionResizeMode(i, QHeaderView.Interactive)
                head_w = fm.horizontalAdvance(headers[i]) + 16
                if isinstance(w, str):   # 예시 글자로 폭 계산 ('■' 는 색 견본 자리)
                    w = fm.horizontalAdvance(w.replace('■ ', '')) + (38 if w.startswith('■') else 16)
                self.setColumnWidth(i, max(head_w, w or 60))
        self.empty_text = ''
        self.model_.modelAboutToBeReset.connect(self._remember)
        self.model_.modelReset.connect(self._restore)
        self._keep = None

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._fit_stretch()

    def keyPressEvent(self, ev) -> None:
        # 글자 키는 표 안 글자 찾기 대신 위(주 창)로 넘긴다 → 바코드 리더 입력이 스캔 칸으로 간다
        from .widgets import scan_key_text
        if scan_key_text(ev):
            ev.ignore()
            return
        super().keyPressEvent(ev)

    def _fit_stretch(self) -> None:
        """내용 열이 min_stretch 보다 좁아지면 늘림을 풀고 가로 스크롤 (글자가 너무 잘리지 않게)."""
        h = self.horizontalHeader()
        c = self._stretch_col
        if c is None or c < 0 or c >= self.model_.columnCount():
            return
        others = sum(h.sectionSize(i) for i in range(self.model_.columnCount()) if i != c)
        room = self.viewport().width() - others
        want = QHeaderView.Stretch if room >= self._min_stretch else QHeaderView.Interactive
        if h.sectionResizeMode(c) != want:
            h.setSectionResizeMode(c, want)
            if want == QHeaderView.Interactive:
                self.setColumnWidth(c, self._min_stretch)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if self.empty_text and self.model_.rowCount() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor(theme.TEXT_3))
            p.drawText(self.viewport().rect().adjusted(16, 12, -16, -12), Qt.AlignCenter | Qt.TextWordWrap,
                       self.empty_text)
            p.end()

    def _remember(self) -> None:
        self._keep = self.selected_key()

    def _restore(self) -> None:
        if self._keep is not None:
            r = self.model_.find(self._keep)
            if r >= 0:
                self.selectRow(r)
        self._keep = None

    def selected_row(self) -> Row | None:
        sel = self.selectionModel().selectedRows() if self.selectionModel() else []
        if not sel:
            return None
        return self.model_.row_at(sel[0].row())

    def selected_key(self):
        row = self.selected_row()
        return row.key if row is not None else None

    def select_key(self, key) -> bool:
        r = self.model_.find(key)
        if r >= 0:
            self.selectRow(r)
            self.scrollTo(self.model_.index(r, 0))
            return True
        return False


def text_width(widget, sample: str, pad: int = 22) -> int:
    return widget.fontMetrics().horizontalAdvance(sample) + pad
