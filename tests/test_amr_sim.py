import math

import pytest

from amr.models import RobotMode, Step
from amr.sim_driver import SimulatedAmrDriver
from amr.topology import build_cell_topology
from core.config import RackConfig, SimConfig


def make(cfg: SimConfig | None = None, start: str = 'PARK1', trays: int | None = None):
    topo = build_cell_topology(RackConfig(), parks=2)
    drv = SimulatedAmrDriver('AMR-01', topo, cfg or SimConfig(), start, trays=trays)
    return topo, drv


class Clock:
    """now 를 누적하지 않고 곱셈으로 계산 (dt 를 바꿔도 같은 시각)."""

    def __init__(self, drv):
        self.drv = drv
        self.now = 0.0

    def tick(self, dt: float) -> None:
        self.now += dt
        self.drv.tick(self.now, dt)

    def run(self, total: float, dt: float) -> None:
        start = self.now
        n = int(round(total / dt))
        for k in range(1, n + 1):
            now = start + k * dt
            self.drv.tick(now, dt)
        self.now = start + n * dt


def events(drv):
    return [(e.kind, e.step_id, e.detail) for e in drv.poll()]


def tray_h(cfg: SimConfig, tray: int) -> float:
    return cfg.tray_base_m + tray * cfg.tray_pitch_m


def test_initial_status():
    topo, drv = make(start='PARK2')
    st = drv.status()
    assert st.robot_id == 'AMR-01'
    assert st.mode == RobotMode.IDLE
    assert st.node == 'PARK2'
    assert (st.x, st.y) == (topo.nodes['PARK2'].x, topo.nodes['PARK2'].y)
    assert st.trays == [None, None, None]
    assert st.battery == 100.0
    assert st.connected
    assert not drv.busy
    _, drv4 = make(trays=4)
    assert drv4.status().trays == [None] * 4
    with pytest.raises(ValueError):
        SimulatedAmrDriver('X', topo, SimConfig(), 'NOWHERE')


def test_move_time_equals_length_over_speed():
    cfg = SimConfig(speed_mps=0.8)
    topo, drv = make(cfg)
    path = topo.shortest_path('PARK1', 'S05')
    length = topo.path_length(path)
    t_move = length / cfg.speed_mps
    clock = Clock(drv)
    drv.send_step(Step('O1-S1', 'S05', path=path))
    assert events(drv) == [('step_started', 'O1-S1', '')]
    assert drv.status().mode == RobotMode.MOVING
    clock.tick(t_move - 1e-6)
    assert drv.busy and events(drv) == []
    clock.tick(2e-6)
    assert events(drv) == [('step_done', 'O1-S1', '')]
    st = drv.status()
    assert st.node == 'S05' and st.mode == RobotMode.IDLE
    assert st.x == pytest.approx(topo.nodes['S05'].x) and st.y == pytest.approx(topo.nodes['S05'].y)


def test_move_exact_single_tick_and_many_nodes():
    topo, drv = make()
    path = topo.shortest_path('PARK1', 'DOCK')
    assert len(path) > 5
    t_move = topo.path_length(path) / SimConfig().speed_mps
    drv.send_step(Step('O1-S1', 'DOCK', path=path))
    drv.tick(t_move, t_move)            # 한 번의 tick 으로 여러 노드 통과
    assert events(drv)[-1] == ('step_done', 'O1-S1', '')
    assert drv.status().node == 'DOCK'


def test_empty_path_uses_shortest_path():
    topo, drv = make()
    drv.send_step(Step('O1-S1', 'N03'))
    drv.tick(100.0, 100.0)
    assert events(drv)[-1][0] == 'step_done'
    assert drv.status().node == 'N03'


