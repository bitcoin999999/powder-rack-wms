"""AmrDriver.manual_unload — 확인 필요 박스를 사람이 정리할 때 트레이 표시 비우기 (SPEC §12)."""
from __future__ import annotations

import pytest

from amr.driver import AmrDriver
from amr.models import RobotStatus, Step
from amr.sim_driver import SimulatedAmrDriver
from amr.tcp_json import TcpJsonDriver
from amr.topology import build_cell_topology
from amr.vda5050 import Vda5050Driver
from core.config import RackConfig, RobotConfig, SimConfig


class _Minimal(AmrDriver):
    """추상 메서드만 채운 드라이버 (manual_unload 는 구현하지 않음)."""

    def connect(self): ...
    def disconnect(self): ...
    def send_step(self, step): ...
    def pause(self): ...
    def resume(self): ...
    def cancel(self): ...
    def estop(self): ...

    def status(self):
        return RobotStatus(self.robot_id)


def make(trays: int = 3):
    topo = build_cell_topology(RackConfig(), parks=2)
    return SimulatedAmrDriver('AMR-01', topo, SimConfig(), 'DOCK', trays=trays)


def run(drv, seconds: float, now: list[float], dt: float = 0.2) -> None:
    for _ in range(int(round(seconds / dt))):
        now[0] = round(now[0] + dt, 9)
        drv.tick(now[0], dt)


def pick(drv, now, box: str, tray: int) -> None:
    drv.send_step(Step(f'P-{box}', 'DOCK', action='pick', height_m=0.85, tray=tray, ref=box, path=['DOCK']))
    run(drv, 30.0, now)
    assert drv.status().trays[tray] == box


def test_manual_unload_is_not_abstract_and_defaults_to_false():
    assert 'manual_unload' not in AmrDriver.__abstractmethods__
    assert _Minimal('X').manual_unload('B1') is False


def test_real_drivers_inherit_default_false():
    topo = build_cell_topology(RackConfig(), parks=2)
    vda = Vda5050Driver(RobotConfig(id='V1', driver='vda5050'), topo, client_factory=lambda: None)
    tcp = TcpJsonDriver(RobotConfig(id='T1', driver='tcp_json'))
    assert vda.manual_unload('B1') is False
    assert tcp.manual_unload('B1') is False


def test_sim_unload_clears_only_that_tray():
    drv, now = make(), [0.0]
    pick(drv, now, 'B1', 0)
    pick(drv, now, 'B2', 2)
    assert drv.manual_unload('B1') is True
    assert drv.status().trays == [None, None, 'B2']
    assert drv.manual_unload('B1') is False          # 이미 비움
    assert drv.manual_unload('NOPE') is False
    assert drv.manual_unload('') is False
    assert drv.status().trays == [None, None, 'B2']


def test_sim_unload_ignores_case_and_spaces():
    drv, now = make(), [0.0]
    pick(drv, now, 'W261020-0001', 1)
    assert drv.manual_unload('  w261020-0001 ') is True
    assert drv.status().trays == [None, None, None]


def test_unloaded_tray_can_be_picked_into_again_and_drop_then_fails():
    drv, now = make(), [0.0]
    pick(drv, now, 'B1', 0)
    assert drv.manual_unload('B1')
    drv.poll()
    # 비운 트레이로 내려놓기 → 즉시 실패 (박스가 없음)
    drv.send_step(Step('D1', 'N01', action='drop', height_m=0.4, tray=0, ref='N-01-1'))
    kinds = [(e.kind, e.detail) for e in drv.poll()]
    assert kinds[-1][0] == 'step_failed' and '비어' in kinds[-1][1]
    pick(drv, now, 'B9', 0)                            # 같은 트레이에 다시 실을 수 있다


@pytest.mark.parametrize('phase_seconds', [0.4, 4.0])   # 0.4 s: 이동 중, 4 s: 포크 동작 중
def test_unload_refused_while_a_step_uses_that_tray(phase_seconds):
    drv, now = make(), [0.0]
    pick(drv, now, 'B1', 0)
    pick(drv, now, 'B2', 1)
    drv.poll()
    drv.send_step(Step('D1', 'S01', action='drop', height_m=0.4, tray=0, ref='S-01-1', meta={'box': 'B1'}))
    run(drv, phase_seconds, now)
    assert drv.busy
    assert drv.manual_unload('B1') is False            # 그 트레이로 작업 중 → 거부
    assert drv.status().trays[0] == 'B1'
    assert drv.manual_unload('B2') is True             # 다른 트레이는 정리 가능
    run(drv, 40.0, now)
    assert [e.kind for e in drv.poll()][-1] == 'step_done'
    assert drv.status().trays == [None, None, None]


def test_unload_allowed_after_failed_step_on_that_tray():
    drv, now = make(), [0.0]
    pick(drv, now, 'B1', 0)
    drv.send_step(Step('D1', 'N05', action='drop', height_m=0.4, tray=0, ref='N-05-1'))
    run(drv, 1.0, now)
    drv.inject_failure('시험')
    assert not drv.busy and drv.status().trays[0] == 'B1'   # 실패해도 박스는 실린 채
    assert drv.manual_unload('B1') is True
