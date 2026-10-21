"""core.inventory.InventoryService — 입고·예약·적재·출고·수정·영속성·불변식 (SPEC §4~§8)."""
from __future__ import annotations

import csv
import hashlib
import random
import time

import pytest

from core.config import AppConfig, RackConfig
from core.grades import SAMPLE_GRADES
from core.inventory import ConveyorFull, InventoryError, InventoryService, RackFull
from core.models import BoxState, Grade, SlotState
from core.rack import POLICIES

T0 = time.mktime((2026, 10, 20, 9, 0, 0, 0, 0, -1))       # 로컬 2026-10-20 09:00:00


class FakeClock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, kind: str, payload: dict) -> None:
        self.calls.append((kind, payload))

    @property
    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_inv(tmp_path, clock):
    opened: list[InventoryService] = []

    def factory(config: AppConfig | None = None, name: str = 'rack.db') -> InventoryService:
        s = InventoryService(tmp_path / name, config or AppConfig(), clock=clock)
        opened.append(s)
        return s

    yield factory
    for s in opened:
        s.close()


@pytest.fixture
def inv(make_inv):
    return make_inv()


def event_count(inv: InventoryService) -> int:
    return int(inv._db.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0])


def digest(inv: InventoryService) -> str:
    """DB 전체 상태 요약 (이벤트는 건수·마지막 ID)."""
    conn = inv._db.conn
    parts = []
    for table in ('slots', 'boxes', 'grades', 'meta'):
        parts.append(repr([tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]))
    parts.append(repr(tuple(conn.execute('SELECT COUNT(*), MAX(id) FROM events').fetchone())))
    parts.append(f'{inv.policy}|{inv.layout.columns}x{inv.layout.tiers}')
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def assert_unchanged_on_error(inv: InventoryService, fn, exc=InventoryError, match: str | None = None):
    rec = Recorder()
    inv.subscribe(rec)
    before = digest(inv)
    with pytest.raises(exc, match=match):
        fn()
    assert digest(inv) == before
    assert rec.calls == []
    inv.unsubscribe(rec)


def arrive_store(inv: InventoryService, grade: str, slot: str | None = None, **kw):
    b = inv.register_arrival(grade, **kw)
    inv.manual_store(b.id, slot)
    return inv.box(b.id)


# ================================================================ 생성·조회
def test_new_db_seeds_grades_slots_and_one_event(inv):
    assert [g.code for g in inv.grades()] == [g.code for g in SAMPLE_GRADES]
    assert inv.grade('PC9030') == next(g for g in SAMPLE_GRADES if g.code == 'PC9030')
    assert inv.grade('pc9030').code == 'PC9030' and inv.grade('NOPE') is None and inv.grade('') is None
    slots = inv.slots()
    assert [s.id for s in slots] == inv.layout.slot_ids() and len(slots) == 80
    assert all(s.state == SlotState.EMPTY and s.box_id is None and s.reserved_for is None for s in slots)
    ev = inv.events()
    assert len(ev) == 1 and ev[0].kind == 'CONFIG' and '재고 DB 생성' in ev[0].detail
    assert inv.check_invariants() == []
    snap = inv.snapshot()
    assert (snap.capacity, snap.occupied, snap.reserved, snap.blocked, snap.conveyor, snap.needs_check) == (80, 0, 0, 0, 0, 0)
    assert inv.policy == 'grade_zone' and inv.layout.capacity == 80


def test_slot_and_box_lookup_errors(inv):
    assert inv.slot('n-03-2').id == 'N-03-2'
    for bad in ('N-11-1', 'garbage', '', None):
        with pytest.raises(InventoryError):
            inv.slot(bad)
    for bad in ('W000000-0000', '', None):
        with pytest.raises(InventoryError):
            inv.box(bad)


def test_db_file_created_in_missing_folder(tmp_path, clock):
    path = tmp_path / 'data' / 'sub' / 'rack.db'
    s = InventoryService(path, AppConfig(), clock=clock)
    try:
        assert path.exists()
    finally:
        s.close()
    s.close()   # 두 번 닫아도 된다


def test_invalid_rack_config_on_create(tmp_path, clock):
    cfg = AppConfig()
    cfg.rack.columns = 0
    with pytest.raises(InventoryError):
        InventoryService(tmp_path / 'x.db', cfg, clock=clock)


# ================================================================ 발번
def test_box_id_and_lot_numbering(inv, clock):
    assert inv.next_box_id() == 'W261020-0001'
    assert inv.next_box_id() == 'W261020-0001'              # 미리보기는 순번을 쓰지 않는다
    a = inv.register_arrival('PC9030')
    b = inv.register_arrival('PC9030')
    c = inv.register_arrival('NC3215')
    assert (a.id, b.id, c.id) == ('W261020-0001', 'W261020-0002', 'W261020-0003')
    assert (a.lot, b.lot, c.lot) == ('0001015661578', '0001015661579', '0001015661580')   # 재종 무관 일련번호
    assert a.arrived_seq < b.arrived_seq < c.arrived_seq
    assert a.arrived_at == '2026-10-20 09:00:00' and a.state == BoxState.CONVEYOR and a.weight_kg == 15.0
    clock.advance(24 * 3600)                                 # 다음 날 순번 1부터
    assert inv.next_box_id() == 'W261021-0001'
    inv.manual_store(a.id)
    d = inv.register_arrival('PC9030', lot='  ')             # 빈 로트 = 자동
    assert d.id == 'W261021-0001' and d.lot == '0001015661581' and d.arrived_seq > c.arrived_seq


def test_auto_box_id_skips_ids_entered_by_hand(inv):
    inv.register_arrival('PC9030', box_id='W261020-0002')
    assert inv.next_box_id() == 'W261020-0001'
    assert inv.register_arrival('PC9030').id == 'W261020-0001'
    assert inv.register_arrival('PC9030').id == 'W261020-0003'


def test_register_arrival_fields_and_event(inv):
    rec = Recorder()
    inv.subscribe(rec)
    b = inv.register_arrival('pc9030', lot='L-TEST', weight_kg=12.3, box_id=' BX-1 ', bags=2, note='시험')
    assert (b.id, b.grade, b.lot, b.weight_kg, b.bags, b.note) == ('BX-1', 'PC9030', 'L-TEST', 12.3, 2, '시험')
    assert inv.conveyor()[0].id == 'BX-1'
    ev = inv.events(limit=1)[0]
    assert ev.kind == 'ARRIVE' and ev.box_id == 'BX-1' and ev.grade == 'PC9030' and '잔량' in ev.detail
    assert rec.kinds == ['ARRIVE'] and rec.calls[0][1]['box'] == 'BX-1'


