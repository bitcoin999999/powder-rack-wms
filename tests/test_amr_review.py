"""적대적 리뷰 테스트 (src/amr). 각 테스트는 리뷰에서 찾은 결함 하나를 재현한다 — 고치기 전에는 실패했다.

결함 번호는 리뷰 보고서와 같다.
"""
import json
import logging
import math
import random
import socket
import threading
import time
from dataclasses import replace

import pytest

from amr.driver import AmrDriver
from amr.fleet import FleetManager
from amr.models import OrderState, RobotMode, RobotStatus, Step
from amr.sim_driver import SimulatedAmrDriver
from amr.tcp_json import TcpJsonDriver
from amr.topology import build_cell_topology
from amr.vda5050 import VDA_VERSION, Vda5050Driver
from core.config import RackConfig, RobotConfig, SimConfig

AISLE = [f'{f}{c:02d}' for f in 'NS' for c in range(1, 11)]


# ============================================================================ 공통 도구
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

    def status(self, rid):
        return self.fleet.driver(rid).status()


def inbound(boxes, nodes, zone='DOCK'):
    picks = [Step('', 'DOCK', 'pick', height_m=0.85, tray=i, ref=b, zone=zone, meta={'box': b})
             for i, b in enumerate(boxes)]
    drops = [Step('', n, 'drop', height_m=0.4 + 0.3 * (i % 4), tray=i, ref=f'{n[0]}-{n[1:]}-{i % 4 + 1}',
                  meta={'box': b}) for i, (b, n) in enumerate(zip(boxes, nodes))]
    return picks + drops


def dist(st, node):
    return math.hypot(st.x - node.x, st.y - node.y)


def exclusive_near(h, node_id, radius=0.8):
    """두 AMR 이 동시에 node 중심 radius 안에 있으면 실패 (도크·충전기·주차 자리 충돌)."""
    node = h.topo.nodes[node_id]

    def check():
        near = [d.robot_id for d in h.drivers if dist(d.status(), node) < radius]
        assert len(near) <= 1, f'{node_id} 에 두 대: {near} (t={h.now})'
    return check


# ============================================================================ D1 구간 중간에 멈춘 AMR 을 구역 점유로 보지 않음
def test_d1_dock_not_entered_while_failed_robot_stands_next_to_it():
    """도크 0.3 m 앞에서 고장 난 AMR(마지막 노드는 JSW)이 있는데 다른 AMR 을 도크로 보내면 충돌."""
    h = Harness(2, park_after_s=1000.0)
    dock = h.topo.nodes['DOCK']
    a = h.fleet.new_order('INBOUND', inbound(['A1'], ['S02']), robot='AMR-01')
    h.run_until(lambda: dist(h.status('AMR-01'), dock) < 0.3)
    assert h.status('AMR-01').node != 'DOCK'          # 아직 도착 전 (구간 위)
    h.drivers[0].inject_failure('시험 고장')
    b = h.fleet.new_order('INBOUND', inbound(['B1'], ['S03']), robot='AMR-02', now=h.now)
    h.run_until(lambda: b.state == OrderState.DONE, limit=300.0, check=exclusive_near(h, 'DOCK'))
    assert a.state == OrderState.FAILED


def test_d1_dock_not_entered_while_offline_robot_stands_next_to_it():
    h = Harness(2, park_after_s=1000.0)
    dock = h.topo.nodes['DOCK']
    h.fleet.new_order('INBOUND', inbound(['A1'], ['S02']), robot='AMR-01')
    h.run_until(lambda: dist(h.status('AMR-01'), dock) < 0.3)
    h.drivers[0].disconnect()                          # 도크 바로 앞에서 통신 끊김 (움직이지 못함)
    b = h.fleet.new_order('INBOUND', inbound(['B1'], ['S03']), robot='AMR-02', now=h.now)
    check = exclusive_near(h, 'DOCK')
    h.run(30.0, check=check)
    assert b.state != OrderState.DONE                  # 막힌 동안은 기다린다
    h.drivers[0].connect()
    h.run_until(lambda: b.state == OrderState.DONE, limit=300.0, check=check)


def test_d1_charger_not_entered_while_cancelled_robot_stands_next_to_it():
    cfg = SimConfig(charge_per_s=1.0)
    h = Harness(2, cfg=cfg, park_after_s=1000.0)
    charger = h.topo.nodes['CHARGER']
    h.drivers[0]._battery = 20.0
    h.drivers[1]._battery = 20.0
    h.tick()
    first = h.fleet.robot_order('AMR-01')
    assert first is not None and first.kind == 'CHARGE'
    h.run_until(lambda: dist(h.status('AMR-01'), charger) < 0.3)
    assert h.fleet.zone_owner('CHARGER') == 'AMR-01'
    h.fleet.cancel(first.id, '작업자 취소')
    check = exclusive_near(h, 'CHARGER')
    h.run_until(lambda: sum(1 for o in h.fleet.orders() if o.kind == 'CHARGE' and o.state == OrderState.DONE) >= 2,
                limit=600.0, check=check)


