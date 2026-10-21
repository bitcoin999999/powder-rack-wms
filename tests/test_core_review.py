"""core 패키지 적대적 검토 — 결함 재현 테스트 + 스트레스 회귀 테스트.

앞부분(결함 재현)은 수정 전 구현에서 실패하던 것들이다. 뒷부분(스트레스)은 영속성·결정성·경계 조건을
구현자 테스트와 다른 방식으로 한 번 더 두드린다.
"""
from __future__ import annotations

import csv
import hashlib
import random
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from core.config import AppConfig, RackConfig, SimConfig
from core.conveyor import OverbridgeSimulator
from core.inventory import ConveyorFull, InventoryError, InventoryService, RackFull, parse_scan
from core.models import Box, BoxState, Slot, SlotState
from core.rack import POLICIES, RackLayout, SlotAllocator

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


def small_cfg(columns: int, tiers: int) -> AppConfig:
    cfg = AppConfig()
    cfg.rack = RackConfig(columns=columns, tiers=tiers)
    return cfg


def digest(inv: InventoryService) -> str:
    conn = inv._db.conn
    parts = [repr([tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY 1')])
             for t in ('slots', 'boxes', 'grades', 'meta', 'events')]
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


# ===================================================================================== 결함 1
# 박스 ID 대소문자: 스캔(parse_scan)은 대문자로 바꾸는데 직접 입력은 그대로 저장되어,
# 같은 실물 박스가 'bx-7' / 'BX-7' 두 건으로 등록되고 소문자로는 찾지 못했다.
def test_box_ids_are_case_insensitive_and_stored_upper(inv):
    a = inv.register_arrival('PC9030', box_id='bx-7')
    assert a.id == 'BX-7'                                         # 스캔 경로와 같은 표기
    assert parse_scan('BOX:bx-7;PC9030', {'PC9030'}).box_id == a.id
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('PC9030', box_id='BX-7'), match='이미 등록된')
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('PC9030', box_id='Bx-7'), match='이미 등록된')
    assert inv.box('bx-7').id == 'BX-7' and inv.box(' bx-7 ').id == 'BX-7'
    inv.manual_store('bx-7', 'N-01-1')                            # 변경 메서드도 소문자 입력을 받는다
    assert inv.slot('N-01-1').box_id == 'BX-7'
    assert {e.kind for e in inv.events(box_id='bx-7')} == {'ARRIVE', 'STORE'}
    c = inv.put_directly('S-02-1', 'H01', 'L1', box_id='rx-1')
    assert c.id == 'RX-1'
    assert_unchanged_on_error(inv, lambda: inv.put_directly('S-03-1', 'H01', 'L1', box_id='RX-1'), match='이미 등록된')
    assert inv.check_invariants() == []


def test_auto_box_id_skips_manual_id_typed_in_lower_case(inv):
    inv.register_arrival('PC9030', box_id='w261020-0001')
    nxt = inv.next_box_id()
    assert nxt == 'W261020-0002'
    assert inv.register_arrival('PC9030').id == nxt
    ids = [b.id.upper() for b in inv.boxes()]
    assert len(ids) == len(set(ids))                              # 대소문자만 다른 박스 ID 가 없다


def test_legacy_mixed_case_id_in_old_db_still_found_and_blocks_twins(inv):
    """수정 전 DB 에 소문자로 남아 있는 박스도 그대로 찾고, 대문자 쌍둥이 등록은 막는다."""
    b = inv.register_arrival('PC9030', box_id='OLD-1')
    conn = inv._db.conn
    conn.execute("UPDATE boxes SET id = 'old-1' WHERE id = ?", (b.id,))   # 옛 표기 흉내
    conn.execute("UPDATE events SET box_id = 'old-1' WHERE box_id = ?", (b.id,))
    assert inv.box('old-1').id == 'old-1' and inv.box('OLD-1').id == 'old-1'
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('PC9030', box_id='OLD-1'), match='이미 등록된')
    inv.manual_store('OLD-1', 'N-01-1')
    assert inv.box('old-1').state == BoxState.STORED
    assert len(inv.events(box_id='OLD-1')) == 2