def test_duplicate_box_id_rejected(inv):
    b = inv.register_arrival('PC9030')
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('NC3215', box_id=b.id), match='이미 등록된 박스 ID')
    inv.manual_store(b.id)
    inv.retrieve(inv.box(b.id).slot_id)
    # 출고된 박스 ID 도 다시 쓸 수 없다 (이력 보존)
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('NC3215', box_id=b.id))
    assert_unchanged_on_error(inv, lambda: inv.put_directly('S-05-1', 'H01', 'L', box_id=b.id))


def test_unknown_and_inactive_grade_rejected(inv):
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('XX999'), match='등록되지 않은 재종')
    inv.set_grade_active('PC9030', False)
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('PC9030'), match='사용중지')
    assert [g.code for g in inv.grades(active_only=True)].count('PC9030') == 0
    inv.set_grade_active('PC9030', True)
    assert inv.register_arrival('PC9030').grade == 'PC9030'


@pytest.mark.parametrize('kwargs', [
    {'weight_kg': 0}, {'weight_kg': -1}, {'weight_kg': 'abc'}, {'weight_kg': float('nan')}, {'weight_kg': 1000},
    {'weight_kg': float('inf')}, {'weight_kg': None}, {'bags': 0}, {'bags': 1.5}, {'box_id': 'A B'},
    {'box_id': 'X' * 41}, {'lot': 123}, {'grade': ''}, {'grade': None},
])
def test_register_arrival_input_validation(inv, kwargs):
    args = {'grade': 'PC9030'} | kwargs
    assert_unchanged_on_error(inv, lambda: inv.register_arrival(**args))


def test_conveyor_full_at_capacity(make_inv):
    cfg = AppConfig()
    cfg.conveyor_capacity = 3
    inv = make_inv(cfg)
    boxes = [inv.register_arrival('PC9030') for _ in range(3)]
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('PC9030'), exc=ConveyorFull, match='3/3')
    assert issubclass(ConveyorFull, InventoryError)
    inv.manual_store(boxes[0].id)                          # 하나 치우면 다시 받는다
    inv.register_arrival('PC9030')
    assert len(inv.conveyor()) == 3
    # 예약·AMR 위 박스도 용량 계산: CONVEYOR 만 센다
    inv.reserve_slot(boxes[1].id)
    inv.mark_picked(boxes[1].id, 'AMR-01')
    assert len(inv.conveyor()) == 2
    inv.register_arrival('PC9030')
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('PC9030'), exc=ConveyorFull)


def test_default_conveyor_capacity_is_five(inv):
    for _ in range(5):
        inv.register_arrival('H01')
    with pytest.raises(ConveyorFull):
        inv.register_arrival('H01')


# ================================================================ 입고 흐름
def test_full_inbound_lifecycle(inv, clock):
    rec = Recorder()
    inv.subscribe(rec)
    b = inv.register_arrival('PC9030')
    s = inv.reserve_slot(b.id, order_id='O0001')
    assert s.state == SlotState.RESERVED and s.reserved_for == b.id and s.id == 'N-01-1'
    box = inv.box(b.id)
    assert box.slot_id == s.id and box.order_id == 'O0001' and box.state == BoxState.CONVEYOR
    assert inv.snapshot().reserved == 1 and inv.check_invariants() == []

    clock.advance(5)
    inv.mark_picked(b.id, 'AMR-01', order_id='O0001')
    box = inv.box(b.id)
    assert box.state == BoxState.ON_AMR and box.robot == 'AMR-01' and inv.conveyor() == []
    assert inv.slot(s.id).state == SlotState.RESERVED

    clock.advance(30)
    stored = inv.confirm_store(b.id, robot='AMR-01')
    assert stored.state == SlotState.OCCUPIED and stored.box_id == b.id and stored.reserved_for is None
    box = inv.box(b.id)
    assert box.state == BoxState.STORED and box.slot_id == s.id and box.stored_at == '2026-10-20 09:00:35'
    assert box.robot is None and box.order_id is None and not box.needs_check
    assert rec.kinds == ['ARRIVE', 'RESERVE', 'PICK', 'STORE']
    assert [e.kind for e in inv.events(box_id=b.id)] == ['STORE', 'PICK', 'RESERVE', 'ARRIVE']
    store_ev = inv.events(kind='STORE')[0]
    assert store_ev.robot == 'AMR-01' and store_ev.order_id == 'O0001' and store_ev.slot_id == s.id
    snap = inv.snapshot()
    assert (snap.occupied, snap.reserved, snap.conveyor, snap.by_grade) == (1, 0, 0, {'PC9030': 1})
    assert inv.check_invariants() == []


def test_reserve_slot_explicit_and_errors(inv):
    a, b = inv.register_arrival('PC9030'), inv.register_arrival('PC9030')
    assert inv.reserve_slot(a.id, 's-05-3').id == 'S-05-3'
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(a.id), match='이미')               # 두 번 예약
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(b.id, 'S-05-3'), match='비어 있지 않습니다')
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(b.id, 'N-11-1'))
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(b.id, 'garbage'))
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot('NOPE'))
    inv.block_slot('N-01-1', '점검')
    assert inv.reserve_slot(b.id).id != 'N-01-1'
    c = inv.register_arrival('H01')
    inv.manual_store(c.id)
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(c.id), match='컨베이어 위 박스만')


def test_reserve_with_exclude_and_grade_grouping(inv):
    a, b, c = (inv.register_arrival('PC9030') for _ in range(3))
    s1 = inv.reserve_slot(a.id, order_id='O1')
    s2 = inv.reserve_slot(b.id, order_id='O1')
    assert (s1.id, s2.id) == ('N-01-1', 'N-01-2')            # 같은 작업의 같은 재종은 같은 열로
    s3 = inv.reserve_slot(c.id, exclude={'N-01-3', 'N-01-4'})
    assert s3.id not in {'N-01-3', 'N-01-4'}


def test_rack_full(make_inv):
    cfg = AppConfig()
    cfg.rack = RackConfig(columns=1, tiers=1)
    inv = make_inv(cfg)
    assert inv.layout.capacity == 2
    a, b, c = (inv.register_arrival('PC9030') for _ in range(3))
    inv.reserve_slot(a.id)
    inv.manual_store(b.id)
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(c.id), exc=RackFull, match='빈 칸이 없습니다')
    assert_unchanged_on_error(inv, lambda: inv.manual_store(c.id), exc=RackFull)
    assert issubclass(RackFull, InventoryError)
    inv.release_reservation(a.id)
    inv.block_slot(inv.slots()[0].id if inv.slots()[0].state == SlotState.EMPTY else inv.slots()[1].id, '고장')
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(c.id), exc=RackFull)