def test_interpolated_position_and_heading():
    topo, drv = make(start='N01')
    drv.send_step(Step('O1-S1', 'N03', path=['N01', 'N02', 'N03']))
    pitch = RackConfig().col_pitch_m
    drv.tick(pitch * 1.5, pitch * 1.5)  # 1.5 구간
    st = drv.status()
    assert st.x == pytest.approx(topo.nodes['N01'].x + 1.5 * pitch)
    assert st.y == pytest.approx(topo.nodes['N01'].y)
    assert st.node == 'N02'
    assert st.heading == pytest.approx(0.0)       # +x 방향
    # 반대 방향
    _, drv2 = make(start='S05')
    drv2.send_step(Step('O1-S1', 'S03', path=['S05', 'S04', 'S03']))
    drv2.tick(0.1, 0.1)
    assert abs(drv2.status().heading) == pytest.approx(math.pi)


def test_pick_and_drop_time_formula():
    cfg = SimConfig()
    topo, drv = make(cfg, start='DOCK')
    clock = Clock(drv)
    # pick: 포크 0 → 컨베이어 0.85 → 트레이 1 (0.80)
    h = cfg.conveyor_height_m
    t_pick = (abs(0.0 - h) + abs(h - tray_h(cfg, 1))) / cfg.lift_mps + cfg.fork_time_s
    drv.send_step(Step('O1-S1', 'DOCK', action='pick', height_m=h, tray=1, ref='BOX-1', path=['DOCK']))
    events(drv)
    clock.tick(t_pick - 1e-6)
    assert drv.busy and drv.status().mode == RobotMode.LOADING
    clock.tick(2e-6)
    assert events(drv) == [('step_done', 'O1-S1', '')]
    st = drv.status()
    assert st.trays == [None, 'BOX-1', None]
    assert st.fork_height == pytest.approx(tray_h(cfg, 1))
    # drop: 포크 0.80 → 칸 높이 1.30 → 트레이 0.80
    slot_h = cfg.tray_base_m + 0.85
    t_drop = (abs(tray_h(cfg, 1) - slot_h) + abs(slot_h - tray_h(cfg, 1))) / cfg.lift_mps + cfg.fork_time_s
    drv.send_step(Step('O1-S2', 'DOCK', action='drop', height_m=slot_h, tray=1, ref='N-01-1', path=['DOCK']))
    events(drv)
    clock.tick(t_drop - 1e-6)
    assert drv.busy and drv.status().mode == RobotMode.UNLOADING
    clock.tick(2e-6)
    assert events(drv) == [('step_done', 'O1-S2', '')]
    assert drv.status().trays == [None, None, None]


def test_move_then_pick_total_time_and_fork_profile():
    cfg = SimConfig()
    topo, drv = make(cfg, start='PARK1')
    path = topo.shortest_path('PARK1', 'DOCK')
    t_move = topo.path_length(path) / cfg.speed_mps
    t1 = cfg.conveyor_height_m / cfg.lift_mps
    t3 = abs(cfg.conveyor_height_m - tray_h(cfg, 0)) / cfg.lift_mps
    total = t_move + t1 + cfg.fork_time_s + t3
    drv.send_step(Step('O1-S1', 'DOCK', action='pick', height_m=cfg.conveyor_height_m, tray=0, ref='B', path=path))
    clock = Clock(drv)
    clock.tick(t_move + t1 / 2)                       # 포크 올라가는 중
    assert drv.status().fork_height == pytest.approx(cfg.conveyor_height_m / 2)
    clock.tick(t1 / 2 + cfg.fork_time_s / 2)          # 포크 동작 중 (대상 높이)
    assert drv.status().fork_height == pytest.approx(cfg.conveyor_height_m)
    clock.tick(total - clock.now - 1e-6)
    assert drv.busy
    clock.tick(2e-6)
    assert events(drv)[-1] == ('step_done', 'O1-S1', '')


def _scenario(dt: float):
    """이동 + 집기 1단계, 그 뒤 대기 — 73 s 진행 후 상태."""
    cfg = SimConfig(battery_per_s=0.01, battery_per_m=0.05)
    topo, drv = make(cfg, start='PARK2')
    path = topo.shortest_path('PARK2', 'DOCK')
    drv.send_step(Step('O1-S1', 'DOCK', action='pick', height_m=0.85, tray=2, ref='BOX', path=path))
    clock = Clock(drv)
    clock.run(73.0, dt)
    return topo, cfg, drv.status(), [e for e in events(drv)], path