# ===================================================================================== 결함 2
# 변경 중 sqlite3.OperationalError 만 InventoryError 로 바꾸고 DatabaseError(파일 손상)·IntegrityError 등은
# 날것으로 새어 나갔다. Controller 는 InventoryError 만 화면 알림으로 돌리므로 Qt 틱 루프가 죽는다.
@pytest.mark.parametrize('exc', [sqlite3.DatabaseError('database disk image is malformed'),
                                 sqlite3.IntegrityError('UNIQUE constraint failed: slots.box_id'),
                                 sqlite3.OperationalError('disk I/O error')])
def test_any_sqlite_error_inside_a_mutation_becomes_inventory_error(inv, exc):
    inv.register_arrival('PC9030')

    def boom(key: str) -> int:
        raise exc

    inv._db.next_seq = boom                                       # 트랜잭션 중간에서 실패
    try:
        assert_unchanged_on_error(inv, lambda: inv.register_arrival('H01'), match='재고 DB 오류')
    finally:
        del inv._db.next_seq
    assert not inv._db.conn.in_transaction
    assert inv.register_arrival('H01').grade == 'H01'             # 이후 정상 동작
    assert inv.check_invariants() == []


def test_db_folder_that_cannot_be_created_is_inventory_error(tmp_path, clock):
    blocker = tmp_path / 'data'
    blocker.write_text('파일이 폴더 자리를 차지')                    # data 가 폴더가 아니라 파일
    with pytest.raises(InventoryError, match='열 수 없습니다'):
        InventoryService(blocker / 'rack.db', AppConfig(), clock=clock)


# ===================================================================================== 결함 3
# CSV 를 엑셀로 열면 '=' '+' '-' '@' 로 시작하는 칸이 수식으로 실행된다 (박스 ID·사유는 사용자 입력).
def read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')                         # 엑셀 한글용 BOM
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.reader(f))


def test_csv_export_neutralises_formula_cells(inv, tmp_path):
    a = inv.register_arrival('PC9030', box_id='=1+2')
    inv.manual_store(a.id, 'N-01-1')
    inv.retrieve('N-01-1', reason='=HYPERLINK("http://x","클릭")')
    b = inv.register_arrival('H01', box_id='@SUM(1)')
    c = inv.register_arrival('H01', box_id='+1')
    path = tmp_path / '이력 내보내기' / 'events.csv'
    n = inv.export_events_csv(path)
    rows = read_csv(path)
    assert len(rows) == n + 1
    cells = [cell for row in rows[1:] for cell in row]
    assert not any(cell[:1] in ('=', '+', '-', '@', '\t', '\r') for cell in cells), cells
    assert "'=1+2" in cells and "'@SUM(1)" in cells and "'+1" in cells
    assert '\'=HYPERLINK("http://x","클릭")' in cells
    # 평범한 값은 그대로
    assert 'PC9030' in cells and 'N-01-1' in cells and b.id != c.id


# ===================================================================================== 결함 4
# WAL + synchronous=NORMAL 은 정전 시 마지막 커밋(적재·출고 기록)을 잃을 수 있다. 실장비용은 FULL.
def test_db_commits_are_durable_synchronous_full(inv):
    assert inv._db.conn.execute('PRAGMA synchronous').fetchone()[0] == 2


# ===================================================================================== 결함 5
# 문서: "출고할 재종이 하나도 없으면 … 기다린다 (라인 정지 표시는 하지 않음)". 그런데 만재로 정지 표시가 켜진 뒤
# 재종을 비우면 컨베이어가 비어도 '라인 정지' 표시가 계속 남았다.
def test_line_stopped_flag_cleared_when_waiting_for_grades():
    sim = OverbridgeSimulator(SimConfig(), ['PC9030'])
    sim.start(5)
    assert sim.tick(95, lambda: False) == [] and sim.line_stopped
    sim.set_grades([])
    assert sim.tick(1, lambda: True) == []
    assert not sim.line_stopped
    sim.set_grades(['PC9030'])
    assert len(sim.tick(0.1, lambda: True)) == 1 and not sim.line_stopped


# ===================================================================================== 결함 6
# 로트(추적 키)가 60자를 넘으면 조용히 잘려 저장됐다 — 스캔 경로는 거부하는데 직접 입력은 잘라 버림.
def test_long_lot_rejected_instead_of_silently_truncated(inv):
    lot = 'L' * 61
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('PC9030', lot=lot), match='로트')
    assert_unchanged_on_error(inv, lambda: inv.put_directly('N-01-1', 'PC9030', lot), match='로트')
    ok = inv.register_arrival('PC9030', lot='L' * 60)
    assert ok.lot == 'L' * 60
    assert inv.register_arrival('PC9030', lot='  L 2610  x ').lot == 'L 2610 x'     # 공백 정리는 그대로


