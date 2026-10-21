import math
import random

import pytest

from amr.driver import AmrDriver
from amr.fleet import FleetManager
from amr.models import OrderState, RobotMode, RobotStatus, Step
from amr.sim_driver import SimulatedAmrDriver
from amr.topology import build_cell_topology
from core.config import RackConfig, SimConfig

AISLE = [f'{f}{c:02d}' for f in 'NS' for c in range(1, 11)]


class Harness:
    def __init__(self, n=2, cfg=None, starts=None, parks=None, **kw):
        self.topo = build_cell_topology(RackConfig(), parks=parks if parks is not None else max(2, n))
        self.cfg = cfg or SimConfig()
        starts = starts or [f'PARK{i + 1}' for i in range(n)]
        self.drivers = [SimulatedAmrDriver(f'AMR-{i + 1:02d}', self.topo, self.cfg, starts[i]) for i in range(n)]
        self.fleet = FleetManager(self.topo, self.drivers, **kw)
        self.log = []
        self.fleet.subscribe(self.log.append)
        self.now = 0.0
        self.fleet.tick(self.now)

    def tick(self, dt=0.2):
        self.now = round(self.now + dt, 9)
        self.fleet.tick(self.now)

    def run(self, seconds, dt=0.2, check=None):
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.tick(dt)
            if check:
                check()

    def run_until(self, cond, limit=2000.0, dt=0.2, check=None):
        end = self.now + limit
        while not cond():
            assert self.now < end, '시간 안에 조건이 만족되지 않음'
            self.tick(dt)
            if check:
                check()

    def events(self, order_id):
        return [(e.kind, e.step.id if e.step else None) for e in self.log if e.order.id == order_id]

    def status(self, rid):
        return self.fleet.driver(rid).status()


def inbound(boxes, nodes, zone='DOCK'):
    picks = [Step('', 'DOCK', 'pick', height_m=0.85, tray=i, ref=b, zone=zone, meta={'box': b})
             for i, b in enumerate(boxes)]
    drops = [Step('', n, 'drop', height_m=0.4 + 0.3 * (i % 4), tray=i, ref=f'{n[0]}-{n[1:]}-{i % 4 + 1}',
                  meta={'box': b}) for i, (b, n) in enumerate(zip(boxes, nodes))]
    return picks + drops


def move(node, **kw):
    return [Step('', node, **kw)]


# ---------------------------------------------------------------------------- 작업 등록·조회
def test_new_order_ids_and_validation():
    h = Harness(1)
    o1 = h.fleet.new_order('MOVE', move('N03'), now=1.0)
    o2 = h.fleet.new_order('INBOUND', inbound(['B1', 'B2'], ['N01', 'N02']), meta={'x': 1}, now=2.0)
    assert (o1.id, o2.id) == ('O0001', 'O0002')
    assert [s.id for s in o2.steps] == ['O0002-S1', 'O0002-S2', 'O0002-S3', 'O0002-S4']
    assert o1.state == OrderState.QUEUED and o1.created == 1.0 and o2.meta == {'x': 1}
    assert [e.kind for e in h.log] == ['created', 'created']
    assert h.fleet.orders() == [o1, o2] and h.fleet.orders(active_only=True) == [o1, o2]
    assert h.fleet.order('O0002') is o2
    with pytest.raises(KeyError):
        h.fleet.order('O9999')
    with pytest.raises(ValueError):
        h.fleet.new_order('MOVE', [])
    with pytest.raises(ValueError):
        h.fleet.new_order('MOVE', move('NOWHERE'))
    with pytest.raises(ValueError):
        h.fleet.new_order('MOVE', move('N01'), robot='AMR-99')
    with pytest.raises(KeyError):
        h.fleet.driver('AMR-99')


# ---------------------------------------------------------------------------- 배차
def test_nearest_robot_dispatch():
    h = Harness(2, starts=['S01', 'N10'])
    near_s = h.fleet.new_order('MOVE', move('S03'))
    near_n = h.fleet.new_order('MOVE', move('N08'))
    h.tick()
    assert near_s.robot == 'AMR-01' and near_n.robot == 'AMR-02'
    assert near_s.state == OrderState.RUNNING and near_n.state == OrderState.RUNNING
    assert near_s.steps[0].path == h.topo.shortest_path('S01', 'S03')   # 경로는 로봇 현재 노드부터
    assert h.fleet.robot_order('AMR-01') is near_s


