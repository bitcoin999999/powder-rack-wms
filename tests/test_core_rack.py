"""core.rack — 슬롯 ID, 배치(높이·접근 노드), 배정 정책(SPEC §2, §6)."""
from __future__ import annotations

import random

import pytest

from core.config import RackConfig
from core.models import Box, BoxState, Slot, SlotState
from core.rack import POLICIES, RackLayout, SlotAllocator, parse_slot_id, slot_id


# ---------------------------------------------------------------- 슬롯 ID
@pytest.mark.parametrize('face, col, tier, expected', [
    ('N', 3, 2, 'N-03-2'),
    ('S', 10, 4, 'S-10-4'),
    ('N', 1, 1, 'N-01-1'),
    ('S', 99, 9, 'S-99-9'),
])
def test_slot_id_format(face, col, tier, expected):
    assert slot_id(face, col, tier) == expected
    assert parse_slot_id(expected) == (face, col, tier)


@pytest.mark.parametrize('face, col, tier', [
    ('X', 1, 1), ('n', 1, 1), ('', 1, 1), ('N', 0, 1), ('N', -1, 1), ('N', 1, 0), ('N', 1.0, 1), ('N', 1, '2'),
    ('N', True, 1),
])
def test_slot_id_rejects_bad_parts(face, col, tier):
    with pytest.raises(ValueError):
        slot_id(face, col, tier)


@pytest.mark.parametrize('text', [
    '', ' ', 'N032', 'N-3-2', 'X-03-2', 'N-03-0', 'N-00-1', 'N-03-2-1', 'N-003-2', 'N-03-02', 'N_03_2', 'N-03-',
    '-03-2', 'N--03-2', 'NS-03-2', 'N-0A-2', 'N 03 2', None, 123, ('N', 3, 2),
])
def test_parse_slot_id_errors(text):
    with pytest.raises(ValueError):
        parse_slot_id(text)


def test_parse_slot_id_error_message_is_korean():
    with pytest.raises(ValueError, match='슬롯 ID 형식'):
        parse_slot_id('garbage')


def test_parse_slot_id_ignores_case_and_outer_spaces():
    assert parse_slot_id(' n-03-2 ') == ('N', 3, 2)
    assert parse_slot_id('s-10-4') == ('S', 10, 4)


def test_every_layout_id_round_trips():
    layout = RackLayout(RackConfig())
    for sid in layout.slot_ids():
        assert slot_id(*parse_slot_id(sid)) == sid


# ---------------------------------------------------------------- 배치
def test_default_layout_capacity_and_order():
    layout = RackLayout(RackConfig())
    ids = layout.slot_ids()
    assert layout.capacity == 80 == len(ids) == len(set(ids))
    assert ids[:5] == ['N-01-1', 'N-01-2', 'N-01-3', 'N-01-4', 'N-02-1']
    assert ids[39] == 'N-10-4' and ids[40] == 'S-01-1' and ids[-1] == 'S-10-4'
    # 면 N→S, 열, 단 순서 = 정렬 키
    assert ids == sorted(ids, key=lambda s: (parse_slot_id(s)[0] != 'N', parse_slot_id(s)[1], parse_slot_id(s)[2]))
    assert layout.contains('S-10-4') and not layout.contains('S-11-1')


def test_slot_ids_returns_a_copy():
    layout = RackLayout(RackConfig())
    layout.slot_ids().clear()
    assert layout.capacity == 80 and len(layout.slot_ids()) == 80


def test_fork_heights():
    layout = RackLayout(RackConfig())
    assert [layout.fork_height(t) for t in (1, 2, 3, 4)] == pytest.approx([0.40, 0.70, 1.00, 1.30])
    custom = RackLayout(RackConfig(tiers=3, tier_base_m=0.25, tier_pitch_m=0.5))
    assert [custom.fork_height(t) for t in (1, 2, 3)] == pytest.approx([0.25, 0.75, 1.25])
    for bad in (0, 5, -1, 1.5):
        with pytest.raises(ValueError):
            layout.fork_height(bad)


