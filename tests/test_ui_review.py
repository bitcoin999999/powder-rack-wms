"""화면 리뷰에서 고친 결함의 회귀 시험 (offscreen, 위젯 grab 만).

- 바코드 리더(키보드처럼 입력) 글자가 버튼·표·선택 상자에 포커스가 있을 때 스캔 칸으로 가는지
  (예전: 공백이 [수동 출고]를 눌러 사유 칸에 나머지 글자가 들어가고 Enter 로 출고가 실행됐다)
- 상단 바 버튼은 포커스를 가져가지 않는다, 작업 뒤 스캔 칸으로 포커스 복귀
- 칸 선택 해제(Esc·[요약 보기])로 적재대 요약 복귀
- 일시정지·비상정지가 상단 바 AMR 지표에 보인다
- 1280×800 에서 기준정보 설정 값·버튼이 잘리지 않는다
- 영문 칸 한글 입력기 끔, 대화상자 검증이 core 규칙과 같다
- 예외 훅이 루프 밖·offscreen 에서 알림 상자를 띄우지 않는다 (띄우면 접근 위반으로 죽는다)
- Qt 기본 문구 한국어 번역, 앱 아이콘, 글자 대비, 바뀐 것 없을 때 표를 다시 채우지 않음
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if sys.platform == 'win32':
    os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'))

import pytest  # noqa: E402
from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from controller import Controller  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.models import BoxState, SlotState  # noqa: E402


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def boxes(monkeypatch):
    calls: list[tuple[str, str]] = []

    def make(kind, ret):
        def fake(parent, title, text, *a, **kw):
            calls.append((kind, str(text)))
            return ret
        return staticmethod(fake)

    monkeypatch.setattr(QMessageBox, 'warning', make('warning', QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, 'critical', make('critical', QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, 'information', make('information', QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, 'question', make('question', QMessageBox.No))
    return calls


@pytest.fixture
def make_win(tmp_path, qapp, boxes):
    from ui.main_window import MainWindow
    made = []

    def factory(robots: int = 1, sub: str = 'w', size=(1440, 900)):
        cfg = AppConfig()
        cfg.sim.amr_count = robots
        ctrl = Controller(tmp_path / sub, cfg)
        win = MainWindow(ctrl)
        win.tick_timer.stop()            # 시뮬레이션은 시험이 직접 돌린다
        win.resize(*size)
        win.show()
        win.activateWindow()
        qapp.processEvents()
        made.append((win, ctrl))
        return win, ctrl

    yield factory
    for win, ctrl in made:
        win.force_close()
        ctrl.shutdown()
    qapp.processEvents()


def scanner_types(qapp, win, text: str) -> None:
    """바코드 리더처럼 지금 포커스 위젯에 한 글자씩 보낸다 (포커스가 옮겨지면 다음 글자는 새 위젯으로)."""
    for ch in text:
        w = QApplication.focusWidget() or win
        if ch == '\n':
            QTest.keyClick(w, Qt.Key_Return)
        elif ch == ' ':
            QTest.keyClick(w, Qt.Key_Space)
        else:
            QTest.keyClick(w, ch)
        qapp.processEvents()


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------- 바코드 리더 입력
def test_scan_with_focus_on_retrieve_button_registers_arrival_not_retrieval(make_win, qapp, monkeypatch, caplog):
    from ui import dialogs
    opened = []
    monkeypatch.setattr(dialogs.ReasonDialog, 'exec_', lambda self: (opened.append(self.windowTitle()),
                                                                      QDialog.Rejected)[1])
    caplog.set_level(logging.WARNING)
    win, ctrl = make_win(sub='retr')
    ctrl.set_auto_dispatch(False)
    stored = ctrl.put_directly('N-01-1', 'PC9030', None, 15.0, reason='실사')
    win._flush()
    win.select_slot('N-01-1')
    win.slot_detail.b_out.setFocus()
    qapp.processEvents()
    assert QApplication.focusWidget() is win.slot_detail.b_out
    scanner_types(qapp, win, 'PC9030 L-T2\n')
    assert opened == []                                            # [수동 출고] 대화상자가 열리지 않음
    assert ctrl.inventory.box(stored.id).state == BoxState.STORED  # 출고되지 않음
    conv = ctrl.inventory.conveyor()
    assert [(b.grade, b.lot) for b in conv] == [('PC9030', 'L-T2')]
    assert win.tabs.currentWidget() is win.inbound
    assert QApplication.focusWidget() is win.inbound.scan
    assert win.inbound.scan.text() == ''
    assert errors(caplog) == []


@pytest.mark.parametrize('where', ['table', 'combo', 'amr_button', 'checkbox'])
def test_scan_keys_from_non_text_widgets_go_to_scan_field(make_win, qapp, where):
    win, ctrl = make_win(sub=f'redir-{where}')
    ctrl.set_auto_dispatch(False)
    if where == 'table':
        win.tabs.setCurrentWidget(win.history)
        target = win.history.table
    elif where == 'combo':
        target = win.rack.grade_combo                  # 글자 찾기로 재종 검색이 바뀌던 곳
    elif where == 'amr_button':
        win.tabs.setCurrentWidget(win.amr_tab)
        target = win.amr_tab.pause_btn                 # 공백이 [일시정지 전체] 를 누르던 곳
    else:
        win.tabs.setCurrentWidget(win.amr_tab)
        target = win.amr_tab.show_park
    target.setFocus()
    qapp.processEvents()
    assert QApplication.focusWidget() is target
    before_search = win.rack.model.search_grade
    scanner_types(qapp, win, 'NC3010 L-R1\n')
    assert [(b.grade, b.lot) for b in ctrl.inventory.conveyor()] == [('NC3010', 'L-R1')]
    assert win.rack.model.search_grade == before_search
    assert not win.amr_tab.show_park.isChecked()
    assert QApplication.focusWidget() is win.inbound.scan


def test_header_buttons_do_not_take_focus_and_focus_returns_to_scan(make_win, qapp):
    win, ctrl = make_win(sub='hdr')
    h = win.header
    for b in (h.ob_btn, h.dispatch_btn, *h.speed_buttons.values(), win.notice_btn):
        assert b.focusPolicy() == Qt.NoFocus
    for chip in win.rack.legend._buttons.values():
        assert chip.focusPolicy() == Qt.NoFocus
    win.inbound.lot.setFocus()
    qapp.processEvents()
    h.dispatch_btn.click()
    qapp.processEvents()
    assert QApplication.focusWidget() is win.inbound.scan
    # 칸 상세 작업을 취소해도 입고 탭이면 스캔 칸으로
    from ui import dialogs
    ctrl.put_directly('S-01-1', 'PC9030', None, 15.0, reason='실사')
    win._flush()
    win.select_slot('S-01-1')
    win.slot_detail.b_block.setFocus()
    orig = dialogs.ReasonDialog.exec_
    try:
        dialogs.ReasonDialog.exec_ = lambda self: QDialog.Rejected
        win.slot_detail._retrieve()
    finally:
        dialogs.ReasonDialog.exec_ = orig
    assert QApplication.focusWidget() is win.inbound.scan


# ---------------------------------------------------------------- 요약으로 돌아가기
def test_escape_and_summary_button_return_to_rack_summary(make_win, qapp):
    win, ctrl = make_win(sub='esc')
    ctrl.put_directly('N-02-1', 'NC3010', None, 15.0, reason='실사')
    win._flush()
    win.select_slot('N-02-1')
    assert win.slot_detail.sid == 'N-02-1' and win.slot_detail.close_btn.isVisible()
    QTest.keyClick(win, Qt.Key_Escape)
    assert win.slot_detail.sid is None and win.rack.model.selected is None
    assert win.slot_detail.placeholder.isVisible() and not win.slot_detail.close_btn.isVisible()
    win.search_grade('NC3010')
    QTest.keyClick(win, Qt.Key_Escape)
    assert not win.rack.model.searching
    win.select_slot('N-02-1')
    win.slot_detail.close_btn.click()
    assert win.slot_detail.sid is None


# ---------------------------------------------------------------- 일시정지·비상정지 표시
def test_header_shows_pause_and_estop(make_win, qapp, boxes, monkeypatch):
    win, ctrl = make_win(robots=2, sub='halt')
    win._flush()
    assert '일시정지' not in win.header.k_amr.value.text()
    ctrl.pause_all()
    ctrl.set_sim_speed(1)
    for _ in range(3):
        ctrl.tick(0.1)
    win._flush()
    assert win.header.k_amr.value.text().endswith('일시정지')
    assert win.header.k_amr.value.objectName() == 'KpiWarn'
    ctrl.resume_all()
    for _ in range(3):
        ctrl.tick(0.1)
    ctrl.estop()
    for _ in range(3):
        ctrl.tick(0.1)
    win._flush()
    assert win.header.k_amr.value.text().endswith('비상정지')
    assert win.header.k_amr.value.objectName() == 'KpiWarn'


# ---------------------------------------------------------------- 1280×800 배치
def _inside(widget, outer) -> bool:
    top_left = widget.mapTo(outer, widget.rect().topLeft())
    return top_left.x() >= 0 and top_left.x() + widget.width() <= outer.width() + 1


def test_master_settings_not_clipped_at_1280(make_win, qapp):
    win, ctrl = make_win(sub='m1280', size=(1280, 800))
    win.tabs.setCurrentWidget(win.master)
    win._flush()
    for _ in range(3):
        qapp.processEvents()
    m = win.master
    for w in (m.cols, m.tiers, m.amr_count, m.trays, m.charge_below, m.charge_target, m.policy,
              m.settings_apply, m.rack_apply):
        assert w.width() >= w.minimumSizeHint().width(), w
        assert w.width() >= w.minimumWidth(), w
        assert _inside(w, m), w
    assert m.settings_apply.width() >= m.settings_apply.fontMetrics().horizontalAdvance('설정 적용') + 16
    rows_visible = m.table.viewport().height() / m.table.verticalHeader().defaultSectionSize()
    assert rows_visible >= 3
    assert not win.grab().isNull()


def test_amr_map_gets_most_of_the_tab_and_small_map_paints(make_win, qapp):
    win, ctrl = make_win(robots=2, sub='map', size=(1280, 800))
    win.tabs.setCurrentWidget(win.amr_tab)
    win._flush()
    qapp.processEvents()
    tab = win.amr_tab
    assert tab.minimap.height() >= tab.table.height()
    rows = (tab.table.viewport().height()) / tab.table.verticalHeader().defaultSectionSize()
    assert rows >= 1.5
    tab.minimap.resize(220, 120)                 # 아주 작은 지도도 예외 없이
    assert not tab.minimap.grab().isNull()


# ---------------------------------------------------------------- 입력칸·대화상자
def test_latin_fields_have_ime_disabled(make_win, qapp):
    from ui import dialogs
    win, ctrl = make_win(sub='ime')
    for w in (win.inbound.scan, win.inbound.lot, win.rack.search, win.history.text,
              win.outbound.combo.lineEdit()):
        assert not w.testAttribute(Qt.WA_InputMethodEnabled), w
    d = dialogs.BoxEntryDialog(win, 't', 'm', ctrl.inventory.grades())
    assert not d.lot_edit.testAttribute(Qt.WA_InputMethodEnabled)
    assert not d.box_edit.testAttribute(Qt.WA_InputMethodEnabled)
    assert d.reason_edit.testAttribute(Qt.WA_InputMethodEnabled)        # 사유는 한글
    g = dialogs.GradeDialog(win, None, set())
    assert not g.code.testAttribute(Qt.WA_InputMethodEnabled)
    d.deleteLater()
    g.deleteLater()


def test_dialog_validation_matches_core_rules(make_win, qapp):
    from ui import dialogs
    win, ctrl = make_win(sub='val')
    g = dialogs.GradeDialog(win, None, {'PC9030'})
    for bad in ('PC 90', 'PC9030,', '한글', '-AB', 'A/B'):
        g.code.setText(bad)
        assert g.validate(), bad
    g.code.setText('pc9030')
    assert '이미' in g.validate()
    g.code.setText('NEW-01')
    assert g.validate() is None
    d = dialogs.BoxEntryDialog(win, 't', 'm', ctrl.inventory.grades())
    for bad in ('A B', 'A,B', 'A;B', 'A|B'):
        d.box_edit.setText(bad)
        assert d.validate(), bad
    d.box_edit.setText('bx-77')
    assert d.validate() is None
    # 대화상자를 통과한 값은 core 도 받아들인다 (대화상자가 닫힌 뒤 오류로 입력이 사라지지 않게)
    ctrl.put_directly('S-09-1', d.values()['grade'], '', 15.0, d.values()['box_id'], '실사')
    assert ctrl.inventory.box('BX-77').slot_id == 'S-09-1'
    g.deleteLater()
    d.deleteLater()


# ---------------------------------------------------------------- 예외 훅·번역·아이콘
def test_excepthook_does_not_show_box_outside_event_loop(qapp, monkeypatch, caplog):
    from ui import util
    shown = []
    monkeypatch.setattr(QMessageBox, 'show', lambda self: shown.append(self.text()))
    monkeypatch.setattr(util, '_hook_shown', False)
    monkeypatch.setattr(sys, '__excepthook__', lambda *a: None)
    got = []
    util.install_excepthook(lambda text: got.append(text))
    try:
        try:
            raise ValueError('시험 예외')
        except ValueError:
            sys.excepthook(*sys.exc_info())
    finally:
        util.restore_excepthook()
    assert got and '시험 예외' in got[0]
    assert shown == []                      # offscreen·루프 밖: 상자 없음 (띄우면 접근 위반)
    assert not util._can_show_box()


def test_korean_qt_strings_and_app_icon(make_win, qapp):
    from ui.util import install_korean_qt
    assert install_korean_qt(qapp)
    assert QCoreApplication.translate('QPlatformTheme', '&Yes') != '&Yes'
    assert QCoreApplication.translate('QPlatformTheme', 'Cancel') != 'Cancel'
    win, ctrl = make_win(sub='icon')
    assert not win.windowIcon().isNull()
    assert not QApplication.windowIcon().isNull()


# ---------------------------------------------------------------- 글자 대비
def _contrast(a: str, b: str) -> float:
    def lum(h):
        h = h.lstrip('#')
        r, g, bl = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        f = (lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl)
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_cell_text_contrast_on_every_sample_grade(qapp):
    from core.grades import SAMPLE_GRADES
    from ui import theme
    for g in SAMPLE_GRADES:          # 노란 M 재종·짙은 남색·밤색 바탕 모두
        bg = theme.tint(g.color, 0.74).name()
        assert _contrast(theme.TEXT, bg) >= 7.0, g.code
        assert _contrast(theme.CELL_SUB, bg) >= 4.5, g.code
        assert _contrast(theme.RESIDUAL, bg) >= 4.5, g.code
    assert _contrast(theme.TEXT_2, theme.RESERVED_CELL) >= 4.5   # 예약 칸 재종 글자
    assert _contrast(theme.EMPTY_ID, theme.EMPTY_CELL) >= 2.5  # 빈 칸 번호 (흐리지만 읽힘)


# ---------------------------------------------------------------- 표: 바뀐 것 없으면 다시 채우지 않음
def test_flush_without_changes_does_not_reset_tables(make_win, qapp):
    win, ctrl = make_win(robots=2, sub='flick')
    ctrl.start_overbridge(30)
    ctrl.set_sim_speed(20)
    for i in range(400):
        ctrl.tick(0.1)
        if i % 10 == 0:
            win._flush()
    for tab in (win.inbound, win.amr_tab, win.history, win.outbound, win.master, win.inbound):
        win.tabs.setCurrentWidget(tab)      # 모든 탭을 한 번씩 최신으로
        win._flush()
    counts = {'reset': 0, 'changed': 0}
    tables = [win.inbound.table, win.outbound.table, win.outbound.overview, win.amr_tab.table,
              win.history.table, win.master.table]
    for t in tables:
        t.model_.modelReset.connect(lambda: counts.__setitem__('reset', counts['reset'] + 1))
        t.model_.dataChanged.connect(lambda *a: counts.__setitem__('changed', counts['changed'] + 1))
    for tab in (win.inbound, win.amr_tab, win.history, win.outbound, win.master):
        win.tabs.setCurrentWidget(tab)
        win._flush()
        win._flush()
    assert counts == {'reset': 0, 'changed': 0}