# ============================================================================ D2 주차 자리 바로 앞에 선 AMR 을 무시하고 같은 자리 배정
def test_d2_free_park_ignores_robot_stopped_next_to_park():
    h = Harness(2, starts=['N08', 'N07'], parks=3, park_after_s=2.0)
    park2 = h.topo.nodes['PARK2']
    mv = h.fleet.new_order('MOVE', [Step('', 'PARK2')], robot='AMR-01')
    h.run_until(lambda: dist(h.status('AMR-01'), park2) < 0.2)
    h.fleet.cancel(mv.id)
    check = exclusive_near(h, 'PARK2')
    h.run_until(lambda: all(not d.busy and h.fleet.robot_order(d.robot_id) is None
                            and min(dist(d.status(), h.topo.nodes[p]) for p in ('PARK1', 'PARK2', 'PARK3')) < 1e-6
                            for d in h.drivers), limit=120.0, check=check)
    assert h.status('AMR-01').node != h.status('AMR-02').node


# ============================================================================ D3 주차 자리를 막 떠나다 멈춘 AMR 은 다시 주차되지 않음
def test_d3_robot_stopped_just_after_leaving_park_is_parked_again():
    h = Harness(1, park_after_s=5.0)
    park1 = h.topo.nodes['PARK1']
    mv = h.fleet.new_order('MOVE', [Step('', 'S05')])
    h.run_until(lambda: dist(h.status('AMR-01'), park1) > 0.5)
    h.fleet.cancel(mv.id)
    h.tick()
    st = h.status('AMR-01')
    assert st.node == 'PARK1' and dist(st, park1) > 0.4   # 마지막 노드는 PARK1 이지만 통로 쪽 구간 위
    h.run(30.0)
    st = h.status('AMR-01')
    assert dist(st, park1) < 1e-6, f'주차 자리로 돌아가지 않음: ({st.x:.2f}, {st.y:.2f})'


# ============================================================================ D4 연결 끊긴 AMR 의 비켜서기 재시도가 매 tick 예외 로그
def test_d4_offline_robot_step_aside_does_not_flood_log(caplog):
    cfg = SimConfig(charge_per_s=0.013)
    h = Harness(3, cfg=cfg, starts=['DOCK', 'PARK2', 'CHARGER'], parks=3, park_after_s=1000.0)
    h.drivers[2]._battery = 20.0                       # AMR-03: 충전기에서 오래 충전 (CHARGER 잠금)
    h.tick()
    assert h.fleet.zone_owner('CHARGER') == 'AMR-03'
    a = h.fleet.new_order('MOVE', [Step('', 'CHARGER', zone='CHARGER')], robot='AMR-01', now=h.now)
    b = h.fleet.new_order('INBOUND', inbound(['B1'], ['S03']), robot='AMR-02', now=h.now)
    h.tick()
    h.drivers[0].disconnect()                          # 도크 위에서 CHARGER 를 기다리던 AMR-01 통신 끊김
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger='amr.fleet'):
        h.run(10.0)
    flood = [r for r in caplog.records if '비켜서기' in r.getMessage()]
    assert len(flood) <= 1, f'{len(flood)}건의 예외 로그'
    h.drivers[0].connect()
    h.run_until(lambda: b.state == OrderState.DONE, limit=300.0)
    assert a.state in (OrderState.ASSIGNED, OrderState.RUNNING)   # 여전히 충전기 대기


# ============================================================================ D11 주차 작업 무한 반복 (실제 AMR)
class StubAgv(AmrDriver):
    """좌표를 시험이 정하는 가짜 실제 AMR: 단계는 바로 끝나지만 움직이지 않는다 (reject=True 면 전송 거절)."""
    kind = 'stub'

    def __init__(self, rid, node, x, y, reject=False):
        super().__init__(rid)
        self.st = RobotStatus(rid, node=node, x=x, y=y)
        self.reject = reject
        self.sent = []

    def connect(self): ...
    def disconnect(self): ...
    def pause(self): ...
    def resume(self): ...
    def cancel(self): ...
    def estop(self): ...

    def send_step(self, step):
        if self.reject:
            raise RuntimeError('AGV 거절 (시험)')
        self.sent.append(step)
        self._emit('step_started', step.id)
        self._emit('step_done', step.id)

    def status(self):
        return replace(self.st, trays=list(self.st.trays))


def test_d11_agv_parked_slightly_off_is_not_parked_again_and_again():
    """정지 오차로 주차 자리에서 0.35 m 떨어져 선 AGV — 주차 작업을 한 번만 만든다 (회귀: 구간 위 판정 추가 뒤)."""
    topo = build_cell_topology(RackConfig(), parks=2)
    p, n = topo.nodes['PARK1'], topo.nodes['N10']
    length = math.hypot(n.x - p.x, n.y - p.y)
    agv = StubAgv('AGV-1', 'PARK1', p.x + (n.x - p.x) * 0.35 / length, p.y + (n.y - p.y) * 0.35 / length)
    fleet = FleetManager(topo, [agv], park_after_s=5.0)
    for k in range(600):
        fleet.tick(k * 0.2)
    parks = [o for o in fleet.orders() if o.kind == 'PARK']
    assert len(parks) <= 1, f'주차 작업 {len(parks)}건'


