"""AMR·작업 탭: 셀 미니맵(위에서 본 그림), AMR 카드, 작업 목록, 일시정지·재개·취소·비상정지."""
from __future__ import annotations

import math

from PyQt5.QtCore import QPointF, QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QProgressBar, QScrollArea, QSizePolicy, QSplitter,
                             QVBoxLayout, QWidget)

from amr.models import MODE_LABEL, ORDER_LABEL, OrderState, RobotStatus, TransportOrder
from core.inventory import InventoryError
from core.models import SlotState

from . import theme
from .table import Row, RowTable
from .util import ask, fmt_sim, run_action
from .widgets import Chip, ElidedLabel, button, label

KIND_LABEL = {'INBOUND': '입고', 'OUTBOUND': '출고', 'MOVE': '이동', 'CHARGE': '충전', 'PARK': '주차'}
ACTION_LABEL = {'pick': '집기', 'drop': '내려놓기', 'charge': '충전', 'none': '이동'}


def step_text(order: TransportOrder | None) -> str:
    if order is None:
        return '작업 없음'
    n = len(order.steps)
    if order.current < n:
        s = order.steps[order.current]
        what = ACTION_LABEL.get(s.action, s.action)
        target = s.meta.get('slot') if s.action == 'drop' else s.node
        return f'{order.id} {KIND_LABEL.get(order.kind, order.kind)} · {order.current + 1}/{n} {what} {target or ""}'.strip()
    return f'{order.id} {KIND_LABEL.get(order.kind, order.kind)} · {n}/{n}'


def order_content(o: TransportOrder) -> str:
    if o.kind == 'INBOUND':
        drops = [s for s in o.steps if s.action == 'drop']
        parts = [f'{s.meta.get("box", "?")}→{s.meta.get("slot", s.ref)}' for s in drops]
        text = ', '.join(parts)
    elif o.kind == 'CHARGE':
        tgt = next((s.meta.get('target') for s in o.steps if s.action == 'charge'), None)
        text = f'충전기에서 {tgt:g}% 까지' if isinstance(tgt, (int, float)) else '충전기'
    elif o.kind == 'PARK':
        text = f'{o.steps[-1].node} 로 주차' if o.steps else '주차'
    else:
        text = ' → '.join(s.node for s in o.steps)
    if o.error and o.state in (OrderState.FAILED, OrderState.CANCELLED):
        text = f'{o.error} · {text}' if text else o.error
    return text


