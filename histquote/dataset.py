"""数据集：SQLite 持久化 + 双时态合并查询。

事件（quote_event）永不就地更新；同一 (证券, 业务日) 下，
as-of 查询只取 received_at <= known_at 的最后一条有效事件。
重复记录（同业务时间、同收到时间、同来源记录号）被视为同一事实，只存一次。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .calendar import TradingCalendar
from .identity import Security, Split
from .query import QuoteView
from .quote import Quote, QuoteEvent, QuoteStatus

SCHEMA_VERSION = 1


def _parse_dt(value: str | _dt.datetime) -> _dt.datetime:
    return value if isinstance(value, _dt.datetime) else _dt.datetime.fromisoformat(value)


def _quote_to_tuple(q: Quote | None) -> tuple:
    if q is None:
        return (None, None, None, None, None)
    return (q.open, q.high, q.low, q.close, q.volume)


_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS security(
    security_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_session(session_date TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS corp_split(
    security_id TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    ratio REAL NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (security_id, ex_date, received_at)
);
CREATE TABLE IF NOT EXISTS quote_event(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id TEXT NOT NULL,
    business_date TEXT NOT NULL,
    received_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    source_record TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(security_id, business_date, source, source_record)
);
CREATE INDEX IF NOT EXISTS idx_event_asof
    ON quote_event(security_id, business_date, received_at, seq);
"""


