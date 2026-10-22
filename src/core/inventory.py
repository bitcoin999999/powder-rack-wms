"""재고(적재대·컨베이어·박스) 서비스 — WMS 핵심. 계약: 공개 시그니처 유지.

모든 변경 메서드는 SQLite 한 트랜잭션 안에서 상태를 바꾸고 Event 를 1건 이상 기록한 뒤,
구독자(subscribe)에게 `callback(kind: str, payload: dict)` 로 알린다 (payload 예: {'slot': 'N-03-2', 'box': 'W…'}).
잘못된 요청은 InventoryError(또는 하위 클래스)를 던지고 아무것도 바꾸지 않는다.
시각은 clock() (기본 time.time) 기준, 저장은 'YYYY-MM-DD HH:MM:SS' 로컬 시각 문자열.

구현 메모
- DB 가 원본이다. 메모리에 두는 것은 배치(RackLayout)·배정기(SlotAllocator)·구독자뿐이며, 커밋이 끝난 뒤에만 바꾼다.
- payload 키: 'box', 'slot', 'grade' (해당 없으면 None), 'slots' (상태가 바뀐 칸 목록 — 화면 부분 갱신용),
  'detail' (사람이 읽는 한 줄). 변경 1회당 알림 1회.
- 출고(OUT)된 박스는 칸과의 연결(slot_id)을 지운다. 어느 칸에서 나갔는지는 이벤트에 남는다.
- 같은 초에 적재된 박스도 순서가 정해지도록 stored_seq(단조 증가)를 함께 저장한다.
"""
from __future__ import annotations

import csv
import logging
import math
import re
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator

from .config import AppConfig
from .db import SCHEMA_VERSION, Database
from .grades import SAMPLE_GRADES
from .models import (ISO_CLASSES, Box, BoxState, Event, Grade, InventorySnapshot, Slot, SlotState, StockRow)
from .rack import POLICIES, RackLayout, SlotAllocator, parse_slot_id

log = logging.getLogger(__name__)

EVENT_LABELS = {
    'ARRIVE': '도착', 'RESERVE': '예약', 'UNRESERVE': '예약 해제', 'PICK': 'AMR 집음', 'STORE': '적재', 'OUT': '출고',
    'MOVE': '이동', 'CORRECT': '재고 수정', 'BLOCK': '사용중지', 'UNBLOCK': '사용중지 해제', 'ORDER': '작업',
    'ERROR': '오류·확인 필요', 'MASTER': '기준정보', 'CONFIG': '설정',
}
SLOT_STATE_LABEL = {'EMPTY': '빈 칸', 'RESERVED': '예약됨', 'OCCUPIED': '적재됨', 'BLOCKED': '사용중지'}
BOX_STATE_LABEL = {'CONVEYOR': '컨베이어', 'ON_AMR': 'AMR 적재', 'STORED': '적재대 보관', 'OUT': '출고됨'}

MAX_WEIGHT_KG = 100.0
MAX_LOT_LEN = 60                                          # 로트 최대 글자 수 (스캔·직접 입력 공통)
# 현장 로트 = 숫자 일련번호 (예: 0001015661578). 자동 발번(샘플·시뮬레이션)도 같은 형식으로 이어서 붙인다.
LOT_WIDTH = 13                                            # 자동 로트 자릿수 (앞을 0으로 채움)
LOT_FIRST = 1015661578                                    # 자동 로트 첫 번호 → '0001015661578'
LOT_MIN_DIGITS = 6                                        # 재종 뒤 숫자만 있는 토큰이 이 자리 이상이면 중량이 아니라 로트
_TS_FMT = '%Y-%m-%d %H:%M:%S'
_FREE_ID_RE = re.compile(r'^[^\s,;|]{1,40}$')            # 직접 입력 박스 ID: 공백·구분자 없이 40자 이내
_SCAN_SEP_RE = re.compile(r'[\s,;|]+')
_SCAN_TOKEN_RE = re.compile(r'^[A-Z0-9][A-Z0-9_.\-/]*$')
_SCAN_WEIGHT_RE = re.compile(r'^(\d+(?:\.\d+)?)(?:KG)?$')
_COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')
_GRADE_CODE_RE = re.compile(r'^[A-Z0-9][A-Z0-9_.\-]{0,23}$')
_CSV_FORMULA_START = ('=', '+', '-', '@', '\t', '\r')     # 엑셀이 수식으로 읽는 첫 글자
_ACTIVE_STATES = (BoxState.CONVEYOR.value, BoxState.ON_AMR.value, BoxState.STORED.value)


class InventoryError(Exception):
    """사용자에게 그대로 보여 줄 한국어 메시지."""


class ConveyorFull(InventoryError):
    pass


class RackFull(InventoryError):
    pass


class LotOnlyScan(InventoryError):
    """바코드에 재종 없이 숫자 로트만 있다 — 화면이 로트 칸을 채우고 사람이 재종을 고르게 한다."""

    def __init__(self, lot: str):
        super().__init__(f'바코드에 재종이 없습니다 — 로트 {lot} 만 읽혔습니다. 재종을 고르고 [도착 등록]을 누르세요.')
        self.lot = lot


# 완분 라벨 바코드 (MFC 출고 라벨, 현장 사진 3종으로 확인): 앞 2자리 + 로트 8 + 중량(g) 6 + 생산순서 3 + 재종
#   0026080160015000001KP320 → 로트 26080160 · 15,000 g · 생산순서 1 · KP320
#   0026080140015000010A30   → 로트 26080140 · 15,000 g · 생산순서 10 · A30
#   0020080030002000017KU10  → 로트 20080030 ·  2,000 g · 생산순서 17 · KU10
LABEL_RE = re.compile(r'^(\d{2})(\d{8})(\d{6})(\d{3})([A-Z][A-Z0-9_.\-]{0,23})$', re.ASCII)   # 전각 숫자 등은 아님
LABEL_DIGITS = 19                                       # 재종 앞 숫자 자리수 (00 + 로트 8 + 중량 6 + 생산순서 3)
_AIM_ID_RE = re.compile(r'^\][A-Za-z][0-9A-Za-z]')     # 리더가 앞에 붙이는 바코드 종류 표시 (예: ]C1)
_ASCII_DIGITS_RE = re.compile(r'^[0-9]+$')
_LABEL_LIKE_RE = re.compile(r'^([0-9]{15,})[A-Z]')     # 숫자 15자리 이상 + 영문 = 라벨을 찍었는데 자리수가 틀림


class ScanResult:
    """스캔 문자열 해석 결과. label=True 면 완분 라벨 바코드 (seq = 생산순서, new_grade = 재종표에 없는 재종)."""

    def __init__(self, grade: str, lot: str | None = None, weight_kg: float | None = None, box_id: str | None = None):
        self.grade, self.lot, self.weight_kg, self.box_id = grade, lot, weight_kg, box_id
        self.label = False
        self.seq: int | None = None
        self.new_grade = False
        self.raw = ''

    def __repr__(self) -> str:
        return f'ScanResult(grade={self.grade!r}, lot={self.lot!r}, weight_kg={self.weight_kg!r}, box_id={self.box_id!r})'


# ---------------------------------------------------------------- 입력 검사 도우미
def _check_weight(value: object) -> float:
    if isinstance(value, bool):
        raise InventoryError(f'중량이 올바르지 않습니다: {value!r}')
    try:
        w = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise InventoryError(f'중량이 올바르지 않습니다: {value!r}') from None
    if not math.isfinite(w) or w <= 0 or w > MAX_WEIGHT_KG:
        raise InventoryError(f'중량이 올바르지 않습니다: {value!r} (0 초과 {MAX_WEIGHT_KG:g} kg 이하)')
    return round(w, 3)