# ====================================================================== 미니맵
class MiniMap(QWidget):
    """셀 지도: 통로·노드·적재대(칸별 작은 사각형)·도크·컨베이어·충전기·주차·AMR."""

    BOX_M = 0.42          # 컨베이어 위 박스 한 개 길이 (그림용, m)

    def __init__(self, win, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._grade_of: dict[str, str] = {}

    def sizeHint(self) -> QSize:
        return QSize(560, 250)

    @property
    def ctrl(self):
        return self.win.ctrl

    def forget_boxes(self) -> None:
        self._grade_of.clear()

    def _box_grade(self, bid: str) -> str | None:
        key = bid.upper()
        g = self._grade_of.get(key)
        if g is None:
            try:
                g = self.ctrl.inventory.box(bid).grade
            except InventoryError:
                g = ''
            self._grade_of[key] = g
        return g or None

    def _world(self):
        topo = self.ctrl.topology
        cfg = self.ctrl.inventory.layout.cfg
        x0, y0, x1, y1 = topo.bounds()
        dock = topo.nodes.get('DOCK')
        cap = int(self.ctrl.config.conveyor_capacity)
        conv_len = cap * self.BOX_M + 0.3
        if dock is not None:
            x0 = min(x0, dock.x - 0.35 - conv_len - 0.2)
        length = cfg.columns * cfg.col_pitch_m
        x1 = max(x1, length)
        return x0 - 0.2, y0 - 0.32, x1 + 0.25, y1 + 0.42, conv_len

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p: QPainter) -> None:
        ctrl = self.ctrl
        topo = ctrl.topology
        cfg = ctrl.inventory.layout.cfg
        model = self.win.rack.model
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), theme.qc(theme.PANEL))
        wx0, wy0, wx1, wy1, conv_len = self._world()
        s = min((W - 12) / max(0.1, wx1 - wx0), (H - 12) / max(0.1, wy1 - wy0))
        ox = (W - (wx1 - wx0) * s) / 2 - wx0 * s
        oy = (H - (wy1 - wy0) * s) / 2 - wy0 * s

        def pt(x: float, y: float) -> QPointF:
            return QPointF(ox + x * s, oy + y * s)

        def rect(x0: float, y0: float, x1: float, y1: float) -> QRectF:
            return QRectF(pt(x0, y0), pt(x1, y1)).normalized()

        base = QFont(self.font())
        small = QFont(base)
        small.setPixelSize(max(10, min(13, int(s * 0.22))))
        bold = QFont(small)
        bold.setBold(True)

        # 바닥 격자 (1 m)
        floor = rect(wx0 + 0.1, wy0 + 0.1, wx1 - 0.1, wy1 - 0.1)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qc(theme.PANEL_2))
        p.drawRoundedRect(floor, 6, 6)
        p.setPen(QPen(theme.qc(theme.GRID), 1))
        gx = math.ceil(wx0)
        while gx < wx1:
            p.drawLine(pt(gx, wy0 + 0.1), pt(gx, wy1 - 0.1))
            gx += 1
        gy = math.ceil(wy0)
        while gy < wy1:
            p.drawLine(pt(wx0 + 0.1, gy), pt(wx1 - 0.1, gy))
            gy += 1

        # 통로 (구간)
        nodes = topo.nodes
        seen = set()
        lane = QPen(theme.qc(theme.LANE), max(4.0, 0.5 * s), Qt.SolidLine, Qt.RoundCap)
        center = QPen(theme.qc(theme.BORDER_STRONG), 1.2, Qt.DashLine)
        center.setDashPattern([3, 3])
        edges = []
        for a in nodes:
            for b, _d in topo.neighbors(a):
                key = tuple(sorted((a, b)))
                if key in seen:
                    continue
                seen.add(key)
                edges.append((nodes[a], nodes[b]))
        p.setPen(lane)
        for na, nb in edges:
            p.drawLine(pt(na.x, na.y), pt(nb.x, nb.y))
        p.setPen(center)
        for na, nb in edges:
            p.drawLine(pt(na.x, na.y), pt(nb.x, nb.y))

        # 적재대
        length = cfg.columns * cfg.col_pitch_m
        half = cfg.depth_m / 2
        rack = rect(0, -half, length, half)
        p.setPen(QPen(theme.qc(theme.TEXT_3), 1.2))
        p.setBrush(theme.qc(theme.MUTED_FILL))
        p.drawRect(rack)
        p.setPen(QPen(theme.qc(theme.TEXT_3), 1))
        p.drawLine(pt(0, 0), pt(length, 0))
        tiers = max(1, cfg.tiers)
        for face, sign in (('N', -1), ('S', 1)):
            for col in range(1, cfg.columns + 1):
                cx0 = (col - 1) * cfg.col_pitch_m
                # 면 쪽 반 깊이 안에 단 수만큼 작은 칸 (1단 = 통로 쪽)
                inner, outer = 0.04, half - 0.04
                span = (outer - inner) / tiers
                for t in range(1, tiers + 1):
                    d0 = outer - t * span
                    d1 = d0 + span
                    y0, y1 = (sign * d0, sign * d1)
                    r = rect(cx0 + 0.05, y0, cx0 + cfg.col_pitch_m - 0.05, y1).adjusted(0.6, 0.6, -0.6, -0.6)
                    cell = model.cells.get(f'{face}-{col:02d}-{t}')
                    if cell is None:
                        continue
                    sl, bx = cell
                    if sl.state == SlotState.OCCUPIED and bx is not None:
                        p.setPen(Qt.NoPen)
                        p.setBrush(theme.qc(model.color(bx.grade)))
                    elif sl.state == SlotState.RESERVED:
                        p.setPen(QPen(theme.qc(theme.ERROR if (bx and bx.needs_check) else theme.ACCENT), 1.2))
                        p.setBrush(theme.qc(theme.RESERVED_CELL))
                    elif sl.state == SlotState.BLOCKED:
                        p.setPen(Qt.NoPen)
                        p.setBrush(theme.qc(theme.NODE_2))
                    else:
                        p.setPen(QPen(theme.qc(theme.BORDER), 0.8))
                        p.setBrush(theme.qc(theme.EMPTY_CELL))
                    p.drawRect(r)
        p.setFont(bold)
        p.setPen(theme.qc(theme.TEXT_2))
        p.drawText(rect(-0.42, -half, -0.02, 0), Qt.AlignRight | Qt.AlignVCenter, 'N')
        p.drawText(rect(-0.42, 0, -0.02, half), Qt.AlignRight | Qt.AlignVCenter, 'S')

        # 통로 이름
        p.setFont(small)
        p.setPen(theme.qc(theme.TEXT_3))
        sfm = QFontMetrics(small)
        fixed: list[QRectF] = []           # 고정 글자 자리 (AMR 이름표가 가리지 않게)
        yn = nodes['N01'].y if 'N01' in nodes else -half - 0.6
        ys = nodes['S01'].y if 'S01' in nodes else half + 0.6
        for text, r in (('북면 통로', rect(length / 2 - 1.5, yn - 0.32, length / 2 + 1.5, yn - 0.08)),
                        ('남면 통로', rect(length / 2 - 1.5, ys + 0.08, length / 2 + 1.5, ys + 0.32))):
            p.drawText(r, Qt.AlignCenter, text)
            tw = sfm.horizontalAdvance(text)
            fixed.append(QRectF(r.center().x() - tw / 2, r.center().y() - sfm.height() / 2, tw, sfm.height()))

        # 노드
        for n in nodes.values():
            c = pt(n.x, n.y)
            if n.kind == 'aisle':
                p.setPen(Qt.NoPen)
                p.setBrush(theme.qc(theme.NODE))
                p.drawEllipse(c, 2.4, 2.4)
            elif n.kind == 'junction':
                p.setPen(Qt.NoPen)
                p.setBrush(theme.qc(theme.NODE_2))
                p.drawEllipse(c, 1.8, 1.8)
        # 충전기·주차
        for n in nodes.values():
            if n.kind not in ('charger', 'park'):
                continue
            r = rect(n.x - 0.32, n.y - 0.26, n.x + 0.32, n.y + 0.26)
            if n.kind == 'charger':
                p.setPen(QPen(theme.qc(theme.WARN), 1.2))
                p.setBrush(theme.qc(theme.WARN_SOFT))
                text = '충전'
            else:
                pen = QPen(theme.qc(theme.TEXT_3), 1, Qt.DashLine)
                pen.setDashPattern([3, 2])
                p.setPen(pen)
                p.setBrush(theme.qc(theme.PANEL_2))
                text = 'P' + n.id[4:]
            p.drawRoundedRect(r, 3, 3)
            fixed.append(QRectF(r))
            f = QFont(small)
            while QFontMetrics(f).horizontalAdvance(text) > r.width() - 2 and f.pixelSize() > 7:
                f.setPixelSize(f.pixelSize() - 1)
            if QFontMetrics(f).horizontalAdvance(text) <= r.width() - 1:
                p.setFont(f)
                p.setPen(theme.qc(theme.WARN) if n.kind == 'charger' else theme.qc(theme.TEXT_2))
                p.drawText(r, Qt.AlignCenter, text)

        # 도크 + 컨베이어
        dock = nodes.get('DOCK')
        if dock is not None:
            conv = ctrl.inventory.conveyor()
            cap = int(ctrl.config.conveyor_capacity)
            cr = rect(dock.x - 0.32 - conv_len, dock.y - 0.24, dock.x - 0.32, dock.y + 0.24)
            p.setPen(QPen(theme.qc(theme.TEXT_3), 1))
            p.setBrush(theme.qc(theme.MUTED_FILL))
            p.drawRoundedRect(cr, 3, 3)
            p.setPen(QPen(theme.qc(theme.BORDER), 1))
            x = cr.left() + 4
            while x < cr.right() - 2:
                p.drawLine(QPointF(x, cr.top() + 2), QPointF(x, cr.bottom() - 2))
                x += max(4.0, 0.12 * s)
            for i in range(min(cap, len(conv))):
                b = conv[i]
                bx1 = dock.x - 0.32 - 0.1 - i * self.BOX_M
                br = rect(bx1 - self.BOX_M + 0.06, dock.y - 0.18, bx1, dock.y + 0.18)
                p.setPen(QPen(theme.qc(theme.ERROR), 1.6) if b.needs_check else QPen(theme.qc(theme.BG), 1))
                p.setBrush(theme.qc(model.color(b.grade)))
                p.drawRoundedRect(br, 2, 2)
            p.setFont(small)
            p.setPen(theme.qc(theme.ERROR) if len(conv) >= cap else theme.qc(theme.TEXT_2))
            full = len(conv) >= cap
            ctext = f'컨베이어 {len(conv)}/{cap}' + (' 가득' if full else '')
            cw_ = sfm.horizontalAdvance(ctext)
            lr = QRectF(cr.center().x() - cw_ / 2, cr.bottom() + 1, cw_, sfm.height())
            p.drawText(lr, Qt.AlignCenter, ctext)
            fixed.append(lr)
            p.setPen(theme.qc(theme.TEXT_2))
            otext = '오버브릿지 →'
            if sfm.horizontalAdvance(otext) <= cr.width() + 4:      # 작은 지도에서는 생략 (글자가 겹친다)
                orr = QRectF(cr.left(), cr.top() - sfm.height() - 1, sfm.horizontalAdvance(otext), sfm.height())
                p.drawText(orr, Qt.AlignLeft | Qt.AlignVCenter, otext)
                fixed.append(orr)
            dr = rect(dock.x - 0.3, dock.y - 0.26, dock.x + 0.3, dock.y + 0.26)
            owner = ctrl.fleet.zone_owner('DOCK')
            p.setPen(QPen(theme.qc(theme.ACCENT_DARK), 1.4))
            p.setBrush(theme.qc(theme.ACCENT_SOFT if not owner else '#5a3a0c'))
            p.drawRoundedRect(dr, 3, 3)
            p.setFont(bold)
            p.setPen(theme.qc(theme.ACCENT_DARK))
            p.drawText(dr, Qt.AlignCenter, '도크')
            fixed.append(QRectF(dr))

        # 제목 (왼쪽 위)
        tf = QFont(base)
        tf.setPixelSize(13)
        tf.setBold(True)
        p.setFont(tf)
        p.setPen(theme.qc(theme.TEXT))
        p.drawText(QRectF(10, 6, 200, 18), Qt.AlignLeft | Qt.AlignVCenter, '셀 지도')

        # AMR
        robots = ctrl.fleet.robots()
        orders = {st.robot_id: ctrl.fleet.robot_order(st.robot_id) for st in robots}
        for st in robots:
            self._paint_path(p, st, orders.get(st.robot_id), pt)
        bodies = [self._paint_robot(p, st, pt, s) for st in robots]
        tags: list[QRectF] = []
        for st, c, half in zip(robots, [b[0] for b in bodies], [b[1] for b in bodies]):
            self._paint_tag(p, st, c, half, bold, tags, fixed)

    def _paint_path(self, p: QPainter, st: RobotStatus, order: TransportOrder | None, pt) -> None:
        if order is None or order.current >= len(order.steps) or not st.connected:
            return
        step = order.steps[order.current]
        path = list(step.path or [])
        nodes = self.ctrl.topology.nodes
        if not path or st.node not in path:
            return
        rest = path[path.index(st.node) + 1:]
        if not rest:
            return
        color = theme.qc(theme.MODE_COLOR.get(st.mode.value, theme.ACCENT))
        pen = QPen(color, 2, Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        prev = pt(st.x, st.y)
        for nid in rest:
            n = nodes.get(nid)
            if n is None:
                break
            cur = pt(n.x, n.y)
            p.drawLine(prev, cur)
            prev = cur
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(prev, 3.5, 3.5)

    def _paint_robot(self, p: QPainter, st: RobotStatus, pt, s: float):
        mode = st.mode.value
        color = theme.qc(theme.MODE_COLOR.get(mode, '#6b7d87'))
        c = pt(st.x, st.y)
        L, Wd = max(18.0, 0.7 * s), max(13.0, 0.5 * s)
        p.save()
        p.translate(c)
        p.rotate(math.degrees(st.heading))
        body = QRectF(-L / 2, -Wd / 2, L, Wd)
        p.setPen(QPen(theme.mix(color, '#000000', 0.3), 1.4))
        p.setBrush(color)
        p.drawRoundedRect(body, 4, 4)
        # 앞쪽 표시
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 210))
        tri = QPolygonF([QPointF(L / 2 - 2, 0), QPointF(L / 2 - 7, -4), QPointF(L / 2 - 7, 4)])
        p.drawPolygon(tri)
        # 트레이 (사다리 단) — 실은 박스 재종 색
        n = len(st.trays)
        if n:
            d = max(4.0, min(Wd * 0.42, (L - 12) / n - 2))
            for i, bid in enumerate(st.trays):
                x = -L / 2 + 6 + i * (d + 2) + d / 2
                if bid:
                    g = self._box_grade(bid)
                    p.setPen(QPen(QColor('#ffffff'), 1))
                    p.setBrush(theme.qc(self.win.rack.model.color(g)) if g else QColor('#ffffff'))
                else:
                    p.setPen(QPen(QColor(255, 255, 255, 200), 1))
                    p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(x, 0), d / 2, d / 2)
        p.restore()
        return c, max(L, Wd) / 2

    def _paint_tag(self, p: QPainter, st: RobotStatus, c: QPointF, half: float, bold: QFont,
                   taken: list[QRectF], fixed: list[QRectF] | None = None) -> None:
        """이름표 (회전 없음). 다른 AMR 이름표·고정 글자(도크·컨베이어·통로 이름)와 겹치면 위·아래·옆으로 비켜 놓는다."""
        mode = st.mode.value
        text = f'{st.robot_id} · {MODE_LABEL.get(mode, mode)}'
        if not st.connected:
            text = f'{st.robot_id} · 연결 끊김'
        p.setFont(bold)
        fm = QFontMetrics(bold)
        tw, th = fm.horizontalAdvance(text) + 10, fm.height() + 3
        above = c.y() - half - th - 4
        below = c.y() + half + 4
        cands = [QPointF(c.x() - tw / 2, above), QPointF(c.x() - tw / 2, below),
                 QPointF(c.x() - tw / 2, above - th - 2), QPointF(c.x() - tw / 2, below + th + 2),
                 QPointF(c.x() + half + 4, c.y() - th / 2), QPointF(c.x() - half - 4 - tw, c.y() - th / 2)]
        tag = None
        for pos in cands:
            r = QRectF(pos.x(), pos.y(), tw, th)
            r.moveLeft(max(2, min(self.width() - tw - 2, r.left())))
            if r.top() < 2 or r.bottom() > self.height() - 2:
                continue
            clear_of_tags = not any(r.adjusted(-2, -1, 2, 1).intersects(t) for t in taken)
            if clear_of_tags and not any(r.intersects(f) for f in (fixed or ())):
                tag = r
                break
        if tag is None:      # 고정 글자는 못 피해도 다른 이름표와는 겹치지 않게
            for pos in cands:
                r = QRectF(pos.x(), pos.y(), tw, th)
                r.moveLeft(max(2, min(self.width() - tw - 2, r.left())))
                inside = r.top() >= 2 and r.bottom() <= self.height() - 2
                if inside and not any(r.adjusted(-2, -1, 2, 1).intersects(t) for t in taken):
                    tag = r
                    break
        if tag is None:
            tag = QRectF(max(2, min(self.width() - tw - 2, c.x() - tw / 2)), max(2, above), tw, th)
        taken.append(tag)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(23, 25, 28, 235))
        p.drawRoundedRect(tag, 4, 4)
        p.setPen(QColor('#ffffff'))
        p.drawText(tag, Qt.AlignCenter, text)


