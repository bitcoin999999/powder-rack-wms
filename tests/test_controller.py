"""Controller — 재고(core) ↔ AMR(amr) 연결, 자동 배차, 작업 이벤트 처리, 설정, 자가 시험 (SPEC §5·§9·§11·§12).
화면 없이 임시 폴더에서 돈다."""
from __future__ import annotations

import copy
import json

import pytest

from amr.models import OrderState, RobotMode
from controller import SCAN_DEBOUNCE_TEXT, Controller
from core.config import AppConfig, RobotConfig
from core.inventory import ConveyorFull, InventoryError
from core.models import BoxState, SlotState


# ---------------------------------------------------------------- 도우미
class Rec:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, kind: str, payload: dict) -> None:
        self.calls.append((kind, payload))

    def notices(self, part: str = '') -> list[str]:
        return [p['text'] for k, p in self.calls if k == 'notice' and part in p['text']]

    def of(self, kind: str) -> list[dict]:
        return [p for k, p in self.calls if k == kind]


class FakeMono:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def make(tmp_path):
    made: list[Controller] = []

    def factory(robots: int = 1, sub: str = 'a', columns: int | None = None, tiers: int | None = None,
                seed: int = 1, config: AppConfig | None = None) -> Controller:
        cfg = config or AppConfig()
        if config is None:
            cfg.sim.amr_count = robots
            cfg.sim.seed = seed
            if columns is not None:
                cfg.rack.columns = columns
            if tiers is not None:
                cfg.rack.tiers = tiers
        c = Controller(tmp_path / sub, cfg)
        made.append(c)
        return c

    yield factory
    for c in made:
        c.shutdown()


def advance(c: Controller, seconds: float, check=None) -> None:
    """공개 tick 으로 시뮬레이션 seconds 초 (배속 1, 한 번에 0.2 s = 한 조각)."""
    c.set_sim_speed(1.0)
    for _ in range(int(round(seconds / 0.2))):
        c.tick(0.2)
        if check:
            check()


def advance_until(c: Controller, cond, limit: float = 3600.0, check=None) -> None:
    c.set_sim_speed(1.0)
    end = c.sim_now + limit
    while not cond():
        assert c.sim_now < end, f'시뮬레이션 {limit:.0f} s 안에 조건이 만족되지 않음'
        c.tick(0.2)
        if check:
            check()


def inbound(c: Controller, active: bool = True):
    return [o for o in c.fleet.orders(active_only=active) if o.kind == 'INBOUND']


def picking(c: Controller):
    return [o for o in inbound(c) if any(s.action == 'pick' for s in o.steps[o.current:])]


def trays(c: Controller) -> dict[str, list]:
    return {st.robot_id: list(st.trays) for st in c.fleet.robots()}


def events_of(c: Controller, box_id: str, kind: str):
    return [e for e in c.inventory.events(limit=0, box_id=box_id) if e.kind == kind]


def one_dock_picker(c: Controller):
    def check():
        assert len(picking(c)) <= 1, '도크에서 집는 입고 작업이 둘 이상'
        if c.sim_now % 30 < 0.2:
            assert c.check_consistency() == []
    return check


# ---------------------------------------------------------------- 시작
def test_startup_writes_config_and_db_and_builds_cell(make, tmp_path):
    c = make()
    data = tmp_path / 'a' / 'data'
    assert (data / 'config.json').exists() and (data / 'rack.db').exists()
    raw = json.loads((data / 'config.json').read_text(encoding='utf-8'))
    assert raw['rack']['columns'] == 10 and raw['sim']['amr_count'] == 1
    assert c.mode_label() == '시뮬레이션 AMR'
    assert [r.robot_id for r in c.fleet.robots()] == ['AMR-01']
    assert {'DOCK', 'CHARGER', 'PARK1', 'PARK2', 'N10', 'S10'} <= set(c.topology.nodes)
    assert c.sim_now == 0.0 and c.auto_dispatch is True and c.sim_speed == 1.0
    assert c.inventory.snapshot().capacity == 80
    assert c.check_consistency() == []


def test_db_rack_wins_and_config_object_is_kept(make, tmp_path):
    first = make(columns=6, tiers=3)
    first.shutdown()
    cfg = AppConfig()                                    # 기본 10×4 로 다시 열어도 DB 의 6×3
    c = make(config=cfg)
    assert c.config is cfg
    assert (cfg.rack.columns, cfg.rack.tiers) == (6, 3)
    assert 'N06' in c.topology.nodes and 'N07' not in c.topology.nodes
    raw = json.loads((tmp_path / 'a' / 'data' / 'config.json').read_text(encoding='utf-8'))
    assert (raw['rack']['columns'], raw['rack']['tiers']) == (6, 3)


def test_parks_at_least_robot_count(make):
    c = make(robots=3)
    assert {'PARK1', 'PARK2', 'PARK3'} <= set(c.topology.nodes)
    assert [st.node for st in c.fleet.robots()] == ['PARK1', 'PARK2', 'PARK3']


