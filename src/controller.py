"""재고(core) ↔ AMR(amr) 연결 + 시뮬레이션 시계. 화면(ui)은 이 클래스만 쓴다. 계약: 공개 시그니처 유지.

시간
- sim_now: 시뮬레이션 시각(s). tick(real_dt) 마다 real_dt × sim_speed 만큼 진행. 가상 AMR·오버브릿지는 이 시계로 움직인다.
  고배속에서도 정확하도록 한 번에 최대 0.2 s 씩 잘라서 진행한다 (sim_dt 가 크면 여러 번).
- 재고 기록 시각은 벽시계 (InventoryService 기본 clock).

입고 자동 배차 (auto_dispatch 켜짐, 매 tick)
- 컨베이어에 '작업에 묶이지 않은' 박스가 있고, 진행 중인 INBOUND 작업이 없거나(도크는 한 대씩) 그 작업이 이미 도크를 떠났고,
  쉬는 AMR 이 있으면: 그 AMR 트레이 수만큼 컨베이어 앞쪽 박스를 골라 슬롯 예약 → INBOUND 작업 생성:
  pick@DOCK(zone 'DOCK', height=conveyor_height, tray i, ref=박스, meta{box}) × n →
  drop@접근노드(height=칸 높이, tray i, ref=슬롯, meta{box, slot}) × n — drop 순서는 현재 노드에서 경로가 짧은 순(가까운 접근 노드부터).
  적재대가 가득(RackFull)이면 예약한 만큼만, 0개면 작업을 만들지 않는다.
- OrderEvent 처리: pick step_done → inventory.mark_picked, drop step_done → inventory.confirm_store,
  작업 failed/cancelled → 안 집은 박스 release_reservation, 실린 박스 flag_needs_check.
- 재고 쪽 예외는 화면 알림(notice)으로 보내고 계속 돈다.

구독: subscribe(cb) → cb(kind, payload). kind: 재고 이벤트 이름(ARRIVE/STORE/…), 'order'(payload {'order': id, 'event': kind}),
'notice'(payload {'text': 한국어 메시지, 'level': 'info'|'warn'|'error'}), 'tick'(payload {'sim_now': …}).

구현 메모 (위 규칙의 세부 — SPEC §5·§12, 1단계 보고서의 Controller 조언)
- 시작 순서: config.json 읽기(없으면 기본값) → InventoryService(이때 DB 의 열·단·정책이 같은 config 객체에 써진다.
  Controller 는 끝까지 이 객체 하나만 쓰고 apply_config 도 값만 바꿔 넣는다) → 셀 지도(주차 자리 ≥ AMR 대수) →
  드라이버(연결 실패는 알림만, 그 AMR 은 '연결 끊김') → FleetManager → OverbridgeSimulator → 재시작 정리 → 구독.
- 설정 파일이 깨졌거나 값이 틀리면 앱은 켜고(재고 조회가 먼저) 알림을 띄운다: 깨진 파일은 config.json.broken-… 로
  옮기고 기본값, 틀린 값은 그 항목만 기본값.
- 재시작 정리: 작업에 묶인 채 남은 CONVEYOR 박스는 예약 해제('재시작'), ON_AMR 박스는 확인 필요.
- 자동 배차 세부: 아직 집을 단계가 남은 INBOUND 작업이 있으면 새로 만들지 않는다(도크 집기는 한 작업씩).
  빈 트레이가 1개 이상인 쉬는 AMR 중 빈 트레이가 많은 → 도크에 가까운 AMR 을 고르고, 컨베이어 맨 앞부터 차례로
  (건너뛰지 않고 — AMR 은 실물 맨 앞 박스를 집는다) 빈 트레이 수만큼 박스를 고른다. 트레이는 빈 단 아래부터.
  배정한 AMR 이 작업을 받기 전에 다른 일(자동 충전 등)을 시작하면 그 입고 작업을 취소하고 다시 배차한다.
  적재대가 가득 차면 알림은 '가득 찬 동안 한 번'.
- 작업이 끝나면(done/failed/cancelled) 그 작업 meta['boxes'] 중 아직 이 작업에 묶인 박스를 정리한다:
  CONVEYOR → 예약 해제, ON_AMR → 확인 필요 (예약 유지). 화면의 [적재 완료 처리]/[컨베이어로 되돌림]
  (resolve_stored/resolve_return)은 그 AMR 트레이 표시도 비운다 (AmrDriver.manual_unload).
- 그 밖의 구독 종류: 'config'(payload {'rack_changed', 'fleet_rebuilt'} — 적재대·AMR 구성이 바뀜, 화면 다시 그리기).
  시작 중에 생긴 알림은 subscribe 할 때 그 구독자에게 다시 보낸다 (화면이 Controller 보다 늦게 만들어지므로).
- tick(real_dt) 의 real_dt 는 최대 5 s 로 자른다 (창이 오래 멈췄다 돌아와도 한꺼번에 몇 시간씩 돌지 않게).
- 화면용 재고 작업 (2단계): 화면은 ctrl.inventory 를 읽기만 하고, 바꾸는 일은 register_arrival / retrieve / move_box /
  put_directly / replace_slot_content / clear_slot / block_slot / unblock_slot / upsert_grade / set_grade_active /
  export_events_csv 로 한다 (재고 서비스로 그대로 전달, 사유가 필요한 것은 빈 사유 거부). recommend_slot 은 예약 없이
  배정 정책이 고를 칸만 알려 준다 (칸 고르기 대화상자의 추천).
"""
from __future__ import annotations

import copy
import csv
import logging
import math
import random
import shutil
import time
from collections import Counter, deque
from dataclasses import MISSING, asdict, fields, replace
from pathlib import Path
from typing import Callable

from amr.driver import AmrDriver
from amr.fleet import FleetManager
from amr.models import ORDER_LABEL, OrderEvent, OrderState, RobotMode, RobotStatus, Step, TransportOrder
from amr.sim_driver import SimulatedAmrDriver
from amr.topology import Topology, build_cell_topology
from core.config import AppConfig, RackConfig, RobotConfig, SimConfig
from core.conveyor import OverbridgeSimulator
from core.inventory import BOX_STATE_LABEL, ConveyorFull, InventoryError, InventoryService, RackFull, parse_scan
from core.models import Box, BoxState, Slot
from core.rack import MAX_COLUMNS, MAX_TIERS, POLICIES

log = logging.getLogger(__name__)

SLICE_S = 0.2               # 시뮬레이션 한 조각 (s)
TICK_PUBLISH_S = 0.25       # 'tick' 알림 최소 간격 (실제 시간, s)
SCAN_DEBOUNCE_S = 2.0       # 같은 스캔 무시 시간 (실제 시간, s)
MAX_REAL_DT = 5.0           # tick 한 번에 받는 실제 경과 시간 상한 (s)
MAX_SIM_SPEED = 1000.0
STUCK_ORDER_S = 1800.0      # 입고 작업이 이보다 오래 안 끝나면 자가 시험 위반 (시뮬레이션 s)
DRIVER_KINDS = ('sim', 'vda5050', 'tcp_json')
SCAN_DEBOUNCE_TEXT = '같은 스캔이 2초 안에 다시 들어와 무시했습니다'
SCAN_LOG_NAME = '스캔기록.csv'   # data 폴더. 현장 시험에서 바코드에 실제로 무엇이 들어 있는지 보려고 모든 스캔을 남긴다
_CTRL_NAMES = {'\t': '<TAB>', '\r': '<CR>', '\n': '<LF>', '\x1d': '<GS>', '\x1e': '<RS>', '\x04': '<EOT>'}


def scan_visible(text: str) -> str:
    """스캔 원문을 눈으로 볼 수 있게: 제어 문자는 <TAB> 처럼, 공백은 그대로."""
    out = []
    for ch in text:
        if ch in _CTRL_NAMES:
            out.append(_CTRL_NAMES[ch])
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f'<{ord(ch):02X}>')
        else:
            out.append(ch)
    return ''.join(out)


def _csv_cell(text: str) -> str:
    """엑셀이 수식으로 실행하지 않게 = + - @ 로 시작하면 앞에 ' 를 붙인다."""
    return "'" + text if text[:1] in ('=', '+', '-', '@') else text

_DOCK = 'DOCK'
_ZONES = ('DOCK', 'CHARGER')
_ACTIVE_BOX = (BoxState.CONVEYOR, BoxState.ON_AMR, BoxState.STORED)


class DockBusy(InventoryError):
    """도크에서 AMR 이 컨베이어 박스를 집는 중이라 컨베이어 맨 앞을 바꿀 수 없음 (집기가 끝난 뒤 다시)."""


