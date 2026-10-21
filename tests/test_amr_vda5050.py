import json
import sys

import pytest

from amr.fleet import FleetManager
from amr.models import OrderState, RobotMode, Step
from amr.topology import build_cell_topology
from amr.vda5050 import VDA_VERSION, Vda5050Driver, build_instant_action, build_order, parse_state, topic
from core.config import RackConfig, RobotConfig

TOPO = build_cell_topology(RackConfig(), parks=2)
PREFIX = 'uagv/v2/ACME/SN-7'


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []
        self.connected_to = None
        self.on_message = None
        self.looping = False

    def connect(self, host, port):
        self.connected_to = (host, port)

    def subscribe(self, topic_name, qos=0):
        self.subscribed.append((topic_name, qos))

    def publish(self, topic_name, payload, qos=0, retain=False):
        self.published.append((topic_name, json.loads(payload), qos))

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        self.connected_to = None


class FakeMsg:
    def __init__(self, topic_name, payload):
        self.topic = topic_name
        self.payload = json.dumps(payload).encode('utf-8')


def make_driver():
    client = FakeClient()
    cfg = RobotConfig(id='AMR-V1', driver='vda5050', host='10.0.0.5', port=1883, manufacturer='ACME', serial='SN-7')
    drv = Vda5050Driver(cfg, TOPO, client_factory=lambda: client)
    return drv, client


def online_driver():
    """연결 + AMR ONLINE 까지 마친 드라이버 (이벤트 비움)."""
    drv, client = make_driver()
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


# ---------------------------------------------------------------------------- 순수 함수
def test_topic():
    assert topic('uagv', 'ACME', 'SN-7', 'order') == 'uagv/v2/ACME/SN-7/order'
    assert topic('uagv', 'ACME', 'SN-7', 'state', major='v1') == 'uagv/v1/ACME/SN-7/state'


def test_build_order_structure_with_drop():
    path = TOPO.shortest_path('PARK1', 'N07')
    step = Step('O0003-S4', 'N07', 'drop', height_m=1.0, tray=2, ref='N-07-3', path=path, meta={'box': 'B1'})
    msg = build_order(step, TOPO, 42, 'ORD-1', 'ACME', 'SN-7', '2026-10-20T01:02:03.000Z')
    assert {k: msg[k] for k in ('headerId', 'timestamp', 'version', 'manufacturer', 'serialNumber', 'orderId',
                                'orderUpdateId')} == {
        'headerId': 42, 'timestamp': '2026-10-20T01:02:03.000Z', 'version': '2.0.0', 'manufacturer': 'ACME',
        'serialNumber': 'SN-7', 'orderId': 'ORD-1', 'orderUpdateId': 0}
    nodes, edges = msg['nodes'], msg['edges']
    assert [n['nodeId'] for n in nodes] == path
    assert [n['sequenceId'] for n in nodes] == list(range(0, 2 * len(path), 2))   # 짝수
    assert len(edges) == len(nodes) - 1
    assert [e['sequenceId'] for e in edges] == list(range(1, 2 * len(path) - 1, 2))   # 홀수
    for i, e in enumerate(edges):
        assert e['startNodeId'] == path[i] and e['endNodeId'] == path[i + 1]
        assert e['released'] is True and e['actions'] == [] and e['edgeId']
    assert all(n['released'] is True for n in nodes)
    assert all(n['actions'] == [] for n in nodes[:-1])          # 동작은 마지막 노드에만
    pos = nodes[-1]['nodePosition']
    assert (pos['x'], pos['y'], pos['mapId']) == (TOPO.nodes['N07'].x, TOPO.nodes['N07'].y, 'cell')
    assert 'theta' in pos
    (action,) = nodes[-1]['actions']
    assert action['actionType'] == 'drop'
    assert action['actionId'] == 'ORD-1-O0003-S4'
    assert action['blockingType'] == 'HARD'
    params = {p['key']: p['value'] for p in action['actionParameters']}
    assert params == {'stationType': 'rack', 'loadType': '완분박스', 'loadId': 'N-07-3', 'height': 1.0, 'tray': 2}
    json.dumps(msg, ensure_ascii=False)   # 직렬화 가능


