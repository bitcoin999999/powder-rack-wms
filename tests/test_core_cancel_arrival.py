"""InventoryService.cancel_arrival — 잘못 스캔·두 번 읽힌 컨베이어 박스 지우기 (SPEC §12)."""
from __future__ import annotations

import time

import pytest

from core.config import AppConfig
from core.inventory import ConveyorFull, InventoryError, InventoryService
from core.models import BoxState, SlotState

T0 = time.mktime((2026, 10, 20, 9, 0, 0, 0, 0, -1))


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, kind: str, payload: dict) -> None:
        self.calls.append((kind, payload))


@pytest.fixture
def inv(tmp_path):
    s = InventoryService(tmp_path / 'rack.db', AppConfig(), clock=lambda: T0)
    yield s
    s.close()


def n_events(inv: InventoryService) -> int:
    return len(inv.events(limit=0))


def test_conveyor_box_goes_out_with_correct_event(inv):
    a = inv.register_arrival('PC9030')
    b = inv.register_arrival('NC3020')
    out = inv.cancel_arrival(a.id, '두 번 읽힘')
    assert out.id == a.id and out.state == BoxState.OUT
    assert out.out_at and out.slot_id is None and out.order_id is None and not out.needs_check
    assert [x.id for x in inv.conveyor()] == [b.id]
    ev = inv.events(limit=1)[0]
    assert ev.kind == 'CORRECT' and ev.box_id == a.id and ev.grade == 'PC9030'
    assert '도착 취소' in ev.detail and '두 번 읽힘' in ev.detail
    assert inv.snapshot().conveyor == 1
    assert inv.check_invariants() == []


@pytest.mark.parametrize('reason', ['', '   ', None, '\n\t'])
def test_reason_required_and_nothing_changes(inv, reason):
    a = inv.register_arrival('PC9030')
    before = n_events(inv)
    with pytest.raises(InventoryError, match='사유'):
        inv.cancel_arrival(a.id, reason)
    assert inv.box(a.id).state == BoxState.CONVEYOR
    assert n_events(inv) == before


def test_box_bound_to_an_order_is_refused(inv):
    a = inv.register_arrival('PC9030')
    slot = inv.reserve_slot(a.id, order_id='O0001')
    before = n_events(inv)
    with pytest.raises(InventoryError, match='O0001'):
        inv.cancel_arrival(a.id, '잘못 스캔')
    box = inv.box(a.id)
    assert box.state == BoxState.CONVEYOR and box.order_id == 'O0001' and box.slot_id == slot.id
    assert inv.slot(slot.id).state == SlotState.RESERVED
    assert n_events(inv) == before


def test_reservation_without_order_is_released(inv):
    a = inv.register_arrival('PC9030')
    slot = inv.reserve_slot(a.id)
    inv.cancel_arrival(a.id, '잘못 스캔')
    assert inv.slot(slot.id).state == SlotState.EMPTY
    assert inv.slot(slot.id).reserved_for is None
    kinds = [e.kind for e in inv.events(limit=2)]
    assert kinds == ['CORRECT', 'UNRESERVE']
    assert inv.check_invariants() == []


def test_one_notification_per_cancel(inv):
    a = inv.register_arrival('PC9030')
    slot = inv.reserve_slot(a.id)
    rec = Recorder()
    inv.subscribe(rec)
    inv.cancel_arrival(a.id, '잘못 스캔')
    assert len(rec.calls) == 1
    kind, payload = rec.calls[0]
    assert kind == 'CORRECT'
    assert payload['box'] == a.id and payload['slot'] == slot.id and payload['grade'] == 'PC9030'
    assert slot.id in payload['slots'] and len(payload['events']) == 2


def test_refused_for_boxes_not_on_conveyor(inv):
    a = inv.register_arrival('PC9030')
    inv.reserve_slot(a.id, order_id='O1')
    inv.mark_picked(a.id, 'AMR-01', 'O1')
    with pytest.raises(InventoryError, match='컨베이어 위 박스만'):
        inv.cancel_arrival(a.id, 'x')                              # ON_AMR
    inv.confirm_store(a.id, robot='AMR-01')
    with pytest.raises(InventoryError, match='컨베이어 위 박스만'):
        inv.cancel_arrival(a.id, 'x')                              # STORED
    b = inv.register_arrival('PC9030')
    inv.cancel_arrival(b.id, '중복')
    with pytest.raises(InventoryError, match='컨베이어 위 박스만'):
        inv.cancel_arrival(b.id, '중복')                           # 이미 OUT
    with pytest.raises(InventoryError, match='없는 박스'):
        inv.cancel_arrival('NO-SUCH', '중복')
    assert inv.check_invariants() == []


def test_lower_case_id_and_frees_a_conveyor_place(inv):
    boxes = [inv.register_arrival('PC9030') for _ in range(5)]
    with pytest.raises(ConveyorFull):
        inv.register_arrival('PC9030')
    inv.cancel_arrival(boxes[2].id.lower(), '두 번 읽힘')
    c = inv.register_arrival('NC3020')
    assert [b.id for b in inv.conveyor()] == [boxes[0].id, boxes[1].id, boxes[3].id, boxes[4].id, c.id]
    assert inv.check_invariants() == []


def test_cancelled_box_id_stays_taken_and_survives_reopen(tmp_path):
    cfg = AppConfig()
    s = InventoryService(tmp_path / 'r.db', cfg, clock=lambda: T0)
    a = s.register_arrival('PC9030', box_id='BX-1')
    s.cancel_arrival('BX-1', '잘못 스캔')
    with pytest.raises(InventoryError, match='이미 등록된'):
        s.register_arrival('PC9030', box_id='BX-1')
    s.close()
    s2 = InventoryService(tmp_path / 'r.db', cfg, clock=lambda: T0)
    try:
        box = s2.box(a.id)
        assert box.state == BoxState.OUT and box.out_at
        assert s2.conveyor() == []
        assert s2.check_invariants() == []
    finally:
        s2.close()