def test_release_reservation(inv):
    rec = Recorder()
    a = inv.register_arrival('PC9030')
    s = inv.reserve_slot(a.id, order_id='O7')
    inv.subscribe(rec)
    inv.release_reservation(a.id, '작업 취소')
    assert inv.slot(s.id).state == SlotState.EMPTY and inv.slot(s.id).reserved_for is None
    box = inv.box(a.id)
    assert box.slot_id is None and box.order_id is None and box.state == BoxState.CONVEYOR
    assert rec.kinds == ['UNRESERVE']
    ev = inv.events(limit=1)[0]
    assert ev.kind == 'UNRESERVE' and '작업 취소' in ev.detail and ev.slot_id == s.id
    assert_unchanged_on_error(inv, lambda: inv.release_reservation(a.id), match='예약된 칸이 없는')
    inv.reserve_slot(a.id)
    inv.mark_picked(a.id, 'AMR-01')
    assert_unchanged_on_error(inv, lambda: inv.release_reservation(a.id), match='AMR에 실린')
    inv.confirm_store(a.id)
    assert_unchanged_on_error(inv, lambda: inv.release_reservation(a.id))
    assert_unchanged_on_error(inv, lambda: inv.release_reservation('NOPE'))


def test_mark_picked_errors(inv):
    a = inv.register_arrival('PC9030')
    inv.reserve_slot(a.id, order_id='O1')
    assert_unchanged_on_error(inv, lambda: inv.mark_picked(a.id, ''))
    assert_unchanged_on_error(inv, lambda: inv.mark_picked(a.id, 'AMR-01', order_id='O2'), match='다른 작업')
    inv.mark_picked(a.id, 'AMR-01')
    assert inv.box(a.id).order_id == 'O1'
    assert_unchanged_on_error(inv, lambda: inv.mark_picked(a.id, 'AMR-01'))      # 이미 AMR 위
    # 예약 없이 집는 것도 허용 (적재 칸은 confirm_store 에서 지정)
    b = inv.register_arrival('H01')
    inv.mark_picked(b.id, 'AMR-02')
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(b.id), match='칸이 정해지지')
    assert inv.confirm_store(b.id, 'S-09-1').id == 'S-09-1'
    assert inv.check_invariants() == []


def test_confirm_store_errors(inv):
    a, b = inv.register_arrival('PC9030'), inv.register_arrival('NC3215')
    sa = inv.reserve_slot(a.id)
    inv.mark_picked(a.id, 'AMR-01')
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(b.id, sa.id), match='다른 박스')
    c = arrive_store(inv, 'H01', 'S-02-2')
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(a.id, c.slot_id), match='넣을 수 없습니다')
    inv.block_slot('S-03-1', '파손')
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(a.id, 'S-03-1'), match='넣을 수 없습니다')
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(c.id), match='적재할 수 없는')     # 이미 STORED
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(a.id, 'N-12-1'))
    # 작업에 묶인 채 컨베이어에 있는 박스는 수동 확정 불가
    inv.reserve_slot(b.id, order_id='O9')
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(b.id), match='묶인')
    # 예약과 다른 빈 칸에 넣으면 원래 예약은 풀린다
    rec = Recorder()
    inv.subscribe(rec)
    stored = inv.confirm_store(a.id, 'S-08-1', robot='AMR-01')
    assert stored.id == 'S-08-1' and inv.slot(sa.id).state == SlotState.EMPTY
    assert rec.kinds == ['STORE'] and set(rec.calls[0][1]['slots']) == {'S-08-1', sa.id}
    assert [e.kind for e in inv.events(box_id=a.id, limit=2)] == ['STORE', 'UNRESERVE']
    inv.retrieve('S-08-1')
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(a.id), match='출고됨')
    assert inv.check_invariants() == []


def test_manual_store(inv):
    a = inv.register_arrival('PC9030')
    s = inv.manual_store(a.id)                              # 자동 추천 칸
    assert s.id == 'N-01-1' and inv.box(a.id).state == BoxState.STORED
    assert inv.events(limit=1)[0].detail.startswith('수동 적재')
    b = inv.register_arrival('PC9030')
    assert inv.manual_store(b.id, 'S-04-3').id == 'S-04-3'  # 지정 칸
    c = inv.register_arrival('NC3215')
    r = inv.reserve_slot(c.id)
    assert inv.manual_store(c.id).id == r.id                # 예약 칸으로
    d = inv.register_arrival('NC3215')
    r = inv.reserve_slot(d.id)
    assert inv.manual_store(d.id, 'S-10-4').id == 'S-10-4'  # 다른 칸 → 예약 해제
    assert inv.slot(r.id).state == SlotState.EMPTY
    e = inv.register_arrival('H01')
    assert_unchanged_on_error(inv, lambda: inv.manual_store(e.id, 'S-04-3'))                    # OCCUPIED
    inv.block_slot('S-06-1', '점검')
    assert_unchanged_on_error(inv, lambda: inv.manual_store(e.id, 'S-06-1'))                    # BLOCKED
    inv.reserve_slot(e.id, order_id='O1')
    assert_unchanged_on_error(inv, lambda: inv.manual_store(e.id), match='묶인')
    inv.mark_picked(e.id, 'AMR-01')
    assert_unchanged_on_error(inv, lambda: inv.manual_store(e.id), match='적재 완료 처리')
    assert_unchanged_on_error(inv, lambda: inv.manual_store(a.id))                               # STORED
    f = inv.register_arrival('H05')
    g = inv.register_arrival('H05')
    sg = inv.reserve_slot(g.id)
    assert_unchanged_on_error(inv, lambda: inv.manual_store(f.id, sg.id), match='다른 박스')
    assert inv.check_invariants() == []


# ================================================================ 확인 필요
def picked_box(inv, grade='PC9030', order='O1'):
    b = inv.register_arrival(grade)
    s = inv.reserve_slot(b.id, order_id=order)
    inv.mark_picked(b.id, 'AMR-01', order_id=order)
    return b, s


def test_needs_check_resolved_by_confirm_store(inv):
    b, s = picked_box(inv)
    inv.flag_needs_check(b.id, '작업 실패: 통신 끊김')
    box = inv.box(b.id)
    assert box.needs_check and box.state == BoxState.ON_AMR and box.slot_id == s.id
    assert inv.slot(s.id).state == SlotState.RESERVED       # 예약 유지
    assert inv.snapshot().needs_check == 1
    ev = inv.events(limit=1)[0]
    assert ev.kind == 'ERROR' and '통신 끊김' in ev.detail
    slot = inv.confirm_store(b.id)
    assert slot.id == s.id and slot.state == SlotState.OCCUPIED
    assert not inv.box(b.id).needs_check and inv.snapshot().needs_check == 0
    assert '확인 필요 해제' in inv.events(limit=1)[0].detail
    assert inv.check_invariants() == []