# ===================================================================================== 스트레스: 영속성
REVIEW_GRADES = ['PC9030', 'NC3215', 'H01', 'PC5400', 'pc9030', 'XX1']


def _review_op(inv: InventoryService, rng: random.Random):
    boxes = inv.boxes()
    by = {s: [b.id for b in boxes if b.state == s] for s in BoxState}
    slots = inv.slots()
    sst = {s: [x.id for x in slots if x.state == s] for s in SlotState}
    sids = [x.id for x in slots]

    def pick(pool: list[str], junk: str = 'NOPE') -> str:
        if pool and rng.random() < 0.85:
            v = rng.choice(pool)
            return rng.choice([v, v, v.lower(), f'  {v} '])          # 대소문자·공백 섞인 입력
        return rng.choice([junk, '', rng.choice(sids)])

    grade = rng.choice(REVIEW_GRADES)
    ops = [
        (12, 'arrive', lambda: inv.register_arrival(grade, lot=rng.choice([None, '', 'L-1']),
                                                     box_id=rng.choice([None] * 6 + [f'm-{rng.randint(1, 20)}']))),
        (8, 'reserve', lambda: inv.reserve_slot(pick(by[BoxState.CONVEYOR]), order_id=rng.choice([None, 'O1']),
                                                exclude=set(rng.sample(sids, min(2, len(sids)))))),
        (6, 'pick', lambda: inv.mark_picked(pick(by[BoxState.CONVEYOR]), 'AMR-01', rng.choice([None, 'O1']))),
        (6, 'confirm', lambda: inv.confirm_store(pick(by[BoxState.ON_AMR]))),
        (6, 'manual', lambda: inv.manual_store(pick(by[BoxState.CONVEYOR]))),
        (3, 'release', lambda: inv.release_reservation(pick(by[BoxState.CONVEYOR]))),
        (3, 'flag', lambda: inv.flag_needs_check(pick(by[BoxState.ON_AMR]), '중단')),
        (3, 'return', lambda: inv.return_to_conveyor(pick(by[BoxState.ON_AMR]))),
        (5, 'retrieve', lambda: inv.retrieve(pick(sst[SlotState.OCCUPIED]).lower())),
        (3, 'move', lambda: inv.move(pick(by[BoxState.STORED]), pick(sst[SlotState.EMPTY]))),
        (2, 'put', lambda: inv.put_directly(pick(sst[SlotState.EMPTY]), grade, '')),
        (2, 'block', lambda: inv.block_slot(pick(sst[SlotState.EMPTY]), "x'; DROP TABLE slots;--")),
        (2, 'unblock', lambda: inv.unblock_slot(pick(sst[SlotState.BLOCKED]))),
        (1, 'policy', lambda: inv.set_policy(rng.choice(list(POLICIES)))),
        (1, 'active', lambda: inv.set_grade_active(rng.choice(['PC9030', 'H01']), rng.random() < 0.8)),
    ]
    _, name, fn = rng.choices(ops, weights=[w for w, _, _ in ops])[0]
    return name, fn


@pytest.mark.parametrize('seed, n_ops, columns, tiers', [(11, 400, 3, 2), (12, 250, 10, 4), (13, 200, 1, 1)])
def test_reopen_after_every_operation_is_transparent(tmp_path, seed, n_ops, columns, tiers):
    """매 동작 뒤 닫고 다시 열어도: 상태·이벤트·카운터·다음 박스 ID·정책·배치가 그대로, 불변식 정상."""
    clock = FakeClock()
    path = tmp_path / 'reopen.db'
    inv = InventoryService(path, small_cfg(columns, tiers), clock=clock)
    rng = random.Random(seed)
    ok = 0
    try:
        for i in range(n_ops):
            name, fn = _review_op(inv, rng)
            try:
                fn()
                ok += 1
            except InventoryError as e:
                # DB 오류는 이제 InventoryError 로 바뀌므로, 로직 버그(제약 위반 등)가 숨지 않게 따로 잡는다
                assert not str(e).startswith('재고 DB 오류'), f'#{i} {name}: {e}'
            before, nxt = digest(inv), inv.next_box_id()
            inv.close()
            other = AppConfig()                       # 다른 설정으로 열어도 DB 쪽을 따른다
            other.allocation_policy = 'lowest_first'
            inv = InventoryService(path, other, clock=clock)
            assert digest(inv) == before, f'#{i} {name}: 다시 열었더니 상태가 다릅니다'
            assert inv.next_box_id() == nxt
            assert inv.check_invariants() == [], f'#{i} {name}'
            assert (other.rack.columns, other.rack.tiers) == (columns, tiers)
            clock.advance(rng.choice([0, 0, 1, 61, 3600 * 7]))
        assert ok > n_ops * 0.25
    finally:
        inv.close()


