"""AMR 여러 대의 작업 관리 (배차·단계 실행·구역 잠금·충전·주차). 계약: 공개 시그니처 유지.

규칙
- 상태 변경은 tick(now) 안에서만 (메인 스레드). submit/cancel/pause 는 즉시 기록하고 실제 처리는 다음 tick.
  단, cancel·estop 은 드라이버에 즉시 전달해도 된다.
- 배차: QUEUED 작업을 (priority 큰 순, created 순)으로, order.robot 이 지정이면 그 AMR, 아니면 쉬는 AMR 중 첫 단계 노드까지
  경로가 가장 짧은 AMR. 쉬는 AMR = 작업 없음 + 연결됨 + 모드 IDLE + (배터리 > charge_below 또는 작업이 CHARGE).
  PARK 작업 중인 AMR 은 쉬는 것으로 본다 (PARK 를 취소하고 새 작업 배정).
- 단계 전송: step.zone 이 있으면 그 구역을 잡아야 보낸다 (못 잡으면 OrderEvent 'waiting', 로봇 상태 표시만).
  경로 = topology.shortest_path(로봇 현재 노드, step.node). step_done/failed 에서 구역 해제.
- step_failed: detail 'cancelled' 이면 작업 CANCELLED, 그 외 FAILED. 남은 단계는 버린다.
- 충전: 작업 없는 AMR 배터리 ≤ charge_below → CHARGE 작업 (단계 1개: CHARGER, action 'charge', zone 'CHARGER',
  meta target=charge_target). 충전기는 zone 이라 한 대씩.
- 주차: 작업 없는 AMR 이 PARK 노드가 아닌 곳에 park_after_s 넘게 서 있으면 비어 있는 가장 가까운 PARKn 으로 PARK 작업
  (도크·통로를 비워 둔다). 다른 AMR 이 서 있거나 가고 있는 PARK 는 제외.
- 구독자에게 OrderEvent 를 보낸다 (created/assigned/step_started/step_done/step_failed/done/failed/cancelled/waiting).

구현 메모 (위 규칙의 세부)
- tick(now) 는 드라이버도 tick 한다 (dt = 지난 tick 이후 경과). 가상 AMR 은 같은 now 중복 tick 을 무시하므로
  Controller 가 따로 드라이버를 tick 해도 이중 진행되지 않는다.
- 구역 잠금 보강: 같은 작업의 다음 단계가 같은 구역이면 잠금을 넘겨 쥔다 (도크 연속 집기 사이에 다른 AMR 이 끼지 않게).
  또 구역 단계의 목적 노드에 다른 AMR 이 아직 서 있으면(status.node 가 그 노드) 잠금이 비어도 기다린다.
  그 자리를 막고 선 AMR 이 작업이 없으면 park_after_s 를 기다리지 않고 바로 주차시킨다.
  막고 선 AMR 이 자기도 구역 대기 중이면(서로의 자리를 기다리는 교착) 먼저 빈 PARK 로 비켜선 뒤 기다린다.
  이 비켜서기 이동은 FleetManager 내부 단계라 작업의 steps 에 넣지 않고 구독자에게도 알리지 않는다.
- PARK 선점: 주차 단계가 드라이버에 가 있으면 드라이버 cancel → step_failed 를 받은 뒤 PARK 를 CANCELLED 로 닫고
  새 작업 첫 단계를 보낸다. 아직 안 보냈으면 바로 CANCELLED.
- 드라이버가 모르는 단계 ID 로 이벤트를 보내면 무시한다.
- tick 순서: 일시정지/재개 적용 → 드라이버 tick → 드라이버 이벤트 → (비상정지 정리) → 자동 충전 → 배차 → 자동 주차 → 단계 전송.
  주차는 배차 뒤에 만든다 (같은 tick 에 새 작업을 받을 AMR 을 괜히 주차시키지 않게).
- '노드를 차지함' = 마지막 노드가 그 노드이거나, 그 노드로 이어진 구간 위에서 노드 중심 _NEAR_M 안에 있음
  (취소·고장·통신 끊김으로 구간 중간에 멈춘 AMR 도 도크·충전기·주차 자리를 막는 것으로 본다). 구간 위인지는
  보고된 좌표가 지도 구간 선분 위에 있을 때만 판단하므로, 좌표를 안 주는 업체 AMR 은 마지막 노드로만 본다.
  같은 이유로 주차 자리를 떠나다 구간 위에 멈춘 AMR 은 '주차됨'이 아니라서 다시 주차시킨다.
- 충전 기준은 0 ≤ charge_below < charge_target ≤ 100 이어야 한다 (아니면 충전 직후 다시 충전 작업이 끝없이 생긴다).
- 교통 관제(통로 맞부딪힘 회피)는 하지 않는다 — 구역 잠금(DOCK·CHARGER)만. 실제 AMR 은 자체 장애물 정지에 맡긴다.
"""
from __future__ import annotations