def test_nearest_robot_order_independent_of_driver_order():
    h = Harness(2, starts=['N10', 'S01'])
    o = h.fleet.new_order('MOVE', move('S02'))
    h.tick()
    assert o.robot == 'AMR-02'


def test_priority_then_created_order():
    h = Harness(1, starts=['N01'])
    low = h.fleet.new_order('MOVE', move('N03'), priority=0, now=0.0)
    low2 = h.fleet.new_order('MOVE', move('N04'), priority=0, now=0.5)
    high = h.fleet.new_order('MOVE', move('N05'), priority=5, now=1.0)
    h.tick()
    assert high.state == OrderState.RUNNING and low.state == OrderState.QUEUED
    h.run_until(lambda: high.state == OrderState.DONE)
    h.tick()
    assert low.state == OrderState.RUNNING and low2.state == OrderState.QUEUED
    h.run_until(lambda: low2.state == OrderState.DONE)
    starts = [o.started for o in (high, low, low2)]
    assert starts == sorted(starts)


def test_fixed_robot_assignment():
    h = Harness(2, starts=['S01', 'N10'])
    o = h.fleet.new_order('MOVE', move('S02'), robot='AMR-02')   # AMR-01 이 더 가깝지만 지정
    h.tick()
    assert o.robot == 'AMR-02' and o.state == OrderState.RUNNING
    # 지정 AMR 이 바쁘면 다른 AMR 이 쉬어도 기다린다
    o2 = h.fleet.new_order('MOVE', move('S03'), robot='AMR-02')
    h.tick()
    assert o2.state == OrderState.QUEUED
    assert 'AMR-01' in h.fleet.idle_robots()
    h.run_until(lambda: o2.state == OrderState.DONE)
    assert o2.robot == 'AMR-02'


def test_idle_robots_definition():
    h = Harness(2)
    assert h.fleet.idle_robots() == ['AMR-01', 'AMR-02']
    h.drivers[1]._battery = 20.0            # charge_below 25 이하 → 일반 작업 대상 아님
    assert h.fleet.idle_robots() == ['AMR-01']
    o = h.fleet.new_order('MOVE', move('N05'))
    h.tick()
    assert o.robot == 'AMR-01'
    assert h.fleet.idle_robots() == []
    h.drivers[1]._battery = 80.0
    h.drivers[1].pause()
    assert h.fleet.idle_robots() == []      # PAUSED
    h.drivers[1].resume()
    h.drivers[1].disconnect()
    assert h.fleet.idle_robots() == []      # 연결 끊김


# ---------------------------------------------------------------------------- 단계 진행·이벤트
def test_event_sequence_three_picks_three_drops():
    h = Harness(1)
    o = h.fleet.new_order('INBOUND', inbound(['B1', 'B2', 'B3'], ['S03', 'N05', 'S08']))
    zone_seen = []

    def check():
        if o.state == OrderState.RUNNING:
            zone_seen.append((o.current, h.fleet.zone_owner('DOCK')))

    h.run_until(lambda: o.state == OrderState.DONE, check=check)
    expected = [('created', None), ('assigned', None)]
    for n in range(1, 7):
        expected += [('step_started', f'O0001-S{n}'), ('step_done', f'O0001-S{n}')]
    expected.append(('done', None))
    assert h.events(o.id) == expected
    assert o.current == 6 and o.started is not None and o.finished >= o.started
    assert o.steps[0].path[0] == 'PARK1' and o.steps[0].path[-1] == 'DOCK'
    assert o.steps[1].path == ['DOCK'] and o.steps[3].path[0] == 'DOCK'
    # 집기 3번 동안 도크 잠금을 계속 쥐고, 내려놓기 단계에선 풀려 있음
    assert all(owner == 'AMR-01' for cur, owner in zone_seen if cur < 3)
    assert all(owner is None for cur, owner in zone_seen if cur >= 3)
    assert h.fleet.zone_owner('DOCK') is None
    assert h.status('AMR-01').trays == [None, None, None]
    assert h.fleet.robot_order('AMR-01') is None
    assert all(e.robot == 'AMR-01' for e in h.log if e.kind != 'created')