def test_fifo_same_second_survives_reopen(tmp_path, clock):
    path = tmp_path / 'fifo.db'
    inv = InventoryService(path, AppConfig(), clock=clock)
    order = ['S-10-4', 'N-05-3', 'S-01-1', 'N-01-1']
    ids = []
    for sid in order:
        b = inv.register_arrival('NC3215')
        inv.manual_store(b.id, sid)
        ids.append(b.id)
    inv.close()
    again = InventoryService(path, AppConfig(), clock=clock)
    try:
        assert [r.box.id for r in again.find_grade('nc3215')] == ids
        clock.advance(5)
        b = again.register_arrival('NC3215')
        again.manual_store(b.id, 'N-02-1')
        assert [r.box.id for r in again.find_grade('NC3215')] == ids + [b.id]
    finally:
        again.close()


def test_two_instances_on_one_db_keep_db_invariants(tmp_path, clock):
    """같은 DB 를 두 인스턴스가 번갈아 써도(앱 두 번 실행) DB 자체 불변식은 깨지지 않는다."""
    path = tmp_path / 'shared.db'
    a = InventoryService(path, small_cfg(3, 2), clock=clock)
    b = InventoryService(path, AppConfig(), clock=clock)
    rng = random.Random(3)
    try:
        for i in range(300):
            target = a if rng.random() < 0.5 else b
            name, fn = _review_op(target, rng)
            try:
                fn()
            except InventoryError:
                pass
            assert a.check_invariants() == [] and b.check_invariants() == [], f'#{i} {name}'
    finally:
        a.close()
        b.close()


def test_sql_metacharacters_are_stored_literally(inv):
    nasty = "x'); DROP TABLE boxes; --"
    a = inv.register_arrival('PC9030', lot=nasty, note=nasty, box_id="O'BRIEN\"1")
    inv.block_slot('N-03-1', nasty)
    assert inv.box("o'brien\"1").lot == nasty and inv.box(a.id).note == nasty
    assert inv.slot('N-03-1').blocked_reason == nasty
    assert inv.events(kind="STORE' OR '1'='1") == []
    assert inv.events(box_id="' OR 1=1 --") == []
    assert len(inv.slots()) == 80 and inv.check_invariants() == []


def test_csv_round_trip_korean_quotes_commas(inv, tmp_path):
    b = inv.register_arrival('PC9030', lot='L,"쉼표"따옴표')
    inv.manual_store(b.id, 'N-01-1')
    inv.retrieve('N-01-1', reason='라인 1, "긴급" 출고\n둘째 줄')
    path = tmp_path / 'e.csv'
    inv.export_events_csv(path)
    rows = read_csv(path)
    assert rows[0][:3] == ['번호', '시각', '종류']
    details = [r[-1] for r in rows[1:]]
    assert any('L,"쉼표"따옴표' in d for d in details)
    assert '라인 1, "긴급" 출고 둘째 줄' in details                # 줄바꿈은 공백으로 정리
    assert all(len(r) == len(rows[0]) for r in rows)


# ===================================================================================== 스트레스: 배정 정책
def _random_state(layout: RackLayout, rng: random.Random) -> tuple[list[Slot], dict[str, Box]]:
    slots, boxes = [], {}
    grades = ['PC9030', 'NC3215', 'H01']
    for i, sid in enumerate(layout.slot_ids()):
        face, col, tier = sid[0], int(sid[2:4]), int(sid[5:])
        r = rng.random()
        if r < 0.25:
            bid = f'B{i}'
            boxes[bid] = Box(bid, rng.choice(grades), 'L', state=BoxState.STORED, slot_id=sid)
            slots.append(Slot(sid, face, col, tier, SlotState.OCCUPIED, box_id=bid))
        elif r < 0.32:
            bid = f'R{i}'
            boxes[bid] = Box(bid, rng.choice(grades), 'L', state=BoxState.CONVEYOR, slot_id=sid)
            slots.append(Slot(sid, face, col, tier, SlotState.RESERVED, reserved_for=bid))
        elif r < 0.36:
            slots.append(Slot(sid, face, col, tier, SlotState.BLOCKED, blocked_reason='x'))
        else:
            slots.append(Slot(sid, face, col, tier))
    return slots, boxes


