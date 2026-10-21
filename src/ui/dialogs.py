"""작은 입력 대화상자: 사유 입력, 칸 고르기, 박스 내용 입력, 재종 편집, 알림 목록."""
from __future__ import annotations

import re
import time

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                             QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget)

from core.models import FACE_LABEL, ISO_CLASSES, Grade

from . import theme
from .rack_view import color_icon
from .widgets import Swatch, button, label, latin_input

KINDS = ['CVD', 'PVD', '무코팅', '서멧', '세라믹', 'CBN']
# core.inventory 와 같은 규칙 — 대화상자 안에서 미리 걸러야 [저장]/[등록] 뒤 오류 상자로 입력이 사라지지 않는다
GRADE_CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9_.\-]{0,23}$')
BOX_ID_RE = re.compile(r'^[^\s,;|]{1,40}$')


class _Base(QDialog):
    def __init__(self, parent: QWidget | None, title: str, message: str = '', width: int = 420):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 16, 18, 14)
        self.lay.setSpacing(10)
        head = label(title, 'PanelTitle')
        self.lay.addWidget(head)
        if message:
            msg = label(message, 'Muted')
            msg.setWordWrap(True)
            msg.setTextFormat(Qt.PlainText)
            self.lay.addWidget(msg)
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.form.setHorizontalSpacing(12)
        self.form.setVerticalSpacing(8)
        self.lay.addLayout(self.form)
        self.err = label('', None)
        self.err.setStyleSheet(f'color: {theme.ERROR};')
        self.err.setWordWrap(True)
        self.err.hide()
        self.lay.addWidget(self.err)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = button('취소')
        self.ok_btn = button('확인', 'primary')
        self.ok_btn.setDefault(True)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.ok_btn)
        self.lay.addLayout(row)
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn.clicked.connect(self._try_accept)

    def fail(self, text: str) -> None:
        self.err.setText(text)
        self.err.show()

    def validate(self) -> str | None:
        """문제가 있으면 한국어 문구, 없으면 None."""
        return None

    def _try_accept(self) -> None:
        problem = self.validate()
        if problem:
            self.fail(problem)
            return
        self.accept()


# ---------------------------------------------------------------- 사유
class ReasonDialog(_Base):
    def __init__(self, parent, title: str, message: str = '', field: str = '사유', default: str = '',
                 required: bool = True, ok_text: str = '확인', danger: bool = False):
        super().__init__(parent, title, message)
        self.required = required
        self.edit = QLineEdit(default)
        self.edit.setMaxLength(100)
        self.edit.setPlaceholderText('필수' if required else '선택')
        self.form.addRow(field, self.edit)
        self.ok_btn.setText(ok_text)
        if danger:
            self.ok_btn.setProperty('kind', 'danger')
        self.edit.selectAll()
        self.edit.setFocus()

    def validate(self) -> str | None:
        if self.required and not self.edit.text().strip():
            return '사유를 입력하세요.'
        return None

    @classmethod
    def get(cls, parent, title: str, message: str = '', **kw) -> str | None:
        d = cls(parent, title, message, **kw)
        return d.edit.text().strip() if d.exec_() == QDialog.Accepted else None


# ---------------------------------------------------------------- 칸 고르기
def slot_text(sid: str) -> str:
    face, col, tier = sid[0], int(sid[2:4]), int(sid[5:])
    return f'{sid}   ({FACE_LABEL.get(face, face)} {col}열 {tier}단)'


