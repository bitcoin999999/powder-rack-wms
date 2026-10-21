"""상단 바: 제목·모드, 적재/컨베이어/AMR/확인 필요 지표, 배속, 오버브릿지 출고, 자동 배차, 시뮬레이션 시계."""
from __future__ import annotations

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton,
                             QVBoxLayout, QWidget)

from amr.models import MODE_LABEL

from .resources import app_icon
from .util import fmt_sim
from .widgets import label, mouse_only

SPEEDS = (1, 5, 20)
BUSY_MODES = ('MOVING', 'LOADING', 'UNLOADING', 'WAITING', 'CHARGING')


class Kpi(QWidget):
    def __init__(self, name: str, parent: QWidget | None = None, min_w: int = 0):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.name = label(name, 'KpiName')
        self.value = label('-', 'KpiValue')
        lay.addWidget(self.name)
        lay.addWidget(self.value)
        if min_w:
            self.setMinimumWidth(min_w)

    def set(self, text: str, warn: bool = False, tip: str = '') -> None:
        if self.value.text() != text:
            self.value.setText(text)
        name = 'KpiWarn' if warn else 'KpiValue'
        if self.value.objectName() != name:
            self.value.setObjectName(name)
            self.value.style().unpolish(self.value)
            self.value.style().polish(self.value)
        if tip:
            self.setToolTip(tip)


def _sep() -> QFrame:
    f = QFrame()
    f.setObjectName('HeaderSep')
    f.setFixedSize(1, 30)
    return f


