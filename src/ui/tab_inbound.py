"""입고 탭: 스캔 입력, 수동 입력, 샘플 박스, 오버브릿지 상태, 컨베이어 그림·목록, 수동 적재·도착 취소."""
from __future__ import annotations

import os

from PyQt5.QtCore import QEvent, QRect, QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QHBoxLayout, QLineEdit, QSizePolicy, QVBoxLayout,
                             QWidget)

from controller import SCAN_DEBOUNCE_TEXT, scan_visible
from core.inventory import InventoryError, LotOnlyScan
from core.models import Box
from core.rack import POLICIES

from . import theme
from .dialogs import ReasonDialog, SlotPickDialog
from .rack_view import color_icon
from .table import Row, RowTable
from .util import fmt_kg, lot_tail, run_action, show_unexpected, warn
from .widgets import ElidedLabel, button, label, latin_input

SCAN_HINT = '형식: 재종 · 재종 로트 · 재종,로트,중량 · BOX:박스ID;재종;로트;중량'


class ConveyorStrip(QWidget):
    """롤러컨베이어 그림: 왼쪽 오버브릿지 → 오른쪽 도크(AMR 이 집는 곳). 맨 앞 박스가 도크 쪽."""

    def __init__(self, win, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        self.boxes: list[Box] = []
        self.capacity = 5
        self.line_text = ''
        self.setMinimumHeight(54)
        self.setMaximumHeight(72)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def sizeHint(self) -> QSize:
        return QSize(500, 66)

    def set_data(self, boxes: list[Box], capacity: int) -> None:
        self.boxes, self.capacity = list(boxes), max(1, int(capacity))
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        base = QFont(self.font())
        w, h = self.width(), self.height()
        sf = QFont(base)
        sf.setPixelSize(11)
        fm = QFontMetrics(sf)
        left_w = fm.horizontalAdvance('오버브릿지') + 18
        right_w = fm.horizontalAdvance('도크 (AMR)') + 18
        belt = QRectF(left_w, 8, w - left_w - right_w, h - 16)
        # 벨트
        p.setPen(QPen(theme.qc('#9aa8b0'), 1))
        p.setBrush(theme.qc('#e4e9ec'))
        p.drawRoundedRect(belt, 5, 5)
        p.setPen(QPen(theme.qc('#cfd8dd'), 1))
        x = belt.left() + 6
        while x < belt.right() - 3:
            p.drawLine(int(x), int(belt.top() + 3), int(x), int(belt.bottom() - 3))
            x += 9
        # 양 끝 표시
        p.setFont(sf)
        p.setPen(theme.qc(theme.TEXT_2))
        p.drawText(QRect(0, 0, left_w - 4, h), Qt.AlignRight | Qt.AlignVCenter, '오버브릿지')
        dock = QRectF(w - right_w + 6, belt.top(), right_w - 8, belt.height())
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc(theme.ACCENT_SOFT))
        p.drawRoundedRect(dock, 5, 5)
        p.setPen(theme.qc(theme.ACCENT_DARK))
        bf = QFont(sf)
        bf.setBold(True)
        p.setFont(bf)
        p.drawText(dock, Qt.AlignCenter, '도크\n(AMR)')
        # 칸 (오른쪽이 맨 앞)
        cap = self.capacity
        gap = 6
        slot_w = (belt.width() - gap * (cap + 1)) / cap
        model = self.win.rack.model
        for i in range(cap):
            pos = cap - 1 - i                      # i=0 이 왼쪽(맨 뒤)
            r = QRectF(belt.left() + gap + i * (slot_w + gap), belt.top() + 5, slot_w, belt.height() - 10)
            if pos < len(self.boxes):
                b = self.boxes[pos]
                color = model.color(b.grade)
                p.setPen(QPen(theme.qc(theme.ERROR), 2) if b.needs_check else QPen(theme.mix(color, '#000000', 0.2), 1))
                p.setBrush(theme.tint(color, 0.55))
                p.drawRoundedRect(r, 4, 4)
                p.setPen(Qt.NoPen)
                p.setBrush(theme.qc(color))
                p.drawRect(QRectF(r.left() + 1, r.top() + 1, 4, r.height() - 2))
                cf = QFont(base)
                cf.setBold(True)
                cf.setPixelSize(max(10, min(13, int(r.height() * 0.3))))
                p.setFont(cf)
                p.setPen(theme.qc(theme.TEXT))
                inner = r.adjusted(8, 2, -3, -2)
                cfm = QFontMetrics(cf)
                p.drawText(QRectF(inner.left(), inner.top(), inner.width(), inner.height() / 2),
                           Qt.AlignLeft | Qt.AlignBottom, cfm.elidedText(b.grade, Qt.ElideRight, int(inner.width())))
                p.setFont(sf)
                p.setPen(theme.qc(theme.CELL_SUB))
                sub = f'→ {b.slot_id}' if b.slot_id else lot_tail(b.lot)
                if b.needs_check:
                    sub = '확인 필요'
                    p.setPen(theme.qc(theme.ERROR))
                p.drawText(QRectF(inner.left(), inner.top() + inner.height() / 2, inner.width(), inner.height() / 2),
                           Qt.AlignLeft | Qt.AlignTop, fm.elidedText(sub, Qt.ElideRight, int(inner.width())))
                # 순번
                if pos == 0:
                    p.setPen(Qt.NoPen)
                    p.setBrush(theme.qc(theme.HEADER))
                    tag = QRectF(r.right() - 17, r.top() - 4, 20, 14)
                    p.drawRoundedRect(tag, 4, 4)
                    nf = QFont(base)
                    nf.setPixelSize(9)
                    nf.setBold(True)
                    p.setFont(nf)
                    p.setPen(QColor('#ffffff'))
                    p.drawText(tag, Qt.AlignCenter, '앞')
            else:
                pen = QPen(theme.qc('#bcc7cd'), 1, Qt.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(r, 4, 4)
        p.end()


class ScanEdit(QLineEdit):
    """스캔 칸. 2차원 바코드(QR 등) 리더는 칸 사이에 Tab·GS(Ctrl+])를 보내는 경우가 많다 —
    포커스가 다음 위젯으로 넘어가 스캔이 쪼개지지 않게 구분 공백으로 바꿔 넣는다."""

    def event(self, ev) -> bool:
        if ev.type() == QEvent.KeyPress:
            key, mods = ev.key(), ev.modifiers()
            if key in (Qt.Key_Tab, Qt.Key_Backtab) or (key == Qt.Key_BracketRight and mods & Qt.ControlModifier):
                self.insert(' ')
                return True
        return super().event(ev)


class InboundTab(QWidget):
    def __init__(self, win, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 8)
        lay.setSpacing(6)

        # 스캔
        scan_head = QHBoxLayout()
        scan_head.addWidget(label('스캔 입력', 'PanelTitle'))
        scan_head.addStretch(1)
        self.log_btn = button('스캔 기록', tip='지금까지 찍은 바코드 원문과 결과(data\\스캔기록.csv)를 엽니다')
        scan_head.addWidget(self.log_btn)
        self.sample_btn = button('샘플 박스 도착', tip='사용 중인 재종 중 하나로 시험용 박스를 컨베이어에 올립니다')
        scan_head.addWidget(self.sample_btn)
        lay.addLayout(scan_head)
        self.scan = ScanEdit()
        self.scan.setObjectName('ScanEdit')
        self.scan.setPlaceholderText('바코드/RFID 스캔 또는 입력 후 Enter')
        self.scan.setAttribute(Qt.WA_InputMethodEnabled, False)   # 한글 입력기 끔 (리더가 'ㅔㅊ9030' 을 치지 않게)
        self.scan.setInputMethodHints(Qt.ImhLatinOnly | Qt.ImhNoPredictiveText | Qt.ImhPreferUppercase)
        self.scan.setMaxLength(200)
        self.scan.setClearButtonEnabled(True)
        lay.addWidget(self.scan)
        # 형식 안내 줄 — 스캔하면 같은 줄에 결과가 나온다 (세로 공간 절약, 형식은 툴팁에 남음)
        self.result = label(SCAN_HINT, 'Faint')
        self.result.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.result.setMinimumHeight(self.fontMetrics().height() + 4)
        self.result.setToolTip(SCAN_HINT + '\n예) PC9030   ·   PC9030 0001015661578   ·   PC9030,0001015661578,14.2')
        lay.addWidget(self.result)

        # 수동 입력
        man = QHBoxLayout()
        man.setSpacing(6)
        man.addWidget(label('재종', 'FieldName'))
        self.grade = QComboBox()
        self.grade.setMinimumWidth(130)
        self.grade.setMaxVisibleItems(20)
        man.addWidget(self.grade)
        man.addWidget(label('로트', 'FieldName'))
        self.lot = QLineEdit()
        self.lot.setPlaceholderText('자동')
        self.lot.setMaxLength(60)
        latin_input(self.lot)
        self.lot.setMinimumWidth(80)
        man.addWidget(self.lot, 1)
        man.addWidget(label('중량', 'FieldName'))
        self.weight = QDoubleSpinBox()
        self.weight.setRange(0.1, 100.0)
        self.weight.setDecimals(1)
        self.weight.setSingleStep(0.5)
        self.weight.setValue(15.0)
        self.weight.setSuffix(' kg')
        self.weight.setMinimumWidth(78)
        man.addWidget(self.weight)
        self.add_btn = button('도착 등록', 'primary', '고른 재종·로트·중량으로 컨베이어 도착을 등록합니다')
        man.addWidget(self.add_btn)
        lay.addLayout(man)

        # 컨베이어
        conv_head = QHBoxLayout()
        self.conv_title = label('컨베이어', 'PanelTitle')
        conv_head.addWidget(self.conv_title)
        conv_head.addSpacing(8)
        self.ob_label = ElidedLabel('', 'Muted')
        conv_head.addWidget(self.ob_label, 1)
        lay.addSpacing(2)
        lay.addLayout(conv_head)
        self.strip = ConveyorStrip(win)
        lay.addWidget(self.strip)
        self.table = RowTable(['순번', '박스', '재종', '로트', '중량', '상태', '예약 칸', '작업'],
                              ['00', 'W261020-0000', '■ NC3010', None, '15.0 kg', '예약 (수동)', 'N-10-4', 'O0000'],
                              stretch=3, align={0: Qt.AlignCenter, 4: Qt.AlignRight}, min_stretch=90)
        self.table.empty_text = '컨베이어가 비어 있습니다 — 스캔하거나 [오버브릿지 출고 시작]을 누르세요'
        self.table.setMinimumHeight(4 * self.table.verticalHeader().defaultSectionSize() + 4)
        lay.addWidget(self.table, 1)
        acts = QHBoxLayout()
        acts.setSpacing(6)
        self.store_btn = button('수동 적재…', tip='고른 박스를 사람이 직접 칸에 넣었을 때')
        self.cancel_btn = button('도착 취소…', tip='잘못 스캔했거나 두 번 읽힌 박스를 컨베이어 목록에서 지웁니다')
        acts.addWidget(self.store_btn)
        acts.addWidget(self.cancel_btn)
        acts.addSpacing(8)
        self.check_label = ElidedLabel('', None)
        self.check_label.setStyleSheet(f'color: {theme.ERROR}; font-weight: bold;')
        acts.addWidget(self.check_label, 1)
        lay.addLayout(acts)

        self.scan.returnPressed.connect(self._scan)
        self.add_btn.clicked.connect(self._manual_arrival)
        self.sample_btn.clicked.connect(self._sample)
        self.log_btn.clicked.connect(self._open_scan_log)
        self.store_btn.clicked.connect(self._manual_store)
        self.cancel_btn.clicked.connect(self._cancel_arrival)
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        self.table.clicked.connect(self._row_clicked)
        self.table.doubleClicked.connect(lambda _i: self._manual_store())
        self._boxes: dict[str, Box] = {}
        self._update_buttons()

    @property
    def ctrl(self):
        return self.win.ctrl

    # ---- 갱신
    def refresh_grades(self) -> None:
        cur = self.grade.currentData()
        self.grade.blockSignals(True)
        self.grade.clear()
        for g in self.ctrl.inventory.grades(active_only=True):
            self.grade.addItem(color_icon(g.color), g.code, g.code)
        i = self.grade.findData(cur) if cur else 0
        self.grade.setCurrentIndex(max(0, i))
        self.grade.blockSignals(False)

    def refresh_conveyor(self) -> None:
        inv = self.ctrl.inventory
        boxes = inv.conveyor()
        cap = int(self.ctrl.config.conveyor_capacity)
        self._boxes = {b.id: b for b in boxes}
        rows = []
        for i, b in enumerate(boxes, 1):
            if b.needs_check:
                state, fg = '확인 필요', theme.ERROR
            elif b.order_id:
                state, fg = 'AMR 배정', theme.ACCENT_DARK
            elif b.slot_id:
                state, fg = '예약 (수동)', theme.ACCENT_DARK
            else:
                state, fg = '대기', theme.TEXT_2
            rows.append(Row(b.id, [str(i), b.id, b.grade, b.lot, fmt_kg(b.weight_kg), state, b.slot_id or '',
                                   b.order_id or ''], fg={5: fg}, swatch={2: self.win.rack.model.color(b.grade)},
                            bold={1} if i == 1 else set(), data=b))
        self.table.model_.set_rows(rows)
        self.strip.set_data(boxes, cap)
        full = len(boxes) >= cap
        self.conv_title.setText(f'컨베이어 {len(boxes)}/{cap}' + (' · 가득' if full else ''))
        self.conv_title.setStyleSheet(f'color: {theme.ERROR};' if full else '')
        snap_needs = sum(1 for b in inv.boxes(('ON_AMR',)) if b.needs_check)
        self.check_label.setText(f'확인 필요 {snap_needs}개 — 빨간 칸을 눌러 정리' if snap_needs else '')
        self._update_buttons()

    def refresh_overbridge(self) -> None:
        ob = self.ctrl.overbridge
        if not ob.running:
            text = '오버브릿지: 정지'
            color = theme.TEXT_2
        elif ob.line_stopped:
            text = f'오버브릿지: 라인 정지 (컨베이어 만재) · 남은 {ob.remaining}박스'
            color = '#9a6b12'
        else:
            nxt = ob.next_in
            text = f'오버브릿지: 출고 중 · 다음 박스 {int(nxt) + 1 if nxt is not None else "-"}초 · 남은 {ob.remaining}박스'
            color = theme.ACCENT_DARK
        if self.ob_label.text() != text:
            self.ob_label.setText(text)
            self.ob_label.setStyleSheet(f'color: {color};')

    def refresh(self) -> None:
        self.refresh_grades()
        self.refresh_conveyor()
        self.refresh_overbridge()

    def _update_buttons(self, *_a) -> None:
        row = self.table.selected_row()
        b: Box | None = row.data if row is not None else None
        bound = b is not None and bool(b.order_id)
        self.store_btn.setEnabled(b is not None and not bound)
        self.cancel_btn.setEnabled(b is not None and not bound)
        tip = 'AMR 작업에 묶인 박스입니다 — 작업을 먼저 취소하세요' if bound else ''
        self.store_btn.setToolTip(tip or '고른 박스를 사람이 직접 칸에 넣었을 때')
        self.cancel_btn.setToolTip(tip or '잘못 스캔했거나 두 번 읽힌 박스를 컨베이어 목록에서 지웁니다')

    def _row_clicked(self, _index) -> None:
        row = self.table.selected_row()
        if row is not None and row.data is not None and row.data.slot_id:
            self.win.select_slot(row.data.slot_id)

    # ---- 동작
    def focus_scan(self) -> None:
        self.scan.setFocus(Qt.OtherFocusReason)

    def _scan(self) -> None:
        text = self.scan.text()
        if not text.strip():
            return
        seen = scan_visible(text.strip())
        try:
            box = self.ctrl.scan(text)
        except LotOnlyScan as exc:      # 로트만 찍힘 — 로트 칸을 채우고 재종을 고르게 한다 (막는 창은 띄우지 않음)
            self.lot.setText(exc.lot)
            self._show_result(f'※ {exc}', theme.RESIDUAL)
            self.scan.clear()
            self.grade.setFocus(Qt.OtherFocusReason)
            return
        except InventoryError as exc:
            msg = str(exc)
            if msg == SCAN_DEBOUNCE_TEXT:     # 리더가 두 번 읽음 — 막지 않고 표시만
                self._show_result(f'※ {msg}', theme.RESIDUAL)     # ⚠ 는 맑은 고딕에 없는 글자
                self.scan.clear()
            else:
                self._show_result(f'✕ {msg}  · 읽힌 원문: {seen}', theme.ERROR)
                warn(self, f'{msg}\n\n읽힌 원문: {seen}\n(모든 스캔은 [스캔 기록]에 남습니다)', '스캔 오류')
                self.scan.selectAll()
            self.focus_scan()
            return
        except Exception as exc:   # 예상 못 한 오류도 루프를 죽이지 않게
            show_unexpected(self, exc)
            self.focus_scan()
            return
        self._show_result(f'✓ 도착 등록  {box.id} · {box.grade} · {box.lot} · {box.weight_kg:g} kg', theme.ACCENT_DARK)
        self.scan.clear()
        self.focus_scan()
        self.win.request_flush()

    def _open_scan_log(self) -> None:
        path = self.ctrl.scan_log_path
        if not path.exists():
            warn(self, '아직 스캔 기록이 없습니다 — 바코드를 한 번 찍으면 생깁니다.', '스캔 기록')
        else:
            try:
                os.startfile(str(path))   # 엑셀(또는 연결된 프로그램)로 연다
            except OSError as exc:
                warn(self, f'스캔 기록을 열지 못했습니다: {exc}\n파일: {path}', '스캔 기록')
        self.focus_scan()

    def _show_result(self, text: str, color: str) -> None:
        self.result.setText(text)
        self.result.setStyleSheet(f'color: {color}; font-weight: bold;')

    def _manual_arrival(self) -> None:
        code = self.grade.currentData()
        if not code:
            warn(self, '사용 중인 재종이 없습니다 — 기준정보에서 재종을 사용으로 바꾸세요.')
            return
        ok, box = run_action(self, self.ctrl.register_arrival, code, self.lot.text().strip() or None,
                             round(self.weight.value(), 1))
        if ok:
            self._show_result(f'✓ 도착 등록  {box.id} · {box.grade} · {box.lot} · {box.weight_kg:g} kg', theme.ACCENT_DARK)
            self.lot.clear()
        self.focus_scan()
        self.win.request_flush()

    def _sample(self) -> None:
        ok, box = run_action(self, self.ctrl.add_sample_box)
        if ok:
            self._show_result(f'✓ 샘플 박스  {box.id} · {box.grade} · {box.lot} · {box.weight_kg:g} kg', theme.ACCENT_DARK)
        self.focus_scan()
        self.win.request_flush()

    def _selected_box(self) -> Box | None:
        row = self.table.selected_row()
        if row is None:
            return None
        try:
            return self.ctrl.inventory.box(row.key)
        except InventoryError:
            return None

    def _manual_store(self) -> None:
        b = self._selected_box()
        if b is None:
            return
        if b.order_id:
            warn(self, f'AMR 작업 {b.order_id} 에 묶인 박스는 수동으로 적재할 수 없습니다 — [AMR·작업] 탭에서 작업을 먼저 취소하세요.')
            return
        free = [s.id for s in self.ctrl.inventory.slots() if s.state.value == 'EMPTY']
        rec = b.slot_id or self.ctrl.recommend_slot(b.grade)
        if b.slot_id and b.slot_id not in free:
            free.insert(0, b.slot_id)
        d = SlotPickDialog(self, '수동 적재', f'{b.grade} 박스 {b.id} 를 사람이 직접 칸에 넣었다고 기록합니다.', free, rec,
                           POLICIES.get(self.ctrl.inventory.policy, ''), ok_text='적재')
        if d.exec_() != QDialog.Accepted:
            return
        sid, _ = d.result_value()
        ok, slot = run_action(self, self.ctrl.manual_store, b.id, sid)
        if ok:
            self.win.select_slot(slot.id)
        self.focus_scan()
        self.win.request_flush()

    def _cancel_arrival(self) -> None:
        b = self._selected_box()
        if b is None:
            return
        reason = ReasonDialog.get(self, '도착 취소', f'{b.grade} 박스 {b.id} 를 컨베이어 목록에서 지웁니다. '
                                  '(잘못 스캔·두 번 읽힘 — 기록은 이력에 남습니다)', default='잘못 스캔', ok_text='도착 취소',
                                  danger=True)
        if reason is None:
            return
        run_action(self, self.ctrl.cancel_arrival, b.id, reason)
        self.focus_scan()
        self.win.request_flush()
