"""적재대 배치(슬롯 ID·높이·접근 노드)와 슬롯 배정 정책. 계약: 공개 시그니처 유지."""
from __future__ import annotations

import re
from dataclasses import replace

from .config import RackConfig
from .models import FACES, Box, Slot, SlotState

POLICIES = {'grade_zone': '같은 재종 열 우선', 'lowest_first': '아래 단 우선', 'balanced': '면 균형'}

# 슬롯 ID 표기 한계: 열은 2자리 표기, 단은 1자리 표기.
MAX_COLUMNS = 99
MAX_TIERS = 9

_SID_RE = re.compile(r'^([NS])-(\d{2,})-(\d+)$')
_FACE_ORDER = {face: i for i, face in enumerate(FACES)}   # N 먼저


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def slot_id(face: str, col: int, tier: int) -> str:
    """('N', 3, 2) -> 'N-03-2'"""
    if face not in FACES:
        raise ValueError(f'면은 N 또는 S 여야 합니다: {face!r}')
    if not _is_int(col) or col < 1:
        raise ValueError(f'열 번호는 1 이상의 정수여야 합니다: {col!r}')
    if not _is_int(tier) or tier < 1:
        raise ValueError(f'단 번호는 1 이상의 정수여야 합니다: {tier!r}')
    return f'{face}-{col:02d}-{tier}'


def parse_slot_id(sid: str) -> tuple[str, int, int]:
    """'N-03-2' -> ('N', 3, 2). 형식이 틀리면 ValueError."""
    if not isinstance(sid, str):
        raise ValueError(f'슬롯 ID 형식이 틀렸습니다: {sid!r} (예: N-03-2)')
    text = sid.strip().upper()
    m = _SID_RE.match(text)
    if not m:
        raise ValueError(f'슬롯 ID 형식이 틀렸습니다: {sid!r} (예: N-03-2)')
    face, col, tier = m.group(1), int(m.group(2)), int(m.group(3))
    if col < 1 or tier < 1:
        raise ValueError(f'슬롯 ID 의 열·단은 1부터입니다: {sid!r}')
    # 'N-003-2', 'N-03-02' 처럼 표준 표기가 아닌 것은 거부 (같은 칸에 ID 가 둘이 되지 않게)
    if slot_id(face, col, tier) != text:
        raise ValueError(f'슬롯 ID 형식이 틀렸습니다: {sid!r} (예: N-03-2)')
    return face, col, tier


class RackLayout:
    """양면 적재대. 면 'N','S' × columns × tiers."""

    def __init__(self, cfg: RackConfig):
        if not _is_int(cfg.columns) or not 1 <= cfg.columns <= MAX_COLUMNS:
            raise ValueError(f'열 수는 1~{MAX_COLUMNS} 사이여야 합니다: {cfg.columns!r}')
        if not _is_int(cfg.tiers) or not 1 <= cfg.tiers <= MAX_TIERS:
            raise ValueError(f'단 수는 1~{MAX_TIERS} 사이여야 합니다: {cfg.tiers!r}')
        self.cfg: RackConfig = replace(cfg)          # 바깥에서 설정을 바꿔도 배치가 흔들리지 않게 복사
        self.faces: tuple[str, ...] = FACES
        self.columns: int = cfg.columns
        self.tiers: int = cfg.tiers
        self._ids: list[str] = [slot_id(f, c, t)
                                for f in FACES
                                for c in range(1, self.columns + 1)
                                for t in range(1, self.tiers + 1)]
        self._id_set = frozenset(self._ids)

    @property
    def capacity(self) -> int:
        return len(self._ids)

    def slot_ids(self) -> list[str]:
        """면 N→S, 열 1→, 단 1→ 순서."""
        return list(self._ids)

    def contains(self, sid: str) -> bool:
        """이 배치에 있는 칸인지 (표준 표기 기준)."""
        return sid in self._id_set

    def fork_height(self, tier: int) -> float:
        """tier_base + (tier-1) × tier_pitch (m)."""
        if not _is_int(tier) or not 1 <= tier <= self.tiers:
            raise ValueError(f'단 번호는 1~{self.tiers} 사이여야 합니다: {tier!r}')
        return round(self.cfg.tier_base_m + (tier - 1) * self.cfg.tier_pitch_m, 6)

    def access_node(self, sid: str) -> str:
        """'N-03-2' -> 'N03' (그 열 정면 통로 노드)."""
        face, col, tier = parse_slot_id(sid)
        if col > self.columns or tier > self.tiers:
            raise ValueError(f'적재대에 없는 칸입니다: {sid}')
        return f'{face}{col:02d}'

    def column_x(self, col: int) -> float:
        """열 중심 x (m, 적재대 도크 쪽 끝 = 0)."""
        if not _is_int(col) or not 1 <= col <= self.columns:
            raise ValueError(f'열 번호는 1~{self.columns} 사이여야 합니다: {col!r}')
        return round((col - 0.5) * self.cfg.col_pitch_m, 6)