# ====================================================================== 설정 검사
def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# (묶음, 항목, 이름, 정수?, 최솟값, 최솟값 미포함?, 최댓값)
_RULES: list[tuple[str, str, str, bool, float, bool, float | None]] = [
    ('rack', 'columns', '적재대 열 수', True, 1, False, MAX_COLUMNS),
    ('rack', 'tiers', '적재대 단 수', True, 1, False, MAX_TIERS),
    ('rack', 'col_pitch_m', '열 피치(m)', False, 0, True, None),
    ('rack', 'tier_base_m', '1단 높이(m)', False, 0, False, None),
    ('rack', 'tier_pitch_m', '단 간격(m)', False, 0, True, None),
    ('rack', 'depth_m', '적재대 깊이(m)', False, 0, True, None),
    ('rack', 'aisle_m', '통로 폭(m)', False, 0, True, None),
    ('sim', 'amr_count', '가상 AMR 대수', True, 1, False, 20),
    ('sim', 'trays', '트레이(사다리) 단수', True, 1, False, 9),
    ('sim', 'tray_base_m', '트레이 1단 높이(m)', False, 0, False, None),
    ('sim', 'tray_pitch_m', '트레이 간격(m)', False, 0, True, None),
    ('sim', 'speed_mps', '주행 속도(m/s)', False, 0, True, None),
    ('sim', 'lift_mps', '승강 속도(m/s)', False, 0, True, None),
    ('sim', 'fork_time_s', '포크 시간(s)', False, 0, False, None),
    ('sim', 'conveyor_height_m', '컨베이어 높이(m)', False, 0, False, None),
    ('sim', 'battery_per_m', '주행 배터리 소모(%/m)', False, 0, False, None),
    ('sim', 'battery_per_s', '대기 배터리 소모(%/s)', False, 0, False, None),
    ('sim', 'charge_per_s', '충전 속도(%/s)', False, 0, True, None),
    ('sim', 'first_arrival_s', '첫 도착 시간(s)', False, 0, False, None),
    ('sim', 'release_interval_s', '출고 간격(s)', False, 0, True, None),
    ('sim', 'session_boxes', '1회 출고 박스 수', True, 1, False, 100_000),
    ('sim', 'residual_prob', '잔량 확률', False, 0, False, 1),
    ('', 'conveyor_capacity', '컨베이어 적체 한도', True, 1, False, 100),
]


def _rule_text(integer: bool, lo: float, lo_open: bool, hi: float | None) -> str:
    kind = '정수' if integer else '수'
    if hi is not None:
        return f'{lo:g}~{hi:g} 사이 {kind}'
    return f'{lo:g}{"보다 큰" if lo_open else " 이상인"} {kind}'


def config_problems(cfg: AppConfig, strict_drivers: bool = False) -> list[tuple[str, str]]:
    """설정 값 검사. (항목 키, 한국어 설명) 목록 — 비어 있으면 정상. strict_drivers: 모르는 드라이버 종류도 오류로."""
    out: list[tuple[str, str]] = []
    for group, attr, label, integer, lo, lo_open, hi in _RULES:
        obj = getattr(cfg, group) if group else cfg
        v = getattr(obj, attr, None)
        ok = (_is_int(v) if integer else _is_num(v)) and (v > lo if lo_open else v >= lo) and (hi is None or v <= hi)
        if not ok:
            out.append((f'{group}.{attr}' if group else attr, f'{label} {v!r} — {_rule_text(integer, lo, lo_open, hi)}'))
    if not _is_int(cfg.sim.seed):
        out.append(('sim.seed', f'난수 시드 {cfg.sim.seed!r} — 정수'))
    if cfg.allocation_policy not in POLICIES:
        out.append(('allocation_policy', f'배정 정책 {cfg.allocation_policy!r} — {", ".join(POLICIES)} 중 하나'))
    if not isinstance(cfg.auto_dispatch, bool):
        out.append(('auto_dispatch', f'자동 배차 {cfg.auto_dispatch!r} — true/false'))
    if not isinstance(cfg.operator, str):
        out.append(('operator', f'작업자 {cfg.operator!r} — 글자'))
    below, target = cfg.charge_below, cfg.charge_target
    if not (_is_num(below) and _is_num(target) and 0 <= below < target <= 100):
        out.append(('charge', f'충전 기준 시작 {below!r}% · 목표 {target!r}% — 0 ≤ 시작 < 목표 ≤ 100'))
    if not isinstance(cfg.robots, list):
        out.append(('robots', 'AMR 목록(robots) 형식이 올바르지 않습니다'))
        return out
    seen: set[str] = set()
    for i, r in enumerate(cfg.robots):
        where = f'AMR 목록 {i + 1}번'
        if not isinstance(r, RobotConfig):
            out.append(('robots', f'{where} 형식이 올바르지 않습니다'))
            continue
        if not isinstance(r.id, str) or not r.id.strip():
            out.append(('robots', f'{where}: ID 가 비어 있습니다'))
            continue
        key = r.id.strip().upper()
        if key in seen:
            out.append(('robots', f'{where}: AMR ID 중복 ({r.id})'))
        seen.add(key)
        if not isinstance(r.driver, str) or (strict_drivers and r.driver.strip().lower() not in DRIVER_KINDS):
            out.append(('robots', f'{r.id}: 드라이버 {r.driver!r} — {"/".join(DRIVER_KINDS)} 중 하나'))
        if not _is_int(r.port) or not 0 <= r.port <= 65535:
            out.append(('robots', f'{r.id}: 포트 {r.port!r} — 0~65535 정수'))
        for attr in ('host', 'manufacturer', 'serial', 'interface', 'start_node'):
            if not isinstance(getattr(r, attr), str):
                out.append(('robots', f'{r.id}: {attr} {getattr(r, attr)!r} — 글자'))
    return out


def _default_of(cls, name: str):
    f = next(x for x in fields(cls) if x.name == name)
    return f.default_factory() if f.default is MISSING else f.default   # type: ignore[misc]


def _sanitize(cfg: AppConfig, problems: list[tuple[str, str]]) -> None:
    """틀린 항목만 기본값으로 (시작할 때 — 앱은 켜져야 재고를 볼 수 있다)."""
    for key, _ in problems:
        if key == 'charge':
            cfg.charge_below, cfg.charge_target = _default_of(AppConfig, 'charge_below'), _default_of(AppConfig, 'charge_target')
        elif key == 'robots':
            continue
        elif '.' in key:
            group, attr = key.split('.', 1)
            setattr(getattr(cfg, group), attr, _default_of(RackConfig if group == 'rack' else SimConfig, attr))
        else:
            setattr(cfg, key, _default_of(AppConfig, key))
    if any(key == 'robots' for key, _ in problems):
        # 틀린 칸만 기본값으로 고친다 (AMR 을 빼 버리면 robots 가 비어 가상 AMR 로 바뀌어 버린다). ID 중복은 뒤엣것을 뺀다.
        kept: list[RobotConfig] = []
        seen: set[str] = set()
        for i, r in enumerate(cfg.robots if isinstance(cfg.robots, list) else []):
            if not isinstance(r, RobotConfig):
                continue
            if not isinstance(r.id, str) or not r.id.strip():
                r.id = f'AMR-{i + 1:02d}'
            if r.id.strip().upper() in seen:
                continue
            if not isinstance(r.driver, str):
                r.driver = '?'
            if not _is_int(r.port) or not 0 <= r.port <= 65535:
                r.port = 0
            for attr in ('host', 'manufacturer', 'serial', 'interface', 'start_node'):
                if not isinstance(getattr(r, attr), str):
                    setattr(r, attr, _default_of(RobotConfig, attr))
            seen.add(r.id.strip().upper())
            kept.append(r)
        cfg.robots = kept


def _copy_config(src: AppConfig, dst: AppConfig) -> None:
    """src 의 값을 dst 객체에 그대로 써 넣는다 (dst·dst.rack·dst.sim 객체는 그대로 — 다른 곳이 같은 객체를 쥐고 있다)."""
    for f in fields(AppConfig):
        if f.name == 'rack':
            for rf in fields(RackConfig):
                setattr(dst.rack, rf.name, getattr(src.rack, rf.name))
        elif f.name == 'sim':
            for sf in fields(SimConfig):
                setattr(dst.sim, sf.name, getattr(src.sim, sf.name))
        else:
            setattr(dst, f.name, copy.deepcopy(getattr(src, f.name)))


def _fleet_signature(cfg: AppConfig) -> tuple:
    robots = tuple(tuple(sorted(asdict(r).items())) for r in cfg.robot_list())
    return robots, cfg.sim.trays


# ====================================================================== 만들 수 없는 AMR 자리
class _UnavailableDriver(AmrDriver):
    """설정 오류로 드라이버를 만들 수 없는 AMR — 화면에는 '연결 끊김'으로 보이고 작업은 받지 않는다."""

    def __init__(self, cfg: RobotConfig, reason: str):
        super().__init__(cfg.id)
        self.kind = str(cfg.driver or '?')
        self.reason = reason
        self._status = RobotStatus(cfg.id, mode=RobotMode.OFFLINE, node='', connected=False, message=reason)

    def connect(self) -> None:
        raise RuntimeError(self.reason)

    def disconnect(self) -> None:
        pass

    def send_step(self, step: Step) -> None:
        raise RuntimeError(f'{self.robot_id}: {self.reason}')

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def cancel(self) -> None:
        pass

    def estop(self) -> None:
        pass

    def status(self) -> RobotStatus:
        return replace(self._status, trays=list(self._status.trays))