def test_two_robots_dock_zone_exclusive_long_run():
    h = Harness(2)
    rng = random.Random(7)
    dock = h.topo.nodes['DOCK']
    orders = []
    box = 0
    next_submit = 0.0

    def check():
        nonlocal next_submit, box
        # 도크 단계는 동시에 한 대만, 그 AMR 이 잠금 주인
        in_dock = [d.robot_id for d in h.drivers if d._step is not None and d._step.zone == 'DOCK']
        assert len(in_dock) <= 1, in_dock
        if in_dock:
            assert h.fleet.zone_owner('DOCK') == in_dock[0]
        near = [d.robot_id for d in h.drivers
                if math.hypot(d.status().x - dock.x, d.status().y - dock.y) < 0.3]
        assert len(near) <= 1, near
        if len(orders) < 24 and h.now >= next_submit:
            n = rng.randint(1, 3)
            boxes = [f'B{box + i:03d}' for i in range(n)]
            box += n
            nodes = sorted(rng.sample(AISLE, n), key=lambda x: h.topo.distance('DOCK', x))
            orders.append(h.fleet.new_order('INBOUND', inbound(boxes, nodes), now=h.now))
            next_submit += 15.0

    h.run_until(lambda: len(orders) == 24 and all(o.state == OrderState.DONE for o in orders),
                limit=6000.0, check=check)
    assert {o.robot for o in orders} == {'AMR-01', 'AMR-02'}       # 두 대 모두 일함
    assert any(e.kind == 'waiting' and e.step.zone == 'DOCK' for e in h.log)   # 실제로 경합이 있었음
    assert h.fleet.zone_owner('DOCK') is None
    assert all(not d.busy or h.fleet.robot_order(d.robot_id).kind == 'PARK' for d in h.drivers)


def test_waiting_event_and_status():
    h = Harness(2)
    a = h.fleet.new_order('INBOUND', inbound(['A1', 'A2'], ['S02', 'S03']))
    b = h.fleet.new_order('INBOUND', inbound(['B1'], ['N04']))
    h.tick()
    assert a.robot != b.robot
    holder = h.fleet.zone_owner('DOCK')
    other = b.robot if holder == a.robot else a.robot
    h.run(20.0)
    waiting = [e for e in h.log if e.kind == 'waiting']
    assert len(waiting) == 1 and waiting[0].robot == other and 'DOCK' in waiting[0].detail
    st = {s.robot_id: s for s in h.fleet.robots()}
    assert st[other].mode == RobotMode.WAITING and 'DOCK' in st[other].message
    assert st[holder].mode != RobotMode.WAITING
    waiting_order = b if other == b.robot else a
    assert waiting_order.state == OrderState.ASSIGNED
    h.run_until(lambda: a.state == OrderState.DONE and b.state == OrderState.DONE)


# ---------------------------------------------------------------------------- 취소·실패
def test_cancel_queued_and_assigned():
    h = Harness(1)
    running = h.fleet.new_order('MOVE', move('S10'))
    queued = h.fleet.new_order('MOVE', move('S05'))
    h.tick()
    assert queued.state == OrderState.QUEUED
    h.fleet.cancel(queued.id, '계획 변경')
    assert queued.state == OrderState.CANCELLED and queued.error == '계획 변경'   # tick 없이 바로
    assert h.events(queued.id)[-1] == ('cancelled', None)
    h.fleet.cancel(queued.id)              # 끝난 작업 취소는 무시
    assert h.fleet.orders(active_only=True) == [running]
    # ASSIGNED (구역 대기) 취소
    h2 = Harness(2)
    a = h2.fleet.new_order('INBOUND', inbound(['A1'], ['S02']))
    b = h2.fleet.new_order('INBOUND', inbound(['B1'], ['S03']))
    h2.tick()
    h2.tick()
    waiting = a if a.state == OrderState.ASSIGNED else b
    assert waiting.state == OrderState.ASSIGNED
    h2.fleet.cancel(waiting.id)
    assert waiting.state == OrderState.CANCELLED
    assert h2.fleet.robot_order(waiting.robot) is None


