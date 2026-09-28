"""SQLite 存储层。

所有行情/公司行为/停牌记录都以“版本追加”方式保存：
每条记录携带 received_at（系统收到时间）与 superseded_at（被后到版本
取代的时间）。as-of 查询时只挑选当时已知、且未被取代的版本。

数据集保存为一个自包含的 .mrdb 文件（即 SQLite 数据库），
另附 .manifest.json 记录数据集身份与内容哈希，重开时校验，
保证“保存后重开，查询结果和来源一致”。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 证券身份主数据：身份行本身也是版本化的（拆股不改身份，代码改名走别名）
CREATE TABLE IF NOT EXISTS instruments (
    figi        TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    name        TEXT NOT NULL,
    currency    TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    lot_size    INTEGER NOT NULL DEFAULT 100,
    first_date  TEXT,
    last_date   TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instrument_aliases (
    figi       TEXT NOT NULL REFERENCES instruments(figi),
    alias      TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    PRIMARY KEY (figi, alias, valid_from)
);

-- 交易日历按交易所维护
CREATE TABLE IF NOT EXISTS trading_calendar (
    exchange  TEXT NOT NULL,
    cal_date  TEXT NOT NULL,
    is_open   INTEGER NOT NULL,
    note      TEXT,
    PRIMARY KEY (exchange, cal_date)
);

-- 双时态行情：business date + received_at 两个时间轴
CREATE TABLE IF NOT EXISTS quote_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    figi             TEXT NOT NULL REFERENCES instruments(figi),
    trade_date       TEXT NOT NULL,
    open             REAL,
    high             REAL,
    low              REAL,
    close            REAL,
    volume           REAL,
    source           TEXT NOT NULL,
    source_record_id TEXT,
    received_at      TEXT NOT NULL,
    superseded_at    TEXT,
    remark           TEXT
);

CREATE INDEX IF NOT EXISTS idx_qv_lookup
    ON quote_versions (figi, trade_date, received_at);
CREATE INDEX IF NOT EXISTS idx_qv_received
    ON quote_versions (received_at);

-- 公司行为（拆股/送股），同样版本化：factor 为“每股拆成多少股”
CREATE TABLE IF NOT EXISTS corporate_action_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    figi             TEXT NOT NULL REFERENCES instruments(figi),
    action_type      TEXT NOT NULL,
    ex_date          TEXT NOT NULL,
    factor           REAL NOT NULL,
    source           TEXT NOT NULL,
    source_record_id TEXT,
    received_at      TEXT NOT NULL,
    superseded_at    TEXT,
    remark           TEXT
);

CREATE INDEX IF NOT EXISTS idx_ca_lookup
    ON corporate_action_versions (figi, ex_date, received_at);

-- 停牌/复牌版本区间
CREATE TABLE IF NOT EXISTS halt_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    figi             TEXT NOT NULL REFERENCES instruments(figi),
    halt_date        TEXT NOT NULL,
    status           TEXT NOT NULL,   -- HALTED / RESUMED
    source           TEXT NOT NULL,
    source_record_id TEXT,
    received_at      TEXT NOT NULL,
    superseded_at    TEXT,
    remark           TEXT
);

CREATE INDEX IF NOT EXISTS idx_halt_lookup
    ON halt_versions (figi, halt_date, received_at);

-- 数据摄入批次（来源审计）
CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id    TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_sha TEXT NOT NULL,
    n_rows      INTEGER NOT NULL,
    note        TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
