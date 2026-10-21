"""통합 검토 (Controller + core + amr + main.py) 에서 찾은 결함의 재현 시험.
화면 없이 임시 폴더에서 돈다. 각 시험은 고치기 전에 실패하는 것을 확인했다."""
from __future__ import annotations

import copy

import pytest

from amr.models import OrderState, RobotMode
from controller import Controller
from core.config import AppConfig
from core.inventory import InventoryError
from core.models import BoxState


# ---------------------------------------------------------------- 도우미
@pytest.fixture
def make(tmp_path):
    made: list[Controller] = []

    def factory(robots: int = 1, sub: str = 'a', trays: int = 3) -> Controller:
        cfg = AppConfig()
        cfg.sim.amr_count = robots
        cfg.sim.trays = trays
        c = Controller(tmp_path / sub, cfg)
        made.append(c)
        return c

    yield factory
    for c in made:
        c.shutdown()


def run_until(c: Controller, cond, limit: float = 900.0) -> None:
    c.set_sim_speed(1.0)
    end = c.sim_now + limit
    while not cond():
        assert c.sim_now < end, f'시뮬레이션 {limit:.0f} s 안에 조건이 만족되지 않음'
        c.tick(0.2)


def active_inbound(c: Controller):
    return [o for o in c.fleet.orders(active_only=True) if o.kind == 'INBOUND']


def watch_picks(c: Controller) -> list[tuple[str, str | None]]:
    """AMR 이 집었다고 기록하는 순간의 (기록된 박스, 그때 컨베이어 맨 앞 박스). 실물 AMR 은 맨 앞 박스를 집는다."""
    seen: list[tuple[str, str | None]] = []
    inv = c.inventory
    orig = inv.mark_picked

    def mark_picked(box_id, robot, order_id=None):
        conv = inv.conveyor()
        seen.append((box_id, conv[0].id if conv else None))
        return orig(box_id, robot, order_id)

    inv.mark_picked = mark_picked
    return seen


def needs_check_on_tray0(c: Controller) -> str:
    """박스 1개 입고 → 내려놓기 단계에서 고장 → 확인 필요 박스가 AMR 0단 트레이에 남는다."""
    box = c.add_sample_box()
    run_until(c, lambda: c.inventory.box(box.id).state == BoxState.ON_AMR, 300)
    order = active_inbound(c)[0]
    run_until(c, lambda: order.current == 1, 60)
    c.fleet.driver(order.robot).inject_failure('시험 고장')
    run_until(c, lambda: order.state == OrderState.FAILED, 30)
    b = c.inventory.box(box.id)
    assert b.state == BoxState.ON_AMR and b.needs_check
    return box.id


# ---------------------------------------------------------------- I1: 도크에서 집는 중 [컨베이어로 되돌림]
def test_resolve_return_refused_while_amr_is_picking_at_the_dock(make):
    """되돌린 박스는 컨베이어 맨 앞에 놓인다. 그 사이 다른 박스를 집으러 가는 입고 작업이 있으면 AMR 은 실물 맨 앞
    (되돌린 박스)을 집는데 기록은 작업에 묶인 박스로 남는다 → 칸 기록이 실물과 어긋난다. 집기가 끝날 때까지 거부해야 한다."""
    c = make()
    seen = watch_picks(c)
    back = needs_check_on_tray0(c)
    c1, c2 = c.add_sample_box(), c.add_sample_box()
    run_until(c, lambda: any(o.state == OrderState.RUNNING for o in active_inbound(c)), 30)
    order = active_inbound(c)[0]
    assert [s.tray for s in order.steps if s.action == 'pick'] == [1, 2]   # 빈 트레이만 — 0단 확인 필요 박스는 그대로

    with pytest.raises(InventoryError, match='집는 중'):
        c.resolve_return(back)
    assert c.inventory.box(back).state == BoxState.ON_AMR and c.inventory.box(back).needs_check
    assert [b.id for b in c.inventory.conveyor()] == [c1.id, c2.id]

    run_until(c, lambda: order.state == OrderState.DONE, 900)
    assert all(box == head for box, head in seen), seen
    c.resolve_return(back)                               # 집기가 끝나면 된다
    assert c.inventory.conveyor()[0].id == back
    assert c.check_consistency() == []