def test_needs_check_resolved_by_return_to_conveyor(inv):
    waiting = [inv.register_arrival('H01'), inv.register_arrival('H05')]
    b, s = picked_box(inv)
    assert_unchanged_on_error(inv, lambda: inv.return_to_conveyor(b.id), match='확인 필요로 표시된')
    inv.flag_needs_check(b.id, '작업 취소')
    rec = Recorder()
    inv.subscribe(rec)
    inv.return_to_conveyor(b.id, '사람이 컨베이어에 다시 올림')
    conv = inv.conveyor()
    assert [x.id for x in conv] == [b.id] + [w.id for w in waiting]     # 맨 앞
    assert conv[0].arrived_seq < conv[1].arrived_seq
    box = inv.box(b.id)
    assert box.state == BoxState.CONVEYOR and box.slot_id is None and box.order_id is None
    assert box.robot is None and not box.needs_check
    assert inv.slot(s.id).state == SlotState.EMPTY
    assert rec.kinds == ['CORRECT'] and s.id in rec.calls[0][1]['slots']
    assert [e.kind for e in inv.events(box_id=b.id, limit=2)] == ['CORRECT', 'UNRESERVE']
    # 다시 되돌린 박스가 맨 앞이니 다음 배차에서 먼저 나간다; 새 도착은 맨 뒤
    n = inv.register_arrival('NC3215')
    assert inv.conveyor()[-1].id == n.id and inv.conveyor()[0].id == b.id
    assert inv.check_invariants() == []


def test_return_to_conveyor_errors(make_inv):
    cfg = AppConfig()
    cfg.conveyor_capacity = 2
    inv = make_inv(cfg)
    b, _ = picked_box(inv)
    inv.flag_needs_check(b.id, '실패')
    inv.register_arrival('H01')
    inv.register_arrival('H01')
    assert_unchanged_on_error(inv, lambda: inv.return_to_conveyor(b.id), exc=ConveyorFull)
    c = inv.conveyor()[0]
    assert_unchanged_on_error(inv, lambda: inv.return_to_conveyor(c.id), match='AMR에 실린')
    inv.manual_store(c.id)
    inv.return_to_conveyor(b.id)
    assert inv.conveyor()[0].id == b.id
    assert_unchanged_on_error(inv, lambda: inv.return_to_conveyor('NOPE'))


def test_return_to_empty_conveyor_and_without_reservation(inv):
    b = inv.register_arrival('PC9030')
    inv.mark_picked(b.id, 'AMR-01')                          # 예약 없이 집은 박스
    inv.flag_needs_check(b.id, '위치 불명')
    inv.return_to_conveyor(b.id)
    assert [x.id for x in inv.conveyor()] == [b.id]
    assert inv.register_arrival('H01').arrived_seq > inv.box(b.id).arrived_seq
    assert inv.check_invariants() == []


def test_flag_needs_check_errors(inv):
    a = arrive_store(inv, 'PC9030')
    assert_unchanged_on_error(inv, lambda: inv.flag_needs_check(a.id, 'x'))
    assert_unchanged_on_error(inv, lambda: inv.flag_needs_check('NOPE', 'x'))
    c = inv.register_arrival('H01')
    inv.flag_needs_check(c.id, '')                           # 컨베이어 박스도 표시 가능
    assert inv.box(c.id).needs_check and '사유 없음' in inv.events(limit=1)[0].detail
    inv.manual_store(c.id)
    assert not inv.box(c.id).needs_check


# ================================================================ 출고·수정
def test_retrieve(inv, clock):
    a = arrive_store(inv, 'PC9030')
    clock.advance(100)
    rec = Recorder()
    inv.subscribe(rec)
    out = inv.retrieve(a.slot_id)
    assert out.state == BoxState.OUT and out.slot_id is None and out.out_at == '2026-10-20 09:01:40'
    assert inv.slot(a.slot_id).state == SlotState.EMPTY and inv.slot(a.slot_id).box_id is None
    ev = inv.events(limit=1)[0]
    assert ev.kind == 'OUT' and ev.slot_id == a.slot_id and ev.detail == '수동 출고'
    assert rec.kinds == ['OUT'] and rec.calls[0][1]['slot'] == a.slot_id
    assert_unchanged_on_error(inv, lambda: inv.retrieve(a.slot_id), match='박스가 없습니다')
    assert_unchanged_on_error(inv, lambda: inv.retrieve('X-01-1'))
    b = inv.register_arrival('H01')
    s = inv.reserve_slot(b.id)
    assert_unchanged_on_error(inv, lambda: inv.retrieve(s.id))
    assert inv.find_grade('PC9030') == []
    assert inv.check_invariants() == []


def test_move(inv, clock):
    a = arrive_store(inv, 'PC9030', 'N-01-1')
    stored_at = a.stored_at
    clock.advance(50)
    rec = Recorder()
    inv.subscribe(rec)
    inv.move(a.id, 'S-07-3', '실사: 위치 다름')
    box = inv.box(a.id)
    assert box.slot_id == 'S-07-3' and box.stored_at == stored_at   # 입고 시각(FIFO 기준)은 그대로
    assert inv.slot('N-01-1').state == SlotState.EMPTY
    assert inv.slot('S-07-3').state == SlotState.OCCUPIED and inv.slot('S-07-3').box_id == a.id
    assert rec.kinds == ['CORRECT'] and set(rec.calls[0][1]['slots']) == {'N-01-1', 'S-07-3'}
    assert len(inv.events(slot_id='N-01-1', kind='CORRECT')) == 1
    assert len(inv.events(slot_id='S-07-3', kind='CORRECT')) == 1
    assert '실사: 위치 다름' in inv.events(limit=1)[0].detail
    b = arrive_store(inv, 'H01', 'N-02-1')
    assert_unchanged_on_error(inv, lambda: inv.move(a.id, 'N-02-1'), match='비어 있지 않습니다')
    assert_unchanged_on_error(inv, lambda: inv.move(a.id, 'S-07-3'), match='같은 칸')
    inv.block_slot('N-03-1', '점검')
    assert_unchanged_on_error(inv, lambda: inv.move(a.id, 'N-03-1'))
    c = inv.register_arrival('NC3215')
    rs = inv.reserve_slot(c.id)
    assert_unchanged_on_error(inv, lambda: inv.move(b.id, rs.id))
    assert_unchanged_on_error(inv, lambda: inv.move(c.id, 'S-01-4'), match='적재대에 있는 박스만')
    assert_unchanged_on_error(inv, lambda: inv.move(a.id, 'S-11-1'))
    assert_unchanged_on_error(inv, lambda: inv.move('NOPE', 'S-01-4'))
    assert inv.check_invariants() == []