def test_cancel_running_order():
    h = Harness(1)
    o = h.fleet.new_order('INBOUND', inbound(['B1', 'B2'], ['S05', 'S06']))
    h.run_until(lambda: o.current == 1 and h.drivers[0].busy)   # 두 번째 집기 진행 중 (도크 잠금)
    assert h.fleet.zone_owner('DOCK') == 'AMR-01'
    h.fleet.cancel(o.id, '작업자 취소')
    assert o.state == OrderState.RUNNING           # 드라이버 응답 대기
    h.tick()
    assert o.state == OrderState.CANCELLED and o.error == '작업자 취소'
    assert h.events(o.id)[-2:] == [('step_failed', 'O0001-S2'), ('cancelled', None)]
    assert h.fleet.zone_owner('DOCK') is None
    assert h.fleet.robot_order('AMR-01') is None and not h.drivers[0].busy
    assert h.status('AMR-01').trays == ['B1', None, None]   # 이미 집은 박스는 실린 채 (Controller 가 확인 처리)
    assert 'AMR-01' in h.fleet.idle_robots()


def test_injected_failure_fails_order_and_frees_robot():
    h = Harness(2)
    o = h.fleet.new_order('INBOUND', inbound(['B1', 'B2', 'B3'], ['S05', 'S06', 'S07']))
    h.run_until(lambda: o.current == 1 and o.robot is not None and h.fleet.driver(o.robot).busy)
    rid = o.robot
    assert h.fleet.zone_owner('DOCK') == rid
    h.fleet.driver(rid).inject_failure('포크 센서 오류')
    h.tick()
    assert o.state == OrderState.FAILED and o.error == '포크 센서 오류'
    assert [k for k, _ in h.events(o.id)][-2:] == ['step_failed', 'failed']
    assert h.fleet.zone_owner('DOCK') is None
    assert h.fleet.robot_order(rid) is None
    assert rid in h.fleet.idle_robots()
    # 다음 작업 정상 진행
    o2 = h.fleet.new_order('MOVE', move('N02'), robot=rid)
    h.run_until(lambda: o2.state == OrderState.DONE)


def test_send_failure_marks_order_failed():
    class Rejecting(AmrDriver):
        kind = 'fake'

        def connect(self): ...
        def disconnect(self): ...
        def send_step(self, step): raise RuntimeError('AMR 거절')
        def pause(self): ...
        def resume(self): ...
        def cancel(self): ...
        def estop(self): ...
        def status(self): return RobotStatus(self.robot_id, node='N01')

    topo = build_cell_topology(RackConfig())
    fleet = FleetManager(topo, [Rejecting('AMR-X')])
    o = fleet.new_order('INBOUND', inbound(['B1'], ['S05']))
    fleet.tick(0.0)
    assert o.state == OrderState.FAILED and 'AMR 거절' in o.error
    assert fleet.zone_owner('DOCK') is None and fleet.robot_order('AMR-X') is None


def test_unknown_step_events_are_ignored():
    h = Harness(1)
    o = h.fleet.new_order('MOVE', move('S08'))
    h.tick()
    drv = h.drivers[0]
    drv._emit('step_done', 'BOGUS-S1')
    drv._emit('step_failed', None, '???')
    drv._emit('step_started', 'O0099-S1')
    drv._emit('status')
    drv._emit('error', detail='시험 오류')
    h.tick()
    assert o.state == OrderState.RUNNING
    h.run_until(lambda: o.state == OrderState.DONE)
    drv._emit('step_done', o.steps[0].id)   # 이미 끝난 단계의 늦은 이벤트
    h.tick()
    assert o.state == OrderState.DONE
    assert [k for k, _ in h.events(o.id)].count('done') == 1


def test_subscriber_exception_does_not_break_fleet():
    h = Harness(1)

    def bad(_event):
        raise ValueError('구독자 오류')

    h.fleet.subscribe(bad)
    o = h.fleet.new_order('MOVE', move('N02'))
    h.run_until(lambda: o.state == OrderState.DONE)