def test_d11_rejecting_agv_on_dock_does_not_create_park_order_every_tick():
    """도크 위에 선 AGV 가 주차 주문을 거절하는데 다른 AMR 이 도크를 기다리면, 원래 코드는 tick 마다 PARK 작업을 새로 만들었다."""
    topo = build_cell_topology(RackConfig(), parks=2)
    dock = topo.nodes['DOCK']
    agv = StubAgv('AGV-1', 'DOCK', dock.x, dock.y, reject=True)
    sim = SimulatedAmrDriver('AMR-02', topo, SimConfig(), 'PARK2')
    fleet = FleetManager(topo, [agv, sim], park_after_s=10.0)
    fleet.new_order('INBOUND', inbound(['B1'], ['S03']), robot='AMR-02')
    for k in range(100):                                  # 20 s
        fleet.tick(k * 0.2)
    parks = [o for o in fleet.orders() if o.kind == 'PARK' and o.robot == 'AGV-1']
    assert 1 <= len(parks) <= 3, f'주차 작업 {len(parks)}건'
    assert all(o.state == OrderState.FAILED for o in parks)


# ============================================================================ D5 충전 목표 ≤ 충전 시작 기준이면 CHARGE 작업 무한 생성
def test_d5_charge_thresholds_validated():
    topo = build_cell_topology(RackConfig())
    with pytest.raises(ValueError):
        FleetManager(topo, [], charge_below=50.0, charge_target=40.0)
    with pytest.raises(ValueError):
        FleetManager(topo, [], charge_below=25.0, charge_target=25.0)
    with pytest.raises(ValueError):
        FleetManager(topo, [], charge_below=25.0, charge_target=120.0)
    FleetManager(topo, [], charge_below=0.0, charge_target=100.0)   # 경계값은 허용


# ============================================================================ VDA 5050 도구
TOPO = build_cell_topology(RackConfig(), parks=2)
PREFIX = 'uagv/v2/ACME/SN-7'


class FakeClient:
    def __init__(self):
        self.published = []
        self.on_message = None

    def connect(self, host, port): ...
    def subscribe(self, topic_name, qos=0): ...
    def loop_start(self): ...
    def loop_stop(self): ...
    def disconnect(self): ...

    def publish(self, topic_name, payload, qos=0, retain=False):
        self.published.append((topic_name, json.loads(payload), qos))

    def instants(self):
        return [m['actions'][0]['actionType'] for t, m, _ in self.published if t.endswith('/instantActions')]


def online_driver():
    client = FakeClient()
    cfg = RobotConfig(id='AMR-V1', driver='vda5050', host='10.0.0.5', port=1883, manufacturer='ACME', serial='SN-7')
    drv = Vda5050Driver(cfg, TOPO, client_factory=lambda: client)
    drv.connect()
    drv.handle_message(f'{PREFIX}/connection', {'connectionState': 'ONLINE'})
    drv.poll()
    return drv, client


def state(**kw):
    msg = {'headerId': 1, 'timestamp': '2026-10-20T00:00:00.000Z', 'version': VDA_VERSION, 'manufacturer': 'ACME',
           'serialNumber': 'SN-7', 'orderId': '', 'orderUpdateId': 0, 'lastNodeId': 'PARK1', 'lastNodeSequenceId': 0,
           'driving': False, 'paused': False, 'operatingMode': 'AUTOMATIC', 'nodeStates': [], 'edgeStates': [],
           'actionStates': [], 'errors': [], 'loads': [],
           'agvPosition': {'x': 3.7, 'y': -2.17, 'theta': 0.5, 'mapId': 'cell', 'positionInitialized': True},
           'batteryState': {'batteryCharge': 77.5, 'charging': False}}
    msg.update(kw)
    return msg


def events(drv):
    return [(e.kind, e.step_id, e.detail) for e in drv.poll()]


def last_order(client):
    msg = [m for t, m, _ in client.published if t.endswith('/order')][-1]
    actions = msg['nodes'][-1]['actions']
    return msg['orderId'], (actions[0]['actionId'] if actions else None)


# ============================================================================ D6 VDA 5050 startCharging FINISHED 를 충전 완료로 착각
def test_d6_vda_charge_step_runs_until_target_then_stops_charging():
    drv, client = online_driver()
    drv.send_step(Step('O0009-S1', 'CHARGER', 'charge', path=['JNE', 'CHARGER'], meta={'target': 90.0}))
    events(drv)
    oid, aid = last_order(client)
    started = [{'actionId': aid, 'actionType': 'startCharging', 'actionStatus': 'FINISHED'}]
    # VDA 5050: startCharging FINISHED = '충전이 시작됨'. 목표(90 %)까지는 단계가 끝나지 않아야 한다.
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='CHARGER', actionStates=started,
                                                batteryState={'batteryCharge': 30.0, 'charging': True}))
    assert events(drv) == [] and drv.busy
    assert drv.status().mode == RobotMode.CHARGING
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='CHARGER', actionStates=started,
                                                batteryState={'batteryCharge': 89.9, 'charging': True}))
    assert events(drv) == [] and drv.busy
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='CHARGER', actionStates=started,
                                                batteryState={'batteryCharge': 90.0, 'charging': True}))
    assert events(drv) == [('step_done', 'O0009-S1', '')]
    assert client.instants()[-1] == 'stopCharging'      # 목표에 닿으면 충전을 멈춘다
    assert not drv.busy