def _check_bags(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 99:
        raise InventoryError(f'봉 수가 올바르지 않습니다: {value!r}')
    return value


def _clean_box_id(box_id: object) -> str:
    """직접 입력 박스 ID → 저장 표기. 스캔(parse_scan)과 같게 대문자로 맞춘다 — 'bx-7' 과 'BX-7' 은 같은 박스."""
    if not isinstance(box_id, str) or not box_id.strip():
        raise InventoryError('박스 ID가 비어 있습니다.')
    bid = box_id.strip().upper()
    if not _FREE_ID_RE.match(bid):
        raise InventoryError(f'박스 ID 형식이 올바르지 않습니다: {box_id!r}')
    return bid


def _clean_text(value: object, what: str, limit: int = 200) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise InventoryError(f'{what}이(가) 올바르지 않습니다: {value!r}')
    text = ' '.join(value.split())   # 줄바꿈·탭 정리
    return text[:limit]


def _clean_lot(lot: str) -> str:
    # 로트는 추적 키라서 길면 자르지 않고 거부한다 (스캔 경로와 같은 한도)
    text = _clean_text(lot, '로트', limit=10_000)
    if not text:
        raise InventoryError('로트가 비어 있습니다.')
    if len(text) > MAX_LOT_LEN:
        raise InventoryError(f'로트가 너무 깁니다: {len(text)}자 ({MAX_LOT_LEN}자 이내)')
    return text


def _csv_safe(value: object) -> object:
    """엑셀이 수식으로 실행하지 않도록 '=', '+', '-', '@' 로 시작하는 글자 칸 앞에 ' 를 붙인다."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_START):
        return "'" + value
    return value


def _norm_grade_code(code: object) -> str:
    if not isinstance(code, str) or not code.strip():
        raise InventoryError('재종 코드가 비어 있습니다.')
    return code.strip().upper()


def _sv(x: object) -> str:
    """Enum → 값 문자열."""
    return x.value if hasattr(x, 'value') else str(x)  # type: ignore[union-attr]


# ---------------------------------------------------------------- 스캔
def _strip_reader_noise(text: str) -> str:
    """리더 잡음 정리: 제어문자(탭·CR·LF·GS…)는 빈칸으로 바꾸고 앞뒤 빈칸과 앞의 리더 표시(]C1 등)를 뗀다."""
    t = ''.join(ch if ch.isprintable() else ' ' for ch in text).strip()
    return _AIM_ID_RE.sub('', t, count=1).strip()


def _label_candidate(text: str) -> tuple[str, list[str]]:
    """라벨 후보 (붙인 글자, 그 뒤에 남은 덩어리). 빈칸은 숫자 덩어리 사이에만 허용한다
    ('00 26080160 015000 001 KP320' — 사람이 띄어 친 경우). 첫 숫자 아닌 덩어리(재종으로 끝남)까지가 라벨이다."""
    tokens = _strip_reader_noise(text).upper().split()
    if not tokens:
        return '', []
    k = next((i for i, t in enumerate(tokens) if not _ASCII_DIGITS_RE.match(t)), len(tokens) - 1)
    return ''.join(tokens[:k + 1]), tokens[k + 1:]


def parse_label(text: str, known_grades: set[str]) -> ScanResult | None:
    """완분 라벨 바코드면 해석, 아니면 None. 박스 ID = '로트-생산순서' (예: 26080160-001).
    재종표에 없는 재종이어도 거절하지 않고 new_grade=True 로 돌려준다 (Controller 가 자동 추가).

    재종 뒤에 다른 글자가 붙으면(탭·GS 뒤 다른 칸, 라벨 두 장) 재종 코드에 이어 붙이지 않고 InventoryError —
    'KP32020261020' 같은 엉터리 재종이 재종표에 자동 추가되지 않게. 중량 0·100 kg 초과도 InventoryError."""
    if not isinstance(text, str):
        return None
    compact, rest = _label_candidate(text)
    m = LABEL_RE.match(compact)
    if not m:
        return None
    if rest:
        raise InventoryError(f'라벨 바코드 뒤에 다른 글자가 붙어 있습니다: {" ".join(rest)} — '
                             f'라벨 바코드 하나만 다시 찍어 주세요 (라벨 {compact})')
    _prefix, lot, grams, seq, grade = m.groups()
    if int(grams) <= 0:
        raise InventoryError(f'바코드의 중량이 0 입니다: {compact}')
    weight = _check_weight(int(grams) / 1000.0)
    lookup = {g.strip().upper(): g for g in known_grades}
    res = ScanResult(lookup.get(grade, grade), lot, weight, f'{lot}-{seq}')
    res.label, res.seq, res.new_grade, res.raw = True, int(seq), grade not in lookup, compact
    return res


def parse_scan(text: str, known_grades: set[str]) -> ScanResult:
    """바코드/RFID 리더 입력 해석. 허용 형식 (공백·쉼표·세미콜론·| 구분, 대소문자 무시):
      'PC9030'                         → 재종만 (로트 자동)
      'PC9030 0001015661578'           → 재종 + 로트 (현장 숫자 로트)
      'PC9030 L2610-PC9030-0012'       → 재종 + 로트
      'PC9030,L2610-0012,14.2'         → 재종 + 로트 + 중량(kg)
      'BOX:W261020-0007;PC9030;L…;15'  → 박스 ID 지정 + 재종 + 로트 + 중량
      '0026080160015000001KP320'       → 완분 라벨 바코드 (parse_label: 로트·중량·생산순서·재종)
    재종이 known_grades 에 없으면 InventoryError('등록되지 않은 재종: …').

    보충 규칙: 재종 뒤 토큰이 하나뿐일 때 숫자만 LOT_MIN_DIGITS(6)자리 이상이면 로트, 그보다 짧은 숫자(끝에 KG 허용)면
    중량으로 본다 ('PC9030 0001015661578' → 로트, 'PC9030 14.2' → 중량).
    'BOX: W…' 처럼 콜론 뒤를 띄어 써도 된다. 결과의 로트·박스 ID 는 대문자.
    리더가 앞에 붙이는 바코드 종류 표시(]C1 등)는 모든 형식에서 뗀다. 숫자 15자리 이상 + 영문인데 라벨 형식(19자리)이
    아니면 '라벨 바코드의 숫자 자리수가 맞지 않습니다' InventoryError."""
    if not isinstance(text, str) or not text.strip():
        raise InventoryError('스캔 내용이 비어 있습니다.')
    label = parse_label(text, known_grades)
    if label is not None:
        return label
    lookup = {g.strip().upper(): g for g in known_grades}
    cand, _rest = _label_candidate(text)
    lead = _LABEL_LIKE_RE.match(cand)
    if lead and cand not in lookup:      # 라벨을 찍었는데 자리수가 틀림 → '등록되지 않은 재종: 0026…' 대신
        raise InventoryError(f'라벨 바코드의 숫자 자리수가 맞지 않습니다: {len(lead.group(1))}자리 '
                             f'({LABEL_DIGITS}자리여야 함: 00 + 로트 8 + 중량 g 6 + 생산순서 3, 그 뒤 재종) — '
                             f'다시 찍어 주세요. 읽힌 값 {cand}')
    raw = _AIM_ID_RE.sub('', text.strip(), count=1).strip()     # 리더 표시(]C1)는 라벨과 같이 뗀다
    if not raw:
        raise InventoryError('스캔 내용이 비어 있습니다.')
    tokens = [t for t in _SCAN_SEP_RE.split(raw.upper()) if t]
    box_id: str | None = None
    if tokens and tokens[0].startswith('BOX:'):
        rest = tokens.pop(0)[4:]
        if not rest:
            if not tokens:
                raise InventoryError('스캔에 박스 ID가 없습니다.')
            rest = tokens.pop(0)
        if not _SCAN_TOKEN_RE.match(rest) or len(rest) > 40:
            raise InventoryError(f'박스 ID 형식이 올바르지 않습니다: {rest}')
        box_id = rest
    if not tokens:
        raise InventoryError('스캔에 재종이 없습니다.')
    grade_tok = tokens.pop(0)
    if grade_tok not in lookup and not tokens and box_id is None and grade_tok.isascii() \
            and grade_tok.isdigit() and len(grade_tok) == 19:
        # 라벨 바코드의 숫자 부분(00 + 로트 8 + 중량 6 + 생산순서 3)만 들어옴 — 끝의 재종이 안 읽혔다
        raise InventoryError(f'라벨 바코드 끝의 재종이 읽히지 않았습니다: {grade_tok} — 라벨을 다시 찍어 주세요')
    if grade_tok not in lookup and not tokens and box_id is None \
            and grade_tok.isdigit() and LOT_MIN_DIGITS <= len(grade_tok) <= MAX_LOT_LEN:
        raise LotOnlyScan(grade_tok)          # 숫자 로트만 찍힘 (예: 0001015661578)
    if grade_tok not in lookup:
        raise InventoryError(f'등록되지 않은 재종: {grade_tok}')
    if len(tokens) > 2:
        raise InventoryError(f'스캔 형식을 알 수 없습니다: {raw}')

    def as_lot(tok: str) -> str:
        if not _SCAN_TOKEN_RE.match(tok) or len(tok) > MAX_LOT_LEN:
            raise InventoryError(f'로트 형식이 올바르지 않습니다: {tok}')
        return tok

    def as_weight(tok: str) -> float:
        m = _SCAN_WEIGHT_RE.match(tok)
        if not m:
            raise InventoryError(f'중량이 올바르지 않습니다: {tok}')
        return _check_weight(m.group(1))

    lot: str | None = None
    weight: float | None = None
    if len(tokens) == 2:
        lot, weight = as_lot(tokens[0]), as_weight(tokens[1])
    elif len(tokens) == 1:
        if tokens[0].isdigit() and len(tokens[0]) >= LOT_MIN_DIGITS:
            lot = as_lot(tokens[0])
        elif _SCAN_WEIGHT_RE.match(tokens[0]):
            weight = as_weight(tokens[0])
        else:
            lot = as_lot(tokens[0])
    return ScanResult(lookup[grade_tok], lot, weight, box_id)


# ---------------------------------------------------------------- 변경 1건
class _Change:
    """트랜잭션 하나에서 쓴 이벤트와 알림 내용."""

    def __init__(self, conn: sqlite3.Connection, now: float, kind: str):
        self.conn = conn
        self.now = now
        self.ts = time.strftime(_TS_FMT, time.localtime(now))
        self.kind = kind                      # 구독자에게 보낼 종류
        self.event_ids: list[int] = []
        self.slots: list[str] = []            # 상태가 바뀐 칸
        self.payload: dict = {}
        self.after_commit: list[Callable[[], None]] = []

    def event(self, kind: str, box_id: str | None = None, slot_id: str | None = None, grade: str | None = None,
              robot: str | None = None, order_id: str | None = None, detail: str = '') -> int:
        cur = self.conn.execute(
            'INSERT INTO events(ts, kind, box_id, slot_id, grade, robot, order_id, detail) VALUES (?,?,?,?,?,?,?,?)',
            (self.ts, kind, box_id, slot_id, grade, robot, order_id, detail))
        self.event_ids.append(int(cur.lastrowid))
        return int(cur.lastrowid)

    def touch(self, *sids: str | None) -> None:
        for sid in sids:
            if sid and sid not in self.slots:
                self.slots.append(sid)


# ---------------------------------------------------------------- 서비스
class InventoryService:
    def __init__(self, db_path: Path, config: AppConfig, clock: Callable[[], float] | None = None):
        """DB 파일이 없으면 만들고 SAMPLE_GRADES·슬롯을 넣는다. 있으면 그대로 연다 (설정의 열·단과 DB 슬롯이 다르면
        DB 쪽을 따른다 — 재구성은 reconfigure_rack 로만).
        DB 쪽 열·단·배정 정책은 config(rack.columns/tiers, allocation_policy)에도 반영해 둔다."""
        self._config = config
        self._clock: Callable[[], float] = clock or time.time
        self._subs: list[Callable[[str, dict], None]] = []
        try:
            self._db = Database(Path(db_path))
        except (sqlite3.Error, OSError) as e:   # OSError: data 폴더를 만들 수 없음(권한·같은 이름 파일 등)
            raise InventoryError(f'재고 DB 를 열 수 없습니다: {db_path} ({e})') from e
        try:
            self._open()
        except sqlite3.Error as e:   # 손상된 파일·SQLite 가 아닌 파일 등
            self._db.close()
            raise InventoryError(f'재고 DB 를 열 수 없습니다: {db_path} ({e})') from e
        except BaseException:
            self._db.close()
            raise

    def _open(self) -> None:
        db = self._db
        if not db.has_schema():
            try:
                layout = RackLayout(self._config.rack)
            except ValueError as e:
                raise InventoryError(f'적재대 설정이 올바르지 않습니다: {e}') from e
            policy = self._config.allocation_policy if self._config.allocation_policy in POLICIES else 'grade_zone'
            now = self._clock()
            with db.tx() as conn:
                db.create_schema()
                for i, g in enumerate(SAMPLE_GRADES):
                    conn.execute('INSERT INTO grades(code, iso_class, iso_range, kind, description, color, active, '
                                 'sort_order) VALUES (?,?,?,?,?,?,?,?)',
                                 (g.code.upper(), g.iso_class, g.iso_range, g.kind, g.description, g.color,
                                  int(g.active), i))
                ch = _Change(conn, now, 'CONFIG')
                self._insert_slots(ch, layout)
                db.meta_set('policy', policy)
                db.meta_set('columns', layout.columns)
                db.meta_set('tiers', layout.tiers)
                for key in ('arrived_seq', 'stored_seq', 'box_seq'):
                    db.meta_set(key, 0)
                ch.event('CONFIG', detail=f'재고 DB 생성: 재종 {len(SAMPLE_GRADES)}종, 적재대 {layout.columns}열×'
                                          f'{layout.tiers}단 양면 {layout.capacity}칸')
        else:
            version = db.meta_int('schema_version', 0)
            if version != SCHEMA_VERSION:
                raise InventoryError(f'지원하지 않는 재고 DB 버전입니다: {version} (필요: {SCHEMA_VERSION})')
            db.ensure_indexes()
        columns, tiers = self._db_dimensions()
        try:
            layout = RackLayout(replace(self._config.rack, columns=columns, tiers=tiers))
        except ValueError as e:
            raise InventoryError(f'재고 DB 의 적재대 구성이 올바르지 않습니다: {e}') from e
        policy = db.meta_get('policy', 'grade_zone') or 'grade_zone'
        if policy not in POLICIES:
            policy = 'grade_zone'
        self._apply_layout(layout, policy)

    def _db_dimensions(self) -> tuple[int, int]:
        cols = self._db.meta_int('columns', 0)
        tiers = self._db.meta_int('tiers', 0)
        if cols <= 0 or tiers <= 0:   # meta 가 비었으면 슬롯 표에서 추정
            row = self._db.c.execute('SELECT MAX(col), MAX(tier) FROM slots').fetchone()
            cols, tiers = int(row[0] or self._config.rack.columns), int(row[1] or self._config.rack.tiers)
        return cols, tiers

    def _apply_layout(self, layout: RackLayout, policy: str) -> None:
        """커밋된 DB 상태에 맞춰 메모리 배치·배정기·설정을 맞춘다."""
        self._layout = layout
        self._allocator = SlotAllocator(layout, policy)
        self._config.rack.columns = layout.columns
        self._config.rack.tiers = layout.tiers
        self._config.allocation_policy = policy

    @staticmethod
    def _insert_slots(ch: _Change, layout: RackLayout) -> None:
        rows = []
        for sid in layout.slot_ids():
            face, col, tier = parse_slot_id(sid)
            rows.append((sid, face, col, tier, SlotState.EMPTY.value, ch.ts))
        ch.conn.executemany('INSERT INTO slots(id, face, col, tier, state, updated_at) VALUES (?,?,?,?,?,?)', rows)

    def close(self) -> None:
        self._db.close()

    # ---- 구독
    def subscribe(self, callback: Callable[[str, dict], None]) -> None:
        self._subs.append(callback)

    def unsubscribe(self, callback: Callable[[str, dict], None]) -> None:
        """구독 해제 (없으면 무시)."""
        if callback in self._subs:
            self._subs.remove(callback)

    def _notify(self, kind: str, payload: dict) -> None:
        for cb in list(self._subs):
            try:
                cb(kind, payload)
            except Exception:  # 구독자 오류가 재고 변경을 되돌리거나 다른 구독자를 막지 않게
                log.exception('재고 이벤트 구독자 오류 (%s)', kind)

    @contextmanager
    def _mutation(self, kind: str) -> Iterator[_Change]:
        """변경 1건 = 트랜잭션 1개. 성공(커밋)했을 때만 after_commit 실행 후 구독자에게 한 번 알린다."""
        try:
            with self._db.tx() as conn:
                ch = _Change(conn, self._clock(), kind)
                yield ch
                if not ch.event_ids:
                    raise RuntimeError('이벤트 없이 재고를 바꿀 수 없습니다.')
        except sqlite3.Error as e:
            # 잠김·디스크·파일 손상·제약 위반 모두 사용자 메시지로 (트랜잭션은 이미 되돌려졌다).
            # Controller 는 InventoryError 만 화면 알림으로 돌리므로 날것의 sqlite3 예외가 새면 틱 루프가 죽는다.
            if not isinstance(e, sqlite3.OperationalError):
                log.exception('재고 DB 내부 오류 (%s)', kind)
            raise InventoryError(f'재고 DB 오류: {e}') from e
        for fn in ch.after_commit:
            fn()
        payload = {'box': None, 'slot': None, 'grade': None}
        payload.update(ch.payload)
        payload['slots'] = list(ch.slots)
        payload['events'] = list(ch.event_ids)
        self._notify(ch.kind, payload)

    # ---- 행 → 모델
    @staticmethod
    def _grade_from(row: sqlite3.Row) -> Grade:
        return Grade(row['code'], row['iso_class'], row['iso_range'], row['kind'], row['description'], row['color'],
                     bool(row['active']))

    @staticmethod
    def _slot_from(row: sqlite3.Row) -> Slot:
        return Slot(row['id'], row['face'], row['col'], row['tier'], SlotState(row['state']), row['box_id'],
                    row['reserved_for'], row['blocked_reason'], row['updated_at'])

    @staticmethod
    def _box_from(row: sqlite3.Row) -> Box:
        return Box(id=row['id'], grade=row['grade'], lot=row['lot'], weight_kg=float(row['weight_kg']),
                   bags=int(row['bags']), state=BoxState(row['state']), slot_id=row['slot_id'],
                   order_id=row['order_id'], robot=row['robot'], arrived_seq=int(row['arrived_seq']),
                   arrived_at=row['arrived_at'], stored_at=row['stored_at'], out_at=row['out_at'],
                   needs_check=bool(row['needs_check']), note=row['note'])

    @staticmethod
    def _event_from(row: sqlite3.Row) -> Event:
        return Event(row['id'], row['ts'], row['kind'], row['box_id'], row['slot_id'], row['grade'], row['robot'],
                     row['order_id'], row['detail'])

    # ---- 내부 조회 (트랜잭션 안팎 공용)
    def _conn(self) -> sqlite3.Connection:
        return self._db.c

    def _canon_sid(self, sid: object) -> str:
        try:
            face, col, tier = parse_slot_id(sid)  # type: ignore[arg-type]
        except ValueError as e:
            raise InventoryError(str(e)) from None
        return f'{face}-{col:02d}-{tier}'

    def _slot_row(self, conn: sqlite3.Connection, sid: object) -> sqlite3.Row:
        canon = self._canon_sid(sid)
        row = conn.execute('SELECT * FROM slots WHERE id = ?', (canon,)).fetchone()
        if row is None:
            raise InventoryError(f'적재대에 없는 칸입니다: {canon}')
        return row

    @staticmethod
    def _find_box_row(conn: sqlite3.Connection, text: str) -> sqlite3.Row | None:
        """박스 ID 로 찾기: 그대로 → 대문자 → 대소문자 무시(예전 DB 에 남은 소문자 ID) 순."""
        for sql, key in (('SELECT * FROM boxes WHERE id = ?', text),
                         ('SELECT * FROM boxes WHERE id = ?', text.upper()),
                         ('SELECT * FROM boxes WHERE id = ? COLLATE NOCASE ORDER BY rowid LIMIT 1', text)):
            row = conn.execute(sql, (key,)).fetchone()
            if row is not None:
                return row
        return None

    @classmethod
    def _box_row(cls, conn: sqlite3.Connection, box_id: object) -> sqlite3.Row:
        if not isinstance(box_id, str) or not box_id.strip():
            raise InventoryError('박스 ID가 비어 있습니다.')
        row = cls._find_box_row(conn, box_id.strip())
        if row is None:
            raise InventoryError(f'없는 박스입니다: {box_id.strip()}')
        return row

    @staticmethod
    def _grade_row(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
        return conn.execute('SELECT * FROM grades WHERE code = ?', (code,)).fetchone()

    @staticmethod
    def _box_exists(conn: sqlite3.Connection, box_id: str) -> bool:
        """대소문자만 다른 ID 도 같은 박스로 본다 (ix_boxes_id_nocase 색인 사용)."""
        return conn.execute('SELECT 1 FROM boxes WHERE id = ? COLLATE NOCASE', (box_id,)).fetchone() is not None

    @staticmethod
    def _conveyor_count(conn: sqlite3.Connection) -> int:
        return int(conn.execute('SELECT COUNT(*) FROM boxes WHERE state = ?', (BoxState.CONVEYOR.value,)).fetchone()[0])

    def _set_slot(self, ch: _Change, sid: str, state: SlotState, box_id: str | None = None,
                  reserved_for: str | None = None, blocked_reason: str = '') -> None:
        ch.conn.execute('UPDATE slots SET state = ?, box_id = ?, reserved_for = ?, blocked_reason = ?, updated_at = ? '
                        'WHERE id = ?', (state.value, box_id, reserved_for, blocked_reason, ch.ts, sid))
        ch.touch(sid)

    def _allocate(self, conn: sqlite3.Connection, grade: str, exclude: set[str] | None) -> str | None:
        slots = [self._slot_from(r) for r in conn.execute('SELECT * FROM slots ORDER BY face, col, tier')]
        marks = ','.join('?' * len(_ACTIVE_STATES))
        boxes = {r['id']: self._box_from(r)
                 for r in conn.execute(f'SELECT * FROM boxes WHERE state IN ({marks})', _ACTIVE_STATES)}
        excl: set[str] = set()
        for sid in exclude or ():
            try:
                excl.add(self._canon_sid(sid))
            except InventoryError:
                continue
        return self._allocator.choose(grade, slots, boxes, excl)

    # ---- 발번
    def _peek_box_id(self, conn: sqlite3.Connection, now: float) -> tuple[str, int, str]:
        day = time.strftime('%y%m%d', time.localtime(now))
        seq = self._db.meta_int('box_seq', 0) if self._db.meta_get('box_seq_day') == day else 0
        while True:   # 직접 입력한 ID 와 겹치면 건너뛴다
            seq += 1
            bid = f'W{day}-{seq:04d}'
            if not self._box_exists(conn, bid):
                return day, seq, bid

    def _take_box_id(self, conn: sqlite3.Connection, now: float) -> str:
        day, seq, bid = self._peek_box_id(conn, now)
        self._db.meta_set('box_seq_day', day)
        self._db.meta_set('box_seq', seq)
        return bid

    def _take_lot(self, now: float, grade: str) -> str:
        """자동 로트: 재종과 관계없이 숫자 일련번호 하나로 이어 붙인다 (현장 로트가 숫자 순서대로 채워지는 방식)."""
        seq = self._db.next_seq('lot_num')
        return str(LOT_FIRST + seq - 1).zfill(LOT_WIDTH)

    def _new_box_id(self, conn: sqlite3.Connection, now: float, box_id: str | None) -> str:
        if box_id is None or (isinstance(box_id, str) and not box_id.strip()):
            return self._take_box_id(conn, now)
        bid = _clean_box_id(box_id)
        if self._box_exists(conn, bid):
            raise InventoryError(f'이미 등록된 박스 ID 입니다: {bid}')
        return bid

    # ================================================================ 조회
    def grades(self, active_only: bool = False) -> list[Grade]:
        sql = 'SELECT * FROM grades' + (' WHERE active = 1' if active_only else '') + ' ORDER BY sort_order, code'
        return [self._grade_from(r) for r in self._conn().execute(sql)]

    def grade(self, code: str) -> Grade | None:
        if not isinstance(code, str) or not code.strip():
            return None
        row = self._grade_row(self._conn(), code.strip().upper())
        return self._grade_from(row) if row else None

    def slots(self) -> list[Slot]:
        """SPEC 순서 (면 N→S, 열, 단)."""
        return [self._slot_from(r) for r in self._conn().execute('SELECT * FROM slots ORDER BY face, col, tier')]

    def slot(self, sid: str) -> Slot:
        """없으면 InventoryError."""
        return self._slot_from(self._slot_row(self._conn(), sid))

    def box(self, box_id: str) -> Box:
        """없으면 InventoryError."""
        return self._box_from(self._box_row(self._conn(), box_id))

    def boxes(self, states: tuple[str, ...] | None = None) -> list[Box]:
        """등록 순서. states 를 주면 그 상태만."""
        if states is None:
            rows = self._conn().execute('SELECT * FROM boxes ORDER BY rowid')
        else:
            vals = tuple(_sv(s) for s in states)
            if not vals:
                return []
            rows = self._conn().execute(f"SELECT * FROM boxes WHERE state IN ({','.join('?' * len(vals))}) "
                                        'ORDER BY rowid', vals)
        return [self._box_from(r) for r in rows]

    def conveyor(self) -> list[Box]:
        """CONVEYOR 박스, 도착 순서."""
        rows = self._conn().execute('SELECT * FROM boxes WHERE state = ? ORDER BY arrived_seq, rowid',
                                    (BoxState.CONVEYOR.value,))
        return [self._box_from(r) for r in rows]

    def find_grade(self, code: str) -> list[StockRow]:
        """그 재종 STORED 박스, FIFO (stored_at, slot id). 같은 초는 적재 순서(stored_seq)로 가른다."""
        if not isinstance(code, str) or not code.strip():
            return []
        now = self._clock()
        rows = self._conn().execute(
            'SELECT b.*, s.id AS s_id, s.face AS s_face, s.col AS s_col, s.tier AS s_tier, s.state AS s_state, '
            's.box_id AS s_box_id, s.reserved_for AS s_reserved_for, s.blocked_reason AS s_blocked_reason, '
            's.updated_at AS s_updated_at '
            'FROM boxes b JOIN slots s ON s.id = b.slot_id '
            'WHERE b.state = ? AND b.grade = ? ORDER BY b.stored_at, b.stored_seq, s.id',
            (BoxState.STORED.value, code.strip().upper()))
        out = []
        for r in rows:
            slot = Slot(r['s_id'], r['s_face'], r['s_col'], r['s_tier'], SlotState(r['s_state']), r['s_box_id'],
                        r['s_reserved_for'], r['s_blocked_reason'], r['s_updated_at'])
            stored_ts = r['stored_ts']
            age = max(0.0, now - float(stored_ts)) if stored_ts is not None else 0.0
            out.append(StockRow(slot=slot, box=self._box_from(r), age_s=age))
        return out

    def snapshot(self) -> InventorySnapshot:
        conn = self._conn()
        by_state = {r[0]: int(r[1]) for r in conn.execute('SELECT state, COUNT(*) FROM slots GROUP BY state')}
        by_grade = {r[0]: int(r[1]) for r in conn.execute(
            'SELECT b.grade, COUNT(*) FROM boxes b LEFT JOIN grades g ON g.code = b.grade '
            'WHERE b.state = ? GROUP BY b.grade ORDER BY MIN(g.sort_order), b.grade', (BoxState.STORED.value,))}
        needs = int(conn.execute('SELECT COUNT(*) FROM boxes WHERE needs_check = 1 AND state != ?',
                                 (BoxState.OUT.value,)).fetchone()[0])
        return InventorySnapshot(capacity=sum(by_state.values()), occupied=by_state.get('OCCUPIED', 0),
                                 reserved=by_state.get('RESERVED', 0), blocked=by_state.get('BLOCKED', 0),
                                 conveyor=self._conveyor_count(conn), by_grade=by_grade, needs_check=needs)

    def events(self, limit: int = 500, kind: str | None = None, grade: str | None = None, box_id: str | None = None,
               slot_id: str | None = None) -> list[Event]:
        """최신순. limit <= 0 이면 전부."""
        where, params = [], []
        if kind:
            where.append('kind = ?')
            params.append(kind.strip().upper())
        if grade:
            where.append('grade = ?')
            params.append(grade.strip().upper())
        if box_id:
            row = self._find_box_row(self._conn(), box_id.strip())
            where.append('box_id = ?')
            params.append(row['id'] if row is not None else box_id.strip())
        if slot_id:
            try:
                sid = self._canon_sid(slot_id)
            except InventoryError:
                sid = slot_id.strip()
            where.append('slot_id = ?')
            params.append(sid)
        sql = 'SELECT * FROM events' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY id DESC'
        if limit and limit > 0:
            sql += ' LIMIT ?'
            params.append(int(limit))
        return [self._event_from(r) for r in self._conn().execute(sql, params)]

    def export_events_csv(self, path: Path) -> int:
        """UTF-8-SIG CSV (오래된 것부터). 쓴 줄 수 (머리글 제외)."""
        rows = self._conn().execute('SELECT * FROM events ORDER BY id').fetchall()
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(['번호', '시각', '종류', '종류명', '박스', '재종', '슬롯', 'AMR', '작업', '내용'])
                for r in rows:
                    w.writerow([_csv_safe(v) for v in (
                        r['id'], r['ts'], r['kind'], EVENT_LABELS.get(r['kind'], r['kind']), r['box_id'] or '',
                        r['grade'] or '', r['slot_id'] or '', r['robot'] or '', r['order_id'] or '', r['detail'])])
        except OSError as e:
            raise InventoryError(f'CSV 저장에 실패했습니다: {path} ({e})') from e
        return len(rows)

    # ================================================================ 입고
    def next_box_id(self) -> str:
        """W{yymmdd}-{seq4}, 그날 순번. 미리보기 — 순번을 소비하지 않는다 (register_arrival 이 소비)."""
        return self._peek_box_id(self._conn(), self._clock())[2]

    def _check_conveyor_room(self, conn: sqlite3.Connection) -> None:
        n, cap = self._conveyor_count(conn), int(self._config.conveyor_capacity)
        if n >= cap:
            raise ConveyorFull(f'컨베이어가 가득 찼습니다 ({n}/{cap}). 적재가 끝날 때까지 기다리세요.')

    def check_arrival(self, box_id: str | None = None) -> None:
        """register_arrival 이 재종과 관계없이 거절할 이유가 있으면 같은 오류를 미리 던진다 (컨베이어 만재 → ConveyorFull,
        박스 ID 형식·중복 → InventoryError). 아무것도 바꾸지 않는다 — 라벨 스캔이 재종을 자동 추가하기 전에 쓴다
        (거절될 스캔이 재종표만 바꿔 놓지 않게)."""
        conn = self._conn()
        self._check_conveyor_room(conn)
        if isinstance(box_id, str) and box_id.strip():
            self._new_box_id(conn, self._clock(), box_id)

    def register_arrival(self, grade: str, lot: str | None = None, weight_kg: float = 15.0, box_id: str | None = None,
                         bags: int = 3, note: str = '') -> Box:
        """컨베이어에 박스 도착. 가득이면 ConveyorFull, 재종 미등록·사용중지면 InventoryError, 박스 ID 중복이면 InventoryError.
        lot 없으면(빈 문자열 포함) 자동(숫자 일련번호 0001015661578, 0001015661579 …). box_id 없거나 빈 문자열이면 next_box_id() 규칙으로 발번."""
        code = _norm_grade_code(grade)
        weight = _check_weight(weight_kg)
        bags = _check_bags(bags)
        note_s = _clean_text(note, '메모')
        with self._mutation('ARRIVE') as ch:
            conn = ch.conn
            g = self._grade_row(conn, code)
            if g is None:
                raise InventoryError(f'등록되지 않은 재종: {code}')
            if not g['active']:
                raise InventoryError(f'사용중지된 재종입니다: {code}')
            self._check_conveyor_room(conn)
            bid = self._new_box_id(conn, ch.now, box_id)
            lot_s = self._take_lot(ch.now, code) if lot is None or (isinstance(lot, str) and not lot.strip()) \
                else _clean_lot(lot)
            seq = self._db.next_seq('arrived_seq')
            conn.execute('INSERT INTO boxes(id, grade, lot, weight_kg, bags, state, arrived_seq, arrived_at, note) '
                         'VALUES (?,?,?,?,?,?,?,?,?)',
                         (bid, code, lot_s, weight, bags, BoxState.CONVEYOR.value, seq, ch.ts, note_s))
            residual = ' (잔량)' if weight < 15.0 else ''
            ch.event('ARRIVE', box_id=bid, grade=code, detail=f'컨베이어 도착 · 로트 {lot_s} · {weight:g} kg{residual}')
            ch.payload = {'box': bid, 'grade': code, 'detail': f'{bid} 도착'}
            result = self._box_from(self._box_row(conn, bid))
        return result

    def reserve_slot(self, box_id: str, slot_id: str | None = None, order_id: str | None = None,
                     exclude: set[str] | None = None) -> Slot:
        """CONVEYOR 박스의 목적지 칸 예약. slot_id 없으면 배정 정책. 빈 칸 없으면 RackFull.
        order_id 가 있으면 box.order_id 에 묶는다."""
        with self._mutation('RESERVE') as ch:
            conn = ch.conn
            b = self._box_row(conn, box_id)
            bid = b['id']
            if b['state'] != BoxState.CONVEYOR.value:
                raise InventoryError(f'컨베이어 위 박스만 예약할 수 있습니다: {bid} ({BOX_STATE_LABEL[b["state"]]})')
            if b['slot_id']:
                raise InventoryError(f'이미 {b["slot_id"]} 칸이 예약된 박스입니다: {bid}')
            if order_id is not None and b['order_id'] and b['order_id'] != order_id:
                raise InventoryError(f'다른 작업({b["order_id"]})에 묶인 박스입니다: {bid}')
            if slot_id is not None:
                s = self._slot_row(conn, slot_id)
                sid = s['id']
                if s['state'] != SlotState.EMPTY.value:
                    raise InventoryError(f'{sid} 칸은 비어 있지 않습니다 ({SLOT_STATE_LABEL[s["state"]]}).')
            else:
                sid = self._allocate(conn, b['grade'], exclude)
                if sid is None:
                    raise RackFull('적재대에 빈 칸이 없습니다.')
            order = order_id if order_id is not None else b['order_id']
            self._set_slot(ch, sid, SlotState.RESERVED, reserved_for=bid)
            conn.execute('UPDATE boxes SET slot_id = ?, order_id = ? WHERE id = ?', (sid, order, bid))
            how = '지정' if slot_id is not None else POLICIES[self._allocator.policy]
            ch.event('RESERVE', box_id=bid, slot_id=sid, grade=b['grade'], order_id=order,
                     detail=f'목적지 {sid} 예약 ({how})')
            ch.payload = {'box': bid, 'slot': sid, 'grade': b['grade'], 'order': order}
            result = self._slot_from(self._slot_row(conn, sid))
        return result

    def release_reservation(self, box_id: str, reason: str = '') -> None:
        """예약 해제 (슬롯 EMPTY, box.slot_id/order_id 비움). 박스가 ON_AMR 이면 InventoryError (확인 필요 흐름 사용)."""
        reason_s = _clean_text(reason, '사유')
        with self._mutation('UNRESERVE') as ch:
            conn = ch.conn
            b = self._box_row(conn, box_id)
            bid = b['id']
            if b['state'] == BoxState.ON_AMR.value:
                raise InventoryError(f'AMR에 실린 박스는 예약을 풀 수 없습니다: {bid} — '
                                     '[적재 완료 처리] 또는 [컨베이어로 되돌림]을 사용하세요.')
            if b['state'] != BoxState.CONVEYOR.value:
                raise InventoryError(f'컨베이어 위 박스가 아닙니다: {bid} ({BOX_STATE_LABEL[b["state"]]})')
            sid = b['slot_id']
            if not sid:
                raise InventoryError(f'예약된 칸이 없는 박스입니다: {bid}')
            self._release_slot_of(ch, bid, sid)
            conn.execute('UPDATE boxes SET slot_id = NULL, order_id = NULL WHERE id = ?', (bid,))
            ch.event('UNRESERVE', box_id=bid, slot_id=sid, grade=b['grade'], order_id=b['order_id'],
                     detail='예약 해제' + (f': {reason_s}' if reason_s else ''))
            ch.payload = {'box': bid, 'slot': sid, 'grade': b['grade']}

    def _release_slot_of(self, ch: _Change, bid: str, sid: str) -> None:
        """sid 가 bid 예약 칸이면 비운다."""
        row = ch.conn.execute('SELECT state, reserved_for FROM slots WHERE id = ?', (sid,)).fetchone()
        if row is not None and row['state'] == SlotState.RESERVED.value and row['reserved_for'] == bid:
            self._set_slot(ch, sid, SlotState.EMPTY)

    def mark_picked(self, box_id: str, robot: str, order_id: str | None = None) -> None:
        """컨베이어 → AMR (CONVEYOR → ON_AMR)."""
        if not isinstance(robot, str) or not robot.strip():
            raise InventoryError('AMR 이름이 비어 있습니다.')
        robot_s = robot.strip()
        with self._mutation('PICK') as ch:
            conn = ch.conn
            b = self._box_row(conn, box_id)
            bid = b['id']
            if b['state'] != BoxState.CONVEYOR.value:
                raise InventoryError(f'컨베이어 위 박스가 아니라 집을 수 없습니다: {bid} ({BOX_STATE_LABEL[b["state"]]})')
            if order_id is not None and b['order_id'] and b['order_id'] != order_id:
                raise InventoryError(f'다른 작업({b["order_id"]})에 묶인 박스입니다: {bid}')
            order = order_id if order_id is not None else b['order_id']
            conn.execute('UPDATE boxes SET state = ?, robot = ?, order_id = ? WHERE id = ?',
                         (BoxState.ON_AMR.value, robot_s, order, bid))
            dest = f' → {b["slot_id"]}' if b['slot_id'] else ''
            ch.event('PICK', box_id=bid, slot_id=b['slot_id'], grade=b['grade'], robot=robot_s, order_id=order,
                     detail=f'{robot_s} 가 컨베이어에서 집음{dest}')
            ch.touch(b['slot_id'])
            ch.payload = {'box': bid, 'slot': b['slot_id'], 'grade': b['grade'], 'robot': robot_s, 'order': order}

    def _store(self, ch: _Change, b: sqlite3.Row, target: str | None, robot: str | None, how: str) -> Slot:
        """박스를 칸에 넣는다 (CONVEYOR/ON_AMR → STORED). 다른 칸 예약이 있었으면 그 예약은 푼다."""
        conn = ch.conn
        bid = b['id']
        if target is not None:
            s = self._slot_row(conn, target)
        elif b['slot_id']:
            s = self._slot_row(conn, b['slot_id'])
        else:
            raise InventoryError(f'적재할 칸이 정해지지 않았습니다: {bid}')
        sid = s['id']
        if s['state'] == SlotState.RESERVED.value and s['reserved_for'] != bid:
            raise InventoryError(f'{sid} 칸은 다른 박스({s["reserved_for"]})의 예약 칸입니다.')
        if s['state'] in (SlotState.OCCUPIED.value, SlotState.BLOCKED.value):
            raise InventoryError(f'{sid} 칸에는 넣을 수 없습니다 ({SLOT_STATE_LABEL[s["state"]]}).')
        old = b['slot_id']
        if old and old != sid:   # 예약과 다른 칸에 넣음 → 원래 예약 해제 (UNIQUE 제약 때문에 먼저)
            self._release_slot_of(ch, bid, old)
            ch.event('UNRESERVE', box_id=bid, slot_id=old, grade=b['grade'], order_id=b['order_id'],
                     detail=f'예약 칸 대신 {sid} 에 적재')
        self._set_slot(ch, sid, SlotState.OCCUPIED, box_id=bid)
        seq = self._db.next_seq('stored_seq')
        conn.execute('UPDATE boxes SET state = ?, slot_id = ?, stored_at = ?, stored_ts = ?, stored_seq = ?, '
                     'robot = NULL, order_id = NULL, needs_check = 0 WHERE id = ?',
                     (BoxState.STORED.value, sid, ch.ts, ch.now, seq, bid))
        robot_s = robot.strip() if isinstance(robot, str) and robot.strip() else b['robot']
        ch.event('STORE', box_id=bid, slot_id=sid, grade=b['grade'], robot=robot_s, order_id=b['order_id'],
                 detail=how + (' (확인 필요 해제)' if b['needs_check'] else ''))
        ch.payload = {'box': bid, 'slot': sid, 'grade': b['grade'], 'robot': robot_s, 'order': b['order_id']}
        return self._slot_from(self._slot_row(conn, sid))

    def confirm_store(self, box_id: str, slot_id: str | None = None, robot: str | None = None) -> Slot:
        """적재 완료. slot_id 없으면 box.slot_id(예약 칸). 예약이 다른 박스 것이거나 OCCUPIED/BLOCKED 면 InventoryError.
        CONVEYOR(수동)·ON_AMR 둘 다 허용. needs_check 해제.
        단, AMR 작업에 묶인 채 아직 컨베이어에 있는 박스는 거부 (AMR 이 집으러 오는 중)."""
        with self._mutation('STORE') as ch:
            b = self._box_row(ch.conn, box_id)
            state = b['state']
            if state not in (BoxState.CONVEYOR.value, BoxState.ON_AMR.value):
                raise InventoryError(f'적재할 수 없는 박스 상태입니다: {b["id"]} ({BOX_STATE_LABEL[state]})')
            if state == BoxState.CONVEYOR.value and b['order_id']:
                raise InventoryError(f'AMR 작업 {b["order_id"]} 에 묶인 박스는 수동으로 적재할 수 없습니다: {b["id"]}')
            how = 'AMR 적재 완료' if state == BoxState.ON_AMR.value else '수동 적재'
            result = self._store(ch, b, slot_id, robot, how)
        return result

    def manual_store(self, box_id: str, slot_id: str | None = None) -> Slot:
        """AMR 없이 사람이 적재: (필요하면 예약 후) confirm_store. 작업에 묶인 박스(order_id)는 거부."""
        with self._mutation('STORE') as ch:
            b = self._box_row(ch.conn, box_id)
            bid = b['id']
            if b['state'] == BoxState.ON_AMR.value:
                raise InventoryError(f'AMR에 실린 박스입니다: {bid} — [적재 완료 처리]를 사용하세요.')
            if b['state'] != BoxState.CONVEYOR.value:
                raise InventoryError(f'컨베이어 위 박스가 아닙니다: {bid} ({BOX_STATE_LABEL[b["state"]]})')
            if b['order_id']:
                raise InventoryError(f'AMR 작업 {b["order_id"]} 에 묶인 박스는 수동으로 적재할 수 없습니다: {bid}')
            target = slot_id
            if target is None and not b['slot_id']:
                target = self._allocate(ch.conn, b['grade'], None)
                if target is None:
                    raise RackFull('적재대에 빈 칸이 없습니다.')
            result = self._store(ch, b, target, None, '수동 적재')
        return result

    def flag_needs_check(self, box_id: str, reason: str) -> None:
        """작업 중단 등으로 실제 위치 확인 필요 표시 (컨베이어·AMR 위 박스만). 예약은 그대로 둔다."""
        reason_s = _clean_text(reason, '사유') or '사유 없음'
        with self._mutation('ERROR') as ch:
            b = self._box_row(ch.conn, box_id)
            bid = b['id']
            if b['state'] not in (BoxState.CONVEYOR.value, BoxState.ON_AMR.value):
                raise InventoryError(f'컨베이어·AMR 위 박스만 확인 필요로 표시할 수 있습니다: {bid} '
                                     f'({BOX_STATE_LABEL[b["state"]]})')
            ch.conn.execute('UPDATE boxes SET needs_check = 1 WHERE id = ?', (bid,))
            ch.event('ERROR', box_id=bid, slot_id=b['slot_id'], grade=b['grade'], robot=b['robot'],
                     order_id=b['order_id'], detail=f'확인 필요: {reason_s}')
            ch.touch(b['slot_id'])
            ch.payload = {'box': bid, 'slot': b['slot_id'], 'grade': b['grade'], 'detail': reason_s}

    def return_to_conveyor(self, box_id: str, reason: str = '') -> None:
        """ON_AMR(확인 필요) 박스를 컨베이어 맨 앞으로 되돌림. 예약 해제. 컨베이어가 가득이면 ConveyorFull."""
        reason_s = _clean_text(reason, '사유')
        with self._mutation('CORRECT') as ch:
            conn = ch.conn
            b = self._box_row(conn, box_id)
            bid = b['id']
            if b['state'] != BoxState.ON_AMR.value:
                raise InventoryError(f'AMR에 실린 박스만 컨베이어로 되돌릴 수 있습니다: {bid} ({BOX_STATE_LABEL[b["state"]]})')
            if not b['needs_check']:
                raise InventoryError(f'확인 필요로 표시된 박스만 되돌릴 수 있습니다 (작업 진행 중): {bid}')
            n, cap = self._conveyor_count(conn), int(self._config.conveyor_capacity)
            if n >= cap:
                raise ConveyorFull(f'컨베이어가 가득 차서 되돌릴 수 없습니다 ({n}/{cap}).')
            # 맨 앞 = 지금 컨베이어 박스 중 가장 작은 순번보다 작게
            row = conn.execute('SELECT MIN(arrived_seq) FROM boxes WHERE state = ?', (BoxState.CONVEYOR.value,)).fetchone()
            front = int(row[0]) - 1 if row[0] is not None else int(b['arrived_seq'])
            sid = b['slot_id']
            if sid:
                self._release_slot_of(ch, bid, sid)
                ch.event('UNRESERVE', box_id=bid, slot_id=sid, grade=b['grade'], robot=b['robot'],
                         order_id=b['order_id'], detail='컨베이어로 되돌려 예약 해제')
            conn.execute('UPDATE boxes SET state = ?, slot_id = NULL, order_id = NULL, robot = NULL, needs_check = 0, '
                         'arrived_seq = ? WHERE id = ?', (BoxState.CONVEYOR.value, front, bid))
            ch.event('CORRECT', box_id=bid, slot_id=sid, grade=b['grade'], robot=b['robot'], order_id=b['order_id'],
                     detail='컨베이어 맨 앞으로 되돌림' + (f': {reason_s}' if reason_s else ''))
            ch.payload = {'box': bid, 'slot': sid, 'grade': b['grade']}

    def cancel_arrival(self, box_id: str, reason: str) -> Box:
        """도착 취소 (잘못 스캔했거나 두 번 읽힌 컨베이어 박스): CONVEYOR → OUT, 이벤트 CORRECT. 사유 필수.
        AMR 작업에 묶인 박스(order_id)는 거부 — 작업을 먼저 취소한다. 작업 없이 예약만 된 칸은 함께 푼다.
        박스 기록(ID·이력)은 남는다 (같은 박스 ID 로 다시 등록할 수는 없다)."""
        reason_s = _clean_text(reason, '사유')
        if not reason_s:
            raise InventoryError('도착 취소 사유를 입력하세요.')
        with self._mutation('CORRECT') as ch:
            conn = ch.conn
            b = self._box_row(conn, box_id)
            bid = b['id']
            if b['state'] != BoxState.CONVEYOR.value:
                raise InventoryError(f'컨베이어 위 박스만 도착 취소할 수 있습니다: {bid} ({BOX_STATE_LABEL[b["state"]]})')
            if b['order_id']:
                raise InventoryError(f'AMR 작업 {b["order_id"]} 에 묶인 박스는 도착 취소할 수 없습니다: {bid} '
                                     '— 작업을 먼저 취소하세요.')
            sid = b['slot_id']
            if sid:
                self._release_slot_of(ch, bid, sid)
                ch.event('UNRESERVE', box_id=bid, slot_id=sid, grade=b['grade'], detail='도착 취소로 예약 해제')
            conn.execute('UPDATE boxes SET state = ?, slot_id = NULL, out_at = ?, robot = NULL, order_id = NULL, '
                         'needs_check = 0 WHERE id = ?', (BoxState.OUT.value, ch.ts, bid))
            detail = f'도착 취소: {reason_s}'
            ch.event('CORRECT', box_id=bid, slot_id=sid, grade=b['grade'], detail=detail)
            ch.payload = {'box': bid, 'slot': sid, 'grade': b['grade'], 'detail': detail}
            result = self._box_from(self._box_row(conn, bid))
        return result

    # ================================================================ 출고·수정
    def _take_out(self, ch: _Change, slot_id: str, kind: str, detail: str) -> Box:
        s = self._slot_row(ch.conn, slot_id)
        sid = s['id']
        if s['state'] != SlotState.OCCUPIED.value:
            raise InventoryError(f'{sid} 칸에 박스가 없습니다 ({SLOT_STATE_LABEL[s["state"]]}).')
        b = self._box_row(ch.conn, s['box_id'])
        self._set_slot(ch, sid, SlotState.EMPTY)
        ch.conn.execute('UPDATE boxes SET state = ?, slot_id = NULL, out_at = ?, robot = NULL, order_id = NULL, '
                        'needs_check = 0 WHERE id = ?', (BoxState.OUT.value, ch.ts, b['id']))
        ch.event(kind, box_id=b['id'], slot_id=sid, grade=b['grade'], detail=detail)
        ch.payload = {'box': b['id'], 'slot': sid, 'grade': b['grade'], 'detail': detail}
        return self._box_from(self._box_row(ch.conn, b['id']))

    def retrieve(self, slot_id: str, reason: str = '수동 출고') -> Box:
        """OCCUPIED → EMPTY, 박스 OUT."""
        reason_s = _clean_text(reason, '사유') or '수동 출고'
        with self._mutation('OUT') as ch:
            result = self._take_out(ch, slot_id, 'OUT', reason_s)
        return result

    def move(self, box_id: str, to_slot: str, reason: str = '') -> None:
        """STORED 박스를 빈 칸으로 이동 (실사 수정). 이벤트는 CORRECT, 출발·도착 칸에 한 건씩."""
        reason_s = _clean_text(reason, '사유')
        with self._mutation('CORRECT') as ch:
            conn = ch.conn
            b = self._box_row(conn, box_id)
            bid = b['id']
            if b['state'] != BoxState.STORED.value:
                raise InventoryError(f'적재대에 있는 박스만 옮길 수 있습니다: {bid} ({BOX_STATE_LABEL[b["state"]]})')
            dest = self._slot_row(conn, to_slot)
            to_sid, from_sid = dest['id'], b['slot_id']
            if to_sid == from_sid:
                raise InventoryError(f'같은 칸으로는 옮길 수 없습니다: {to_sid}')
            if dest['state'] != SlotState.EMPTY.value:
                raise InventoryError(f'{to_sid} 칸은 비어 있지 않습니다 ({SLOT_STATE_LABEL[dest["state"]]}).')
            self._set_slot(ch, from_sid, SlotState.EMPTY)          # UNIQUE(box_id) 때문에 먼저 비운다
            self._set_slot(ch, to_sid, SlotState.OCCUPIED, box_id=bid)
            conn.execute('UPDATE boxes SET slot_id = ? WHERE id = ?', (to_sid, bid))
            detail = f'이동 {from_sid} → {to_sid}' + (f': {reason_s}' if reason_s else '')
            ch.event('CORRECT', box_id=bid, slot_id=from_sid, grade=b['grade'], detail=detail)
            ch.event('CORRECT', box_id=bid, slot_id=to_sid, grade=b['grade'], detail=detail)
            ch.payload = {'box': bid, 'slot': to_sid, 'from_slot': from_sid, 'grade': b['grade'], 'detail': detail}

    def put_directly(self, slot_id: str, grade: str, lot: str, weight_kg: float = 15.0, box_id: str | None = None,
                     reason: str = '실사 등록') -> Box:
        """빈 칸에 박스를 직접 등록 (컨베이어 거치지 않음). 실물 기준이라 사용중지 재종도 허용 (미등록 재종은 거부).
        lot 이 비어 있으면 자동 발번."""
        code = _norm_grade_code(grade)
        weight = _check_weight(weight_kg)
        reason_s = _clean_text(reason, '사유') or '실사 등록'
        with self._mutation('CORRECT') as ch:
            conn = ch.conn
            s = self._slot_row(conn, slot_id)
            sid = s['id']
            if s['state'] != SlotState.EMPTY.value:
                raise InventoryError(f'{sid} 칸은 비어 있지 않습니다 ({SLOT_STATE_LABEL[s["state"]]}).')
            if self._grade_row(conn, code) is None:
                raise InventoryError(f'등록되지 않은 재종: {code}')
            bid = self._new_box_id(conn, ch.now, box_id)
            lot_s = self._take_lot(ch.now, code) if lot is None or (isinstance(lot, str) and not lot.strip()) \
                else _clean_lot(lot)
            seq = self._db.next_seq('stored_seq')
            # 컨베이어를 거치지 않았으므로 arrived_seq = 0
            conn.execute('INSERT INTO boxes(id, grade, lot, weight_kg, bags, state, slot_id, arrived_seq, arrived_at, '
                         'stored_at, stored_ts, stored_seq, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (bid, code, lot_s, weight, 3, BoxState.STORED.value, sid, 0, ch.ts, ch.ts, ch.now, seq,
                          reason_s))
            self._set_slot(ch, sid, SlotState.OCCUPIED, box_id=bid)
            detail = f'직접 입력: {reason_s} · 로트 {lot_s} · {weight:g} kg'
            ch.event('CORRECT', box_id=bid, slot_id=sid, grade=code, detail=detail)
            ch.payload = {'box': bid, 'slot': sid, 'grade': code, 'detail': detail}
            result = self._box_from(self._box_row(conn, bid))
        return result

    def clear_slot(self, slot_id: str, reason: str) -> None:
        """칸 비우기 (박스는 OUT, 사유 기록). 예약 칸은 AMR 이 오는 중이라 거부."""
        reason_s = _clean_text(reason, '사유') or '사유 없음'
        with self._mutation('CORRECT') as ch:
            self._take_out(ch, slot_id, 'CORRECT', f'비우기: {reason_s}')

    def block_slot(self, slot_id: str, reason: str) -> None:
        """EMPTY 칸만."""
        reason_s = _clean_text(reason, '사유', 100) or '사용중지'
        with self._mutation('BLOCK') as ch:
            s = self._slot_row(ch.conn, slot_id)
            sid = s['id']
            if s['state'] != SlotState.EMPTY.value:
                raise InventoryError(f'빈 칸만 사용중지할 수 있습니다: {sid} ({SLOT_STATE_LABEL[s["state"]]})')
            self._set_slot(ch, sid, SlotState.BLOCKED, blocked_reason=reason_s)
            ch.event('BLOCK', slot_id=sid, detail=f'사용중지: {reason_s}')
            ch.payload = {'slot': sid, 'detail': reason_s}

    def unblock_slot(self, slot_id: str) -> None:
        with self._mutation('UNBLOCK') as ch:
            s = self._slot_row(ch.conn, slot_id)
            sid = s['id']
            if s['state'] != SlotState.BLOCKED.value:
                raise InventoryError(f'사용중지된 칸이 아닙니다: {sid} ({SLOT_STATE_LABEL[s["state"]]})')
            self._set_slot(ch, sid, SlotState.EMPTY)
            ch.event('UNBLOCK', slot_id=sid, detail=f'사용중지 해제 (이전 사유: {s["blocked_reason"]})')
            ch.payload = {'slot': sid}

    # ================================================================ 기준정보·설정
    def upsert_grade(self, grade: Grade) -> None:
        """재종 추가 또는 수정 (코드는 대문자로 저장)."""
        if not isinstance(grade, Grade):
            raise InventoryError('재종 정보가 올바르지 않습니다.')
        code = _norm_grade_code(grade.code)
        if not _GRADE_CODE_RE.match(code):
            raise InventoryError(f'재종 코드 형식이 올바르지 않습니다: {grade.code!r} (영문·숫자, 24자 이내)')
        iso = (grade.iso_class or '').strip().upper()
        if iso not in ISO_CLASSES:
            raise InventoryError(f'ISO 분류는 {"/".join(ISO_CLASSES)} 중 하나여야 합니다: {grade.iso_class!r}')
        color = (grade.color or '').strip()
        if not _COLOR_RE.match(color):
            raise InventoryError(f'색은 #RRGGBB 형식이어야 합니다: {grade.color!r}')
        fields = (iso, _clean_text(grade.iso_range, 'ISO 범위', 40), _clean_text(grade.kind, '종류', 40),
                  _clean_text(grade.description, '설명'), color, int(bool(grade.active)))
        with self._mutation('MASTER') as ch:
            conn = ch.conn
            exists = self._grade_row(conn, code) is not None
            if exists:
                conn.execute('UPDATE grades SET iso_class = ?, iso_range = ?, kind = ?, description = ?, color = ?, '
                             'active = ? WHERE code = ?', fields + (code,))
            else:
                order = int(conn.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 FROM grades').fetchone()[0])
                conn.execute('INSERT INTO grades(code, iso_class, iso_range, kind, description, color, active, '
                             'sort_order) VALUES (?,?,?,?,?,?,?,?)', (code,) + fields + (order,))
            what = '재종 수정' if exists else '재종 추가'
            parts = ['ISO 미분류' if iso == '-' else iso] + [x for x in (fields[1], fields[2]) if x]
            ch.event('MASTER', grade=code, detail=f'{what}: {code} ({", ".join(parts)})'
                                                 + ('' if grade.active else ' · 사용중지'))
            ch.payload = {'grade': code, 'detail': what}

    def set_grade_active(self, code: str, active: bool) -> None:
        c = _norm_grade_code(code)
        with self._mutation('MASTER') as ch:
            if self._grade_row(ch.conn, c) is None:
                raise InventoryError(f'등록되지 않은 재종: {c}')
            ch.conn.execute('UPDATE grades SET active = ? WHERE code = ?', (int(bool(active)), c))
            what = '사용' if active else '사용중지'
            ch.event('MASTER', grade=c, detail=f'재종 {what}: {c}')
            ch.payload = {'grade': c, 'detail': what}

    def backup(self, dest: Path) -> None:
        """재고 DB 를 dest 파일로 통째 복사 (SQLite backup — 쓰는 중이어도 일관된 사본)."""
        dest = Path(dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            out = sqlite3.connect(str(dest))
            try:
                self._db.c.backup(out)
            finally:
                out.close()
        except (sqlite3.Error, OSError) as e:
            raise InventoryError(f'재고 DB 백업 실패: {dest} ({e})') from e

    def reset_stock(self, reason: str = '처음부터 다시') -> None:
        """시연용 초기화: 박스·이력을 지우고 모든 칸을 빈 칸으로, 박스·로트 번호를 처음으로.
        재종 기준정보·적재대 구성·배정 정책은 그대로 둔다. 먼저 backup() 할 것."""
        with self._mutation('CONFIG') as ch:
            conn = ch.conn
            n = int(conn.execute('SELECT COUNT(*) FROM boxes').fetchone()[0])
            conn.execute('DELETE FROM slots')          # 칸이 박스를 참조하므로 먼저
            conn.execute('DELETE FROM boxes')
            conn.execute('DELETE FROM events')
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'events'")
            conn.execute("DELETE FROM meta WHERE key LIKE 'lot_seq:%' OR key IN ('lot_num', 'box_seq_day')")
            for key in ('arrived_seq', 'stored_seq', 'box_seq'):
                self._db.meta_set(key, 0)
            self._insert_slots(ch, self._layout)
            ch.event('CONFIG', detail=f'{reason}: 박스 {n}개·이력 정리, 적재대 {self._layout.capacity}칸 비움 '
                                      f'(재종 기준정보·설정 유지)')
            ch.slots = list(self._layout.slot_ids())
            ch.payload = {'detail': reason, 'reconfigured': True}

    def reconfigure_rack(self, columns: int, tiers: int) -> None:
        """적재·예약·사용중지 칸이 하나도 없을 때만. 아니면 InventoryError.
        슬롯 표를 새로 만들고 배치(layout)·설정(config.rack)을 바꾼다."""
        try:
            new_layout = RackLayout(replace(self._config.rack, columns=columns, tiers=tiers))
        except (ValueError, TypeError) as e:
            raise InventoryError(f'적재대 구성이 올바르지 않습니다: {e}') from e
        with self._mutation('CONFIG') as ch:
            conn = ch.conn
            busy = {r[0]: int(r[1]) for r in conn.execute(
                "SELECT state, COUNT(*) FROM slots WHERE state != 'EMPTY' GROUP BY state")}
            if busy:
                parts = ', '.join(f'{SLOT_STATE_LABEL[k]} {v}' for k, v in sorted(busy.items()))
                raise InventoryError(f'적재대가 비어 있을 때만 구성을 바꿀 수 있습니다 ({parts}).')
            linked = int(conn.execute('SELECT COUNT(*) FROM boxes WHERE state IN (?, ?) AND slot_id IS NOT NULL',
                                      (BoxState.CONVEYOR.value, BoxState.ON_AMR.value)).fetchone()[0])
            if linked:
                raise InventoryError(f'목적지 칸이 정해진 박스가 {linked}개 있어 구성을 바꿀 수 없습니다.')
            old = self._layout
            conn.execute('DELETE FROM slots')
            self._insert_slots(ch, new_layout)
            self._db.meta_set('columns', new_layout.columns)
            self._db.meta_set('tiers', new_layout.tiers)
            ch.event('CONFIG', detail=f'적재대 구성 변경: {old.columns}열×{old.tiers}단 → '
                                      f'{new_layout.columns}열×{new_layout.tiers}단 ({new_layout.capacity}칸)')
            ch.slots = list(new_layout.slot_ids())
            ch.payload = {'detail': '적재대 구성 변경', 'reconfigured': True}
            policy = self._allocator.policy
            ch.after_commit.append(lambda: self._apply_layout(new_layout, policy))

    def set_policy(self, policy: str) -> None:
        if policy not in POLICIES:
            raise InventoryError(f'알 수 없는 배정 정책입니다: {policy!r} ({", ".join(POLICIES)})')
        with self._mutation('CONFIG') as ch:
            self._db.meta_set('policy', policy)
            ch.event('CONFIG', detail=f'배정 정책: {POLICIES[policy]} ({policy})')
            ch.payload = {'detail': '배정 정책 변경', 'policy': policy}
            ch.after_commit.append(lambda: self._apply_layout(self._layout, policy))

    @property
    def policy(self) -> str:
        """현재 배정 정책 이름."""
        return self._allocator.policy

    @property
    def layout(self):
        """현재 RackLayout."""
        return self._layout

    # ================================================================ 자가진단
    def check_invariants(self) -> list[str]:
        """SPEC §4 불변식 위반 목록 (빈 목록 = 정상). 테스트·자가진단용."""
        conn = self._conn()
        problems: list[str] = []
        slots = {r['id']: r for r in conn.execute('SELECT * FROM slots')}
        boxes = {r['id']: r for r in conn.execute('SELECT rowid AS rid, * FROM boxes')}
        grades = {r[0] for r in conn.execute('SELECT code FROM grades')}
        slot_states = {s.value for s in SlotState}
        box_states = {s.value for s in BoxState}

        # 0) 슬롯 표 = 현재 배치
        expected = set(self._layout.slot_ids())
        for sid in sorted(expected - slots.keys()):
            problems.append(f'칸 {sid} 이(가) DB 에 없습니다.')
        for sid in sorted(slots.keys() - expected):
            problems.append(f'배치에 없는 칸 {sid} 이(가) DB 에 있습니다.')
        for sid, s in slots.items():
            try:
                parts = parse_slot_id(sid)
            except ValueError:
                problems.append(f'칸 ID 형식 오류: {sid}')
                continue
            if parts != (s['face'], s['col'], s['tier']):
                problems.append(f'칸 {sid} 의 면·열·단 값이 ID 와 다릅니다.')

        # 1) 칸 쪽: OCCUPIED ⇔ box_id, RESERVED ⇔ reserved_for, 상대 박스 상태·연결
        links: Counter[str] = Counter()
        for sid, s in slots.items():
            st, bid, rid = s['state'], s['box_id'], s['reserved_for']
            if st not in slot_states:
                problems.append(f'칸 {sid}: 알 수 없는 상태 {st}')
            if (st == SlotState.OCCUPIED.value) != (bid is not None):
                problems.append(f'칸 {sid}: 상태 {st} 인데 박스 연결이 {"있음" if bid else "없음"}')
            if (st == SlotState.RESERVED.value) != (rid is not None):
                problems.append(f'칸 {sid}: 상태 {st} 인데 예약 박스가 {"있음" if rid else "없음"}')
            if bid is not None:
                links[bid] += 1
                b = boxes.get(bid)
                if b is None:
                    problems.append(f'칸 {sid}: 없는 박스 {bid} 가 들어 있다고 되어 있습니다.')
                elif b['state'] != BoxState.STORED.value:
                    problems.append(f'칸 {sid}: 든 박스 {bid} 의 상태가 STORED 가 아닙니다 ({b["state"]}).')
                elif b['slot_id'] != sid:
                    problems.append(f'칸 {sid}: 든 박스 {bid} 의 칸이 {b["slot_id"]} 로 되어 있습니다.')
            if rid is not None:
                links[rid] += 1
                b = boxes.get(rid)
                if b is None:
                    problems.append(f'칸 {sid}: 없는 박스 {rid} 에 예약되어 있습니다.')
                elif b['state'] not in (BoxState.CONVEYOR.value, BoxState.ON_AMR.value):
                    problems.append(f'칸 {sid}: 예약 박스 {rid} 가 컨베이어·AMR 위가 아닙니다 ({b["state"]}).')
                elif b['slot_id'] != sid:
                    problems.append(f'칸 {sid}: 예약 박스 {rid} 의 목적지가 {b["slot_id"]} 로 되어 있습니다.')

        # 2) 박스 쪽
        for bid, b in boxes.items():
            st, sid = b['state'], b['slot_id']
            if st not in box_states:
                problems.append(f'박스 {bid}: 알 수 없는 상태 {st}')
            if b['grade'] not in grades:
                problems.append(f'박스 {bid}: 등록되지 않은 재종 {b["grade"]}')
            if links[bid] > 1:
                problems.append(f'박스 {bid} 가 칸 {links[bid]}곳에 동시에 연결되어 있습니다.')
            s = slots.get(sid) if sid else None
            if st == BoxState.STORED.value:
                if s is None:
                    problems.append(f'박스 {bid}: 보관 상태인데 칸이 없습니다 ({sid}).')
                elif s['state'] != SlotState.OCCUPIED.value or s['box_id'] != bid:
                    problems.append(f'박스 {bid}: 칸 {sid} 에 들어 있지 않습니다 (칸 상태 {s["state"]}).')
                if not b['stored_at'] or b['stored_seq'] is None:
                    problems.append(f'박스 {bid}: 보관 상태인데 적재 시각이 없습니다.')
            elif st in (BoxState.CONVEYOR.value, BoxState.ON_AMR.value):
                if sid:
                    if s is None:
                        problems.append(f'박스 {bid}: 목적지 칸 {sid} 이(가) 없습니다.')
                    elif s['state'] != SlotState.RESERVED.value or s['reserved_for'] != bid:
                        problems.append(f'박스 {bid}: 목적지 {sid} 가 이 박스 예약이 아닙니다 (칸 상태 {s["state"]}).')
                elif links[bid]:
                    problems.append(f'박스 {bid}: 목적지가 없는데 칸에 연결되어 있습니다.')
            elif st == BoxState.OUT.value:
                if sid or links[bid]:
                    problems.append(f'박스 {bid}: 출고됐는데 칸에 연결되어 있습니다.')
            if st == BoxState.ON_AMR.value and not b['robot']:
                problems.append(f'박스 {bid}: AMR 위인데 AMR 이름이 없습니다.')
            if st != BoxState.ON_AMR.value and b['robot']:
                problems.append(f'박스 {bid}: AMR 위가 아닌데 AMR({b["robot"]}) 이 기록되어 있습니다.')
            # 보관·출고된 박스는 AMR 작업·확인 필요 표시가 남으면 안 된다 (남으면 화면이 계속 경고·수동 처리 거부)
            if st in (BoxState.STORED.value, BoxState.OUT.value):
                if b['order_id']:
                    problems.append(f'박스 {bid}: {st} 상태인데 작업({b["order_id"]})에 묶여 있습니다.')
                if b['needs_check']:
                    problems.append(f'박스 {bid}: {st} 상태인데 확인 필요 표시가 남아 있습니다.')
            if st == BoxState.OUT.value and not b['out_at']:
                problems.append(f'박스 {bid}: 출고 상태인데 출고 시각이 없습니다.')

        # 3) 컨베이어 = CONVEYOR 박스, 도착 순번 오름차순 (순번 중복 없음)
        conv = [b for b in boxes.values() if b['state'] == BoxState.CONVEYOR.value]
        seq_count = Counter(int(b['arrived_seq']) for b in conv)
        for seq, n in seq_count.items():
            if n > 1:
                problems.append(f'컨베이어 도착 순번 {seq} 이(가) {n}개 박스에 겹칩니다.')
        listed = self.conveyor()
        if {b.id for b in listed} != {b['id'] for b in conv}:
            problems.append('컨베이어 목록이 CONVEYOR 박스와 다릅니다.')
        seqs = [b.arrived_seq for b in listed]
        if seqs != sorted(seqs):
            problems.append('컨베이어 목록이 도착 순서가 아닙니다.')

        # 4) 모든 변경은 이벤트로 남는다 — 박스마다 이벤트가 1건 이상, 순번 카운터는 쓰인 값 이상
        with_events = {r[0] for r in conn.execute('SELECT DISTINCT box_id FROM events WHERE box_id IS NOT NULL')}
        for bid in boxes:
            if bid not in with_events:
                problems.append(f'박스 {bid}: 이벤트 기록이 없습니다.')
        max_arr = max((int(b['arrived_seq']) for b in boxes.values()), default=0)
        if self._db.meta_int('arrived_seq', 0) < max_arr:
            problems.append('도착 순번 카운터가 박스 순번보다 작습니다.')
        stored_seqs = [int(b['stored_seq']) for b in boxes.values()
                       if b['state'] == BoxState.STORED.value and b['stored_seq'] is not None]
        if len(stored_seqs) != len(set(stored_seqs)):
            problems.append('적재 순번(stored_seq)이 겹치는 박스가 있습니다.')
        if self._db.meta_int('stored_seq', 0) < max(stored_seqs, default=0):
            problems.append('적재 순번 카운터가 박스 순번보다 작습니다.')
        return problems
