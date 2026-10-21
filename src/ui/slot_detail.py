"""슬롯 상세 (오른쪽 위): 칸 상태·박스 정보·최근 이력 5건·작업 버튼 (상태에 맞는 것만 켜짐)."""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog, QFrame, QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from core.inventory import BOX_STATE_LABEL, EVENT_LABELS, InventoryError
from core.models import FACE_LABEL, BoxState, SlotState
from core.rack import POLICIES

from . import theme
from .dialogs import BoxEntryDialog, ReasonDialog, SlotPickDialog
from .table import RowTable, Row
from .util import age_s, ask, fmt_age, run_action, short_ts
from .widgets import Chip, ElidedLabel, FlowLayout, Panel, Swatch, button, label

STATE_CHIP = {'EMPTY': ('빈 칸', '#8a979e'), 'RESERVED': ('예약됨 · AMR', theme.ACCENT), 'OCCUPIED': ('적재됨', '#3d84e6'),
              'BLOCKED': ('사용중지', '#8a9098')}


class StatTile(QFrame):
    """요약 숫자 칸: 큰 숫자 + 이름."""

    def __init__(self, name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName('Tile')
        self.setStyleSheet(f'QFrame#Tile {{ background: {theme.PANEL_2}; border: 1px solid {theme.BORDER}; border-radius: 2px; }}')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(3)
        self.value = label('-', None)
        self.value.setStyleSheet(f'font-size: 15pt; font-weight: bold; font-family: {theme.MONO};')
        self.unit = label('', 'Muted')
        row.addWidget(self.value)
        row.addWidget(self.unit, 0, Qt.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(label(name, 'Muted'))

    def set(self, value: str, unit: str, color: str) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(f'font-size: 15pt; font-weight: bold; font-family: {theme.MONO}; color: {color};')
        self.unit.setText(unit)


class SlotDetailPanel(Panel):
    def __init__(self, win, parent: QWidget | None = None):
        super().__init__('', parent, margins=(14, 10, 14, 10), spacing=6)
        self.win = win
        self.sid: str | None = None
        self._box = None
        self._slot = None

        # 머리줄
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = label('칸 상세', 'Big')
        self.chip = Chip('', theme.TEXT_2)
        self.chip2 = Chip('', theme.ERROR)
        self.where = ElidedLabel('', 'Muted')
        self.where.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.title)
        top.addWidget(self.chip, 0, Qt.AlignVCenter)
        top.addWidget(self.chip2, 0, Qt.AlignVCenter)
        top.addWidget(self.where, 1)
        # 칸 선택을 풀고 적재대 요약으로 (Esc 도 같음) — 이게 없으면 한 번 칸을 누른 뒤 요약을 다시 볼 수 없다
        self.close_btn = button('요약 보기', 'flat', '칸 선택을 풀고 적재대 요약을 봅니다 (Esc)')
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.clicked.connect(lambda: self.win.select_slot(None))
        top.addWidget(self.close_btn, 0, Qt.AlignVCenter)
        self.outer.addLayout(top)

        # 칸을 고르지 않았을 때: 적재대 요약
        self.placeholder = QWidget()
        ph = QVBoxLayout(self.placeholder)
        ph.setContentsMargins(0, 2, 0, 0)
        ph.setSpacing(5)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tiles = {}
        for key, name in (('occupied', '적재'), ('reserved', '예약'), ('empty', '빈 칸'), ('blocked', '사용중지'),
                          ('needs', '확인 필요')):
            t = StatTile(name)
            self.tiles[key] = t
            tiles.addWidget(t)
        ph.addLayout(tiles)
        self.oldest = ElidedLabel('', None)
        ph.addWidget(self.oldest)
        self.mix = ElidedLabel('', 'Muted')
        ph.addWidget(self.mix)
        tip = label('칸을 누르면 칸 정보와 작업 버튼(출고·이동·수정·사용중지)이 여기에 나옵니다.', 'Faint')
        tip.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        ph.addWidget(tip)
        ph.addStretch(1)
        self.outer.addWidget(self.placeholder, 1)

        # 본문: 왼쪽 항목 / 오른쪽 최근 이력
        self.body = QWidget()
        body = QHBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(3)
        self.names: list = []
        self.values: list = []
        self.swatch = Swatch('#cccccc', 12)
        for i in range(6):
            n = label('', 'FieldName')
            v = ElidedLabel('', 'FieldValue')
            self.grid.addWidget(n, i, 0, Qt.AlignLeft | Qt.AlignVCenter)
            if i == 1:
                cell = QHBoxLayout()
                cell.setSpacing(6)
                cell.addWidget(self.swatch)
                cell.addWidget(v, 1)
                self.grid.addLayout(cell, i, 1)
            else:
                self.grid.addWidget(v, i, 1)
            self.names.append(n)
            self.values.append(v)
        self.grid.setColumnStretch(1, 1)
        left = QVBoxLayout()
        left.setSpacing(0)
        left.addLayout(self.grid)
        left.addStretch(1)
        body.addLayout(left, 5)
        right = QVBoxLayout()
        right.setSpacing(4)
        right.addWidget(label('이 칸 최근 이력', 'Muted'))
        self.events = RowTable(['시각', '종류', '내용'], ['10-20 14:39', '예약 해제', None], stretch=2)
        self.events.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 6)
        self.events.horizontalHeader().setVisible(False)
        self.events.setFocusPolicy(Qt.NoFocus)
        self.events.setSelectionMode(RowTable.NoSelection)
        self.events.setMinimumHeight(5 * (self.fontMetrics().height() + 6) + 4)
        self.events.setMaximumHeight(5 * (self.fontMetrics().height() + 6) + 4)
        self.events.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right.addWidget(self.events)
        right.addStretch(1)
        body.addLayout(right, 6)
        self.outer.addWidget(self.body, 1)

        # 버튼
        self.btn_box = QWidget()
        flow = FlowLayout(self.btn_box, spacing=6)
        self.b_out = button('수동 출고', 'primary', '이 칸 박스를 출고 처리합니다 (박스 → 출고됨, 칸 → 빈 칸)')
        self.b_move = button('이동…', tip='박스를 다른 빈 칸으로 옮겼을 때 (실사 수정)')
        self.b_edit = button('내용 수정/등록…', tip='빈 칸: 실제로 든 박스 등록 · 적재 칸: 재종·로트·중량 고치기')
        self.b_clear = button('비우기…', tip='실제로 비어 있는 칸 (박스 기록은 출고로 남김)')
        self.b_block = button('사용중지…', tip='점검·고장 칸 — 자동 배정에서 뺍니다')
        self.b_stored = button('적재 완료 처리', 'warn', '확인 필요 박스: 예약 칸에 실제로 들어갔을 때')
        self.b_return = button('컨베이어로 되돌림', 'warn', '확인 필요 박스: 실물을 컨베이어 맨 앞에 놓았을 때')
        for b in (self.b_out, self.b_move, self.b_edit, self.b_clear, self.b_block, self.b_stored, self.b_return):
            flow.addWidget(b)
        self.outer.addWidget(self.btn_box)

        self.b_out.clicked.connect(self._retrieve)
        self.b_move.clicked.connect(self._move)
        self.b_edit.clicked.connect(self._edit)
        self.b_clear.clicked.connect(self._clear)
        self.b_block.clicked.connect(self._block)
        self.b_stored.clicked.connect(self._resolve_stored)
        self.b_return.clicked.connect(self._resolve_return)
        self.set_slot(None)

    @property
    def ctrl(self):
        return self.win.ctrl

    # ---- 표시
    def set_slot(self, sid: str | None) -> None:
        self.sid = sid
        self.refresh()

    def refresh(self) -> None:
        sid = self.sid
        slot = box = None
        if sid:
            try:
                slot = self.ctrl.inventory.slot(sid)
            except InventoryError:
                sid = self.sid = None
        if slot is None:
            self._slot = self._box = None
            self.title.setText('적재대 요약')
            self.chip.set('', theme.TEXT_2)
            self.chip2.set('', theme.ERROR)
            self.where.setText('')
            self.close_btn.hide()
            self.body.hide()
            self.btn_box.hide()
            self.placeholder.show()
            self._refresh_summary()
            return
        bid = slot.box_id or slot.reserved_for
        if bid:
            try:
                box = self.ctrl.inventory.box(bid)
            except InventoryError:
                box = None
        self._slot, self._box = slot, box
        self.placeholder.hide()
        self.close_btn.show()
        self.body.show()
        self.btn_box.show()
        self.title.setText(slot.id)
        text, color = STATE_CHIP.get(slot.state.value, (slot.state.value, theme.TEXT_2))
        if slot.state == SlotState.RESERVED and box is not None and box.state == BoxState.CONVEYOR:
            text = '예약됨 · 컨베이어'
        self.chip.set(text, color)
        needs = bool(box is not None and box.needs_check)
        self.chip2.set('확인 필요' if needs else '', theme.ERROR)
        lay = self.ctrl.inventory.layout
        try:
            extra = f' · 접근 {lay.access_node(slot.id)} · 포크 {lay.fork_height(slot.tier):.2f} m'
        except ValueError:
            extra = ''
        self.where.setText(f'{FACE_LABEL.get(slot.face, slot.face)} {slot.col}열 {slot.tier}단{extra}')

        rows: list[tuple[str, str]] = []
        grade_color = None
        if box is not None:
            g = self.ctrl.inventory.grade(box.grade)
            grade_color = g.color if g else '#9aa5ab'
            gtext = box.grade + (f'   {g.iso_class} · {g.kind} · {g.description}' if g else '')
            rows += [('박스', box.id), ('재종', gtext), ('로트', box.lot or '-'),
                     ('중량', f'{box.weight_kg:g} kg · {box.bags}봉' + ('  (잔량)' if box.weight_kg < 15 else ''))]
            if slot.state == SlotState.OCCUPIED:
                rows.append(('입고', f'{short_ts(box.stored_at)}  ({fmt_age(age_s(box.stored_at))} 전)'))
                rows.append(('도착', short_ts(box.arrived_at) or '-'))
            else:
                where = BOX_STATE_LABEL.get(box.state.value, box.state.value)
                if box.state == BoxState.ON_AMR and box.robot:
                    where = f'{box.robot} 트레이 위'
                rows.append(('위치', where))
                if box.order_id:
                    rows.append(('작업', box.order_id + (' (중단 — 사람이 정리)' if box.needs_check else ' (AMR 진행 중)')))
                else:
                    rows.append(('도착', short_ts(box.arrived_at) or '-'))
        elif slot.state == SlotState.BLOCKED:
            rows += [('사유', slot.blocked_reason or '-'), ('변경', short_ts(slot.updated_at))]
        else:
            rows += [('상태', '비어 있음'), ('변경', short_ts(slot.updated_at) or '-')]
        for i in range(6):
            n, v = (rows[i] if i < len(rows) else ('', ''))
            self.names[i].setText(n)
            self.values[i].setText(v)
            if needs and n in ('위치', '작업'):
                self.values[i].setStyleSheet(f'color: {theme.ERROR};')
            else:
                self.values[i].setStyleSheet('')
        self.swatch.setVisible(grade_color is not None)
        if grade_color:
            self.swatch.set_color(grade_color)

        evs = self.ctrl.inventory.events(limit=5, slot_id=slot.id)
        self.events.model_.set_rows([Row(e.id, [short_ts(e.ts)[:-3], EVENT_LABELS.get(e.kind, e.kind), e.detail],
                                         fg={0: theme.TEXT_2}, tip=f'{e.ts}  {EVENT_LABELS.get(e.kind, e.kind)}\n{e.detail}')
                                     for e in evs])

        st = slot.state
        occupied = st == SlotState.OCCUPIED
        self.b_out.setEnabled(occupied)
        self.b_move.setEnabled(occupied)
        self.b_edit.setEnabled(st in (SlotState.EMPTY, SlotState.OCCUPIED))
        self.b_edit.setText('내용 수정…' if occupied else '내용 등록…' if st == SlotState.EMPTY else '내용 수정/등록…')
        self.b_clear.setEnabled(occupied)
        self.b_block.setEnabled(st in (SlotState.EMPTY, SlotState.BLOCKED))
        self.b_block.setText('사용재개' if st == SlotState.BLOCKED else '사용중지…')
        resolvable = needs and box is not None and box.state == BoxState.ON_AMR
        self.b_stored.setVisible(resolvable)
        self.b_return.setVisible(resolvable)
        self.btn_box.updateGeometry()
        ensure = getattr(self.win, 'ensure_detail_room', None)
        if ensure is not None:
            ensure()

    def _refresh_summary(self) -> None:
        inv = self.ctrl.inventory
        snap = inv.snapshot()
        empty = snap.capacity - snap.occupied - snap.reserved - snap.blocked
        self.tiles['occupied'].set(f'{snap.occupied}', f'/ {snap.capacity}', theme.TEXT)
        self.tiles['reserved'].set(str(snap.reserved), '', theme.ACCENT_DARK if snap.reserved else theme.TEXT_3)
        self.tiles['empty'].set(str(empty), '', theme.TEXT if empty else theme.ERROR)
        self.tiles['blocked'].set(str(snap.blocked), '', theme.TEXT_2 if snap.blocked else theme.TEXT_3)
        self.tiles['needs'].set(str(snap.needs_check), '', theme.ERROR if snap.needs_check else theme.TEXT_3)
        stored = [b for b in inv.boxes((BoxState.STORED,)) if b.stored_at]
        if stored:
            b = min(stored, key=lambda x: (x.stored_at or '', x.slot_id or ''))
            self.oldest.setText(f'가장 오래된 박스  {b.slot_id} · {b.grade} · {short_ts(b.stored_at)} 입고 '
                                f'({fmt_age(age_s(b.stored_at))} 전)')
            self.oldest.setStyleSheet(f'color: {theme.TEXT};')
        else:
            self.oldest.setText('적재된 박스가 없습니다.')
            self.oldest.setStyleSheet(f'color: {theme.TEXT_2};')
        kinds = len(snap.by_grade)
        top = sorted(snap.by_grade.items(), key=lambda kv: -kv[1])[:3]
        self.mix.setText(f'재종 {kinds}종 적재' + (' · 많은 순 ' + ', '.join(f'{g} {n}' for g, n in top) if top else '')
                         + f' · 컨베이어 {snap.conveyor}/{int(self.ctrl.config.conveyor_capacity)}')

    # ---- 동작
    def _after(self) -> None:
        self.refresh()                 # 버튼 상태를 바로 맞춘다 (다음 클릭이 옛 상태를 보지 않게)
        self.win.request_flush()
        restore = getattr(self.win, 'restore_scan_focus', None)
        if restore is not None:
            restore()                  # 입고 탭이면 다음 바코드가 스캔 칸으로

    def _back(self) -> None:
        """대화상자를 취소했을 때: 입고 탭이면 스캔 칸으로 포커스."""
        restore = getattr(self.win, 'restore_scan_focus', None)
        if restore is not None:
            restore()

    def _free_slots(self, exclude: str | None = None) -> list[str]:
        return [s.id for s in self.ctrl.inventory.slots() if s.state == SlotState.EMPTY and s.id != exclude]

    def _retrieve(self) -> None:
        if self._box is None or self._slot is None:
            return
        reason = ReasonDialog.get(self, '수동 출고', f'{self._slot.id} 칸의 {self._box.grade} 박스 {self._box.id} 를 '
                                  '출고 처리합니다.', default='수동 출고', ok_text='출고')
        if reason is None:
            return self._back()
        ok, _ = run_action(self, self.ctrl.retrieve, self._slot.id, reason)
        if ok:
            self.win.toast(f'{self._slot.id} 출고 처리 — {self._box.grade} {self._box.id}')
        self._after()

    def _move(self) -> None:
        if self._box is None or self._slot is None:
            return
        free = self._free_slots(self._slot.id)
        rec = self.ctrl.recommend_slot(self._box.grade, {self._slot.id})
        d = SlotPickDialog(self, '박스 이동', f'{self._box.grade} 박스 {self._box.id} 를 {self._slot.id} 에서 다른 빈 칸으로 '
                           '옮겼다고 기록합니다.', free, rec, POLICIES.get(self.ctrl.inventory.policy, ''),
                           reason=('사유', '실사 이동', False), ok_text='이동')
        if d.exec_() != QDialog.Accepted:
            return self._back()
        to, reason = d.result_value()
        ok, _ = run_action(self, self.ctrl.move_box, self._box.id, to, reason)
        if ok:
            self.win.select_slot(to)
        self._after()

    def _edit(self) -> None:
        slot = self._slot
        if slot is None:
            return
        grades = self.ctrl.inventory.grades()
        if slot.state == SlotState.EMPTY:
            d = BoxEntryDialog(self, '빈 칸에 박스 등록', f'{slot.id} 칸에 실제로 들어 있는 박스를 등록합니다 (실사).',
                               grades, reason_default='실사 등록')
            if d.exec_() != QDialog.Accepted:
                return self._back()
            v = d.values()
            ok, _ = run_action(self, self.ctrl.put_directly, slot.id, v['grade'], v['lot'], v['weight'],
                               v['box_id'] or None, v['reason'])
        elif slot.state == SlotState.OCCUPIED and self._box is not None:
            b = self._box
            d = BoxEntryDialog(self, '칸 내용 수정', f'{slot.id} 칸의 실제 내용으로 고칩니다. 지금 박스 {b.id} 는 비우기로 기록하고 '
                               '새 박스 번호로 다시 등록합니다.', grades, b.grade, b.lot, b.weight_kg,
                               reason_default='', with_box_id=False)
            d.ok_btn.setText('수정')
            if d.exec_() != QDialog.Accepted:
                return self._back()
            v = d.values()
            ok, _ = run_action(self, self.ctrl.replace_slot_content, slot.id, v['grade'], v['lot'], v['weight'],
                               v['reason'])
        else:
            return
        self._after()

    def _clear(self) -> None:
        if self._slot is None or self._box is None:
            return
        reason = ReasonDialog.get(self, '칸 비우기', f'{self._slot.id} 칸이 실제로 비어 있을 때 씁니다. 박스 {self._box.id} '
                                  '기록은 출고(비우기)로 남습니다.', ok_text='비우기', danger=True)
        if reason is None:
            return self._back()
        run_action(self, self.ctrl.clear_slot, self._slot.id, reason)
        self._after()

    def _block(self) -> None:
        slot = self._slot
        if slot is None:
            return
        if slot.state == SlotState.BLOCKED:
            run_action(self, self.ctrl.unblock_slot, slot.id)
        else:
            reason = ReasonDialog.get(self, '칸 사용중지', f'{slot.id} 칸을 자동 배정에서 뺍니다.', default='점검',
                                      ok_text='사용중지')
            if reason is None:
                return self._back()
            run_action(self, self.ctrl.block_slot, slot.id, reason)
        self._after()

    def _resolve_stored(self) -> None:
        b, s = self._box, self._slot
        if b is None or s is None:
            return
        if not ask(self, f'박스 {b.id} ({b.grade}) 가 {s.id} 칸에 실제로 들어 있습니까?\n\n예 → 적재 완료로 기록하고 '
                         f'{b.robot or "AMR"} 트레이 표시를 비웁니다.', '적재 완료 처리'):
            return self._back()
        run_action(self, self.ctrl.resolve_stored, b.id)
        self._after()

    def _resolve_return(self) -> None:
        b = self._box
        if b is None:
            return
        if not ask(self, f'박스 {b.id} ({b.grade}) 실물을 컨베이어 맨 앞에 놓았습니까?\n\n예 → 컨베이어 맨 앞으로 되돌리고 '
                         '예약을 풉니다.', '컨베이어로 되돌림'):
            return self._back()
        run_action(self, self.ctrl.resolve_return, b.id)
        self._after()