class SlotPickDialog(_Base):
    def __init__(self, parent, title: str, message: str, free: list[str], recommended: str | None,
                 policy_label: str = '', reason: tuple[str, str, bool] | None = None, ok_text: str = '확인'):
        super().__init__(parent, title, message)
        self.combo = QComboBox()
        self.combo.setMaxVisibleItems(16)
        for sid in free:
            text = slot_text(sid) + ('   ★ 추천' if sid == recommended else '')
            self.combo.addItem(text, sid)
        if recommended and recommended in free:
            self.combo.setCurrentIndex(free.index(recommended))
        self.form.addRow('넣을 칸', self.combo)
        if recommended:
            hint = label(f'★ 추천 = 배정 정책({policy_label})이 고른 칸', 'Faint')
            self.form.addRow('', hint)
        self.reason_edit: QLineEdit | None = None
        self.reason_required = False
        if reason is not None:
            name, default, required = reason
            self.reason_edit = QLineEdit(default)
            self.reason_edit.setMaxLength(100)
            self.reason_edit.setPlaceholderText('필수' if required else '선택')
            self.reason_required = required
            self.form.addRow(name, self.reason_edit)
        if not free:
            self.fail('빈 칸이 없습니다.')
            self.ok_btn.setEnabled(False)
        self.ok_btn.setText(ok_text)

    def validate(self) -> str | None:
        if self.combo.currentData() is None:
            return '칸을 고르세요.'
        if self.reason_edit is not None and self.reason_required and not self.reason_edit.text().strip():
            return '사유를 입력하세요.'
        return None

    def result_value(self) -> tuple[str, str]:
        return self.combo.currentData(), (self.reason_edit.text().strip() if self.reason_edit is not None else '')


# ---------------------------------------------------------------- 박스 내용
class BoxEntryDialog(_Base):
    """빈 칸 직접 등록 / 적재 칸 내용 수정."""

    def __init__(self, parent, title: str, message: str, grades: list[Grade], grade: str | None = None,
                 lot: str = '', weight: float = 15.0, reason_default: str = '실사 등록', with_box_id: bool = True):
        super().__init__(parent, title, message, width=460)
        self.grade_combo = QComboBox()
        self.grade_combo.setMaxVisibleItems(20)
        for g in grades:
            self.grade_combo.addItem(color_icon(g.color), f'{g.code}   {g.iso_class} · {g.kind}'
                                     + ('' if g.active else ' (사용중지)'), g.code)
        if grade:
            i = self.grade_combo.findData(grade)
            if i >= 0:
                self.grade_combo.setCurrentIndex(i)
        self.lot_edit = QLineEdit(lot)
        self.lot_edit.setMaxLength(60)
        latin_input(self.lot_edit)
        self.lot_edit.setPlaceholderText('비우면 자동 발번')
        self.weight = QDoubleSpinBox()
        self.weight.setRange(0.1, 100.0)
        self.weight.setDecimals(1)
        self.weight.setSingleStep(0.5)
        self.weight.setSuffix(' kg')
        self.weight.setValue(weight)
        self.box_edit: QLineEdit | None = None
        self.form.addRow('재종', self.grade_combo)
        self.form.addRow('로트', self.lot_edit)
        self.form.addRow('중량', self.weight)
        if with_box_id:
            self.box_edit = QLineEdit()
            self.box_edit.setMaxLength(40)
            latin_input(self.box_edit)
            self.box_edit.setPlaceholderText('비우면 자동 (W날짜-순번)')
            self.form.addRow('박스 ID', self.box_edit)
        self.reason_edit = QLineEdit(reason_default)
        self.reason_edit.setMaxLength(100)
        self.reason_edit.setPlaceholderText('필수')
        self.form.addRow('사유', self.reason_edit)
        self.ok_btn.setText('등록')

    def validate(self) -> str | None:
        if self.grade_combo.currentData() is None:
            return '재종을 고르세요.'
        if not self.reason_edit.text().strip():
            return '사유를 입력하세요.'
        if self.box_edit is not None:
            t = self.box_edit.text().strip()
            if t and not BOX_ID_RE.match(t):
                return '박스 ID 에는 빈칸과 , ; | 를 넣을 수 없습니다 (40자 이내).'
        return None

    def values(self) -> dict:
        return {'grade': self.grade_combo.currentData(), 'lot': self.lot_edit.text().strip(),
                'weight': round(self.weight.value(), 1),
                'box_id': self.box_edit.text().strip() if self.box_edit is not None else '',
                'reason': self.reason_edit.text().strip()}