def test_shutdown_is_idempotent_and_tick_after_is_ignored(make):
    c = make()
    c.shutdown()
    c.shutdown()
    c.tick(1.0)
    assert c.sim_now == 0.0


def test_driver_failures_are_notices_not_crashes(make):
    cfg = AppConfig()
    cfg.robots = [RobotConfig(id='T1', driver='tcp_json', host='', port=0),
                  RobotConfig(id='X1', driver='foo', start_node='PARK2')]
    c = make(config=cfg)
    rec = Rec()
    c.subscribe(rec)                                     # 시작 중 알림은 구독할 때 다시 받는다
    assert rec.notices('T1 연결 실패') and rec.notices('알 수 없는 드라이버')
    assert c.mode_label() == '실제 AMR (foo·tcp_json)'
    assert all(not st.connected and st.mode == RobotMode.OFFLINE for st in c.fleet.robots())
    assert c.fleet.idle_robots() == []
    box = c.add_sample_box()
    advance(c, 120)
    assert inbound(c, active=False) == []
    assert c.inventory.box(box.id).state == BoxState.CONVEYOR and c.inventory.box(box.id).slot_id is None


def test_mixed_real_and_sim_mode_label(make):
    cfg = AppConfig()
    cfg.robots = [RobotConfig(id='S1', driver='sim', start_node='PARK1'),
                  RobotConfig(id='T1', driver='tcp_json', host='', port=0, start_node='PARK2')]
    c = make(config=cfg)
    assert c.mode_label() == '실제 AMR (tcp_json) + 시뮬레이션 1대'


