"""가상 AMR — 목업·시험용. 계약: 공개 시그니처 유지.

동작 모델 (SimConfig):
- 이동: path 의 노드를 차례로 speed_mps 로 직선 이동 (노드마다 정지·회전은 무시). 이동 거리만큼 battery_per_m 소모.
- pick/drop: 시간 = (|현재 포크 높이 − 대상 높이| + |대상 높이 − 트레이 높이|) / lift_mps + fork_time_s.
  pick 완료 시 trays[tray] = step.ref (박스 ID), drop 완료 시 trays[tray] = None. 트레이 높이 = tray_base + tray × tray_pitch.
  pick 하려는 트레이가 차 있거나 drop 하려는 트레이가 비어 있으면 step_failed.
- charge: CHARGER 노드에서 charge_per_s 로 step.meta.get('target', 90) 까지.
- 항상 battery_per_s 로 조금씩 소모 (충전 중 제외). 배터리 0 → ERROR + step_failed('battery').
- tick(now, dt) 로만 시간이 흐른다 (결정적). dt 가 커도(고배속) 정확해야 한다 — 한 tick 안에서 여러 노드를 지나고
  남은 시간으로 다음 동작까지 진행.
- inject_failure(reason): 다음(또는 진행 중) 단계를 실패시킨다 (시험용).

구현 메모
- tick 은 "다음 사건(노드 도착·동작 끝·충전 끝·방전)까지 시간을 정확히 잘라서" 진행하므로 dt 크기와 무관하게
  같은 결과가 나온다 (부동소수 오차 수준 차이만).
- 같은 now 로 tick 이 두 번 오면 두 번째는 무시한다 (FleetManager 와 Controller 가 둘 다 드라이버를 tick 해도
  시간이 두 배로 흐르지 않게).
- 단계 시작(step_started)은 send_step 안에서 바로 낸다. 트레이·충전기 조건이 안 맞으면 그 자리에서 step_failed.
  단계 값 형식이 잘못되면(tray 가 정수가 아님 등) 아무 상태도 바꾸지 않고 ValueError.
"""
from __future__ import annotations

import math

from core.config import SimConfig

from .driver import AmrDriver
from .models import ACTIONS, RobotMode, RobotStatus, Step
from .topology import Topology

_EPS = 1e-9          # 시간 비교 여유 (s): 사건 시각이 tick 끝과 이만큼 안쪽이면 이번 tick 에 처리
_MAX_LOOP = 100_000  # tick 한 번 안의 사건 처리 상한 (무한 루프 방지)


