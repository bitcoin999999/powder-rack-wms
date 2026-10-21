"""core.conveyor.OverbridgeSimulator — 첫 도착 90 s, 이후 10 s 간격, 라인 정지, 잔량 1/16, 결정성."""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.config import SimConfig
from core.conveyor import Arrival, OverbridgeSimulator

GRADES = ['PC9030', 'NC3215', 'NC3020', 'H01']


def always() -> bool:
    return True


def never() -> bool:
    return False


def make(cfg: SimConfig | None = None, grades=None, weights=None) -> OverbridgeSimulator:
    return OverbridgeSimulator(cfg or SimConfig(), list(GRADES if grades is None else grades), weights)


def run_small_steps(sim: OverbridgeSimulator, total: float, step: float, accept=always) -> list[tuple[float, Arrival]]:
    """작은 dt 로 진행하며 (도착 시각, 박스) 기록."""
    out, t = [], 0.0
    for _ in range(round(total / step)):
        t += step
        for a in sim.tick(step, accept):
            out.append((t, a))
    return out


def test_idle_until_started():
    sim = make()
    assert not sim.running and sim.remaining == 0 and not sim.line_stopped
    assert sim.tick(1000, always) == []


def test_first_box_exactly_at_first_arrival():
    sim = make()
    sim.start()
    assert sim.running and sim.remaining == 20
    assert sim.tick(89.9, always) == []
    got = sim.tick(0.1, always)
    assert len(got) == 1 and sim.remaining == 19

    sim2 = make()
    sim2.start()
    assert len(sim2.tick(90.0, always)) == 1               # 정확히 90 s 에 1개


def test_then_one_box_every_release_interval():
    sim = make()
    sim.start()
    times = [t for t, _ in run_small_steps(sim, 300.0, 0.1)]
    assert times[0] == pytest.approx(90.0)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and all(g == pytest.approx(10.0) for g in gaps)


def test_boxes_per_session_and_auto_stop():
    sim = make()
    sim.start()
    got = sim.tick(10_000, always)
    assert len(got) == SimConfig().session_boxes == 20
    assert not sim.running and sim.remaining == 0
    assert sim.tick(10_000, always) == []
    sim.start(boxes=3)
    assert len(sim.tick(10_000, always)) == 3


def test_session_count_with_small_ticks():
    cfg = replace(SimConfig(), session_boxes=7)
    sim = make(cfg)
    sim.start()
    got = run_small_steps(sim, 90 + 10 * 10, 0.2)
    assert len(got) == 7
    assert got[-1][0] == pytest.approx(150.0)


def test_line_stop_pauses_the_timer():
    sim = make()
    sim.start()
    assert sim.tick(90, never) == []
    assert sim.line_stopped and sim.remaining == 20
    assert sim.tick(500, never) == []                       # 기다리는 동안 박스도, 시간 누적도 없다
    assert sim.line_stopped
    first = sim.tick(0.1, always)                            # 컨베이어가 비면 그 tick 시작 시점에 바로 1개
    assert len(first) == 1 and not sim.line_stopped
    assert sim.tick(9.8, always) == []                       # 다음은 그 시점부터 10 s 뒤 (0.1 + 9.8 + 0.1)
    assert len(sim.tick(0.1, always)) == 1


def test_line_stop_gating_with_a_real_capacity():
    """컨베이어 용량 3, 아무도 안 가져가면 3개에서 멈춘다. 하나 치우면 즉시 1개 더."""
    belt: list[Arrival] = []
    accept = lambda: len(belt) < 3  # noqa: E731
    sim = make()
    sim.start()
    for _ in range(5000):
        belt.extend(sim.tick(0.2, accept))
    assert len(belt) == 3 and sim.line_stopped and sim.remaining == 17
    belt.pop(0)
    belt.extend(sim.tick(0.2, accept))
    assert len(belt) == 3 and not sim.line_stopped and sim.remaining == 16   # 다음 출고 시각 전이라 아직 정지 아님
    for _ in range(50):                                                     # 10 s 뒤 다시 만재 → 라인 정지
        belt.extend(sim.tick(0.2, accept))
    assert len(belt) == 3 and sim.line_stopped and sim.remaining == 16


def test_can_accept_none_means_always():
    sim = make()
    sim.start(boxes=2)
    assert len(sim.tick(100, None)) == 2


def test_can_accept_not_called_before_due():
    calls = []

    def acc() -> bool:
        calls.append(1)
        return True

    sim = make()
    sim.start()
    sim.tick(89.0, acc)
    assert calls == []
    sim.tick(1.0, acc)
    assert calls == [1]


def test_large_dt_equals_many_small_dt():
    a, b = make(), make()
    a.start()
    b.start()
    big = a.tick(400.0, always)
    small = [x for _, x in run_small_steps(b, 400.0, 0.1)]
    assert len(big) == len(small) == 20
    assert big == small
    assert a.remaining == b.remaining and a.running == b.running