def test_d6_vda_cancel_while_charging_stops_charging():
    drv, client = online_driver()
    drv.send_step(Step('O0010-S1', 'CHARGER', 'charge', path=['CHARGER'], meta={'target': 90.0}))
    events(drv)
    oid, aid = last_order(client)
    drv.handle_message(f'{PREFIX}/state', state(
        orderId=oid, lastNodeId='CHARGER', batteryState={'batteryCharge': 40.0, 'charging': True},
        actionStates=[{'actionId': aid, 'actionType': 'startCharging', 'actionStatus': 'FINISHED'}]))
    assert events(drv) == []
    drv.cancel()
    assert 'stopCharging' in client.instants()
    assert events(drv) == [('step_failed', 'O0010-S1', 'cancelled')]
    assert not drv.busy


def test_d6_vda_charge_under_fleet_keeps_order_running_until_target():
    client = FakeClient()
    cfg = RobotConfig(id='AMR-V1', driver='vda5050', manufacturer='ACME', serial='SN-7')
    drv = Vda5050Driver(cfg, TOPO, client_factory=lambda: client)
    drv.connect()
    fleet = FleetManager(TOPO, [drv])
    drv.handle_message(f'{PREFIX}/state', state(lastNodeId='JNE', batteryState={'batteryCharge': 20.0}))
    fleet.tick(0.0)
    charge = fleet.robot_order('AMR-V1')
    assert charge is not None and charge.kind == 'CHARGE' and charge.state == OrderState.RUNNING
    oid, aid = last_order(client)
    done = [{'actionId': aid, 'actionType': 'startCharging', 'actionStatus': 'FINISHED'}]
    for t, bat in enumerate((21.0, 50.0, 89.0), start=1):
        drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='CHARGER', actionStates=done,
                                                    batteryState={'batteryCharge': bat, 'charging': True}))
        fleet.tick(float(t))
        assert charge.state == OrderState.RUNNING, f'{bat} % 에서 충전 작업이 끝나 버림'
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='CHARGER', actionStates=done,
                                                batteryState={'batteryCharge': 90.5, 'charging': True}))
    fleet.tick(5.0)
    assert charge.state == OrderState.DONE
    assert fleet.robot_order('AMR-V1') is None or fleet.robot_order('AMR-V1').kind != 'CHARGE'


# ============================================================================ D7 연결 끊긴(OFFLINE) AGV 에도 주문을 보내 영원히 대기
def test_d7_vda_send_step_refused_while_agv_offline():
    drv, client = online_driver()
    drv.handle_message(f'{PREFIX}/connection', {'connectionState': 'CONNECTIONBROKEN'})
    events(drv)
    n = len(client.published)
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O0011-S1', 'N03', path=['N01', 'N02', 'N03']))
    assert not drv.busy and len(client.published) == n   # 보내지 않았다 (QoS 0 주문이 사라져 영원히 대기하는 것 방지)
    assert events(drv) == []


def test_d7_vda_offline_between_steps_fails_order_instead_of_hanging():
    client = FakeClient()
    drv = Vda5050Driver(RobotConfig(id='AMR-V1', driver='vda5050', manufacturer='ACME', serial='SN-7'), TOPO,
                        client_factory=lambda: client)
    drv.connect()
    fleet = FleetManager(TOPO, [drv])
    drv.handle_message(f'{PREFIX}/state', state(lastNodeId='N01'))
    o = fleet.new_order('MOVE', [Step('', 'N03'), Step('', 'N05')])
    fleet.tick(0.0)
    oid, _ = last_order(client)
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='N03'))
    drv.handle_message(f'{PREFIX}/connection', {'connectionState': 'OFFLINE'})
    fleet.tick(0.1)
    assert o.state == OrderState.FAILED and '단계 전송 실패' in o.error


# ============================================================================ D8 이전 주문의 거절 오류가 남아 있으면 다음 주문까지 즉시 실패
def test_d8_vda_stale_rejection_error_does_not_fail_next_order():
    drv, client = online_driver()
    reject = [{'errorType': 'validationError', 'errorDescription': 'unknown node', 'errorLevel': 'WARNING'}]
    drv.send_step(Step('O0012-S1', 'S05', path=['S04', 'S05']))
    events(drv)
    drv.handle_message(f'{PREFIX}/state', state(orderId='OLD', errors=reject))
    assert events(drv)[0][0] == 'step_failed'
    drv.send_step(Step('O0012-S2', 'S05', path=['S04', 'S05']))
    events(drv)
    oid, _ = last_order(client)
    # AGV 가 새 주문을 처리하기 전에 낸 주기 state: 옛 주문 ID + 옛 오류 그대로
    drv.handle_message(f'{PREFIX}/state', state(orderId='OLD', errors=reject))
    assert events(drv) == [] and drv.busy
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='S05'))
    assert events(drv) == [('step_done', 'O0012-S2', '')]


