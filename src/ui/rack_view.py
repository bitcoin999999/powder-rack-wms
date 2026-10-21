"""적재대 화면: 북면·남면 정면도(직접 그림), 검색(재종 → 선입선출 순번), 재종 범례.

- 칸 모양은 RackModel 이 가진 슬롯·박스 상태로만 그린다. 재고 이벤트의 'slots'(바뀐 칸)만 다시 읽고 그 칸만 다시 그린다.
- 경과 시간 글자는 30초 타이머로 전체를 다시 그린다 (MainWindow).
"""
from __future__ import annotations

import html

from PyQt5.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLineEdit, QPushButton, QSizePolicy, QToolTip,
                             QWidget)

from core.inventory import BOX_STATE_LABEL, SLOT_STATE_LABEL, InventoryError
from core.models import FACE_LABEL, Box, BoxState, Grade, Slot, SlotState

from . import theme
from .util import age_s, fmt_age, lot_tail
from .widgets import ElidedLabel, FlowLayout, Panel, button, label, latin_input, mouse_only


# ====================================================================== 모델
class RackModel:
    """화면용 적재대 상태 사본 (슬롯 → 박스). 읽기만 한다."""

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.columns = 1
        self.tiers = 1
        self.cells: dict[str, tuple[Slot, Box | None]] = {}
        self.grades: dict[str, Grade] = {}
        self.selected: str | None = None
        self.search_grade: str | None = None     # 재종 검색 (선입선출 순번 표시)
        self.search_text: str = ''               # 그 밖의 글자 검색 (박스·로트·칸)
        self.ranks: dict[str, int] = {}
        self.matches: set[str] = set()

    @property
    def inv(self):
        return self.ctrl.inventory

    def reload_all(self) -> None:
        inv = self.inv
        lay = inv.layout
        self.columns, self.tiers = lay.columns, lay.tiers
        self.grades = {g.code: g for g in inv.grades()}
        boxes = {b.id: b for b in inv.boxes((BoxState.CONVEYOR, BoxState.ON_AMR, BoxState.STORED))}
        self.cells = {}
        for s in inv.slots():
            bid = s.box_id or s.reserved_for
            self.cells[s.id] = (s, boxes.get(bid) if bid else None)
        if self.selected and self.selected not in self.cells:
            self.selected = None
        self.recompute_search()

    def reload_grades(self) -> None:
        self.grades = {g.code: g for g in self.inv.grades()}

    def update_slots(self, sids) -> list[str]:
        inv = self.inv
        done: list[str] = []
        for sid in sids:
            if sid not in self.cells:
                continue
            try:
                s = inv.slot(sid)
            except InventoryError:
                continue
            bid = s.box_id or s.reserved_for
            box = None
            if bid:
                try:
                    box = inv.box(bid)
                except InventoryError:
                    box = None
            self.cells[sid] = (s, box)
            done.append(sid)
        return done

    # ---- 검색
    def set_search(self, grade: str | None, text: str = '') -> None:
        self.search_grade = grade or None
        self.search_text = '' if grade else (text or '').strip().upper()
        self.recompute_search()

    @property
    def searching(self) -> bool:
        return bool(self.search_grade or self.search_text)

    def recompute_search(self) -> None:
        self.ranks, self.matches = {}, set()
        if self.search_grade:
            try:
                rows = self.inv.find_grade(self.search_grade)
            except InventoryError:
                rows = []
            self.ranks = {r.slot.id: i + 1 for i, r in enumerate(rows)}
            self.matches = set(self.ranks)
            # 예약 칸(들어올 박스)도 같은 재종이면 보이게
            for sid, (s, b) in self.cells.items():
                if s.state == SlotState.RESERVED and b is not None and b.grade == self.search_grade:
                    self.matches.add(sid)
        elif self.search_text:
            q = self.search_text
            for sid, (s, b) in self.cells.items():
                hay = [sid]
                if b is not None:
                    hay += [b.id, b.grade, b.lot or '']
                if s.state == SlotState.BLOCKED:
                    hay.append(s.blocked_reason or '')
                if any(q in h.upper() for h in hay):
                    self.matches.add(sid)

    # ---- 집계
    def face_counts(self, face: str) -> dict[str, int]:
        out = {'OCCUPIED': 0, 'RESERVED': 0, 'EMPTY': 0, 'BLOCKED': 0, 'total': 0}
        for sid, (s, _b) in self.cells.items():
            if s.face == face:
                out[s.state.value] = out.get(s.state.value, 0) + 1
                out['total'] += 1
        return out

    def grade_counts(self) -> list[tuple[str, int, int]]:
        """(재종, 적재 수, 예약 수) — 기준정보 순서, 재고나 예약이 있는 재종만."""
        stored: dict[str, int] = {}
        reserved: dict[str, int] = {}
        for s, b in self.cells.values():
            if b is None:
                continue
            if s.state == SlotState.OCCUPIED:
                stored[b.grade] = stored.get(b.grade, 0) + 1
            elif s.state == SlotState.RESERVED:
                reserved[b.grade] = reserved.get(b.grade, 0) + 1
        order = list(self.grades) + sorted((set(stored) | set(reserved)) - set(self.grades))
        return [(g, stored.get(g, 0), reserved.get(g, 0)) for g in order if stored.get(g) or reserved.get(g)]

    def color(self, code: str | None) -> str:
        g = self.grades.get(code or '')
        return g.color if g else '#9aa5ab'