import logging
import math
from typing import Callable

from .driver import AmrDriver
from .models import ACTIONS, DriverEvent, OrderEvent, OrderState, RobotMode, RobotStatus, Step, TransportOrder
from .topology import Topology

log = logging.getLogger(__name__)

_ACTIVE = (OrderState.QUEUED, OrderState.ASSIGNED, OrderState.RUNNING)
_CHARGER = 'CHARGER'
_CHARGE_PRIORITY = 50     # 자동 충전 작업 우선순위
_PARK_PRIORITY = -10      # 자동 주차 작업 우선순위 (가장 낮음)
_FINISH_EVENT = {OrderState.DONE: 'done', OrderState.FAILED: 'failed', OrderState.CANCELLED: 'cancelled'}
# 드라이버 실패 코드(계약) → 작업 오류 문구 (화면에 그대로 나간다). 'cancelled' 는 취소 사유로 따로 처리.
_DETAIL_TEXT = {'estop': '비상정지', 'battery': '배터리 방전'}
_NEAR_M = 0.8       # 이웃 구간 위 AMR 중심이 노드에서 이 거리 안이면 그 노드를 차지한 것으로 본다 (AMR 몸체 여유)
_ON_EDGE_M = 0.15    # 보고 좌표가 구간 선분에서 이만큼 안이면 그 구간 위에 있는 것으로 본다 (실제 AMR 주행 오차)
_AT_NODE_M = 0.25    # 노드 좌표에서 이만큼 안이면 노드 위에 서 있는 것으로 본다 (실제 AMR 정지 오차)