def test_d8_vda_rejection_referencing_our_order_still_fails():
    drv, client = online_driver()
    reject = [{'errorType': 'validationError', 'errorDescription': 'unknown node', 'errorLevel': 'WARNING'}]
    drv.handle_message(f'{PREFIX}/state', state(orderId='OLD', errors=reject))   # 보내기 전부터 있던 오류
    drv.send_step(Step('O0013-S1', 'S05', path=['S04', 'S05']))
    events(drv)
    oid, _ = last_order(client)
    drv.handle_message(f'{PREFIX}/state', state(orderId='OLD', errors=[dict(
        reject[0], errorReferences=[{'referenceKey': 'orderId', 'referenceValue': oid}])]))
    (ev,) = events(drv)
    assert ev[0] == 'step_failed' and 'validationError' in ev[2]


# ============================================================================ D9 VDA 5050 취소 응답을 기다리지 않아 다음 주문이 거절됨
def test_d9_vda_cancel_waits_for_agv_confirmation():
    drv, client = online_driver()
    drv.send_step(Step('O0014-S1', 'N05', path=['N03', 'N04', 'N05']))
    events(drv)
    oid, _ = last_order(client)
    drv.cancel()
    assert client.instants()[-1] == 'cancelOrder'
    # AGV 가 아직 멈추는 중: 단계는 끝나지 않았고 새 단계도 받지 않는다
    assert events(drv) == [] and drv.busy
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O0014-S2', 'N01'))
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='N04', driving=True,
                                                nodeStates=[{'nodeId': 'N05', 'sequenceId': 4, 'released': True}]))
    assert events(drv) == [] and drv.busy
    cancel_id = [m for t, m, _ in client.published if t.endswith('/instantActions')][-1]['actions'][0]['actionId']
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='N04', actionStates=[
        {'actionId': cancel_id, 'actionType': 'cancelOrder', 'actionStatus': 'FINISHED'}]))
    assert events(drv) == [('step_failed', 'O0014-S1', 'cancelled')]
    assert not drv.busy


def test_d9_vda_cancel_done_when_order_cleared_or_action_finished():
    drv, client = online_driver()
    # (가) cancelOrder 상태 보고가 없어도 주문이 비워지면(nodeStates·edgeStates 없음, 정지) 취소 완료
    drv.send_step(Step('O0015-S1', 'N05', path=['N03', 'N04', 'N05']))
    events(drv)
    oid, _ = last_order(client)
    drv.cancel()
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='N04'))
    assert events(drv) == [('step_failed', 'O0015-S1', 'cancelled')]
    # (나) 취소가 닿기 전에 집기가 이미 끝났으면 실제 결과(step_done)를 알린다
    drv.send_step(Step('O0015-S2', 'DOCK', 'pick', height_m=0.85, tray=0, ref='B1', path=['DOCK']))
    events(drv)
    oid, aid = last_order(client)
    drv.cancel()
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='DOCK', actionStates=[
        {'actionId': aid, 'actionType': 'pick', 'actionStatus': 'FINISHED'}]))
    assert events(drv) == [('step_done', 'O0015-S2', '')]


def test_d9_vda_cancel_times_out_and_offline_cancel_is_immediate():
    drv, client = online_driver()
    drv.send_step(Step('O0016-S1', 'N05', path=['N03', 'N04', 'N05']))
    events(drv)
    drv.cancel()
    drv.tick(10.0, 10.0)
    assert drv.busy
    drv.tick(40.0, 30.0)                                  # AGV 가 끝내 답하지 않음 → 시간 초과로 정리
    evs = events(drv)
    assert ('step_failed', 'O0016-S1', 'cancelled') in evs
    assert any(k == 'error' for k, _, _ in evs) and not drv.busy
    # AGV 가 OFFLINE 이면 기다릴 수 없으므로 바로 취소
    drv.send_step(Step('O0016-S2', 'N05', path=['N03', 'N04', 'N05']))
    events(drv)
    drv.handle_message(f'{PREFIX}/connection', {'connectionState': 'OFFLINE'})
    events(drv)
    drv.cancel()
    assert events(drv) == [('step_failed', 'O0016-S2', 'cancelled')]