def test_build_order_pick_move_charge():
    pick = build_order(Step('O1-S1', 'DOCK', 'pick', height_m=0.85, tray=0, ref='BOX-9', path=['DOCK']),
                       TOPO, 1, 'A', 'M', 'S', 't')
    assert len(pick['nodes']) == 1 and pick['edges'] == []
    params = {p['key']: p['value'] for p in pick['nodes'][0]['actions'][0]['actionParameters']}
    assert pick['nodes'][0]['actions'][0]['actionType'] == 'pick'
    assert params['stationType'] == 'conveyor' and params['loadId'] == 'BOX-9' and params['tray'] == 0
    mv = build_order(Step('O1-S2', 'S03', path=['S01', 'S02', 'S03']), TOPO, 2, 'B', 'M', 'S', 't')
    assert all(n['actions'] == [] for n in mv['nodes']) and len(mv['edges']) == 2
    ch = build_order(Step('O1-S3', 'CHARGER', 'charge', path=['JNE', 'CHARGER']), TOPO, 3, 'C', 'M', 'S', 't')
    assert ch['nodes'][-1]['actions'][0]['actionType'] == 'startCharging'
    no_path = build_order(Step('O1-S4', 'N02'), TOPO, 4, 'D', 'M', 'S', 't')
    assert [n['nodeId'] for n in no_path['nodes']] == ['N02']


def test_build_instant_action():
    msg = build_instant_action('cancelOrder', 7, 'ACME', 'SN-7', 'ts')
    assert {k: msg[k] for k in ('headerId', 'timestamp', 'version', 'manufacturer', 'serialNumber')} == {
        'headerId': 7, 'timestamp': 'ts', 'version': VDA_VERSION, 'manufacturer': 'ACME', 'serialNumber': 'SN-7'}
    (a,) = msg['actions']
    assert a['actionType'] == 'cancelOrder' and a['actionId'] and a['blockingType'] in ('NONE', 'SOFT', 'HARD')
    assert a['actionParameters'] == []
    msg = build_instant_action('initPosition', 8, 'ACME', 'SN-7', 'ts', params={'x': 1.0, 'lastNodeId': 'PARK1'})
    assert msg['actions'][0]['actionParameters'] == [{'key': 'x', 'value': 1.0}, {'key': 'lastNodeId', 'value': 'PARK1'}]


def test_parse_state():
    st = parse_state(state(loads=[{'loadId': 'B1', 'loadPosition': '0'}, {'loadId': 'B3', 'loadPosition': '2'}]),
                     'AMR-V1')
    assert st.robot_id == 'AMR-V1'
    assert (st.x, st.y, st.heading) == (3.7, -2.17, 0.5)
    assert st.battery == 77.5 and st.node == 'PARK1'
    assert st.mode == RobotMode.IDLE and st.connected
    assert st.trays == ['B1', None, 'B3']
    assert parse_state(state(driving=True), 'R').mode == RobotMode.MOVING
    assert parse_state(state(paused=True, driving=True), 'R').mode == RobotMode.PAUSED
    assert parse_state(state(batteryState={'batteryCharge': 50, 'charging': True}), 'R').mode == RobotMode.CHARGING
    running = state(actionStates=[{'actionId': 'x', 'actionType': 'pick', 'actionStatus': 'RUNNING'}])
    assert parse_state(running, 'R').mode == RobotMode.LOADING
    err = parse_state(state(errors=[{'errorType': 'laserBlocked', 'errorDescription': '전방 장애물',
                                     'errorLevel': 'FATAL'}]), 'R')
    assert err.mode == RobotMode.ERROR and 'laserBlocked' in err.message and '전방 장애물' in err.message
    warn = parse_state(state(errors=[{'errorType': 'lowBattery', 'errorLevel': 'WARNING'}]), 'R')
    assert warn.mode == RobotMode.IDLE and 'lowBattery' in warn.message
    manual = parse_state(state(operatingMode='MANUAL'), 'R')
    assert manual.mode == RobotMode.ERROR and 'MANUAL' in manual.message
    empty = parse_state({}, 'R')
    assert empty.node == '' and empty.trays == [] and empty.mode == RobotMode.IDLE