class FleetManager:
    def __init__(self, topology: Topology, drivers: list[AmrDriver], charge_below: float = 25.0,
                 charge_target: float = 90.0, park_after_s: float = 10.0):
        if not 0.0 <= charge_below < charge_target <= 100.0:
            raise ValueError(f'충전 기준 오류: 충전 시작 {charge_below}% 는 0 이상이고 충전 목표 {charge_target}% 보다 '
                             f'작아야 하며, 충전 목표는 100% 이하여야 합니다')
        self.topology = topology
        self.charge_below = charge_below
        self.charge_target = charge_target
        self.park_after_s = park_after_s
        self._drivers: dict[str, AmrDriver] = {}
        for drv in drivers:
            if drv.robot_id in self._drivers:
                raise ValueError(f'AMR ID 중복: {drv.robot_id}')
            self._drivers[drv.robot_id] = drv
        self._subs: list[Callable[[OrderEvent], None]] = []
        self._orders: dict[str, TransportOrder] = {}      # 생성 순
        self._active: dict[str, TransportOrder] = {}      # QUEUED/ASSIGNED/RUNNING (생성 순)
        self._seq = 0
        self._now = 0.0
        self._last_tick: float | None = None
        self._current: dict[str, str] = {}                # AMR → 배정·진행 중 작업 ID (PARK 포함)
        self._inflight: dict[str, tuple[str, str]] = {}   # AMR → (작업 ID, 단계 ID) 드라이버에 가 있는 단계
        self._started: set[str] = set()                   # step_started 를 이미 알린 단계 ID
        self._draining: dict[str, str] = {}               # AMR → 선점으로 취소 응답을 기다리는 PARK 작업 ID
        self._cancel_req: dict[str, str] = {}             # 작업 ID → 취소 사유 (드라이버 응답 대기)
        self._zones: dict[str, str] = {}                  # 구역 → 쥔 AMR
        self._waiting: dict[str, tuple[str, str]] = {}    # AMR → (단계 ID, 구역) 구역 대기 중
        self._aside: dict[str, Step] = {}                 # AMR → 비켜서기 이동 단계 (내부용, 작업 steps 밖)
        self._aside_seq = 0
        self._idle_since: dict[str, float] = {}
        self._settled: dict[str, str] = {}                # AMR → 자동 주차를 마친 노드 (위치 오차로 주차를 되풀이하지 않게)
        self._park_retry: dict[str, float] = {}           # AMR → 자동 주차 실패 뒤 다시 시도해도 되는 시각
        self._halted = False                             # 일시정지·비상정지 중: 배차·전송 멈춤
        self._run_request: str | None = None              # 다음 tick 에 적용할 'pause' / 'resume'
        self._estop_pending = False

    def subscribe(self, callback: Callable[[OrderEvent], None]) -> None:
        self._subs.append(callback)

    # ================================================================== 작업
    def new_order(self, kind: str, steps: list[Step], priority: int = 0, robot: str | None = None,
                  meta: dict | None = None, now: float = 0.0) -> TransportOrder:
        """ID 'O0001'… 발번, 단계 ID f'{order}-S{n}' 로 채우고 QUEUED 로 등록. 'created' 이벤트."""
        if not steps:
            raise ValueError('작업 단계가 없습니다')
        nodes = self.topology.nodes
        for step in steps:
            if step.node not in nodes:
                raise ValueError(f'지도에 없는 노드: {step.node}')
            if step.action not in ACTIONS:
                raise ValueError(f'알 수 없는 동작: {step.action}')
        if robot is not None and robot not in self._drivers:
            raise ValueError(f'없는 AMR: {robot}')
        self._seq += 1
        oid = f'O{self._seq:04d}'
        steps = list(steps)
        for n, step in enumerate(steps, 1):
            step.id = f'{oid}-S{n}'
        order = TransportOrder(id=oid, kind=kind, steps=steps, robot=robot, created=now,
                               priority=priority, meta=dict(meta or {}))
        self._orders[oid] = order
        self._active[oid] = order
        self._publish(order, 'created', robot=robot)
        return order

    def cancel(self, order_id: str, reason: str = '사용자 취소') -> None:
        """QUEUED/ASSIGNED 는 바로 CANCELLED, RUNNING 은 드라이버 cancel → step_failed('cancelled') 로 마무리."""
        order = self.order(order_id)
        if order.state not in _ACTIVE:
            return
        rid = order.robot
        inflight = self._inflight.get(rid) if rid is not None else None
        mine = inflight is not None and inflight[0] == order.id
        if mine and order.state == OrderState.RUNNING:
            self._cancel_req[order.id] = reason
            self._call(rid, 'cancel')
            return
        if mine:   # 배정 상태에서 비켜서기 이동 중: 이동을 멈추고 바로 닫는다 (늦게 오는 실패 이벤트는 무시됨)
            self._call(rid, 'cancel')
        # 대기·배정 상태 또는 단계 사이(구역 대기 등): 바로 닫는다
        self._finish(order, OrderState.CANCELLED, reason)

    def orders(self, active_only: bool = False) -> list[TransportOrder]:
        """생성 순. active = QUEUED/ASSIGNED/RUNNING."""
        return list((self._active if active_only else self._orders).values())

    def order(self, order_id: str) -> TransportOrder:
        try:
            return self._orders[order_id]
        except KeyError:
            raise KeyError(f'없는 작업: {order_id}') from None

    def robot_order(self, robot_id: str) -> TransportOrder | None:
        """그 AMR 이 하고 있는 작업 (PARK 포함)."""
        oid = self._current.get(robot_id)
        return self._orders[oid] if oid is not None else None

    # ================================================================== AMR
    def robots(self) -> list[RobotStatus]:
        out: list[RobotStatus] = []
        for rid, drv in self._drivers.items():
            st = drv.status()
            wait = self._waiting.get(rid)
            if wait is not None and st.mode == RobotMode.IDLE:
                st.mode = RobotMode.WAITING
                st.message = f'{wait[1]} 구역 대기'
            out.append(st)
        return out

    def driver(self, robot_id: str) -> AmrDriver:
        try:
            return self._drivers[robot_id]
        except KeyError:
            raise KeyError(f'없는 AMR: {robot_id}') from None

    def idle_robots(self) -> list[str]:
        """새 작업을 받을 수 있는 AMR (위 정의)."""
        return [rid for rid in self._drivers if self._available(rid, 'INBOUND')]

    def zone_owner(self, zone: str) -> str | None:
        return self._zones.get(zone)

    def pause_all(self) -> None:
        self._run_request = 'pause'      # 다음 tick 에 적용

    def resume_all(self) -> None:
        self._run_request = 'resume'     # 다음 tick 에 적용 (비상정지 해제 포함)

    def estop_all(self) -> None:
        # 비상정지는 드라이버에 바로 전달. 작업 정리는 다음 tick.
        self._halted = True
        self._run_request = None
        self._estop_pending = True
        for rid in self._drivers:
            self._call(rid, 'estop')

    # ================================================================== 주기 처리
    def tick(self, now: float) -> None:
        """드라이버 이벤트 처리 → 단계 진행 → 충전·주차 → 배차 → 다음 단계 전송."""
        dt = 0.0 if self._last_tick is None else max(0.0, now - self._last_tick)
        self._last_tick = now
        self._now = now

        self._apply_run_request()
        for rid, drv in self._drivers.items():
            try:
                drv.tick(now, dt)
            except Exception:   # 드라이버 하나의 오류로 전체가 멈추지 않게
                log.exception('%s: 드라이버 tick 오류', rid)
        for drv in list(self._drivers.values()):
            for ev in drv.poll():
                self._handle_driver_event(ev)
        if self._estop_pending:
            self._estop_pending = False
            self._fail_unsent('비상정지')
        if self._halted:
            return
        self._auto_charge()
        self._dispatch()
        self._auto_park()
        self._send_ready()

    # ================================================================== 내부: 드라이버 이벤트
    def _handle_driver_event(self, ev: DriverEvent) -> None:
        rid = ev.robot_id
        if rid not in self._drivers or ev.kind not in ('step_started', 'step_done', 'step_failed'):
            if ev.kind == 'error':
                log.warning('%s: %s', rid, ev.detail)
            return
        inflight = self._inflight.get(rid)
        if inflight is None or ev.step_id != inflight[1]:
            return   # 모르는 단계 (늦게 온 이벤트 등) → 무시
        order = self._orders.get(inflight[0])
        aside = self._aside.get(rid)
        if aside is not None and aside.id == ev.step_id:
            self._on_aside_event(rid, order, ev)
            return
        step = next((s for s in order.steps if s.id == ev.step_id), None) if order else None
        if order is None or step is None:
            self._inflight.pop(rid, None)
            return
        if ev.kind == 'step_started':
            if step.id not in self._started and order.state in _ACTIVE:
                self._started.add(step.id)
                self._publish(order, 'step_started', step=step, robot=rid)
            return

        del self._inflight[rid]
        self._started.discard(step.id)
        if order.state not in _ACTIVE:
            return   # 이미 닫힌 작업
        if self._draining.get(rid) == order.id:   # 선점된 PARK: 어떻게 끝났든 취소로 닫는다
            self._finish(order, OrderState.CANCELLED, self._cancel_req.get(order.id, '주차 취소'))
            return
        if ev.kind == 'step_done':
            self._on_step_done(rid, order, step)
        else:
            self._on_step_failed(rid, order, step, ev.detail)

    def _on_step_done(self, rid: str, order: TransportOrder, step: Step) -> None:
        self._publish(order, 'step_done', step=step, robot=rid)
        if order.state not in _ACTIVE:
            return   # 구독자가 취소함
        if order.id in self._cancel_req:
            self._finish(order, OrderState.CANCELLED, self._cancel_req[order.id])
            return
        nxt = order.current + 1
        if nxt >= len(order.steps):
            self._finish(order, OrderState.DONE)
            return
        # 다음 단계가 같은 구역이면 잠금을 넘겨 쥔다, 아니면 해제
        if step.zone and order.steps[nxt].zone != step.zone:
            self._release(step.zone, rid)
        order.current = nxt

    def _on_step_failed(self, rid: str, order: TransportOrder, step: Step, detail: str) -> None:
        if step.zone:
            self._release(step.zone, rid)
        self._publish(order, 'step_failed', step=step, robot=rid, detail=detail)
        if order.state not in _ACTIVE:
            return
        if detail == 'cancelled':
            self._finish(order, OrderState.CANCELLED, self._cancel_req.get(order.id, '취소됨'))
        else:
            self._finish(order, OrderState.FAILED, _DETAIL_TEXT.get(detail, detail) or '단계 실패')

    def _on_aside_event(self, rid: str, order: TransportOrder | None, ev: DriverEvent) -> None:
        if ev.kind == 'step_started':
            return
        del self._inflight[rid]
        del self._aside[rid]
        if order is None or order.state not in _ACTIVE:
            return
        if ev.kind == 'step_failed':
            if ev.detail == 'cancelled' or order.id in self._cancel_req:
                self._finish(order, OrderState.CANCELLED, self._cancel_req.get(order.id, '취소됨'))
            else:
                self._finish(order, OrderState.FAILED, f'비켜서기 이동 실패: {_DETAIL_TEXT.get(ev.detail, ev.detail)}')
        elif order.id in self._cancel_req:
            self._finish(order, OrderState.CANCELLED, self._cancel_req[order.id])
        # 끝나면 다음 전송 단계에서 원래 단계를 다시 시도한다

    def _fail_unsent(self, reason: str) -> None:
        """비상정지 후: 드라이버에 가 있는 단계가 없는 배정·진행 작업을 실패 처리 (대기 작업은 남긴다)."""
        for order in list(self._active.values()):
            if order.state not in (OrderState.ASSIGNED, OrderState.RUNNING):
                continue
            inflight = self._inflight.get(order.robot) if order.robot else None
            if inflight is None or inflight[0] != order.id:
                self._finish(order, OrderState.FAILED, reason)

    # ================================================================== 내부: 배차
    def _available(self, rid: str, kind: str) -> bool:
        """rid 가 kind 작업을 새로 받을 수 있나 (쉬는 AMR 정의 + PARK 선점)."""
        drv = self._drivers[rid]
        st = drv.status()
        if not st.connected or rid in self._draining:
            return False
        cur = self._current.get(rid)
        if cur is not None:
            if self._orders[cur].kind != 'PARK' or kind == 'PARK':
                return False
            if st.mode not in (RobotMode.IDLE, RobotMode.MOVING):
                return False
        elif st.mode != RobotMode.IDLE or drv.busy or rid in self._inflight:
            return False
        return kind == 'CHARGE' or st.battery > self.charge_below

    def _dispatch(self) -> None:
        queued = sorted((o for o in self._active.values() if o.state == OrderState.QUEUED),
                        key=lambda o: (-o.priority, o.created, o.id))
        robot_ids = list(self._drivers)
        for order in queued:
            if order.state != OrderState.QUEUED:
                continue   # 앞선 처리(구독자 콜백 등)로 바뀜
            if order.robot is not None:
                if self._available(order.robot, order.kind):
                    self._assign(order, order.robot)
                continue
            first = order.steps[0].node
            best: tuple[tuple[float, int], str] | None = None
            for idx, rid in enumerate(robot_ids):
                if not self._available(rid, order.kind):
                    continue
                key = (self._distance(rid, first), idx)
                if best is None or key < best[0]:
                    best = (key, rid)
            if best is not None:
                self._assign(order, best[1])

    def _assign(self, order: TransportOrder, rid: str) -> None:
        cur = self._current.get(rid)
        if cur is not None:   # PARK 선점
            park = self._orders[cur]
            reason = f'새 작업 {order.id} 배정으로 주차 취소'
            inflight = self._inflight.get(rid)
            if inflight is not None and inflight[0] == park.id:
                self._cancel_req[park.id] = reason
                self._draining[rid] = park.id
                del self._current[rid]
                self._call(rid, 'cancel')
            else:
                self._finish(park, OrderState.CANCELLED, reason)
        if order.kind != 'PARK':   # 이 AMR 앞으로 대기 중인 주차 작업은 의미 없음
            for other in list(self._active.values()):
                if other.kind == 'PARK' and other.robot == rid and other.state == OrderState.QUEUED:
                    self._finish(other, OrderState.CANCELLED, f'새 작업 {order.id} 배정으로 주차 취소')
        self._current[rid] = order.id
        self._waiting.pop(rid, None)
        self._idle_since.pop(rid, None)
        if order.kind != 'PARK':
            self._settled.pop(rid, None)
            self._park_retry.pop(rid, None)
        order.robot = rid
        order.state = OrderState.ASSIGNED
        self._publish(order, 'assigned', robot=rid)

    def _distance(self, rid: str, node: str) -> float:
        start = self._drivers[rid].status().node
        return self.topology.distance(start, node) if start else math.inf

    # ================================================================== 내부: 충전·주차
    def _auto_charge(self) -> None:
        if _CHARGER not in self.topology.nodes:
            return
        charging = {o.robot for o in self._active.values() if o.kind == 'CHARGE'}
        for rid, drv in self._drivers.items():
            if rid in charging or rid in self._draining:
                continue
            cur = self._current.get(rid)
            if cur is not None and self._orders[cur].kind != 'PARK':
                continue
            st = drv.status()
            if not st.connected or st.mode in (RobotMode.ERROR, RobotMode.OFFLINE, RobotMode.PAUSED):
                continue
            if st.battery > self.charge_below:
                continue
            step = Step(id='', node=_CHARGER, action='charge', zone=_CHARGER, meta={'target': self.charge_target})
            self.new_order('CHARGE', [step], priority=_CHARGE_PRIORITY, robot=rid, meta={'auto': True}, now=self._now)

    def _auto_park(self) -> None:
        parks = self._park_nodes()
        if not parks:
            return
        # 구역 대기 중인 AMR 이 가려는 노드: 그 자리에 서 있는 쉬는 AMR 은 바로 비켜 준다
        wanted = self._wanted_nodes()
        for rid, drv in self._drivers.items():
            if rid in self._current or rid in self._draining or rid in self._inflight:
                continue
            st = drv.status()
            if not st.connected or st.mode != RobotMode.IDLE or drv.busy:
                self._idle_since[rid] = self._now
                continue
            if self._parked(st, parks) or self._settled.get(rid) == st.node or st.battery <= self.charge_below:
                continue
            if self._now < self._park_retry.get(rid, -math.inf):
                continue   # 방금 주차에 실패: park_after_s 동안은 다시 시도하지 않는다 (매 tick 실패 반복 방지)
            since = self._idle_since.setdefault(rid, self._now)
            if not (self._near_nodes(st) & wanted) and self._now - since < self.park_after_s:
                continue
            target = self._free_park(rid, st.node, parks)
            if target is None:
                continue
            order = self.new_order('PARK', [Step(id='', node=target)], priority=_PARK_PRIORITY, robot=rid,
                                   meta={'auto': True}, now=self._now)
            self._assign(order, rid)

    def _free_park(self, rid: str, node: str, parks: list[str]) -> str | None:
        taken = set()
        for other, drv in self._drivers.items():
            if other != rid:   # 서 있는 노드 + 구간 위에서 바짝 다가가 있는 노드
                st = drv.status()
                taken.add(st.node)
                taken |= self._near_nodes(st)
        for order in self._active.values():
            if order.robot != rid:
                taken.update(s.node for s in order.steps[order.current:])
        taken.update(step.node for other, step in self._aside.items() if other != rid)
        free = [p for p in parks if p not in taken]
        if not free:
            return None
        return min(free, key=lambda p: (self.topology.distance(node, p), parks.index(p)))

    # ================================================================== 내부: 단계 전송
    def _send_ready(self) -> None:
        ready = []
        for rid, oid in self._current.items():
            if rid in self._inflight or rid in self._draining:
                continue
            order = self._orders[oid]
            if order.state in (OrderState.ASSIGNED, OrderState.RUNNING):
                ready.append((-order.priority, order.created, order.id, rid))
        for _, _, oid, rid in sorted(ready):
            order = self._orders[oid]
            if order.state in (OrderState.ASSIGNED, OrderState.RUNNING) and self._current.get(rid) == oid:
                self._try_send(rid, order)

    def _try_send(self, rid: str, order: TransportOrder) -> None:
        step = order.steps[order.current]
        drv = self._drivers[rid]
        if step.zone:
            owner = self._zones.get(step.zone)
            blocker = owner if owner not in (None, rid) else self._occupant(step.node, rid)
            if blocker is not None:
                if self._waiting.get(rid) != (step.id, step.zone):
                    self._waiting[rid] = (step.id, step.zone)
                    self._publish(order, 'waiting', step=step, robot=rid,
                                  detail=f'{step.zone} 구역 대기 — {blocker} 사용 중')
                self._step_aside(rid, order)
                return
            self._zones[step.zone] = rid
        self._waiting.pop(rid, None)
        try:
            step.path = self._path_for(drv.status().node, step.node)
            drv.send_step(step)
        except Exception as exc:   # 경로 없음·드라이버 거절 → 작업 실패
            if step.zone:
                self._release(step.zone, rid)
            detail = f'단계 전송 실패: {exc}'
            self._publish(order, 'step_failed', step=step, robot=rid, detail=detail)
            if order.state in _ACTIVE:
                self._finish(order, OrderState.FAILED, detail)
            return
        self._inflight[rid] = (order.id, step.id)
        if order.state == OrderState.ASSIGNED:
            order.state = OrderState.RUNNING
            order.started = self._now

    def _step_aside(self, rid: str, order: TransportOrder) -> None:
        """구역 대기 중인 AMR 이 다른 대기 AMR 이 가려는 노드 위에 서 있으면 빈 PARK 로 비켜선다 (교착 방지)."""
        drv = self._drivers[rid]
        st = drv.status()
        if not st.connected or st.mode != RobotMode.IDLE or drv.busy:
            return   # 움직일 수 없는 AMR (연결 끊김·오류·정지) — 매 tick 전송 실패를 되풀이하지 않는다
        if not (self._near_nodes(st) & self._wanted_nodes(exclude=rid)):
            return
        parks = self._park_nodes()
        if self._parked(st, parks):
            return
        node = st.node
        target = self._free_park(rid, node, parks)
        if target is None:
            return
        self._aside_seq += 1
        aside = Step(id=f'{order.id}-AS{self._aside_seq}', node=target, meta={'aside': True})
        try:
            aside.path = self._path_for(node, target)
            drv.send_step(aside)
        except Exception:
            log.exception('%s: 비켜서기 이동 전송 실패', rid)
            return
        self._inflight[rid] = (order.id, aside.id)
        self._aside[rid] = aside

    def _wanted_nodes(self, exclude: str | None = None) -> set[str]:
        """구역 대기 중인 AMR 들이 가려는 노드."""
        wanted = set()
        for rid in self._waiting:
            order = self.robot_order(rid)
            if rid != exclude and order is not None and order.current < len(order.steps):
                wanted.add(order.steps[order.current].node)
        return wanted

    def _park_nodes(self) -> list[str]:
        return [n.id for n in self.topology.nodes.values() if n.kind == 'park']

    def _path_for(self, start: str, goal: str) -> list[str]:
        if not start or start not in self.topology.nodes:
            return [goal]   # 위치를 모르는 실제 AMR: 목적지만 주고 AMR 이 알아서 간다
        return self.topology.shortest_path(start, goal)

    def _occupant(self, node: str, rid: str) -> str | None:
        """node 를 차지한 다른 AMR (마지막 노드가 node 이거나, node 로 이어진 구간 위에서 node 바로 앞에 있음)."""
        for other, drv in self._drivers.items():
            if other != rid and node in self._near_nodes(drv.status()):
                return other
        return None

    def _edge_toward(self, st: RobotStatus) -> str | None:
        """AMR 이 마지막 노드에서 이웃 노드로 가는 구간 중간에 있으면 그 이웃 노드.
        노드 위에 서 있거나, 보고 좌표가 지도 구간 위에 있지 않으면(좌표를 안 주는 AMR 등) None."""
        nodes = self.topology.nodes
        a = nodes.get(st.node)
        if a is None or math.hypot(st.x - a.x, st.y - a.y) <= _AT_NODE_M:
            return None
        for nb, length in self.topology.neighbors(st.node):
            if length <= 0.0:
                continue
            b = nodes[nb]
            # 선분 a→b 위로 투영: 0 < t < 1 이고 선분에서 벗어난 거리가 작으면 그 구간 위
            t = ((st.x - a.x) * (b.x - a.x) + (st.y - a.y) * (b.y - a.y)) / (length * length)
            if 0.0 < t < 1.0:
                off = math.hypot(st.x - (a.x + t * (b.x - a.x)), st.y - (a.y + t * (b.y - a.y)))
                if off <= _ON_EDGE_M:
                    return nb
        return None

    def _near_nodes(self, st: RobotStatus) -> set[str]:
        """AMR 이 차지한 노드: 마지막 노드 + (구간 위라면) 바로 앞에 다가가 있는 다음 노드."""
        out = {st.node} if st.node else set()
        nb = self._edge_toward(st)
        if nb is not None:
            n = self.topology.nodes[nb]
            if math.hypot(st.x - n.x, st.y - n.y) < _NEAR_M:
                out.add(nb)
        return out

    def _parked(self, st: RobotStatus, parks: list[str]) -> bool:
        """주차 자리에 서 있나 (주차 자리를 떠나다 구간 위에 멈춘 AMR 은 아니다)."""
        return st.node in parks and self._edge_toward(st) is None

    # ================================================================== 내부: 공통
    def _finish(self, order: TransportOrder, state: OrderState, error: str = '') -> None:
        order.state = state
        order.finished = self._now
        if error:
            order.error = error
        if state == OrderState.DONE:
            order.current = len(order.steps)
        self._active.pop(order.id, None)
        self._cancel_req.pop(order.id, None)
        rid = order.robot
        if rid is not None:
            if self._current.get(rid) == order.id:
                del self._current[rid]
                self._waiting.pop(rid, None)
                self._release_all(rid)
                self._idle_since[rid] = self._now
            if self._draining.get(rid) == order.id:
                del self._draining[rid]
            if order.kind == 'PARK' and order.meta.get('auto'):
                if state == OrderState.DONE:
                    self._settled[rid] = order.steps[-1].node
                elif state == OrderState.FAILED:
                    self._park_retry[rid] = self._now + self.park_after_s
        self._publish(order, _FINISH_EVENT[state], robot=rid, detail=error)

    def _release(self, zone: str, rid: str) -> None:
        if self._zones.get(zone) == rid:
            del self._zones[zone]

    def _release_all(self, rid: str) -> None:
        for zone in [z for z, owner in self._zones.items() if owner == rid]:
            del self._zones[zone]

    def _apply_run_request(self) -> None:
        req, self._run_request = self._run_request, None
        if req is None:
            return
        for rid in self._drivers:
            self._call(rid, req)
        self._halted = req == 'pause'
        if req == 'resume':
            for rid in self._drivers:   # 정지해 있던 시간은 주차 대기 시간에 넣지 않는다
                if rid not in self._current:
                    self._idle_since[rid] = self._now

    def _call(self, rid: str, method: str) -> None:
        try:
            getattr(self._drivers[rid], method)()
        except Exception:
            log.exception('%s: 드라이버 %s 실패', rid, method)

    def _publish(self, order: TransportOrder, kind: str, step: Step | None = None, robot: str | None = None,
                 detail: str = '') -> None:
        event = OrderEvent(order=order, kind=kind, step=step, robot=robot, detail=detail)
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception:   # 구독자 오류가 작업 상태 기계를 깨지 않게
                log.exception('OrderEvent 구독자 오류 (%s %s)', order.id, kind)
