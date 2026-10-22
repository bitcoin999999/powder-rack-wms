"""완분 라벨 바코드 (MFC 출고 라벨): 00 + 로트 8 + 중량 g 6 + 생산순서 3 + 재종. 현장 사진 3종이 기준."""
from __future__ import annotations

import pytest

from controller import Controller
from core.config import AppConfig
from core.inventory import InventoryError, LotOnlyScan, parse_label, parse_scan

# (바코드 아래 숫자, 라벨 큰 번호, 재종, 중량 kg, 생산순서) — 현장 사진 그대로
REAL = [
    ('0026080160015000001KP320', '26080160', 'KP320', 15.0, 1),
    ('0026080140015000010A30', '26080140', 'A30', 15.0, 10),
    ('0020080030002000017KU10', '20080030', 'KU10', 2.0, 17),
]


@pytest.mark.parametrize('code,lot,grade,kg,seq', REAL)
def test_real_labels_decode(code, lot, grade, kg, seq):
    r = parse_scan(code, set())
    assert (r.grade, r.lot, r.weight_kg, r.seq, r.box_id) == (grade, lot, kg, seq, f'{lot}-{seq:03d}')
    assert r.label and r.new_grade and r.raw == code


def test_reader_noise_is_ignored():
    base = parse_scan('0026080160015000001KP320', {'KP320'})
    for noisy in (']C10026080160015000001KP320', '0026080160015000001kp320\r\n', '  0026080160015000001KP320 ',
                  '0026080160015000001KP320\t'):
        r = parse_scan(noisy, {'KP320'})
        assert (r.grade, r.lot, r.weight_kg, r.seq, r.box_id, r.new_grade) == \
            (base.grade, base.lot, base.weight_kg, base.seq, base.box_id, False)


def test_known_grade_keeps_master_spelling():
    assert parse_scan('0026080140015000010A30', {'a30'}).grade == 'a30'


def test_not_a_label_falls_back_to_old_formats():
    assert parse_label('PC9030 0001015661578', {'PC9030'}) is None
    assert parse_label('002608016001500000KP320', set()) is None          # 한 자리 모자람
    assert parse_scan('PC9030 0001015661578', {'PC9030'}).lot == '0001015661578'
    with pytest.raises(LotOnlyScan):
        parse_scan('26080160', set())                                    # 라벨 큰 번호만 입력 → 로트로
    with pytest.raises(InventoryError, match='중량이 0'):
        parse_scan('0026080160000000001KP320', set())


def test_controller_registers_label_and_auto_adds_grade(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        notes = []
        ctrl.subscribe(lambda k, p: notes.append(p.get('text', '')) if k == 'notice' else None)
        box = ctrl.scan('0026080160015000001KP320')
        assert (box.id, box.grade, box.lot, box.weight_kg, box.bags) == ('26080160-001', 'KP320', '26080160', 15.0, 3)
        assert '생산순서 1' in box.note and '0026080160015000001KP320' in box.note
        g = ctrl.inventory.grade('KP320')
        assert g is not None and g.iso_class == '-' and g.active
        assert any('KP320' in n for n in notes)
        small = ctrl.scan('0020080030002000017KU10')
        assert small.weight_kg == 2.0 and small.bags == 1
        assert ctrl.inventory.grade('KU10').color != g.color                 # 자동 색은 서로 다르게
        ctrl._last_scan = None                                               # 2초 중복 방지를 건너뛰고
        with pytest.raises(InventoryError, match='이미 등록된'):
            ctrl.scan('0026080160015000001KP320')                            # 같은 박스(일회성 ID) 다시 → 거절
        assert len([x for x in ctrl.inventory.grades() if x.code == 'KP320']) == 1
        assert ctrl.inventory.check_invariants() == []
    finally:
        ctrl.shutdown()


def test_label_digits_without_grade_ask_to_rescan():
    with pytest.raises(InventoryError, match='재종이 읽히지 않았습니다') as e:
        parse_scan('0026080160015000001', set())
    assert not isinstance(e.value, LotOnlyScan)
    with pytest.raises(LotOnlyScan):                      # 다른 길이의 숫자는 지금처럼 로트
        parse_scan('0001015661578', set())


def test_master_event_text_for_auto_grade(tmp_path):
    ctrl = Controller(tmp_path, AppConfig())
    try:
        ctrl.scan('0026080160015000001KP320')
        ev = [e for e in ctrl.inventory.events(kind='MASTER') if e.grade == 'KP320'][0]
        assert ev.detail == '재종 추가: KP320 (ISO 미분류, 완분)'
    finally:
        ctrl.shutdown()
