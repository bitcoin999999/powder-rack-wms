"""재고 도메인 모델 (공유 계약 — 필드 이름·의미를 바꾸지 말 것)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SlotState(str, Enum):
    EMPTY = 'EMPTY'
    RESERVED = 'RESERVED'    # AMR가 넣으러 오는 중 (reserved_for = 박스 ID)
    OCCUPIED = 'OCCUPIED'
    BLOCKED = 'BLOCKED'      # 점검·고장 칸. 배정 제외


class BoxState(str, Enum):
    CONVEYOR = 'CONVEYOR'    # 오버브릿지 롤러컨베이어 위
    ON_AMR = 'ON_AMR'        # AMR 사다리 트레이 위
    STORED = 'STORED'        # 적재대 칸 안
    OUT = 'OUT'              # 출고됨 (기록만 남음)


ISO_CLASSES = ('P', 'M', 'K', 'N', 'S', 'H')
FACES = ('N', 'S')
FACE_LABEL = {'N': '북면', 'S': '남면'}


@dataclass
class Grade:
    code: str                # 예) PC9030
    iso_class: str           # P M K N S H 중 하나
    iso_range: str           # 예) M20–M35
    kind: str                # CVD / PVD / 무코팅 / 서멧 …
    description: str         # 용도 설명 (한 줄)
    color: str               # '#RRGGBB' 화면 색
    active: bool = True


@dataclass
class Box:
    id: str                  # 박스 ID (바코드 또는 자동 발번 W{yymmdd}-{seq4})
    grade: str               # 재종 코드
    lot: str                 # 완분 로트
    weight_kg: float = 15.0
    bags: int = 3
    state: BoxState = BoxState.CONVEYOR
    slot_id: str | None = None        # STORED: 들어 있는 칸, CONVEYOR/ON_AMR: 예약된 목적지 칸
    order_id: str | None = None       # 묶여 있는 AMR 작업 (없으면 수동 처리 가능)
    robot: str | None = None          # ON_AMR 일 때 실은 AMR
    arrived_seq: int = 0              # 컨베이어 도착 순번 (컨베이어 순서)
    arrived_at: str = ''              # ISO 시각 'YYYY-MM-DD HH:MM:SS'
    stored_at: str | None = None
    out_at: str | None = None
    needs_check: bool = False         # 작업 중단으로 실제 위치 확인 필요
    note: str = ''


@dataclass
class Slot:
    id: str                  # 'N-03-2'
    face: str                # 'N' | 'S'
    col: int                 # 1..columns (도크 쪽이 1)
    tier: int                # 1..tiers (1 = 맨 아래)
    state: SlotState = SlotState.EMPTY
    box_id: str | None = None         # OCCUPIED 일 때
    reserved_for: str | None = None   # RESERVED 일 때 박스 ID
    blocked_reason: str = ''
    updated_at: str = ''


@dataclass
class Event:
    id: int | None
    ts: str
    kind: str                # ARRIVE RESERVE UNRESERVE PICK STORE OUT MOVE CORRECT BLOCK UNBLOCK ORDER ERROR MASTER CONFIG
    box_id: str | None = None
    slot_id: str | None = None
    grade: str | None = None
    robot: str | None = None
    order_id: str | None = None
    detail: str = ''


@dataclass
class StockRow:
    """출고 조회 결과 한 줄 (FIFO 정렬)."""
    slot: Slot
    box: Box
    age_s: float             # 입고 후 경과 초


@dataclass
class InventorySnapshot:
    """화면용 요약."""
    capacity: int
    occupied: int
    reserved: int
    blocked: int
    conveyor: int
    by_grade: dict[str, int] = field(default_factory=dict)
    needs_check: int = 0