# ====================================================================== Controller
class Controller:
    def __init__(self, base_dir: Path, config: AppConfig | None = None, db_name: str = 'rack.db'):
        """base_dir/data/config.json 을 읽고(없으면 기본값 저장), base_dir/data/{db_name} 재고 DB, 셀 지도, 드라이버(설정의 robots,
        비어 있으면 가상 AMR), FleetManager, OverbridgeSimulator 를 만든다. 시작 시 이전에 끝나지 않은 박스(ON_AMR)는 needs_check."""
        self.config: AppConfig
        self.inventory: InventoryService
        self.fleet: FleetManager
        self.topology: Topology
        self.overbridge: OverbridgeSimulator
        self.sim_now: float = 0.0
        self.sim_speed: float = 1.0
        self.auto_dispatch: bool = True

        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / 'data'
        self.config_path = self.data_dir / 'config.json'
        self.notice_log: deque[dict] = deque(maxlen=300)   # 최근 알림 (화면 기록용)
        self._subs: list[Callable[[str, dict], None]] = []
        self._startup_notices: list[dict] = []
        self._starting = True
        self._closed = False
        self._in_tick = False
        self._monotonic: Callable[[], float] = time.monotonic   # 시험에서 바꿔 끼운다
        self._last_tick_pub: float | None = None
        self._last_scan: tuple[str, float] | None = None
        self._drivers: list[AmrDriver] = []
        self._fleet_sig: tuple = ()
        self._inv_version = 0               # 재고 알림마다 +1 (배차 헛걸음 줄이기)
        self._idle_version: int | None = None
        self._rack_full = False              # 적재대 가득 알림을 보낸 상태 (빈 칸이 생기면 풀림)
        self._blocked_front: str | None = None
        self._anomalies: list[str] = []      # 자동으로 바로잡은 이상 (자가 시험이 위반으로 센다)
        self._lost_boxes: set[str] = set()   # 시작할 때 이미 AMR 위였던 박스 (새 드라이버 트레이에는 없다)
        self._internal_errors: list[str] = []
        self._error_texts: set[str] = set()
        self._halt: str | None = None        # 'pause' | 'estop' — [재개] 전까지 (AMR 구성을 다시 만들어도 이어받는다)
        self.stats: Counter[str] = Counter()  # 재고 이벤트 종류별 횟수

        cfg = config if config is not None else self._load_config()
        problems = config_problems(cfg)
        if problems:
            _sanitize(cfg, problems)
            self._notice('설정 값 오류 — 기본값으로 바꿨습니다: ' + ' / '.join(p for _, p in problems), 'error')
        self.config = cfg
        self.inventory = InventoryService(self.data_dir / db_name, self.config)
        try:
            self.auto_dispatch = bool(self.config.auto_dispatch)
            self._save_config()
            self._build_fleet()
            self._ob_seed = self.config.sim.seed
            self.overbridge = OverbridgeSimulator(self.config.sim, self._active_codes())
            self._sample_rng = random.Random(self.config.sim.seed * 7919 + 1)
            self._startup_cleanup()
            self.inventory.subscribe(self._on_inventory)
        except BaseException:
            self._disconnect_drivers()
            self.inventory.close()
            raise
        self._starting = False

    # ================================================================== 구독·알림
    def subscribe(self, callback: Callable[[str, dict], None]) -> None:
        self._subs.append(callback)
        for payload in self._startup_notices:   # 시작 중 알림 (드라이버 연결 실패·재시작 정리 등)
            try:
                callback('notice', dict(payload))
            except Exception:
                log.exception('Controller 구독자 오류 (notice)')

    def unsubscribe(self, callback: Callable[[str, dict], None]) -> None:
        if callback in self._subs:
            self._subs.remove(callback)

    def _publish(self, kind: str, payload: dict) -> None:
        for cb in list(self._subs):
            try:
                cb(kind, payload)
            except Exception:   # 화면 오류가 재고·AMR 처리를 멈추지 않게
                log.exception('Controller 구독자 오류 (%s)', kind)

    def _notice(self, text: str, level: str = 'info') -> None:
        payload = {'text': text, 'level': level, 'sim_now': self.sim_now}
        self.notice_log.append(payload)
        if level == 'error':
            log.warning('%s', text)
        if self._starting:
            self._startup_notices.append(payload)
        self._publish('notice', dict(payload))

    def _anomaly(self, text: str) -> None:
        self._anomalies.append(text)
        self._notice(text, 'warn')

    def _internal_error(self, what: str, exc: BaseException) -> None:
        log.exception('%s', what)
        text = f'{what}: {exc}'
        self._internal_errors.append(text)
        if text not in self._error_texts:      # 같은 오류가 매 tick 되풀이돼도 알림은 한 번
            self._error_texts.add(text)
            self._notice(f'내부 오류 — {text}', 'error')

    def _on_inventory(self, kind: str, payload: dict) -> None:
        self._inv_version += 1
        self.stats[kind] += 1
        if kind == 'MASTER':
            self._refresh_grades()
        self._publish(kind, payload)

    # ================================================================== 만들기
    def _load_config(self) -> AppConfig:
        path = self.config_path
        try:
            return AppConfig.load(path)
        except Exception as exc:   # JSON 문법 오류·형식 오류: 앱은 켜고(재고 조회) 원본은 남긴다
            backup = path.with_name(f'config.json.broken-{time.strftime("%Y%m%d-%H%M%S")}')
            try:
                path.replace(backup)
                where = f'원본은 {backup.name} 로 옮겼습니다'
            except OSError:
                where = '원본 파일은 그대로 두었습니다'
            self._notice(f'설정 파일(config.json)을 읽을 수 없어 기본값으로 시작합니다 ({exc}) — {where}.', 'error')
            return AppConfig()

    def _save_config(self) -> None:
        try:
            self.config.save(self.config_path)
        except OSError as exc:
            self._notice(f'설정 파일을 저장하지 못했습니다: {self.config_path} ({exc})', 'error')

    def _build_fleet(self) -> None:
        robots = list(self.config.robot_list())
        self.topology = build_cell_topology(self.inventory.layout.cfg, parks=max(2, len(robots)))
        self._drivers = [self._make_driver(rc, i) for i, rc in enumerate(robots)]
        self.fleet = FleetManager(self.topology, self._drivers, self.config.charge_below, self.config.charge_target)
        self.fleet.subscribe(self._on_order_event)
        self.fleet.tick(self.sim_now)   # 시계 맞춤 (첫 조각이 dt 0 으로 사라지지 않게)
        self._fleet_sig = _fleet_signature(self.config)
        self._idle_version = None

    def _make_driver(self, rc: RobotConfig, index: int) -> AmrDriver:
        kind = rc.driver.strip().lower() if isinstance(rc.driver, str) else ''
        try:
            if kind == 'sim':
                start = rc.start_node
                if start not in self.topology.nodes:
                    start = f'PARK{index + 1}'
                    self._notice(f'{rc.id}: 시작 위치 {rc.start_node!r} 가 지도에 없어 {start} 에서 시작합니다', 'warn')
                return SimulatedAmrDriver(rc.id, self.topology, self.config.sim, start)
            if kind == 'vda5050':
                from amr.vda5050 import Vda5050Driver
                drv: AmrDriver = Vda5050Driver(rc, self.topology)
            elif kind == 'tcp_json':
                from amr.tcp_json import TcpJsonDriver
                drv = TcpJsonDriver(rc)
            # 새 업체 드라이버는 여기에 한 줄씩 추가 (docs/AMR_연동_가이드.md §4) 하고 DRIVER_KINDS 에도 넣는다:
            # elif kind == '업체명':
            #     from amr.업체명 import 업체Driver
            #     drv = 업체Driver(rc)
            else:
                reason = f'알 수 없는 드라이버 종류 {rc.driver!r} ({"/".join(DRIVER_KINDS)})'
                self._notice(f'{rc.id}: {reason} — 이 AMR 은 쓰지 않습니다', 'error')
                return _UnavailableDriver(rc, reason)
        except Exception as exc:
            reason = f'드라이버를 만들 수 없습니다 ({exc})'
            self._notice(f'{rc.id}: {reason}', 'error')
            return _UnavailableDriver(rc, reason)
        try:
            drv.connect()
        except Exception as exc:   # 연결 실패: 앱은 계속, 그 AMR 은 연결 끊김으로 표시
            self._notice(f'{rc.id} 연결 실패 — 연결 끊김으로 표시합니다: {exc}', 'error')
        return drv

    def _disconnect_drivers(self) -> None:
        for drv in self._drivers:
            try:
                drv.disconnect()
            except Exception:
                log.exception('%s: 연결 끊기 실패', drv.robot_id)

    def _startup_cleanup(self) -> None:
        """이전 실행에서 끝나지 못한 작업의 박스 정리 (SPEC §12)."""
        released = flagged = 0
        for box in self.inventory.boxes((BoxState.CONVEYOR, BoxState.ON_AMR)):
            if box.state == BoxState.ON_AMR:
                self._lost_boxes.add(box.id)
            try:
                if box.state == BoxState.CONVEYOR and box.order_id:
                    self.inventory.release_reservation(box.id, '재시작')
                    released += 1
                elif box.state == BoxState.ON_AMR and not box.needs_check:
                    self.inventory.flag_needs_check(box.id, '재시작 — AMR 작업이 끝나지 않았습니다. 실제 위치를 확인하세요')
                    flagged += 1
            except InventoryError as exc:
                self._notice(f'재시작 정리 실패 ({box.id}): {exc}', 'error')
        if released or flagged:
            parts = []
            if released:
                parts.append(f'컨베이어 박스 {released}개 예약 해제')
            if flagged:
                parts.append(f'AMR 위 박스 {flagged}개 확인 필요')
            self._notice('재시작 정리: ' + ', '.join(parts), 'warn')

    def _active_codes(self) -> list[str]:
        return [g.code for g in self.inventory.grades(active_only=True)]

    def _refresh_grades(self) -> None:
        try:
            self.overbridge.set_grades(self._active_codes())
        except (ValueError, AttributeError) as exc:
            self._notice(f'오버브릿지 재종 목록을 바꾸지 못했습니다: {exc}', 'error')

    # ================================================================== 시간
    def tick(self, real_dt: float) -> None:
        if self._closed or self._in_tick:
            return
        try:
            real = float(real_dt)
        except (TypeError, ValueError):
            real = 0.0
        if not math.isfinite(real) or real < 0:
            real = 0.0
        real = min(real, MAX_REAL_DT)
        self._in_tick = True
        try:
            self._advance(real * self.sim_speed)
        finally:
            self._in_tick = False
        now = self._monotonic()
        if self._last_tick_pub is None or now - self._last_tick_pub >= TICK_PUBLISH_S:
            self._last_tick_pub = now
            self._publish('tick', {'sim_now': self.sim_now, 'speed': self.sim_speed})

    def _advance(self, sim_dt: float) -> None:
        left = sim_dt
        while left > 1e-9 and not self._closed:
            dt = min(SLICE_S, left)
            left -= dt
            self._slice(dt)

    def _slice(self, dt: float) -> None:
        """시뮬레이션 한 조각: 오버브릿지 → 도착 등록 → FleetManager → 자동 배차."""
        self.sim_now = round(self.sim_now + dt, 9)
        try:
            arrivals = self.overbridge.tick(dt, self._conveyor_has_room)
        except Exception as exc:
            self._internal_error('오버브릿지 시뮬레이션 오류', exc)
            arrivals = []
        for a in arrivals:
            try:
                self.inventory.register_arrival(a.grade, a.lot or None, a.weight_kg, note='오버브릿지')
            except ConveyorFull as exc:
                self._notice(f'오버브릿지 박스를 받지 못했습니다: {exc}', 'warn')
            except InventoryError as exc:
                self._notice(f'오버브릿지 박스 등록 실패 ({a.grade}): {exc}', 'error')
        try:
            self.fleet.tick(self.sim_now)
        except Exception as exc:
            self._internal_error('AMR 작업 관리 오류', exc)
        try:
            self._auto_dispatch()
        except InventoryError as exc:
            self._notice(f'자동 배차 실패: {exc}', 'error')
        except Exception as exc:
            self._internal_error('자동 배차 오류', exc)

    def _conveyor_has_room(self) -> bool:
        return len(self.inventory.conveyor()) < int(self.config.conveyor_capacity)

    # ================================================================== 자동 배차
    @staticmethod
    def _picks_pending(order: TransportOrder) -> bool:
        return any(s.action == 'pick' for s in order.steps[order.current:])

    def _auto_dispatch(self) -> None:
        fleet = self.fleet
        idle = fleet.idle_robots()
        # 배정한 AMR 이 작업을 받기 전에 다른 일(자동 충전 등)을 시작함 → 이 입고 작업은 취소하고 다시 배차.
        # 자동 배차를 끈 뒤에도 한다 (안 하면 충전 내내 컨베이어 박스 예약을 쥐고 수동 처리도 막는다).
        for o in fleet.orders(active_only=True):
            if (o.kind == 'INBOUND' and o.state == OrderState.QUEUED and o.meta.get('auto')
                    and o.robot is not None and o.robot not in idle):
                why = '다시 배차' if self.auto_dispatch else '자동 배차 꺼짐'
                fleet.cancel(o.id, f'{o.robot} 가 다른 작업을 시작해 {why}')
        if not self.auto_dispatch:
            return
        active = fleet.orders(active_only=True)
        if any(o.kind == 'INBOUND' and self._picks_pending(o) for o in active):
            return   # 도크 집기는 한 작업씩
        best: tuple[tuple, str, list[int]] | None = None
        for idx, rid in enumerate(idle):
            drv = fleet.driver(rid)
            st = drv.status()
            empty = [i for i, b in enumerate(self._tray_view(drv, st)) if b is None]
            if not empty:
                continue
            key = (-len(empty), round(self.topology.distance(st.node, _DOCK), 6) if st.node else math.inf, idx)
            if best is None or key < best[0]:
                best = (key, rid, empty)
        if best is None:
            return
        if self._idle_version == self._inv_version:
            return   # 지난번 이후 재고가 그대로 → 할 일 없음 (빈 컨베이어·가득 찬 적재대)
        _, rid, empty = best

        active_ids = {o.id for o in active}
        conveyor = self.inventory.conveyor()
        stale = [b for b in conveyor if b.order_id and b.order_id not in active_ids]
        for b in stale:   # 끝난 작업에 묶인 채 남은 박스 (정상이면 없음) — 풀어 준다
            try:
                self.inventory.release_reservation(b.id, f'끝난 작업 {b.order_id} 예약 정리')
                self._anomaly(f'박스 {b.id}: 끝난 작업 {b.order_id} 에 묶여 있어 예약을 풀었습니다')
            except InventoryError as exc:
                self._notice(f'박스 {b.id} 예약 정리 실패: {exc}', 'error')
        if stale:
            conveyor = self.inventory.conveyor()

        # 컨베이어 맨 앞부터 차례로 (건너뛰지 않는다 — AMR 은 실물 맨 앞 박스를 집는다)
        boxes: list[Box] = []
        for b in conveyor:
            if len(boxes) >= len(empty):
                break
            if b.order_id or b.slot_id or b.needs_check:
                if not boxes and self._blocked_front != b.id:
                    self._blocked_front = b.id
                    why = '확인 필요' if b.needs_check else f'{b.slot_id} 칸 예약'
                    self._notice(f'컨베이어 맨 앞 박스 {b.id} ({why}) 때문에 자동 배차를 멈췄습니다 — 수동 적재하거나 '
                                 '도착 취소하세요', 'warn')
                break
            boxes.append(b)
        if boxes:
            self._blocked_front = None
        if not boxes:
            self._idle_version = self._inv_version
            return

        snap = self.inventory.snapshot()
        free = snap.capacity - snap.occupied - snap.reserved - snap.blocked
        if free <= 0:
            self._on_rack_full()
            self._idle_version = self._inv_version
            return
        self._rack_full = False
        boxes = boxes[:free]

        oid = f'O{len(fleet.orders()) + 1:04d}'   # FleetManager 가 다음에 줄 작업 번호 (아래에서 확인)
        chosen: list[tuple[Box, Slot]] = []
        exclude: set[str] = set()
        for b in boxes:
            try:
                slot = self.inventory.reserve_slot(b.id, order_id=oid, exclude=exclude)
            except RackFull:
                self._on_rack_full()
                break
            except InventoryError as exc:
                self._notice(f'자동 배차: {b.id} 칸 예약 실패 — {exc}', 'error')
                break
            exclude.add(slot.id)
            chosen.append((b, slot))
        if not chosen:
            self._idle_version = self._inv_version
            return
        try:
            steps = self._inbound_steps(chosen, empty)
            order = fleet.new_order('INBOUND', steps, robot=rid, now=self.sim_now,
                                    meta={'boxes': [b.id for b, _ in chosen], 'auto': True})
        except Exception as exc:
            self._release_all(chosen, '입고 작업을 만들지 못함')
            self._internal_error('입고 작업 생성 실패', exc)
            return
        if order.id != oid:   # 작업 번호 예측이 틀림 (FleetManager 번호 규칙이 바뀐 경우) — 되돌린다
            fleet.cancel(order.id, '작업 번호 불일치')
            self._release_all(chosen, '작업 번호 불일치')
            self._internal_error('입고 작업 생성 실패', RuntimeError(f'작업 번호 {order.id} ≠ 예상 {oid}'))

    def _tray_view(self, drv: AmrDriver, st: RobotStatus) -> list[str | None]:
        """AMR 사다리 단별 박스 ID. 실제 AMR 은 실린 단만 보고하기도 한다 (VDA 5050 loads — 빈 AGV 는 []) →
        보고되지 않은 단은 설정의 사다리 단수(sim.trays)까지 빈 단으로 본다. 가상 AMR 은 보고 그대로."""
        trays = list(st.trays)
        if not isinstance(drv, SimulatedAmrDriver):
            trays += [None] * max(0, int(self.config.sim.trays) - len(trays))
        return trays

    def _on_rack_full(self) -> None:
        if not self._rack_full:
            self._rack_full = True
            self._notice('적재대에 빈 칸이 없어 자동 배차를 멈췄습니다 — 출고하거나 사용중지 칸을 풀면 다시 시작합니다', 'warn')

    def _release_all(self, chosen: list[tuple[Box, Slot]], reason: str) -> None:
        for b, _ in chosen:
            try:
                self.inventory.release_reservation(b.id, reason)
            except InventoryError as exc:
                self._notice(f'박스 {b.id} 예약 해제 실패: {exc}', 'error')

    def _inbound_steps(self, chosen: list[tuple[Box, Slot]], empty_trays: list[int]) -> list[Step]:
        """pick@DOCK × n (빈 트레이 아래부터) → drop × n (도크에서 가까운 접근 노드부터, 그다음 가까운 곳)."""
        layout = self.inventory.layout
        height = float(self.config.sim.conveyor_height_m)
        items = [(b.id, s, t) for (b, s), t in zip(chosen, sorted(empty_trays))]
        steps = [Step('', _DOCK, 'pick', height_m=height, tray=t, ref=bid, zone=_DOCK, meta={'box': bid, 'slot': s.id})
                 for bid, s, t in items]
        remaining = [(bid, s, t, layout.access_node(s.id)) for bid, s, t in items]
        cur = _DOCK
        while remaining:
            k = min(range(len(remaining)), key=lambda i: (round(self.topology.distance(cur, remaining[i][3]), 6), i))
            bid, s, t, node = remaining.pop(k)
            steps.append(Step('', node, 'drop', height_m=layout.fork_height(s.tier), tray=t, ref=s.id,
                              meta={'box': bid, 'slot': s.id}))
            cur = node
        return steps

    # ================================================================== 작업 이벤트
    def _on_order_event(self, ev: OrderEvent) -> None:
        order, step = ev.order, ev.step
        robot = ev.robot or order.robot
        if ev.kind == 'step_done' and step is not None and step.meta.get('box'):
            box = step.meta['box']
            try:
                if step.action == 'pick':
                    self.inventory.mark_picked(box, robot or '?', order.id)
                elif step.action == 'drop':
                    self.inventory.confirm_store(box, step.meta.get('slot') or None, robot)
            except InventoryError as exc:
                self._notice(f'작업 {order.id} ({step.id}): 재고 기록 실패 — {exc}', 'error')
        if ev.kind in ('done', 'failed', 'cancelled'):
            self._settle_order(order, ev.kind)
        self._publish('order', {'order': order.id, 'event': ev.kind, 'robot': robot, 'detail': ev.detail,
                                'kind': order.kind, 'step': step.id if step else None})

    def _settle_order(self, order: TransportOrder, kind: str) -> None:
        """끝난 작업에 아직 묶인 박스 정리: 컨베이어 → 예약 해제, AMR 위 → 확인 필요 (예약 유지)."""
        boxes = order.meta.get('boxes') or []
        label = ORDER_LABEL.get(order.state.value, kind)
        if kind == 'done':
            reason = f'작업 {order.id} 은 끝났는데 기록이 남음'
        else:
            reason = f'작업 {order.id} {label}' + (f': {order.error}' if order.error else '')
        released = flagged = 0
        for bid in boxes:
            try:
                box = self.inventory.box(bid)
            except InventoryError:
                continue
            if box.order_id != order.id:
                continue
            try:
                if box.state == BoxState.CONVEYOR:
                    self.inventory.release_reservation(box.id, reason)
                    released += 1
                elif box.state == BoxState.ON_AMR and not box.needs_check:
                    self.inventory.flag_needs_check(box.id, reason)
                    flagged += 1
            except InventoryError as exc:
                self._notice(f'작업 {order.id}: 박스 {box.id} 정리 실패 — {exc}', 'error')
        if kind == 'done':
            if released or flagged:
                self._anomaly(f'작업 {order.id} 완료 뒤 정리: 예약 해제 {released}개, 확인 필요 {flagged}개')
            return
        if not boxes:
            if kind == 'failed' and order.kind == 'CHARGE':
                self._notice(f'{order.robot} 충전 실패: {order.error}', 'warn')
            return
        text = f'작업 {order.id} {label}' + (f': {order.error}' if order.error else '')
        if flagged:
            self._notice(f'{text} — AMR 위 박스 {flagged}개 확인 필요 ([적재 완료 처리] 또는 [컨베이어로 되돌림])', 'warn')
        else:
            self._notice(text, 'warn' if kind == 'failed' else 'info')

    # ================================================================== 입고
    def scan(self, text: str) -> Box:
        """parse_scan → register_arrival. 오류는 InventoryError 그대로 (화면이 메시지 표시).
        성공·실패 모두 data/스캔기록.csv 에 원문과 결과를 남긴다."""
        raw = text if isinstance(text, str) else ''
        try:
            box = self._scan(text)
        except InventoryError as exc:
            self._log_scan(raw, '로트만' if type(exc).__name__ == 'LotOnlyScan' else '거절', str(exc))
            raise
        self._log_scan(raw, '등록', f'{box.id} · {box.grade} · {box.lot} · {box.weight_kg:g} kg')
        return box

    @property
    def scan_log_path(self) -> Path:
        return self.data_dir / SCAN_LOG_NAME

    def _log_scan(self, raw: str, result: str, detail: str) -> None:
        path = self.scan_log_path
        try:
            new = not path.exists()
            with path.open('a', encoding='utf-8-sig' if new else 'utf-8', newline='') as f:
                w = csv.writer(f)
                if new:
                    w.writerow(['시각', '보이는 원문', '글자 수', '결과', '내용'])
                w.writerow([time.strftime('%Y-%m-%d %H:%M:%S'), _csv_cell(scan_visible(raw)), len(raw), result,
                            _csv_cell(detail)])
        except OSError as exc:   # 기록 실패가 입고를 막으면 안 된다
            log.warning('스캔 기록 실패: %s', exc)

    def _scan(self, text: str) -> Box:
        key = ' '.join(text.split()).upper() if isinstance(text, str) else ''
        now = self._monotonic()
        last = self._last_scan
        if key and last is not None and last[0] == key and now - last[1] < SCAN_DEBOUNCE_S:
            raise InventoryError(SCAN_DEBOUNCE_TEXT)
        result = parse_scan(text, {g.code for g in self.inventory.grades()})
        box = self.inventory.register_arrival(result.grade, result.lot,
                                              15.0 if result.weight_kg is None else result.weight_kg,
                                              box_id=result.box_id, note='스캔')
        self._last_scan = (key, now)   # 받아들인 스캔만 기억 (실패한 스캔은 바로 다시 읽어도 된다)
        return box

    def add_sample_box(self, grade: str | None = None) -> Box:
        """샘플 박스 도착 (재종 없으면 사용 중인 재종 중 난수)."""
        rng = self._sample_rng
        if grade is None:
            codes = self._active_codes()
            if not codes:
                raise InventoryError('사용 중인 재종이 없습니다 — 기준정보에서 재종을 사용으로 바꾸세요.')
            grade = rng.choice(codes)
        weight = rng.randint(100, 149) / 10.0 if rng.random() < self.config.sim.residual_prob else 15.0
        return self.inventory.register_arrival(grade, None, weight, note='샘플')

    def manual_store(self, box_id: str, slot_id: str | None = None) -> Slot:
        return self.inventory.manual_store(box_id, slot_id)

    def cancel_arrival(self, box_id: str, reason: str) -> Box:
        """컨베이어 박스 도착 취소 (잘못 스캔·두 번 읽힘). 작업에 묶인 박스는 InventoryError."""
        return self.inventory.cancel_arrival(box_id, reason)

    def resolve_stored(self, box_id: str) -> Slot:
        """확인 필요(ON_AMR) 박스: 예약 칸에 실제로 들어갔다고 처리."""
        box = self._needs_check_box(box_id)
        slot = self.inventory.confirm_store(box.id, None, box.robot)
        self._unload(box.robot, box.id)
        return slot

    def resolve_return(self, box_id: str) -> None:
        """확인 필요 박스: 컨베이어로 되돌림.
        되돌린 박스는 컨베이어 맨 앞에 놓인다 → 아직 집을 박스가 남은 입고 작업이 있으면 AMR 이 실물 맨 앞(되돌린 박스)을
        집고 기록은 작업에 묶인 다른 박스로 남으므로, 그동안은 DockBusy 로 거부한다."""
        box = self._needs_check_box(box_id)
        picking = [o.id for o in self.fleet.orders(active_only=True) if o.kind == 'INBOUND' and self._picks_pending(o)]
        if picking:
            raise DockBusy(f'AMR 이 컨베이어에서 박스를 집는 중입니다 (작업 {", ".join(picking)}) — 집기가 끝난 뒤 '
                           '다시 하세요. 지금 맨 앞에 놓으면 AMR 이 다른 박스를 집게 됩니다.')
        self.inventory.return_to_conveyor(box.id, '확인 후 컨베이어로 되돌림')
        self._unload(box.robot, box.id)

    def _needs_check_box(self, box_id: str) -> Box:
        box = self.inventory.box(box_id)
        if box.state != BoxState.ON_AMR:
            raise InventoryError(f'AMR 위 박스가 아닙니다: {box.id} ({BOX_STATE_LABEL[box.state.value]})')
        if not box.needs_check:
            raise InventoryError(f'AMR 작업이 진행 중인 박스입니다: {box.id} — 작업이 끝나거나 멈춘 뒤 정리하세요.')
        rid = box.robot
        if rid is not None:
            try:
                drv = self.fleet.driver(rid)
            except KeyError:
                return box
            trays = [t.upper() if t else None for t in drv.status().trays]
            tray = trays.index(box.id.upper()) if box.id.upper() in trays else None
            order = self.fleet.robot_order(rid)
            if tray is not None and order is not None and order.current < len(order.steps):
                step = order.steps[order.current]
                if step.action in ('pick', 'drop') and step.tray == tray:
                    raise InventoryError(f'{rid} 가 그 트레이({tray}단)로 작업 중입니다 — 작업이 끝난 뒤 다시 하세요.')
        return box

    def _unload(self, rid: str | None, box_id: str) -> None:
        """사람이 정리한 박스를 AMR 트레이 표시에서도 뺀다."""
        if rid is None:
            return
        try:
            drv = self.fleet.driver(rid)
        except KeyError:
            return
        try:
            ok = drv.manual_unload(box_id)
        except Exception:
            log.exception('%s: manual_unload 실패', rid)
            ok = False
        if not ok and any(t and t.upper() == box_id.upper() for t in drv.status().trays):
            self._notice(f'{rid} 트레이 표시에 {box_id} 가 남아 있습니다 — AMR 쪽에서 트레이를 비워 주세요', 'warn')

    # ================================================================== 화면용 재고 작업 (재고 서비스로 그대로 전달)
    # 화면(ui)은 재고를 읽기만 하고, 바꾸는 일은 모두 이 메서드들로 한다. 오류는 InventoryError 그대로.
    def register_arrival(self, grade: str, lot: str | None = None, weight_kg: float = 15.0,
                         box_id: str | None = None) -> Box:
        """수동 입력 도착 (입고 탭의 재종·로트·중량 입력). 로트가 비어 있으면 자동 발번."""
        return self.inventory.register_arrival(grade, lot or None, weight_kg, box_id=box_id or None, note='수동 입력')

    def retrieve(self, slot_id: str, reason: str = '수동 출고') -> Box:
        """수동 출고 (OCCUPIED → EMPTY, 박스 OUT)."""
        return self.inventory.retrieve(slot_id, reason)

    def move_box(self, box_id: str, to_slot: str, reason: str = '') -> None:
        """적재된 박스를 다른 빈 칸으로 옮김 (실사 수정)."""
        self.inventory.move(box_id, to_slot, reason)

    def put_directly(self, slot_id: str, grade: str, lot: str | None, weight_kg: float = 15.0,
                     box_id: str | None = None, reason: str = '실사 등록') -> Box:
        """빈 칸에 박스 직접 등록 (실사)."""
        return self.inventory.put_directly(slot_id, grade, lot or '', weight_kg, box_id=box_id or None, reason=reason)

    def replace_slot_content(self, slot_id: str, grade: str, lot: str | None, weight_kg: float, reason: str) -> Box:
        """적재 칸 내용 수정: 지금 박스는 비우기(OUT) 기록 후 같은 칸에 새 박스로 다시 등록 (박스 ID 는 새로 발번).
        두 단계라서 먼저 값을 모두 확인한다 — 비운 뒤 등록이 실패해 칸이 빈 채로 남지 않게."""
        reason_s = ' '.join(str(reason or '').split())
        if not reason_s:
            raise InventoryError('수정 사유를 입력하세요.')
        slot = self.inventory.slot(slot_id)
        if slot.state.value != 'OCCUPIED':
            raise InventoryError(f'박스가 든 칸만 수정할 수 있습니다: {slot.id}')
        code = str(grade or '').strip().upper()
        if self.inventory.grade(code) is None:
            raise InventoryError(f'등록되지 않은 재종: {code or "(비어 있음)"}')
        if not _is_num(weight_kg) or not 0 < float(weight_kg) <= 100:
            raise InventoryError(f'중량이 올바르지 않습니다: {weight_kg!r} (0 초과 100 kg 이하)')
        lot_s = ' '.join(str(lot or '').split())
        if len(lot_s) > 60:
            raise InventoryError(f'로트가 너무 깁니다: {len(lot_s)}자 (60자 이내)')
        old = slot.box_id
        self.inventory.clear_slot(slot.id, f'내용 수정 ({reason_s}) — 새 박스로 다시 등록')
        return self.inventory.put_directly(slot.id, code, lot_s, float(weight_kg), reason=f'내용 수정: {reason_s} (이전 {old})')

    def clear_slot(self, slot_id: str, reason: str) -> None:
        """칸 비우기 (박스 OUT, 사유 필수)."""
        if not ' '.join(str(reason or '').split()):
            raise InventoryError('비우기 사유를 입력하세요.')
        self.inventory.clear_slot(slot_id, reason)

    def block_slot(self, slot_id: str, reason: str) -> None:
        """빈 칸 사용중지 (사유 필수)."""
        if not ' '.join(str(reason or '').split()):
            raise InventoryError('사용중지 사유를 입력하세요.')
        self.inventory.block_slot(slot_id, reason)
        self._idle_version = None

    def unblock_slot(self, slot_id: str) -> None:
        self.inventory.unblock_slot(slot_id)
        self._idle_version = None

    def upsert_grade(self, grade) -> None:
        """재종 추가·수정 (기준정보)."""
        self.inventory.upsert_grade(grade)

    def set_grade_active(self, code: str, active: bool) -> None:
        self.inventory.set_grade_active(code, active)

    def export_events_csv(self, path: Path) -> int:
        """이력 전체를 CSV 로 저장. 쓴 줄 수."""
        return self.inventory.export_events_csv(Path(path))

    def recommend_slot(self, grade: str, exclude: set[str] | None = None) -> str | None:
        """배정 정책이 지금 고를 칸 (예약하지 않음 — 화면 추천용). 빈 칸이 없으면 None."""
        from core.rack import SlotAllocator
        inv = self.inventory
        boxes = {b.id: b for b in inv.boxes(_ACTIVE_BOX)}
        try:
            return SlotAllocator(inv.layout, inv.policy).choose(str(grade or '').strip().upper(), inv.slots(), boxes,
                                                                set(exclude or ()))
        except ValueError:
            return None

    # ================================================================== 오버브릿지 시뮬레이션·운전
    def start_overbridge(self, boxes: int | None = None) -> None:
        if self.config.sim.seed != self._ob_seed and not self.overbridge.running:
            self.overbridge = OverbridgeSimulator(self.config.sim, self._active_codes())   # 시드가 바뀜
            self._ob_seed = self.config.sim.seed
        self._refresh_grades()
        try:
            self.overbridge.start(boxes)
        except ValueError as exc:
            raise InventoryError(str(exc)) from None

    def stop_overbridge(self) -> None:
        self.overbridge.stop()

    def set_sim_speed(self, speed: float) -> None:
        if not _is_num(speed) or not 0 < speed <= MAX_SIM_SPEED:
            raise InventoryError(f'배속은 0보다 크고 {MAX_SIM_SPEED:g} 이하여야 합니다: {speed!r}')
        self.sim_speed = float(speed)

    def set_auto_dispatch(self, on: bool) -> None:
        self.auto_dispatch = bool(on)
        if self.config.auto_dispatch != self.auto_dispatch:
            self.config.auto_dispatch = self.auto_dispatch
            self._save_config()
        self._idle_version = None

    def cancel_order(self, order_id: str) -> None:
        try:
            self.fleet.cancel(order_id, '사용자 취소')
        except KeyError:
            raise InventoryError(f'없는 작업입니다: {order_id}') from None

    def pause_all(self) -> None:
        self.fleet.pause_all()
        if self._halt != 'estop':
            self._halt = 'pause'

    def resume_all(self) -> None:
        self.fleet.resume_all()
        self._halt = None
        self._idle_version = None

    def estop(self) -> None:
        self._halt = 'estop'
        self.fleet.estop_all()
        self._notice('비상정지 — 모든 AMR 을 멈췄습니다. [재개]로 풉니다. 실려 있던 박스는 확인 필요로 표시됩니다.', 'warn')

    # ================================================================== 설정
    def apply_config(self, config: AppConfig) -> None:
        """설정 저장 + 반영 (AMR 대수·트레이 변경은 진행 중 작업이 없을 때만, 아니면 InventoryError)."""
        if not isinstance(config, AppConfig):
            raise InventoryError('설정 형식이 올바르지 않습니다.')
        new = copy.deepcopy(config)
        problems = config_problems(new, strict_drivers=True)
        if problems:
            raise InventoryError('설정을 바꿀 수 없습니다: ' + ' / '.join(p for _, p in problems))
        on_conveyor = len(self.inventory.conveyor())
        if new.conveyor_capacity < on_conveyor:
            raise InventoryError(f'컨베이어에 박스가 {on_conveyor}개 있어 적체 한도를 {new.conveyor_capacity}(으)로 '
                                 '줄일 수 없습니다 — 컨베이어 박스를 먼저 적재하세요.')
        rack_changed = asdict(new.rack) != asdict(self.inventory.layout.cfg)
        fleet_changed = rack_changed or _fleet_signature(new) != self._fleet_sig
        if fleet_changed:
            busy = [o for o in self.fleet.orders(active_only=True) if o.kind != 'PARK']
            if busy:
                ids = ', '.join(f'{o.id} {o.kind}' for o in busy[:3]) + (' …' if len(busy) > 3 else '')
                raise InventoryError(f'진행 중인 AMR 작업이 있어 적재대·AMR 구성을 바꿀 수 없습니다 ({ids}) — '
                                     '자동 배차를 끄고 작업이 끝난 뒤 다시 하세요.')
            on_amr = self.inventory.boxes((BoxState.ON_AMR,))
            if on_amr:
                raise InventoryError(f'AMR 위 박스가 {len(on_amr)}개 있어 적재대·AMR 구성을 바꿀 수 없습니다 — '
                                     '확인 필요 박스를 먼저 정리하세요.')
        backup = copy.deepcopy(self.config)
        _copy_config(new, self.config)
        try:
            if rack_changed:
                self.inventory.reconfigure_rack(new.rack.columns, new.rack.tiers)
            if new.allocation_policy != self.inventory.policy:
                self.inventory.set_policy(new.allocation_policy)
        except InventoryError:
            _copy_config(backup, self.config)
            layout = self.inventory.layout   # DB 가 원본: 적재대 구성이 이미 바뀌었으면 그쪽을 따른다
            for f in fields(RackConfig):
                setattr(self.config.rack, f.name, getattr(layout.cfg, f.name))
            self.config.allocation_policy = self.inventory.policy
            raise
        self.auto_dispatch = bool(self.config.auto_dispatch)
        self.fleet.charge_below = float(self.config.charge_below)
        self.fleet.charge_target = float(self.config.charge_target)
        if fleet_changed:
            self._disconnect_drivers()
            self._build_fleet()
            # 일시정지·비상정지 중이었으면 새 AMR 도 멈춘 채로 (설정 변경이 정지를 조용히 풀면 안 된다)
            if self._halt == 'estop':
                self.fleet.estop_all()
            elif self._halt == 'pause':
                self.fleet.pause_all()
            if self._halt:
                what = '비상정지' if self._halt == 'estop' else '일시정지'
                self._notice(f'새 AMR 구성도 {what} 상태입니다 — [재개]로 풉니다.', 'warn')
        self._refresh_grades()
        self._idle_version = None
        self._save_config()
        self._publish('config', {'rack_changed': rack_changed, 'fleet_rebuilt': fleet_changed})

    def mode_label(self) -> str:
        """'시뮬레이션 AMR' 또는 '실제 AMR (vda5050 …)'."""
        kinds = [str(getattr(d, 'kind', '?')) for d in self._drivers]
        real = sorted({k for k in kinds if k != 'sim'})
        if not real:
            return '시뮬레이션 AMR'
        label = f'실제 AMR ({"·".join(real)})'
        n_sim = kinds.count('sim')
        return label + (f' + 시뮬레이션 {n_sim}대' if n_sim else '')

    # ================================================================== 자가 진단
    def check_consistency(self) -> list[str]:
        """재고 불변식 + Controller 수준 점검 (트레이 ↔ ON_AMR 박스, 작업·구역·수량). 빈 목록 = 정상."""
        problems = list(self.inventory.check_invariants())
        boxes = {b.id: b for b in self.inventory.boxes(_ACTIVE_BOX)}
        holders: dict[str, list[str]] = {}
        sim_ids = {d.robot_id for d in self._drivers if isinstance(d, SimulatedAmrDriver)}
        for st in self.fleet.robots():
            if st.robot_id not in sim_ids:
                continue   # 실제 AMR 의 트레이 보고(적재물 ID)는 업체마다 달라 비교하지 않는다
            for i, bid in enumerate(st.trays):
                if bid is None:
                    continue
                key = bid.upper()
                holders.setdefault(key, []).append(st.robot_id)
                b = boxes.get(key)
                if b is None or b.state != BoxState.ON_AMR:
                    what = BOX_STATE_LABEL[b.state.value] if b else '기록 없음'
                    problems.append(f'{st.robot_id} {i}단 트레이에 {bid} 가 있는데 재고 기록은 {what}')
                elif b.robot != st.robot_id:
                    problems.append(f'{st.robot_id} {i}단 트레이에 {bid} 가 있는데 재고 기록은 {b.robot} 위')
        active = self.fleet.orders(active_only=True)
        active_ids = {o.id for o in active}
        for b in boxes.values():
            if b.state == BoxState.ON_AMR and b.robot in sim_ids:
                n = len(holders.get(b.id, []))
                if n > 1:
                    problems.append(f'박스 {b.id} 가 AMR 트레이 {n}곳에 있습니다')
                elif n == 0 and b.id not in self._lost_boxes:   # 재시작 전부터 실려 있던 박스만 트레이에 없어도 된다
                    problems.append(f'박스 {b.id}: {b.robot} 위로 기록됐는데 트레이에 없습니다')
            if b.order_id and b.order_id not in active_ids and not b.needs_check:
                problems.append(f'박스 {b.id}: 끝난 작업 {b.order_id} 에 묶여 있습니다 ({BOX_STATE_LABEL[b.state.value]})')
        picking = [o.id for o in active if o.kind == 'INBOUND' and self._picks_pending(o)]
        if len(picking) > 1:
            problems.append(f'도크에서 집는 입고 작업이 동시에 {len(picking)}건: {", ".join(picking)}')
        # AMR 은 실물 컨베이어 맨 앞 박스를 집는다 → 집을 박스는 컨베이어 맨 앞에 그 순서대로 있어야 한다
        front = [b.id for b in self.inventory.conveyor()]
        for o in active:
            if o.kind != 'INBOUND' or not self._picks_pending(o):
                continue
            want = [str(s.meta.get('box')) for s in o.steps[o.current:] if s.action == 'pick']
            if front[:len(want)] != want:
                problems.append(f'작업 {o.id} 이 집을 박스 {", ".join(want)} 가 컨베이어 맨 앞에 차례로 있지 않습니다 '
                                f'(맨 앞: {", ".join(front[:len(want)]) or "없음"})')
        for o in active:
            if o.kind == 'INBOUND' and self.sim_now - o.created > STUCK_ORDER_S:
                problems.append(f'작업 {o.id}: {STUCK_ORDER_S / 60:.0f}분 넘게 끝나지 않았습니다 '
                                f'({ORDER_LABEL[o.state.value]}, {o.current + 1}/{len(o.steps)}단계)')
        for zone in _ZONES:
            owner = self.fleet.zone_owner(zone)
            if owner is None:
                continue
            o = self.fleet.robot_order(owner)
            if o is None or o.current >= len(o.steps) or o.steps[o.current].zone != zone:
                problems.append(f'{zone} 구역을 {owner} 가 쥐고 있는데 그 구역 단계가 아닙니다')
        snap = self.inventory.snapshot()
        stored = sum(1 for b in boxes.values() if b.state == BoxState.STORED)
        reserved = sum(1 for b in boxes.values() if b.state != BoxState.STORED and b.slot_id)
        if snap.occupied != stored:
            problems.append(f'적재 칸 {snap.occupied}개 ≠ 보관 박스 {stored}개')
        if snap.reserved != reserved:
            problems.append(f'예약 칸 {snap.reserved}개 ≠ 목적지가 정해진 박스 {reserved}개')
        if snap.capacity != self.inventory.layout.capacity:
            problems.append(f'칸 수 {snap.capacity} ≠ 적재대 배치 {self.inventory.layout.capacity}')
        conveyor = sum(1 for b in boxes.values() if b.state == BoxState.CONVEYOR)
        if snap.conveyor != conveyor:
            problems.append(f'컨베이어 수 {snap.conveyor} ≠ 컨베이어 박스 {conveyor}')
        if conveyor > int(self.config.conveyor_capacity):
            problems.append(f'컨베이어 박스 {conveyor}개가 적체 한도 {self.config.conveyor_capacity} 를 넘었습니다')
        return problems

    def selftest(self, sim_hours: float = 2.0) -> list[str]:
        """화면 없이 오버브릿지 출고 + 자동 배차로 sim_hours 진행, 중간중간 수동 출고도 섞어서 불변식 검사. 위반 목록 반환.

        120 s 마다 무작위 재종의 가장 오래된 박스 출고, 약 15분마다 가상 AMR 고장 1회 주입 → 확인 필요 박스는
        [적재 완료 처리]/[컨베이어로 되돌림] 을 번갈아 정리, 약 20분마다 컨베이어 박스 1개 도착 취소. 60 s 마다 점검."""
        problems: list[str] = []
        seen: set[str] = set()

        def report(msgs: list[str]) -> None:
            for m in msgs:
                if m not in seen and len(problems) < 200:
                    seen.add(m)
                    h, rem = divmod(int(self.sim_now), 3600)
                    problems.append(f'[{h}:{rem // 60:02d}:{rem % 60:02d}] {m}')

        rng = random.Random(int(self.config.sim.seed) * 1_000_003 + 17)
        was_auto, self.auto_dispatch = self.auto_dispatch, True
        anomalies0, errors0 = len(self._anomalies), len(self._internal_errors)
        stores0, outs0 = self.stats['STORE'], self.stats['OUT']
        start = self.sim_now
        end = start + max(0.0, float(sim_hours)) * 3600.0
        next_check, next_out, next_fail = start + 60, start + 120, start + 900
        next_cancel, next_resolve = start + 1200, start + 5
        pending: dict[str, tuple[float, str]] = {}   # 확인 필요 박스 → (처음 본 시각, 'stored' | 'return')
        self._in_tick = True
        try:
            while self.sim_now < end - 1e-9 and not self._closed:
                if not self.overbridge.running and self._active_codes():
                    try:
                        self.start_overbridge()
                    except InventoryError as exc:
                        report([f'오버브릿지 출고 시작 실패: {exc}'])
                self._slice(SLICE_S)
                t = self.sim_now
                if t >= next_out:
                    next_out += 120
                    self._selftest_retrieve(rng, report)
                if t >= next_fail:
                    next_fail += 900
                    self._selftest_fail(rng)
                if t >= next_cancel:
                    next_cancel += 1200
                    free = [b for b in self.inventory.conveyor() if not b.order_id]
                    if free:
                        try:
                            self.cancel_arrival(free[-1].id, '자가 시험: 두 번 읽힘')
                        except InventoryError as exc:
                            report([f'도착 취소 실패: {exc}'])
                if t >= next_resolve:
                    next_resolve += 5
                    self._selftest_resolve(pending, report)
                if t >= next_check:
                    next_check += 60
                    report(self.check_consistency())
            report(self.check_consistency())
        finally:
            self._in_tick = False
            self.auto_dispatch = was_auto
        report(self._anomalies[anomalies0:])
        report([f'내부 오류: {e}' for e in self._internal_errors[errors0:]])
        if sim_hours >= 0.5 and self.stats['STORE'] == stores0:
            report(['자가 시험 동안 적재된 박스가 하나도 없습니다 (배차·AMR 이 멈춤)'])
        if sim_hours >= 0.5 and self.stats['OUT'] == outs0:
            report(['자가 시험 동안 출고된 박스가 하나도 없습니다'])
        return problems

    def _selftest_retrieve(self, rng: random.Random, report) -> None:
        stocked = sorted(self.inventory.snapshot().by_grade)
        if not stocked:
            return
        rows = self.inventory.find_grade(rng.choice(stocked))
        if rows:
            try:
                self.inventory.retrieve(rows[0].slot.id, '자가 시험 출고 (선입선출)')
            except InventoryError as exc:
                report([f'출고 실패: {exc}'])

    def _selftest_fail(self, rng: random.Random) -> None:
        sims = [d for d in self._drivers if isinstance(d, SimulatedAmrDriver)]
        if not sims:
            return
        busy = [d for d in sims if (o := self.fleet.robot_order(d.robot_id)) is not None and o.kind == 'INBOUND']
        rng.choice(busy or sims).inject_failure('자가 시험: 고장 주입')

    def _selftest_resolve(self, pending: dict[str, tuple[float, str]], report) -> None:
        """사람 흉내: 확인 필요 박스를 30 s 뒤에 번갈아 [적재 완료 처리]/[컨베이어로 되돌림].
        되돌림은 컨베이어에 자리가 날 때까지 기다리고, 10분이 지나도 자리가 없으면 적재 완료로 처리."""
        flagged = [b for b in self.inventory.boxes((BoxState.ON_AMR,)) if b.needs_check]
        live = {b.id for b in flagged}
        for bid in [k for k in pending if k not in live]:
            del pending[bid]
        t = self.sim_now
        for box in flagged:
            if box.id not in pending:
                pending[box.id] = (t, 'return' if self.stats['_resolve_turn'] % 2 else 'stored')
                self.stats['_resolve_turn'] += 1
            seen_at, how = pending[box.id]
            if t - seen_at < 30:
                continue
            try:
                if how == 'return':
                    try:
                        self.resolve_return(box.id)
                        self.stats['_resolved_return'] += 1
                    except (ConveyorFull, DockBusy):   # 컨베이어 만재·도크 집기 중 → 기다린다
                        if t - seen_at < 600:
                            continue
                        self.resolve_stored(box.id)
                        self.stats['_resolved_stored'] += 1
                else:
                    self.resolve_stored(box.id)
                    self.stats['_resolved_stored'] += 1
            except InventoryError as exc:
                report([f'확인 필요 박스 {box.id} 정리 실패: {exc}'])
            pending.pop(box.id, None)

    def reset_demo(self) -> Path:
        """시연용 '처음부터 다시'. 지금 기록을 data/백업/날짜-시각/ 에 복사한 뒤 적재대·컨베이어·이력을 비우고
        가상 AMR 을 주차 위치·배터리 가득으로, 시뮬레이션 시각을 0 으로 되돌린다. 재종·설정·스캔기록은 유지.
        실제 AMR 이 작업 중이면 거부 (InventoryError). 백업 폴더를 돌려준다."""
        real = [d for d in self._drivers if getattr(d, 'kind', '') != 'sim']
        if real and self.fleet.orders(active_only=True):
            raise InventoryError('실제 AMR 작업이 진행 중이라 초기화할 수 없습니다 — 작업을 끝내거나 취소한 뒤 하세요.')
        backup = self.data_dir / '백업' / time.strftime('%Y%m%d-%H%M%S')
        self.inventory.backup(backup / 'rack.db')
        for name in (SCAN_LOG_NAME, 'config.json'):
            src = self.data_dir / name
            if src.exists():
                try:
                    shutil.copy2(src, backup / name)    # 스캔기록은 복사만 (현장 시험 기록이 지워지지 않게)
                except OSError as exc:
                    log.warning('백업 복사 실패 %s: %s', src, exc)
        self.overbridge.stop()
        self._disconnect_drivers()
        self.inventory.reset_stock('처음부터 다시 (시연 초기화)')
        self.sim_now = 0.0
        self._last_tick_pub = None
        self._last_scan = None
        self._rack_full = False
        self._blocked_front = None
        self._halt = None
        self._anomalies.clear()
        self._lost_boxes.clear()
        self._internal_errors.clear()
        self._error_texts.clear()
        self.stats.clear()
        self._inv_version += 1
        self._build_fleet()
        self.overbridge = OverbridgeSimulator(self.config.sim, self._active_codes())
        self._sample_rng = random.Random(self.config.sim.seed * 7919 + 1)
        self._publish('config', {'rack_changed': True, 'fleet_rebuilt': True})
        self._notice(f'처음부터 다시 시작합니다 — 이전 기록은 {backup} 에 백업했습니다')
        return backup

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.overbridge.stop()
        except Exception:
            log.exception('오버브릿지 정지 실패')
        self._disconnect_drivers()
        try:
            self.inventory.close()
        except Exception:
            log.exception('재고 DB 닫기 실패')