def test_broken_config_json_starts_with_defaults_and_keeps_original(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(parents=True)
    (data / 'config.json').write_text('{"rack": {"columns": 6,', encoding='utf-8')
    c = Controller(tmp_path)
    try:
        rec = Rec()
        c.subscribe(rec)
        assert rec.notices('설정 파일(config.json)을 읽을 수 없어')
        assert list(data.glob('config.json.broken-*'))
        assert json.loads((data / 'config.json').read_text(encoding='utf-8'))['rack']['columns'] == 10
    finally:
        c.shutdown()


def test_invalid_config_values_are_reset_to_defaults_with_notice(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(parents=True)
    (data / 'config.json').write_text(json.dumps({'charge_below': 95, 'charge_target': 90, 'conveyor_capacity': 0,
                                                  'sim': {'speed_mps': 0, 'trays': 3}}), encoding='utf-8')
    c = Controller(tmp_path)
    try:
        rec = Rec()
        c.subscribe(rec)
        assert rec.notices('설정 값 오류')
        assert (c.config.charge_below, c.config.charge_target) == (25.0, 90.0)
        assert c.config.sim.speed_mps == 1.0 and c.config.conveyor_capacity == 5
        assert c.fleet.charge_below == 25.0
    finally:
        c.shutdown()


def test_bad_robot_fields_are_fixed_not_dropped(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(parents=True)
    (data / 'config.json').write_text(json.dumps({'robots': [
        {'id': 'T1', 'driver': 'tcp_json', 'port': 'abc', 'start_node': 'PARK1'},
        {'id': 't1', 'driver': 'tcp_json'},                 # ID 중복 (대소문자만 다름) → 뺀다
        {'id': 'V1', 'driver': 'vda5050', 'host': 5, 'port': 1883, 'start_node': 'PARK2'}]}), encoding='utf-8')
    c = Controller(tmp_path)
    try:
        assert [r.id for r in c.config.robots] == ['T1', 'V1']
        assert c.config.robots[0].port == 0 and c.config.robots[1].host == ''
        assert c.mode_label().startswith('실제 AMR (')               # 가상 AMR 로 바뀌지 않는다
        assert [st.robot_id for st in c.fleet.robots()] == ['T1', 'V1']
        assert all(not st.connected for st in c.fleet.robots())
    finally:
        c.shutdown()


# ---------------------------------------------------------------- 시간
def test_tick_slices_speed_cap_and_publish_rate(make):
    c = make()
    mono = FakeMono()
    c._monotonic = mono
    rec = Rec()
    c.subscribe(rec)
    dts: list[float] = []
    orig = c._slice
    c._slice = lambda dt: (dts.append(dt), orig(dt))
    c.set_sim_speed(20)
    c.tick(1.0)
    assert c.sim_now == pytest.approx(20.0)
    assert max(dts) <= 0.2 + 1e-12 and sum(dts) == pytest.approx(20.0) and len(dts) == 100
    assert len(rec.of('tick')) == 1 and rec.of('tick')[0]['sim_now'] == pytest.approx(20.0)
    c.tick(0.1)                                          # 0.25 s 안 → tick 알림 없음
    assert len(rec.of('tick')) == 1
    mono.t += 0.3
    c.tick(0.1)
    assert len(rec.of('tick')) == 2
    c.set_sim_speed(1)
    before = c.sim_now
    c.tick(100.0)                                        # 실제 경과는 5 s 까지만
    assert c.sim_now == pytest.approx(before + 5.0)
    for bad in (-1.0, float('nan'), float('inf'), 'x'):
        before = c.sim_now
        c.tick(bad)                                      # type: ignore[arg-type]
        assert c.sim_now == before
    for bad in (0, -2, 1001, float('nan'), True):
        with pytest.raises(InventoryError, match='배속'):
            c.set_sim_speed(bad)


# ---------------------------------------------------------------- 입고 한 바퀴
@pytest.mark.parametrize('robots', [1, 2])
def test_full_inbound_cycle(make, robots):
    c = make(robots=robots)
    rec = Rec()
    c.subscribe(rec)
    c.start_overbridge(9)
    advance_until(c, lambda: c.stats['STORE'] == 9 and not inbound(c), limit=3000, check=one_dock_picker(c))
    boxes = c.inventory.boxes()
    assert len(boxes) == 9 and all(b.state == BoxState.STORED for b in boxes)
    for b in boxes:
        reserve = events_of(c, b.id, 'RESERVE')
        store = events_of(c, b.id, 'STORE')
        pick = events_of(c, b.id, 'PICK')
        assert len(reserve) == len(store) == len(pick) == 1
        assert reserve[0].slot_id == store[0].slot_id == b.slot_id       # 예약한 칸에 들어갔다
        assert reserve[0].order_id == pick[0].order_id == store[0].order_id
        assert c.inventory.slot(b.slot_id).state == SlotState.OCCUPIED
    assert all(t is None for ts in trays(c).values() for t in ts)
    assert c.inventory.conveyor() == []
    assert c.check_consistency() == []
    done = [o for o in inbound(c, active=False)]
    assert done and all(o.state == OrderState.DONE for o in done)
    assert all(len(o.meta['boxes']) <= 3 for o in done)
    assert sum(len(o.meta['boxes']) for o in done) == 9
    events = {(p['order'], p['event']) for p in rec.of('order')}
    assert all((o.id, 'created') in events and (o.id, 'done') in events for o in done)
    if robots == 2:
        assert {o.robot for o in done} == {'AMR-01', 'AMR-02'}            # 두 대 모두 일했다


def test_inbound_order_shape(make):
    c = make()
    c.set_auto_dispatch(False)
    boxes = [c.add_sample_box() for _ in range(4)]
    c.set_auto_dispatch(True)
    advance_until(c, lambda: bool(inbound(c)), limit=5)
    order = inbound(c)[0]
    picks = [s for s in order.steps if s.action == 'pick']
    drops = [s for s in order.steps if s.action == 'drop']
    assert order.steps == picks + drops and order.robot == 'AMR-01'
    assert order.meta['boxes'] == [b.id for b in boxes[:3]]                # 컨베이어 맨 앞 3개
    assert [s.tray for s in picks] == [0, 1, 2]
    assert all(s.node == 'DOCK' and s.zone == 'DOCK' and s.height_m == 0.85 for s in picks)
    assert [s.ref for s in picks] == order.meta['boxes']
    layout = c.inventory.layout
    for d in drops:
        box = c.inventory.box(d.meta['box'])
        assert d.ref == d.meta['slot'] == box.slot_id and box.order_id == order.id
        assert d.node == layout.access_node(d.ref) and d.zone is None
        assert d.height_m == layout.fork_height(c.inventory.slot(d.ref).tier)
        assert d.tray == next(p.tray for p in picks if p.meta['box'] == d.meta['box'])
    # 내려놓기 순서: 도크에서 가까운 접근 노드부터, 그다음은 직전 노드에서 가까운 곳
    cur = 'DOCK'
    for d in drops:
        rest = drops[drops.index(d):]
        assert c.topology.distance(cur, d.node) == pytest.approx(min(c.topology.distance(cur, r.node) for r in rest))
        cur = d.node
    assert c.inventory.box(boxes[3].id).order_id is None                   # 네 번째는 다음 작업


def test_auto_dispatch_off_dispatches_nothing(make, tmp_path):
    c = make()
    c.set_auto_dispatch(False)
    assert json.loads((tmp_path / 'a' / 'data' / 'config.json').read_text(encoding='utf-8'))['auto_dispatch'] is False
    boxes = [c.add_sample_box() for _ in range(3)]
    advance(c, 300)
    assert inbound(c, active=False) == []
    assert all(c.inventory.box(b.id).slot_id is None and c.inventory.box(b.id).order_id is None for b in boxes)
    c.set_auto_dispatch(True)
    advance_until(c, lambda: c.stats['STORE'] == 3, limit=600)


def test_queued_inbound_is_redispatched_when_its_robot_becomes_busy(make):
    c = make(robots=2)
    box = c.add_sample_box()
    c._auto_dispatch()                                   # 작업만 만들고 (FleetManager tick 전)
    first = inbound(c)[0]
    assert first.state == OrderState.QUEUED
    c.fleet.driver(first.robot).pause()                  # 배정 AMR 이 다른 상태로 (예: 충전 시작)
    c._auto_dispatch()
    assert first.state == OrderState.CANCELLED
    second = inbound(c)[0]
    assert second.robot != first.robot and second.meta['boxes'] == [box.id]
    assert c.inventory.box(box.id).order_id == second.id
    c.fleet.driver(first.robot).resume()
    advance_until(c, lambda: c.inventory.box(box.id).state == BoxState.STORED, limit=600)
    assert c.check_consistency() == []


# ---------------------------------------------------------------- 빈 트레이만, 적재대 가득
def fail_after_first_pick(c: Controller) -> str:
    """박스 1개 입고 작업을 내려놓기 도중 고장 → 그 박스는 확인 필요로 트레이에 남는다."""
    box = c.add_sample_box()
    advance_until(c, lambda: c.inventory.box(box.id).state == BoxState.ON_AMR, limit=300)
    order = inbound(c)[0]
    advance(c, 1.0)
    c.fleet.driver(order.robot).inject_failure('시험 고장')
    advance_until(c, lambda: order.state == OrderState.FAILED, limit=10)
    b = c.inventory.box(box.id)
    assert b.state == BoxState.ON_AMR and b.needs_check and b.slot_id
    assert c.inventory.slot(b.slot_id).state == SlotState.RESERVED          # 예약은 유지
    return box.id


def test_dispatch_uses_only_empty_trays(make):
    c = make()
    stuck = fail_after_first_pick(c)
    assert trays(c)['AMR-01'] == [stuck, None, None]
    new = [c.add_sample_box() for _ in range(3)]
    advance_until(c, lambda: bool(inbound(c)), limit=5)
    order = inbound(c)[0]
    picks = [s for s in order.steps if s.action == 'pick']
    assert [s.tray for s in picks] == [1, 2]
    assert order.meta['boxes'] == [new[0].id, new[1].id]
    advance_until(c, lambda: not inbound(c) and c.stats['STORE'] == 3, limit=1200)
    last = inbound(c, active=False)[-1]
    assert last.meta['boxes'] == [new[2].id] and [s.tray for s in last.steps if s.action == 'pick'] == [1]
    assert trays(c)['AMR-01'] == [stuck, None, None]
    assert c.check_consistency() == []


def test_rack_full_on_tiny_rack(make):
    c = make(columns=2, tiers=1)                          # 2열 × 1단 × 양면 = 4칸
    rec = Rec()
    c.subscribe(rec)
    c.start_overbridge(7)
    advance_until(c, lambda: c.stats['STORE'] == 4 and len(c.inventory.conveyor()) == 3, limit=3000)
    advance(c, 600)
    assert len(rec.notices('빈 칸이 없어')) == 1                            # 가득 찬 동안 한 번만
    assert c.stats['STORE'] == 4 and not inbound(c)
    assert all(b.order_id is None and b.slot_id is None for b in c.inventory.conveyor())
    assert c.check_consistency() == []
    out = c.inventory.retrieve('N-01-1', '출고')
    assert out.state == BoxState.OUT
    advance_until(c, lambda: c.stats['STORE'] == 5, limit=600)
    advance(c, 600)
    assert len(rec.notices('빈 칸이 없어')) == 2                            # 새로 가득 참 → 한 번 더
    assert len(c.inventory.conveyor()) == 2 and c.check_consistency() == []


def test_blocked_slots_count_as_full(make):
    c = make(columns=1, tiers=1)                          # 2칸
    c.inventory.block_slot('N-01-1', '점검')
    c.inventory.block_slot('S-01-1', '점검')
    rec = Rec()
    c.subscribe(rec)
    c.add_sample_box()
    advance(c, 60)
    assert inbound(c, active=False) == [] and len(rec.notices('빈 칸이 없어')) == 1
    c.inventory.unblock_slot('S-01-1')
    advance_until(c, lambda: c.stats['STORE'] == 1, limit=300)


# ---------------------------------------------------------------- 실패 → 확인 필요 → 정리
def test_failure_mid_drop_then_resolve_stored_and_return(make):
    c = make()
    rec = Rec()
    c.subscribe(rec)
    c.set_auto_dispatch(False)
    b1, b2, b3 = (c.add_sample_box() for _ in range(3))
    c.set_auto_dispatch(True)
    advance_until(c, lambda: c.stats['STORE'] == 1, limit=900)
    order = inbound(c)[0]
    c.fleet.driver('AMR-01').inject_failure('시험 고장')
    advance_until(c, lambda: order.state == OrderState.FAILED, limit=10)
    states = {b.id: c.inventory.box(b.id) for b in (b1, b2, b3)}
    stored = [x for x in states.values() if x.state == BoxState.STORED]
    flagged = [x for x in states.values() if x.state == BoxState.ON_AMR]
    assert len(stored) == 1 and len(flagged) == 2 and all(x.needs_check for x in flagged)
    assert rec.notices('확인 필요')
    assert sorted(t for t in trays(c)['AMR-01'] if t) == sorted(x.id for x in flagged)
    assert c.check_consistency() == []
    with pytest.raises(InventoryError, match='AMR 위 박스가 아닙니다'):
        c.resolve_stored(stored[0].id)

    keep, back = flagged
    slot = c.resolve_stored(keep.id)
    assert slot.id == keep.slot_id and slot.state == SlotState.OCCUPIED     # 예약 칸에 들어간 것으로
    assert keep.id not in trays(c)['AMR-01']
    assert not c.inventory.box(keep.id).needs_check

    c.set_auto_dispatch(False)
    c.resolve_return(back.id)
    b = c.inventory.box(back.id)
    assert b.state == BoxState.CONVEYOR and b.slot_id is None and not b.needs_check
    assert c.inventory.slot(back.slot_id).state == SlotState.EMPTY
    assert c.inventory.conveyor()[0].id == back.id and back.id not in trays(c)['AMR-01']
    assert trays(c)['AMR-01'] == [None, None, None]
    c.set_auto_dispatch(True)
    advance_until(c, lambda: c.inventory.box(back.id).state == BoxState.STORED, limit=900)
    assert c.check_consistency() == []


def test_resolve_refused_while_box_is_in_a_running_order(make):
    c = make()
    box = c.add_sample_box()
    advance_until(c, lambda: c.inventory.box(box.id).state == BoxState.ON_AMR, limit=300)
    with pytest.raises(InventoryError, match='진행 중'):
        c.resolve_stored(box.id)
    with pytest.raises(InventoryError, match='진행 중'):
        c.resolve_return(box.id)


def test_resolve_return_when_conveyor_full_raises_and_keeps_tray(make):
    c = make()
    stuck = fail_after_first_pick(c)
    c.set_auto_dispatch(False)
    for _ in range(5):
        c.add_sample_box()
    with pytest.raises(ConveyorFull):
        c.resolve_return(stuck)
    assert trays(c)['AMR-01'][0] == stuck and c.inventory.box(stuck).needs_check
    c.resolve_stored(stuck)
    assert trays(c)['AMR-01'] == [None, None, None]


def test_pick_failure_releases_unpicked_boxes_and_retries(make):
    c = make()
    c.set_auto_dispatch(False)
    boxes = [c.add_sample_box() for _ in range(3)]
    c.fleet.driver('AMR-01').inject_failure('시험 고장')   # 다음 단계(첫 집기) 실패
    c.set_auto_dispatch(True)
    advance_until(c, lambda: any(o.state == OrderState.FAILED for o in inbound(c, active=False)), limit=60)
    for b in boxes:
        unres = events_of(c, b.id, 'UNRESERVE')
        assert unres and '실패' in unres[0].detail
    advance_until(c, lambda: c.stats['STORE'] == 3, limit=900)
    assert c.check_consistency() == []


def test_estop_flags_loaded_boxes_and_resume_continues(make):
    c = make()
    c.set_auto_dispatch(False)
    boxes = [c.add_sample_box() for _ in range(3)]
    c.set_auto_dispatch(True)
    advance_until(c, lambda: c.stats['PICK'] == 2, limit=300)
    order = inbound(c)[0]
    c.estop()
    advance(c, 1.0)
    assert order.state == OrderState.FAILED
    states = [c.inventory.box(b.id) for b in boxes]
    assert [s.state for s in states] == [BoxState.ON_AMR, BoxState.ON_AMR, BoxState.CONVEYOR]
    assert states[0].needs_check and states[1].needs_check and states[2].slot_id is None
    advance(c, 60)
    assert len(inbound(c, active=False)) == 1                               # 정지 중에는 배차 없음
    c.resume_all()
    advance_until(c, lambda: c.inventory.box(boxes[2].id).state == BoxState.STORED, limit=900)
    later = inbound(c, active=False)[-1]
    assert [s.tray for s in later.steps if s.action == 'pick'] == [2]       # 남은 빈 트레이만
    for b in boxes[:2]:
        c.resolve_stored(b.id)
    assert trays(c)['AMR-01'] == [None, None, None]
    assert c.check_consistency() == []


def test_cancel_order_releases_reservations(make):
    c = make()
    rec = Rec()
    c.subscribe(rec)
    boxes = [c.add_sample_box() for _ in range(2)]
    advance_until(c, lambda: bool(inbound(c)), limit=5)
    order = inbound(c)[0]
    c.set_auto_dispatch(False)
    c.cancel_order(order.id)
    advance(c, 1.0)
    assert order.state == OrderState.CANCELLED
    assert all(c.inventory.box(b.id).slot_id is None and c.inventory.box(b.id).order_id is None for b in boxes)
    assert {'order': order.id, 'event': 'cancelled'}.items() <= next(
        p for p in rec.of('order') if p['event'] == 'cancelled').items()
    with pytest.raises(InventoryError, match='없는 작업'):
        c.cancel_order('O9999')
    assert c.check_consistency() == []


# ---------------------------------------------------------------- 재시작
def test_restart_cleanup_leaves_no_stuck_boxes(tmp_path):
    c1 = Controller(tmp_path)
    c1.set_auto_dispatch(False)
    boxes = [c1.add_sample_box() for _ in range(3)]
    c1.set_auto_dispatch(True)
    advance_until(c1, lambda: c1.stats['PICK'] == 1, limit=300)
    oid = c1.inventory.box(boxes[1].id).order_id
    assert oid and c1.inventory.box(boxes[2].id).order_id == oid
    reserved = c1.inventory.box(boxes[0].id).slot_id
    c1.shutdown()                                        # 작업 도중 종료

    c2 = Controller(tmp_path)
    try:
        rec = Rec()
        c2.subscribe(rec)
        assert rec.notices('재시작 정리')
        first, *rest = (c2.inventory.box(b.id) for b in boxes)
        assert first.state == BoxState.ON_AMR and first.needs_check and first.slot_id == reserved
        assert all(b.state == BoxState.CONVEYOR and b.order_id is None and b.slot_id is None for b in rest)
        assert c2.check_consistency() == []
        c2.resolve_return(first.id)                      # 새 가상 AMR 은 트레이가 비어 있다 — 그래도 정리된다
        advance_until(c2, lambda: c2.stats['STORE'] == 3, limit=1800)
        assert c2.check_consistency() == []
    finally:
        c2.shutdown()
    c3 = Controller(tmp_path)                            # 다시 열어도 새 정리 없음
    try:
        assert not c3.notice_log or not any('재시작 정리' in n['text'] for n in c3.notice_log)
        assert all(b.state == BoxState.STORED for b in c3.inventory.boxes())
    finally:
        c3.shutdown()


# ---------------------------------------------------------------- 스캔·샘플·도착 취소
def test_scan_and_debounce(make):
    c = make()
    c.set_auto_dispatch(False)
    mono = FakeMono()
    c._monotonic = mono
    a = c.scan('PC9030')
    assert a.grade == 'PC9030' and a.weight_kg == 15.0 and a.state == BoxState.CONVEYOR
    mono.t += 0.5
    with pytest.raises(InventoryError, match=SCAN_DEBOUNCE_TEXT):
        c.scan(' pc9030 \r\n')
    assert len(c.inventory.conveyor()) == 1
    c.scan('NC3020')                                     # 다른 내용은 바로 받는다
    mono.t += 0.5
    with pytest.raises(InventoryError, match='등록되지 않은 재종'):
        c.scan('ZZ999')
    with pytest.raises(InventoryError, match='등록되지 않은 재종'):  # 실패한 스캔은 무시 대상이 아니다
        c.scan('ZZ999')
    mono.t += 2.1
    c.scan('PC9030')
    b = c.scan('BOX:BX-1;PC9030;L77;14.2')
    assert (b.id, b.lot, b.weight_kg) == ('BX-1', 'L77', 14.2)
    c.scan('H01')                                        # 5개째 → 컨베이어 가득
    with pytest.raises(ConveyorFull):
        c.scan('PC5400')
    c.cancel_arrival(b.id, '시험')
    c.scan('PC5400')                                     # 만재로 실패했던 스캔은 바로 다시 된다
    assert SCAN_DEBOUNCE_TEXT == '같은 스캔이 2초 안에 다시 들어와 무시했습니다'


def test_add_sample_box_is_seeded(tmp_path):
    def run(sub: str) -> list[tuple[str, float]]:
        cfg = AppConfig()
        cfg.conveyor_capacity = 100
        cfg.auto_dispatch = False
        c = Controller(tmp_path / sub, cfg)
        try:
            return [(b.grade, b.weight_kg) for b in (c.add_sample_box() for _ in range(96))]
        finally:
            c.shutdown()

    first, second = run('a'), run('b')
    assert first == second
    assert len({g for g, _ in first}) > 5
    assert all(w == 15.0 or 10.0 <= w <= 14.9 for _, w in first)
    assert any(w < 15.0 for _, w in first)


def test_add_sample_box_grade_and_inactive(make):
    c = make()
    c.set_auto_dispatch(False)
    assert c.add_sample_box('H01').grade == 'H01'
    c.inventory.set_grade_active('H05', False)
    with pytest.raises(InventoryError, match='사용중지'):
        c.add_sample_box('H05')
    for g in c.inventory.grades():
        c.inventory.set_grade_active(g.code, False)
    with pytest.raises(InventoryError, match='사용 중인 재종이 없습니다'):
        c.add_sample_box()


def test_cancel_arrival_pass_through(make):
    c = make()
    c.set_auto_dispatch(False)
    a = c.add_sample_box()
    out = c.cancel_arrival(a.id, '두 번 읽힘')
    assert out.state == BoxState.OUT
    b = c.add_sample_box()
    c.set_auto_dispatch(True)
    advance_until(c, lambda: c.inventory.box(b.id).order_id is not None, limit=5)
    with pytest.raises(InventoryError, match='묶인 박스'):
        c.cancel_arrival(b.id, '두 번 읽힘')


def test_manual_store_pass_through(make):
    c = make()
    c.set_auto_dispatch(False)
    a = c.add_sample_box('PC9030')
    slot = c.manual_store(a.id, 'S-05-2')
    assert slot.id == 'S-05-2' and c.inventory.box(a.id).state == BoxState.STORED


def test_master_change_refreshes_overbridge_grades(make):
    c = make()
    c.set_auto_dispatch(False)
    c.config.conveyor_capacity = 10
    for g in c.inventory.grades():
        if g.code != 'PC9030':
            c.inventory.set_grade_active(g.code, False)
    c.start_overbridge(6)
    advance_until(c, lambda: c.stats['ARRIVE'] == 6, limit=300)
    assert {b.grade for b in c.inventory.conveyor()} == {'PC9030'}
    c.inventory.set_grade_active('PC9030', False)        # 출고 재종이 없으면 기다린다
    with pytest.raises(InventoryError, match='재종'):
        c.start_overbridge(3)


# ---------------------------------------------------------------- 설정
def test_apply_config_in_place(make, tmp_path):
    c = make()
    cfg, rack, sim, fleet = c.config, c.config.rack, c.config.sim, c.fleet
    rec = Rec()
    c.subscribe(rec)
    new = copy.deepcopy(c.config)
    new.conveyor_capacity = 2
    new.charge_below, new.charge_target = 30.0, 80.0
    new.allocation_policy = 'lowest_first'
    new.sim.speed_mps = 1.5
    new.operator = '홍길동'
    new.auto_dispatch = False
    c.apply_config(new)
    assert c.config is cfg and c.config.rack is rack and c.config.sim is sim and c.fleet is fleet
    assert cfg.conveyor_capacity == 2 and cfg.sim.speed_mps == 1.5 and cfg.operator == '홍길동'
    assert (c.fleet.charge_below, c.fleet.charge_target) == (30.0, 80.0)
    assert c.inventory.policy == 'lowest_first' and cfg.allocation_policy == 'lowest_first'
    assert c.auto_dispatch is False
    c.add_sample_box()
    c.add_sample_box()
    with pytest.raises(ConveyorFull):                    # 재고 서비스가 같은 설정 객체를 본다
        c.add_sample_box()
    raw = json.loads((tmp_path / 'a' / 'data' / 'config.json').read_text(encoding='utf-8'))
    assert raw['conveyor_capacity'] == 2 and raw['operator'] == '홍길동' and raw['allocation_policy'] == 'lowest_first'
    assert rec.of('config')[-1] == {'rack_changed': False, 'fleet_rebuilt': False}
    new.sim.speed_mps = 0.5                               # 넘겨준 객체를 나중에 바꿔도 반영되지 않는다
    assert cfg.sim.speed_mps == 1.5


@pytest.mark.parametrize('change, match', [
    (lambda n: setattr(n, 'charge_below', 90.0), '충전 기준'),
    (lambda n: setattr(n, 'charge_target', 101.0), '충전 기준'),
    (lambda n: setattr(n.rack, 'columns', 0), '열 수'),
    (lambda n: setattr(n.rack, 'tiers', 10), '단 수'),
    (lambda n: setattr(n.sim, 'speed_mps', 0), '주행 속도'),
    (lambda n: setattr(n.sim, 'trays', 0), '트레이'),
    (lambda n: setattr(n, 'conveyor_capacity', 0), '컨베이어'),
    (lambda n: setattr(n, 'allocation_policy', 'random'), '배정 정책'),
    (lambda n: setattr(n, 'robots', [RobotConfig(id='A'), RobotConfig(id='a')]), '중복'),
    (lambda n: setattr(n, 'robots', [RobotConfig(id='A', driver='foo')]), '드라이버'),
])
def test_apply_config_rejects_bad_values(make, change, match):
    c = make()
    before = json.dumps(c.config.__dict__, default=lambda o: o.__dict__, sort_keys=True)
    new = copy.deepcopy(c.config)
    change(new)
    with pytest.raises(InventoryError, match=match):
        c.apply_config(new)
    assert json.dumps(c.config.__dict__, default=lambda o: o.__dict__, sort_keys=True) == before


def test_apply_config_rejects_non_config(make):
    with pytest.raises(InventoryError):
        make().apply_config({'rack': {}})               # type: ignore[arg-type]


def test_apply_config_rebuilds_rack_and_fleet(make):
    c = make()
    rec = Rec()
    c.subscribe(rec)
    old_fleet = c.fleet
    new = copy.deepcopy(c.config)
    new.rack.columns, new.rack.tiers = 6, 3
    new.sim.amr_count = 2
    c.apply_config(new)
    assert c.inventory.layout.capacity == 36 and (c.config.rack.columns, c.config.rack.tiers) == (6, 3)
    assert 'N06' in c.topology.nodes and 'N07' not in c.topology.nodes
    assert c.fleet is not old_fleet and [r.robot_id for r in c.fleet.robots()] == ['AMR-01', 'AMR-02']
    assert rec.of('config')[-1] == {'rack_changed': True, 'fleet_rebuilt': True}
    c.start_overbridge(4)
    advance_until(c, lambda: c.stats['STORE'] == 4 and not inbound(c), limit=1800)
    assert all(c.inventory.layout.contains(b.slot_id) for b in c.inventory.boxes())
    assert c.check_consistency() == []


def test_apply_config_trays_change_rebuilds_drivers(make):
    c = make()
    new = copy.deepcopy(c.config)
    new.sim.trays = 2
    c.apply_config(new)
    assert trays(c) == {'AMR-01': [None, None]}
    c.set_auto_dispatch(False)
    for _ in range(3):
        c.add_sample_box()
    c.set_auto_dispatch(True)
    advance_until(c, lambda: bool(inbound(c)), limit=5)
    assert len(inbound(c)[0].meta['boxes']) == 2


def test_apply_config_refused_while_busy(make):
    c = make()
    c.set_auto_dispatch(False)
    for _ in range(3):
        c.add_sample_box()
    c.set_auto_dispatch(True)
    advance_until(c, lambda: c.stats['PICK'] == 1, limit=300)
    fleet = c.fleet
    new = copy.deepcopy(c.config)
    new.sim.amr_count = 2
    with pytest.raises(InventoryError, match='진행 중인 AMR 작업'):
        c.apply_config(new)
    assert c.config.sim.amr_count == 1 and c.fleet is fleet
    calm = copy.deepcopy(c.config)
    calm.charge_below = 20.0                             # 충전 기준 같은 값은 작업 중에도 바꿀 수 있다
    c.apply_config(calm)
    assert c.fleet.charge_below == 20.0
    advance_until(c, lambda: c.stats['STORE'] == 3 and not inbound(c), limit=900)
    # 적재대에 박스가 있으면 구성 변경은 재고 서비스가 거부 → 설정은 그대로
    rack = copy.deepcopy(c.config)
    rack.rack.columns = 8
    with pytest.raises(InventoryError, match='비어 있을 때만'):
        c.apply_config(rack)
    assert c.config.rack.columns == 10 and c.inventory.layout.columns == 10
    assert c.check_consistency() == []


def test_apply_config_refused_with_box_on_amr(make):
    c = make()
    fail_after_first_pick(c)
    new = copy.deepcopy(c.config)
    new.sim.amr_count = 2
    with pytest.raises(InventoryError, match='AMR 위 박스'):
        c.apply_config(new)


# ---------------------------------------------------------------- 점검·자가 시험·결정성
def test_check_consistency_catches_tray_mismatch(make):
    c = make()
    c.fleet.driver('AMR-01')._trays[1] = 'GHOST'          # 시험용으로 트레이를 망가뜨림
    assert any('GHOST' in p for p in c.check_consistency())


@pytest.mark.parametrize('robots', [1, 2])
def test_selftest_two_hours_is_clean(make, robots):
    c = make(robots=robots)
    problems = c.selftest(2.0)
    assert problems == []
    assert c.sim_now == pytest.approx(7200.0)
    assert c.stats['STORE'] > 60 and c.stats['OUT'] > 30
    assert c.stats['ERROR'] >= 1                                            # 고장 주입 → 확인 필요가 실제로 생겼다
    assert c.stats['_resolved_stored'] >= 1 and c.stats['_resolved_return'] >= 1
    assert c.check_consistency() == []
    assert c.auto_dispatch is True


def test_selftest_reports_broken_state(make):
    c = make()
    c.fleet.driver('AMR-01')._trays[2] = 'GHOST'
    problems = c.selftest(0.05)
    assert problems and any('GHOST' in p for p in problems)


def test_same_seed_same_actions_same_slots(tmp_path):
    def run(sub: str):
        cfg = AppConfig()
        cfg.sim.amr_count = 2
        cfg.sim.seed = 7
        c = Controller(tmp_path / sub, cfg)
        try:
            c.start_overbridge(15)
            c.set_sim_speed(10)
            for _ in range(300):
                c.tick(0.5)
            boxes = [(b.id, b.grade, b.state.value, b.slot_id, b.weight_kg) for b in c.inventory.boxes()]
            orders = [(o.id, o.kind, o.robot, o.state.value, tuple(o.meta.get('boxes', ()))) for o in c.fleet.orders()]
            return boxes, orders
        finally:
            c.shutdown()

    first, second = run('a'), run('b')
    assert first == second
    assert sum(1 for b in first[0] if b[2] == 'STORED') >= 10
