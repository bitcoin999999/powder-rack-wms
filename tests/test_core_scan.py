"""core.inventory.parse_scan — 바코드/RFID 리더 입력 해석."""
from __future__ import annotations

import pytest

from core.grades import SAMPLE_GRADES
from core.inventory import InventoryError, ScanResult, parse_scan

KNOWN = {g.code for g in SAMPLE_GRADES}


def scan(text: str) -> tuple:
    r = parse_scan(text, KNOWN)
    assert isinstance(r, ScanResult)
    return r.grade, r.lot, r.weight_kg, r.box_id


def test_grade_only():
    assert scan('PC9030') == ('PC9030', None, None, None)
    assert scan('  NC3215\r\n') == ('NC3215', None, None, None)      # 리더의 Enter·공백


def test_numeric_site_lot():
    assert scan('PC9030 0001015661578') == ('PC9030', '0001015661578', None, None)      # 현장 숫자 로트
    assert scan('PC9030,0001015661578,14.2') == ('PC9030', '0001015661578', 14.2, None)
    assert scan('BOX:W261021-0001;NC3215;000101566157;15') == ('NC3215', '000101566157', 15.0, 'W261021-0001')
    assert scan('PC9030 14.2') == ('PC9030', None, 14.2, None)                        # 짧은 숫자는 중량
    assert scan('PC9030 15KG') == ('PC9030', None, 15.0, None)


def test_grade_and_lot():
    assert scan('PC9030 L2610-PC9030-0012') == ('PC9030', 'L2610-PC9030-0012', None, None)


def test_grade_lot_weight():
    assert scan('PC9030,L2610-0012,14.2') == ('PC9030', 'L2610-0012', 14.2, None)


def test_box_id_grade_lot_weight():
    assert scan('BOX:W261020-0007;PC9030;L2610-PC9030-0003;15') == ('PC9030', 'L2610-PC9030-0003', 15.0, 'W261020-0007')


def test_box_id_with_grade_only_and_with_lot_only():
    assert scan('BOX:W261020-0007;PC9030') == ('PC9030', None, None, 'W261020-0007')
    assert scan('BOX:W261020-0007 PC9030 L1') == ('PC9030', 'L1', None, 'W261020-0007')


@pytest.mark.parametrize('text', [
    'PC9030|L2610-0012|14.2',
    'PC9030;L2610-0012;14.2',
    'PC9030 L2610-0012 14.2',
    'PC9030\tL2610-0012\t14.2',
    'PC9030 , L2610-0012 ;  14.2',
    'pc9030,l2610-0012,14.2',
    'PC9030,L2610-0012,14.2kg',
    'PC9030,L2610-0012,14.2KG',
])
def test_all_separators_and_case(text):
    assert scan(text) == ('PC9030', 'L2610-0012', 14.2, None)


def test_case_insensitive_box_prefix_and_space_after_colon():
    assert scan('box:w261020-0007;pc9030;l2610-0001;15') == ('PC9030', 'L2610-0001', 15.0, 'W261020-0007')
    assert scan('BOX: W261020-0007 PC9030') == ('PC9030', None, None, 'W261020-0007')


def test_single_number_after_grade_is_weight():
    assert scan('PC9030 12.5') == ('PC9030', None, 12.5, None)


def test_returns_canonical_code_from_known_set():
    r = parse_scan('h01', {'H01', 'H05'})
    assert r.grade == 'H01'


def test_unknown_grade_message():
    with pytest.raises(InventoryError, match='등록되지 않은 재종: XX123'):
        parse_scan('XX123', KNOWN)
    with pytest.raises(InventoryError, match='등록되지 않은 재종'):
        parse_scan('PC9030', set())


@pytest.mark.parametrize('text', [
    '', '   ', '\r\n', ',,,;;;', '@@@', '한글재종', 'BOX:', 'BOX:W1', 'BOX: ', 'BOX:@@;PC9030', 'BOX:W1:X;PC9030',
    'PC9030 L1 15 extra', 'PC9030,L1,abc', 'PC9030,L1,0', 'PC9030,L1,-3', 'PC9030,L1,1e3', 'PC9030,L1,250',
    'PC9030 @@@', 'PC9030 L1 15 kg', 'L2610-PC9030-0012 PC9030', 'PC9030 0', 'PC9030 999',
    '\x00\x01', 'PC9030,,,,L1,15,16', 'BOX:W1;BOX:W2;PC9030',
])
def test_garbage_input_raises_inventory_error(text):
    with pytest.raises(InventoryError):
        parse_scan(text, KNOWN)


def test_non_string_input():
    for bad in (None, 123, b'PC9030'):
        with pytest.raises(InventoryError):
            parse_scan(bad, KNOWN)  # type: ignore[arg-type]


def test_error_messages_are_korean():
    for text in ('', 'BOX:', 'PC9030,L1,abc'):
        with pytest.raises(InventoryError) as ei:
            parse_scan(text, KNOWN)
        assert any('가' <= ch <= '힣' for ch in str(ei.value))