def test_d9_vda_park_preemption_sends_new_order_only_after_cancel_confirmed():
    client = FakeClient()
    drv = Vda5050Driver(RobotConfig(id='AMR-V1', driver='vda5050', manufacturer='ACME', serial='SN-7'), TOPO,
                        client_factory=lambda: client)
    drv.connect()
    fleet = FleetManager(TOPO, [drv], park_after_s=0.0)
    drv.handle_message(f'{PREFIX}/state', state(lastNodeId='S05', agvPosition={'x': 1.71, 'y': 1.17}))
    fleet.tick(0.0)
    park = fleet.robot_order('AMR-V1')
    assert park is not None and park.kind == 'PARK' and park.state == OrderState.RUNNING
    park_oid, _ = last_order(client)
    real = fleet.new_order('MOVE', [Step('', 'S07')], now=0.1)
    fleet.tick(0.1)                                       # 주차 취소 요청 (cancelOrder)
    fleet.tick(0.2)
    orders = [m for t, m, _ in client.published if t.endswith('/order')]
    assert len(orders) == 1, 'AGV 가 취소를 끝내기 전에 새 주문을 보냄 (VDA 5050 AGV 는 거절)'
    drv.handle_message(f'{PREFIX}/state', state(orderId=park_oid, lastNodeId='N05'))   # 취소 완료
    fleet.tick(0.3)
    fleet.tick(0.4)
    assert park.state == OrderState.CANCELLED and real.state == OrderState.RUNNING
    assert len([m for t, m, _ in client.published if t.endswith('/order')]) == 2


# ============================================================================ D15 VDA 5050 잘못된 state 값 하나가 MQTT 수신 콜백에서 예외
class FakeMsg:
    def __init__(self, topic_name, payload):
        self.topic = topic_name
        self.payload = json.dumps(payload).encode('utf-8')


@pytest.mark.parametrize('bad', [
    {'batteryState': {'batteryCharge': None}},
    {'agvPosition': {'x': 'abc', 'y': 0.0}},
    {'loads': ['B1']},
    {'actionStates': ['RUNNING']},
])
def test_d15_vda_malformed_state_does_not_raise_in_mqtt_thread(bad):
    drv, client = online_driver()
    drv.handle_message(f'{PREFIX}/state', state(lastNodeId='N02'))
    events(drv)
    drv.send_step(Step('O0017-S1', 'N05', path=['N02', 'N03', 'N04', 'N05']))
    events(drv)
    # paho 1.x 는 on_message 예외를 다시 던져 수신 루프 스레드가 죽는다 → 이후 state 를 못 받음
    client.on_message(client, None, FakeMsg(f'{PREFIX}/state', state(lastNodeId='N04', **bad)))
    evs = events(drv)
    assert [k for k, _, _ in evs] == ['error'], evs
    assert drv.status().node == 'N02' and drv.busy          # 잘못된 메시지는 상태를 바꾸지 않는다
    oid, _ = last_order(client)
    drv.handle_message(f'{PREFIX}/state', state(orderId=oid, lastNodeId='N05'))
    assert events(drv) == [('step_done', 'O0017-S1', '')]


# ============================================================================ D10 TCP/JSON 잘못된 필드 값 하나로 수신 스레드가 죽고 연결이 끊김
class OneShotServer:
    """접속하면 주어진 줄들을 보내고, 연결은 열어 둔다."""

    def __init__(self, lines):
        self.lines = lines
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        conn, _ = self.sock.accept()
        with conn:
            for line in self.lines:
                conn.sendall((json.dumps(line) + '\n').encode('utf-8'))
            self.stop.wait(5.0)

    def close(self):
        self.stop.set()
        self.sock.close()


def test_d10_tcp_malformed_value_keeps_connection_and_status_atomic():
    srv = OneShotServer([
        {'type': 'status', 'x': 1.0, 'y': 2.0, 'battery': 80.0, 'node': 'N01', 'mode': 'idle'},
        {'type': 'status', 'x': 9.0, 'battery': None, 'node': 'N09'},     # battery 값이 잘못됨
        {'type': 'status', 'x': 3.0, 'battery': 70.0, 'node': 'N03'},
    ])
    drv = TcpJsonDriver(RobotConfig(id='AMR-T1', driver='tcp_json', host='127.0.0.1', port=srv.port))
    try:
        drv.connect()
        got = []
        end = time.monotonic() + 3.0
        while time.monotonic() < end and drv.status().node != 'N03':
            got += drv.poll()
            time.sleep(0.01)
        got += drv.poll()
        st = drv.status()
        assert st.connected and st.mode != RobotMode.OFFLINE, f'연결이 끊김: {[(e.kind, e.detail) for e in got]}'
        assert st.node == 'N03' and st.battery == 70.0 and st.x == 3.0
        assert any(e.kind == 'error' for e in got)
        assert not any(e.kind == 'disconnected' for e in got)
    finally:
        drv.disconnect()
        srv.close()


def test_d10_tcp_bad_message_does_not_half_update_status():
    srv = OneShotServer([
        {'type': 'status', 'x': 1.0, 'y': 2.0, 'battery': 80.0, 'node': 'N01', 'mode': 'idle'},
        {'type': 'status', 'x': 9.0, 'y': 9.0, 'battery': 'abc', 'node': 'N09'},
    ])
    drv = TcpJsonDriver(RobotConfig(id='AMR-T1', driver='tcp_json', host='127.0.0.1', port=srv.port))
    try:
        drv.connect()
        end = time.monotonic() + 3.0
        while time.monotonic() < end and not any(e.kind == 'error' for e in drv.poll()):
            time.sleep(0.01)
        st = drv.status()
        assert (st.x, st.y, st.node, st.battery) == (1.0, 2.0, 'N01', 80.0)
    finally:
        drv.disconnect()
        srv.close()


