"""완분 라벨 바코드 기능 검토 시험: 리더 잡음·닮은 바코드, 거절된 스캔의 부작용, 사용중지 재종, 2초 중복·스캔 기록,
실제 화면 경로(스캔 칸에 치고 Enter)에서 자동 추가 재종이 콤보·범례·기준정보·칸 색·오버브릿지에 바로 나오는지."""
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

from controller import SCAN_DEBOUNCE_TEXT, Controller  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.inventory import ConveyorFull, InventoryError, parse_label, parse_scan  # noqa: E402

LABEL = '0026080160015000001KP320'


# ---------------------------------------------------------------- 해석 (리더 잡음·닮은 바코드)
@pytest.mark.parametrize('noisy', [
    LABEL + '\t20261020',          # 2차원 바코드·리더 설정으로 탭 뒤에 다른 칸이 붙음
    LABEL + ' 15KG',
    LABEL + '\x1d123',             # GS 구분 문자
    LABEL + '\r\n0026080140015000010A30',   # 두 라벨이 한 번에 들어옴
])
def test_trailing_data_is_not_glued_onto_the_grade(noisy):
    """빈칸·탭·GS 뒤의 글자를 재종 코드에 이어 붙이면 'KP32020261020' 같은 엉터리 재종이 재종표에 영구히 추가된다."""
    with pytest.raises(InventoryError, match='라벨'):
        parse_label(noisy, set())


@pytest.mark.parametrize('spaced', ['00 26080160 015000 001 KP320', '0026080160015000001 KP320',
                                    '0026080160 015000 001KP320'])
def test_digit_groups_typed_with_spaces_still_read(spaced):
    r = parse_label(spaced, set())
    assert r is not None and (r.grade, r.lot, r.weight_kg, r.box_id) == ('KP320', '26080160', 15.0, '26080160-001')