def _low_key(s: Slot) -> tuple[int, int, int]:
    """lowest_first 순서: 아래 단 → 도크 가까운 열 → N 면."""
    return s.tier, s.col, _FACE_ORDER.get(s.face, 9)


class SlotAllocator:
    """빈 칸 고르기. SPEC §6. 결정적이어야 한다."""

    def __init__(self, layout: RackLayout, policy: str = 'grade_zone'):
        if policy not in POLICIES:
            raise ValueError(f'알 수 없는 배정 정책: {policy!r}')
        self.layout = layout
        self.policy = policy

    def choose(self, grade: str, slots: list[Slot], boxes: dict[str, Box], exclude: set[str] | None = None) -> str | None:
        """grade 박스를 넣을 슬롯 ID. 빈 칸이 없으면 None.
        slots: 현재 전체 슬롯 상태, boxes: 박스 ID → Box (OCCUPIED 칸 재종 확인용), exclude: 이번에 이미 고른 칸."""
        excluded = set(exclude or ())
        free = [s for s in slots
                if s.state == SlotState.EMPTY and s.box_id is None and s.reserved_for is None and s.id not in excluded]
        if not free:
            return None
        if self.policy == 'lowest_first':
            return min(free, key=_low_key).id
        if self.policy == 'balanced':
            return self._balanced(free, slots, excluded)
        return self._grade_zone(grade, free, slots, boxes, excluded)

    # ---- 정책별
    @staticmethod
    def _excluded_columns(excluded: set[str]) -> set[tuple[str, int]]:
        cols = set()
        for sid in excluded:
            try:
                face, col, _tier = parse_slot_id(sid)
            except ValueError:
                continue
            cols.add((face, col))
        return cols

    def _balanced(self, free: list[Slot], slots: list[Slot], excluded: set[str]) -> str:
        # 면별 박스 수 = 적재 + 예약(곧 들어올 박스) + 이번에 고른 칸
        count = {face: 0 for face in FACES}
        for s in slots:
            if s.state in (SlotState.OCCUPIED, SlotState.RESERVED) or s.id in excluded:
                count[s.face] = count.get(s.face, 0) + 1
        # 박스 적은 면 → (같으면 N 면) → 그 면 안에서 lowest_first
        return min(free, key=lambda s: (count.get(s.face, 0), _FACE_ORDER.get(s.face, 9), s.tier, s.col)).id

    def _grade_zone(self, grade: str, free: list[Slot], slots: list[Slot], boxes: dict[str, Box],
                    excluded: set[str]) -> str:
        def holder_grade(s: Slot) -> str | None:
            # 적재 칸은 든 박스, 예약 칸은 들어올 박스의 재종 (같은 작업 안 여러 박스도 같은 열로 모이게)
            bid = s.box_id if s.state == SlotState.OCCUPIED else s.reserved_for if s.state == SlotState.RESERVED else None
            box = boxes.get(bid) if bid else None
            return box.grade if box else None

        # 1) 같은 재종이 이미 있는 열의 빈 칸 (아래 단부터, 그다음 도크 가까운 열, N 면)
        same_cols = {(s.face, s.col) for s in slots if holder_grade(s) == grade}
        cand = [s for s in free if (s.face, s.col) in same_cols]
        if cand:
            return min(cand, key=_low_key).id
        # 2) 완전히 빈 열 중 도크에서 가까운 열의 맨 아래 (같은 번호 열이면 N 면 먼저)
        used_cols = {(s.face, s.col) for s in slots if s.state in (SlotState.OCCUPIED, SlotState.RESERVED)}
        used_cols |= self._excluded_columns(excluded)
        cand = [s for s in free if (s.face, s.col) not in used_cols]
        if cand:
            return min(cand, key=lambda s: (s.col, _FACE_ORDER.get(s.face, 9), s.tier)).id
        # 3) 아무 빈 칸 (아래 단, 도크 가까운 열)
        return min(free, key=_low_key).id