@pytest.mark.parametrize('policy', list(POLICIES))
@pytest.mark.parametrize('dims', [(10, 4), (1, 1), (1, 4), (3, 1)])
def test_policy_result_independent_of_input_order_and_box_dict_order(policy, dims):
    layout = RackLayout(RackConfig(columns=dims[0], tiers=dims[1]))
    alloc = SlotAllocator(layout, policy)
    rng = random.Random(f'{policy}-{dims}')                  # 문자열 시드는 실행마다 같다
    for _ in range(60):
        slots, boxes = _random_state(layout, rng)
        exclude = set(rng.sample(layout.slot_ids(), min(2, layout.capacity))) if rng.random() < 0.3 else None
        for grade in ('PC9030', 'NC3215', 'H05'):
            want = alloc.choose(grade, slots, boxes, exclude)
            for _ in range(4):
                sh = slots[:]
                rng.shuffle(sh)
                items = list(boxes.items())
                rng.shuffle(items)
                assert alloc.choose(grade, sh, dict(items), set(exclude) if exclude else None) == want
            if want is not None:
                s = next(x for x in slots if x.id == want)
                assert s.state == SlotState.EMPTY and not (exclude and want in exclude)


def test_balanced_tie_goes_to_north_and_lowest_first_tie_order():
    layout = RackLayout(RackConfig(columns=2, tiers=2))
    slots = [Slot(sid, sid[0], int(sid[2:4]), int(sid[5:])) for sid in layout.slot_ids()]
    assert SlotAllocator(layout, 'balanced').choose('PC9030', slots, {}) == 'N-01-1'
    assert SlotAllocator(layout, 'lowest_first').choose('PC9030', slots, {}) == 'N-01-1'
    assert SlotAllocator(layout, 'grade_zone').choose('PC9030', slots, {}) == 'N-01-1'
    # N 면에 하나 있으면 balanced 는 S 면, lowest_first 는 같은 단 다음 열
    slots[0] = Slot('N-01-1', 'N', 1, 1, SlotState.OCCUPIED, box_id='B')
    boxes = {'B': Box('B', 'PC9030', 'L', state=BoxState.STORED, slot_id='N-01-1')}
    assert SlotAllocator(layout, 'balanced').choose('PC9030', slots, boxes) == 'S-01-1'
    assert SlotAllocator(layout, 'lowest_first').choose('PC9030', slots, boxes) == 'S-01-1'
    assert SlotAllocator(layout, 'grade_zone').choose('PC9030', slots, boxes) == 'N-01-2'
    assert SlotAllocator(layout, 'grade_zone').choose('H01', slots, boxes) == 'S-01-1'


def test_one_column_one_tier_rack_full_cycle(make_inv):
    inv = make_inv(small_cfg(1, 1))
    assert inv.layout.slot_ids() == ['N-01-1', 'S-01-1']
    a, b, c = (inv.register_arrival(g) for g in ('PC9030', 'PC9030', 'H01'))
    assert inv.reserve_slot(a.id).id == 'N-01-1'
    assert inv.reserve_slot(b.id).id == 'S-01-1'
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(c.id), exc=RackFull)
    assert_unchanged_on_error(inv, lambda: inv.manual_store(c.id), exc=RackFull)
    inv.mark_picked(a.id, 'AMR-01')
    inv.confirm_store(a.id)
    inv.release_reservation(b.id)
    assert inv.manual_store(c.id).id == 'S-01-1'
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(b.id, exclude={'n-01-1'}), exc=RackFull)
    inv.retrieve('n-01-1')
    assert inv.reserve_slot(b.id, exclude={'garbage', 'S-01-1'}).id == 'N-01-1'
    assert inv.check_invariants() == []