# ---------------------------------------------------------------- 재종
class GradeDialog(_Base):
    def __init__(self, parent, grade: Grade | None, existing: set[str]):
        title = '재종 수정' if grade else '재종 추가'
        super().__init__(parent, title, '코드는 영문·숫자 24자 이내. 색은 적재대 칸 바탕에 쓰입니다.', width=440)
        self.existing = existing
        self.editing = grade is not None
        self.code = QLineEdit(grade.code if grade else '')
        self.code.setMaxLength(24)
        latin_input(self.code)
        self.code.setEnabled(not self.editing)
        self.iso = QComboBox()
        for c in ISO_CLASSES:
            self.iso.addItem(c, c)
        self.range = QLineEdit(grade.iso_range if grade else '')
        self.range.setPlaceholderText('예) M20–M35')
        self.kind = QComboBox()
        self.kind.setEditable(True)
        self.kind.addItems(KINDS)
        self.desc = QLineEdit(grade.description if grade else '')
        self.desc.setPlaceholderText('용도 한 줄')
        self.active = QCheckBox('사용 (오버브릿지·입고 재종 목록에 나옴)')
        self.active.setChecked(grade.active if grade else True)
        self._color = grade.color if grade else '#2f74d6'
        color_row = QHBoxLayout()
        color_row.setSpacing(8)
        self.swatch = Swatch(self._color, 20)
        self.color_text = label(self._color, 'Muted')
        self.color_btn = button('색 고르기…')
        color_row.addWidget(self.swatch)
        color_row.addWidget(self.color_text)
        color_row.addStretch(1)
        color_row.addWidget(self.color_btn)
        if grade:
            self.iso.setCurrentIndex(max(0, self.iso.findData(grade.iso_class)))
            self.kind.setCurrentText(grade.kind)
        self.iso.currentIndexChanged.connect(self._iso_changed)
        self.color_btn.clicked.connect(self._pick_color)
        self.form.addRow('코드', self.code)
        self.form.addRow('ISO 분류', self.iso)
        self.form.addRow('ISO 범위', self.range)
        self.form.addRow('종류', self.kind)
        self.form.addRow('설명', self.desc)
        self.form.addRow('색', color_row)
        self.form.addRow('', self.active)
        self.ok_btn.setText('저장')

    def _iso_changed(self) -> None:
        if not self.editing:
            self._set_color(theme.ISO_COLOR.get(self.iso.currentData(), self._color))

    def _set_color(self, c: str) -> None:
        self._color = c
        self.swatch.set_color(c)
        self.color_text.setText(c)

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self, '재종 색')
        if c.isValid():
            self._set_color(c.name())

    def validate(self) -> str | None:
        code = self.code.text().strip().upper()
        if not code:
            return '코드를 입력하세요.'
        if not GRADE_CODE_RE.match(code):
            return '코드는 영문·숫자로 시작하고 영문·숫자·_ . - 만 쓸 수 있습니다 (빈칸 없이 24자 이내).'
        if not self.editing and code in self.existing:
            return f'이미 있는 재종입니다: {code}'
        return None

    def grade(self) -> Grade:
        return Grade(self.code.text().strip().upper(), self.iso.currentData(), self.range.text().strip(),
                     self.kind.currentText().strip(), self.desc.text().strip(), self._color, self.active.isChecked())


# ---------------------------------------------------------------- 알림 목록
class NoticeDialog(QDialog):
    def __init__(self, parent, notices: list[tuple[float, dict]]):
        super().__init__(parent)
        self.setWindowTitle('알림')
        self.resize(720, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.addWidget(label(f'최근 알림 {len(notices)}건 (새것이 위)', 'PanelTitle'))
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setAlternatingRowColors(False)
        self.list.setSelectionMode(QListWidget.NoSelection)
        self.list.setFocusPolicy(Qt.NoFocus)
        names = {'info': '안내', 'warn': '주의', 'error': '오류'}
        for wall, payload in reversed(notices):
            lvl = payload.get('level', 'info')
            stamp = time.strftime('%H:%M:%S', time.localtime(wall))
            it = QListWidgetItem(f'{stamp}  [{names.get(lvl, lvl)}]  {payload.get("text", "")}')
            it.setForeground(QColor(theme.ERROR if lvl == 'error' else '#8a5d0b' if lvl == 'warn' else theme.TEXT))
            it.setIcon(color_icon(theme.LEVEL_COLOR.get(lvl, theme.ACCENT), 10))
            self.list.addItem(it)
        if not notices:
            self.list.addItem('알림이 없습니다.')
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = button('닫기', 'primary')
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
