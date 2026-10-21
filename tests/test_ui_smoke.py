"""화면 스모크 시험 (offscreen): 주 창을 만들고 시뮬레이션을 돌리며 탭·스캔·칸 선택·전체 갱신이 예외 없이 되는지.

알림 상자(QMessageBox)는 가짜로 바꿔 모달로 멈추지 않게 하고, 무엇이 떴는지 기록해 확인한다."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')     # PyQt5 를 들여오기 전에
if sys.platform == 'win32':
    os.environ.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'))

import pytest  # noqa: E402
from PyQt5.QtCore import QPoint, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from controller import Controller  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.models import BoxState, SlotState  # noqa: E402


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class Boxes:
    """가짜 QMessageBox 정적 함수 — 호출 기록."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def kinds(self, kind: str) -> list[str]:
        return [t for k, t in self.calls if k == kind]


@pytest.fixture
def boxes(monkeypatch):
    rec = Boxes()

    def make(kind, ret=None):
        def fake(parent, title, text, *a, **kw):
            rec.calls.append((kind, str(text)))
            return QMessageBox.Yes if ret is None else ret
        return staticmethod(fake)

    monkeypatch.setattr(QMessageBox, 'warning', make('warning', QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, 'critical', make('critical', QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, 'information', make('information', QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, 'question', make('question', QMessageBox.Yes))
    return rec


@pytest.fixture
def make_win(tmp_path, qapp, boxes):
    from ui.main_window import MainWindow
    made = []

    def factory(robots: int = 1, sub: str = 'a'):
        cfg = AppConfig()
        cfg.sim.amr_count = robots
        ctrl = Controller(tmp_path / sub, cfg)
        win = MainWindow(ctrl)
        win.resize(1440, 900)
        win.show()
        qapp.processEvents()
        made.append((win, ctrl))
        return win, ctrl

    yield factory
    for win, ctrl in made:
        win.force_close()
        ctrl.shutdown()
    qapp.processEvents()


def run_sim(win, ctrl, qapp, sim_seconds: float, speed: float = 20.0) -> None:
    ctrl.set_sim_speed(speed)
    steps = int(sim_seconds / speed / 0.1)
    for i in range(steps):
        ctrl.tick(0.1)
        if i % 25 == 0:
            win._flush()
            qapp.processEvents()
    win._flush()
    qapp.processEvents()


def click_slot(win, qapp, sid: str) -> None:
    view = win.rack.north if sid.startswith('N') else win.rack.south
    rect = view.cell_rect(int(sid[2:4]), int(sid[5:]))
    QTest.mouseClick(view, Qt.LeftButton, Qt.NoModifier, rect.center())
    qapp.processEvents()


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------- 시험
def test_main_window_smoke(make_win, qapp, boxes, caplog):
    caplog.set_level(logging.WARNING)
    win, ctrl = make_win()
    assert win.windowTitle().startswith('완분 적재대 관리')
    assert win.minimumWidth() >= 1280 and win.minimumHeight() >= 800

    # 스캔: 정상 → 컨베이어에 1개, 알림 상자 없음
    scan = win.inbound.scan
    assert not scan.testAttribute(Qt.WA_InputMethodEnabled)     # 한글 입력기 끔
    scan.setText('PC9030')
    QTest.keyClick(scan, Qt.Key_Return)
    conv = ctrl.inventory.conveyor()
    assert [b.grade for b in conv] == ['PC9030']
    assert boxes.kinds('warning') == []
    assert scan.text() == ''
    # 같은 스캔 2초 안 → 무시 (모달 없이 표시만)
    scan.setText('PC9030')
    QTest.keyClick(scan, Qt.Key_Return)
    assert len(ctrl.inventory.conveyor()) == 1
    assert boxes.kinds('warning') == []
    assert '2초' in win.inbound.result.text()
    # 잘못된 스캔 → 경고 상자에 InventoryError 문구
    scan.setText('ZZZ999')
    QTest.keyClick(scan, Qt.Key_Return)
    assert any('등록되지 않은 재종' in t for t in boxes.kinds('warning'))
    assert len(ctrl.inventory.conveyor()) == 1

    # 수동 입력·샘플 박스
    win.inbound.lot.setText('L-TEST-1')
    win.inbound.add_btn.click()
    win.inbound.sample_btn.click()
    win._flush()
    assert len(ctrl.inventory.conveyor()) == 3
    assert win.inbound.table.model_.rowCount() == 3

    # 시뮬레이션 (오버브릿지 + 자동 배차) 몇 백 초
    ctrl.start_overbridge(12)
    run_sim(win, ctrl, qapp, 600)
    snap = ctrl.inventory.snapshot()
    assert snap.occupied > 0
    assert win.header.k_stock.value.text() == f'{snap.occupied}/{snap.capacity}'

    # 탭 전부 돌기 (그릴 때 예외 없게 grab)
    for i in range(win.tabs.count()):
        win.tabs.setCurrentIndex(i)
        win._flush()
        qapp.processEvents()
        assert not win.grab().isNull()
    win.tabs.setCurrentWidget(win.amr_tab)
    win._map_frame()
    assert win.amr_tab.table.model_.rowCount() > 0
    assert len(win.amr_tab.cards) == 1
    win.tabs.setCurrentWidget(win.history)
    win._flush()
    assert win.history.table.model_.rowCount() > 0

    # 칸 선택 (마우스) → 상세
    sid = next(s.id for s in ctrl.inventory.slots() if s.state == SlotState.OCCUPIED)
    click_slot(win, qapp, sid)
    assert win.slot_detail.sid == sid
    assert win.rack.model.selected == sid
    assert win.slot_detail.b_out.isEnabled() and not win.slot_detail.b_stored.isVisible()

    # 재종 검색 → 선입선출 순번
    grade = ctrl.inventory.slot(sid)
    code = ctrl.inventory.box(grade.box_id).grade
    win.search_grade(code)
    assert win.rack.model.ranks and min(win.rack.model.ranks.values()) == 1
    win.outbound.set_grade(code)
    assert win.outbound.table.model_.rowCount() == len(ctrl.inventory.find_grade(code))

    win.refresh_all()
    qapp.processEvents()
    assert not win.grab().isNull()
    assert boxes.kinds('critical') == []
    assert errors(caplog) == []


def test_config_change_rebuilds_amr_view(make_win, qapp, boxes, caplog):
    caplog.set_level(logging.WARNING)
    win, ctrl = make_win(sub='cfg')
    win.tabs.setCurrentWidget(win.amr_tab)
    win._flush()
    assert len(win.amr_tab.cards) == 1
    import copy
    cfg = copy.deepcopy(ctrl.config)
    cfg.sim.amr_count = 3
    ctrl.apply_config(cfg)
    win._flush()
    assert len(win.amr_tab.cards) == 3
    assert not win.amr_tab.minimap.grab().isNull()
    # 적재대 구성 변경 (비어 있을 때) → 칸 수 반영
    cfg = copy.deepcopy(ctrl.config)
    cfg.rack.columns, cfg.rack.tiers = 6, 3
    ctrl.apply_config(cfg)
    win._flush()
    assert win.rack.model.columns == 6 and win.rack.model.tiers == 3
    assert len(win.rack.model.cells) == 36
    assert win.header.k_stock.value.text() == '0/36'
    win.tabs.setCurrentWidget(win.master)
    win._flush()
    assert win.master.cols.value() == 6
    assert not win.grab().isNull()
    assert errors(caplog) == []


def test_slot_states_and_actions_render(make_win, qapp, boxes, caplog):
    """사용중지·직접 등록·이동·수정·확인 필요(AMR 고장) 상태를 화면이 그리고 버튼이 상태에 맞게 켜진다."""
    caplog.set_level(logging.WARNING)
    win, ctrl = make_win(sub='act')
    ctrl.block_slot('N-02-1', '점검')
    box = ctrl.put_directly('N-01-1', 'PC9030', '', 14.2, reason='실사')
    win._flush()
    assert win.rack.model.cells['N-02-1'][0].state == SlotState.BLOCKED
    win.select_slot('N-02-1')
    assert win.slot_detail.b_block.text() == '사용재개' and win.slot_detail.b_block.isEnabled()
    rec = ctrl.recommend_slot('PC9030', {'N-01-1'})
    assert rec and rec != 'N-02-1'
    ctrl.move_box(box.id, rec, '실사 이동')
    new = ctrl.replace_slot_content(rec, 'NC3010', 'L-FIX', 15.0, '재종 잘못 기록')
    win._flush()
    assert win.rack.model.cells[rec][1].grade == 'NC3010'
    assert ctrl.inventory.box(box.id).state == BoxState.OUT
    win.select_slot(rec)
    assert win.slot_detail.values[0].text() == new.id

    # AMR 이 박스를 싣고 가다 고장 → 확인 필요 칸 (빨간 테두리) + 정리 버튼
    ctrl.add_sample_box('NC3020')
    ctrl.set_sim_speed(20)
    for _ in range(400):
        ctrl.tick(0.1)
        on = ctrl.inventory.boxes((BoxState.ON_AMR,))
        if on:
            break
    assert ctrl.inventory.boxes((BoxState.ON_AMR,))
    drv = ctrl.fleet.driver('AMR-01')
    drv.inject_failure('시험 고장')
    for _ in range(5):
        ctrl.tick(0.1)
    flagged = [b for b in ctrl.inventory.boxes((BoxState.ON_AMR,)) if b.needs_check]
    assert flagged
    win._flush()
    sid = flagged[0].slot_id
    win.select_slot(sid)
    win.tabs.setCurrentWidget(win.amr_tab)
    win._flush()
    qapp.processEvents()
    assert win.slot_detail.b_stored.isVisible() and win.slot_detail.b_return.isVisible()
    assert not win.grab().isNull()
    win.slot_detail._resolve_stored()          # 확인 질문은 가짜 상자가 '예'
    win._flush()
    assert ctrl.inventory.slot(sid).state == SlotState.OCCUPIED
    assert ctrl.check_consistency() == []
    assert errors(caplog) == []


def test_close_asks_when_amr_busy(make_win, qapp, boxes):
    win, ctrl = make_win(sub='close')
    ctrl.add_sample_box('PC9030')
    ctrl.set_sim_speed(5)
    for _ in range(20):
        ctrl.tick(0.1)
        if [o for o in ctrl.fleet.orders(active_only=True) if o.kind != 'PARK']:
            break
    assert [o for o in ctrl.fleet.orders(active_only=True) if o.kind != 'PARK']
    win.close()
    assert any('진행 중인 AMR 작업' in t for t in boxes.kinds('question'))


def test_action_handlers_with_dialogs(make_win, qapp, boxes, caplog, monkeypatch, tmp_path):
    """대화상자를 '확인'으로 바로 닫히게 바꾸고 화면의 작업 버튼 처리기를 차례로 실행한다."""
    from PyQt5.QtWidgets import QDialog, QFileDialog
    from ui import dialogs
    from ui.rack_view import tooltip_html
    caplog.set_level(logging.WARNING)
    for cls in (dialogs.ReasonDialog, dialogs.SlotPickDialog, dialogs.BoxEntryDialog):
        monkeypatch.setattr(cls, 'exec_', lambda self: QDialog.Accepted)
    monkeypatch.setattr(dialogs.GradeDialog, 'exec_',
                        lambda self: (self.code.setText('TEST01') if self.code.isEnabled() else None,
                                      QDialog.Accepted)[1])
    win, ctrl = make_win(sub='ui-act')
    ctrl.set_auto_dispatch(False)
    inb = win.inbound

    # 입고 탭: 도착 → 수동 적재 (추천 칸) / 도착 취소 (기본 사유)
    inb._sample()
    inb._sample()
    win._flush()
    inb.table.selectRow(0)
    first = inb.table.selected_key()
    inb._manual_store()
    assert ctrl.inventory.box(first).state == BoxState.STORED
    win._flush()
    inb.table.selectRow(0)
    second = inb.table.selected_key()
    inb._cancel_arrival()
    assert ctrl.inventory.box(second).state == BoxState.OUT
    sid = ctrl.inventory.box(first).slot_id
    assert win.slot_detail.sid == sid           # 수동 적재 후 그 칸 선택

    # 슬롯 상세: 이동 → 수동 출고
    win.slot_detail._move()
    moved_to = ctrl.inventory.box(first).slot_id
    assert moved_to != sid and win.slot_detail.sid == moved_to
    win.slot_detail._retrieve()
    assert ctrl.inventory.box(first).state == BoxState.OUT
    # 빈 칸 등록 → 사용중지 시도(빈 칸 아님 → 경고) → 비우기(사유 없음 → 경고)
    win.select_slot('S-05-2')
    win.slot_detail._edit()
    assert ctrl.inventory.slot('S-05-2').state == SlotState.OCCUPIED
    win.slot_detail._clear()
    assert any('사유' in t for t in boxes.kinds('warning'))
    assert ctrl.inventory.slot('S-05-2').state == SlotState.OCCUPIED
    win.select_slot('S-06-2')
    win.slot_detail._block()                    # 기본 사유 '점검'
    assert ctrl.inventory.slot('S-06-2').state == SlotState.BLOCKED
    win._flush()
    assert '점검' in tooltip_html(win.rack.model, 'S-06-2')
    win.slot_detail._block()                    # 사용재개
    assert ctrl.inventory.slot('S-06-2').state == SlotState.EMPTY

    # 출고 조회: 재종 선택 → 1번 출고
    code = ctrl.inventory.box(ctrl.inventory.slot('S-05-2').box_id).grade
    win.tabs.setCurrentWidget(win.outbound)
    win.outbound.set_grade(code)
    win.outbound._retrieve()
    assert ctrl.inventory.slot('S-05-2').state == SlotState.EMPTY

    # 기준정보: 재종 추가 → 입고 재종 목록에 나옴, 사용중지 토글
    win.master._add()
    win._flush()
    assert ctrl.inventory.grade('TEST01') is not None
    assert win.inbound.grade.findData('TEST01') >= 0
    win.master.table.select_key('TEST01')
    win.master._toggle_active()
    win._flush()
    assert not ctrl.inventory.grade('TEST01').active
    assert win.inbound.grade.findData('TEST01') < 0

    # 이력: CSV 저장 (파일 고르기 대화상자는 가짜)
    out = tmp_path / '이력.csv'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', staticmethod(lambda *a, **k: (str(out), '')))
    win.tabs.setCurrentWidget(win.history)
    win.history._csv()
    assert out.exists() and out.stat().st_size > 0
    assert any('저장' in t for t in boxes.kinds('information'))

    # 상단 바: 배속·오버브릿지·자동 배차
    win.header.speed_buttons[5.0].click()
    assert ctrl.sim_speed == 5.0
    win.header.ob_btn.click()
    assert ctrl.overbridge.running and win.header.ob_btn.text().endswith('정지')
    win.header.ob_btn.click()
    assert not ctrl.overbridge.running
    win.header.dispatch_btn.click()
    assert ctrl.auto_dispatch
    assert win._notices                         # 알림 목록 (모달이라 띄우지 않고 만들어서 그려 본다)
    nd = dialogs.NoticeDialog(win, list(win._notices))
    assert nd.list.count() == len(win._notices) and not nd.grab().isNull()
    nd.deleteLater()
    assert ctrl.check_consistency() == []
    assert boxes.kinds('critical') == []
    assert errors(caplog) == []


def test_search_filters_and_dev_shots(make_win, qapp, boxes, caplog, tmp_path):
    caplog.set_level(logging.WARNING)
    win, ctrl = make_win(sub='search')
    for g in ('PC9030', 'PC9030', 'NC3010'):
        ctrl.manual_store(ctrl.add_sample_box(g).id)
    win._flush()
    # 적재대 글자 검색 (칸 ID 일부) / 재종 코드 그대로 → 재종 검색
    win.rack._text_search('N-01')
    assert win.rack.model.search_text == 'N-01' and win.rack.model.matches
    win.rack._text_search('pc9030')
    assert win.rack.model.search_grade == 'PC9030' and sorted(win.rack.model.ranks.values()) == [1, 2]
    win.rack.legend.refresh()
    assert win.rack.legend._buttons['PC9030'].isChecked()
    win.rack.clear_search()
    assert not win.rack.model.searching
    # 출고 조회: 없는 재종 / 전체 보기
    win.tabs.setCurrentWidget(win.outbound)
    win.outbound.combo.setEditText('ZZZ1')
    win.outbound._find()
    assert '등록되지 않은 재종' in win.outbound.summary.text()
    win.outbound.set_grade(None)
    assert win.outbound.overview.model_.rowCount() == 2
    # 이력: 종류·글자 필터
    win.tabs.setCurrentWidget(win.history)
    win.history.kind.setCurrentIndex(win.history.kind.findData('STORE'))
    assert all(r.cells[1] == '적재' for r in win.history.table.model_.rows)
    box_id = ctrl.inventory.boxes((BoxState.STORED,))[0].id
    win.history.kind.setCurrentIndex(0)
    win.history.text.setText(box_id[-4:])
    win.history.reload()
    assert win.history.table.model_.rowCount() > 0
    assert all(box_id[-4:] in (r.cells[2] + r.cells[4]) for r in win.history.table.model_.rows)
    # 개발용 캡처 (RACKWMS_SHOT_TABS)
    saved = win.dev_save_shots(tmp_path / 'shots')
    assert len(saved) == win.tabs.count() + 1 and all(p.exists() for p in saved)
    assert errors(caplog) == []