def test_put_directly(inv):
    rec = Recorder()
    inv.subscribe(rec)
    b = inv.put_directly('S-02-3', 'nc3215', 'L-REAL-1', 13.5)
    assert b.state == BoxState.STORED and b.slot_id == 'S-02-3' and b.grade == 'NC3215' and b.weight_kg == 13.5
    assert b.id == 'W261020-0001' and b.arrived_seq == 0 and b.note == '실사 등록'
    assert inv.slot('S-02-3').box_id == b.id
    ev = inv.events(limit=1)[0]
    assert ev.kind == 'CORRECT' and '직접 입력' in ev.detail and rec.kinds == ['CORRECT']
    assert_unchanged_on_error(inv, lambda: inv.put_directly('S-02-3', 'H01', 'L'), match='비어 있지 않습니다')
    assert_unchanged_on_error(inv, lambda: inv.put_directly('S-02-4', 'XX1', 'L'), match='등록되지 않은 재종')
    assert_unchanged_on_error(inv, lambda: inv.put_directly('S-02-4', 'H01', 'L', weight_kg=0))
    assert_unchanged_on_error(inv, lambda: inv.put_directly('S-02-4', 'H01', 'L', box_id=b.id))
    inv.set_grade_active('H05', False)                      # 실물 기준: 사용중지 재종도 등록 가능
    c = inv.put_directly('S-02-4', 'H05', '', box_id='REAL-77', reason='창고 실사')
    assert c.id == 'REAL-77' and c.lot == '0001015661578' and c.note == '창고 실사'
    assert inv.check_invariants() == []


def test_clear_slot(inv):
    a = arrive_store(inv, 'PC9030')
    inv.clear_slot(a.slot_id, '빈 박스 확인')
    assert inv.box(a.id).state == BoxState.OUT and inv.slot(a.slot_id).state == SlotState.EMPTY
    ev = inv.events(limit=1)[0]
    assert ev.kind == 'CORRECT' and '비우기: 빈 박스 확인' in ev.detail and ev.slot_id == a.slot_id
    assert_unchanged_on_error(inv, lambda: inv.clear_slot(a.slot_id, 'x'), match='박스가 없습니다')
    b = inv.register_arrival('H01')
    s = inv.reserve_slot(b.id)
    assert_unchanged_on_error(inv, lambda: inv.clear_slot(s.id, 'x'))
    inv.block_slot('N-10-4', '파손')
    assert_unchanged_on_error(inv, lambda: inv.clear_slot('N-10-4', 'x'))


def test_block_and_unblock(inv):
    rec = Recorder()
    inv.subscribe(rec)
    inv.block_slot('N-01-1', '선반 파손')
    s = inv.slot('N-01-1')
    assert s.state == SlotState.BLOCKED and s.blocked_reason == '선반 파손'
    assert inv.snapshot().blocked == 1
    assert_unchanged_on_error(inv, lambda: inv.block_slot('N-01-1', '다시'), match='빈 칸만')
    a = inv.register_arrival('PC9030')
    assert inv.reserve_slot(a.id).id != 'N-01-1'            # 배정 제외
    assert_unchanged_on_error(inv, lambda: inv.block_slot(inv.box(a.id).slot_id, 'x'))   # RESERVED
    b = arrive_store(inv, 'H01', 'S-05-1')
    assert_unchanged_on_error(inv, lambda: inv.block_slot(b.slot_id, 'x'), match='빈 칸만')  # OCCUPIED
    assert_unchanged_on_error(inv, lambda: inv.unblock_slot('S-09-1'), match='사용중지된 칸이 아닙니다')
    assert_unchanged_on_error(inv, lambda: inv.unblock_slot(b.slot_id))
    inv.unblock_slot('N-01-1')
    s = inv.slot('N-01-1')
    assert s.state == SlotState.EMPTY and s.blocked_reason == ''
    assert rec.kinds[0] == 'BLOCK' and rec.kinds[-1] == 'UNBLOCK'
    assert [e.kind for e in inv.events(slot_id='N-01-1')][:2] == ['UNBLOCK', 'BLOCK']
    assert '선반 파손' in inv.events(kind='UNBLOCK')[0].detail


# ================================================================ 출고 조회 (FIFO)
def test_find_grade_fifo_across_seconds(inv, clock):
    first = arrive_store(inv, 'PC9030', 'S-10-4')
    clock.advance(10)
    second = arrive_store(inv, 'PC9030', 'N-01-1')
    clock.advance(10)
    arrive_store(inv, 'NC3215', 'N-02-1')
    third = arrive_store(inv, 'PC9030', 'N-05-2')
    clock.advance(40)
    rows = inv.find_grade('PC9030')
    assert [r.box.id for r in rows] == [first.id, second.id, third.id]
    assert [r.slot.id for r in rows] == ['S-10-4', 'N-01-1', 'N-05-2']
    assert [r.age_s for r in rows] == pytest.approx([60, 50, 40])
    assert all(r.slot.box_id == r.box.id and r.box.state == BoxState.STORED for r in rows)
    assert [r.box.id for r in inv.find_grade('pc9030')] == [first.id, second.id, third.id]
    assert inv.find_grade('H01') == [] and inv.find_grade('') == []


def test_find_grade_fifo_within_the_same_second(inv):
    # 시계가 멈춰 있어도(같은 초) 적재 순서대로 — 슬롯 ID 순서와 반대로 넣어 확인
    order = ['S-10-4', 'S-03-1', 'N-07-2', 'N-01-1']
    boxes = [arrive_store(inv, 'H01', sid) for sid in order]
    assert len({b.stored_at for b in boxes}) == 1
    rows = inv.find_grade('H01')
    assert [r.slot.id for r in rows] == order
    # 이동해도 입고 순서는 유지
    inv.move(boxes[0].id, 'N-01-2')
    assert [r.box.id for r in inv.find_grade('H01')] == [b.id for b in boxes]


def test_find_grade_after_retrieve_keeps_fifo(inv, clock):
    boxes = []
    for i in range(4):
        boxes.append(arrive_store(inv, 'PC5400'))
        clock.advance(1)
    inv.retrieve(inv.find_grade('PC5400')[0].slot.id)       # 가장 오래된 것부터 출고
    assert [r.box.id for r in inv.find_grade('PC5400')] == [b.id for b in boxes[1:]]


# ================================================================ 이벤트·CSV
def test_events_filters_and_order(inv):
    a = arrive_store(inv, 'PC9030', 'N-01-1')
    b = arrive_store(inv, 'H01', 'N-02-1')
    inv.block_slot('S-01-1', '점검')
    all_ev = inv.events(limit=0)
    assert [e.id for e in all_ev] == sorted((e.id for e in all_ev), reverse=True)     # 최신순
    assert len(inv.events(limit=2)) == 2 and inv.events(limit=2) == all_ev[:2]
    assert {e.kind for e in inv.events(kind='STORE')} == {'STORE'} and len(inv.events(kind='store')) == 2
    assert {e.box_id for e in inv.events(box_id=a.id)} == {a.id}
    assert {e.grade for e in inv.events(grade='h01')} == {'H01'}
    assert [e.kind for e in inv.events(slot_id='s-01-1')] == ['BLOCK']
    assert len(inv.events(kind='STORE', grade='H01', box_id=b.id, slot_id='N-02-1')) == 1
    assert inv.events(slot_id='garbage') == []