def test_check_consistency_flags_conveyor_front_not_matching_pick_order(make):
    """자가 진단: 집기가 남은 입고 작업의 박스가 컨베이어 맨 앞에 순서대로 있지 않으면 위반으로 알린다."""
    c = make()
    back = needs_check_on_tray0(c)
    c.add_sample_box()
    run_until(c, lambda: bool(active_inbound(c)), 30)
    c.inventory.return_to_conveyor(back, '시험: Controller 를 거치지 않고 맨 앞에 끼워 넣음')
    assert any('맨 앞' in p for p in c.check_consistency())


def test_selftest_still_uses_both_resolve_paths(make):
    c = make(robots=2)
    assert c.selftest(2.0) == []
    assert c.stats['_resolved_return'] >= 1 and c.stats['_resolved_stored'] >= 1


# ---------------------------------------------------------------- I2: 설정 변경이 일시정지·비상정지를 풀어 버림
@pytest.mark.parametrize('how, mode', [('pause_all', RobotMode.PAUSED), ('estop', RobotMode.ERROR)])
def test_apply_config_rebuild_keeps_pause_and_estop(make, how, mode):
    """일시정지·비상정지 중에 AMR 대수·트레이를 바꾸면 새 AMR 이 멈춘 상태를 이어받아야 한다 (조용히 풀리면 안 된다)."""
    c = make()
    getattr(c, how)()
    run_until(c, lambda: c.fleet.robots()[0].mode == mode, 5)
    new = copy.deepcopy(c.config)
    new.sim.amr_count = 2
    c.apply_config(new)
    box = c.add_sample_box()
    for _ in range(300):
        c.tick(0.2)
    assert [r.mode for r in c.fleet.robots()] == [mode, mode]
    assert c.inventory.box(box.id).state == BoxState.CONVEYOR and c.stats['PICK'] == 0
    c.resume_all()
    run_until(c, lambda: c.inventory.box(box.id).state == BoxState.STORED, 900)


# ---------------------------------------------------------------- I3: 적체 한도를 지금 컨베이어 수보다 낮춤
def test_apply_config_refuses_conveyor_capacity_below_current_count(make):
    c = make()
    c.set_auto_dispatch(False)
    for _ in range(4):
        c.add_sample_box()
    new = copy.deepcopy(c.config)
    new.conveyor_capacity = 3
    with pytest.raises(InventoryError, match='적체 한도'):
        c.apply_config(new)
    assert c.config.conveyor_capacity == 5 and c.check_consistency() == []
    new.conveyor_capacity = 4                            # 지금 수와 같으면 된다
    c.apply_config(new)
    assert c.config.conveyor_capacity == 4 and c.check_consistency() == []