@pytest.mark.parametrize('sid, node', [('N-03-2', 'N03'), ('S-10-4', 'S10'), ('N-01-1', 'N01'), ('s-07-3', 'S07')])
def test_access_nodes(sid, node):
    assert RackLayout(RackConfig()).access_node(sid) == node


@pytest.mark.parametrize('sid', ['N-11-1', 'S-01-5', 'bad', ''])
def test_access_node_rejects_slots_outside_layout(sid):
    with pytest.raises(ValueError):
        RackLayout(RackConfig()).access_node(sid)


def test_column_x():
    layout = RackLayout(RackConfig())
    assert layout.column_x(1) == pytest.approx(0.19)
    assert layout.column_x(10) == pytest.approx(3.61)
    assert layout.column_x(2) - layout.column_x(1) == pytest.approx(0.38)
    for bad in (0, 11):
        with pytest.raises(ValueError):
            layout.column_x(bad)


@pytest.mark.parametrize('cfg', [RackConfig(columns=0), RackConfig(tiers=0), RackConfig(columns=100),
                                 RackConfig(tiers=10), RackConfig(columns=2.5)])
def test_invalid_layout_config(cfg):
    with pytest.raises(ValueError):
        RackLayout(cfg)


def test_custom_layout_is_independent_of_later_config_changes():
    cfg = RackConfig(columns=3, tiers=2)
    layout = RackLayout(cfg)
    cfg.columns = 9
    assert layout.capacity == 12 and layout.columns == 3
    assert layout.slot_ids()[-1] == 'S-03-2'


# ---------------------------------------------------------------- 배정 정책
def make_state(layout: RackLayout, occupied: dict[str, str] | None = None, reserved: dict[str, str] | None = None,
               blocked: set[str] | None = None) -> tuple[list[Slot], dict[str, Box]]:
    """occupied/reserved: 칸 → 재종."""
    occupied, reserved, blocked = occupied or {}, reserved or {}, blocked or set()
    slots, boxes = [], {}
    for i, sid in enumerate(layout.slot_ids()):
        face, col, tier = parse_slot_id(sid)
        s = Slot(sid, face, col, tier)
        if sid in occupied:
            bid = f'B{i:03d}'
            boxes[bid] = Box(bid, occupied[sid], 'L', state=BoxState.STORED, slot_id=sid)
            s.state, s.box_id = SlotState.OCCUPIED, bid
        elif sid in reserved:
            bid = f'R{i:03d}'
            boxes[bid] = Box(bid, reserved[sid], 'L', state=BoxState.CONVEYOR, slot_id=sid)
            s.state, s.reserved_for = SlotState.RESERVED, bid
        elif sid in blocked:
            s.state = SlotState.BLOCKED
        slots.append(s)
    return slots, boxes


@pytest.fixture
def layout():
    return RackLayout(RackConfig())


def choose(layout, policy, grade, exclude=None, **state):
    slots, boxes = make_state(layout, **state)
    return SlotAllocator(layout, policy).choose(grade, slots, boxes, exclude)


def test_unknown_policy_rejected(layout):
    with pytest.raises(ValueError):
        SlotAllocator(layout, 'random')
    assert set(POLICIES) == {'grade_zone', 'lowest_first', 'balanced'}


def test_default_policy_is_grade_zone(layout):
    assert SlotAllocator(layout).policy == 'grade_zone'


# grade_zone
def test_grade_zone_empty_rack_takes_bottom_of_nearest_column(layout):
    assert choose(layout, 'grade_zone', 'PC9030') == 'N-01-1'


def test_grade_zone_stacks_same_grade_in_its_column(layout):
    assert choose(layout, 'grade_zone', 'PC9030', occupied={'N-03-1': 'PC9030'}) == 'N-03-2'


def test_grade_zone_new_grade_goes_to_nearest_empty_column(layout):
    # N-01 에 다른 재종이 있으면 그 위가 아니라 다음 빈 열(같은 번호 열이면 N 면 먼저 → S-01)
    assert choose(layout, 'grade_zone', 'PC9030', occupied={'N-01-1': 'NC3215'}) == 'S-01-1'
    occ = {'N-01-1': 'NC3215', 'S-01-1': 'H01'}
    assert choose(layout, 'grade_zone', 'PC9030', occupied=occ) == 'N-02-1'