class Dataset:
    def __init__(self, path: str | os.PathLike = ":memory:", name: str = "research") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_DDL)
        self._init_meta(name)
        self.calendar = TradingCalendar(self.conn)

    # ---------- 基础信息 ----------

    def _init_meta(self, name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('name', ?)", (name,)
        )
        self.conn.commit()

    @property
    def name(self) -> str:
        return self.conn.execute("SELECT value FROM meta WHERE key='name'").fetchone()[0]

    def add_security(self, security: Security) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO security(security_id, code, name) VALUES (?,?,?)",
            (security.security_id, security.code, security.name),
        )
        self.conn.commit()

    def securities(self) -> list[Security]:
        rows = self.conn.execute(
            "SELECT security_id, code, name FROM security ORDER BY security_id"
        )
        return [Security(*r) for r in rows]

    def add_split(self, split: Split) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO corp_split(security_id, ex_date, ratio, received_at)"
            " VALUES (?,?,?,?)",
            (
                split.security_id,
                split.ex_date.isoformat(),
                split.ratio,
                split.received_at.isoformat(),
            ),
        )
        self.conn.commit()

    # ---------- 行情写入 ----------

    def add_event(self, event: QuoteEvent) -> tuple[bool, int | None]:
        """写入一条事件。返回 (是否为新记录, seq)。

        同一来源记录号重放时返回 False（幂等）；来源记录号相同但内容不同
        （真冲突）抛出异常，绝不静默覆盖。
        """
        h = self._content_hash(event)
        existing = self.conn.execute(
            "SELECT seq, content_hash FROM quote_event"
            " WHERE security_id=? AND business_date=? AND source=? AND source_record=?",
            (
                event.security_id,
                event.business_date.isoformat(),
                event.source,
                event.source_record,
            ),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != h:
                raise ValueError(
                    f"来源记录 {event.source}/{event.source_record} 内容冲突，"
                    f"({event.security_id}, {event.business_date}) 已存在不同数据"
                )
            return False, existing["seq"]
        o, hi, lo, c, v = _quote_to_tuple(event.quote)
        cur = self.conn.execute(
            "INSERT INTO quote_event(security_id, business_date, received_at, kind,"
            " open, high, low, close, volume, reason, source, source_record, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.security_id,
                event.business_date.isoformat(),
                event.received_at.isoformat(),
                event.kind,
                o, hi, lo, c, v,
                event.reason,
                event.source,
                event.source_record,
                h,
            ),
        )
        self.conn.commit()
        return True, cur.lastrowid

    def add_events(self, events) -> int:
        n = 0
        for e in events:
            inserted, _ = self.add_event(e)
            n += int(inserted)
        return n

    @staticmethod
    def _content_hash(event: QuoteEvent) -> str:
        payload = json.dumps(
            {
                "kind": event.kind,
                "quote": dataclasses_as_dict(event.quote),
                "reason": event.reason,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---------- 双时态查询 ----------

    def _visible_event(
        self, security_id: str, business_date: str, known_at: _dt.datetime
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM quote_event"
            " WHERE security_id=? AND business_date=? AND received_at <= ?"
            " ORDER BY received_at DESC, seq DESC LIMIT 1",
            (security_id, business_date, known_at.isoformat()),
        ).fetchone()

    def _adjustment(
        self, security_id: str, business_date: _dt.date, known_at: _dt.datetime
    ) -> float:
        """截至 known_at 已知、且除权日晚于 business_date 的拆分因子之积。"""
        factor = 1.0
        rows = self.conn.execute(
            "SELECT ratio FROM corp_split"
            " WHERE security_id=? AND ex_date > ? AND received_at <= ?",
            (security_id, business_date.isoformat(), known_at.isoformat()),
        )
        for r in rows:
            factor *= r[0]
        return factor

    def quote_at(
        self,
        security_id: str,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
    ) -> QuoteView:
        """as-of 查询：在 known_at 时点，回看 business_date 当天可知的行情。"""
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        row = self._visible_event(security_id, bd.isoformat(), ka)

        if row is None:
            if self.calendar.is_session(bd):
                status = QuoteStatus.MISSING
            else:
                status = QuoteStatus.NON_SESSION
            return QuoteView(
                security_id, bd, ka, status, None, None, "", "", None, "none"
            )

        kind = row["kind"]
        if kind == "cancel":
            status = QuoteStatus.CANCELLED
            quote = None
        elif kind == "suspension":
            status = QuoteStatus.SUSPENDED
            quote = None
        else:
            status = QuoteStatus.OK
            quote = Quote(
                row["open"], row["high"], row["low"], row["close"], row["volume"]
            )

        adjusted = None
        if quote is not None and quote.close is not None:
            factor = self._adjustment(security_id, bd, ka)
            adjusted = round(quote.close / factor, 6)

        return QuoteView(
            security_id=security_id,
            business_date=bd,
            known_at=ka,
            status=status,
            quote=quote,
            adjusted_close=adjusted,
            source=row["source"],
            source_record=row["source_record"],
            received_at=_parse_dt(row["received_at"]),
            event_kind=kind,
        )

    def series(
        self,
        security_id: str,
        start: str | _dt.date,
        end: str | _dt.date,
        known_at: str | _dt.datetime,
        only_sessions: bool = True,
    ) -> list[QuoteView]:
        """按业务时间（交易日）回放；停牌、缺失都会显式体现。"""
        ka = _parse_dt(known_at)
        dates = self.calendar.sessions(start, end) if only_sessions else _enumerate_dates(start, end)
        return [self.quote_at(security_id, d, ka) for d in dates]

    def received_log(
        self,
        security_id: str,
        start: str | _dt.datetime,
        end: str | _dt.datetime,
    ) -> list[dict]:
        """按收到时间追踪：该时间窗内实际到达的所有事件（含修订与撤销）。"""
        rows = self.conn.execute(
            "SELECT business_date, received_at, kind, close, reason, source, source_record"
            " FROM quote_event WHERE security_id=? AND received_at >= ? AND received_at <= ?"
            " ORDER BY received_at, seq",
            (security_id, str(start), str(end)),
        )
        return [dict(r) for r in rows]

    # ---------- 持久化 ----------

    def close(self) -> None:
        self.conn.close()

    def save_as(self, path: str | os.PathLike) -> None:
        """把内存/当前数据库完整复制到 path（离线本地文件）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(str(target))
        try:
            self.conn.backup(backup)
        finally:
            backup.close()

    def __enter__(self) -> "Dataset":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextlib.contextmanager
    @staticmethod
    def open(path: str | os.PathLike):
        ds = Dataset(path)
        try:
            yield ds
        finally:
            ds.close()

    def snapshot_info(self) -> dict:
        """数据集指纹，保存后重开应保持一致。"""
        counts = {}
        for table in ("security", "trading_session", "corp_split", "quote_event"):
            counts[table] = self.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        hashes = (
            r[0] for r in self.conn.execute(
                "SELECT content_hash FROM quote_event ORDER BY seq"
            )
        )
        digest = hashlib.sha256("".join(hashes).encode("utf-8")).hexdigest()
        return {"name": self.name, "schema_version": SCHEMA_VERSION, "counts": counts,
                "event_digest": digest}


def dataclasses_as_dict(obj) -> dict | None:
    if obj is None:
        return None
    import dataclasses
    return dataclasses.asdict(obj)


def _enumerate_dates(start, end) -> list[_dt.date]:
    s = start if isinstance(start, _dt.date) else _dt.date.fromisoformat(start)
    e = end if isinstance(end, _dt.date) else _dt.date.fromisoformat(end)
    out, cur = [], s
    while cur <= e:
        out.append(cur)
        cur += _dt.timedelta(days=1)
    return out