# ---------------------------------------------------------------- I4: 실행 중에 다시 빌드하면 app 폴더가 반쯤 지워짐
def _load_build():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'build.py'
    spec = importlib.util.spec_from_file_location('build_script_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='실행 중인 exe 잠금은 윈도 동작')
def test_build_refuses_while_app_exe_is_running(tmp_path, monkeypatch):
    """완분적재대.exe 가 실행 중이면 윈도가 exe·DLL 을 잠가 rmtree 가 중간에 실패 → app 의 _internal 폴더가 반쯤 지워진 채
    빌드가 PermissionError 로 죽는다. 빌드 전에 알아채고 아무것도 지우지 않은 채 안내 후 끝나야 한다."""
    import sys
    from pathlib import Path
    build = _load_build()
    assert build.exe_in_use(Path(sys.executable)) is True        # 지금 돌고 있는 python.exe 는 잠겨 있다
    idle = tmp_path / 'idle.exe'
    idle.write_bytes(b'MZ')
    assert build.exe_in_use(idle) is False
    assert build.exe_in_use(tmp_path / 'none.exe') is False

    app = tmp_path / 'app'
    (app / '_internal').mkdir(parents=True)
    (app / '_internal' / 'keep.dll').write_bytes(b'x')
    (app / f'{build.NAME}.exe').write_bytes(b'MZ')
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    monkeypatch.setattr(build, 'exe_in_use', lambda p: True)
    ran = []
    import PyInstaller.__main__ as pim
    monkeypatch.setattr(pim, 'run', lambda args: ran.append(args))
    assert build.main() == 1
    assert ran == [] and (app / '_internal' / 'keep.dll').exists()   # PyInstaller 도 안 돌리고 아무것도 안 지움


# ---------------------------------------------------------------- I5: exe 에서 paho-mqtt 안내가 틀림
def test_vda_hint_in_frozen_exe_says_rebuild(monkeypatch):
    """exe 는 시스템 Python 의 패키지를 못 쓴다 — 'pip install 후 다시 시작' 만으로는 계속 같은 오류.
    exe 로 실행 중이면 build.py 로 다시 만들라고 안내해야 한다."""
    import sys
    from amr.topology import build_cell_topology
    from amr.vda5050 import Vda5050Driver
    from core.config import RackConfig, RobotConfig
    for name in ('paho', 'paho.mqtt', 'paho.mqtt.client'):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    drv = Vda5050Driver(RobotConfig(id='AMR-V9', driver='vda5050', host='localhost', port=1883),
                        build_cell_topology(RackConfig(), parks=2))
    with pytest.raises(RuntimeError) as exc:
        drv.connect()
    assert 'build.py' in str(exc.value) and 'pip install paho-mqtt' in str(exc.value)


# ---------------------------------------------------------------- I6: 실제 AMR(VDA 5050) 은 빈 트레이를 보고하지 않음
class _FakeMqtt:
    def __init__(self):
        self.published = []
        self.on_message = None

    def connect(self, host, port):
        pass

    def subscribe(self, topic_name, qos=0):
        pass

    def publish(self, topic_name, payload, qos=0, retain=False):
        import json
        self.published.append((topic_name, json.loads(payload)))

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def _vda_state(loads, node='PARK1'):
    from amr.vda5050 import VDA_VERSION
    return {'headerId': 1, 'timestamp': '2026-10-20T00:00:00.000Z', 'version': VDA_VERSION, 'manufacturer': 'ACME',
            'serialNumber': 'SN-7', 'orderId': '', 'orderUpdateId': 0, 'lastNodeId': node, 'lastNodeSequenceId': 0,
            'driving': False, 'paused': False, 'operatingMode': 'AUTOMATIC', 'nodeStates': [], 'edgeStates': [],
            'actionStates': [], 'errors': [], 'loads': loads,
            'agvPosition': {'x': 0.0, 'y': 0.0, 'theta': 0.0, 'mapId': 'cell', 'positionInitialized': True},
            'batteryState': {'batteryCharge': 80.0, 'charging': False}}


@pytest.mark.parametrize('loads, trays', [([], [0, 1, 2]),
                                          ([{'loadId': 'OLD-1', 'loadPosition': '0'}], [1, 2])])
def test_auto_dispatch_uses_real_amr_that_reports_only_loaded_trays(tmp_path, monkeypatch, loads, trays):
    """VDA 5050 의 loads 는 실린 짐만 담는다 → 빈 AGV 는 trays == [] 로 보인다. Controller 가 이것을 '트레이 없음'으로 읽으면
    실제 AMR 에는 입고 작업을 영원히 주지 않는다 (화면은 'AMR 1대 · 대기' 인데 아무 일도 안 일어남).
    보고되지 않은 단은 설정의 사다리 단수(sim.trays)까지 빈 단으로 본다."""
    import amr.vda5050 as vda
    from core.config import RobotConfig
    client = _FakeMqtt()

    class FakeVda(vda.Vda5050Driver):
        def __init__(self, cfg, topology):
            super().__init__(cfg, topology, client_factory=lambda: client)

    monkeypatch.setattr(vda, 'Vda5050Driver', FakeVda)
    cfg = AppConfig()
    cfg.robots = [RobotConfig(id='V1', driver='vda5050', host='10.0.0.5', port=1883, manufacturer='ACME',
                              serial='SN-7', start_node='PARK1')]
    c = Controller(tmp_path, cfg)
    try:
        drv = c.fleet.driver('V1')
        drv.handle_message('uagv/v2/ACME/SN-7/connection', {'connectionState': 'ONLINE'})
        drv.handle_message('uagv/v2/ACME/SN-7/state', _vda_state(loads))
        assert c.fleet.idle_robots() == ['V1']
        c.set_auto_dispatch(False)
        for _ in range(3):
            c.add_sample_box()
        c.set_auto_dispatch(True)
        for _ in range(5):
            c.tick(0.2)
        orders = active_inbound(c)
        assert len(orders) == 1 and orders[0].robot == 'V1'
        assert [s.tray for s in orders[0].steps if s.action == 'pick'] == trays
        assert any(t.endswith('/order') for t, _ in client.published)          # AGV 로 실제 주문이 나갔다
    finally:
        c.shutdown()


# ---------------------------------------------------------------- I7: 자동 배차를 끈 직후 AMR 이 충전을 시작하면 입고 작업이 예약을 쥔 채 멈춤
def test_queued_auto_inbound_is_released_even_after_auto_dispatch_is_turned_off(make):
    """입고 작업이 만들어진(QUEUED) 다음 tick 전에 [자동 배차 끄기] → 그 AMR 이 자동 충전을 시작하면, 재배차 규칙이
    자동 배차가 켜져 있을 때만 돌아서 작업이 충전(최대 1시간 반) 내내 컨베이어 박스 예약을 쥔다 (수동 적재·도착 취소도 거부)."""
    c = make()
    c.set_auto_dispatch(False)
    boxes = [c.add_sample_box() for _ in range(2)]
    c.set_auto_dispatch(True)
    c.set_sim_speed(1.0)
    c.tick(0.2)
    order = active_inbound(c)[0]
    assert order.state == OrderState.QUEUED
    c.set_auto_dispatch(False)
    c.fleet.driver(order.robot)._battery = 10.0          # 시험: 다음 tick 에 자동 충전이 먼저 잡힌다
    for _ in range(5):
        c.tick(0.2)
    assert order.state == OrderState.CANCELLED
    assert all(c.inventory.box(b.id).order_id is None and c.inventory.box(b.id).slot_id is None for b in boxes)
    c.manual_store(boxes[0].id)                          # 사람이 바로 처리할 수 있다
    assert c.check_consistency() == []


# ---------------------------------------------------------------- I8: --selftest·--shot 이 임시 데이터 폴더를 남김
def test_selftest_cli_removes_its_temp_data_folder(tmp_path):
    """--data 없이 --selftest 하면 임시 폴더(TEMP) 의 rackwms-… 에 재고 DB·설정·로그를 만들고 지우지 않아,
    실행할 때마다 쌓인다."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    main = Path(__file__).resolve().parents[1] / 'src' / 'main.py'
    tmp = tmp_path / 'temp'
    tmp.mkdir()
    env = dict(os.environ, TEMP=str(tmp), TMP=str(tmp), QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
    log = tmp_path / 'st.log'
    r = subprocess.run([sys.executable, str(main), '--selftest', str(log), '--hours', '0.05'], env=env,
                       capture_output=True, timeout=180)
    assert r.returncode == 0, (r.stdout, r.stderr, log.read_text(encoding='utf-8') if log.exists() else '')
    assert log.read_text(encoding='utf-8').startswith('OK')
    assert (tmp_path / 'selftest.png').exists()
    left = [p.name for p in tmp.iterdir() if p.name.startswith('rackwms-') and p.name != 'rackwms-ui']
    assert left == []


# ---------------------------------------------------------------- 무작위 통합 시험 (회귀 방지용 축약판)
class _Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Fuzz:
    """Controller 공개 API 를 현장처럼 무작위로 섞어 부르고, 동작마다·시뮬레이션 1분마다 점검한다.
    점검: 재고 불변식 + Controller 일관성(트레이 ↔ AMR 위 박스, 컨베이어 맨 앞 ↔ 집을 박스, 구역·수량),
    tick 예외 없음, 공개 API 는 InventoryError 만, 내부 오류·자동 정리 이상 없음, 진행 없이 30분 넘은 RUNNING 작업 없음,
    AMR 이 집었다고 기록한 박스 = 그 순간 컨베이어 맨 앞 박스 (실물 AMR 은 맨 앞 박스를 집는다)."""

    def __init__(self, base, seed: int, robots: int, trays: int):
        import random
        self.rng = random.Random(seed)
        self.base = base
        cfg = AppConfig()
        cfg.sim.amr_count, cfg.sim.trays, cfg.sim.seed = robots, trays, seed
        cfg.sim.battery_per_s *= 10          # 자동 충전도 일어나게
        cfg.sim.battery_per_m *= 10
        self.problems: list[str] = []
        self.mono = _Mono()
        self.halted_at: float | None = None
        self.elapsed = 0.0                   # 재시작 전까지 흐른 시뮬레이션 시간 (재시작하면 sim_now 가 0 부터)
        self.ctrl = Controller(base, cfg)
        self._wire()

    def _wire(self):
        c = self.ctrl
        c._monotonic = self.mono
        inv, orig = c.inventory, c.inventory.mark_picked

        def mark_picked(box_id, robot, order_id=None):
            conv = inv.conveyor()
            if not conv or conv[0].id != box_id:
                self.fail(f'집은 박스 {box_id} ≠ 컨베이어 맨 앞 {conv[0].id if conv else None}')
            return orig(box_id, robot, order_id)

        inv.mark_picked = mark_picked
        self.fleet, self.progress = None, {}
        self._hook_fleet()

    def _hook_fleet(self):
        if self.fleet is not self.ctrl.fleet:
            self.fleet, self.progress = self.ctrl.fleet, {}

            def on_order(ev):
                if ev.kind != 'waiting':
                    self.progress[ev.order.id] = self.ctrl.sim_now

            self.fleet.subscribe(on_order)

    def fail(self, text: str):
        if len(self.problems) < 20:
            self.problems.append(f'[{self.elapsed + self.ctrl.sim_now:.0f}s] {text}')

    def check(self, where: str):
        c = self.ctrl
        for p in c.check_consistency():
            if '분 넘게 끝나지 않았습니다' in p and self.halted_at is not None:
                continue
            self.fail(f'{where}: {p}')
        for e in c._internal_errors + c._anomalies:
            self.fail(f'{where}: {e}')
        c._internal_errors.clear()
        c._anomalies.clear()
        if self.halted_at is None:
            for o in c.fleet.orders(active_only=True):
                if o.kind != 'CHARGE' and o.state == OrderState.RUNNING \
                        and c.sim_now - self.progress.get(o.id, o.created) > 1800:
                    self.fail(f'{o.id} {o.kind} 30분 넘게 진행 없음')

    def act(self, name: str, fn, *args):
        try:
            fn(*args)
        except InventoryError:
            pass
        except Exception as exc:   # 공개 API 는 InventoryError 만 던져야 한다
            self.fail(f'{name}{args}: {type(exc).__name__} {exc}')
        self.check(name)

    def advance(self, sim_s: float):
        c = self.ctrl
        end, next_min = c.sim_now + sim_s, (int(c.sim_now // 60) + 1) * 60
        while c.sim_now < end:
            real = self.rng.choice([0.05, 0.1, 0.2, 0.5])
            self.mono.t += real
            try:
                c.tick(real)
            except Exception as exc:
                self.fail(f'tick 예외 {type(exc).__name__} {exc}')
                return
            if c.sim_now >= next_min:
                next_min += 60
                self.check('1분 점검')
                occ = [s.id for s in c.inventory.slots() if s.state.value == 'OCCUPIED']
                if occ and self.rng.random() < 0.6:   # 출고가 있어야 적재대가 가득 찬 채 멈추지 않는다
                    self.act('retrieve', c.retrieve, self.rng.choice(occ))
            if self.halted_at is not None and c.sim_now - self.halted_at > 120:
                self.act('resume_all', c.resume_all)
                self.halted_at = None

    def step(self):
        c, r = self.ctrl, self.rng
        inv = c.inventory
        grades = [g.code for g in inv.grades(active_only=True)]
        conv = inv.conveyor()
        slots = inv.slots()
        empty = [s.id for s in slots if s.state.value == 'EMPTY']
        occ = [s for s in slots if s.state.value == 'OCCUPIED']
        blocked = [s.id for s in slots if s.state.value == 'BLOCKED']
        checks = [b.id for b in inv.boxes((BoxState.ON_AMR,)) if b.needs_check]
        active = [o.id for o in c.fleet.orders(active_only=True)]
        k = r.randrange(22)
        if k < 4:
            g, n = r.choice(grades), r.randrange(10 ** 6)
            self.act('scan', c.scan, r.choice([g, f'{g} L{n}', f'{g},L{n},14.2', g.lower()]))
        elif k == 4:
            self.act('scan_bad', c.scan, r.choice(['ZZ999', '', 'PC9030 999']))
        elif k == 5:
            self.act('sample', c.add_sample_box)
        elif k == 6:
            self.act('overbridge', c.start_overbridge if r.random() < 0.8 else c.stop_overbridge)
        elif k == 7:
            self.act('speed', c.set_sim_speed, r.choice([5, 20, 60]))
        elif k == 8:
            self.act('auto', c.set_auto_dispatch, r.random() < 0.8)
        elif k == 9 and conv:
            self.act('manual_store', c.manual_store, r.choice(conv).id)
        elif k == 10 and occ and empty:
            self.act('move', c.move_box, r.choice(occ).box_id, r.choice(empty), '실사')
        elif k == 11 and empty:
            self.act('put', c.put_directly, r.choice(empty), r.choice(grades), None)
        elif k == 12 and occ:
            self.act('clear', c.clear_slot, r.choice(occ).id, '실사')
        elif k == 13:
            if empty and r.random() < 0.5:
                self.act('block', c.block_slot, r.choice(empty), '점검')
            elif blocked:
                self.act('unblock', c.unblock_slot, r.choice(blocked))
        elif k == 14 and conv:
            self.act('cancel_arrival', c.cancel_arrival, r.choice(conv).id, '두 번 읽힘')
        elif k == 15 and active:
            self.act('cancel_order', c.cancel_order, r.choice(active))
        elif k == 16:
            self.act('halt', c.pause_all if r.random() < 0.6 else c.estop)
            self.halted_at = c.sim_now
        elif k == 17:
            self.act('fail', r.choice(c._drivers).inject_failure, '주입')
        elif k in (18, 19) and checks:
            fn = c.resolve_stored if r.random() < 0.5 else c.resolve_return
            self.act(fn.__name__, fn, r.choice(checks))
        elif k == 20:
            new = copy.deepcopy(c.config)
            which = r.randrange(4)
            if which == 0:
                new.sim.trays = r.choice([2, 3, 4])
            elif which == 1:
                new.sim.amr_count = r.choice([1, 2, 3])
            elif which == 2:
                new.allocation_policy = r.choice(['grade_zone', 'lowest_first', 'balanced'])
            else:
                new.conveyor_capacity = r.choice([4, 5, 6])
            self.act('apply_config', c.apply_config, new)
            self._hook_fleet()
        elif k == 21:                                   # 앱 종료 후 다시 켜기 (작업 도중일 수도)
            self.elapsed += c.sim_now
            c.shutdown()
            self.ctrl = Controller(self.base)
            self.halted_at = None
            self._wire()
            self.check('restart')

    def run(self, hours: float) -> list[str]:
        self.act('overbridge', self.ctrl.start_overbridge)
        self.ctrl.set_sim_speed(20)
        while self.elapsed + self.ctrl.sim_now < hours * 3600 and len(self.problems) < 20:
            self.step()
            self.advance(self.rng.uniform(5, 90))
        self.check('끝')
        self.ctrl.shutdown()
        return self.problems


# AMR 1~3대 × 트레이 2~4단. 이 시드들은 I1(집는 중 되돌림)을 고치기 전 코드에서 위반을 잡았다.
@pytest.mark.parametrize('seed, robots, trays', [(3, 1, 2), (30, 1, 2), (13, 2, 3), (11, 3, 4), (14, 3, 4)])
def test_randomized_controller_run_keeps_every_invariant(tmp_path, seed, robots, trays):
    assert _Fuzz(tmp_path, seed, robots, trays).run(1.5) == []
