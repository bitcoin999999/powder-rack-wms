"""AMR 제어 모델 (공유 계약 — 필드 이름·의미를 바꾸지 말 것)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RobotMode(str, Enum):
    IDLE = 'IDLE'
    MOVING = 'MOVING'
    LOADING = 'LOADING'        # 포크로 집는 중
    UNLOADING = 'UNLOADING'    # 포크로 내려놓는 중
    WAITING = 'WAITING'        # 구역 잠금·교통 대기
    CHARGING = 'CHARGING'
    PAUSED = 'PAUSED'
    ERROR = 'ERROR'
    OFFLINE = 'OFFLINE'


MODE_LABEL = {
    'IDLE': '대기', 'MOVING': '주행', 'LOADING': '집는 중', 'UNLOADING': '내려놓는 중', 'WAITING': '구역 대기',
    'CHARGING': '충전', 'PAUSED': '일시정지', 'ERROR': '오류', 'OFFLINE': '연결 끊김',
}


class OrderState(str, Enum):
    QUEUED = 'QUEUED'          # 배차 전
    ASSIGNED = 'ASSIGNED'      # AMR 지정, 첫 단계 전송 전
    RUNNING = 'RUNNING'
    DONE = 'DONE'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'


ORDER_LABEL = {'QUEUED': '대기', 'ASSIGNED': '배정', 'RUNNING': '진행', 'DONE': '완료', 'FAILED': '실패', 'CANCELLED': '취소'}
ACTIONS = ('none', 'pick', 'drop', 'charge')


@dataclass
class RobotStatus:
    robot_id: str
    mode: RobotMode = RobotMode.IDLE
    node: str = ''                     # 마지막으로 지난(또는 서 있는) 노드
    x: float = 0.0                     # m, 셀 지도 좌표
    y: float = 0.0
    heading: float = 0.0               # rad
    battery: float = 100.0             # %
    trays: list[str | None] = field(default_factory=list)   # 사다리 단별 박스 ID (0 = 맨 아래)
    fork_height: float = 0.0           # m
    message: str = ''
    connected: bool = True
    updated_at: float = 0.0            # 단조 시각 (s)


@dataclass
class Step:
    id: str                            # 작업 안에서 유일 ('O0001-S1')
    node: str                          # 목적 노드
    action: str = 'none'               # ACTIONS
    height_m: float = 0.0              # 포크 높이 (pick/drop)
    tray: int | None = None            # 사다리 단 (pick: 실을 단, drop: 내릴 단)
    ref: str = ''                      # 박스 ID 또는 슬롯 ID (드라이버가 업체 API로 넘길 참조)
    zone: str | None = None            # 잠글 구역 (예: 'DOCK'). None = 잠금 없음
    path: list[str] = field(default_factory=list)  # FleetManager가 보낼 때 채움 (현재 노드 → node)
    meta: dict = field(default_factory=dict)       # 예) {'box': 'W261020-0001', 'slot': 'N-03-2'}


@dataclass
class TransportOrder:
    id: str                            # 'O0001'
    kind: str                          # INBOUND / OUTBOUND / MOVE / CHARGE / PARK
    steps: list[Step]
    state: OrderState = OrderState.QUEUED
    robot: str | None = None           # 지정 AMR (None = FleetManager가 고름)
    current: int = 0                   # 진행 중 단계 번호
    created: float = 0.0
    started: float | None = None
    finished: float | None = None
    error: str = ''
    priority: int = 0                  # 클수록 먼저
    meta: dict = field(default_factory=dict)


@dataclass
class DriverEvent:
    robot_id: str
    kind: str                          # step_started / step_done / step_failed / status / connected / disconnected / error
    step_id: str | None = None
    detail: str = ''


@dataclass
class OrderEvent:
    """FleetManager → 구독자 (Controller·화면)."""
    order: TransportOrder
    kind: str                          # created / assigned / step_started / step_done / step_failed / done / failed / cancelled / waiting
    step: Step | None = None
    robot: str | None = None
    detail: str = ''