def test_grade_zone_full_grade_column_falls_back_to_empty_column(layout):
    occ = {f'N-03-{t}': 'PC9030' for t in (1, 2, 3, 4)} | {'N-01-1': 'NC3215'}
    assert choose(layout, 'grade_zone', 'PC9030', occupied=occ) == 'S-01-1'


def test_grade_zone_prefers_lower_tier_among_same_grade_columns(layout):
    occ = {'N-05-1': 'PC9030', 'N-05-2': 'PC9030', 'S-02-1': 'PC9030'}
    assert choose(layout, 'grade_zone', 'PC9030', occupied=occ) == 'S-02-2'
    # 같은 단이면 도크에 가까운 열
    occ = {'N-04-1': 'PC9030', 'S-02-1': 'PC9030'}
    assert choose(layout, 'grade_zone', 'PC9030', occupied=occ) == 'S-02-2'


def test_grade_zone_skips_blocked_cells_in_grade_column(layout):
    occ = {'N-03-1': 'PC9030'}
    assert choose(layout, 'grade_zone', 'PC9030', occupied=occ, blocked={'N-03-2'}) == 'N-03-3'


def test_grade_zone_counts_reserved_same_grade(layout):
    # 같은 작업에서 먼저 예약된 같은 재종 박스가 있으면 그 열로 모은다
    assert choose(layout, 'grade_zone', 'PC9030', reserved={'N-06-1': 'PC9030'}) == 'N-06-2'
    # 다른 재종 예약 열은 '빈 열'이 아니다
    assert choose(layout, 'grade_zone', 'PC9030', reserved={'N-01-1': 'NC3215'}) == 'S-01-1'


def test_grade_zone_exclude_set(layout):
    assert choose(layout, 'grade_zone', 'PC9030', exclude={'N-01-1'}) == 'S-01-1'
    assert choose(layout, 'grade_zone', 'PC9030', exclude={'N-01-1', 'S-01-1'}) == 'N-02-1'
    # 같은 재종 열 안에서도 제외 칸은 건너뜀
    assert choose(layout, 'grade_zone', 'PC9030', occupied={'N-03-1': 'PC9030'}, exclude={'N-03-2'}) == 'N-03-3'
    # 잘못된 ID 가 섞여도 무시
    assert choose(layout, 'grade_zone', 'PC9030', exclude={'garbage'}) == 'N-01-1'


def test_grade_zone_blocked_bottom_cell_in_empty_column(layout):
    assert choose(layout, 'grade_zone', 'PC9030', blocked={'N-01-1'}) == 'N-01-2'


def test_grade_zone_any_free_cell_when_no_empty_column(layout):
    occ = {slot_id(f, c, 1): 'NC3215' for f in 'NS' for c in range(1, 11)}
    assert choose(layout, 'grade_zone', 'PC9030', occupied=occ) == 'N-01-2'
    assert choose(layout, 'grade_zone', 'PC9030', occupied=occ, blocked={'N-01-2', 'S-01-2'}) == 'N-02-2'


def test_full_or_blocked_rack_gives_none(layout):
    all_ids = layout.slot_ids()
    for policy in POLICIES:
        assert choose(layout, policy, 'PC9030', occupied={s: 'H01' for s in all_ids}) is None
        assert choose(layout, policy, 'PC9030', blocked=set(all_ids)) is None
        assert choose(layout, policy, 'PC9030', exclude=set(all_ids)) is None
        half = {s: 'H01' for s in all_ids[:40]}
        assert choose(layout, policy, 'PC9030', occupied=half, blocked=set(all_ids[40:])) is None


# lowest_first
def test_lowest_first_order(layout):
    assert choose(layout, 'lowest_first', 'PC9030') == 'N-01-1'
    assert choose(layout, 'lowest_first', 'PC9030', occupied={'N-01-1': 'PC9030'}) == 'S-01-1'
    assert choose(layout, 'lowest_first', 'PC9030', occupied={'N-01-1': 'X', 'S-01-1': 'X'}) == 'N-02-1'
    tier1 = {slot_id(f, c, 1): 'X' for f in 'NS' for c in range(1, 11)}
    assert choose(layout, 'lowest_first', 'PC9030', occupied=tier1) == 'N-01-2'


