"""앱 설정 (config.json). 없는 값은 기본값."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class RackConfig:
    columns: int = 10
    tiers: int = 4
    col_pitch_m: float = 0.38
    tier_base_m: float = 0.40
    tier_pitch_m: float = 0.30
    depth_m: float = 1.04            # 양면 합계 깊이
    aisle_m: float = 1.30            # 접근 통로 폭


@dataclass
class SimConfig:
    amr_count: int = 1
    trays: int = 3                   # 사다리 단수
    tray_base_m: float = 0.45
    tray_pitch_m: float = 0.35
    speed_mps: float = 1.0
    lift_mps: float = 0.6
    fork_time_s: float = 10.0
    conveyor_height_m: float = 0.85
    battery_per_m: float = 0.004     # % / m
    battery_per_s: float = 0.0012    # % / s (대기 소모)
    charge_per_s: float = 0.013      # % / s
    first_arrival_s: float = 90.0
    release_interval_s: float = 10.0
    session_boxes: int = 20          # 오버브릿지 자동 출고 1회 박스 수
    residual_prob: float = 1 / 16
    seed: int = 1


@dataclass
class RobotConfig:
    id: str = 'AMR-01'
    driver: str = 'sim'              # sim / vda5050 / tcp_json
    host: str = ''
    port: int = 0
    manufacturer: str = ''           # VDA 5050
    serial: str = ''                 # VDA 5050
    interface: str = 'uagv'          # VDA 5050 topic prefix
    start_node: str = 'PARK1'


@dataclass
class AppConfig:
    rack: RackConfig = field(default_factory=RackConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    robots: list[RobotConfig] = field(default_factory=list)   # 비어 있으면 sim.amr_count 대 가상 AMR
    conveyor_capacity: int = 5
    allocation_policy: str = 'grade_zone'                     # grade_zone / lowest_first / balanced
    auto_dispatch: bool = True
    charge_below: float = 25.0
    charge_target: float = 90.0
    operator: str = ''

    @staticmethod
    def load(path: Path) -> 'AppConfig':
        cfg = AppConfig()
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8'))
            for key in ('conveyor_capacity', 'allocation_policy', 'auto_dispatch', 'charge_below', 'charge_target', 'operator'):
                if key in raw:
                    setattr(cfg, key, raw[key])
            for key, val in raw.get('rack', {}).items():
                if hasattr(cfg.rack, key):
                    setattr(cfg.rack, key, val)
            for key, val in raw.get('sim', {}).items():
                if hasattr(cfg.sim, key):
                    setattr(cfg.sim, key, val)
            cfg.robots = [RobotConfig(**{k: v for k, v in r.items() if k in RobotConfig.__dataclass_fields__}) for r in raw.get('robots', [])]
        return cfg

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding='utf-8')

    def robot_list(self) -> list[RobotConfig]:
        """실제 사용할 AMR 목록. robots 가 비어 있으면 가상 AMR sim.amr_count 대."""
        if self.robots:
            return self.robots
        return [RobotConfig(id=f'AMR-{i + 1:02d}', driver='sim', start_node=f'PARK{i + 1}') for i in range(self.sim.amr_count)]