def test_export_events_csv(inv, tmp_path):
    arrive_store(inv, 'PC9030')
    inv.block_slot('S-01-1', '선반 파손')
    path = tmp_path / 'out' / '이력.csv'
    n = inv.export_events_csv(path)
    raw = path.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')                   # UTF-8-SIG (엑셀 한글)
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    assert rows[0][:3] == ['번호', '시각', '종류'] and '내용' in rows[0]
    assert n == len(rows) - 1 == event_count(inv)
    detail_col = rows[0].index('내용')
    assert any('선반 파손' in r[detail_col] for r in rows[1:])
    assert any(r[rows[0].index('종류명')] == '사용중지' for r in rows[1:])
    assert [int(r[0]) for r in rows[1:]] == sorted(int(r[0]) for r in rows[1:])   # 오래된 것부터


def test_export_events_csv_error_is_inventory_error(inv, tmp_path):
    folder = tmp_path / 'is_a_dir.csv'
    folder.mkdir()
    with pytest.raises(InventoryError, match='CSV'):
        inv.export_events_csv(folder)


# ================================================================ 구독
def test_subscribers_notified_once_per_success_and_not_on_failure(inv):
    rec = Recorder()
    inv.subscribe(rec)
    a = inv.register_arrival('PC9030')
    with pytest.raises(InventoryError):
        inv.register_arrival('XX')
    with pytest.raises(InventoryError):
        inv.retrieve('N-01-1')
    inv.reserve_slot(a.id)
    assert rec.kinds == ['ARRIVE', 'RESERVE']
    payload = rec.calls[1][1]
    assert payload['box'] == a.id and payload['slot'] == 'N-01-1' and payload['slots'] == ['N-01-1']
    assert payload['grade'] == 'PC9030' and payload['events']


def test_bad_subscriber_does_not_break_mutation(inv):
    def boom(kind, payload):
        raise RuntimeError('구독자 오류')

    rec = Recorder()
    inv.subscribe(boom)
    inv.subscribe(rec)
    b = inv.register_arrival('PC9030')
    assert inv.box(b.id).state == BoxState.CONVEYOR and rec.kinds == ['ARRIVE']
    inv.unsubscribe(boom)
    inv.unsubscribe(boom)    # 없는 구독 해제는 무시


def test_rollback_when_failure_happens_mid_transaction(inv, monkeypatch):
    """적재 도중(칸은 이미 OCCUPIED 로 바꾼 뒤) 오류가 나도 전부 되돌아간다."""
    b = inv.register_arrival('PC9030')
    s = inv.reserve_slot(b.id)
    inv.mark_picked(b.id, 'AMR-01')
    real = inv._db.next_seq

    def failing(key):
        if key == 'stored_seq':
            raise RuntimeError('디스크 오류 흉내')
        return real(key)

    monkeypatch.setattr(inv._db, 'next_seq', failing)
    assert_unchanged_on_error(inv, lambda: inv.confirm_store(b.id), exc=RuntimeError)
    assert inv.slot(s.id).state == SlotState.RESERVED and inv.box(b.id).state == BoxState.ON_AMR
    monkeypatch.setattr(inv._db, 'next_seq', real)
    inv.confirm_store(b.id)
    assert inv.check_invariants() == []


# ================================================================ 기준정보·설정
def test_upsert_grade_add_update_and_validation(inv):
    rec = Recorder()
    inv.subscribe(rec)
    inv.upsert_grade(Grade('nc5340', 'm', 'M25–M40', 'CVD', '스테인리스 황삭 (시험)', '#aa8800'))
    g = inv.grade('NC5340')
    assert g.code == 'NC5340' and g.iso_class == 'M' and g.active
    assert inv.grades()[-1].code == 'NC5340'                 # 새 재종은 맨 뒤
    inv.upsert_grade(Grade('NC5340', 'M', 'M25–M40', 'CVD', '설명 수정', '#bb9900', active=False))
    g = inv.grade('NC5340')
    assert g.description == '설명 수정' and g.color == '#bb9900' and not g.active
    assert len(inv.grades()) == len(SAMPLE_GRADES) + 1
    assert rec.kinds == ['MASTER', 'MASTER']
    assert [e.detail.split(':')[0] for e in inv.events(kind='MASTER')] == ['재종 수정', '재종 추가']
    for bad in (Grade('', 'P', '', '', '', '#000000'), Grade('A B', 'P', '', '', '', '#000000'),
                Grade('X1', 'Q', '', '', '', '#000000'), Grade('X1', 'P', '', '', '', 'red'),
                Grade('X1', 'P', '', '', '', '#12345')):
        assert_unchanged_on_error(inv, lambda bad=bad: inv.upsert_grade(bad))
    assert_unchanged_on_error(inv, lambda: inv.upsert_grade('PC9030'))  # type: ignore[arg-type]


def test_set_grade_active(inv):
    inv.set_grade_active('h01', False)
    assert not inv.grade('H01').active
    assert 'H01' not in {g.code for g in inv.grades(active_only=True)}
    assert len(inv.grades()) == len(SAMPLE_GRADES)
    inv.set_grade_active('H01', True)
    assert inv.grade('H01').active
    assert_unchanged_on_error(inv, lambda: inv.set_grade_active('NOPE', True))


def test_set_policy(inv, make_inv):
    for policy in POLICIES:
        inv.set_policy(policy)
        assert inv.policy == policy and inv._config.allocation_policy == policy
    assert inv.events(limit=1)[0].kind == 'CONFIG'
    assert_unchanged_on_error(inv, lambda: inv.set_policy('random'), match='배정 정책')
    inv.set_policy('lowest_first')
    a, b = inv.register_arrival('PC9030'), inv.register_arrival('PC9030')
    assert [inv.reserve_slot(a.id).id, inv.reserve_slot(b.id).id] == ['N-01-1', 'S-01-1']


def test_initial_policy_comes_from_config(make_inv):
    cfg = AppConfig()
    cfg.allocation_policy = 'balanced'
    assert make_inv(cfg).policy == 'balanced'
    cfg2 = AppConfig()
    cfg2.allocation_policy = 'nonsense'
    assert make_inv(cfg2, name='other.db').policy == 'grade_zone'


def test_reconfigure_rack_allowed_when_empty(inv, make_inv):
    a = arrive_store(inv, 'PC9030')
    inv.retrieve(a.slot_id)                                  # 출고된 박스(이력)는 있어도 된다
    inv.register_arrival('H01')                              # 목적지 없는 컨베이어 박스도 괜찮다
    rec = Recorder()
    inv.subscribe(rec)
    inv.reconfigure_rack(3, 2)
    assert inv.layout.capacity == 12 and inv.layout.columns == 3 and inv.layout.tiers == 2
    assert [s.id for s in inv.slots()] == inv.layout.slot_ids()
    assert inv.slots()[-1].id == 'S-03-2' and inv.snapshot().capacity == 12
    assert inv._config.rack.columns == 3 and inv._config.rack.tiers == 2
    assert rec.kinds == ['CONFIG'] and rec.calls[0][1]['reconfigured']
    assert '3열×2단' in inv.events(limit=1)[0].detail
    with pytest.raises(InventoryError):
        inv.slot('N-04-1')
    assert inv.check_invariants() == []