# ====================================================================== AMR 카드
class RobotCard(QFrame):
    """AMR 한 대 (두 줄): 이름·모드·배터리·위치 / 트레이·현재 작업."""

    HEIGHT = 62

    def __init__(self, robot_id: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.robot_id = robot_id
        self.setObjectName('Card')
        self.setStyleSheet(f'QFrame#Card {{ background: {theme.PANEL_2}; border: 1px solid {theme.BORDER}; border-radius: 6px; }}')
        self.setFixedHeight(self.HEIGHT)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name = label(robot_id, 'PanelTitle')
        self.mode = Chip('', theme.TEXT_2)
        top.addWidget(self.name)
        top.addWidget(self.mode)
        top.addSpacing(4)
        top.addWidget(label('배터리', 'FieldName'))
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(72, 9)
        top.addWidget(self.bar)
        self.bat_text = label('', 'FieldValue')
        self.bat_text.setMinimumWidth(self.fontMetrics().horizontalAdvance('100%') + 4)
        top.addWidget(self.bat_text)
        self.pos = ElidedLabel('', 'Muted')
        self.pos.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.pos, 1)
        lay.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        bottom.addWidget(label('트레이', 'FieldName'))
        self.trays = QHBoxLayout()
        self.trays.setSpacing(3)
        bottom.addLayout(self.trays)
        bottom.addSpacing(10)
        self.job = ElidedLabel('', None)
        self.job.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bottom.addWidget(self.job, 1)
        lay.addLayout(bottom)
        self._tray_chips: list[Chip] = []
        self._sig = None

    def update_status(self, st: RobotStatus, order: TransportOrder | None, model) -> None:
        mode = st.mode.value if st.connected else 'OFFLINE'
        sig = (mode, round(st.battery), st.node, round(st.fork_height, 2), tuple(st.trays), step_text(order), st.message)
        if sig == self._sig:
            return
        self._sig = sig
        self.mode.set(MODE_LABEL.get(mode, mode), theme.MODE_COLOR.get(mode, theme.TEXT_2))
        b = max(0.0, min(100.0, st.battery))
        self.bar.setValue(int(round(b)))
        lvl = 'error' if b <= 15 else 'warn' if b <= 30 else ''
        if self.bar.property('level') != lvl:
            self.bar.setProperty('level', lvl)
            self.bar.style().unpolish(self.bar)
            self.bar.style().polish(self.bar)
        self.bat_text.setText(f'{b:.0f}%')
        where = st.node or '-'
        pos = f'위치 {where} · 포크 {st.fork_height:.2f} m' + (f' · {st.message}' if st.message else '')
        self.pos.setText(pos)
        while len(self._tray_chips) < len(st.trays):
            ch = Chip('', theme.TEXT_2)
            self._tray_chips.append(ch)
            self.trays.addWidget(ch)
        for i, ch in enumerate(self._tray_chips):
            if i >= len(st.trays):
                ch.hide()
                continue
            ch.show()
            bid = st.trays[i]
            if bid:
                try:
                    box = model.ctrl.inventory.box(bid)
                    grade, needs = box.grade, box.needs_check
                except InventoryError:
                    grade, needs = '?', False
                ch.set(f'{i} {grade}', theme.ERROR if needs else model.color(grade))
                ch.setToolTip(f'{i}단 트레이: {bid} ({grade})' + (' — 확인 필요' if needs else ''))
            else:
                ch.set(f'{i} 빈', '#9aa5ab', filled=False)
                ch.setToolTip(f'{i}단 트레이: 비어 있음')
        job = step_text(order)
        self.job.setText(job)
        self.job.setStyleSheet(f'color: {theme.ACCENT_DARK}; font-weight: bold;' if order else f'color: {theme.TEXT_3};')


