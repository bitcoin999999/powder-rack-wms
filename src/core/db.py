"""재고 SQLite 저장소 (core 내부 전용).

- 자동 트랜잭션을 끄고(isolation_level=None) BEGIN IMMEDIATE / COMMIT / ROLLBACK 을 직접 쓴다.
- journal_mode=WAL, foreign_keys=ON.
- meta 표: schema_version, policy, columns, tiers, 순번 카운터(arrived_seq, stored_seq, box_seq_day/box_seq, lot_seq:…).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

# 박스 ID 는 대소문자 무시로 찾는다 ('bx-7' = 'BX-7'). 이 색인이 있어야 COLLATE NOCASE 비교가 전체 검색이 되지 않는다.
_BOX_ID_NOCASE_INDEX = "CREATE INDEX IF NOT EXISTS ix_boxes_id_nocase ON boxes(id COLLATE NOCASE)"

_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE grades (
        code        TEXT PRIMARY KEY,
        iso_class   TEXT NOT NULL,
        iso_range   TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        color       TEXT NOT NULL,
        active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        sort_order  INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE boxes (
        id          TEXT PRIMARY KEY,
        grade       TEXT NOT NULL REFERENCES grades(code),
        lot         TEXT NOT NULL,
        weight_kg   REAL NOT NULL,
        bags        INTEGER NOT NULL,
        state       TEXT NOT NULL CHECK (state IN ('CONVEYOR', 'ON_AMR', 'STORED', 'OUT')),
        slot_id     TEXT,
        order_id    TEXT,
        robot       TEXT,
        arrived_seq INTEGER NOT NULL,
        arrived_at  TEXT NOT NULL,
        stored_at   TEXT,
        stored_ts   REAL,
        stored_seq  INTEGER,
        out_at      TEXT,
        needs_check INTEGER NOT NULL DEFAULT 0 CHECK (needs_check IN (0, 1)),
        note        TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX ix_boxes_state ON boxes(state, arrived_seq)",
    "CREATE INDEX ix_boxes_grade ON boxes(grade, state)",
    _BOX_ID_NOCASE_INDEX,
    # box_id / reserved_for 는 UNIQUE — 한 박스가 두 칸에 걸리는 것을 DB 차원에서도 막는다 (NULL 은 여러 개 허용).
    """CREATE TABLE slots (
        id             TEXT PRIMARY KEY,
        face           TEXT NOT NULL CHECK (face IN ('N', 'S')),
        col            INTEGER NOT NULL,
        tier           INTEGER NOT NULL,
        state          TEXT NOT NULL CHECK (state IN ('EMPTY', 'RESERVED', 'OCCUPIED', 'BLOCKED')),
        box_id         TEXT UNIQUE REFERENCES boxes(id),
        reserved_for   TEXT UNIQUE REFERENCES boxes(id),
        blocked_reason TEXT NOT NULL DEFAULT '',
        updated_at     TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL,
        kind     TEXT NOT NULL,
        box_id   TEXT,
        slot_id  TEXT,
        grade    TEXT,
        robot    TEXT,
        order_id TEXT,
        detail   TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX ix_events_kind ON events(kind)",
    "CREATE INDEX ix_events_box ON events(box_id)",
    "CREATE INDEX ix_events_slot ON events(slot_id)",
    "CREATE INDEX ix_events_grade ON events(grade)",
)


class Database:
    """sqlite3 연결 하나 + 명시적 트랜잭션."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        if str(path) != ':memory:':
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn: sqlite3.Connection | None = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA journal_mode=WAL')
            # FULL: 커밋이 끝나면 정전이 나도 기록이 남는다 (NORMAL 은 WAL 에서 마지막 커밋을 잃을 수 있음).
            # 이 설비의 쓰기량(하루 수백 건)에서는 속도 차이가 없다.
            self.conn.execute('PRAGMA synchronous=FULL')
            self.conn.execute('PRAGMA foreign_keys=ON')
        except sqlite3.Error:
            self.conn.close()
            self.conn = None
            raise

    def close(self) -> None:
        if self.conn is not None:
            if self.conn.in_transaction:
                self.conn.execute('ROLLBACK')
            self.conn.close()
            self.conn = None

    @property
    def c(self) -> sqlite3.Connection:
        if self.conn is None:
            raise sqlite3.ProgrammingError('DB 가 닫혔습니다.')
        return self.conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE … COMMIT. 블록 안에서 예외가 나면(또는 COMMIT 이 실패하면) ROLLBACK 후 다시 던진다."""
        conn = self.c
        if conn.in_transaction:
            raise RuntimeError('트랜잭션을 중첩할 수 없습니다.')
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise

    # ---- 스키마
    def has_schema(self) -> bool:
        row = self.c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
        return row is not None

    def create_schema(self) -> None:
        """tx() 안에서 호출. executescript 는 암묵 COMMIT 을 하므로 쓰지 않는다."""
        for stmt in _SCHEMA:
            self.c.execute(stmt)
        self.meta_set('schema_version', SCHEMA_VERSION)

    def ensure_indexes(self) -> None:
        """이전에 만든 DB 에 나중에 추가한 색인을 만든다 (없을 때만 쓰기)."""
        row = self.c.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'ix_boxes_id_nocase'").fetchone()
        if row is None:
            with self.tx() as conn:
                conn.execute(_BOX_ID_NOCASE_INDEX)

    # ---- meta
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.c.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row[0] if row else default

    def meta_int(self, key: str, default: int = 0) -> int:
        val = self.meta_get(key)
        try:
            return int(val) if val is not None else default
        except ValueError:
            return default

    def meta_set(self, key: str, value: object) -> None:
        self.c.execute('INSERT INTO meta(key, value) VALUES (?, ?) '
                       'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))

    def next_seq(self, key: str) -> int:
        """카운터 +1 후 새 값 (tx() 안에서)."""
        val = self.meta_int(key, 0) + 1
        self.meta_set(key, val)
        return val