@pytest.mark.parametrize('setup', ['occupied', 'reserved', 'blocked'])
def test_reconfigure_rack_refused(inv, setup):
    if setup == 'occupied':
        arrive_store(inv, 'PC9030')
    elif setup == 'reserved':
        inv.reserve_slot(inv.register_arrival('PC9030').id)
    else:
        inv.block_slot('S-04-4', '점검')
    assert_unchanged_on_error(inv, lambda: inv.reconfigure_rack(5, 3), match='비어 있을 때만')
    assert inv.layout.capacity == 80


@pytest.mark.parametrize('cols, tiers', [(0, 4), (10, 0), (100, 4), (10, 10), ('5', 4), (2.5, 2)])
def test_reconfigure_rack_invalid_dimensions(inv, cols, tiers):
    assert_unchanged_on_error(inv, lambda: inv.reconfigure_rack(cols, tiers))


# ================================================================ 영속성
def full_state(inv: InventoryService):
    return (inv.slots(), inv.boxes(), inv.events(limit=0), inv.grades(), inv.next_box_id(), inv.policy,
            inv.layout.slot_ids(), [r.box.id for r in inv.find_grade('PC9030')])


def test_persistence_close_and_reopen(tmp_path, clock):
    path = tmp_path / 'rack.db'
    inv = InventoryService(path, AppConfig(), clock=clock)
    a = arrive_store(inv, 'PC9030')
    clock.advance(3)
    b = arrive_store(inv, 'PC9030')
    c, _ = picked_box(inv, 'NC3215')
    inv.flag_needs_check(c.id, '재시작 전 미완료')
    inv.register_arrival('H01')
    inv.block_slot('S-10-4', '파손')
    inv.retrieve(a.slot_id)
    inv.upsert_grade(Grade('ZX100', 'H', 'H10–H20', 'PVD', '시험 재종', '#123456'))
    inv.set_policy('balanced')
    before = full_state(inv)
    n_events = event_count(inv)
    inv.close()

    other = AppConfig()
    other.rack.columns, other.rack.tiers = 5, 2              # 설정이 달라도 DB 쪽을 따른다
    other.allocation_policy = 'lowest_first'
    again = InventoryService(path, other, clock=clock)
    try:
        assert full_state(again) == before
        assert event_count(again) == n_events
        assert again.layout.capacity == 80 and other.rack.columns == 10 and other.rack.tiers == 4
        assert again.policy == 'balanced' and other.allocation_policy == 'balanced'
        assert again.box(c.id).needs_check
        assert again.check_invariants() == []
        # 순번 카운터도 이어진다
        d = again.register_arrival('PC9030')
        assert d.arrived_seq > max(x.arrived_seq for x in before[1])
        auto = [int(x.lot) for x in before[1] if x.lot.isdigit() and len(x.lot) == 13]
        assert d.lot == str(max(auto) + 1).zfill(13) and d.id not in {x.id for x in before[1]}
        assert [r.box.id for r in again.find_grade('PC9030')] == [b.id]
    finally:
        again.close()


def test_reconfigure_survives_reopen(tmp_path, clock):
    path = tmp_path / 'rack.db'
    inv = InventoryService(path, AppConfig(), clock=clock)
    inv.reconfigure_rack(6, 3)
    inv.close()
    again = InventoryService(path, AppConfig(), clock=clock)
    try:
        assert again.layout.capacity == 36 and len(again.slots()) == 36 and again.check_invariants() == []
    finally:
        again.close()


def test_unsupported_schema_version(tmp_path, clock):
    path = tmp_path / 'rack.db'
    inv = InventoryService(path, AppConfig(), clock=clock)
    inv._db.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    inv.close()
    with pytest.raises(InventoryError, match='버전'):
        InventoryService(path, AppConfig(), clock=clock)


def test_not_a_database_file(tmp_path, clock):
    path = tmp_path / 'rack.db'
    path.write_bytes(b'this is not sqlite' * 100)
    with pytest.raises(InventoryError, match='열 수 없습니다'):
        InventoryService(path, AppConfig(), clock=clock)


# ================================================================ 불변식 검사기 자체
def test_check_invariants_detects_corruption(inv):
    a = arrive_store(inv, 'PC9030', 'N-01-1')
    b = inv.register_arrival('H01')
    inv.reserve_slot(b.id, 'N-02-1')
    conn = inv._db.conn
    conn.execute("UPDATE slots SET state = 'OCCUPIED' WHERE id = 'N-05-1'")                 # 박스 없는 적재 칸
    conn.execute("UPDATE boxes SET slot_id = 'S-01-1' WHERE id = ?", (a.id,))              # 박스 쪽 칸 불일치
    conn.execute("UPDATE boxes SET state = 'ON_AMR' WHERE id = ?", (b.id,))                # AMR 이름 없음
    problems = inv.check_invariants()
    text = '\n'.join(problems)
    assert len(problems) >= 4
    assert 'N-05-1' in text and a.id in text and b.id in text
    assert all(any('가' <= ch <= '힣' for ch in p) for p in problems)               # 한국어 설명


def test_check_invariants_detects_conveyor_order_and_missing_events(inv):
    a, b = inv.register_arrival('PC9030'), inv.register_arrival('PC9030')
    conn = inv._db.conn
    conn.execute('UPDATE boxes SET arrived_seq = 1 WHERE id IN (?, ?)', (a.id, b.id))
    conn.execute('DELETE FROM events WHERE box_id = ?', (a.id,))
    conn.execute("DELETE FROM slots WHERE id = 'S-10-4'")
    text = '\n'.join(inv.check_invariants())
    assert '도착 순번' in text and '이벤트' in text and 'S-10-4' in text


# ================================================================ 무작위 시험 (fuzz)
FUZZ_GRADES = ['PC9030', 'NC3215', 'NC3020', 'H01', 'PC5400', 'PC9030', 'NC3215', 'XX999']


