"""기준정보 탭: 재종 표(추가·수정·사용중지), 적재대 구성(열·단 — 비어 있을 때만), 배정 정책, 가상 AMR 설정."""
from __future__ import annotations

import copy

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QGroupBox, QHBoxLayout, QSpinBox, QVBoxLayout,
                             QWidget)

from core.rack import MAX_TIERS, POLICIES

from . import theme
from .dialogs import GradeDialog
from .table import Row, RowTable
from .util import ask, info, run_action
from .widgets import ElidedLabel, button, label

MAX_COLS_UI = 40


class MasterTab(QWidget):
    def __init__(self, win, parent: QWidget | None = None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        # 재종
        head = QHBoxLayout()
        head.addWidget(label('재종 기준정보', 'PanelTitle'))
        self.grade_count = label('', 'Muted')
        head.addWidget(self.grade_count)
        head.addStretch(1)
        self.add_btn = button('추가…', 'primary')
        self.edit_btn = button('수정…')
        self.active_btn = button('사용중지')
        head.addWidget(self.add_btn)
        head.addWidget(self.edit_btn)
        head.addWidget(self.active_btn)
        lay.addLayout(head)
        self.table = RowTable(['코드', 'ISO', '범위', '종류', '설명', '색', '사용'],
                              ['NC3010', 'P', 'P10–P25', '무코팅', None, '■ #1f5fbf', '중지'],
                              stretch=4, align={1: Qt.AlignCenter, 6: Qt.AlignCenter})
        lay.addWidget(self.table, 1)

        # 설정 두 묶음 — 위아래로 쌓고 묶음마다 가로 두 줄 (1280×800 에서도 값·버튼이 잘리지 않고 재종 표가 남는다)
        rack_box = QGroupBox('적재대 구성')
        rv = QVBoxLayout(rack_box)
        rv.setSpacing(4)
        self.cols = QSpinBox()
        self.cols.setRange(1, MAX_COLS_UI)
        self.cols.setSuffix(' 열')
        self.tiers = QSpinBox()
        self.tiers.setRange(1, MAX_TIERS)
        self.tiers.setSuffix(' 단')
        self.capacity = label('', 'FieldValue')
        self.rack_apply = button('적용', 'primary', '적재대가 비어 있을 때만 바꿀 수 있습니다')
        self.rack_state = ElidedLabel('', 'Faint')
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        r1.addWidget(label('면마다', 'FieldName'))
        r1.addWidget(self.cols)
        r1.addWidget(self.tiers)
        r1.addSpacing(6)
        r1.addWidget(self.capacity)
        r1.addStretch(1)
        r1.addWidget(self.rack_apply)
        rv.addLayout(r1)
        rv.addWidget(self.rack_state)
        lay.addWidget(rack_box)

        run_box = QGroupBox('배정 정책 · 가상 AMR')
        sv = QVBoxLayout(run_box)
        sv.setSpacing(6)
        self.policy = QComboBox()
        for key, text in POLICIES.items():
            self.policy.addItem(text, key)
        self.policy.setToolTip('같은 재종 열 우선: 같은 재종을 같은 열에 모아 찾기 쉽게 (기본)\n'
                               '아래 단 우선: 낮은 칸부터 · 면 균형: 북·남 박스 수를 맞춤')
        self.amr_count = QSpinBox()
        self.amr_count.setRange(1, 20)
        self.amr_count.setSuffix(' 대')
        self.amr_count.setToolTip('가상 AMR 대수')
        self.trays = QSpinBox()
        self.trays.setRange(1, 9)
        self.trays.setSuffix(' 단')
        self.trays.setToolTip('AMR 사다리(트레이) 단수')
        self.charge_below = QDoubleSpinBox()
        self.charge_below.setRange(0, 99)
        self.charge_below.setDecimals(0)
        self.charge_below.setSuffix(' %')
        self.charge_below.setToolTip('배터리가 이 값 이하면 충전하러 갑니다')
        self.charge_target = QDoubleSpinBox()
        self.charge_target.setRange(1, 100)
        self.charge_target.setDecimals(0)
        self.charge_target.setSuffix(' %')
        self.charge_target.setToolTip('이 값까지 충전합니다')
        self.settings_apply = button('설정 적용', 'primary', 'AMR 대수·사다리 단수 변경은 진행 중 작업이 없을 때만 됩니다')
        self.run_note = ElidedLabel('', 'Faint')
        s1 = QHBoxLayout()
        s1.setSpacing(6)
        s1.addWidget(label('배정', 'FieldName'))
        s1.addWidget(self.policy, 1)
        s1.addSpacing(6)
        s1.addWidget(label('AMR', 'FieldName'))
        s1.addWidget(self.amr_count)
        s1.addWidget(label('사다리', 'FieldName'))
        s1.addWidget(self.trays)
        s2 = QHBoxLayout()
        s2.setSpacing(6)
        s2.addWidget(label('충전', 'FieldName'))
        s2.addWidget(self.charge_below)
        s2.addWidget(label('→ 목표', 'FieldName'))
        s2.addWidget(self.charge_target)
        s2.addSpacing(6)
        s2.addWidget(self.run_note, 1)
        s2.addWidget(self.settings_apply)
        sv.addLayout(s1)
        sv.addLayout(s2)
        lay.addWidget(run_box)
        # 값·단위가 잘리지 않는 최소 폭 ('100 %' + 위아래 화살표 자리)
        fm = self.fontMetrics()
        for sp in (self.cols, self.tiers, self.amr_count, self.trays, self.charge_below, self.charge_target):
            sp.setMinimumWidth(fm.horizontalAdvance('100 %') + 44)
        self.policy.setMinimumWidth(fm.horizontalAdvance('같은 재종 열 우선') + 44)
        self.settings_apply.setMinimumWidth(self.settings_apply.sizeHint().width())
        self.rack_apply.setMinimumWidth(self.rack_apply.sizeHint().width())
        self.paths = ElidedLabel('', 'Faint')
        self.reset_btn = button('처음부터 다시…', None, '시연용: 적재대·컨베이어·이력을 비우고 처음 상태로 (지금 기록은 백업)')
        self.reset_btn.setStyleSheet(f'color: {theme.ERROR};')
        foot = QHBoxLayout()
        foot.addWidget(self.paths, 1)
        foot.addWidget(self.reset_btn)
        lay.addLayout(foot)

        self.add_btn.clicked.connect(self._add)
        self.edit_btn.clicked.connect(self._edit)
        self.active_btn.clicked.connect(self._toggle_active)
        self.table.doubleClicked.connect(lambda _i: self._edit())
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        self.cols.valueChanged.connect(self._show_capacity)
        self.tiers.valueChanged.connect(self._show_capacity)
        self.rack_apply.clicked.connect(self._apply_rack)
        self.settings_apply.clicked.connect(self._apply_settings)
        self.reset_btn.clicked.connect(self._reset_demo)
        self._update_buttons()

    def _reset_demo(self) -> None:
        if not ask(self, '적재대·컨베이어·이력을 모두 비우고 처음 상태로 돌아갑니다.\n'
                         '재종 기준정보와 설정은 그대로 남고, 지금 기록은 data\\백업 폴더에 보관됩니다.\n\n계속할까요?',
                   '처음부터 다시'):
            return
        ok, backup = run_action(self, self.ctrl.reset_demo)
        if ok:
            self.win.refresh_all()
            info(self, f'처음 상태로 돌아왔습니다.\n\n이전 기록 백업: {backup}', '처음부터 다시')

    @property
    def ctrl(self):
        return self.win.ctrl

    # ---- 갱신
    def refresh_grades(self) -> None:
        snap = self.ctrl.inventory.snapshot()
        rows = []
        grades = self.ctrl.inventory.grades()
        for g in grades:
            stock = snap.by_grade.get(g.code, 0)
            fg = {6: theme.ACCENT_DARK if g.active else theme.ERROR}
            if not g.active:
                fg.update({0: theme.TEXT_3, 4: theme.TEXT_3})
            rows.append(Row(g.code, [g.code, g.iso_class, g.iso_range, g.kind, g.description, g.color,
                                     '사용' if g.active else '중지'], fg=fg, swatch={5: g.color}, bold={0}, data=g,
                            tip=f'{g.code} · 재고 {stock}박스'
                                + (' · ISO 미분류 (라벨 바코드로 자동 추가 — [수정]에서 분류를 고르세요)'
                                   if g.iso_class == '-' else '')))
        self.table.model_.set_rows(rows)
        n_active = sum(1 for g in grades if g.active)
        self.grade_count.setText(f'{len(grades)}종 · 사용 {n_active}')
        self._update_buttons()

    def refresh_settings(self) -> None:
        cfg = self.ctrl.config
        lay = self.ctrl.inventory.layout
        for w, v in ((self.cols, lay.columns), (self.tiers, lay.tiers), (self.amr_count, cfg.sim.amr_count),
                     (self.trays, cfg.sim.trays), (self.charge_below, cfg.charge_below),
                     (self.charge_target, cfg.charge_target)):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        i = self.policy.findData(self.ctrl.inventory.policy)
        self.policy.setCurrentIndex(max(0, i))
        real = bool(cfg.robots)
        self.amr_count.setEnabled(not real)
        self.trays.setEnabled(not real or any(r.driver == 'sim' for r in cfg.robots))
        note = ('실제 AMR(config.json robots) 사용 중 — 대수는 그 파일에서' if real
                else 'AMR 대수·사다리는 작업이 없을 때만 바뀝니다')
        self.run_note.setText(note)
        self.paths.setText(f'데이터 폴더: {self.ctrl.data_dir}')
        self.paths.setToolTip(f'재고 DB·설정(config.json)·기록(app.log) 위치\n{self.ctrl.data_dir}')
        self._show_capacity()
        self.refresh_rack_state()

    def refresh_rack_state(self) -> None:
        snap = self.ctrl.inventory.snapshot()
        busy = snap.occupied + snap.reserved + snap.blocked
        if busy:
            parts = [f'적재 {snap.occupied}'] + ([f'예약 {snap.reserved}'] if snap.reserved else []) + (
                [f'사용중지 {snap.blocked}'] if snap.blocked else [])
            self.rack_state.setText('비어 있을 때만 변경 (' + ', '.join(parts) + ')')
        else:
            self.rack_state.setText('적재대가 비어 있어 바꿀 수 있습니다')
        self.rack_apply.setEnabled(busy == 0)

    def refresh(self) -> None:
        self.refresh_grades()
        self.refresh_settings()

    def _show_capacity(self) -> None:
        c, t = self.cols.value(), self.tiers.value()
        self.capacity.setText(f'양면 {2 * c * t}칸 ({c}열×{t}단×2)')

    def _update_buttons(self, *_a) -> None:
        row = self.table.selected_row()
        g = row.data if row is not None else None
        self.edit_btn.setEnabled(g is not None)
        self.active_btn.setEnabled(g is not None)
        self.active_btn.setText('사용' if (g is not None and not g.active) else '사용중지')

    # ---- 재종
    def _existing(self) -> set[str]:
        return {g.code for g in self.ctrl.inventory.grades()}

    def _add(self) -> None:
        d = GradeDialog(self, None, self._existing())
        if d.exec_() == QDialog.Accepted:
            g = d.grade()
            ok, _ = run_action(self, self.ctrl.upsert_grade, g)
            if ok:
                self.win.toast(f'재종 {g.code} 추가')
            self.win.request_flush()

    def _edit(self) -> None:
        row = self.table.selected_row()
        if row is None:
            return
        d = GradeDialog(self, row.data, self._existing())
        if d.exec_() == QDialog.Accepted:
            run_action(self, self.ctrl.upsert_grade, d.grade())
            self.win.request_flush()

    def _toggle_active(self) -> None:
        row = self.table.selected_row()
        if row is None:
            return
        g = row.data
        run_action(self, self.ctrl.set_grade_active, g.code, not g.active)
        self.win.request_flush()

    # ---- 설정
    def _apply_rack(self) -> None:
        cfg = copy.deepcopy(self.ctrl.config)
        cfg.rack.columns, cfg.rack.tiers = self.cols.value(), self.tiers.value()
        ok, _ = run_action(self, self.ctrl.apply_config, cfg)
        if ok:
            self.win.toast(f'적재대 구성: {cfg.rack.columns}열 × {cfg.rack.tiers}단 양면')
        self.refresh_settings()
        self.win.request_flush()

    def _apply_settings(self) -> None:
        cfg = copy.deepcopy(self.ctrl.config)
        cfg.allocation_policy = self.policy.currentData()
        if not cfg.robots:
            cfg.sim.amr_count = self.amr_count.value()
        cfg.sim.trays = self.trays.value()
        cfg.charge_below = float(self.charge_below.value())
        cfg.charge_target = float(self.charge_target.value())
        ok, _ = run_action(self, self.ctrl.apply_config, cfg)
        if ok:
            self.win.toast('설정을 적용했습니다')
        self.refresh_settings()
        self.win.request_flush()
