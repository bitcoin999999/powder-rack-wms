"""출고 조회 탭: 재종 → 선입선출(FIFO) 목록, [적재대에서 보기], [수동 출고]."""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox, QCompleter, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from . import theme
from .dialogs import ReasonDialog
from .rack_view import color_icon
from .table import Row, RowTable
from .util import age_s, fmt_age, fmt_kg, run_action, short_ts
from .widgets import button, label


class OutboundTab(QWidget):
    def __init__(self, win, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        self.code: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)
        lay.addWidget(label('재종별 재고 (선입선출)', 'PanelTitle'))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(label('재종', 'FieldName'))
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.setMinimumWidth(180)
        self.combo.setMaxVisibleItems(20)
        self.combo.lineEdit().setPlaceholderText('재종 입력·선택')
        self.combo.lineEdit().setAttribute(Qt.WA_InputMethodEnabled, False)
        comp = self.combo.completer()
        if comp is not None:
            comp.setCompletionMode(QCompleter.PopupCompletion)
            comp.setCaseSensitivity(Qt.CaseInsensitive)
            comp.setFilterMode(Qt.MatchContains)
        row.addWidget(self.combo)
        self.find_btn = button('조회', 'primary')
        row.addWidget(self.find_btn)
        self.all_btn = button('재종 전체 보기', tip='재종별 박스 수와 가장 오래된 박스')
        row.addWidget(self.all_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.summary = label('재종을 고르면 그 재종 박스가 오래된 순서로 나옵니다. 1번부터 출고하세요.', 'Muted')
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.table = RowTable(['순서', '칸', '박스', '로트', '중량', '입고 시각', '경과'],
                              ['000', 'N-10-4', 'W261020-0000', None, '15.0 kg', '10-20 14:39', '9시간 59분'],
                              stretch=3, align={0: Qt.AlignCenter, 4: Qt.AlignRight})
        self.table.empty_text = '이 재종은 적재대에 없습니다'
        lay.addWidget(self.table, 1)
        # 재종을 고르기 전: 재종별 요약 (누르면 그 재종 조회)
        self.overview = RowTable(['재종', '박스', '중량 합', '가장 오래된 칸', '입고 시각', '경과'],
                                 ['■ NC3010', '000', '1500 kg', None, '10-20 14:39', '9시간 59분'],
                                 stretch=3, align={1: Qt.AlignRight, 2: Qt.AlignRight})
        self.overview.empty_text = '적재대에 박스가 없습니다'
        self.overview.setToolTip('줄을 누르면 그 재종의 선입선출 목록을 봅니다')
        lay.addWidget(self.overview, 1)
        acts = QHBoxLayout()
        acts.setSpacing(6)
        self.show_btn = button('적재대에서 보기', tip='왼쪽 적재대에서 이 재종 칸을 강조하고 고른 칸을 선택합니다')
        self.out_btn = button('수동 출고…', 'primary', '고른 박스(없으면 1번)를 출고 처리합니다')
        acts.addWidget(self.show_btn)
        acts.addWidget(self.out_btn)
        self.sel_hint = label('목록에서 고르지 않으면 1번(가장 오래된 박스)', 'Faint')
        self.sel_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        acts.addWidget(self.sel_hint)
        acts.addStretch(1)
        lay.addLayout(acts)

        self.find_btn.clicked.connect(self._find)
        self.combo.activated.connect(lambda _i: self._find())
        self.combo.lineEdit().returnPressed.connect(self._find)
        self.show_btn.clicked.connect(self._show_in_rack)
        self.out_btn.clicked.connect(self._retrieve)
        self.table.doubleClicked.connect(lambda _i: self._show_in_rack())
        self.overview.clicked.connect(self._overview_clicked)
        self.all_btn.clicked.connect(lambda: self.set_grade(None))
        self._rows = []
        self._update_buttons()

    @property
    def ctrl(self):
        return self.win.ctrl

    def refresh_grades(self) -> None:
        snap = self.ctrl.inventory.snapshot()
        text = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        grades = self.ctrl.inventory.grades()
        stocked = [g for g in grades if snap.by_grade.get(g.code)]
        others = [g for g in grades if not snap.by_grade.get(g.code)]
        for g in stocked + others:
            n = snap.by_grade.get(g.code, 0)
            self.combo.addItem(color_icon(g.color), f'{g.code}  ({n})', g.code)
        self.combo.setCurrentIndex(-1)
        self.combo.setEditText(self.code or text if (self.code or text) else '')
        self.combo.blockSignals(False)

    def _code_from_combo(self) -> str | None:
        i = self.combo.currentIndex()
        text = self.combo.currentText().strip()
        if i >= 0 and self.combo.itemText(i) == text:
            return self.combo.itemData(i)
        code = text.split()[0].upper() if text else ''
        return code or None

    def _find(self) -> None:
        code = self._code_from_combo()
        if code and self.ctrl.inventory.grade(code) is None:
            self.summary.setText(f'등록되지 않은 재종: {code}')
            self.summary.setStyleSheet(f'color: {theme.ERROR};')
            self.code = None
            self.table.model_.set_rows([])
            self._update_buttons()
            return
        self.set_grade(code)

    def set_grade(self, code: str | None) -> None:
        self.code = code
        self.combo.blockSignals(True)
        i = self.combo.findData(code) if code else -1
        self.combo.setCurrentIndex(i)
        if i < 0:
            self.combo.setEditText(code or '')
        self.combo.blockSignals(False)
        self.refresh()

    def _overview_clicked(self, _index) -> None:
        row = self.overview.selected_row()
        if row is not None:
            self.set_grade(row.key)

    def _refresh_overview(self) -> None:
        inv = self.ctrl.inventory
        oldest: dict[str, object] = {}
        weight: dict[str, float] = {}
        count: dict[str, int] = {}
        for b in inv.boxes(('STORED',)):
            count[b.grade] = count.get(b.grade, 0) + 1
            weight[b.grade] = weight.get(b.grade, 0.0) + b.weight_kg
            o = oldest.get(b.grade)
            if o is None or (b.stored_at or '', b.slot_id or '') < (o.stored_at or '', o.slot_id or ''):
                oldest[b.grade] = b
        model = self.win.rack.model
        order = [g.code for g in inv.grades()]
        codes = sorted(count, key=lambda c: (order.index(c) if c in order else 999, c))
        rows = []
        for c in codes:
            o = oldest[c]
            rows.append(Row(c, [c, str(count[c]), f'{weight[c]:g} kg', o.slot_id or '', short_ts(o.stored_at)[:-3],
                                fmt_age(age_s(o.stored_at))], swatch={0: model.color(c)}, bold={0}))
        self.overview.model_.set_rows(rows)
        total = sum(count.values())
        self.summary.setText(f'적재대 {total}박스 · 재종 {len(codes)}종. 줄을 누르거나 재종을 고르면 오래된 순서(1번부터 출고)로 나옵니다.')

    def refresh(self) -> None:
        code = self.code
        self.summary.setStyleSheet('')
        self.table.setVisible(bool(code))
        self.overview.setVisible(not code)
        if not code:
            self._rows = []
            self.table.model_.set_rows([])
            self._refresh_overview()
            self._update_buttons()
            return
        rows = self.ctrl.inventory.find_grade(code)
        self._rows = rows
        out = []
        for i, r in enumerate(rows, 1):
            b = r.box
            out.append(Row(b.id, [str(i), r.slot.id, b.id, b.lot, fmt_kg(b.weight_kg), short_ts(b.stored_at)[:-3],
                                  fmt_age(r.age_s)], bold={0, 1} if i == 1 else {1}, data=r,
                           fg={0: theme.ACCENT_DARK} if i == 1 else {}))
        self.table.model_.set_rows(out)
        g = self.ctrl.inventory.grade(code)
        name = code + (f' ({g.iso_class} · {g.kind})' if g else '')
        if rows:
            first = rows[0]
            total = sum(r.box.weight_kg for r in rows)
            self.summary.setText(f'{name} {len(rows)}박스 · {total:g} kg · 가장 오래된 것 {first.slot.id} '
                                 f'({short_ts(first.box.stored_at)} 입고, {fmt_age(first.age_s)} 전)')
        else:
            self.summary.setText(f'{name}: 적재대에 없습니다.')
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = bool(self.code) and self.table.model_.rowCount() > 0
        self.show_btn.setEnabled(bool(self.code))
        self.out_btn.setEnabled(has)
        self.all_btn.setEnabled(bool(self.code))
        for w in (self.show_btn, self.out_btn, self.sel_hint):
            w.setVisible(bool(self.code))

    def _target(self):
        row = self.table.selected_row()
        if row is None:
            row = self.table.model_.row_at(0)
        return row.data if row is not None else None

    def _show_in_rack(self) -> None:
        if not self.code:
            return
        self.win.search_grade(self.code)
        r = self._target()
        if r is not None:
            self.win.select_slot(r.slot.id)

    def _retrieve(self) -> None:
        r = self._target()
        if r is None:
            return
        b = r.box
        rank = next((i for i, x in enumerate(self._rows, 1) if x.box.id == b.id), None)
        extra = '' if rank in (None, 1) else f'\n\n주의: 선입선출 {rank}번 박스입니다 (1번이 더 오래됨).'
        reason = ReasonDialog.get(self, '수동 출고', f'{r.slot.id} 칸 {b.grade} 박스 {b.id} (로트 {b.lot}) 를 출고 처리합니다.'
                                  + extra, default='수동 출고', ok_text='출고')
        if reason is None:
            return
        ok, _ = run_action(self, self.ctrl.retrieve, r.slot.id, reason)
        if ok:
            self.win.toast(f'{r.slot.id} 출고 처리 — {b.grade} {b.id}')
        self.win.request_flush()