# ---------------------------------------------------------------------------- 일시정지·비상정지
def test_pause_and_resume_all():
    h = Harness(1)
    o = h.fleet.new_order('MOVE', move('S10'))
    h.run(1.0)
    h.fleet.pause_all()
    h.tick()
    x = h.status('AMR-01').x
    assert h.status('AMR-01').mode == RobotMode.PAUSED
    h.run(30.0)
    assert h.status('AMR-01').x == x and o.state == OrderState.RUNNING
    # 정지 중에는 새 작업 배차 안 함
    o2 = h.fleet.new_order('MOVE', move('N01'))
    h.run(1.0)
    assert o2.state == OrderState.QUEUED
    h.fleet.resume_all()
    h.run_until(lambda: o.state == OrderState.DONE and o2.state == OrderState.DONE)


def test_estop_all_fails_running_keeps_queued():
    h = Harness(2)
    a = h.fleet.new_order('INBOUND', inbound(['A1'], ['S02']))
    b = h.fleet.new_order('INBOUND', inbound(['B1'], ['S03']))
    h.tick()
    h.tick()
    queued = h.fleet.new_order('MOVE', move('N05'), robot='AMR-01')
    h.fleet.estop_all()
    assert all(d.status().mode == RobotMode.ERROR for d in h.drivers)   # 드라이버엔 즉시
    h.tick()
    assert a.state == OrderState.FAILED and b.state == OrderState.FAILED   # 하나는 구역 대기 중이었음
    assert {a.error, b.error} <= {'estop', '비상정지'}
    assert queued.state == OrderState.QUEUED
    assert h.fleet.zone_owner('DOCK') is None
    h.run(5.0)
    assert queued.state == OrderState.QUEUED      # 해제 전에는 배차 안 함
    h.fleet.resume_all()
    h.run_until(lambda: queued.state == OrderState.DONE)
    assert all(d.status().mode != RobotMode.ERROR for d in h.drivers)


# ---------------------------------------------------------------------------- 충전·주차
def test_auto_charge_one_robot_at_charger_at_a_time():
    cfg = SimConfig(battery_per_s=0.1, charge_per_s=1.0)
    h = Harness(2, cfg=cfg)
    charger = h.topo.nodes['CHARGER']
    charged = []

    def on_event(e):
        if e.order.kind == 'CHARGE' and e.kind == 'done':
            charged.append((e.robot, h.status(e.robot).battery))

    h.fleet.subscribe(on_event)

    def check():
        at = [d.robot_id for d in h.drivers
              if math.hypot(d.status().x - charger.x, d.status().y - charger.y) < 0.05]
        assert len(at) <= 1, at
        steps = [d.robot_id for d in h.drivers if d._step is not None and d._step.action == 'charge']
        assert len(steps) <= 1
        assert all(d.status().mode != RobotMode.ERROR for d in h.drivers)

    h.run_until(lambda: len(charged) >= 4, limit=5000.0, dt=0.5, check=check)
    assert {r for r, _ in charged} == {'AMR-01', 'AMR-02'}
    assert all(b >= 89.9 for _, b in charged)          # 목표 90 % 까지
    charge_orders = [o for o in h.fleet.orders() if o.kind == 'CHARGE']
    assert all(o.steps[0].node == 'CHARGER' and o.steps[0].zone == 'CHARGER' and o.steps[0].action == 'charge'
               and o.steps[0].meta['target'] == 90.0 for o in charge_orders)
    assert any(e.kind == 'waiting' and e.step.zone == 'CHARGER' for e in h.log)   # 한 대는 기다렸음
    first = charge_orders[0]
    assert first.created >= 750.0 - 1.0                 # 100 → 25 % 대기 소모 시간


def test_low_battery_robot_not_dispatched_but_charges_first():
    h = Harness(1, cfg=SimConfig(charge_per_s=1.0))
    h.drivers[0]._battery = 20.0
    o = h.fleet.new_order('MOVE', move('S05'))
    h.tick()
    assert o.state == OrderState.QUEUED
    charge = h.fleet.robot_order('AMR-01')
    assert charge is not None and charge.kind == 'CHARGE'
    h.run_until(lambda: o.state == OrderState.DONE)
    assert charge.state == OrderState.DONE and charge.finished <= o.started


