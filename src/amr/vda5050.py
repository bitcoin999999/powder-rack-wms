"""VDA 5050 v2.0 (MQTT) 드라이버 — 저가 AMR 구매 시 'VDA 5050 지원'이면 이 드라이버로 붙인다. 계약: 공개 시그니처 유지.

메시지 생성·해석은 순수 함수(브로커 없이 시험 가능). 전송은 paho-mqtt 가 설치되어 있을 때만 (없으면 connect 에서 InventoryError 아닌
RuntimeError('paho-mqtt 미설치 …')).
토픽: {interface}/v2/{manufacturer}/{serial}/{order|instantActions|state|connection|visualization}
단계 1개 = order 1건: path 노드들을 nodes(sequenceId 짝수, released=True) + 그 사이 edges(홀수)로, 마지막 노드에 action
(pick/drop → actionType 'pick'/'drop', 파라미터 stationType, loadType='완분박스', loadId=ref, height=height_m, tray;
charge → 'startCharging'). action 없는 이동은 actions 비움.
state 메시지에서: actionStates[actionId].actionStatus FINISHED → step_done, FAILED → step_failed(errors 내용),
action 없는 단계는 lastNodeId == 목적 노드 이고 nodeStates 비고 driving False 이면 step_done.

구현 메모
- QoS: order·instantActions 0, state 0, connection 1 (VDA 5050 권장값).
- headerId 는 보내는 토픽별로 1부터 1씩 증가.
- orderId = '{세션}-{단계 ID}' (앱을 다시 켜도 AMR 이 같은 orderId 로 착각하지 않게 세션 시각을 붙인다).
- cancel: cancelOrder 를 보내고 AGV 가 취소를 끝낼 때까지(cancelOrder actionStatus FINISHED/FAILED, 또는 그 주문의
  nodeStates·edgeStates 가 비고 정지) 단계를 쥐고 있다가 step_failed('cancelled'). VDA 5050 AGV 는 취소 처리 중에 온
  새 주문을 거절하므로, 바로 '취소됨'을 내면 FleetManager 가 곧장 보낸 다음 주문(예: 주차 선점 뒤 입고)이 실패한다.
  취소가 닿기 전에 동작이 이미 끝났으면 실제 결과(step_done)를 낸다. AGV 가 OFFLINE 이면 기다릴 수 없어 바로 낸다.
  _CANCEL_TIMEOUT_S(tick 시간) 안에 답이 없으면 'error' 이벤트와 함께 step_failed('cancelled') 로 정리한다.
- estop 은 응답을 기다리지 않고 바로 step_failed('estop') (FleetManager 는 정지 중 새 주문을 보내지 않는다).
- charge: VDA 5050 의 startCharging FINISHED 는 '충전이 시작됨'이다. 그래서 단계는 batteryCharge 가
  step.meta['target'](기본 90) 에 닿을 때 stopCharging 을 보내고 끝낸다 (AGV 가 스스로 충전을 멈춰도 끝).
  충전 중 cancel 은 stopCharging 을 보내고 바로 step_failed('cancelled').
- 주문 거절 오류(validationError 등)는 errorReferences 의 orderId 가 이 주문이거나, 참조가 없을 때는 보내기 전 state 에
  없던 오류일 때만 이 주문의 실패로 본다 (이전 주문의 거절 오류가 남은 주기 state 로 다음 주문까지 실패하지 않게).
- AGV 가 OFFLINE(connection) 이면 send_step 은 주문을 보내지 않고 RuntimeError (QoS 0 주문이 사라져 영원히 기다리지 않게).
  이미 보낸 단계는 OFFLINE 이어도 실패시키지 않는다 — VDA 5050 AMR 은 주문을 스스로 계속 수행하고
  다시 연결되면 state 로 결과를 알려 준다.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from core.config import RobotConfig

from .driver import AmrDriver
from .models import RobotMode, RobotStatus, Step
from .topology import Topology

VDA_VERSION = '2.0.0'

_QOS = {'order': 0, 'instantActions': 0, 'state': 0, 'connection': 1}
# 주문이 AMR 에 거절되었음을 뜻하는 오류 종류 (VDA 5050 표준 errorType)
_ORDER_ERRORS = ('orderError', 'orderUpdateError', 'validationError', 'noRouteError')
_STATION_TYPE = {'dock': 'conveyor', 'aisle': 'rack', 'charger': 'charger', 'park': 'floor'}
_CANCEL_TIMEOUT_S = 20.0   # cancelOrder 응답을 기다리는 최대 시간 (tick 시간, s)


def topic(interface: str, manufacturer: str, serial: str, name: str, major: str = 'v2') -> str:
    return f'{interface}/{major}/{manufacturer}/{serial}/{name}'


def build_order(step: Step, topology: Topology, header_id: int, order_id: str, manufacturer: str, serial: str,
                timestamp: str, map_id: str = 'cell') -> dict:
    """Step → VDA 5050 order dict. 마지막 노드의 action id = f'{order_id}-{step.id}'."""
    path = list(step.path) if step.path else [step.node]
    if path[-1] != step.node:
        path.append(step.node)
    known = topology.nodes

    nodes: list[dict] = []
    for i, nid in enumerate(path):
        node: dict = {'nodeId': nid, 'sequenceId': 2 * i, 'released': True, 'actions': []}
        if nid in known:
            n = known[nid]
            node['nodePosition'] = {'x': n.x, 'y': n.y, 'theta': _theta(path, i, topology), 'mapId': map_id}
        nodes.append(node)

    edges = [{'edgeId': f'{a}-{b}', 'sequenceId': 2 * i + 1, 'released': True,
              'startNodeId': a, 'endNodeId': b, 'actions': []}
             for i, (a, b) in enumerate(zip(path, path[1:]))]

    action = _step_action(step, topology, f'{order_id}-{step.id}')
    if action is not None:
        nodes[-1]['actions'].append(action)

    return {
        'headerId': header_id,
        'timestamp': timestamp,
        'version': VDA_VERSION,
        'manufacturer': manufacturer,
        'serialNumber': serial,
        'orderId': order_id,
        'orderUpdateId': 0,
        'nodes': nodes,
        'edges': edges,
    }


def build_instant_action(action_type: str, header_id: int, manufacturer: str, serial: str, timestamp: str,
                         params: dict | None = None) -> dict:
    """action_type: cancelOrder / startPause / stopPause / initPosition / factsheetRequest …"""
    return {
        'headerId': header_id,
        'timestamp': timestamp,
        'version': VDA_VERSION,
        'manufacturer': manufacturer,
        'serialNumber': serial,
        'actions': [{
            'actionType': action_type,
            'actionId': f'{action_type}-{header_id}',
            'blockingType': 'HARD',
            'actionParameters': [{'key': k, 'value': v} for k, v in (params or {}).items()],
        }],
    }


def parse_state(msg: dict, robot_id: str) -> RobotStatus:
    """state 메시지 → RobotStatus (agvPosition, batteryState.batteryCharge, lastNodeId, driving, paused, errors, loads)."""
    pos = msg.get('agvPosition') or {}
    battery = msg.get('batteryState') or {}
    errors = msg.get('errors') or []
    op_mode = str(msg.get('operatingMode', 'AUTOMATIC')).upper()
    running = [str(a.get('actionType', '')) for a in msg.get('actionStates') or []
               if str(a.get('actionStatus', '')).upper() in ('INITIALIZING', 'RUNNING')]
    fatal = [e for e in errors if str(e.get('errorLevel', '')).upper() == 'FATAL']

    if fatal:
        mode = RobotMode.ERROR
    elif op_mode not in ('AUTOMATIC', 'SEMIAUTOMATIC'):
        mode = RobotMode.ERROR          # 수동·정비 모드: 배차 대상 아님
    elif msg.get('paused'):
        mode = RobotMode.PAUSED
    elif msg.get('driving'):
        mode = RobotMode.MOVING
    elif 'pick' in running:
        mode = RobotMode.LOADING
    elif 'drop' in running:
        mode = RobotMode.UNLOADING
    elif battery.get('charging'):
        mode = RobotMode.CHARGING
    else:
        mode = RobotMode.IDLE

    message = _error_text(errors)
    if not message and op_mode not in ('AUTOMATIC', 'SEMIAUTOMATIC'):
        message = f'자동 운전 아님 (operatingMode={op_mode})'

    return RobotStatus(
        robot_id=robot_id,
        mode=mode,
        node=str(msg.get('lastNodeId') or ''),
        x=float(pos.get('x', 0.0)),
        y=float(pos.get('y', 0.0)),
        heading=float(pos.get('theta', 0.0)),
        battery=float(battery.get('batteryCharge', 0.0)),
        trays=_loads_to_trays(msg.get('loads') or []),
        message=message,
        connected=True,
    )


class Vda5050Driver(AmrDriver):
    kind = 'vda5050'

    def __init__(self, cfg: RobotConfig, topology: Topology, client_factory=None):
        """client_factory: 시험용 가짜 MQTT 클라이언트 생성 함수 (없으면 paho.mqtt.client.Client)."""
        super().__init__(cfg.id)
        self.cfg = cfg
        self._topo = topology
        self._factory = client_factory
        self._client = None
        self._interface = cfg.interface or 'uagv'
        self._manufacturer = cfg.manufacturer or 'unknown'
        self._serial = cfg.serial or cfg.id
        self._session = time.strftime('%y%m%d%H%M%S')
        self._headers: dict[str, int] = {}               # 토픽별 headerId
        self._header_lock = threading.Lock()             # headerId 는 두 스레드에서 발번 (stopCharging 은 수신 스레드)
        self._lock = threading.Lock()                    # MQTT 수신 스레드 ↔ 메인 스레드
        self._status = RobotStatus(cfg.id, mode=RobotMode.OFFLINE, node=cfg.start_node, connected=False)
        self._step: Step | None = None
        self._order_id = ''
        self._action_id: str | None = None
        self._estopped = False
        self._last_errors: list[dict] = []               # 마지막 state 의 errors
        self._baseline: set[tuple[str, str]] = set()     # 보내기 전부터 있던 주문 거절 오류
        self._charge_target = 90.0
        self._charging = False                           # startCharging FINISHED 받음 → 목표까지 충전 중
        self._saw_charging = False                       # 충전 중 batteryState.charging=True 를 본 적 있음
        self._cancel_id: str | None = None               # 응답을 기다리는 cancelOrder actionId
        self._cancel_wait = 0.0                          # 취소 응답 대기 시간 (tick 누적, s)

    # ------------------------------------------------------------------ 연결
    def connect(self) -> None:
        if self._client is not None:
            return
        if self._factory is None:
            try:
                import paho.mqtt.client as mqtt   # 선택 의존성: 실제 AMR 을 붙일 때만 필요
            except ImportError:
                if getattr(sys, 'frozen', False):   # exe 는 시스템 Python 패키지를 못 쓴다 → exe 를 다시 만들어야 한다
                    raise RuntimeError('paho-mqtt 미설치 — 이 exe 에는 MQTT 부품이 들어 있지 않습니다. 개발 PC 명령창에서 '
                                       '"pip install paho-mqtt" 후 "python build.py" 로 exe 를 다시 만드세요.') from None
                raise RuntimeError('paho-mqtt 미설치 — VDA 5050 AMR 을 쓰려면 명령창에서 '
                                   '"pip install paho-mqtt" 를 실행한 뒤 다시 시작하세요.') from None
            if hasattr(mqtt, 'CallbackAPIVersion'):   # paho-mqtt 2.x
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'wanbun-{self.robot_id}')
            else:
                client = mqtt.Client(client_id=f'wanbun-{self.robot_id}')
        else:
            client = self._factory()
        client.on_message = self._on_mqtt_message
        host, port = self.cfg.host or 'localhost', self.cfg.port or 1883
        try:
            client.connect(host, port)
        except Exception as exc:
            raise RuntimeError(f'{self.robot_id}: MQTT 브로커 연결 실패 ({host}:{port}) — {exc}') from exc
        for name in ('state', 'connection'):
            client.subscribe(self._topic(name), qos=_QOS[name])
        if hasattr(client, 'loop_start'):
            client.loop_start()
        self._client = client

    def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                if hasattr(client, 'loop_stop'):
                    client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        with self._lock:
            was = self._status.connected
            self._status.connected = False
            self._status.mode = RobotMode.OFFLINE
        if was:
            self._emit('disconnected')

    # ------------------------------------------------------------------ 명령
    def send_step(self, step: Step) -> None:
        if self._client is None:
            raise RuntimeError(f'{self.robot_id}: VDA 5050 연결 안 됨 (connect 먼저)')
        with self._lock:
            if self._step is not None:
                raise RuntimeError(f'{self.robot_id}: 이미 진행 중인 단계가 있습니다 ({self._step.id})')
            if not self._status.connected:
                raise RuntimeError(f'{self.robot_id}: AGV 가 연결되어 있지 않습니다 (connection OFFLINE) — '
                                   f'주문을 보내지 않았습니다')
            target = min(100.0, float(step.meta.get('target', 90)))   # 잘못된 값이면 여기서 ValueError (상태 안 바뀜)
            order_id = f'{self._session}-{step.id}'
            msg = build_order(step, self._topo, self._next_header('order'), order_id,
                              self._manufacturer, self._serial, _timestamp())
            self._clear_step()
            self._step = step
            self._order_id = order_id
            actions = msg['nodes'][-1]['actions']
            self._action_id = actions[0]['actionId'] if actions else None
            self._baseline = {_error_key(e) for e in self._last_errors if e.get('errorType') in _ORDER_ERRORS}
            self._charge_target = target
        self._emit('step_started', step.id)
        try:
            self._publish('order', msg)
        except Exception as exc:
            with self._lock:
                if self._step is step:
                    self._clear_step()
            raise RuntimeError(f'{self.robot_id}: 주문 전송 실패 — {exc}') from exc

    def pause(self) -> None:
        self._instant('startPause')

    def resume(self) -> None:
        self._estopped = False
        self._instant('stopPause')

    def cancel(self) -> None:
        msg = None
        with self._lock:
            step = self._step
            if step is None or self._cancel_id is not None:
                return   # 진행 중 단계 없음 / 이미 취소 응답 대기 중
            charging = self._charging
            offline = not self._status.connected or self._client is None
            if charging or offline:
                self._clear_step()
            else:   # AGV 가 취소를 끝낼 때까지 단계를 쥐고 기다린다 (_on_state / tick 에서 마무리)
                msg, self._cancel_id = self._instant_msg('cancelOrder')
                self._cancel_wait = 0.0
        if charging:   # 주문은 이미 끝났고 충전만 하는 중: 충전을 멈춘다
            self._instant('stopCharging')
            self._emit('step_failed', step.id, 'cancelled')
        elif offline:  # 응답을 받을 수 없다: 보내 두고 바로 취소 처리
            self._instant('cancelOrder')
            self._emit('step_failed', step.id, 'cancelled')
        else:
            try:
                self._publish('instantActions', msg)
            except Exception as exc:   # 못 보냈으면 시간 초과로 정리된다
                self._emit('error', detail=f'cancelOrder 전송 실패: {exc}')

    def estop(self) -> None:
        self._estopped = True
        with self._lock:
            step, charging = self._step, self._charging
            self._clear_step()
        self._instant('cancelOrder')
        self._instant('startPause')
        if charging:
            self._instant('stopCharging')   # 충전 중이면 CHARGING 상태로 남아 배차 대상에서 영영 빠지지 않게
        if step is not None:
            self._emit('step_failed', step.id, 'estop')

    def tick(self, now: float, dt: float) -> None:
        """취소 응답 대기 시간만 잰다 (FleetManager 가 매 tick 부른다)."""
        timed_out = None
        with self._lock:
            if self._step is not None and self._cancel_id is not None:
                self._cancel_wait += max(dt, 0.0)
                if self._cancel_wait >= _CANCEL_TIMEOUT_S:
                    timed_out = self._step
                    self._clear_step()
        if timed_out is not None:
            self._emit('error', detail=f'{self.robot_id}: cancelOrder 응답이 {_CANCEL_TIMEOUT_S:.0f}초 동안 없습니다 — '
                                       f'AGV 상태를 현장에서 확인하세요')
            self._emit('step_failed', timed_out.id, 'cancelled')

    # ------------------------------------------------------------------ 상태
    def status(self) -> RobotStatus:
        with self._lock:
            st = replace(self._status, trays=list(self._status.trays))
        if self._estopped:
            st.mode = RobotMode.ERROR
            st.message = '비상정지'
        if not st.connected:
            st.mode = RobotMode.OFFLINE
        return st

    @property
    def busy(self) -> bool:
        return self._step is not None

    # ------------------------------------------------------------------ 수신
    def handle_message(self, topic_name: str, payload: dict) -> None:
        """MQTT 수신 콜백에서 부른다 (시험에서는 직접 호출). state/connection 처리 → 이벤트 발행."""
        name = topic_name.rsplit('/', 1)[-1]
        try:
            if name == 'connection':
                self._on_connection(payload)
            elif name == 'state':
                self._on_state(payload)
            # visualization·factsheet 등은 무시
        except Exception as exc:   # 값이 잘못된 메시지 하나로 수신 스레드가 죽지 않게 (paho 1.x 는 콜백 예외를 다시 던진다)
            self._emit('error', detail=f'{self.robot_id}: {name} 메시지 해석 실패 — {exc!r}')

    def _on_connection(self, payload: dict) -> None:
        online = str(payload.get('connectionState', '')).upper() == 'ONLINE'
        with self._lock:
            was = self._status.connected
            self._status.connected = online
            if not online:
                self._status.mode = RobotMode.OFFLINE
            elif self._status.mode == RobotMode.OFFLINE:
                self._status.mode = RobotMode.IDLE
        if online and not was:
            self._emit('connected')
        elif not online and was:
            self._emit('disconnected', detail=str(payload.get('connectionState', '')))

    def _on_state(self, payload: dict) -> None:
        st = parse_state(payload, self.robot_id)
        st.updated_at = time.monotonic()
        result: tuple[str, str, str] | None = None
        stop_charging = False
        with self._lock:
            was = self._status.connected
            st.fork_height = self._status.fork_height
            self._status = st
            if self._step is not None:
                outcome = self._step_outcome(payload, st)
                if outcome is not None:
                    result = (outcome[0], self._step.id, outcome[1])
                    stop_charging = outcome[0] == 'step_done' and self._charging and outcome[1] == 'target'
                    self._clear_step()
            self._last_errors = list(payload.get('errors') or [])
        if not was:
            self._emit('connected')
        if stop_charging:
            self._instant('stopCharging')   # 목표 충전량 도달 → 충전 끝 (안 보내면 AGV 가 CHARGING 으로 남는다)
        if result is not None:
            self._emit(result[0], result[1], '' if result[0] == 'step_done' else result[2])

    def _step_outcome(self, payload: dict, st: RobotStatus) -> tuple[str, str] | None:
        """진행 중 단계의 결과 (lock 안에서 호출). ('step_done'|'step_failed', 사유) 또는 아직이면 None.
        충전 목표 도달로 끝나면 사유 'target' (stopCharging 을 보내라는 뜻, 이벤트에는 싣지 않음)."""
        step = self._step
        assert step is not None
        errors = payload.get('errors') or []
        state_order = payload.get('orderId')
        cancelling = self._cancel_id is not None
        if state_order not in (None, '') and state_order != self._order_id:
            # AMR 이 아직 이 주문을 받지 않았다. 이 주문을 거절한 오류가 있으면 실패, 취소가 끝났으면 취소.
            if cancelling and _action_status(payload, self._cancel_id) in ('FINISHED', 'FAILED'):
                return ('step_failed', 'cancelled')
            rejected = [e for e in errors if self._rejects_this_order(e)]
            return ('step_failed', _error_text(rejected)) if rejected else None
        driving = bool(payload.get('driving', False))
        if self._action_id is not None:
            status = _action_status(payload, self._action_id)
            if status == 'FINISHED':
                if step.action != 'charge':
                    return ('step_done', '')
                self._charging = True          # 충전 시작 — 목표까지 계속
            elif status == 'FAILED':
                if cancelling:
                    return ('step_failed', 'cancelled')
                action = next(a for a in payload.get('actionStates') or [] if a.get('actionId') == self._action_id)
                detail = _error_text(errors) or str(action.get('resultDescription') or '')
                return ('step_failed', detail or f"동작 실패: {action.get('actionType', '')}")
        elif payload.get('lastNodeId') == step.node and not payload.get('nodeStates') and not driving:
            return ('step_done', '')
        if self._charging:
            charging_now = bool((payload.get('batteryState') or {}).get('charging'))
            if st.battery >= self._charge_target - 1e-9:
                return ('step_done', 'target')
            if charging_now:
                self._saw_charging = True
            elif self._saw_charging:            # AGV 가 스스로 충전을 멈춤 (자체 상한 등)
                return ('step_done', '')
        if cancelling:
            if _action_status(payload, self._cancel_id) in ('FINISHED', 'FAILED'):
                return ('step_failed', 'cancelled')
            if not payload.get('nodeStates') and not payload.get('edgeStates') and not driving:
                return ('step_failed', 'cancelled')   # 주문이 비워지고 멈춤 = 취소 끝
        fatal = [e for e in errors if str(e.get('errorLevel', '')).upper() == 'FATAL']
        if fatal:
            return ('step_failed', _error_text(fatal))
        return None

    def _rejects_this_order(self, error: dict) -> bool:
        """주문 거절 오류가 지금 보낸 주문에 대한 것인가 (lock 안에서 호출)."""
        if error.get('errorType') not in _ORDER_ERRORS:
            return False
        for ref in error.get('errorReferences') or []:
            if ref.get('referenceKey') == 'orderId':
                return ref.get('referenceValue') == self._order_id
        return _error_key(error) not in self._baseline   # 참조가 없으면 보낸 뒤 새로 생긴 오류만

    def _clear_step(self) -> None:
        """진행 단계 정보 초기화 (lock 안에서 호출)."""
        self._step = None
        self._action_id = None
        self._charging = False
        self._saw_charging = False
        self._cancel_id = None
        self._cancel_wait = 0.0

    def _on_mqtt_message(self, client, userdata, msg) -> None:   # paho 콜백 (수신 스레드)
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            self._emit('error', detail=f'수신 메시지 해석 실패 ({msg.topic}): {exc}')
            return
        if isinstance(payload, dict):
            self.handle_message(msg.topic, payload)

    # ------------------------------------------------------------------ 내부
    def _topic(self, name: str) -> str:
        return topic(self._interface, self._manufacturer, self._serial, name)

    def _next_header(self, name: str) -> int:
        with self._header_lock:
            self._headers[name] = self._headers.get(name, 0) + 1
            return self._headers[name]

    def _publish(self, name: str, msg: dict) -> None:
        client = self._client
        if client is None:
            return
        client.publish(self._topic(name), json.dumps(msg, ensure_ascii=False), qos=_QOS.get(name, 0))

    def _instant_msg(self, action_type: str) -> tuple[dict, str]:
        """instantActions 메시지와 그 actionId."""
        msg = build_instant_action(action_type, self._next_header('instantActions'),
                                   self._manufacturer, self._serial, _timestamp())
        return msg, msg['actions'][0]['actionId']

    def _instant(self, action_type: str) -> None:
        if self._client is None:
            return   # 연결 전: 보낼 곳이 없다 (로컬 상태만 바뀜)
        msg, _ = self._instant_msg(action_type)
        try:
            self._publish('instantActions', msg)
        except Exception as exc:
            self._emit('error', detail=f'{action_type} 전송 실패: {exc}')


# ---------------------------------------------------------------------- 보조 함수
def _timestamp() -> str:
    """ISO 8601 UTC (예: 2026-10-20T03:15:00.123Z)."""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _theta(path: list[str], i: int, topology: Topology) -> float:
    """노드에서의 진행 방향 (rad): 들어오는 구간 방향, 첫 노드는 나가는 구간 방향."""
    nodes = topology.nodes
    if i > 0 and path[i - 1] in nodes and path[i] in nodes:
        a, b = nodes[path[i - 1]], nodes[path[i]]
    elif i + 1 < len(path) and path[i] in nodes and path[i + 1] in nodes:
        a, b = nodes[path[i]], nodes[path[i + 1]]
    else:
        return 0.0
    return math.atan2(b.y - a.y, b.x - a.x)


def _step_action(step: Step, topology: Topology, action_id: str) -> dict | None:
    if step.action in ('pick', 'drop'):
        node = topology.nodes.get(step.node)
        station = step.meta.get('stationType') or _STATION_TYPE.get(node.kind if node else '', 'floor')
        params = [
            {'key': 'stationType', 'value': station},
            {'key': 'loadType', 'value': '완분박스'},
            {'key': 'loadId', 'value': step.ref},
            {'key': 'height', 'value': step.height_m},
            {'key': 'tray', 'value': step.tray},
        ]
        return {'actionType': step.action, 'actionId': action_id, 'blockingType': 'HARD',
                'actionParameters': params}
    if step.action == 'charge':
        return {'actionType': 'startCharging', 'actionId': action_id, 'blockingType': 'HARD',
                'actionParameters': []}
    return None


def _action_status(payload: dict, action_id: str | None) -> str | None:
    """state 의 actionStates 에서 action_id 의 actionStatus (대문자), 없으면 None."""
    if action_id is None:
        return None
    for a in payload.get('actionStates') or []:
        if a.get('actionId') == action_id:
            return str(a.get('actionStatus', '')).upper()
    return None


def _error_key(error: dict) -> tuple[str, str]:
    return (str(error.get('errorType', '')), str(error.get('errorDescription', '')))


def _error_text(errors: list[dict]) -> str:
    parts = []
    for e in errors:
        kind = str(e.get('errorType', '')).strip()
        desc = str(e.get('errorDescription', '')).strip()
        parts.append(f'{kind}: {desc}' if kind and desc else kind or desc)
    return '; '.join(p for p in parts if p)


def _loads_to_trays(loads: list[dict]) -> list[str | None]:
    """loads → 사다리 단별 박스 ID. loadPosition 이 숫자면 그 단, 아니면 차례로."""
    trays: list[str | None] = []
    for load in loads:
        load_id = load.get('loadId')
        pos = str(load.get('loadPosition', ''))
        if pos.isdigit():
            idx = int(pos)
            trays.extend([None] * (idx + 1 - len(trays)))
            trays[idx] = load_id
        else:
            trays.append(load_id)
    return trays