def test_lowest_first_ignores_grade_and_respects_exclude_and_blocked(layout):
    occ = {'N-05-1': 'PC9030'}
    assert choose(layout, 'lowest_first', 'PC9030', occupied=occ) == 'N-01-1'
    assert choose(layout, 'lowest_first', 'PC9030', exclude={'N-01-1', 'S-01-1'}) == 'N-02-1'
    assert choose(layout, 'lowest_first', 'PC9030', blocked={'N-01-1'}, reserved={'S-01-1': 'H01'}) == 'N-02-1'


# balanced
def test_balanced_prefers_face_with_fewer_boxes(layout):
    assert choose(layout, 'balanced', 'PC9030') == 'N-01-1'                       # 같으면 N
    assert choose(layout, 'balanced', 'PC9030', occupied={'N-01-1': 'X'}) == 'S-01-1'
    occ = {'N-01-1': 'X', 'N-02-1': 'X', 'S-05-3': 'X'}
    assert choose(layout, 'balanced', 'PC9030', occupied=occ) == 'S-01-1'
    occ = {'S-01-1': 'X', 'S-02-1': 'X', 'N-05-3': 'X'}
    assert choose(layout, 'balanced', 'PC9030', occupied=occ) == 'N-01-1'


def test_balanced_counts_reserved_and_excluded(layout):
    assert choose(layout, 'balanced', 'PC9030', reserved={'N-04-2': 'X'}) == 'S-01-1'
    assert choose(layout, 'balanced', 'PC9030', exclude={'N-01-1'}) == 'S-01-1'
    # 박스 수에 사용중지 칸은 들어가지 않는다
    assert choose(layout, 'balanced', 'PC9030', blocked={'N-01-1'}) == 'N-02-1'
    assert choose(layout, 'balanced', 'PC9030', blocked={'N-02-1', 'S-01-1'}) == 'N-01-1'


def test_balanced_falls_back_to_other_face_when_preferred_face_full(layout):
    s_ids = [s for s in layout.slot_ids() if s.startswith('S')]
    occ = {'N-01-1': 'X', 'N-02-1': 'X'}
    # S 면이 전부 사용중지(박스 0) → S 가 '적은 면'이지만 빈 칸이 없으니 N 면
    assert choose(layout, 'balanced', 'PC9030', occupied=occ, blocked=set(s_ids)) == 'N-03-1'


# 결정성
@pytest.mark.parametrize('policy', sorted(POLICIES))
def test_choice_is_deterministic_and_independent_of_input_order(layout, policy):
    rng = random.Random(7)
    all_ids = layout.slot_ids()
    for _ in range(60):
        occ = {s: rng.choice(['PC9030', 'NC3215', 'H01']) for s in rng.sample(all_ids, rng.randint(0, 70))}
        rest = [s for s in all_ids if s not in occ]
        blocked = set(rng.sample(rest, min(len(rest), rng.randint(0, 5))))
        slots, boxes = make_state(layout, occupied=occ, blocked=blocked)
        alloc = SlotAllocator(layout, policy)
        first = alloc.choose('PC9030', slots, boxes)
        assert alloc.choose('PC9030', slots, boxes) == first
        shuffled = slots[:]
        rng.shuffle(shuffled)
        assert alloc.choose('PC9030', shuffled, boxes) == first
        if first is not None:
            chosen = next(s for s in slots if s.id == first)
            assert chosen.state == SlotState.EMPTY


def test_choose_never_returns_non_empty_cells(layout):
    rng = random.Random(3)
    all_ids = layout.slot_ids()
    for policy in POLICIES:
        for _ in range(40):
            picks = rng.sample(all_ids, 60)
            occ = {s: 'PC9030' for s in picks[:30]}
            res = {s: 'PC9030' for s in picks[30:45]}
            blk = set(picks[45:])
            got = choose(layout, policy, 'PC9030', occupied=occ, reserved=res, blocked=blk)
            assert got is not None and got not in picks