HINT_TEXT = '점선 = AMR 이 넣으러 오는 칸 · 빨간 테두리 = 확인 필요 · 빗금 = 사용중지 · 칸을 누르면 상세'


# ====================================================================== 칸 그리기
def _fit_font(base: QFont, text: str, px: int, max_w: int, bold: bool = False, min_px: int = 8) -> QFont:
    f = QFont(base)
    f.setBold(bold)
    size = px
    while size > min_px:
        f.setPixelSize(size)
        if QFontMetrics(f).horizontalAdvance(text) <= max_w:
            return f
        size -= 1
    f.setPixelSize(min_px)
    return f


def _first_fit(font: QFont, options: list[str], max_w: int) -> str:
    fm = QFontMetrics(font)
    for t in options:
        if fm.horizontalAdvance(t) <= max_w:
            return t
    return fm.elidedText(options[-1], Qt.ElideRight, max_w)


_HATCH = QBrush(theme.qc('#c3ccd2'), Qt.BDiagPattern)
_DIM = QColor(238, 242, 244, 196)


def tooltip_html(model: RackModel, sid: str) -> str:
    s, b = model.cells[sid]
    face_label = FACE_LABEL.get(s.face, s.face)
    lines = [f'<b style="font-size:11pt">{sid}</b> &nbsp; {SLOT_STATE_LABEL.get(s.state.value, s.state.value)}',
             f'<span style="color:#9fb4bf">{face_label} {s.col}열 {s.tier}단</span>']
    if b is not None:
        g = model.grades.get(b.grade)
        gdesc = f' ({g.iso_class} · {g.kind} · {html.escape(g.description)})' if g else ''
        lines += [f'박스 {html.escape(b.id)}', f'재종 <b>{html.escape(b.grade)}</b>{gdesc}',
                  f'로트 {html.escape(b.lot or "-")}', f'중량 {b.weight_kg:g} kg' + (' (잔량)' if b.weight_kg < 15 else '')]
        if s.state == SlotState.OCCUPIED:
            lines.append(f'입고 {b.stored_at or "-"} ({fmt_age(age_s(b.stored_at))} 전)')
            if model.search_grade and sid in model.ranks:
                lines.append(f'출고 순서 {model.ranks[sid]}번 (선입선출)')
        else:
            where = BOX_STATE_LABEL.get(b.state.value, b.state.value)
            if b.state == BoxState.ON_AMR and b.robot:
                where += f' ({html.escape(b.robot)})'
            lines.append(f'박스 위치 {where}' + (f' · 작업 {b.order_id}' if b.order_id else ''))
        if b.needs_check:
            lines.append('<b style="color:#ff8a8a">확인 필요</b> — 실제 위치를 확인하고 정리하세요')
    elif s.state == SlotState.BLOCKED:
        lines.append(f'사유 {html.escape(s.blocked_reason or "-")}')
    if s.updated_at:
        lines.append(f'<span style="color:#9fb4bf">마지막 변경 {s.updated_at}</span>')
    return '<br>'.join(lines)