class HeaderBar(QFrame):
    speed_changed = pyqtSignal(float)
    overbridge_toggled = pyqtSignal(bool)
    dispatch_toggled = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName('Header')
        self.setFixedHeight(58)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 6, 14, 6)
        lay.setSpacing(12)

        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(QSize(26, 26)))
        lay.addWidget(icon)
        self.title = label('완분 적재대 관리', 'AppTitle')
        lay.addWidget(self.title)
        self.mode = label('시뮬레이션 AMR', 'ModeBadge')
        lay.addWidget(self.mode, 0, Qt.AlignVCenter)
        lay.addStretch(1)

        self.k_stock = Kpi('적재', min_w=58)
        self.k_conv = Kpi('컨베이어', min_w=56)
        self.k_amr = Kpi('AMR', min_w=86)
        self.k_amr.value.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.k_check = Kpi('확인 필요', min_w=56)
        for k in (self.k_stock, self.k_conv, self.k_amr, self.k_check):
            lay.addWidget(k)
        lay.addWidget(_sep())

        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.speed_group = QButtonGroup(self)
        self.speed_group.setExclusive(True)
        self.speed_buttons: dict[float, QToolButton] = {}
        for i, sp in enumerate(SPEEDS):
            b = QToolButton()
            b.setText(f'{sp}×')
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f'시뮬레이션 배속 {sp}배')
            if i == 0:
                b.setObjectName('SegFirst')
            elif i == len(SPEEDS) - 1:
                b.setObjectName('SegLast')
            b.setMinimumWidth(38)
            self.speed_group.addButton(b, sp)
            self.speed_buttons[float(sp)] = b
            seg.addWidget(b)
        sp_box = QVBoxLayout()
        sp_box.setSpacing(1)
        sp_box.addWidget(label('배속', 'KpiName'))
        sp_box.addLayout(seg)
        lay.addLayout(sp_box)

        self.ob_btn = QPushButton('오버브릿지 출고 시작')
        self.ob_btn.setObjectName('ObRunning')
        self.ob_btn.setCheckable(True)
        self.ob_btn.setCursor(Qt.PointingHandCursor)
        self.ob_btn.setToolTip('오버브릿지 자동 출고 시뮬레이션 (첫 박스 90초, 이후 10초 간격)')
        self.dispatch_btn = QPushButton('자동 배차 켜짐')
        self.dispatch_btn.setCheckable(True)
        self.dispatch_btn.setCursor(Qt.PointingHandCursor)
        self.dispatch_btn.setToolTip('컨베이어 박스를 AMR 에 자동으로 맡깁니다')
        fm = self.ob_btn.fontMetrics()
        self.ob_btn.setMinimumWidth(fm.horizontalAdvance('오버브릿지 출고 시작') + 34)
        self.dispatch_btn.setMinimumWidth(fm.horizontalAdvance('자동 배차 켜짐') + 34)
        lay.addWidget(self.ob_btn)
        lay.addWidget(self.dispatch_btn)
        lay.addWidget(_sep())
        self.k_clock = Kpi('시뮬레이션 시각', min_w=86)
        lay.addWidget(self.k_clock)

        # 마우스 전용: 포커스가 남으면 바코드 리더의 공백이 이 버튼을 눌러 버린다
        mouse_only(self.ob_btn, self.dispatch_btn, *self.speed_buttons.values())

        self.speed_group.buttonClicked.connect(lambda b: self.speed_changed.emit(float(self.speed_group.id(b))))
        self.ob_btn.clicked.connect(lambda checked: self.overbridge_toggled.emit(bool(checked)))
        self.dispatch_btn.clicked.connect(lambda checked: self.dispatch_toggled.emit(bool(checked)))

    # ---- 표시
    def set_speed(self, speed: float) -> None:
        b = self.speed_buttons.get(float(speed))
        self.speed_group.setExclusive(b is not None)
        for v, btn in self.speed_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(btn is b)
            btn.blockSignals(False)
        self.speed_group.setExclusive(True)

    def set_overbridge(self, running: bool) -> None:
        if self.ob_btn.isChecked() != running:
            self.ob_btn.setChecked(running)
        text = '오버브릿지 출고 정지' if running else '오버브릿지 출고 시작'
        if self.ob_btn.text() != text:
            self.ob_btn.setText(text)

    def set_dispatch(self, on: bool) -> None:
        if self.dispatch_btn.isChecked() != on:
            self.dispatch_btn.setChecked(on)
        text = '자동 배차 켜짐' if on else '자동 배차 꺼짐'
        if self.dispatch_btn.text() != text:
            self.dispatch_btn.setText(text)

    def update_from(self, ctrl) -> None:
        snap = ctrl.inventory.snapshot()
        self.k_stock.set(f'{snap.occupied}/{snap.capacity}',
                         tip=f'적재 {snap.occupied} · 예약 {snap.reserved} · 사용중지 {snap.blocked} · '
                             f'빈 칸 {snap.capacity - snap.occupied - snap.reserved - snap.blocked}')
        cap = int(ctrl.config.conveyor_capacity)
        self.k_conv.set(f'{snap.conveyor}/{cap}', warn=snap.conveyor >= cap,
                        tip='컨베이어 박스 수 / 적체 한도' + (' — 가득 (오버브릿지 라인 정지)' if snap.conveyor >= cap else ''))
        robots = ctrl.fleet.robots()
        bad_n = sum(1 for st in robots if st.mode.value in ('ERROR', 'OFFLINE') or not st.connected)
        busy_n = sum(1 for st in robots if st.connected and st.mode.value in BUSY_MODES)
        # 비상정지·일시정지는 한눈에 보이게 (작업 0 으로만 보이면 '그냥 쉬는 중' 으로 오해한다)
        estop_n = sum(1 for st in robots if st.mode.value == 'ERROR' and '비상정지' in (st.message or ''))
        paused_n = sum(1 for st in robots if st.connected and st.mode.value == 'PAUSED')
        if not robots:
            amr = '없음'
        elif estop_n:
            amr = f'{len(robots)}대 · 비상정지'
        elif paused_n:
            amr = f'{len(robots)}대 · 일시정지'
        elif len(robots) == 1:
            st = robots[0]
            amr = '1대 · ' + (MODE_LABEL.get(st.mode.value, st.mode.value) if st.connected else '연결 끊김')
        else:
            amr = f'{len(robots)}대 · 작업 {busy_n}' + (f' · 이상 {bad_n}' if bad_n else '')
        bad = bad_n > 0 or paused_n > 0
        detail = '\n'.join(f'{st.robot_id}: {MODE_LABEL.get(st.mode.value, st.mode.value)} · 배터리 {st.battery:.0f}% · '
                           f'{st.node or "-"}' for st in robots)
        self.k_amr.set(amr, warn=bad, tip=detail)
        self.k_check.set(str(snap.needs_check), warn=snap.needs_check > 0,
                         tip='AMR 작업이 중간에 멈춰 실제 위치를 확인해야 하는 박스 수')
        self.k_check.setVisible(snap.needs_check > 0)
        self.k_clock.set(fmt_sim(ctrl.sim_now), tip=f'시뮬레이션 시각 (배속 {ctrl.sim_speed:g}×)')
        if self.speed_group.checkedButton() is not self.speed_buttons.get(float(ctrl.sim_speed)):
            self.set_speed(ctrl.sim_speed)
        self.set_overbridge(ctrl.overbridge.running)
        self.set_dispatch(ctrl.auto_dispatch)
        self.mode.setText(ctrl.mode_label())