def test_auto_park_after_idle_and_preemption():
    h = Harness(1, park_after_s=10.0)
    o = h.fleet.new_order('MOVE', move('S05'))
    h.run_until(lambda: o.state == OrderState.DONE)
    t_done = h.now
    h.run(9.0)
    assert h.fleet.robot_order('AMR-01') is None       # 아직 주차 안 함
    h.run_until(lambda: h.fleet.robot_order('AMR-01') is not None, limit=5.0)
    park = h.fleet.robot_order('AMR-01')
    assert park.kind == 'PARK' and h.now - t_done >= 10.0 - 1e-6
    target = park.steps[0].node
    assert target == min(['PARK1', 'PARK2'], key=lambda p: h.topo.distance('S05', p))
    assert 'AMR-01' in h.fleet.idle_robots()           # 주차 중은 쉬는 AMR
    h.run(1.0)
    assert h.drivers[0].busy and park.state == OrderState.RUNNING
    # 주차 중 새 작업 → 주차 취소하고 새 작업
    real = h.fleet.new_order('MOVE', move('N01'), now=h.now)
    h.tick()
    assert real.robot == 'AMR-01'
    assert h.fleet.robot_order('AMR-01') is real
    h.run_until(lambda: real.state == OrderState.DONE)
    assert park.state == OrderState.CANCELLED
    assert h.events(park.id)[-1] == ('cancelled', None)
    # 다시 쉬면 주차해서 PARK 노드에 선다
    h.run_until(lambda: h.status('AMR-01').node.startswith('PARK') and not h.drivers[0].busy, limit=60.0)
    assert h.fleet.robot_order('AMR-01') is None


def test_park_not_started_is_cancelled_immediately_on_preemption():
    h = Harness(1, park_after_s=0.0, starts=['S05'])
    h.fleet.pause_all()
    h.tick()                         # 정지 중이라 주차 안 함
    h.fleet.resume_all()
    h.tick()                         # 재개: 바로 주차 작업 (park_after_s 0)
    park = h.fleet.robot_order('AMR-01')
    assert park is not None and park.kind == 'PARK'
    real = h.fleet.new_order('MOVE', move('S06'))
    h.tick()
    assert h.fleet.robot_order('AMR-01') is real
    h.run_until(lambda: real.state == OrderState.DONE)
    assert park.state == OrderState.CANCELLED


def test_two_robots_park_in_different_places():
    h = Harness(2, starts=['S04', 'S05'], park_after_s=5.0)
    h.run_until(lambda: all(h.status(r).node.startswith('PARK') and not h.fleet.driver(r).busy
                            for r in ('AMR-01', 'AMR-02')), limit=60.0)
    assert {h.status('AMR-01').node, h.status('AMR-02').node} == {'PARK1', 'PARK2'}
    parks = [o for o in h.fleet.orders() if o.kind == 'PARK']
    assert len(parks) == 2 and all(o.state == OrderState.DONE for o in parks)


def test_robot_blocking_dock_moves_away_immediately():
    """작업이 실패해 도크에 선 AMR 은 다른 AMR 이 도크를 기다리면 park_after_s 전에 비켜 준다."""
    h = Harness(2, park_after_s=1000.0)
    a = h.fleet.new_order('INBOUND', inbound(['A1', 'A2'], ['S02', 'S03']), robot='AMR-01')
    h.run_until(lambda: a.current == 1 and h.drivers[0].busy)
    b = h.fleet.new_order('INBOUND', inbound(['B1'], ['S04']), robot='AMR-02', now=h.now)
    h.tick()
    h.drivers[0].inject_failure('시험')
    h.tick()
    assert a.state == OrderState.FAILED
    assert h.status('AMR-01').node == 'DOCK'
    h.run_until(lambda: b.state == OrderState.DONE, limit=200.0)
    parks = [o for o in h.fleet.orders() if o.kind == 'PARK' and o.robot == 'AMR-01']
    assert parks and parks[0].created < 100.0