def test_large_dt_equals_small_dt_with_mid_session_line_stop():
    def drive(steps: list[tuple[float, bool]]) -> list[Arrival]:
        sim = make()
        sim.start()
        out = []
        for dt, ok in steps:
            out += sim.tick(dt, always if ok else never)
        return out

    coarse = drive([(125.0, True), (40.0, False), (200.0, True)])
    fine = drive([(0.5, True)] * 250 + [(0.5, False)] * 80 + [(0.5, True)] * 400)
    assert coarse == fine and len(coarse) == 20


def test_residual_weight_share_near_one_sixteenth():
    cfg = replace(SimConfig(), session_boxes=16_000)
    sim = make(cfg)
    sim.start()
    got = sim.tick(90 + 10 * 16_000, always)
    assert len(got) == 16_000
    residual = [a.weight_kg for a in got if a.weight_kg < 15.0]
    share = len(residual) / len(got)
    assert abs(share - 1 / 16) < 0.008                     # ±4σ 정도
    assert all(10.0 <= w <= 14.9 for w in residual)
    assert all(round(w, 1) == w for w in residual)          # 0.1 kg 단위
    assert {a.weight_kg for a in got if a.weight_kg >= 15.0} == {15.0}
    assert len(set(residual)) > 40                           # 10.0~14.9 범위가 고루 나온다


def test_residual_prob_extremes():
    sim = make(replace(SimConfig(), residual_prob=0.0))
    sim.start(boxes=200)
    assert all(a.weight_kg == 15.0 for a in sim.tick(1e6, always))
    sim = make(replace(SimConfig(), residual_prob=1.0))
    sim.start(boxes=200)
    assert all(10.0 <= a.weight_kg <= 14.9 for a in sim.tick(1e6, always))


def test_deterministic_with_seed():
    def run(seed: int) -> list[Arrival]:
        sim = make(replace(SimConfig(), seed=seed, session_boxes=300))
        sim.start()
        return sim.tick(1e6, always)

    assert run(1) == run(1)
    assert run(1) != run(2)


def test_grades_drawn_from_pool_and_weights():
    sim = make(replace(SimConfig(), session_boxes=2000))
    sim.start()
    got = sim.tick(1e6, always)
    assert {a.grade for a in got} == set(GRADES)            # 모든 재종이 나온다

    sim = make(replace(SimConfig(), session_boxes=4000), weights={'PC9030': 3.0, 'NC3215': 1.0, 'NC3020': 0, 'H01': 0})
    sim.start()
    got = sim.tick(1e6, always)
    counts = {g: sum(a.grade == g for a in got) for g in GRADES}
    assert counts['NC3020'] == counts['H01'] == 0
    assert 2.6 < counts['PC9030'] / counts['NC3215'] < 3.4


def test_set_grades_changes_the_pool():
    sim = make(replace(SimConfig(), session_boxes=100))
    sim.start()
    first = sim.tick(90 + 10 * 9, always)
    assert len(first) == 10
    sim.set_grades(['H01'])
    rest = sim.tick(1e6, always)
    assert len(rest) == 90 and {a.grade for a in rest} == {'H01'}


def test_empty_pool_waits_without_line_stop():
    sim = make()
    sim.start(boxes=3)
    sim.set_grades([])
    assert sim.tick(500, always) == [] and not sim.line_stopped and sim.remaining == 3
    sim.set_grades(['PC9030'])
    got = sim.tick(0.0, always)
    assert [a.grade for a in got] == ['PC9030']


def test_set_grades_dedupes_and_rejects_zero_weight_pool():
    sim = make(weights={'PC9030': 0.0})
    with pytest.raises(ValueError):
        sim.set_grades(['PC9030'])
    sim.set_grades(['NC3215', 'NC3215', 'H01'])
    sim.start(boxes=50)
    assert {a.grade for a in sim.tick(1e6, always)} <= {'NC3215', 'H01'}


def test_stop_and_restart():
    sim = make()
    sim.start()
    sim.tick(95, always)
    sim.stop()
    assert not sim.running and sim.remaining == 0 and not sim.line_stopped
    assert sim.tick(1000, always) == []
    sim.start(boxes=2)
    assert sim.tick(89.0, always) == []                     # 다시 시작하면 첫 도착 대기부터
    assert len(sim.tick(11.0, always)) == 2


def test_arrival_fields():
    sim = make()
    sim.start(boxes=1)
    (a,) = sim.tick(90, always)
    assert isinstance(a, Arrival) and a.grade in GRADES and a.lot == ''   # 로트는 재고 서비스가 발번
    assert a.weight_kg == 15.0 or 10.0 <= a.weight_kg <= 14.9


@pytest.mark.parametrize('kwargs', [{'release_interval_s': 0}, {'release_interval_s': -1}, {'first_arrival_s': -5},
                                    {'residual_prob': 1.5}])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        make(replace(SimConfig(), **kwargs))


def test_invalid_start():
    sim = make()
    for bad in (0, -3, 2.5, True):
        with pytest.raises(ValueError):
            sim.start(boxes=bad)
    with pytest.raises(ValueError):
        make(grades=[]).start()
    with pytest.raises(ValueError):
        make(weights={'PC9030': -1})