class SimulatedAmrDriver(AmrDriver):
    kind = 'sim'

    def __init__(self, robot_id: str, topology: Topology, cfg: SimConfig, start_node: str, trays: int | None = None):
        super().__init__(robot_id)
        if start_node not in topology.nodes:
            raise ValueError(f'{robot_id}: 시작 노드가 지도에 없습니다 ({start_node})')
        if cfg.speed_mps <= 0 or cfg.lift_mps <= 0:
            raise ValueError(f'{robot_id}: 주행·승강 속도는 0보다 커야 합니다')
        if cfg.charge_per_s <= 0:   # 0 이면 충전 단계가 끝나지 않아 충전기(구역)를 영원히 잡는다
            raise ValueError(f'{robot_id}: 충전 속도(charge_per_s)는 0보다 커야 합니다')
        if cfg.battery_per_s < 0 or cfg.battery_per_m < 0 or cfg.fork_time_s < 0:
            raise ValueError(f'{robot_id}: 배터리 소모량·포크 시간은 0 이상이어야 합니다')
        self._topo = topology
        self._cfg = cfg
        start = topology.nodes[start_node]
        self._node = start_node                 # 마지막으로 지난(또는 서 있는) 노드
        self._x, self._y = start.x, start.y
        self._heading = 0.0
        self._toward: str | None = None         # 구간 중간에 멈췄을 때 향하던 노드
        self._battery = 100.0
        count = cfg.trays if trays is None else trays
        self._trays: list[str | None] = [None] * max(0, int(count))
        self._fork = 0.0
        self._connected = True                  # 가상 AMR 은 처음부터 연결된 것으로 본다
        self._paused = False
        self._error = ''                        # '' = 정상, 그 외 = 오류 사유 (ERROR 모드)
        self._pending_failure: str | None = None
        self._last_now: float | None = None
        self._updated_at = 0.0
        # 진행 중 단계
        self._step: Step | None = None
        self._phase = ''                        # 'move' | 'action' | 'charge'
        self._route: list[tuple[str, float, float]] = []   # 남은 목표점 (노드, x, y)
        self._tray_index: int | None = None
        self._plan = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)          # 포크 계획: f0, 대상 높이, 트레이 높이, t1, t2, t3
        self._elapsed = 0.0                     # 동작 경과 (s)
        self._charge_target = 90.0

    # ------------------------------------------------------------------ 연결
    def connect(self) -> None:
        self._connected = True
        self._emit('connected')

    def disconnect(self) -> None:
        if self._step is not None:
            self._fail('연결 끊김')
        self._connected = False
        self._emit('disconnected')

    # ------------------------------------------------------------------ 명령
    def send_step(self, step: Step) -> None:
        if self._step is not None:
            raise RuntimeError(f'{self.robot_id}: 이미 진행 중인 단계가 있습니다 ({self._step.id})')
        if not self._connected:
            raise RuntimeError(f'{self.robot_id}: 연결이 끊겨 단계를 받을 수 없습니다')
        if self._error:
            raise RuntimeError(f'{self.robot_id}: 오류 상태입니다 ({self._error}) — 재개 후 다시 보내세요')
        if step.action not in ACTIONS:
            raise ValueError(f'알 수 없는 동작: {step.action}')
        route = self._build_route(step)

        self._step = step
        self._route = route
        self._phase = 'move'
        self._elapsed = 0.0
        try:
            problem = self._check_action(step)
        except (TypeError, ValueError) as exc:   # 값 형식 오류 (예: tray='0', target='abc'): 받지 않은 것으로 되돌린다
            self._clear_step()
            raise ValueError(f'{self.robot_id}: 단계 값 오류 ({step.id}) — {exc}') from exc
        self._emit('step_started', step.id)

        if self._pending_failure is not None:
            reason, self._pending_failure = self._pending_failure, None
            self._fail(reason)
            return
        if problem:
            self._fail(problem)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._error = ''      # 배터리가 0 이면 다음 tick 에 다시 ERROR

    def cancel(self) -> None:
        if self._step is not None:
            self._fail('cancelled')

    def estop(self) -> None:
        self._error = '비상정지'
        if self._step is not None:
            self._fail('estop')

    def inject_failure(self, reason: str = '시험 실패') -> None:
        if self._step is not None:
            self._fail(reason)
        else:
            self._pending_failure = reason

    def manual_unload(self, box_id: str) -> bool:
        """사람이 트레이에서 박스를 내림 → 그 트레이를 비운다 (대소문자 무시). 없으면 False.
        진행 중인 집기·내려놓기 단계가 그 트레이를 쓰고 있으면(이동 중 포함) 거부하고 False."""
        if not isinstance(box_id, str) or not box_id.strip():
            return False
        key = box_id.strip().upper()
        for i, box in enumerate(self._trays):
            if box is not None and box.upper() == key:
                if self._step is not None and self._tray_index == i:
                    return False
                self._trays[i] = None
                return True
        return False

    # ------------------------------------------------------------------ 상태
    def status(self) -> RobotStatus:
        return RobotStatus(
            robot_id=self.robot_id,
            mode=self._mode(),
            node=self._node,
            x=self._x,
            y=self._y,
            heading=self._heading,
            battery=self._battery,
            trays=list(self._trays),
            fork_height=self._fork_now(),
            message=self._message(),
            connected=self._connected,
            updated_at=self._updated_at,
        )

    @property
    def busy(self) -> bool:
        return self._step is not None

    # ------------------------------------------------------------------ 시간
    def tick(self, now: float, dt: float) -> None:
        if self._last_now is not None and now == self._last_now and dt > 0:
            return   # 같은 시각 중복 tick
        self._last_now = now
        self._updated_at = now
        left = dt if dt > 0 else 0.0
        for _ in range(_MAX_LOOP):
            used, changed = self._advance(left)
            left -= used
            if left <= _EPS and not changed:
                break

    def _advance(self, t: float) -> tuple[float, bool]:
        """현재 상태를 최대 t 초 진행. (쓴 시간, 시간 없이 상태가 바뀌었는지) 를 돌려준다."""
        t = max(t, 0.0)
        if not self._error and self._battery <= 0.0:
            self._battery_empty()
            return 0.0, True
        active = self._step is not None and self._connected and not self._paused and not self._error
        if not active:
            return self._advance_idle(t)
        if self._phase == 'move':
            return self._advance_move(t)
        if self._phase == 'action':
            return self._advance_action(t)
        return self._advance_charge(t)

    def _advance_idle(self, t: float) -> tuple[float, bool]:
        rate = self._cfg.battery_per_s
        if rate <= 0:
            return t, False
        if self._error:   # 오류 중에도 대기 소모, 0 아래로는 안 내려감
            self._battery = max(0.0, self._battery - rate * t)
            return t, False
        to_zero = self._battery / rate
        if to_zero <= t:
            self._battery = 0.0
            self._battery_empty()
            return to_zero, True
        self._battery -= rate * t
        return t, False

    def _advance_move(self, t: float) -> tuple[float, bool]:
        if not self._route:
            self._arrive()
            return 0.0, True
        cfg = self._cfg
        nid, tx, ty = self._route[0]
        dx, dy = tx - self._x, ty - self._y
        rem = math.hypot(dx, dy)
        if rem <= 1e-12:
            self._reach(nid, tx, ty)
            return 0.0, True
        self._heading = math.atan2(dy, dx)
        v = cfg.speed_mps
        seg_time = rem / v
        rate = cfg.battery_per_s + cfg.battery_per_m * v      # % / s (주행 중)
        bat_time = self._battery / rate if rate > 0 else math.inf
        if bat_time < seg_time and bat_time <= t:              # 구간 도중 방전
            self._shift(dx, dy, rem, v * bat_time)
            self._battery = 0.0
            self._battery_empty()
            return bat_time, True
        if seg_time <= t + _EPS:                               # 이번 tick 안에 노드 도착
            self._battery = max(0.0, self._battery - rate * seg_time)
            self._reach(nid, tx, ty)
            return seg_time, True
        self._shift(dx, dy, rem, v * t)
        self._battery -= rate * t
        return t, False

    def _advance_action(self, t: float) -> tuple[float, bool]:
        total = self._plan[3] + self._plan[4] + self._plan[5]
        left = total - self._elapsed
        rate = self._cfg.battery_per_s
        bat_time = self._battery / rate if rate > 0 else math.inf
        if bat_time < left and bat_time <= t:
            self._elapsed += bat_time
            self._battery = 0.0
            self._battery_empty()
            return bat_time, True
        if left <= t + _EPS:
            self._battery = max(0.0, self._battery - rate * max(left, 0.0))
            self._finish_action()
            return max(left, 0.0), True
        self._elapsed += t
        self._battery -= rate * t
        return t, False

    def _advance_charge(self, t: float) -> tuple[float, bool]:
        need = self._charge_target - self._battery
        if need <= 0:
            self._complete()
            return 0.0, True
        rate = self._cfg.charge_per_s
        if rate <= 0:
            return t, False
        needed = need / rate
        if needed <= t + _EPS:
            self._battery = self._charge_target
            self._complete()
            return needed, True
        self._battery += rate * t
        return t, False

    # ------------------------------------------------------------------ 내부: 이동·동작
    def _build_route(self, step: Step) -> list[tuple[str, float, float]]:
        nodes = self._topo.nodes
        ids = list(step.path) if step.path else self._topo.shortest_path(self._node, step.node)
        for nid in ids + [step.node]:
            if nid not in nodes:
                raise ValueError(f'지도에 없는 노드: {nid}')
        if ids[-1] != step.node:   # 경로 끝이 목적지가 아니면 최단 경로로 이어 붙임
            ids += self._topo.shortest_path(ids[-1], step.node)[1:]
        # 구간 중간에 멈춰 있다가 같은 방향으로 다시 가는 경우: 지난 노드로 되돌아가지 않는다
        if self._toward is not None and len(ids) >= 2 and ids[0] == self._node and ids[1] == self._toward:
            ids = ids[1:]
        return [(nid, nodes[nid].x, nodes[nid].y) for nid in ids]

    def _check_action(self, step: Step) -> str:
        """동작 조건 검사. 문제 있으면 사유(한국어), 없으면 ''."""
        cfg = self._cfg
        if step.action == 'pick':
            tray = step.tray if step.tray is not None else self._first_tray(lambda b: b is None)
            if tray is None:
                return '빈 트레이가 없습니다'
            if not 0 <= tray < len(self._trays):
                return f'트레이 번호 오류: {tray}'
            if self._trays[tray] is not None:
                return f'{tray}단 트레이에 이미 박스가 있습니다 ({self._trays[tray]})'
        elif step.action == 'drop':
            tray = step.tray
            if tray is None:
                box = step.meta.get('box')
                tray = self._first_tray(lambda b: b is not None and b == box)
                if tray is None:
                    tray = self._first_tray(lambda b: b is not None)
                if tray is None:
                    return '내려놓을 박스가 없습니다 (트레이가 모두 비어 있음)'
            if not 0 <= tray < len(self._trays):
                return f'트레이 번호 오류: {tray}'
            if self._trays[tray] is None:
                return f'{tray}단 트레이가 비어 있습니다'
        elif step.action == 'charge':
            node = self._topo.nodes[step.node]
            if node.kind != 'charger':
                return f'충전기 노드가 아닙니다: {step.node}'
            self._charge_target = min(100.0, float(step.meta.get('target', 90)))
            return ''
        else:
            return ''
        self._tray_index = tray
        tray_h = cfg.tray_base_m + tray * cfg.tray_pitch_m
        self._plan = (0.0, step.height_m, tray_h, 0.0, cfg.fork_time_s, 0.0)   # f0·t1·t3 는 도착 시 확정
        return ''

    def _first_tray(self, cond) -> int | None:
        for i, box in enumerate(self._trays):
            if cond(box):
                return i
        return None

    def _shift(self, dx: float, dy: float, rem: float, dist: float) -> None:
        self._x += dx / rem * dist
        self._y += dy / rem * dist

    def _reach(self, nid: str, x: float, y: float) -> None:
        self._x, self._y = x, y
        self._node = nid
        self._toward = None
        self._route.pop(0)

    def _arrive(self) -> None:
        """경로 끝 도착 → 동작 단계로."""
        step = self._step
        assert step is not None
        if step.action in ('pick', 'drop'):
            lift = self._cfg.lift_mps
            _, target, tray_h, _, t2, _ = self._plan
            f0 = self._fork
            self._plan = (f0, target, tray_h, abs(f0 - target) / lift, t2, abs(target - tray_h) / lift)
            self._elapsed = 0.0
            self._phase = 'action'
        elif step.action == 'charge':
            self._phase = 'charge'
        else:
            self._complete()

    def _finish_action(self) -> None:
        step = self._step
        assert step is not None and self._tray_index is not None
        self._trays[self._tray_index] = step.ref if step.action == 'pick' else None
        self._fork = self._plan[2]     # 동작이 끝나면 포크는 트레이 높이
        self._complete()

    def _fork_now(self) -> float:
        if self._phase != 'action' or self._step is None:
            return self._fork
        f0, target, tray_h, t1, t2, t3 = self._plan
        e = self._elapsed
        if e < t1:
            return f0 + (target - f0) * (e / t1)
        if e < t1 + t2 or t3 <= 0:
            return target
        return target + (tray_h - target) * min(1.0, (e - t1 - t2) / t3)

    # ------------------------------------------------------------------ 내부: 단계 종료
    def _complete(self) -> None:
        step = self._step
        assert step is not None
        self._clear_step()
        self._emit('step_done', step.id)

    def _fail(self, reason: str) -> None:
        step = self._step
        assert step is not None
        self._fork = self._fork_now()
        if self._phase == 'move' and self._route and math.hypot(
                self._x - self._topo.nodes[self._node].x, self._y - self._topo.nodes[self._node].y) > 1e-9:
            self._toward = self._route[0][0]    # 구간 중간에 멈춤
        self._clear_step()
        self._emit('step_failed', step.id, reason)

    def _clear_step(self) -> None:
        self._step = None
        self._phase = ''
        self._route = []
        self._tray_index = None
        self._elapsed = 0.0

    def _battery_empty(self) -> None:
        self._battery = 0.0
        self._error = '배터리 방전'
        if self._step is not None:
            self._fail('battery')

    # ------------------------------------------------------------------ 내부: 표시
    def _mode(self) -> RobotMode:
        if not self._connected:
            return RobotMode.OFFLINE
        if self._error:
            return RobotMode.ERROR
        if self._paused:
            return RobotMode.PAUSED
        if self._step is None:
            return RobotMode.IDLE
        if self._phase == 'action':
            return RobotMode.LOADING if self._step.action == 'pick' else RobotMode.UNLOADING
        if self._phase == 'charge':
            return RobotMode.CHARGING
        return RobotMode.MOVING

    def _message(self) -> str:
        if self._error:
            return self._error
        if self._paused:
            return '일시정지'
        step = self._step
        if step is None:
            return ''
        if self._phase == 'move':
            return f'{step.node} 로 이동'
        if self._phase == 'charge':
            return f'충전 중 (목표 {self._charge_target:.0f}%)'
        what = '집기' if step.action == 'pick' else '내려놓기'
        return f'{step.node} {what} ({step.ref})' if step.ref else f'{step.node} {what}'