# ---------------------------------------------------------------------------- 무작위 스트레스
@pytest.mark.parametrize('seed', [1, 2, 3])
def test_random_stress_invariants(seed):
    """3대 + 무작위 작업·취소·고장·일시정지·비상정지·자동 충전/주차 — 매 tick 불변식, 끝나면 모든 작업 종료."""
    cfg = SimConfig(battery_per_s=0.1, charge_per_s=1.0)
    h = Harness(3, cfg=cfg, park_after_s=8.0)
    fleet = h.fleet
    rng = random.Random(seed)

    def tidy(e):   # 사람이 '확인 필요' 박스를 치운 것으로 보고 트레이 비움 (다음 작업이 계속 실패하지 않게)
        if e.kind in ('failed', 'cancelled') and e.robot:
            fleet.driver(e.robot)._trays = [None] * 3

    fleet.subscribe(tidy)
    stats = {'done': 0, 'failed': 0, 'cancelled': 0}
    fleet.subscribe(lambda e: stats.__setitem__(e.kind, stats[e.kind] + 1) if e.kind in stats else None)

    def check():
        cur = {r: fleet.robot_order(r) for r in ('AMR-01', 'AMR-02', 'AMR-03')}
        ids = [o.id for o in cur.values() if o is not None]
        assert len(ids) == len(set(ids))                       # 한 작업은 한 대에만
        for r, o in cur.items():
            if o is not None:
                assert o.robot == r and o.state in (OrderState.ASSIGNED, OrderState.RUNNING)
        for zone in ('DOCK', 'CHARGER'):
            owner = fleet.zone_owner(zone)
            if owner is not None:
                o = cur[owner]
                assert o is not None and o.steps[o.current].zone == zone
        dock_steps = [d for d in h.drivers if d._step is not None and d._step.zone == 'DOCK']
        charge_steps = [d for d in h.drivers if d._step is not None and d._step.action == 'charge']
        assert len(dock_steps) <= 1 and len(charge_steps) <= 1
        for d in h.drivers:
            if d.busy:
                assert d.robot_id in fleet._inflight            # 드라이버가 일하면 FleetManager 도 안다
        for o in fleet.orders(active_only=True):
            assert o.state in (OrderState.QUEUED, OrderState.ASSIGNED, OrderState.RUNNING)

    box = 0
    paused_until = None
    for _ in range(4000):                                      # 800 s 동안 무작위 사건
        h.tick(0.2)
        check()
        if paused_until is not None and h.now >= paused_until:
            fleet.resume_all()
            paused_until = None
        r = rng.random()
        if r < 0.02:
            n = rng.randint(1, 3)
            boxes = [f'X{box + i}' for i in range(n)]
            box += n
            nodes = sorted(rng.sample(AISLE, n), key=lambda x: h.topo.distance('DOCK', x))
            fleet.new_order('INBOUND', inbound(boxes, nodes), priority=rng.choice([0, 0, 1]), now=h.now,
                            robot=rng.choice([None, None, None, 'AMR-02']))
        elif r < 0.03:
            fleet.new_order('MOVE', move(rng.choice(AISLE)), now=h.now)
        elif r < 0.036:
            active = fleet.orders(active_only=True)
            if active:
                fleet.cancel(rng.choice(active).id, '무작위 취소')
        elif r < 0.04:
            busy = [d for d in h.drivers if d.busy]
            if busy:
                rng.choice(busy).inject_failure('무작위 고장')
        elif r < 0.042 and paused_until is None:
            fleet.pause_all()
            paused_until = h.now + rng.uniform(1, 10)
        elif r < 0.0425 and paused_until is None:
            fleet.estop_all()
            paused_until = h.now + rng.uniform(1, 5)
    if paused_until is not None:
        fleet.resume_all()
    # 새 사건 없이 마무리: 모든 작업이 끝나야 한다 (교착 없음)
    h.run_until(lambda: not fleet.orders(active_only=True) and not any(d.busy for d in h.drivers),
                limit=3000.0, check=check)
    assert stats['done'] > 20 and stats['failed'] > 0 and stats['cancelled'] > 0
    assert any(o.kind == 'CHARGE' and o.state == OrderState.DONE for o in fleet.orders())
    assert all(fleet.zone_owner(z) is None for z in ('DOCK', 'CHARGER'))
    assert all(st.mode == RobotMode.IDLE for st in fleet.robots())