def test_tick_size_independence():
    results = [_scenario(dt) for dt in (0.05, 0.1, 7.3)]
    topo, cfg, ref, ref_events, path = results[0]
    assert ref_events == [('step_started', 'O1-S1', ''), ('step_done', 'O1-S1', '')]
    length = topo.path_length(path)
    expected_battery = 100.0 - cfg.battery_per_s * 73.0 - cfg.battery_per_m * length
    assert ref.battery == pytest.approx(expected_battery, abs=1e-6)
    for _, _, st, evs, _ in results[1:]:
        assert evs == ref_events
        assert st.node == ref.node == 'DOCK'
        assert st.mode == ref.mode == RobotMode.IDLE
        assert st.trays == ref.trays == [None, None, 'BOX']
        for attr in ('x', 'y', 'battery', 'fork_height', 'heading'):
            assert getattr(st, attr) == pytest.approx(getattr(ref, attr), abs=1e-6), attr


def test_tick_size_independence_mid_step():
    """단계 도중(이동·동작 중간)에 멈춰 본 상태도 dt 와 무관."""
    states = []
    for dt in (0.05, 0.1, 7.3):
        topo, drv = make(SimConfig(battery_per_m=0.02), start='PARK1')
        drv._trays[0] = 'BOX'   # 실린 박스 준비 (시험용)
        drv.send_step(Step('O1-S1', 'N01', action='drop', height_m=1.0, tray=0, ref='N-01-3'))
        Clock(drv).run(14.6, dt)
        states.append(drv.status())
    for st in states[1:]:
        for attr in ('x', 'y', 'battery', 'fork_height'):
            assert getattr(st, attr) == pytest.approx(getattr(states[0], attr), abs=1e-6), attr
        assert st.mode == states[0].mode


def test_idle_battery_drain_and_battery_zero_idle():
    cfg = SimConfig(battery_per_s=1.0)
    _, drv = make(cfg)
    drv.tick(30.0, 30.0)
    assert drv.status().battery == pytest.approx(70.0)
    drv.tick(200.0, 170.0)
    st = drv.status()
    assert st.battery == 0.0 and st.mode == RobotMode.ERROR
    assert events(drv) == []      # 단계가 없으면 실패 이벤트 없음


def test_battery_zero_during_move_fails_step():
    cfg = SimConfig(battery_per_s=0.0, battery_per_m=25.0)   # 4 m 가면 방전
    topo, drv = make(cfg, start='JNW')
    path = topo.shortest_path('JNW', 'JNE')
    assert topo.path_length(path) > 4.0
    drv.send_step(Step('O1-S1', 'JNE', path=path))
    events(drv)
    drv.tick(60.0, 60.0)
    assert events(drv) == [('step_failed', 'O1-S1', 'battery')]
    st = drv.status()
    assert st.mode == RobotMode.ERROR and st.battery == 0.0
    assert st.x == pytest.approx(topo.nodes['JNW'].x + 4.0)   # 4 m 지점에서 멈춤
    assert not drv.busy
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O1-S2', 'JNE'))
    drv.resume()          # 오류 해제는 되지만 배터리가 0 이라 다시 ERROR
    drv.tick(61.0, 1.0)
    assert drv.status().mode == RobotMode.ERROR


def test_move_battery_drain():
    cfg = SimConfig(battery_per_s=0.01, battery_per_m=0.1)
    topo, drv = make(cfg)
    path = topo.shortest_path('PARK1', 'S10')
    length = topo.path_length(path)
    drv.send_step(Step('O1-S1', 'S10', path=path))
    t = length / cfg.speed_mps
    drv.tick(t, t)
    assert drv.status().battery == pytest.approx(100 - 0.1 * length - 0.01 * t)