def _fuzz_ops(inv: InventoryService, rng: random.Random, clock: FakeClock):
    """(이름, 실행 함수) 하나를 고른다. 유효·무효 요청이 섞인다."""
    boxes = inv.boxes()
    live = [b for b in boxes if b.state != BoxState.OUT]
    by_state = {s: [b for b in boxes if b.state == s] for s in BoxState}
    slots = inv.slots()
    by_slot_state = {s: [x.id for x in slots if x.state == s] for s in SlotState}
    sids = [x.id for x in slots]

    def any_box(pref: list | None = None) -> str:
        r = rng.random()
        if pref and r < 0.75:
            return rng.choice(pref).id
        if live and r < 0.92:
            return rng.choice(live).id
        if boxes and r < 0.97:
            return rng.choice(boxes).id
        return rng.choice(['NOPE-1', '', 'W000000-0000'])

    def any_slot(pref: list[str] | None = None) -> str:
        r = rng.random()
        if pref and r < 0.7:
            return rng.choice(pref)
        if r < 0.97:
            return rng.choice(sids)
        return rng.choice(['N-99-1', 'garbage', 'S-01-9'])

    def maybe(p: float, value):
        return value if rng.random() < p else None

    grade = rng.choice(FUZZ_GRADES)
    order = rng.choice([None, None, 'O1', 'O2'])
    ops = {
        'arrive': (14, lambda: inv.register_arrival(
            grade, lot=rng.choice([None, None, 'L-X', '']),
            weight_kg=rng.choice([15.0] * 7 + [12.3, 0, 500]),
            box_id=rng.choice([None] * 8 + [any_box(), 'HAND-' + str(rng.randint(1, 30))]))),
        'reserve': (10, lambda: inv.reserve_slot(
            any_box(by_state[BoxState.CONVEYOR]), slot_id=maybe(0.3, any_slot(by_slot_state[SlotState.EMPTY])),
            order_id=order, exclude=maybe(0.3, set(rng.sample(sids, min(4, len(sids))))))),
        'release': (4, lambda: inv.release_reservation(any_box(by_state[BoxState.CONVEYOR]), '취소')),
        'pick': (8, lambda: inv.mark_picked(any_box(by_state[BoxState.CONVEYOR]), rng.choice(['AMR-01', 'AMR-02', '']),
                                            order_id=order)),
        'confirm': (8, lambda: inv.confirm_store(any_box(by_state[BoxState.ON_AMR] or by_state[BoxState.CONVEYOR]),
                                                 slot_id=maybe(0.3, any_slot(by_slot_state[SlotState.EMPTY])),
                                                 robot=maybe(0.5, 'AMR-01'))),
        'manual': (7, lambda: inv.manual_store(any_box(by_state[BoxState.CONVEYOR]),
                                               maybe(0.4, any_slot(by_slot_state[SlotState.EMPTY])))),
        'flag': (4, lambda: inv.flag_needs_check(any_box(by_state[BoxState.ON_AMR]), '작업 실패')),
        'return': (4, lambda: inv.return_to_conveyor(
            any_box([b for b in by_state[BoxState.ON_AMR] if b.needs_check] or by_state[BoxState.ON_AMR]), '되돌림')),
        'retrieve': (5, lambda: inv.retrieve(any_slot(by_slot_state[SlotState.OCCUPIED]))),
        'move': (4, lambda: inv.move(any_box(by_state[BoxState.STORED]), any_slot(by_slot_state[SlotState.EMPTY]))),
        'put': (3, lambda: inv.put_directly(any_slot(by_slot_state[SlotState.EMPTY]), grade, rng.choice(['L-P', '']),
                                            box_id=rng.choice([None, None, any_box()]))),
        'clear': (3, lambda: inv.clear_slot(any_slot(by_slot_state[SlotState.OCCUPIED]), '실사')),
        'block': (2, lambda: inv.block_slot(any_slot(by_slot_state[SlotState.EMPTY]), '점검')),
        'unblock': (4, lambda: inv.unblock_slot(any_slot(by_slot_state[SlotState.BLOCKED]))),
        'grade_active': (2, lambda: inv.set_grade_active(rng.choice(FUZZ_GRADES), rng.random() < 0.85)),
        'policy': (2, lambda: inv.set_policy(rng.choice(list(POLICIES) + ['bogus']))),
        'reconfigure': (1, lambda: inv.reconfigure_rack(*rng.choice([(4, 2), (3, 3), (0, 2)]))),
        'upsert': (1, lambda: inv.upsert_grade(Grade(rng.choice(['ZZ1', 'H01']), rng.choice(['P', 'N', 'Q']), 'r', 'k',
                                                     'd', rng.choice(['#112233', 'bad'])))),
    }
    names = list(ops)
    name = rng.choices(names, weights=[ops[n][0] for n in names])[0]
    return name, ops[name][1]


@pytest.mark.parametrize('seed, n_ops, columns, tiers', [(20261020, 3000, 4, 2), (7, 1500, 10, 4)])
def test_fuzz_random_operations_keep_invariants(tmp_path, seed, n_ops, columns, tiers):
    clock = FakeClock()
    cfg = AppConfig()
    cfg.rack = RackConfig(columns=columns, tiers=tiers)
    inv = InventoryService(tmp_path / f'fuzz{seed}.db', cfg, clock=clock)
    rec = Recorder()
    inv.subscribe(rec)
    rng = random.Random(seed)
    ok: dict[str, int] = {}
    failed: dict[str, int] = {}
    errors: dict[str, int] = {}
    try:
        for i in range(n_ops):
            name, op = _fuzz_ops(inv, rng, clock)
            before = digest(inv)
            n_ev = event_count(inv)
            rec.calls.clear()
            try:
                op()
            except InventoryError as e:
                failed[name] = failed.get(name, 0) + 1
                errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
                assert digest(inv) == before, f'#{i} {name}: 실패한 요청이 상태를 바꿨습니다'
                assert rec.calls == [], f'#{i} {name}: 실패했는데 알림'
                assert event_count(inv) == n_ev
            else:
                ok[name] = ok.get(name, 0) + 1
                assert len(rec.calls) == 1, f'#{i} {name}: 알림 {len(rec.calls)}회'
                assert event_count(inv) > n_ev, f'#{i} {name}: 이벤트 없음'
            problems = inv.check_invariants()
            assert problems == [], f'#{i} {name}: {problems}'
            clock.advance(rng.choice([0, 0, 0, 0.3, 1, 2, 7, 60, 3600 * 6]))
        # 거의 모든 동작이 성공·실패 양쪽으로 여러 번 실행되었는지
        assert sum(ok.values()) > n_ops * 0.3 and sum(failed.values()) > n_ops * 0.15
        for name in ('arrive', 'reserve', 'release', 'pick', 'confirm', 'manual', 'flag', 'return', 'retrieve',
                     'move', 'put', 'clear', 'block', 'unblock', 'grade_active', 'policy'):
            assert ok.get(name, 0) > 0, f'{name} 성공 0회 (ok={ok})'
        for name in ('arrive', 'reserve', 'pick', 'confirm', 'manual', 'retrieve', 'move', 'block'):
            assert failed.get(name, 0) > 0, f'{name} 실패 0회 (failed={failed})'
        assert errors.get('ConveyorFull', 0) > 0, errors          # 컨베이어 만재까지 가 봤는지
        if inv.layout.capacity <= 16:
            assert errors.get('RackFull', 0) > 0, errors          # 작은 적재대는 가득 차 봤는지
        # 마지막으로 재시작해도 같은 상태
        snap = (inv.slots(), inv.boxes(), event_count(inv))
        inv.close()
        again = InventoryService(tmp_path / f'fuzz{seed}.db', AppConfig(), clock=clock)
        try:
            assert (again.slots(), again.boxes(), event_count(again)) == snap
            assert again.check_invariants() == []
        finally:
            again.close()
    finally:
        inv.close()
