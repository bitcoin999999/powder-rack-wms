"""AMR 드라이버 추상 클래스 — 업체가 바뀌어도 FleetManager는 이것만 안다. 계약: 공개 시그니처 유지.

규칙
- send_step 은 즉시 돌아온다 (비동기). 결과는 이벤트로: step_started → step_done | step_failed.
- 이벤트는 어느 스레드에서 와도 된다: _emit() 이 스레드 안전 큐에 넣고, FleetManager 가 메인 스레드에서 poll() 로 꺼낸다.
- 한 번에 한 단계만. 진행 중에 send_step 이 오면 RuntimeError.
- cancel(): 진행 중 단계를 멈추고 step_failed(detail='cancelled'). estop(): 즉시 정지, 모드 ERROR, 진행 중 단계 step_failed('estop').
  resume() 으로 ERROR/PAUSED 해제.
"""
from __future__ import annotations

import queue
from abc import ABC, abstractmethod

from .models import DriverEvent, RobotStatus, Step


class AmrDriver(ABC):
    kind = 'base'

    def __init__(self, robot_id: str):
        self.robot_id = robot_id
        self._events: 'queue.Queue[DriverEvent]' = queue.Queue()

    # ---- 이벤트 큐 (구현 완료 — 하위 클래스는 _emit 만 쓰면 된다)
    def _emit(self, kind: str, step_id: str | None = None, detail: str = '') -> None:
        self._events.put(DriverEvent(self.robot_id, kind, step_id, detail))

    def poll(self) -> list[DriverEvent]:
        out: list[DriverEvent] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # ---- 하위 클래스 구현
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def send_step(self, step: Step) -> None:
        """step.path (현재 노드 → step.node) 를 따라 가서 step.action 수행."""

    @abstractmethod
    def pause(self) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def cancel(self) -> None: ...

    @abstractmethod
    def estop(self) -> None: ...

    @abstractmethod
    def status(self) -> RobotStatus: ...

    @property
    def busy(self) -> bool:
        """단계 진행 중."""
        return False

    def tick(self, now: float, dt: float) -> None:
        """시뮬레이션 드라이버만 쓴다 (실제 드라이버는 아무것도 안 함)."""

    def manual_unload(self, box_id: str) -> bool:
        """사람이 AMR 트레이에서 박스를 직접 내렸다(확인 필요 박스 정리) — 드라이버의 트레이 표시에서 그 박스를 뺀다.
        뺐으면 True. 기본은 False (실제 AMR 은 자기 센서로 트레이 상태를 다시 보고한다)."""
        return False