def test_controller_does_not_auto_add_a_glued_grade(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        before = {g.code for g in ctrl.inventory.grades()}
        with pytest.raises(InventoryError):
            ctrl.scan(LABEL + '\t20261020')
        assert {g.code for g in ctrl.inventory.grades()} == before
        assert ctrl.inventory.conveyor() == []
    finally:
        ctrl.shutdown()


@pytest.mark.parametrize('code', [
    '002608016001500000KP320',     # 숫자 18자리 (한 자리 빠짐)
    '00260801600150000001KP320',   # 숫자 20자리 (한 자리 더)
    '00260801600150000013KP',      # 재종이 숫자로 시작 → 자리수가 맞지 않는 것과 구분할 수 없음
])
def test_label_lookalike_explains_the_digit_count(code):
    """'등록되지 않은 재종: 0026…' 대신 라벨 자리수가 틀렸다고 알려 준다 (parse_label 은 '라벨 아님' 그대로 None)."""
    assert parse_label(code, {'KP320'}) is None
    with pytest.raises(InventoryError) as e:
        parse_scan(code, {'KP320'})
    msg = str(e.value)
    assert '라벨' in msg and '19자리' in msg and '등록되지 않은 재종' not in msg


def test_non_ascii_digits_are_not_a_label():
    """전각 숫자가 로트에 들어가면 같은 박스가 다른 ID('26０80160-001')로 두 번 등록된다."""
    assert parse_label('0026０80160015000001KP320', set()) is None
    with pytest.raises(InventoryError):
        parse_scan('0026０80160015000001KP320', set())


def test_parse_label_never_raises_on_ordinary_input():
    for text in (None, 123, '', '   ', 'PC9030', 'PC9030 0001015661578', 'BOX:W261020-0007;PC9030;L1;15',
                 'PC9030,L2610-0012,14.2', '0001015661578', '26080160', ']C1', 'ABC-123', '=1+2'):
        assert parse_label(text, {'PC9030'}) is None


def test_aim_prefix_is_also_ignored_for_the_old_formats():
    """라벨만 리더 표시(]C1)를 떼면, 리더 설정 하나로 기존 'PC9030 로트' 스캔이 전부 '등록되지 않은 재종' 이 된다."""
    r = parse_scan(']C1PC9030 0001015661578', {'PC9030'})
    assert (r.grade, r.lot) == ('PC9030', '0001015661578')


# ---------------------------------------------------------------- 거절된 스캔은 아무것도 바꾸지 않는다
def _master_events(ctrl):
    return [e for e in ctrl.inventory.events(limit=0) if e.kind == 'MASTER']


def test_conveyor_full_label_scan_does_not_add_the_grade(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        notes = []
        ctrl.subscribe(lambda k, p: notes.append(p.get('text', '')) if k == 'notice' else None)
        for _ in range(int(ctrl.config.conveyor_capacity)):
            ctrl.register_arrival('PC9030')
        n_master = len(_master_events(ctrl))
        with pytest.raises(ConveyorFull, match='컨베이어가 가득'):
            ctrl.scan(LABEL)
        assert ctrl.inventory.grade('KP320') is None
        assert len(_master_events(ctrl)) == n_master
        assert not any('KP320' in n for n in notes)
        assert 'KP320' not in ctrl.overbridge._pool
        assert ctrl.check_consistency() == []
    finally:
        ctrl.shutdown()


def test_duplicate_label_with_unknown_grade_is_refused_without_adding_it(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        ctrl.scan('0026080140015000010A30')
        ctrl._last_scan = None
        with pytest.raises(InventoryError) as e:
            ctrl.scan('0026080140015000010ZZ9')      # 같은 로트·생산순서인데 재종이 잘못 읽힘
        msg = str(e.value)
        assert '이미 등록된' in msg and '26080140-010' in msg and '컨베이어' in msg   # 박스가 지금 어디 있는지
        assert ctrl.inventory.grade('ZZ9') is None
        assert ctrl.check_consistency() == []
    finally:
        ctrl.shutdown()


def test_duplicate_label_says_where_the_box_is(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        box = ctrl.scan(LABEL)
        slot = ctrl.manual_store(box.id)
        ctrl._last_scan = None
        with pytest.raises(InventoryError) as e:
            ctrl.scan(LABEL)
        assert '이미 등록된' in str(e.value) and slot.id in str(e.value)
    finally:
        ctrl.shutdown()


def test_cancelled_label_can_be_scanned_again(tmp_path):
    """박스 ID = 로트-생산순서 (라벨에서 나옴). 도착 취소한 박스도 기록(ID)이 남으므로, 그대로 두면 잘못 찍고 취소한 라벨은
    실물 박스가 다시 와도 영영 스캔할 수 없다 → 취소된 기록만 있으면 '-2' 를 붙여 다시 등록한다."""
    ctrl = Controller(tmp_path, AppConfig())
    try:
        first = ctrl.scan(LABEL)
        ctrl.cancel_arrival(first.id, '잘못 스캔')
        ctrl._last_scan = None
        again = ctrl.scan(LABEL)
        assert again.id == '26080160-001-2'
        assert (again.grade, again.lot, again.weight_kg) == ('KP320', '26080160', 15.0)
        ctrl._last_scan = None
        with pytest.raises(InventoryError) as e:          # 다시 등록된 박스가 컨베이어에 있으면 거절
            ctrl.scan(LABEL)
        assert '이미 등록된' in str(e.value) and '26080160-001-2' in str(e.value) and '컨베이어' in str(e.value)
        # 적재했다가 출고한 박스(정상 출고)는 다시 받지 않는다
        b = ctrl.scan('0026080160015000005KP320')
        slot = ctrl.manual_store(b.id)
        ctrl.retrieve(slot.id)
        ctrl._last_scan = None
        with pytest.raises(InventoryError) as e:
            ctrl.scan('0026080160015000005KP320')
        assert '출고' in str(e.value)
        assert ctrl.check_consistency() == []
    finally:
        ctrl.shutdown()


def test_inactive_grade_on_label_is_refused_not_reactivated(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        ctrl.scan(LABEL)
        ctrl.set_grade_active('KP320', False)
        n_master = len(_master_events(ctrl))
        with pytest.raises(InventoryError, match='사용중지'):
            ctrl.scan('0026080160015000002KP320')
        assert ctrl.inventory.grade('KP320').active is False
        assert len(_master_events(ctrl)) == n_master
        assert len(ctrl.inventory.conveyor()) == 1
    finally:
        ctrl.shutdown()


def test_label_scan_debounce_and_scan_log(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        ctrl.scan(LABEL)
        with pytest.raises(InventoryError, match=SCAN_DEBOUNCE_TEXT):
            ctrl.scan(LABEL + '\r\n')                # 리더가 한 번 더 읽음
        assert len(ctrl.inventory.conveyor()) == 1
        with ctrl.scan_log_path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
    finally:
        ctrl.shutdown()
    assert [r[3] for r in rows[1:]] == ['등록', '거절']
    assert rows[1][1] == LABEL and '26080160-001' in rows[1][4] and 'KP320' in rows[1][4] and '15 kg' in rows[1][4]


def test_auto_colour_is_not_a_near_copy_of_an_existing_grade(tmp_path):
    """칸은 색으로 재종을 가른다 — KP320 이 NC6215(#e04848) 와 거의 같은 빨강(#d9534f)이 되면 안 된다."""
    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def dist(a, b):
        return sum((x - y) ** 2 for x, y in zip(rgb(a), rgb(b))) ** 0.5

    ctrl = Controller(tmp_path, AppConfig())
    try:
        for i, code in enumerate(('KP320', 'A30', 'KU10')):
            ctrl.scan(f'0026080160015000{i + 1:03d}{code}')
            new = ctrl.inventory.grade(code)
            others = [g.color for g in ctrl.inventory.grades() if g.code != code]
            assert min(dist(new.color, c) for c in others) >= 40, (code, new.color)
    finally:
        ctrl.shutdown()


# ---------------------------------------------------------------- 실제 화면 경로
@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(tmp_path, qapp, monkeypatch):
    from ui.main_window import MainWindow
    shown = []
    monkeypatch.setattr(QMessageBox, 'warning', staticmethod(lambda *a, **k: (shown.append(a[2]), QMessageBox.Ok)[1]))
    ctrl = Controller(tmp_path / 'w', AppConfig())
    ctrl.auto_dispatch = False
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


def _type_scan(win, qapp, text):
    tab = win.inbound
    win.tabs.setCurrentWidget(tab)
    tab.focus_scan()
    qapp.processEvents()
    QTest.keyClicks(tab.scan, text)
    QTest.keyClick(tab.scan, Qt.Key_Return)
    qapp.processEvents()
    win._flush()
    qapp.processEvents()


def test_ui_label_scan_shows_new_grade_everywhere(win, qapp):
    from ui.rack_view import tooltip_html
    ctrl = win.ctrl
    _type_scan(win, qapp, LABEL)
    assert win.shown == []
    res = win.inbound.result.text()
    assert '26080160-001' in res and 'KP320' in res and '15 kg' in res
    g = ctrl.inventory.grade('KP320')
    assert win.inbound.grade.findData('KP320') >= 0                       # 입고 재종 콤보
    assert win.rack.grade_combo.findData('KP320') >= 0                     # 적재대 검색 콤보
    assert win.history.grade.findData('KP320') >= 0                        # 이력 콤보
    assert 'KP320' in [r.key for r in win.master.table.model_.rows]       # 기준정보 표
    assert win.rack.model.color('KP320') == g.color                       # 칸·컨베이어 색
    assert 'KP320' in ctrl.overbridge._pool                                # 오버브릿지 시뮬레이션
    slot = ctrl.manual_store('26080160-001')
    win._flush()
    qapp.processEvents()
    assert 'KP320' in win.rack.legend._buttons                             # 범례 칩
    tip = tooltip_html(win.rack.model, slot.id)
    assert '미분류' in tip and '(- ·' not in tip                           # '-' 대신 사람이 읽는 말
    win.select_slot(slot.id)
    qapp.processEvents()
    win.rack.repaint_all()
    qapp.processEvents()
    assert ctrl.check_consistency() == []


def test_ui_rejected_label_scan_leaves_no_new_grade(win, qapp):
    ctrl = win.ctrl
    for _ in range(int(ctrl.config.conveyor_capacity)):
        ctrl.register_arrival('PC9030')
    win._flush()
    _type_scan(win, qapp, LABEL)
    assert win.shown and '컨베이어가 가득' in win.shown[-1]
    assert win.inbound.grade.findData('KP320') < 0
    assert 'KP320' not in [r.key for r in win.master.table.model_.rows]
    assert ctrl.inventory.grade('KP320') is None