# ---------------------------------------------------------------------------- 드라이버 (가짜 MQTT)
def test_connect_subscribes_and_send_step_publishes_order():
    drv, client = make_driver()
    assert drv.status().mode == RobotMode.OFFLINE and not drv.status().connected
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O1-S1', 'N01'))       # 연결 전
    drv.connect()
    assert client.connected_to == ('10.0.0.5', 1883) and client.looping
    assert (f'{PREFIX}/state', 0) in client.subscribed
    assert (f'{PREFIX}/connection', 1) in client.subscribed
    assert client.on_message is not None
    drv.handle_message(f'{PREFIX}/connection', {'connectionState': 'ONLINE'})
    assert events(drv) == [('connected', None, '')]
    assert drv.status().connected and drv.status().mode == RobotMode.IDLE

    step = Step('O0001-S1', 'DOCK', 'pick', height_m=0.85, tray=0, ref='BOX-1', path=TOPO.shortest_path('PARK1', 'DOCK'))
    drv.send_step(step)
    assert drv.busy
    assert events(drv) == [('step_started', 'O0001-S1', '')]
    topic_name, msg, qos = client.published[-1]
    assert topic_name == f'{PREFIX}/order' and qos in (0, 1)
    assert msg['headerId'] == 1 and msg['serialNumber'] == 'SN-7' and msg['manufacturer'] == 'ACME'
    assert msg['orderId'].endswith('O0001-S1')
    action_id = msg['nodes'][-1]['actions'][0]['actionId']
    assert action_id == f"{msg['orderId']}-O0001-S1"
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O0001-S2', 'N01'))     # 진행 중

    # 진행 중 state: 아직 끝나지 않음
    drv.handle_message(f'{PREFIX}/state', state(orderId=msg['orderId'], driving=True, lastNodeId='N05',
                                                actionStates=[{'actionId': action_id, 'actionType': 'pick',
                                                               'actionStatus': 'WAITING'}]))
    assert events(drv) == [] and drv.busy
    assert drv.status().node == 'N05' and drv.status().mode == RobotMode.MOVING
    drv.handle_message(f'{PREFIX}/state', state(orderId=msg['orderId'], lastNodeId='DOCK',
                                                actionStates=[{'actionId': action_id, 'actionType': 'pick',
                                                               'actionStatus': 'FINISHED'}],
                                                loads=[{'loadId': 'BOX-1', 'loadPosition': '0'}]))
    assert events(drv) == [('step_done', 'O0001-S1', '')]
    assert not drv.busy and drv.status().trays == ['BOX-1']

    drv.send_step(Step('O0001-S2', 'S03', 'drop', height_m=0.4, tray=0, ref='S-03-1', path=['DOCK', 'S01', 'S02', 'S03']))
    assert client.published[-1][1]['headerId'] == 2                 # 토픽별 1씩 증가


def test_action_failed_maps_to_step_failed_with_errors():
    drv, client = online_driver()
    drv.send_step(Step('O0002-S1', 'N03', 'drop', height_m=1.0, tray=1, ref='N-03-3', path=['N01', 'N02', 'N03']))
    events(drv)
    msg = client.published[-1][1]
    action_id = msg['nodes'][-1]['actions'][0]['actionId']
    drv.handle_message(f'{PREFIX}/state', state(
        orderId=msg['orderId'], lastNodeId='N03',
        actionStates=[{'actionId': action_id, 'actionType': 'drop', 'actionStatus': 'FAILED'}],
        errors=[{'errorType': 'loadCollision', 'errorDescription': '칸에 이미 물건', 'errorLevel': 'WARNING'}]))
    (ev,) = events(drv)
    assert ev[0] == 'step_failed' and ev[1] == 'O0002-S1' and '칸에 이미 물건' in ev[2]
    assert not drv.busy


def test_move_only_completion_via_last_node():
    drv, client = online_driver()
    drv.send_step(Step('O0003-S1', 'S05', path=['S03', 'S04', 'S05']))
    events(drv)
    order_id = client.published[-1][1]['orderId']
    # 다른(이전) 주문의 state 는 무시
    drv.handle_message(f'{PREFIX}/state', state(orderId='OLD', lastNodeId='S05'))
    assert events(drv) == []
    # 아직 가는 중 (nodeStates 남음 / driving)
    drv.handle_message(f'{PREFIX}/state', state(orderId=order_id, lastNodeId='S04', driving=True,
                                                nodeStates=[{'nodeId': 'S05', 'sequenceId': 4, 'released': True}]))
    drv.handle_message(f'{PREFIX}/state', state(orderId=order_id, lastNodeId='S05', driving=True))
    assert events(drv) == []
    drv.handle_message(f'{PREFIX}/state', state(orderId=order_id, lastNodeId='S05', driving=False, nodeStates=[]))
    assert events(drv) == [('step_done', 'O0003-S1', '')]


def test_order_rejected_and_fatal_error():
    drv, client = online_driver()
    drv.send_step(Step('O0004-S1', 'S05', path=['S04', 'S05']))
    events(drv)
    drv.handle_message(f'{PREFIX}/state', state(orderId='OLD', errors=[
        {'errorType': 'validationError', 'errorDescription': 'unknown node', 'errorLevel': 'WARNING'}]))
    (ev,) = events(drv)
    assert ev[0] == 'step_failed' and 'validationError' in ev[2]
    drv.send_step(Step('O0004-S2', 'S05', path=['S04', 'S05']))
    events(drv)
    order_id = client.published[-1][1]['orderId']
    drv.handle_message(f'{PREFIX}/state', state(orderId=order_id, errors=[
        {'errorType': 'emergencyStop', 'errorDescription': '비상정지 버튼', 'errorLevel': 'FATAL'}]))
    (ev,) = events(drv)
    assert ev[0] == 'step_failed' and '비상정지 버튼' in ev[2]
    assert drv.status().mode == RobotMode.ERROR