class RackFaceView(QWidget):
    """한 면(N 또는 S)의 정면도. 열 1(도크 쪽)이 왼쪽, 1단이 아래."""

    clicked = pyqtSignal(str)

    TITLE_H = 24
    HEAD_H = 18
    GUTTER = 40
    GAP = 4

    def __init__(self, face: str, model: RackModel, parent: QWidget | None = None):
        super().__init__(parent)
        self.face = face
        self.model = model
        self.hover: str | None = None
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._base = QFont(self.font())

    # ---- 크기
    def minimumSizeHint(self) -> QSize:
        return QSize(self.GUTTER + self.model.columns * 50 + 8, self.TITLE_H + self.HEAD_H + self.model.tiers * 40 + 6)

    def sizeHint(self) -> QSize:
        return QSize(self.GUTTER + self.model.columns * 80 + 8, self.TITLE_H + self.HEAD_H + self.model.tiers * 76 + 6)

    def _geom(self) -> tuple[float, float, float, float]:
        """(격자 왼쪽 x, 격자 위 y, 칸 폭, 칸 높이)."""
        cols, tiers = max(1, self.model.columns), max(1, self.model.tiers)
        w = self.width() - self.GUTTER - 4
        h = self.height() - self.TITLE_H - self.HEAD_H - 4
        cw = max(10.0, w / cols)
        ch = max(10.0, h / tiers)
        return self.GUTTER, self.TITLE_H + self.HEAD_H, cw, ch

    def cell_rect(self, col: int, tier: int) -> QRect:
        x0, y0, cw, ch = self._geom()
        row = self.model.tiers - tier
        g = self.GAP / 2
        return QRectF(x0 + (col - 1) * cw + g, y0 + row * ch + g, cw - self.GAP, ch - self.GAP).toAlignedRect()

    def sid_at(self, pos: QPoint) -> str | None:
        x0, y0, cw, ch = self._geom()
        if pos.x() < x0 or pos.y() < y0:
            return None
        col = int((pos.x() - x0) // cw) + 1
        row = int((pos.y() - y0) // ch)
        tier = self.model.tiers - row
        if not (1 <= col <= self.model.columns and 1 <= tier <= self.model.tiers):
            return None
        return f'{self.face}-{col:02d}-{tier}'

    def update_cell(self, sid: str) -> None:
        try:
            face, col, tier = sid[0], int(sid[2:4]), int(sid[5:])
        except (ValueError, IndexError):
            self.update()
            return
        if face == self.face:
            self.update(self.cell_rect(col, tier).adjusted(-4, -4, 4, 4))

    # ---- 마우스
    def mouseMoveEvent(self, ev) -> None:
        sid = self.sid_at(ev.pos())
        if sid != self.hover:
            old, self.hover = self.hover, sid
            for s in (old, sid):
                if s:
                    self.update_cell(s)
            self.setCursor(Qt.PointingHandCursor if sid else Qt.ArrowCursor)

    def leaveEvent(self, _ev) -> None:
        if self.hover:
            old, self.hover = self.hover, None
            self.update_cell(old)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            sid = self.sid_at(ev.pos())
            if sid and sid in self.model.cells:
                self.clicked.emit(sid)

    def event(self, ev) -> bool:
        if ev.type() == QEvent.ToolTip:
            sid = self.sid_at(ev.pos())
            if sid and sid in self.model.cells:
                QToolTip.showText(ev.globalPos(), tooltip_html(self.model, sid), self, self.cell_rect(
                    int(sid[2:4]), int(sid[5:])))
            else:
                QToolTip.hideText()
                ev.ignore()
            return True
        return super().event(ev)

    # ---- 그리기
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        m = self.model
        x0, y0, cw, ch = self._geom()
        clip = ev.rect()

        # 제목 줄
        if clip.top() < self.TITLE_H:
            tf = QFont(self._base)
            tf.setBold(True)
            tf.setPixelSize(14)
            p.setFont(tf)
            p.setPen(theme.qc(theme.TEXT))
            title = f'{FACE_LABEL.get(self.face, self.face)} ({self.face})'
            p.drawText(QRect(2, 0, 200, self.TITLE_H), Qt.AlignLeft | Qt.AlignVCenter, title)
            tw = QFontMetrics(tf).horizontalAdvance(title)
            c = m.face_counts(self.face)
            sf = QFont(self._base)
            sf.setPixelSize(12)
            p.setFont(sf)
            p.setPen(theme.qc(theme.TEXT_2))
            parts = [f'적재 {c["OCCUPIED"]}/{c["total"]}']
            if c['RESERVED']:
                parts.append(f'예약 {c["RESERVED"]}')
            parts.append(f'빈 칸 {c["EMPTY"]}')
            if c['BLOCKED']:
                parts.append(f'사용중지 {c["BLOCKED"]}')
            p.drawText(QRect(tw + 14, 0, self.width() - tw - 16, self.TITLE_H), Qt.AlignLeft | Qt.AlignVCenter,
                       '  ·  '.join(parts))

        # 열 번호 (1열 = 도크 쪽)
        hf = QFont(self._base)
        hf.setPixelSize(11)
        p.setFont(hf)
        fm = QFontMetrics(hf)
        for col in range(1, m.columns + 1):
            r = QRect(int(x0 + (col - 1) * cw), self.TITLE_H, int(cw), self.HEAD_H)
            if col == 1:
                text = _first_fit(hf, ['1열 · 도크 쪽', '1 · 도크 쪽', '1 도크'], int(cw) - 4)
                p.setPen(theme.qc(theme.ACCENT_DARK))
                hb = QFont(hf)
                hb.setBold(True)
                p.setFont(hb)
                p.drawText(r.adjusted(2, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, '◀ ' + text if fm.horizontalAdvance(
                    '◀ ' + text) <= cw - 4 else text)
                p.setFont(hf)
            else:
                p.setPen(theme.qc(theme.TEXT_2))
                p.drawText(r, Qt.AlignCenter, f'{col}열' if cw >= 40 else str(col))

        # 단 이름 (위가 높은 단)
        for tier in range(1, m.tiers + 1):
            row = m.tiers - tier
            r = QRect(0, int(y0 + row * ch), self.GUTTER - 6, int(ch))
            p.setPen(theme.qc(theme.TEXT_2))
            p.drawText(r, Qt.AlignRight | Qt.AlignVCenter, f'{tier}단')

        # 칸
        for col in range(1, m.columns + 1):
            for tier in range(1, m.tiers + 1):
                sid = f'{self.face}-{col:02d}-{tier}'
                rect = self.cell_rect(col, tier)
                if not rect.adjusted(-4, -4, 4, 4).intersects(clip):
                    continue
                cell = m.cells.get(sid)
                if cell is None:
                    continue
                self._paint_cell(p, rect, sid, cell[0], cell[1])
        p.end()

    def _paint_cell(self, p: QPainter, rect: QRect, sid: str, s: Slot, b: Box | None) -> None:
        m = self.model
        radius = 4.0
        rf = QRectF(rect)
        pad = max(4, min(8, int(rect.width() * 0.07)))
        h = rect.height()
        w = rect.width()
        dim = m.searching and sid not in m.matches
        state = s.state

        if state == SlotState.OCCUPIED and b is not None:
            color = m.color(b.grade)
            p.setPen(Qt.NoPen)
            p.setBrush(theme.tint(color, 0.74))
            p.drawRoundedRect(rf, radius, radius)
            # 왼쪽 색 띠
            strip = max(4, int(w * 0.07))
            p.setBrush(theme.qc(color))
            p.setClipRect(QRectF(rf.x(), rf.y(), strip, rf.height()))
            p.drawRoundedRect(rf, radius, radius)
            p.setClipping(False)
            p.setPen(QPen(theme.mix(color, '#ffffff', 0.45), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rf.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

            left = rect.x() + strip + pad - 2
            avail = rect.right() - left - pad
            g = m.grades.get(b.grade)
            badge = max(13, min(17, int(h * 0.2)))
            rank = m.ranks.get(sid) if m.search_grade else None
            # ISO 분류 글자 (오른쪽 위)
            if g is not None and w >= 70:
                br = QRectF(rect.right() - pad - badge + 2, rect.y() + pad - 1, badge, badge)
                ic = theme.qc(theme.ISO_COLOR.get(g.iso_class, theme.TEXT_2))
                p.setPen(Qt.NoPen)
                p.setBrush(ic)
                p.drawRoundedRect(br, 3, 3)
                bf = QFont(self._base)
                bf.setBold(True)
                bf.setPixelSize(max(9, badge - 4))
                p.setFont(bf)
                p.setPen(theme.text_on(ic))
                p.drawText(br, Qt.AlignCenter, g.iso_class)
                avail_code = avail - badge - 2
            else:
                avail_code = avail
            code_px = max(11, min(19, int(h * 0.25), int(w * 0.2)))
            cf = _fit_font(self._base, b.grade, code_px, max(10, avail_code), bold=True, min_px=9)
            p.setFont(cf)
            p.setPen(theme.qc(theme.TEXT))
            code_h = QFontMetrics(cf).height()
            top = rect.y() + pad - 2
            p.drawText(QRect(left, top, avail_code + 2, code_h), Qt.AlignLeft | Qt.AlignVCenter, b.grade)
            # 둘째 줄: 로트 끝자리 · 경과
            sub_px = max(10, min(13, int(h * 0.17)))
            sf = QFont(self._base)
            sf.setPixelSize(sub_px)
            age = fmt_age(age_s(b.stored_at))
            sub = _first_fit(sf, [f'{lot_tail(b.lot)} · {age}', f'{lot_tail(b.lot)}·{age}', lot_tail(b.lot)], avail)
            p.setFont(sf)
            p.setPen(theme.qc(theme.CELL_SUB))
            sub_h = QFontMetrics(sf).height()
            y2 = top + code_h + max(1, int(h * 0.03))
            p.drawText(QRect(left, y2, avail + 2, sub_h), Qt.AlignLeft | Qt.AlignVCenter, sub)
            # 선입선출 순번 (재종 검색 중, 오른쪽 아래 — 1 = 가장 오래됨, 먼저 출고)
            d = max(16, min(21, int(h * 0.3)))
            if rank is not None:
                rr = QRectF(rect.right() - pad - d + 3, rect.bottom() - pad - d + 3, d, d)
                p.setPen(QPen(QColor('#ffffff'), 1.5))
                p.setBrush(theme.qc(theme.ACCENT if rank == 1 else theme.HEADER))
                p.drawEllipse(rr)
                nf = QFont(self._base)
                nf.setBold(True)
                nf.setPixelSize(max(9, int(d * 0.55)) if rank < 100 else max(8, int(d * 0.42)))
                p.setFont(nf)
                p.setPen(QColor('#ffffff'))
                p.drawText(rr, Qt.AlignCenter, str(rank))
                p.setFont(sf)
            # 셋째 줄: 잔량 박스 표시 (칸이 충분히 클 때) — 순번 동그라미와 겹치지 않게 폭을 줄인다
            if b.weight_kg < 15.0 and y2 + 2 * sub_h <= rect.bottom() - 2:
                avail3 = avail - (d + 2 if rank is not None else 0)
                p.setPen(theme.qc(theme.RESIDUAL))
                p.drawText(QRect(left, y2 + sub_h, avail3 + 2, sub_h), Qt.AlignLeft | Qt.AlignVCenter,
                           _first_fit(sf, [f'잔량 {b.weight_kg:g} kg', f'잔량 {b.weight_kg:g}', f'{b.weight_kg:g}'], avail3))

        elif state == SlotState.RESERVED:
            needs = bool(b is not None and b.needs_check)
            if needs:
                p.setPen(QPen(theme.qc(theme.ERROR), 2))
                p.setBrush(theme.qc(theme.ERROR_SOFT))
                p.drawRoundedRect(rf.adjusted(1, 1, -1, -1), radius, radius)
            else:
                pen = QPen(theme.qc(theme.ACCENT), 1.6, Qt.DashLine)
                pen.setDashPattern([4, 3])
                p.setPen(pen)
                p.setBrush(QColor('#ffffff'))
                p.drawRoundedRect(rf.adjusted(1, 1, -1, -1), radius, radius)
            left = rect.x() + pad
            avail = w - 2 * pad
            code = b.grade if b is not None else (s.reserved_for or '')
            code_px = max(11, min(17, int(h * 0.22), int(w * 0.18)))
            cf = _fit_font(self._base, code, code_px, max(10, avail), bold=True, min_px=9)
            p.setFont(cf)
            p.setPen(theme.qc(theme.TEXT_2) if not needs else theme.qc('#8f3a3a'))
            code_h = QFontMetrics(cf).height()
            top = rect.y() + pad - 2
            p.drawText(QRect(left, top, avail + 2, code_h), Qt.AlignLeft | Qt.AlignVCenter, code)
            sf = QFont(self._base)
            sf.setPixelSize(max(10, min(13, int(h * 0.17))))
            sf.setBold(True)
            if needs:
                text = _first_fit(sf, ['확인 필요', '확인'], avail)
                p.setPen(theme.qc(theme.ERROR))
            elif b is not None and b.state == BoxState.ON_AMR:
                text = _first_fit(sf, ['▶ AMR 운반 중', '▶ AMR'], avail)
                p.setPen(theme.qc(theme.ACCENT_DARK))
            else:
                text = _first_fit(sf, ['▶ AMR 배정', '▶ 예약'] if (b is not None and b.order_id) else ['예약 (수동)', '예약'],
                                  avail)
                p.setPen(theme.qc(theme.ACCENT_DARK))
            p.setFont(sf)
            y2 = top + code_h + max(1, int(h * 0.03))
            p.drawText(QRect(left, y2, avail + 2, QFontMetrics(sf).height()), Qt.AlignLeft | Qt.AlignVCenter, text)

        elif state == SlotState.BLOCKED:
            p.setPen(QPen(theme.qc(theme.BORDER_STRONG), 1))
            p.setBrush(theme.qc('#f1f3f5'))
            p.drawRoundedRect(rf.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            p.setBrush(_HATCH)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(rf.adjusted(1, 1, -1, -1), radius, radius)
            left = rect.x() + pad
            avail = w - 2 * pad
            tf = QFont(self._base)
            tf.setBold(True)
            tf.setPixelSize(max(10, min(13, int(h * 0.18))))
            fm = QFontMetrics(tf)
            label_text = _first_fit(tf, ['사용중지', '중지'], avail)
            reason = s.blocked_reason or ''
            rf2 = QFont(self._base)
            rf2.setPixelSize(max(9, min(12, int(h * 0.16))))
            fm2 = QFontMetrics(rf2)
            total_h = fm.height() + (fm2.height() if reason else 0)
            y = rect.y() + (h - total_h) // 2
            # 글자 뒤 바탕 (빗금 위에서 읽히게)
            tw = max(fm.horizontalAdvance(label_text), fm2.horizontalAdvance(fm2.elidedText(reason, Qt.ElideRight, avail))
                     if reason else 0)
            back = QRectF(rect.x() + (w - tw) / 2 - 4, y - 1, tw + 8, total_h + 2)
            p.setBrush(QColor(241, 243, 245, 235))
            p.drawRoundedRect(back, 3, 3)
            p.setFont(tf)
            p.setPen(theme.qc(theme.TEXT_2))
            p.drawText(QRect(left, y, avail, fm.height()), Qt.AlignHCenter | Qt.AlignVCenter, label_text)
            if reason:
                p.setFont(rf2)
                p.setPen(theme.qc(theme.TEXT_2))
                p.drawText(QRect(left, y + fm.height(), avail, fm2.height()), Qt.AlignHCenter | Qt.AlignVCenter,
                           fm2.elidedText(reason, Qt.ElideRight, avail))

        else:   # EMPTY
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qc(theme.EMPTY_CELL))
            p.drawRoundedRect(rf, radius, radius)
            ef = QFont(self._base)
            ef.setPixelSize(max(9, min(12, int(h * 0.15))))
            p.setFont(ef)
            p.setPen(theme.qc(theme.EMPTY_ID))
            p.drawText(rect, Qt.AlignCenter, _first_fit(ef, [sid, sid[2:]], w - 4))

        if dim:
            p.setPen(Qt.NoPen)
            p.setBrush(_DIM)
            p.drawRoundedRect(rf, radius, radius)
        elif m.search_text and sid in m.matches:
            p.setPen(QPen(theme.qc(theme.ACCENT), 2.4))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rf.adjusted(1, 1, -1, -1), radius, radius)

        if sid == m.selected:
            p.setPen(QPen(theme.qc(theme.HEADER), 3))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rf.adjusted(-0.5, -0.5, 0.5, 0.5), radius + 1, radius + 1)
        elif sid == self.hover:
            p.setPen(QPen(theme.qc(theme.ACCENT), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rf.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


# ====================================================================== 범례
def color_icon(color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(theme.mix(color, '#000000', 0.25), 1))
    p.setBrush(theme.qc(color))
    p.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), 3, 3)
    p.end()
    return QIcon(pm)


class LegendBar(QWidget):
    """재종별 수량 칩 (재고·예약이 있는 재종만). 누르면 그 재종 검색."""

    grade_clicked = pyqtSignal(str)

    def __init__(self, model: RackModel, parent: QWidget | None = None):
        super().__init__(parent)
        self.model = model
        self.flow = FlowLayout(self, spacing=5)
        self._sig: tuple = ()
        self._buttons: dict[str, QPushButton] = {}
        self.empty = label('적재된 박스가 없습니다', 'Faint')
        self.flow.addWidget(self.empty)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return self.flow.heightForWidth(w)

    def refresh(self) -> None:
        counts = self.model.grade_counts()
        sig = tuple((g, n, r, self.model.color(g)) for g, n, r in counts)
        if sig != self._sig:
            self._sig = sig
            while self.flow.count():
                it = self.flow.takeAt(0)
                w = it.widget()
                if w is not None and w is not self.empty:
                    w.hide()               # deleteLater 전까지 옛 칩이 겹쳐 보이지 않게
                    w.setParent(None)
                    w.deleteLater()
            self._buttons = {}
            if not counts:
                self.flow.addWidget(self.empty)
                self.empty.show()
            else:
                self.empty.hide()
                for g, n, r in counts:
                    text = f'{g}  {n}' + (f' (+{r})' if r else '')
                    b = button(text, 'chip', parent=self)
                    b.setIcon(color_icon(self.model.color(g)))
                    b.setCheckable(True)
                    mouse_only(b)
                    tip = f'{g}: 적재 {n}박스' + (f', 들어오는 중 {r}' if r else '') + ' — 누르면 적재대에서 찾기'
                    b.setToolTip(tip)
                    b.clicked.connect(lambda _c=False, code=g: self.grade_clicked.emit(code))
                    self.flow.addWidget(b)
                    if self.isVisible():
                        b.show()          # 바로 보이게 (보통은 다음 이벤트 때 보인다)
                    self._buttons[g] = b
            self.flow.activate()
            self.updateGeometry()
        for g, b in self._buttons.items():
            b.blockSignals(True)
            b.setChecked(g == self.model.search_grade)
            b.blockSignals(False)


# ====================================================================== 패널
class RackPanel(Panel):
    """왼쪽 적재대 패널: 검색 줄 + 북면/남면 + 범례."""

    slot_selected = pyqtSignal(str)

    def __init__(self, ctrl, parent: QWidget | None = None):
        super().__init__('', parent, margins=(12, 10, 12, 10), spacing=6)
        self.ctrl = ctrl
        self.model = RackModel(ctrl)

        self.search = QLineEdit()
        self.search.setPlaceholderText('재종·박스·로트·칸 검색')
        self.search.setClearButtonEnabled(True)
        latin_input(self.search)          # 재종·박스·로트·칸은 영문·숫자 (리더로 박스 라벨을 읽어도 'ㅔㅊ' 가 안 되게)
        self.search.setMinimumWidth(170)
        self.search.setMaximumWidth(240)
        self.grade_combo = QComboBox()
        self.grade_combo.setMinimumWidth(150)
        self.grade_combo.setMaxVisibleItems(20)
        self.grade_combo.setToolTip('재종을 고르면 그 재종 칸만 보이고 출고 순서(선입선출) 번호가 붙습니다')
        self.clear_btn = button('검색 지우기', tip='검색을 지우고 전체 칸을 보여 줍니다 (Esc)')
        mouse_only(self.clear_btn)
        self.result = ElidedLabel('', 'Muted')
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(label('중간 적재대', 'PanelTitle'))
        top.addSpacing(6)
        top.addWidget(self.result, 1)
        top.addWidget(self.search)
        top.addWidget(self.grade_combo)
        top.addWidget(self.clear_btn)
        self.outer.addLayout(top)

        self.north = RackFaceView('N', self.model)
        self.south = RackFaceView('S', self.model)
        self.outer.addWidget(self.north, 1)
        self.outer.addWidget(self.south, 1)
        self.legend = LegendBar(self.model)
        legend_row = QHBoxLayout()
        legend_row.setSpacing(8)
        lt = label('재종별 수량', 'Muted')
        lt.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lt.setContentsMargins(0, 3, 0, 0)
        legend_row.addWidget(lt, 0, Qt.AlignTop)
        legend_row.addWidget(self.legend, 1)
        self.outer.addLayout(legend_row)
        self.hint = label(HINT_TEXT, 'Faint')
        self.hint.setToolTip('칸을 누르면 오른쪽 위에 칸 정보와 작업 버튼이 나옵니다. 칸에 마우스를 올리면 자세한 내용이 보입니다.')
        self.hint.setWordWrap(True)
        self.outer.addWidget(self.hint)

        self.north.clicked.connect(self.slot_selected)
        self.south.clicked.connect(self.slot_selected)
        self.legend.grade_clicked.connect(self._legend_clicked)
        self.search.textEdited.connect(self._text_search)
        self.search.returnPressed.connect(lambda: self._text_search(self.search.text()))
        self.grade_combo.activated.connect(self._combo_picked)
        self.clear_btn.clicked.connect(self.clear_search)

    # ---- 갱신
    def reload(self) -> None:
        self.model.reload_all()
        self._fill_combo()
        self.north.updateGeometry()
        self.south.updateGeometry()
        self.north.update()
        self.south.update()
        self.legend.refresh()
        self._show_result()

    def reload_grades(self) -> None:
        self.model.reload_grades()
        self._fill_combo()
        self.repaint_all()

    def update_slots(self, sids) -> None:
        changed = self.model.update_slots(sids)
        if not changed:
            return
        if self.model.searching:
            self.model.recompute_search()
            self.repaint_all()
        else:
            for sid in changed:
                self.north.update_cell(sid)
                self.south.update_cell(sid)
            self.north.update(QRect(0, 0, self.north.width(), RackFaceView.TITLE_H))
            self.south.update(QRect(0, 0, self.south.width(), RackFaceView.TITLE_H))
        self.legend.refresh()
        self._show_result()

    def repaint_all(self) -> None:
        self.north.update()
        self.south.update()
        self.legend.refresh()

    def set_selected(self, sid: str | None) -> None:
        old, self.model.selected = self.model.selected, sid
        for s in (old, sid):
            if s:
                self.north.update_cell(s)
                self.south.update_cell(s)

    # ---- 검색
    def _fill_combo(self) -> None:
        cur = self.model.search_grade
        self.grade_combo.blockSignals(True)
        self.grade_combo.clear()
        self.grade_combo.addItem('재종 전체', None)
        for g in self.model.grades.values():
            self.grade_combo.addItem(color_icon(g.color), g.code + ('' if g.active else ' (사용중지)'), g.code)
        i = self.grade_combo.findData(cur) if cur else 0
        self.grade_combo.setCurrentIndex(max(0, i))
        self.grade_combo.blockSignals(False)

    def _combo_picked(self, index: int) -> None:
        code = self.grade_combo.itemData(index)
        self.set_grade(code)

    def _legend_clicked(self, code: str) -> None:
        self.set_grade(None if self.model.search_grade == code else code)

    def _text_search(self, text: str) -> None:
        t = (text or '').strip().upper()
        if t and t in self.model.grades:
            self.model.set_search(t)
        else:
            self.model.set_search(None, t)
        self._sync_controls(keep_text=True)
        self.repaint_all()
        self._show_result()

    def set_grade(self, code: str | None) -> None:
        self.model.set_search(code)
        self._sync_controls(keep_text=False)
        self.repaint_all()
        self._show_result()

    def clear_search(self) -> None:
        self.set_grade(None)

    def _sync_controls(self, keep_text: bool) -> None:
        code = self.model.search_grade
        self.grade_combo.blockSignals(True)
        i = self.grade_combo.findData(code) if code else 0
        self.grade_combo.setCurrentIndex(max(0, i))
        self.grade_combo.blockSignals(False)
        if not keep_text:
            self.search.blockSignals(True)
            self.search.setText(code or '')
            self.search.blockSignals(False)

    def _show_result(self) -> None:
        m = self.model
        hint = HINT_TEXT
        if m.search_grade:
            n = len(m.ranks)
            incoming = len(m.matches) - n
            text = f'{m.search_grade} {n}박스' + (f' +{incoming} 입고 중' if incoming else '')
            if n:
                hint = (f'{m.search_grade} 칸 오른쪽 아래 숫자 = 출고 순서 (선입선출, 1번이 가장 오래됨) · '
                        '흐린 칸 = 다른 재종 · Esc = 검색 지우기')
            else:
                text += ' · 적재대에 없음'
        elif m.search_text:
            text = f"'{m.search_text}' {len(m.matches)}칸"
            hint = '테두리 칸 = 검색어가 든 칸 (재종·박스·로트·칸 번호·사용중지 사유) · Esc = 검색 지우기'
        else:
            text = ''
        self.result.setText(text)
        self.hint.setText(hint)
