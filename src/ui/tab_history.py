"""이력 탭: 재고 이벤트 표 (최신 1000건), 종류·재종·글자 필터, 새 이벤트는 위에 끼워 넣기, CSV 저장."""
from __future__ import annotations

import time
from pathlib import Path

from PyQt5.QtCore import QStandardPaths, Qt
from PyQt5.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QSizePolicy, QVBoxLayout, QWidget

from core.inventory import EVENT_LABELS
from core.models import Event

from . import theme
from .rack_view import color_icon
from .table import Row, RowTable
from .util import compact_ts, info, run_action
from .widgets import button, label

KIND_COLOR = {'STORE': theme.ACCENT_DARK, 'OUT': '#3d84e6', 'ERROR': theme.ERROR, 'CORRECT': theme.WARN,
              'BLOCK': theme.TEXT_2, 'UNBLOCK': theme.TEXT_2, 'MASTER': '#b38cff', 'CONFIG': '#b38cff'}
LIMIT = 1000
TEXT_SCAN = 20000     # 글자 검색 때 훑어볼 최근 이벤트 수


class HistoryTab(QWidget):
    def __init__(self, win, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        self.max_id = 0
        self.stale = True
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label('재고 이력', 'PanelTitle'))
        self.count = label('', 'Muted')
        head.addWidget(self.count)
        head.addStretch(1)
        lay.addLayout(head)
        f = QHBoxLayout()
        f.setSpacing(6)
        self.kind = QComboBox()
        self.kind.addItem('종류 전체', None)
        for k, v in EVENT_LABELS.items():
            if k in ('MOVE',):
                continue
            self.kind.addItem(v, k)
        self.kind.setMinimumWidth(110)
        self.grade = QComboBox()
        self.grade.setMinimumWidth(110)
        self.grade.setMaxVisibleItems(20)
        self.text = QLineEdit()
        self.text.setPlaceholderText('박스·칸 검색')
        self.text.setClearButtonEnabled(True)
        self.text.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.text.setMinimumWidth(100)
        self.reload_btn = button('새로고침')
        self.csv_btn = button('CSV 저장…', tip='전체 이력을 CSV(엑셀) 파일로 저장합니다')
        f.addWidget(self.kind)
        f.addWidget(self.grade)
        f.addWidget(self.text, 1)
        f.addWidget(self.reload_btn)
        f.addWidget(self.csv_btn)
        lay.addLayout(f)
        self.table = RowTable(['시각', '종류', '박스', '재종', '칸', 'AMR', '작업', '내용'],
                              ['00:00:00', 'AMR 집음', 'W261020-0000', '■ PC9530', 'N-10-4', 'AMR-01', 'O0000', None],
                              stretch=7,
                              min_stretch=170)
        self.table.empty_text = '조건에 맞는 이력이 없습니다'
        self.table.horizontalHeader().setMinimumSectionSize(40)
        lay.addWidget(self.table, 1)
        self.foot = label('최신 1000건까지 보입니다. 전체 기록은 [CSV 저장]으로.', 'Faint')
        self.foot.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.foot)

        self.kind.currentIndexChanged.connect(lambda _i: self.reload())
        self.grade.currentIndexChanged.connect(lambda _i: self.reload())
        self.text.returnPressed.connect(self.reload)
        self.text.textChanged.connect(self._text_changed)
        self.reload_btn.clicked.connect(self.reload)
        self.csv_btn.clicked.connect(self._csv)

    @property
    def ctrl(self):
        return self.win.ctrl

    def refresh_grades(self) -> None:
        cur = self.grade.currentData()
        self.grade.blockSignals(True)
        self.grade.clear()
        self.grade.addItem('재종 전체', None)
        for g in self.ctrl.inventory.grades():
            self.grade.addItem(color_icon(g.color), g.code, g.code)
        i = self.grade.findData(cur) if cur else 0
        self.grade.setCurrentIndex(max(0, i))
        self.grade.blockSignals(False)

    def _text_changed(self, t: str) -> None:
        if not t:
            self.reload()

    # ---- 행
    def _row(self, e: Event) -> Row:
        label_ = EVENT_LABELS.get(e.kind, e.kind)
        color = self.win.rack.model.color(e.grade) if e.grade else None
        return Row(e.id, [compact_ts(e.ts), label_, e.box_id or '', e.grade or '', e.slot_id or '', e.robot or '',
                          e.order_id or '', e.detail], fg={1: KIND_COLOR.get(e.kind, theme.TEXT)},
                   swatch={3: color} if color else {}, tip=f'{e.ts} · {label_}\n{e.detail}')

    def _match_text(self, e: Event, q: str) -> bool:
        return q in (e.box_id or '').upper() or q in (e.slot_id or '').upper()

    def _filters(self) -> tuple[str | None, str | None, str]:
        return self.kind.currentData(), self.grade.currentData(), self.text.text().strip().upper()

    def reload(self) -> None:
        kind, grade, q = self._filters()
        inv = self.ctrl.inventory
        latest = inv.events(limit=1)
        self.max_id = latest[0].id if latest else 0
        if q:
            evs = [e for e in inv.events(limit=TEXT_SCAN, kind=kind, grade=grade) if self._match_text(e, q)][:LIMIT]
        else:
            evs = inv.events(limit=LIMIT, kind=kind, grade=grade)
        self.table.model_.set_rows([self._row(e) for e in evs])
        self.stale = False
        self._count()

    def add_new(self) -> None:
        """새 이벤트만 위에 끼워 넣는다 (선택·스크롤 유지)."""
        inv = self.ctrl.inventory
        latest = inv.events(limit=300)
        new = [e for e in latest if e.id is not None and e.id > self.max_id]
        if not new:
            return
        if len(new) == len(latest):     # 너무 많이 쌓임 → 다시 읽기
            self.reload()
            return
        self.max_id = new[0].id
        kind, grade, q = self._filters()
        keep = [e for e in new if (not kind or e.kind == kind) and (not grade or e.grade == grade)
                and (not q or self._match_text(e, q))]
        if keep:
            self.table.model_.prepend([self._row(e) for e in keep], cap=LIMIT)
            self._count()

    def _count(self) -> None:
        n = self.table.model_.rowCount()
        self.count.setText(f'{n}건' + (' (최신 1000건)' if n >= LIMIT else ''))

    # ---- CSV
    def _csv(self) -> None:
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or str(Path.home())
        name = f'완분이력_{time.strftime("%Y%m%d")}.csv'
        path, _ = QFileDialog.getSaveFileName(self, 'CSV 저장', str(Path(docs) / name), 'CSV 파일 (*.csv)')
        if not path:
            return
        if not path.lower().endswith('.csv'):
            path += '.csv'
        ok, n = run_action(self, self.ctrl.export_events_csv, Path(path))
        if ok:
            info(self, f'이력 {n}건을 저장했습니다.\n{path}')