# ====================================================================== 탭
class AmrTab(QWidget):
    MAX_ORDERS = 300

    def __init__(self, win, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(6)
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.minimap = MiniMap(win)
        self.minimap.setToolTip('셀 지도 (위에서 본 그림)\n점선 = AMR 이 가는 길 · AMR 위 점 = 실은 박스(재종 색)\n'
                                '적재대 작은 칸 = 칸별 재종 색 (흰 칸 = 예약, 회색 = 사용중지)')
        self.minimap.setObjectName('MapFrame')
        self.split.addWidget(self.minimap)

        lower = QWidget()
        low = QVBoxLayout(lower)
        low.setContentsMargins(0, 0, 0, 0)
        low.setSpacing(6)
        self.cards_area = QScrollArea()
        self.cards_area.setWidgetResizable(True)
        self.cards_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.cards_host = QWidget()
        self.cards_host.setObjectName('CardsHost')
        self.cards_host.setStyleSheet('QWidget#CardsHost { background: transparent; }')
        self.cards_lay = QVBoxLayout(self.cards_host)
        self.cards_lay.setContentsMargins(0, 0, 0, 0)
        self.cards_lay.setSpacing(6)
        self.cards_lay.addStretch(1)
        self.cards_area.setWidget(self.cards_host)
        self.cards_area.setFixedHeight(RobotCard.HEIGHT)
        low.addWidget(self.cards_area)

        ohead = QHBoxLayout()
        ohead.addWidget(label('작업 목록', 'PanelTitle'))
        self.order_count = label('', 'Muted')
        ohead.addWidget(self.order_count)
        ohead.addStretch(1)
        self.show_park = QCheckBox('주차 작업도 표시')
        ohead.addWidget(self.show_park)
        low.addLayout(ohead)
        self.table = RowTable(['작업', '종류', '상태', 'AMR', '진행', '내용', '생성', '완료'],
                              ['O0000', '입고', '진행', 'AMR-01', '6/6', None, '0:00:00', '0:00:00'],
                              stretch=5, align={4: Qt.AlignCenter},
                              min_stretch=160)
        self.table.empty_text = '작업이 없습니다'
        # 최소 2줄 + 머리글 + 가로 스크롤바 자리 (좁은 화면에서 스크롤바가 둘째 줄을 가리지 않게)
        self.table.setMinimumHeight(2 * self.table.verticalHeader().defaultSectionSize() + 30 + 12)
        low.addWidget(self.table, 1)
        acts = QHBoxLayout()
        acts.setSpacing(6)
        self.pause_btn = button('일시정지 전체', tip='모든 AMR 을 멈춥니다 (작업은 남음)')
        self.resume_btn = button('재개', 'primary', '일시정지·비상정지를 풉니다')
        self.cancel_btn = button('선택 작업 취소', tip='고른 작업을 취소합니다 (실린 박스는 확인 필요)')
        self.estop_btn = button('비상정지', 'danger', '모든 AMR 즉시 정지')
        acts.addWidget(self.pause_btn)
        acts.addWidget(self.resume_btn)
        acts.addWidget(self.cancel_btn)
        acts.addStretch(1)
        acts.addWidget(self.estop_btn)
        low.addLayout(acts)
        self.split.addWidget(lower)
        # 지도를 크게 (1280×800·1440×900 에서 지도가 너무 작아 글자가 겹쳤다). 작업 표는 최소 2줄 + 스크롤.
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setSizes([360, 220])
        lay.addWidget(self.split, 1)

        self.cards: dict[str, RobotCard] = {}
        self.pause_btn.clicked.connect(self._pause)
        self.resume_btn.clicked.connect(self._resume)
        self.cancel_btn.clicked.connect(self._cancel)
        self.estop_btn.clicked.connect(self._estop)
        self.show_park.toggled.connect(lambda _v: self.refresh_orders())
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        self._update_buttons()

    @property
    def ctrl(self):
        return self.win.ctrl

    # ---- 갱신
    def rebuild_cards(self) -> None:
        ids = [st.robot_id for st in self.ctrl.fleet.robots()]
        if ids == list(self.cards):
            return
        for card in self.cards.values():
            card.setParent(None)
            card.deleteLater()
        self.cards = {}
        for i, rid in enumerate(ids):
            card = RobotCard(rid)
            self.cards_lay.insertWidget(i, card)
            self.cards[rid] = card
        self._fit_cards()

    def _fit_cards(self) -> None:
        """카드 칸 높이: 탭이 넉넉하면 2대까지, 좁으면 1대 (나머지는 스크롤)."""
        shown = max(1, min(2 if self.height() >= 560 else 1, len(self.cards)))
        h = shown * RobotCard.HEIGHT + (shown - 1) * 6
        if self.cards_area.height() != h:
            self.cards_area.setFixedHeight(h)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._fit_cards()

    def refresh_robots(self) -> None:
        self.rebuild_cards()
        fleet = self.ctrl.fleet
        model = self.win.rack.model
        for st in fleet.robots():
            card = self.cards.get(st.robot_id)
            if card is not None:
                card.update_status(st, fleet.robot_order(st.robot_id), model)

    def refresh_orders(self) -> None:
        orders = self.ctrl.fleet.orders()
        show_park = self.show_park.isChecked()
        active = sum(1 for o in orders if o.state in (OrderState.QUEUED, OrderState.ASSIGNED, OrderState.RUNNING)
                     and o.kind != 'PARK')
        rows = []
        for o in reversed(orders):
            if o.kind == 'PARK' and not show_park:
                continue
            n = len(o.steps)
            done = n if o.state == OrderState.DONE else min(o.current, n)
            state = o.state.value
            fg = {2: theme.ORDER_STATE_COLOR.get(state, theme.TEXT)}
            muted = state in ('DONE', 'CANCELLED')
            if muted:
                fg.update({0: theme.TEXT_2, 5: theme.TEXT_2})
            content = order_content(o)
            rows.append(Row(o.id, [o.id, KIND_LABEL.get(o.kind, o.kind), ORDER_LABEL.get(state, state), o.robot or '-',
                                   f'{done}/{n}', content, fmt_sim(o.created),
                                   fmt_sim(o.finished) if o.finished is not None else ''],
                            fg=fg, bold={2} if state in ('RUNNING', 'FAILED') else set(), data=o.id,
                            tip=f'{o.id} {KIND_LABEL.get(o.kind, o.kind)} · {ORDER_LABEL.get(state, state)}\n{content}'))
            if len(rows) >= self.MAX_ORDERS:
                break
        self.table.model_.set_rows(rows)
        self.order_count.setText(f'진행 중 {active}건 · 전체 {len(orders)}건')
        self._update_buttons()

    def refresh(self) -> None:
        self.minimap.forget_boxes()
        self.refresh_robots()
        self.refresh_orders()
        self.minimap.update()

    def _update_buttons(self, *_a) -> None:
        oid = self.table.selected_key()
        ok = False
        if oid:
            try:
                ok = self.ctrl.fleet.order(oid).state in (OrderState.QUEUED, OrderState.ASSIGNED, OrderState.RUNNING)
            except KeyError:
                ok = False
        self.cancel_btn.setEnabled(ok)

    # ---- 동작
    def _pause(self) -> None:
        run_action(self, self.ctrl.pause_all)
        self.win.toast('모든 AMR 일시정지 — [재개]로 풉니다')

    def _resume(self) -> None:
        run_action(self, self.ctrl.resume_all)
        self.win.toast('AMR 재개')

    def _cancel(self) -> None:
        oid = self.table.selected_key()
        if not oid:
            return
        if not ask(self, f'작업 {oid} 를 취소할까요?\n\n아직 안 집은 박스는 컨베이어에 그대로 남고, 이미 실린 박스는 '
                         '확인 필요로 표시됩니다.', '작업 취소'):
            return
        run_action(self, self.ctrl.cancel_order, oid)
        self.win.request_flush()

    def _estop(self) -> None:
        if not ask(self, '모든 AMR 을 비상정지할까요?\n\n진행 중 작업은 실패로 끝나고, 실린 박스는 확인 필요로 표시됩니다. '
                         '[재개]로 풉니다.', '비상정지'):
            return
        run_action(self, self.ctrl.estop)
        self.win.request_flush()
