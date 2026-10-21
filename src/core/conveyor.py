"""오버브릿지 자동 출고 시뮬레이션 (목업용 박스 발생기). 계약: 공개 시그니처 유지."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .config import SimConfig

_EPS = 1e-9          # 부동소수 누적 오차 허용 (0.1 s × 900 번 = 90 s 가 정확히 맞지 않는 문제)
FULL_WEIGHT_KG = 15.0


@dataclass
class Arrival:
    grade: str
    lot: str         # 빈 문자열 = 재고 서비스가 자동 발번 (register_arrival 의 lot 자동 규칙)
    weight_kg: float


class OverbridgeSimulator:
    """start() 후 first_arrival_s 뒤 첫 박스, 이후 release_interval_s 마다 1박스, session_boxes 개가 나오면 멈춤.
    tick(dt, can_accept) 에서 can_accept() 가 False(컨베이어 만재)면 박스를 내보내지 않고 기다린다(라인 정지 — 타이머 멈춤).
    재종은 grades 목록에서 가중 난수 (시드 고정, 재현 가능). 1/16(residual_prob) 확률로 중량 10~14.9 kg (잔량봉).

    - can_accept 는 박스를 내보내기 직전마다 한 번 불린다. 한 tick 에서 여러 박스가 나올 수 있으므로(큰 dt),
      컨베이어 수를 세는 쪽은 tick 을 짧게(예: 0.2 s) 잘라 부르는 것이 정확하다. None 이면 항상 받는다.
    - 출고할 재종이 하나도 없으면(set_grades([])) 시각이 되어도 내보내지 않고 기다린다 (라인 정지 표시는 하지 않음).
    - 난수는 생성 시 cfg.seed 로 한 번 고정 → 같은 설정·같은 호출 순서면 같은 결과.
    - 로트는 비워 두고(Arrival.lot == '') 재고 서비스가 날짜·재종별 순번으로 발번하게 한다.
    """

    def __init__(self, cfg: SimConfig, grades: list[str], weights: dict[str, float] | None = None):
        if not cfg.release_interval_s > 0:
            raise ValueError(f'출고 간격은 0보다 커야 합니다: {cfg.release_interval_s!r}')
        if not cfg.first_arrival_s >= 0:
            raise ValueError(f'첫 도착 시간은 0 이상이어야 합니다: {cfg.first_arrival_s!r}')
        if not 0.0 <= cfg.residual_prob <= 1.0:
            raise ValueError(f'잔량 확률은 0~1 사이여야 합니다: {cfg.residual_prob!r}')
        self._cfg = cfg
        self._rng = random.Random(cfg.seed)
        self._weights: dict[str, float] = {}
        for g, w in (weights or {}).items():
            wf = float(w)
            if not math.isfinite(wf) or wf < 0:
                raise ValueError(f'재종 가중치가 올바르지 않습니다: {g}={w!r}')
            self._weights[g] = wf
        self._pool: list[str] = []
        self._pool_w: list[float] = []
        self._running = False
        self._remaining = 0
        self._timer = 0.0            # 다음 박스까지 남은 시간 (s). 라인 정지 중에는 0 에 머문다.
        self._line_stopped = False
        self.released = 0            # 지금까지 내보낸 박스 수 (전체)
        self.set_grades(grades)

    def start(self, boxes: int | None = None) -> None:
        """이번 출고 시작 (진행 중이면 처음부터 다시). boxes 없으면 cfg.session_boxes."""
        n = self._cfg.session_boxes if boxes is None else boxes
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(f'출고 박스 수는 1 이상의 정수여야 합니다: {n!r}')
        if not self._pool:
            raise ValueError('출고할 재종이 없습니다.')
        self._running = True
        self._remaining = n
        self._timer = float(self._cfg.first_arrival_s)
        self._line_stopped = False

    def stop(self) -> None:
        self._running = False
        self._remaining = 0
        self._timer = 0.0
        self._line_stopped = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def remaining(self) -> int:
        """이번 출고에서 남은 박스 수."""
        return self._remaining

    @property
    def line_stopped(self) -> bool:
        """컨베이어 만재로 대기 중."""
        return self._line_stopped

    @property
    def next_in(self) -> float | None:
        """다음 박스까지 남은 시간 (s). 멈춰 있으면 None."""
        return self._timer if self._running else None

    def set_grades(self, grades: list[str]) -> None:
        """출고 재종 목록 교체 (순서 유지, 중복 제거). 가중치는 생성 때 준 값, 없으면 1."""
        pool: list[str] = []
        for g in grades:
            if g and g not in pool:
                pool.append(g)
        weights = [self._weights.get(g, 1.0) for g in pool]
        if pool and sum(weights) <= 0:
            raise ValueError('출고 재종의 가중치 합이 0 입니다.')
        self._pool, self._pool_w = pool, weights

    def tick(self, dt: float, can_accept) -> list[Arrival]:
        """dt 초 진행. 이번에 도착한 박스들. can_accept: () -> bool, None 이면 항상 받음."""
        out: list[Arrival] = []
        budget = max(0.0, float(dt))
        while self._running:
            if self._timer > budget + _EPS:
                self._timer -= budget
                break
            budget = max(0.0, budget - self._timer)
            self._timer = 0.0                                  # 출고 시각 도달
            if not self._pool:
                self._line_stopped = False                     # 재종 없음 → 대기 (만재 라인 정지와 구분)
                break
            if can_accept is not None and not can_accept():
                self._line_stopped = True                      # 컨베이어 만재 → 라인 정지, 남은 시간은 버린다
                break
            self._line_stopped = False
            out.append(self._make_arrival())
            self._remaining -= 1
            if self._remaining <= 0:
                self._running = False
                break
            self._timer = float(self._cfg.release_interval_s)
        return out

    # ---- 내부
    def _pick_grade(self) -> str:
        total = sum(self._pool_w)
        r = self._rng.random() * total
        acc = 0.0
        for g, w in zip(self._pool, self._pool_w):
            acc += w
            if r < acc:
                return g
        # 부동소수 경계(r ≈ total): 가중치가 0 이 아닌 마지막 재종
        return next(g for g, w in zip(reversed(self._pool), reversed(self._pool_w)) if w > 0)

    def _make_arrival(self) -> Arrival:
        grade = self._pick_grade()
        if self._rng.random() < self._cfg.residual_prob:
            weight = self._rng.randint(100, 149) / 10.0       # 10.0 ~ 14.9 kg, 0.1 단위 균등
        else:
            weight = FULL_WEIGHT_KG
        self.released += 1
        return Arrival(grade=grade, lot='', weight_kg=weight)