# ============================================================================ D12 잘못된 단계 값으로 send_step 이 예외 → 드라이버가 영원히 busy
@pytest.mark.parametrize('bad', [
    Step('', 'CHARGER', 'charge', meta={'target': 'abc'}),
    Step('', 'DOCK', 'pick', tray='0', ref='B1'),
])
def test_d12_sim_bad_step_value_does_not_leave_robot_busy_forever(bad):
    h = Harness(1, starts=['N05'], park_after_s=1000.0)
    o = h.fleet.new_order('MOVE', [bad])
    h.tick()
    assert o.state == OrderState.FAILED and '단계 전송 실패' in o.error
    drv = h.drivers[0]
    assert not drv.busy, '드라이버가 실패한 단계를 쥐고 있음 → 이 AMR 은 다시는 배차되지 않는다'
    assert 'AMR-01' in h.fleet.idle_robots()
    ok = h.fleet.new_order('MOVE', [Step('', 'N06')], now=h.now)
    h.run_until(lambda: ok.state == OrderState.DONE, limit=60.0)


def test_d12_tcp_encode_error_does_not_leave_driver_busy(monkeypatch):
    import amr.tcp_json as tcp

    srv = OneShotServer([])
    drv = TcpJsonDriver(RobotConfig(id='AMR-T1', driver='tcp_json', host='127.0.0.1', port=srv.port))
    try:
        drv.connect()

        def broken(step, robot_id):
            raise KeyError('업체 매핑표에 없는 노드')
        monkeypatch.setattr(tcp, 'encode_step', broken)
        with pytest.raises(RuntimeError):
            drv.send_step(Step('O1-S1', 'N01'))
        assert not drv.busy
        assert not any(e.kind == 'step_started' for e in drv.poll())
    finally:
        drv.disconnect()
        srv.close()


# ============================================================================ D13 충전 속도 0 이면 충전 단계가 끝나지 않아 충전기를 영원히 잡음
@pytest.mark.parametrize('kw', [{'charge_per_s': 0.0}, {'battery_per_s': -0.01}, {'battery_per_m': -1.0},
                                {'fork_time_s': -1.0}])
def test_d13_sim_rejects_config_that_hangs_or_overcharges(kw):
    topo = build_cell_topology(RackConfig())
    with pytest.raises(ValueError):
        SimulatedAmrDriver('AMR-01', topo, SimConfig(**kw), 'PARK1')


# ============================================================================ D14 작업 오류 문구가 드라이버 코드('estop'·'battery') 그대로
def test_d14_order_error_text_is_korean_for_driver_codes():
    h = Harness(2, cfg=SimConfig(battery_per_s=0.0, battery_per_m=10.0))
    a = h.fleet.new_order('MOVE', [Step('', 'S10')], robot='AMR-01')
    h.run(1.0)
    h.fleet.estop_all()
    h.tick()
    assert a.state == OrderState.FAILED and a.error == '비상정지'
    h.fleet.resume_all()
    h.tick()
    h.drivers[1]._battery = 30.0                          # 3 m 정도 가면 방전
    b = h.fleet.new_order('MOVE', [Step('', 'S10')], robot='AMR-02', now=h.now)
    h.run_until(lambda: b.state == OrderState.FAILED, limit=60.0)
    assert b.error == '배터리 방전'
    failed = [e for e in h.log if e.order is b and e.kind == 'failed']
    assert failed and failed[0].detail == '배터리 방전'