def test_tray_full_and_empty_failures():
    _, drv = make(start='DOCK')
    drv.send_step(Step('O1-S1', 'DOCK', action='drop', height_m=0.4, tray=0, ref='N-01-1'))
    evs = events(drv)
    assert evs[0] == ('step_started', 'O1-S1', '')
    assert evs[1][0] == 'step_failed' and '비어' in evs[1][2]
    assert not drv.busy
    drv.send_step(Step('O1-S2', 'DOCK', action='pick', height_m=0.85, tray=0, ref='A'))
    drv.tick(100.0, 100.0)
    assert events(drv)[-1] == ('step_done', 'O1-S2', '')
    drv.send_step(Step('O1-S3', 'DOCK', action='pick', height_m=0.85, tray=0, ref='B'))
    evs = events(drv)
    assert evs[-1][0] == 'step_failed' and '이미' in evs[-1][2]
    drv.send_step(Step('O1-S4', 'DOCK', action='pick', height_m=0.85, tray=7, ref='B'))
    assert events(drv)[-1][0] == 'step_failed'
    assert drv.status().trays == ['A', None, None]
    # tray 생략: pick 은 빈 단 중 가장 아래, drop 은 meta box 가 실린 단
    drv.send_step(Step('O1-S5', 'DOCK', action='pick', height_m=0.85, ref='C'))
    drv.tick(200.0, 100.0)
    assert drv.status().trays == ['A', 'C', None]
    drv.send_step(Step('O1-S6', 'DOCK', action='drop', height_m=0.4, ref='S-01-1', meta={'box': 'C'}))
    drv.tick(300.0, 100.0)
    assert drv.status().trays == ['A', None, None]


def test_send_while_busy_and_bad_step():
    _, drv = make()
    drv.send_step(Step('O1-S1', 'N05'))
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O1-S2', 'N06'))
    drv.cancel()
    with pytest.raises(ValueError):
        drv.send_step(Step('O1-S3', 'NOWHERE'))
    with pytest.raises(ValueError):
        drv.send_step(Step('O1-S4', 'N01', action='jump'))
    assert not drv.busy


def test_pause_freezes_motion_and_action():
    cfg = SimConfig(battery_per_s=0.0)
    topo, drv = make(cfg, start='N01')
    path = ['N01', 'N02', 'N03', 'N04']
    t_move = topo.path_length(path) / cfg.speed_mps
    t_pick = (1.0 + abs(1.0 - tray_h(cfg, 0))) / cfg.lift_mps + cfg.fork_time_s
    drv.send_step(Step('O1-S1', 'N04', action='pick', height_m=1.0, tray=0, ref='B', path=path))
    clock = Clock(drv)
    clock.tick(0.5)
    drv.pause()
    x = drv.status().x
    assert drv.status().mode == RobotMode.PAUSED
    clock.tick(30.0)
    assert drv.status().x == x and drv.busy
    drv.resume()
    clock.tick(t_move - 0.5 + 2.0)   # 도착 + 동작 2 s
    drv.pause()
    fork = drv.status().fork_height
    clock.tick(50.0)
    assert drv.status().fork_height == fork and drv.busy
    drv.resume()
    clock.tick(t_pick - 2.0 - 1e-6)
    assert drv.busy
    clock.tick(2e-6)
    assert events(drv)[-1] == ('step_done', 'O1-S1', '')


def test_cancel_mid_edge_and_continue():
    cfg = SimConfig()
    topo, drv = make(cfg, start='N01')
    pitch = RackConfig().col_pitch_m
    drv.send_step(Step('O1-S1', 'N05', path=['N01', 'N02', 'N03', 'N04', 'N05']))
    drv.tick(1.5 * pitch, 1.5 * pitch)
    drv.cancel()
    assert events(drv)[-1] == ('step_failed', 'O1-S1', 'cancelled')
    st = drv.status()
    assert not drv.busy and st.mode == RobotMode.IDLE and st.node == 'N02'
    # 같은 방향으로 다시: N02 로 되돌아가지 않고 남은 0.5 구간 + 2 구간
    drv.send_step(Step('O1-S2', 'N05', path=topo.shortest_path(st.node, 'N05')))
    t = 2.5 * pitch / cfg.speed_mps
    drv.tick(1.5 * pitch + t - 1e-6, t - 1e-6)
    assert drv.busy
    drv.tick(1.5 * pitch + t + 1e-6, 2e-6)
    assert events(drv)[-1] == ('step_done', 'O1-S2', '')
    drv.cancel()   # 쉬는 중 cancel 은 아무 일 없음
    assert events(drv) == []