def test_exclude_covering_all_free_slots_gives_rack_full(inv):
    b = inv.register_arrival('PC9030')
    everything = set(inv.layout.slot_ids())
    assert_unchanged_on_error(inv, lambda: inv.reserve_slot(b.id, exclude=everything), exc=RackFull,
                              match='빈 칸이 없습니다')


# ===================================================================================== 스트레스: 컨베이어·스캔
def test_return_to_conveyor_repeatedly_keeps_front_order(make_inv):
    cfg = AppConfig()
    cfg.conveyor_capacity = 3
    inv = make_inv(cfg)
    x, y = inv.register_arrival('PC9030'), inv.register_arrival('H01')
    for b in (x, y):
        inv.reserve_slot(b.id)
        inv.mark_picked(b.id, 'AMR-01')
        inv.flag_needs_check(b.id, 'estop')
    w = inv.register_arrival('NC3215')
    inv.return_to_conveyor(y.id)
    inv.return_to_conveyor(x.id)
    assert [b.id for b in inv.conveyor()] == [x.id, y.id, w.id]     # 마지막에 되돌린 것이 맨 앞
    assert_unchanged_on_error(inv, lambda: inv.register_arrival('H01'), exc=ConveyorFull)
    inv.manual_store(x.id)
    n = inv.register_arrival('H01')
    assert [b.id for b in inv.conveyor()] == [y.id, w.id, n.id]
    assert inv.check_invariants() == []


def test_duplicate_scan_of_same_box_label_is_rejected(inv):
    known = {g.code for g in inv.grades(active_only=True)}
    r = parse_scan('  box: w261020-0042 ; pc9030 ; l2610-pc9030-0001 ; 14.5kg \r\n', known)
    assert (r.box_id, r.grade, r.lot, r.weight_kg) == ('W261020-0042', 'PC9030', 'L2610-PC9030-0001', 14.5)
    first = inv.register_arrival(r.grade, r.lot, r.weight_kg, r.box_id)
    again = parse_scan('BOX:W261020-0042;PC9030', known)
    assert_unchanged_on_error(inv, lambda: inv.register_arrival(again.grade, again.lot, 15.0, again.box_id),
                              match='이미 등록된')
    assert inv.next_box_id() != first.id


@pytest.mark.parametrize('dt', [0.05, 0.1, 0.2, 0.3, 0.7, 2.5, 10.0, 33.3])
def test_overbridge_release_times_do_not_depend_on_tick_size(dt):
    """만재가 없으면 도착 시각(=90 + 10k)과 재종·중량 순서가 tick 크기와 무관."""
    def run(step: float, total: float):
        sim = OverbridgeSimulator(replace(SimConfig(), session_boxes=60), ['PC9030', 'NC3215', 'H01'])
        sim.start()
        t, got = 0.0, []
        for i in range(round(total / step)):
            t = (i + 1) * step
            got += [(t, a) for a in sim.tick(step, None)]
        return got

    total = round(800 / dt) * dt
    ref = run(0.1, total)
    got = run(dt, total)
    assert [a for _, a in got] == [a for _, a in ref][:len(got)]
    for (t, _), k in zip(got, range(len(got))):
        due = 90.0 + 10.0 * k
        assert due - 1e-6 <= t < due + dt + 1e-6               # 도착 시각이 속한 tick 에서 나온다


# ===================================================================================== 결함 7
# check_invariants 가 '보관·출고 박스에 작업·확인 필요 표시가 남아 있음', '출고 박스에 출고 시각 없음'을 못 잡았다.
# (구현은 맞게 지우지만, 자가진단이 이런 회귀를 잡아야 selftest 가 의미 있다.)
def test_check_invariants_flags_leftover_order_needs_check_and_out_time(inv):
    a = inv.register_arrival('PC9030')
    inv.manual_store(a.id, 'N-01-1')
    b = inv.register_arrival('H01')
    inv.manual_store(b.id, 'N-02-1')
    inv.retrieve('N-02-1')
    assert inv.check_invariants() == []
    conn = inv._db.conn
    conn.execute("UPDATE boxes SET order_id = 'ORD-9', needs_check = 1 WHERE id = ?", (a.id,))
    conn.execute('UPDATE boxes SET out_at = NULL WHERE id = ?', (b.id,))
    text = '\n'.join(inv.check_invariants())
    assert 'ORD-9' in text and a.id in text
    assert '확인 필요' in text
    assert '출고 시각' in text and b.id in text
