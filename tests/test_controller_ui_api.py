"""Controller 의 화면용 재고 작업 (2단계에서 추가 — 화면은 재고를 읽기만 하고 바꾸는 일은 Controller 로 한다)."""
from __future__ import annotations

import pytest

from controller import Controller
from core.config import AppConfig
from core.inventory import InventoryError
from core.models import BoxState, Grade, SlotState


@pytest.fixture
def ctrl(tmp_path):
    cfg = AppConfig()
    cfg.auto_dispatch = False
    c = Controller(tmp_path, cfg)
    yield c
    c.shutdown()


def test_register_arrival_and_retrieve(ctrl):
    box = ctrl.register_arrival('pc9030', '', 14.0)
    assert box.state == BoxState.CONVEYOR and box.grade == 'PC9030' and box.lot.isdigit() and len(box.lot) == 13
    assert box.note == '수동 입력'
    slot = ctrl.manual_store(box.id, None)
    out = ctrl.retrieve(slot.id, '시험 출고')
    assert out.state == BoxState.OUT
    assert ctrl.inventory.slot(slot.id).state == SlotState.EMPTY


def test_put_move_and_recommend(ctrl):
    b = ctrl.put_directly('N-01-1', 'NC3010', None, 15.0, box_id='', reason='실사')
    assert ctrl.inventory.slot('N-01-1').box_id == b.id
    rec = ctrl.recommend_slot('NC3010', {'N-01-1'})
    assert rec == 'N-01-2'          # 같은 재종 열 우선: 같은 열의 다음 단
    before = ctrl.inventory.snapshot()
    assert ctrl.recommend_slot('NC3010') == rec   # 예약하지 않는다
    assert ctrl.inventory.snapshot() == before
    ctrl.move_box(b.id, 'S-03-1', '실사 이동')
    assert ctrl.inventory.box(b.id).slot_id == 'S-03-1'


def test_replace_slot_content_checks_before_clearing(ctrl):
    b = ctrl.put_directly('N-02-1', 'PC9030', 'L-OLD', 15.0, reason='실사')
    bad = [('N-02-1', 'PC9030', 'L', 15.0, ''),               # 사유 없음
           ('N-02-1', 'XX999', 'L', 15.0, '오기'),             # 없는 재종
           ('N-02-1', 'PC9030', 'L', 0, '오기'),               # 중량
           ('N-02-1', 'PC9030', 'L' * 61, 15.0, '오기'),       # 로트 길이
           ('N-03-1', 'PC9030', 'L', 15.0, '오기')]            # 빈 칸
    for args in bad:
        with pytest.raises(InventoryError):
            ctrl.replace_slot_content(*args)
        assert ctrl.inventory.slot('N-02-1').box_id == b.id    # 아무것도 바뀌지 않음
    new = ctrl.replace_slot_content('N-02-1', 'nc3010', 'L-NEW', 12.5, '재종 오기')
    assert new.id != b.id and new.grade == 'NC3010' and new.lot == 'L-NEW' and new.weight_kg == 12.5
    assert ctrl.inventory.box(b.id).state == BoxState.OUT
    assert ctrl.inventory.slot('N-02-1').box_id == new.id
    assert ctrl.check_consistency() == []


def test_clear_block_require_reason(ctrl):
    ctrl.put_directly('N-04-1', 'H01', '', 15.0, reason='실사')
    with pytest.raises(InventoryError):
        ctrl.clear_slot('N-04-1', '  ')
    with pytest.raises(InventoryError):
        ctrl.block_slot('N-05-1', '')
    ctrl.clear_slot('N-04-1', '실제로 비어 있음')
    ctrl.block_slot('N-05-1', '점검')
    assert ctrl.inventory.slot('N-05-1').state == SlotState.BLOCKED
    ctrl.unblock_slot('N-05-1')
    assert ctrl.inventory.slot('N-05-1').state == SlotState.EMPTY


def test_grades_and_csv(ctrl, tmp_path):
    ctrl.upsert_grade(Grade('TEST1', 'P', 'P10–P20', 'CVD', '시험', '#123456'))
    assert ctrl.inventory.grade('TEST1').active
    ctrl.set_grade_active('TEST1', False)
    assert not ctrl.inventory.grade('TEST1').active
    n = ctrl.export_events_csv(tmp_path / 'out' / '이력.csv')
    assert n >= 3 and (tmp_path / 'out' / '이력.csv').exists()