def test_estop_and_resume():
    _, drv = make()
    drv.send_step(Step('O1-S1', 'N05'))
    events(drv)
    drv.estop()
    assert events(drv) == [('step_failed', 'O1-S1', 'estop')]
    assert drv.status().mode == RobotMode.ERROR
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O1-S2', 'N05'))
    drv.estop()   # 쉬는 중 비상정지: 이벤트 없음, 모드 ERROR
    assert events(drv) == [] and drv.status().mode == RobotMode.ERROR
    drv.resume()
    assert drv.status().mode == RobotMode.IDLE
    drv.send_step(Step('O1-S3', 'N05'))
    drv.tick(100.0, 100.0)
    assert events(drv)[-1] == ('step_done', 'O1-S3', '')


def test_inject_failure_current_and_next():
    _, drv = make()
    drv.send_step(Step('O1-S1', 'N05'))
    drv.tick(1.0, 1.0)
    drv.inject_failure('포크 센서 오류')
    assert events(drv)[-1] == ('step_failed', 'O1-S1', '포크 센서 오류')
    assert not drv.busy and drv.status().mode == RobotMode.IDLE
    drv.inject_failure('바퀴 미끄럼')    # 쉬는 중: 다음 단계가 시작하자마자 실패
    drv.send_step(Step('O1-S2', 'N05'))
    assert events(drv) == [('step_started', 'O1-S2', ''), ('step_failed', 'O1-S2', '바퀴 미끄럼')]
    drv.send_step(Step('O1-S3', 'N05'))   # 한 번만 적용
    drv.tick(100.0, 99.0)
    assert events(drv)[-1] == ('step_done', 'O1-S3', '')


def test_charge_step():
    cfg = SimConfig(charge_per_s=0.5, battery_per_s=0.01)
    topo, drv = make(cfg, start='CHARGER')
    drv._battery = 40.0   # 시험용으로 배터리 낮춤
    drv.send_step(Step('O1-S1', 'CHARGER', action='charge', meta={'target': 80}))
    events(drv)
    clock = Clock(drv)
    clock.tick(40.0 / 0.5 - 1e-6)
    assert drv.busy and drv.status().mode == RobotMode.CHARGING
    clock.tick(2e-6)
    assert events(drv) == [('step_done', 'O1-S1', '')]
    assert drv.status().battery == pytest.approx(80.0)
    # 이미 목표 이상이면 바로 끝
    drv.send_step(Step('O1-S2', 'CHARGER', action='charge', meta={'target': 50}))
    clock.tick(0.0)
    assert events(drv)[-1] == ('step_done', 'O1-S2', '')
    # 충전기가 아닌 곳에서 충전 → 실패
    drv.send_step(Step('O1-S3', 'N01', action='charge'))
    assert events(drv)[-1][0] == 'step_failed'


def test_status_is_a_copy_and_duplicate_tick_ignored():
    _, drv = make()
    st = drv.status()
    st.trays[0] = 'HACK'
    st.battery = 1.0
    assert drv.status().trays == [None, None, None] and drv.status().battery == 100.0
    drv.send_step(Step('O1-S1', 'N05'))
    drv.tick(1.0, 1.0)
    x = drv.status().x
    drv.tick(1.0, 1.0)            # 같은 now 로 한 번 더 → 무시
    assert drv.status().x == x
    assert drv.status().updated_at == 1.0


def test_disconnect_connect():
    _, drv = make()
    drv.send_step(Step('O1-S1', 'N05'))
    events(drv)
    drv.disconnect()
    assert events(drv) == [('step_failed', 'O1-S1', '연결 끊김'), ('disconnected', None, '')]
    st = drv.status()
    assert not st.connected and st.mode == RobotMode.OFFLINE
    with pytest.raises(RuntimeError):
        drv.send_step(Step('O1-S2', 'N05'))
    drv.connect()
    assert events(drv) == [('connected', None, '')]
    assert drv.status().mode == RobotMode.IDLE
