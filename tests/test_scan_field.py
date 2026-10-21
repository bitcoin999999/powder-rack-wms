"""현장 바코드 시험 대비: 스캔 원문 기록, 로트만 찍힌 바코드, 2차원 바코드의 탭 문자."""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if sys.platform == 'win32':
    os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'))

import pytest  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from controller import Controller, scan_visible  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.inventory import InventoryError, LotOnlyScan, parse_scan  # noqa: E402

GRADES = {'PC9030', 'NC3215'}


def test_lot_only_barcode_is_reported_with_its_lot():
    with pytest.raises(LotOnlyScan) as e:
        parse_scan('0001015661578', GRADES)
    assert e.value.lot == '0001015661578' and '재종' in str(e.value)
    with pytest.raises(InventoryError) as e2:          # 짧은 숫자·문자는 그냥 '등록되지 않은 재종'
        parse_scan('12345', GRADES)
    assert not isinstance(e2.value, LotOnlyScan)
    assert parse_scan('PC9030\t0001015661578', GRADES).lot == '0001015661578'   # 탭 구분도 해석


def test_scan_visible_shows_control_characters():
    assert scan_visible('PC9030\t0001\x1d15\r') == 'PC9030<TAB>0001<GS>15<CR>'
    assert scan_visible('A\x01B') == 'A<01>B'


def test_every_scan_is_logged_with_raw_text_and_result(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        ctrl.scan('PC9030 0001015661578')
        for bad in ('XX999', '0001015661579', '=1+2'):
            with pytest.raises(InventoryError):
                ctrl.scan(bad)
        with ctrl.scan_log_path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
    finally:
        ctrl.shutdown()
    assert rows[0] == ['시각', '보이는 원문', '글자 수', '결과', '내용']
    assert [r[3] for r in rows[1:]] == ['등록', '거절', '로트만', '거절']
    assert rows[1][1] == 'PC9030 0001015661578' and 'PC9030' in rows[1][4]
    assert rows[4][1] == "'=1+2"                        # 엑셀 수식으로 실행되지 않게


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(tmp_path, qapp, monkeypatch):
    from ui.main_window import MainWindow
    shown = []
    monkeypatch.setattr(QMessageBox, 'warning', staticmethod(lambda *a, **k: (shown.append(a[2]), QMessageBox.Ok)[1]))
    ctrl = Controller(tmp_path / 'w', AppConfig())
    w = MainWindow(ctrl)
    w.tick_timer.stop()
    w.resize(1440, 900)
    w.show()
    qapp.processEvents()
    w.shown = shown
    yield w
    w.force_close()
    ctrl.shutdown()
    qapp.processEvents()


def test_tab_from_2d_barcode_stays_in_scan_field(win, qapp):
    tab = win.inbound
    win.tabs.setCurrentWidget(tab)
    tab.focus_scan()
    qapp.processEvents()
    QTest.keyClicks(tab.scan, 'PC9030')
    QTest.keyClick(tab.scan, Qt.Key_Tab)            # 2차원 바코드의 칸 구분
    QTest.keyClicks(tab.scan, '0001015661578')
    assert QApplication.focusWidget() is tab.scan
    QTest.keyClick(tab.scan, Qt.Key_Return)
    qapp.processEvents()
    conv = win.ctrl.inventory.conveyor()
    assert [(b.grade, b.lot) for b in conv] == [('PC9030', '0001015661578')]


def test_lot_only_scan_fills_lot_and_asks_for_grade(win, qapp):
    tab = win.inbound
    win.tabs.setCurrentWidget(tab)
    tab.focus_scan()
    QTest.keyClicks(tab.scan, '0001015661578')
    QTest.keyClick(tab.scan, Qt.Key_Return)
    qapp.processEvents()
    assert tab.lot.text() == '0001015661578' and win.ctrl.inventory.conveyor() == []
    assert win.shown == []                          # 막는 창 없이 안내만
    assert '재종' in tab.result.text()
    tab.grade.setCurrentIndex(tab.grade.findData('NC3215'))
    tab._manual_arrival()
    conv = win.ctrl.inventory.conveyor()
    assert [(b.grade, b.lot) for b in conv] == [('NC3215', '0001015661578')]


def test_unknown_barcode_shows_raw_text(win, qapp):
    tab = win.inbound
    tab.focus_scan()
    QTest.keyClicks(tab.scan, 'ABC-123')
    QTest.keyClick(tab.scan, Qt.Key_Return)
    qapp.processEvents()
    assert win.shown and '읽힌 원문: ABC-123' in win.shown[-1]
    assert 'ABC-123' in tab.result.text()
