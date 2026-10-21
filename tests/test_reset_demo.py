"""시연용 '처음부터 다시'."""
from __future__ import annotations

import sqlite3

from controller import Controller
from core.config import AppConfig
from core.inventory import InventoryService
from core.models import BoxState, SlotState


def test_reset_demo_clears_stock_keeps_grades_and_backs_up(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        ctrl.inventory.set_grade_active('H05', False)          # 기준정보 변경은 남아야 한다
        ctrl.scan('PC9030 0001015661578')
        ctrl.start_overbridge()
        for _ in range(3000):
            ctrl.tick(0.1 * 20)                                 # 20배속 10분
        ctrl.inventory.block_slot('S-10-4', '점검')
        assert ctrl.inventory.snapshot().occupied > 0
        backup = ctrl.reset_demo()

        snap = ctrl.inventory.snapshot()
        assert snap.occupied == snap.reserved == snap.blocked == snap.conveyor == 0
        assert all(s.state == SlotState.EMPTY for s in ctrl.inventory.slots())
        assert ctrl.inventory.boxes() == []
        assert [e.kind for e in ctrl.inventory.events()] == ['CONFIG']
        assert ctrl.inventory.grade('H05').active is False
        assert ctrl.sim_now == 0.0 and not ctrl.overbridge.running and ctrl.fleet.orders() == []
        assert all(r.battery == 100.0 and r.node.startswith('PARK') for r in ctrl.fleet.robots())
        b = ctrl.scan('PC9030')                                 # 번호도 처음부터
        assert b.id.endswith('-0001') and b.lot == '0001015661578'
        assert ctrl.inventory.check_invariants() == []
        assert (backup / 'rack.db').exists() and (backup / '스캔기록.csv').exists()
        assert ctrl.scan_log_path.exists()                     # 현장 스캔 기록은 지우지 않는다
        for _ in range(600):                                   # 초기화 뒤에도 정상 운전
            ctrl.tick(0.1 * 20)
        assert ctrl.check_consistency() == []
    finally:
        ctrl.shutdown()
    # 백업 DB 는 초기화 전 재고를 그대로 담고 있다
    con = sqlite3.connect(str(backup / 'rack.db'))
    try:
        assert con.execute("SELECT COUNT(*) FROM boxes WHERE state = 'STORED'").fetchone()[0] > 0
    finally:
        con.close()


def test_reset_survives_restart(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    ctrl.scan('NC3215')
    ctrl.reset_demo()
    ctrl.shutdown()
    inv = InventoryService(tmp_path / 'data' / 'rack.db', AppConfig())
    try:
        assert inv.boxes() == [] and inv.check_invariants() == []
        assert inv.register_arrival('NC3215').state == BoxState.CONVEYOR
    finally:
        inv.close()