def test_pause_resume_cancel_estop_instant_actions():
    drv, client = online_driver()

    def last_instant():
        topic_name, msg, _ = client.published[-1]
        assert topic_name == f'{PREFIX}/instantActions'
        return msg

    drv.pause()
    assert last_instant()['actions'][0]['actionType'] == 'startPause'
    drv.resume()
    assert last_instant()['actions'][0]['actionType'] == 'stopPause'
    drv.send_step(Step('O0005-S1', 'N05', path=['N04', 'N05']))
    events(drv)
    drv.cancel()
    cancel = last_instant()['actions'][0]
    assert cancel['actionType'] == 'cancelOrder'
    # (리뷰 수정) 취소는 AGV 가 cancelOrder 를 끝냈다고 알린 뒤에 step_failed — test_amr_review.py D9 참고
    drv.handle_message(f'{PREFIX}/state', state(orderId=client.published[-2][1]['orderId'], actionStates=[
        {'actionId': cancel['actionId'], 'actionType': 'cancelOrder', 'actionStatus': 'FINISHED'}]))
    assert events(drv) == [('step_failed', 'O0005-S1', 'cancelled')]
    assert not drv.busy
    drv.send_step(Step('O0005-S2', 'N05', path=['N04', 'N05']))
    events(drv)
    n_before = len(client.published)
    drv.estop()
    kinds = [m['actions'][0]['actionType'] for t, m, _ in client.published[n_before:]]
    assert kinds == ['cancelOrder', 'startPause']
    assert events(drv) == [('step_failed', 'O0005-S2', 'estop')]
    assert drv.status().mode == RobotMode.ERROR
    headers = [m['headerId'] for t, m, _ in client.published if t.endswith('/instantActions')]
    assert headers == list(range(1, len(headers) + 1))
    drv.resume()
    assert last_instant()['actions'][0]['actionType'] == 'stopPause'
    assert drv.status().mode != RobotMode.ERROR


def test_connection_topic_and_mqtt_callback():
    drv, client = make_driver()
    drv.connect()
    client.on_message(client, None, FakeMsg(f'{PREFIX}/state', state(lastNodeId='N02')))   # 수신 스레드 경로
    assert ('connected', None, '') in events(drv)
    assert drv.status().node == 'N02' and drv.status().battery == 77.5
    drv.handle_message(f'{PREFIX}/connection', {'connectionState': 'CONNECTIONBROKEN'})
    assert events(drv) == [('disconnected', None, 'CONNECTIONBROKEN')]
    assert drv.status().mode == RobotMode.OFFLINE and not drv.status().connected
    bad = FakeMsg(f'{PREFIX}/state', {})
    bad.payload = b'{not json'
    client.on_message(client, None, bad)
    assert events(drv)[0][0] == 'error'
    drv.handle_message(f'{PREFIX}/visualization', {'x': 1})   # 무시
    drv.disconnect()
    assert client.connected_to is None and not client.looping


def test_connect_without_paho_gives_korean_hint(monkeypatch):
    for name in ('paho', 'paho.mqtt', 'paho.mqtt.client'):
        monkeypatch.setitem(sys.modules, name, None)   # 설치 안 된 것처럼
    drv = Vda5050Driver(RobotConfig(id='AMR-V2', driver='vda5050', host='localhost', port=1883), TOPO)
    with pytest.raises(RuntimeError) as exc:
        drv.connect()
    assert 'pip install paho-mqtt' in str(exc.value)
    assert '미설치' in str(exc.value)


def test_vda_driver_under_fleet_manager():
    drv, client = make_driver()
    drv.connect()
    fleet = FleetManager(TOPO, [drv])
    fleet.tick(0.0)
    o = fleet.new_order('MOVE', [Step('', 'N03')])
    fleet.tick(0.1)
    assert o.state == OrderState.QUEUED          # AMR 가 아직 ONLINE/state 를 안 보냄 (연결 안 된 것으로 봄)
    drv.handle_message(f'{PREFIX}/state', state(lastNodeId='N01'))
    fleet.tick(0.2)
    assert o.state == OrderState.RUNNING
    msg = client.published[-1][1]
    assert [n['nodeId'] for n in msg['nodes']] == ['N01', 'N02', 'N03']
    drv.handle_message(f'{PREFIX}/state', state(orderId=msg['orderId'], lastNodeId='N03'))
    fleet.tick(0.3)
    assert o.state == OrderState.DONE