# ============================================================================ 긴 무작위 시뮬레이션 (3대, 수백 작업)
def _stress(seed: int, ticks: int = 12000, rate: float = 1.0):
    """3대 + 무작위 입고·이동·취소·고장·일시정지·비상정지·통신 끊김·배터리 저하. 매 tick 불변식 검사."""
    rng = random.Random(seed)
    cfg = SimConfig(battery_per_s=0.02, charge_per_s=1.0, battery_per_m=0.05)
    h = Harness(3, cfg=cfg, parks=3, park_after_s=8.0)
    fleet, drivers = h.fleet, h.drivers
    rids = [d.robot_id for d in drivers]
    dock, charger = h.topo.nodes['DOCK'], h.topo.nodes['CHARGER']
    parks = [h.topo.nodes[f'PARK{i}'] for i in (1, 2, 3)]

    def tidy(e):   # 사람이 '확인 필요' 박스를 내려놓음
        if e.kind in ('failed', 'cancelled') and e.robot and e.order.kind == 'INBOUND':
            fleet.driver(e.robot)._trays = [None] * 3
    fleet.subscribe(tidy)

    progress = {r: (None, 0.0) for r in rids}

    def check():
        cur = {r: fleet.robot_order(r) for r in rids}
        ids = [o.id for o in cur.values() if o is not None]
        assert len(ids) == len(set(ids))
        for r, o in cur.items():
            if o is not None:
                assert o.robot == r and o.state in (OrderState.ASSIGNED, OrderState.RUNNING)
        for zone in ('DOCK', 'CHARGER'):
            owner = fleet.zone_owner(zone)
            if owner is not None:   # 구역 주인은 그 구역 단계를 하고 있는 AMR 뿐
                o = cur[owner]
                assert o is not None and o.current < len(o.steps) and o.steps[o.current].zone == zone
        boxes = [b for d in drivers for b in d.status().trays if b is not None]
        assert len(boxes) == len(set(boxes)), '한 박스가 두 트레이에'
        for d in drivers:
            st = d.status()
            assert 0.0 <= st.battery <= 100.0
            if d.busy:
                assert d.robot_id in fleet._inflight
        for node in [dock, charger] + parks:
            near = [d.robot_id for d in drivers if dist(d.status(), node) < 0.3]
            assert len(near) <= 1, f'{node.id} 충돌 {near} t={h.now}'
        for o in fleet.orders():
            if o.started is not None:
                assert o.started >= o.created - 1e-9
            if o.finished is not None and o.started is not None:
                assert o.finished >= o.started - 1e-9
        for r in rids:   # 일을 쥐고 600 s 동안 아무 진척 없는 AMR 없음
            o = cur[r]
            st = fleet.driver(r).status()
            key = (o.id if o else None, o.current if o else None, fleet._inflight.get(r),
                   round(st.x, 3), round(st.y, 3), st.mode)
            last, since = progress[r]
            if key != last:
                progress[r] = (key, h.now)
            else:
                assert o is None or h.now - since < 600.0 or st.mode in (RobotMode.ERROR, RobotMode.OFFLINE), \
                    f'{r} 정체: {o.id} {o.kind} 단계 {o.current}'

    box = 0
    paused_until = None
    offline_until: dict[str, float] = {}
    for _ in range(ticks):
        h.tick(0.2)
        check()
        if paused_until is not None and h.now >= paused_until:
            fleet.resume_all()
            paused_until = None
        for r, t in list(offline_until.items()):
            if h.now >= t:
                fleet.driver(r).connect()
                del offline_until[r]
        x = rng.random() / rate
        if x < 0.006:
            n = rng.randint(1, 3)
            names = [f'X{box + i}' for i in range(n)]
            box += n
            nodes = sorted(rng.sample(AISLE, n), key=lambda q: h.topo.distance('DOCK', q))
            fleet.new_order('INBOUND', inbound(names, nodes), priority=rng.choice([0, 0, 1]), now=h.now,
                            robot=rng.choice([None, None, None, rng.choice(rids)]))
        elif x < 0.009:
            target = rng.choice(AISLE + ['DOCK', 'CHARGER', 'JNW', 'JSE'])
            zone = target if target in ('DOCK', 'CHARGER') else None
            fleet.new_order('MOVE', [Step('', target, zone=zone)], now=h.now)
        elif x < 0.0105:
            active = fleet.orders(active_only=True)
            if active:
                fleet.cancel(rng.choice(active).id, '무작위 취소')
        elif x < 0.0115:
            busy = [d for d in drivers if d.busy]
            if busy:
                rng.choice(busy).inject_failure('무작위 고장')
        elif x < 0.012 and paused_until is None:
            fleet.pause_all()
            paused_until = h.now + rng.uniform(1, 10)
        elif x < 0.0122 and paused_until is None:
            fleet.estop_all()
            paused_until = h.now + rng.uniform(1, 5)
        elif x < 0.0125:
            d = rng.choice(drivers)
            if d.robot_id not in offline_until:
                d.disconnect()
                offline_until[d.robot_id] = h.now + rng.uniform(1, 20)
        elif x < 0.013:
            d = rng.choice(drivers)
            d._battery = min(d._battery, rng.uniform(5, 30))
    if paused_until is not None:
        fleet.resume_all()
    for r in list(offline_until):
        fleet.driver(r).connect()
    for d in drivers:
        d._trays = [None] * 3
    settled = lambda: not fleet.orders(active_only=True) and not any(d.busy for d in drivers)  # noqa: E731
    h.run_until(settled, limit=40000.0, check=check)
    h.run(60.0, check=check)                       # 쉬는 동안 자동 충전·주차가 생겨도
    h.run_until(settled, limit=2000.0, check=check)   # 다시 다 끝나야 한다
    states = [o.state for o in fleet.orders()]
    assert all(s in (OrderState.DONE, OrderState.FAILED, OrderState.CANCELLED) for s in states)
    assert all(fleet.zone_owner(z) is None for z in ('DOCK', 'CHARGER'))
    return states


@pytest.mark.parametrize('seed', [7, 10, 12])
def test_long_random_fleet_simulation_high_load(seed):
    """고부하(작업이 쌓임) — 리뷰 중 이 시드들에서 도크·충전기 충돌이 났다 (D1)."""
    states = _stress(seed, ticks=8000, rate=4.0)
    assert states.count(OrderState.DONE) > 100


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_long_random_fleet_simulation(seed):
    states = _stress(seed)
    assert states.count(OrderState.DONE) > 100
    assert OrderState.FAILED in states and OrderState.CANCELLED in states
