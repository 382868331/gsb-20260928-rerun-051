"""数据集：SQLite 持久化 + 双时态合并查询与跨来源冲突裁决。

事件（quote_event）永不就地更新。同一 (证券, 业务日) 下可能同时存在多个
来源的不同价格：系统保留全部原始记录，按截至 known_at 已知的来源优先级与
收到时间裁决，并记录采用原因。撤回/撤销也是带 received_at 的事件，只影响
撤回送达之后的视角；来源优先级调整同样保留生效时间，不能回头改写早期研究。
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
from .quote import QuoteCandidate
from .query import QuoteView
from .quote import Quote, QuoteEvent, QuoteStatus
from .source import SourceInfo

SCHEMA_VERSION = 2


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
CREATE TABLE IF NOT EXISTS source_info(
    source_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    PRIMARY KEY (source_id, valid_from)
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
    target_source TEXT NOT NULL DEFAULT '',
    target_record TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    UNIQUE(security_id, business_date, source, source_record, kind)
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
        self._migrate()
        self._init_meta(name)
        self.calendar = TradingCalendar(self.conn)

    def _migrate(self) -> None:
        """补齐旧库（首轮 schema 1）新增列；历史事件永远不重写。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(quote_event)")}
        if "target_source" not in cols:
            self.conn.execute(
                "ALTER TABLE quote_event ADD COLUMN target_source TEXT NOT NULL DEFAULT ''"
            )
        if "target_record" not in cols:
            self.conn.execute(
                "ALTER TABLE quote_event ADD COLUMN target_record TEXT NOT NULL DEFAULT ''"
            )
        self.conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
        )
        self.conn.commit()

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

    # ---------- 来源优先级（也是双时态事实）----------

    def add_source(
        self,
        source: SourceInfo,
        valid_from: str | _dt.datetime | None = None,
    ) -> None:
        """登记来源或登记一次优先级调整。

        每次调整都是从 valid_from 起生效的新事实；旧优先级保留，as-of 裁决
        只使用 valid_from <= known_at 的最新一条，因此调整不能改写过去视角。
        """
        vf = _parse_dt(valid_from) if valid_from is not None else _dt.datetime.min
        latest = self.conn.execute(
            "SELECT MAX(valid_from) FROM source_info WHERE source_id=?",
            (source.source_id,),
        ).fetchone()[0]
        if latest is not None and vf.isoformat() < latest:
            raise ValueError("来源优先级调整只能按生效时间追加，不能回插改写早期视角")
        self.conn.execute(
            "INSERT OR REPLACE INTO source_info(source_id, name, priority, valid_from)"
            " VALUES (?,?,?,?)",
            (source.source_id, source.name, source.priority, vf.isoformat()),
        )
        self.conn.commit()

    def add_sources(self, sources) -> None:
        for source in sources:
            self.add_source(source)

    def source_priority(self, source_id: str, known_at: _dt.datetime) -> int:
        row = self.conn.execute(
            "SELECT priority FROM source_info"
            " WHERE source_id=? AND valid_from <= ?"
            " ORDER BY valid_from DESC, rowid DESC LIMIT 1",
            (source_id, known_at.isoformat()),
        ).fetchone()
        return int(row[0]) if row else 0

    # ---------- 行情写入 ----------

    def add_event(self, event: QuoteEvent) -> tuple[bool, int | None]:
        """写入一条事件。返回 (是否为新记录, seq)。

        同一来源记录号重放时返回 False（幂等）；来源记录号相同但内容不同
        （真冲突）抛出异常，绝不静默覆盖。
        """
        h = self._content_hash(event)
        existing = self.conn.execute(
            "SELECT seq, content_hash FROM quote_event"
            " WHERE security_id=? AND business_date=? AND source=? AND source_record=? AND kind=?",
            (
                event.security_id,
                event.business_date.isoformat(),
                event.source,
                event.source_record,
                event.kind,
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
            " open, high, low, close, volume, reason, source, source_record,"
            " target_source, target_record, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.security_id,
                event.business_date.isoformat(),
                event.received_at.isoformat(),
                event.kind,
                o, hi, lo, c, v,
                event.reason,
                event.source,
                event.source_record,
                event.target_source,
                event.target_record,
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
                "target_source": event.target_source,
                "target_record": event.target_record,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---------- 双时态查询 ----------

    def _visible_rows(
        self, security_id: str, business_date: str, known_at: _dt.datetime
    ) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM quote_event"
            " WHERE security_id=? AND business_date=? AND received_at <= ?"
            " ORDER BY received_at, seq",
            (security_id, business_date, known_at.isoformat()),
        ))

    @staticmethod
    def _row_quote(row: sqlite3.Row) -> Quote:
        return Quote(row["open"], row["high"], row["low"], row["close"], row["volume"])

    def _adjudicate(
        self, security_id: str, bd: _dt.date, ka: _dt.datetime
    ) -> tuple[QuoteStatus, Quote | None, sqlite3.Row | None, str, bool,
               tuple[QuoteCandidate, ...]]:
        """按 known_at 重放当日全部已收到事件，得出当时的裁决。"""
        rows = self._visible_rows(security_id, bd.isoformat(), ka)
        if not rows:
            status = QuoteStatus.MISSING if self.calendar.is_session(bd) else QuoteStatus.NON_SESSION
            return status, None, None, "", False, ()

        publications: dict[tuple[str, str], sqlite3.Row] = {}
        superseded: set[tuple[str, str]] = set()
        retracted: dict[tuple[str, str], str] = {}
        suspensions: list[sqlite3.Row] = []

        def _retract(source_id: str, record_id: str, reason: str) -> None:
            if record_id == "*":
                targets = [key for key in publications if key[0] == source_id]
            else:
                targets = [(source_id, record_id)]
            for key in targets:
                publications.pop(key, None)
                superseded.discard(key)
                retracted[key] = reason

        for row_index, row in enumerate(rows):
            kind = row["kind"]
            if kind in ("raw", "revision"):
                key = (row["source"], row["source_record"])
                if kind == "revision":
                    for old in list(publications):
                        if old[0] == row["source"] and old != key:
                            publications.pop(old, None)
                            superseded.add(old)
                prior_retraction = any(
                    e["kind"] == "retraction"
                    and (e["target_source"] or e["source"]) == row["source"]
                    and (
                        e["target_record"] == "*"
                        or e["target_record"] == row["source_record"]
                    )
                    and _parse_dt(e["received_at"]) <= _parse_dt(row["received_at"])
                    for e in rows[:row_index]
                )
                if prior_retraction:
                    retracted[key] = retracted.get(key, "该记录已被来源撤回")
                    continue
                retracted.pop(key, None)
                superseded.discard(key)
                publications[key] = row
            elif kind == "retraction":
                _retract(row["target_source"] or row["source"],
                         row["target_record"], row["reason"])
            elif kind == "cancel":
                # 首轮兼容：未指定目标时撤销该来源当日全部主张。
                _retract(row["target_source"] or row["source"],
                         row["target_record"] or "*", row["reason"])
            elif kind == "suspension":
                suspensions.append(row)

        candidates: list[QuoteCandidate] = []
        for row in rows:
            key = (row["source"], row["source_record"])
            if row["kind"] in ("raw", "revision"):
                if key in publications:
                    state = "active"
                elif key in retracted:
                    state = "retracted"
                else:
                    state = "superseded"
                candidates.append(QuoteCandidate(
                    row["source"], row["source_record"], row["kind"],
                    _parse_dt(row["received_at"]), self._row_quote(row),
                    state, retracted.get(key, ""),
                ))
            elif row["kind"] in ("cancel", "retraction"):
                candidates.append(QuoteCandidate(
                    row["source"], row["source_record"], row["kind"],
                    _parse_dt(row["received_at"]), None, "retracted", row["reason"],
                ))
            else:
                candidates.append(QuoteCandidate(
                    row["source"], row["source_record"], row["kind"],
                    _parse_dt(row["received_at"]), None, "active", row["reason"],
                ))

        if suspensions:
            row = suspensions[-1]
            if publications:
                reason = (
                    f"已收到 {row['source']} 停牌通知；停牌日报价均不得采用，"
                    f"另有 {len(publications)} 条来源报价保留为候选"
                )
            else:
                reason = f"已收到 {row['source']} 停牌通知"
            return (QuoteStatus.SUSPENDED, None, row,
                    reason, False, tuple(candidates))

        active = list(publications.values())
        if active:
            quotes = {_quote_to_tuple(self._row_quote(r)) for r in active}
            conflict = len(quotes) > 1

            def _rank(row: sqlite3.Row):
                return (
                    self.source_priority(row["source"], ka),
                    _parse_dt(row["received_at"]),
                    row["seq"],
                )

            winner = max(active, key=_rank)
            winner_priority = self.source_priority(winner["source"], ka)
            ties = [r for r in active if self.source_priority(r["source"], ka) == winner_priority]
            if conflict and len(ties) > 1:
                reason = (
                    f"来源 {winner['source']} 与其他来源价格不同；"
                    f"截至 {ka.isoformat()} 其优先级并列最高，按收到时间/送达顺序采用"
                )
            elif conflict:
                reason = (
                    f"来源 {winner['source']} 与其他来源价格不同；"
                    f"截至 {ka.isoformat()} 其来源优先级最高，故采用"
                )
            else:
                reason = f"各有效来源记录内容一致，采用 {winner['source']} 的记录"
            return (QuoteStatus.OK, self._row_quote(winner), winner,
                    reason, conflict, tuple(candidates))

        quote_events = [r for r in rows if r["kind"] in ("raw", "revision", "cancel", "retraction")]
        if quote_events:
            row = quote_events[-1]
            return (QuoteStatus.CANCELLED, None, row,
                    f"全部已知行情记录均已撤回/撤销（最近事件来自 {row['source']}）",
                    False, tuple(candidates))

        row = rows[-1]
        return QuoteStatus.MISSING, None, row, "", False, tuple(candidates)

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
        status, quote, row, decision_reason, conflict, candidates = self._adjudicate(
            security_id, bd, ka
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
            source=row["source"] if row else "",
            source_record=row["source_record"] if row else "",
            received_at=_parse_dt(row["received_at"]) if row else None,
            event_kind=row["kind"] if row else "none",
            decision_reason=decision_reason,
            conflict=conflict,
            candidates=candidates,
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
            "SELECT business_date, received_at, kind, close, reason, source, source_record,"
            " target_source, target_record"
            " FROM quote_event WHERE security_id=? AND received_at >= ? AND received_at <= ?"
            " ORDER BY received_at, seq",
            (security_id, str(start), str(end)),
        )
        return [dict(r) for r in rows]

    # ---------- 多历史视角研究复算与差异归因 ----------

    def run_research(
        self,
        security_id: str,
        start: str | _dt.date,
        end: str | _dt.date,
        known_at: str | _dt.datetime,
        only_sessions: bool = True,
    ) -> dict:
        """冻结一次研究视角：时间窗 + 当时已收到信息重算出的全部结果。"""
        ka = _parse_dt(known_at)
        views = self.series(security_id, start, end, ka, only_sessions=only_sessions)
        return {
            "security_id": security_id,
            "start": (start if isinstance(start, str) else start.isoformat()),
            "end": (end if isinstance(end, str) else end.isoformat()),
            "known_at": ka.isoformat(),
            "views": [v.as_dict() for v in views],
            "content_fingerprint": self._views_fingerprint(views),
        }

    def recompute_research(self, research: dict, known_at: str | _dt.datetime | None = None) -> dict:
        """按同一证券、同一业务时间窗在另一个历史视角复算。"""
        return self.run_research(
            research["security_id"], research["start"], research["end"],
            known_at if known_at is not None else research["known_at"],
        )

    @staticmethod
    def _views_fingerprint(views: list[QuoteView]) -> str:
        payload = [
            [
                v.business_date.isoformat(),
                v.status.value,
                _quote_to_tuple(v.quote) if v.quote else None,
                v.adjusted_close,
                v.source,
                v.source_record,
                v.event_kind,
                v.conflict,
                [(c.source, c.source_record, c.state) for c in v.candidates],
            ]
            for v in views
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def explain_change(
        self,
        security_id: str,
        start: str | _dt.date,
        end: str | _dt.date,
        old_known_at: str | _dt.datetime,
        new_known_at: str | _dt.datetime,
        only_sessions: bool = True,
    ) -> dict:
        """在两个历史视角之间复算，并把结果差异归因到窗内新增信息。

        每次新增事件（修订/停牌/撤回/拆分公告）都逐项列出；views_diff 只包含
        裁决结果实际变化的业务日。即使事件表记录数量不变（例如仅来源优先级
        调整导致换源），content_changed 也会如实反映内容变化。
        """
        old_ka = _parse_dt(old_known_at)
        new_ka = _parse_dt(new_known_at)
        old_research = self.run_research(security_id, start, end, old_ka, only_sessions)
        new_research = self.run_research(security_id, start, end, new_ka, only_sessions)

        start_bd = start if isinstance(start, _dt.date) else _dt.date.fromisoformat(str(start))
        end_bd = end if isinstance(end, _dt.date) else _dt.date.fromisoformat(str(end))
        additions = [
            dict(r) for r in self.conn.execute(
                "SELECT seq, business_date, received_at, kind, close, reason, source,"
                " source_record, target_source, target_record FROM quote_event"
                " WHERE security_id=? AND business_date BETWEEN ? AND ?"
                " AND received_at > ? AND received_at <= ? ORDER BY received_at, seq",
                (security_id, start_bd.isoformat(), end_bd.isoformat(),
                 old_ka.isoformat(), new_ka.isoformat()),
            )
        ]
        splits = [
            {"ex_date": r[0], "ratio": r[1], "received_at": r[2]}
            for r in self.conn.execute(
                "SELECT ex_date, ratio, received_at FROM corp_split"
                " WHERE security_id=? AND ex_date > ?"
                " AND received_at > ? AND received_at <= ?"
                " ORDER BY received_at",
                (security_id, start_bd.isoformat(),
                 old_ka.isoformat(), new_ka.isoformat()),
            )
        ]
        source_changes = [
            {"source": r[0], "priority": r[1], "valid_from": r[2]}
            for r in self.conn.execute(
                "SELECT source_id, priority, valid_from FROM source_info"
                " WHERE valid_from > ? AND valid_from <= ? ORDER BY valid_from",
                (old_ka.isoformat(), new_ka.isoformat()),
            )
        ]

        old_by_date = {v["business_date"]: v for v in old_research["views"]}
        views_diff = []
        for new_view in new_research["views"]:
            old_view = old_by_date.get(new_view["business_date"])
            if old_view is None:
                continue
            material = {
                k: new_view[k]
                for k in ("status", "quote", "adjusted_close", "source",
                          "source_record", "event_kind", "conflict")
            }
            if {k: old_view[k] for k in material} != material:
                material["decision_reason"] = new_view["decision_reason"]
                causal_events = [
                    {
                        "seq": e["seq"], "received_at": e["received_at"],
                        "kind": e["kind"], "source": e["source"],
                        "source_record": e["source_record"],
                        "target_source": e["target_source"],
                        "target_record": e["target_record"],
                    }
                    for e in additions
                    if e["business_date"] == new_view["business_date"]
                ]
                causal_splits = list(splits)
                causal_sources = list(source_changes)
                views_diff.append({
                    "business_date": new_view["business_date"],
                    "before": {k: old_view[k] for k in material},
                    "after": material,
                    "caused_by_quote_events": causal_events,
                    "caused_by_split_announcements": causal_splits,
                    "caused_by_source_priority_changes": causal_sources,
                })

        return {
            "security_id": security_id,
            "old_known_at": old_ka.isoformat(),
            "new_known_at": new_ka.isoformat(),
            "old_fingerprint": old_research["content_fingerprint"],
            "new_fingerprint": new_research["content_fingerprint"],
            "content_changed": old_research["content_fingerprint"] != new_research["content_fingerprint"],
            "new_quote_events": additions,
            "new_split_announcements": splits,
            "source_priority_changes": source_changes,
            "views_diff": views_diff,
            "old_research": old_research,
            "new_research": new_research,
        }

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
        for table in ("security", "trading_session", "corp_split", "source_info", "quote_event"):
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
