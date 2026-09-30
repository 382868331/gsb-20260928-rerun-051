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
import uuid
from pathlib import Path

from .calendar import TradingCalendar
from .adjudication import Adjudication
from .identity import Security, Split
from .query import QuoteView, SourceCandidate
from .quote import Quote, QuoteEvent, QuoteStatus
from .rules import SourceRule

SCHEMA_VERSION = 3


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
    target_source TEXT NOT NULL DEFAULT '',
    target_record TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    UNIQUE(security_id, business_date, source, source_record)
);
CREATE INDEX IF NOT EXISTS idx_event_asof
    ON quote_event(security_id, business_date, received_at, seq);
CREATE TABLE IF NOT EXISTS adjudication(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id TEXT NOT NULL,
    business_date TEXT NOT NULL,
    chosen_source TEXT NOT NULL,
    chosen_record TEXT NOT NULL,
    decision_received_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_adjudication_asof
    ON adjudication(security_id, business_date, decision_received_at, seq);
CREATE TABLE IF NOT EXISTS source_rule(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL,
    priority_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    restores_rule_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_source_rule_asof
    ON source_rule(received_at, seq);
"""

_EXTRA_COLUMNS = {
    "quote_event": {
        "target_source": "TEXT NOT NULL DEFAULT ''",
        "target_record": "TEXT NOT NULL DEFAULT ''",
    },
}


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
        # 默认跨来源优先级（序号越小越优先）；修订由同来源事件链单独处理。
        self.source_priority: dict[str, int] = {}

    def _migrate(self) -> None:
        """为旧库补齐新增列，保证首轮快照可直接重开。"""
        for table, columns in _EXTRA_COLUMNS.items():
            existing = {
                r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for column, decl in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.conn.commit()
        self.conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def set_source_priority(self, priority: dict[str, int]) -> None:
        """设置基线来源优先级（内存态）。

        仅用于尚未收到任何持久化规则修订（add_source_rule）的时期，
        以及对旧快照的兼容；不会被当作某次“修订”记录，也不会改变历史
        视角的结果。需要带身份、带生效时间、可保存的规则请用 add_source_rule。
        """
        self.source_priority = dict(priority)

    # ---------- 来源选择规则（双时态修订） ----------

    def add_source_rule(self, rule: SourceRule) -> tuple[bool, int | None]:
        """追加一版来源选择规则修订。

        规则在其 received_at 之前的历史视角不可见；append-only，永不就地
        覆盖。同一 rule_id 重放且内容一致时幂等忽略，内容不同则抛错。
        把配置改回旧版本的内容也必须使用新的 rule_id（修订身份不回退），
        可用 restores_rule_id 标注本次恢复的是哪份旧配置。
        """
        payload = json.dumps(
            {
                "priority": rule.priority_items(),
                "received_at": rule.received_at.isoformat(),
                "reason": rule.reason,
                "restores_rule_id": rule.restores_rule_id,
            },
            sort_keys=True, ensure_ascii=False,
        )
        existing = self.conn.execute(
            "SELECT seq, priority_json, received_at, reason, restores_rule_id"
            " FROM source_rule WHERE rule_id=?",
            (rule.rule_id,),
        ).fetchone()
        if existing is not None:
            old_payload = json.dumps(
                {
                    "priority": tuple(sorted(
                        (s, int(r)) for s, r in json.loads(existing["priority_json"])
                    )),
                    "received_at": existing["received_at"],
                    "reason": existing["reason"],
                    "restores_rule_id": existing["restores_rule_id"],
                },
                sort_keys=True, ensure_ascii=False,
            )
            if old_payload != payload:
                raise ValueError(
                    f"规则修订 {rule.rule_id} 已存在且内容不同，修订不可覆盖")
            return False, existing["seq"]
        cur = self.conn.execute(
            "INSERT INTO source_rule(rule_id, received_at, priority_json, reason,"
            " restores_rule_id) VALUES (?,?,?,?,?)",
            (
                rule.rule_id,
                rule.received_at.isoformat(),
                json.dumps(rule.priority_items(), ensure_ascii=False),
                rule.reason,
                rule.restores_rule_id,
            ),
        )
        self.conn.commit()
        return True, cur.lastrowid

    def source_rule_history(self) -> list[dict]:
        """按生效顺序返回全部规则修订（含回退留痕），重开后保持不变。"""
        rows = self.conn.execute(
            "SELECT * FROM source_rule ORDER BY received_at, seq"
        )
        return [self._rule_row_to_dict(r) for r in rows]

    @staticmethod
    def _rule_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "rule_id": row["rule_id"],
            "received_at": _parse_dt(row["received_at"]),
            "priority": {s: int(r) for s, r in json.loads(row["priority_json"])},
            "reason": row["reason"],
            "restores_rule_id": row["restores_rule_id"],
        }

    def _rule_asof(self, known_at: _dt.datetime) -> sqlite3.Row | None:
        """known_at 时点实际已生效的最新一版规则（历史视角）。"""
        return self.conn.execute(
            "SELECT * FROM source_rule WHERE received_at <= ?"
            " ORDER BY received_at DESC, seq DESC LIMIT 1",
            (known_at.isoformat(),),
        ).fetchone()

    def _rule_by_id(self, rule_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM source_rule WHERE rule_id=?", (rule_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"规则修订不存在: {rule_id}")
        return row

    def _effective_rule(
        self, known_at: _dt.datetime, rule_id: str | None
    ) -> tuple[dict[str, int], sqlite3.Row | None]:
        """确定本次裁决使用的优先级配置及其修订身份。

        rule_id 为 None 时严格按历史视角：只采用 known_at 之前已生效的规则；
        显式给出 rule_id 表示“拿今天的新偏好重新研究过去时点”——行情可见性
        仍严格受 known_at 约束，仅来源选择规则被替换为该修订版本。
        """
        if rule_id is not None:
            row = self._rule_by_id(rule_id)
            return {s: int(r) for s, r in json.loads(row["priority_json"])}, row
        row = self._rule_asof(known_at)
        if row is None:
            return dict(self.source_priority), None
        return {s: int(r) for s, r in json.loads(row["priority_json"])}, row

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

    def add_adjudication(self, decision: Adjudication) -> tuple[bool, int | None]:
        """追加一条冲突裁决。裁决只引用来源记录，绝不修改来源原记录。

        同一 decision_id 重放视为幂等。裁决在其 decision_received_at 之前不可见。
        """
        if decision.decision_id:
            existing = self.conn.execute(
                "SELECT seq FROM adjudication WHERE decision_id=?",
                (decision.decision_id,),
            ).fetchone()
            if existing is not None:
                return False, existing["seq"]
        cur = self.conn.execute(
            "INSERT INTO adjudication(security_id, business_date, chosen_source,"
            " chosen_record, decision_received_at, reason, decision_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                decision.security_id,
                decision.business_date.isoformat(),
                decision.chosen_source,
                decision.chosen_record,
                decision.decision_received_at.isoformat(),
                decision.reason,
                decision.decision_id or f"_auto_{uuid.uuid4().hex}",
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
                "target": [event.target_source, event.target_record],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---------- 双时态查询 ----------

    def _visible_events(
        self, security_id: str, business_date: str, known_at: _dt.datetime
    ) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM quote_event"
            " WHERE security_id=? AND business_date=? AND received_at <= ?"
            " ORDER BY received_at, seq",
            (security_id, business_date, known_at.isoformat()),
        ))

    def _visible_adjudication(
        self, security_id: str, business_date: str, known_at: _dt.datetime
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM adjudication"
            " WHERE security_id=? AND business_date=? AND decision_received_at <= ?"
            " ORDER BY decision_received_at DESC, seq DESC LIMIT 1",
            (security_id, business_date, known_at.isoformat()),
        ).fetchone()

    @staticmethod
    def _row_key(row: sqlite3.Row) -> tuple[str, str]:
        return row["source"], row["source_record"]

    def _resolve(
        self,
        security_id: str,
        bd: _dt.date,
        known_at: _dt.datetime,
        priority: dict[str, int] | None = None,
    ) -> dict:
        """合并截至 known_at 可见的全部事件，输出候选与裁决结果。

        - 每条来源原始/修订记录都保留为候选，并标注是否已被其来源撤回；
        - revision 是对事实的更正：最新一条未撤回的修订优先于 raw；
        - 多条 raw 冲突时，优先采用可见裁决指向的记录，否则按来源优先级；
        - suspension / cancel 对全天生效，时间晚于被选行情记录时覆盖行情状态。
        """
        bd_s = bd.isoformat()
        rows = self._visible_events(security_id, bd_s, known_at)

        withdrawn_by: dict[tuple[str, str], str] = {}
        status_events: list[sqlite3.Row] = []
        quote_rows: list[sqlite3.Row] = []
        for row in rows:
            kind = row["kind"]
            if kind == "withdrawal":
                withdrawn_by[(row["target_source"], row["target_record"])] = (
                    row["source_record"]
                )
            elif kind in ("suspension", "cancel"):
                status_events.append(row)
            else:
                quote_rows.append(row)

        candidates = tuple(
            SourceCandidate(
                source=r["source"],
                source_record=r["source_record"],
                kind=r["kind"],
                received_at=_parse_dt(r["received_at"]),
                close=r["close"],
                active=self._row_key(r) not in withdrawn_by,
                withdrawn_by=withdrawn_by.get(self._row_key(r), ""),
            )
            for r in quote_rows
        )
        active_rows = [
            r for r in quote_rows if self._row_key(r) not in withdrawn_by
        ]

        decision = self._visible_adjudication(security_id, bd_s, known_at)
        chosen: sqlite3.Row | None = None
        basis = "none"
        chosen_decision: sqlite3.Row | None = None

        if decision is not None:
            target = (decision["chosen_source"], decision["chosen_record"])
            matched = next(
                (r for r in active_rows if self._row_key(r) == target), None
            )
            if matched is not None:
                chosen, chosen_decision, basis = matched, decision, "adjudicated"

        if chosen is None:
            active_revisions = [r for r in active_rows if r["kind"] == "revision"]
            active_raws = [r for r in active_rows if r["kind"] == "raw"]
            if active_revisions:
                chosen = max(active_revisions, key=lambda r: (r["received_at"], r["seq"]))
                basis = "revision"
            elif active_raws:
                chosen = self._choose_raw(active_raws, priority)
                basis = "single-source" if len(active_raws) == 1 else "default-priority"

        return {
            "rows": rows,
            "candidates": candidates,
            "saw_records": bool(quote_rows),
            "status_events": status_events,
            "chosen": chosen,
            "basis": basis,
            "decision": chosen_decision,
        }

    def _choose_raw(
        self,
        raws: list[sqlite3.Row],
        priority: dict[str, int] | None = None,
    ) -> sqlite3.Row:
        def rank(row: sqlite3.Row) -> tuple:
            effective = self.source_priority if priority is None else priority
            rank_value = effective.get(row["source"], 10**9)
            return (rank_value, row["source"], -int(row["seq"]))
        return min(raws, key=rank)

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
        rule_id: str | None = None,
    ) -> QuoteView:
        """as-of 查询：在 known_at 时点，回看 business_date 当天可知的行情。

        rule_id=None 时保持原历史视角：来源选择规则也只取 known_at 时已经
        生效的修订，今天的偏好不会改写过去看到的结果。显式传入 rule_id
        表示按该版规则“重新研究”：行情/撤回/裁决/拆分公告的可见性仍严格
        以 known_at 为准，仅来源选择规则替换为该修订（哪怕它当时尚未生效）。
        """
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        priority, rule_row = self._effective_rule(ka, rule_id)
        resolved = self._resolve(security_id, bd, ka, priority)
        chosen = resolved["chosen"]
        status_events = resolved["status_events"]

        saw_records = resolved["saw_records"]
        if chosen is None and not status_events:
            if saw_records:
                status = QuoteStatus.WITHDRAWN
                return QuoteView(
                    security_id, bd, ka, status, None, None, "", "", None, "withdrawal",
                    candidates=resolved["candidates"],
                    selection_basis=resolved["basis"],
                    rule_id=rule_row["rule_id"] if rule_row else "",
                )
            if self.calendar.is_session(bd):
                status = QuoteStatus.MISSING
            else:
                status = QuoteStatus.NON_SESSION
            return QuoteView(
                security_id, bd, ka, status, None, None, "", "", None, "none",
                candidates=resolved["candidates"],
                rule_id=rule_row["rule_id"] if rule_row else "",
            )

        latest_status = (
            max(status_events, key=lambda r: (r["received_at"], r["seq"]))
            if status_events else None
        )
        status_override = latest_status is not None and (
            chosen is None
            or (latest_status["received_at"], latest_status["seq"])
            > (chosen["received_at"], chosen["seq"])
        )

        if status_override:
            kind = latest_status["kind"]
            if kind == "cancel":
                status, quote = QuoteStatus.CANCELLED, None
            else:
                status, quote = QuoteStatus.SUSPENDED, None
            row = latest_status
        elif chosen is None:
            status, quote, row = QuoteStatus.WITHDRAWN, None, None
        else:
            kind, status, row = chosen["kind"], QuoteStatus.OK, chosen
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
            source=row["source"] if row else "",
            source_record=row["source_record"] if row else "",
            received_at=_parse_dt(row["received_at"]) if row else None,
            event_kind=kind,
            candidates=resolved["candidates"],
            adjudication_id=(resolved["decision"]["decision_id"]
                             if resolved["decision"] else ""),
            decision_received_at=(
                _parse_dt(resolved["decision"]["decision_received_at"])
                if resolved["decision"] else None
            ),
            decision_reason=(
                resolved["decision"]["reason"] if resolved["decision"] else ""
            ),
            selection_basis=resolved["basis"],
            rule_id=rule_row["rule_id"] if rule_row else "",
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

    # ---------- 多历史视角复算与差异归因 ----------

    def recompute_viewpoints(
        self,
        security_id: str,
        business_date: str | _dt.date,
        viewpoints,
        rule_id: str | None = None,
    ) -> list[dict]:
        """在多个历史视角（known_at 列表）下复算同一业务日的行情结论。

        每个视角都只依赖该时点之前实际收到的行情事件、撤回收达和裁决，
        不使用任何后知信息。
        """
        out = []
        for ka in viewpoints:
            view = self.quote_at(security_id, business_date, ka, rule_id=rule_id)
            out.append({"known_at": view.known_at, "view": view})
        return out

    @staticmethod
    def _view_signature(view: QuoteView) -> dict:
        return {
            "status": view.status.value,
            "close": view.quote.close if view.quote else None,
            "adjusted_close": view.adjusted_close,
            "source": view.source,
            "source_record": view.source_record,
            "event_kind": view.event_kind,
            "selection_basis": view.selection_basis,
            "adjudication_id": view.adjudication_id,
            "rule_id": view.rule_id,
            "active_records": tuple(
                (c.source, c.source_record) for c in view.candidates if c.active
            ),
        }

    def new_information(
        self,
        security_id: str,
        business_date: str | _dt.date,
        start: str | _dt.datetime,
        end: str | _dt.datetime,
    ) -> list[dict]:
        """列出 (start, end] 区间内新增到达、且影响该业务日的信息。"""
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        s, e = _parse_dt(start), _parse_dt(end)
        events = []
        for r in self.conn.execute(
            "SELECT * FROM quote_event"
            " WHERE security_id=? AND business_date=?"
            " AND received_at > ? AND received_at <= ? ORDER BY received_at, seq",
            (security_id, bd.isoformat(), s.isoformat(), e.isoformat()),
        ):
            events.append({
                "type": "event", "kind": r["kind"],
                "source": r["source"], "source_record": r["source_record"],
                "target": [r["target_source"], r["target_record"]]
                if r["kind"] == "withdrawal" else None,
                "received_at": _parse_dt(r["received_at"]),
                "reason": r["reason"],
            })
        for r in self.conn.execute(
            "SELECT * FROM adjudication"
            " WHERE security_id=? AND business_date=?"
            " AND decision_received_at > ? AND decision_received_at <= ?"
            " ORDER BY decision_received_at, seq",
            (security_id, bd.isoformat(), s.isoformat(), e.isoformat()),
        ):
            events.append({
                "type": "adjudication", "kind": "adjudication",
                "decision_id": r["decision_id"],
                "chosen": [r["chosen_source"], r["chosen_record"]],
                "source": r["chosen_source"],
                "source_record": r["chosen_record"],
                "received_at": _parse_dt(r["decision_received_at"]),
                "reason": r["reason"],
            })
        # 该区间内新收到、影响 business_date 复权的拆分公告
        for r in self.conn.execute(
            "SELECT ex_date, ratio, received_at FROM corp_split"
            " WHERE security_id=? AND ex_date > ?"
            " AND received_at > ? AND received_at <= ? ORDER BY received_at",
            (security_id, bd.isoformat(), s.isoformat(), e.isoformat()),
        ):
            events.append({
                "type": "split", "kind": "split",
                "ex_date": _dt.date.fromisoformat(r["ex_date"]),
                "ratio": r["ratio"],
                "received_at": _parse_dt(r["received_at"]),
                "source": "", "source_record": "",
            })
        # 该区间内新生效的来源选择规则修订（会改变 default-priority 的选择）
        for r in self.conn.execute(
            "SELECT rule_id, received_at, reason, priority_json, restores_rule_id"
            " FROM source_rule WHERE received_at > ? AND received_at <= ?"
            " ORDER BY received_at, seq",
            (s.isoformat(), e.isoformat()),
        ):
            events.append({
                "type": "rule", "kind": "rule",
                "rule_id": r["rule_id"],
                "priority": {x: int(y) for x, y in json.loads(r["priority_json"])},
                "restores_rule_id": r["restores_rule_id"],
                "received_at": _parse_dt(r["received_at"]),
                "reason": r["reason"],
                "source": "", "source_record": "",
            })
        return sorted(events, key=lambda x: x["received_at"])

    def explain_viewpoint_diff(
        self,
        security_id: str,
        business_date: str | _dt.date,
        earlier: str | _dt.datetime,
        later: str | _dt.datetime,
    ) -> dict:
        """说明两个历史视角之间结果差异具体来自哪次新增信息。

        即使候选记录数量不变（如仅裁决改选或复权公告送达），
        内容字段的变化也会被逐字段列出。
        """
        before = self.quote_at(security_id, business_date, earlier)
        after = self.quote_at(security_id, business_date, later)
        sig_before, sig_after = self._view_signature(before), self._view_signature(after)
        changed = {
            key: {"before": sig_before[key], "after": sig_after[key]}
            for key in sig_before
            if sig_before[key] != sig_after[key]
        }
        return {
            "security_id": security_id,
            "business_date": before.business_date,
            "earlier": before.known_at,
            "later": after.known_at,
            "changed_fields": changed,
            "new_information": self.new_information(
                security_id, business_date, before.known_at, after.known_at
            ),
        }

    # ---------- 规则调整前后的研究对照 ----------

    def research_comparison(
        self,
        security_id: str,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        new_rule_id: str,
    ) -> dict:
        """同一研究时点的“原历史视角” vs “按新规则重新研究”对照。

        - historical：known_at 当时实际生效的规则下看到的结果。规则在
          known_at 之后才生效时，historical 不受其影响（价格保持不变）。
        - revised：显式采用 new_rule_id 重新研究同一时点的结果；行情事件、
          撤回、裁决、拆分公告的可见性仍严格以 known_at 为界。
        两份结论各自记录所用规则身份，便于追溯同一次价格为何不同。
        """
        ka = _parse_dt(known_at)
        historical = self.quote_at(security_id, business_date, ka)
        revised = self.quote_at(
            security_id, business_date, ka, rule_id=new_rule_id)
        return {
            "security_id": security_id,
            "business_date": historical.business_date,
            "known_at": ka,
            "new_rule_id": new_rule_id,
            "historical": {
                "view": historical,
                "rule_id": historical.rule_id,
            },
            "revised": {
                "view": revised,
                "rule_id": revised.rule_id,
            },
            "changed_fields": self._compare_views(historical, revised),
        }

    def research_comparison_series(
        self,
        security_id: str,
        business_date: str | _dt.date,
        viewpoints,
        new_rule_id: str,
    ) -> list[dict]:
        """在多个研究时点上分别给出原视角与新规则重研结果（逐视角对照）。"""
        return [
            self.research_comparison(
                security_id, business_date, ka, new_rule_id=new_rule_id)
            for ka in viewpoints
        ]

    def _compare_views(self, before: QuoteView, after: QuoteView) -> dict:
        sig_before, sig_after = self._view_signature(before), self._view_signature(after)
        return {
            key: {"before": sig_before[key], "after": sig_after[key]}
            for key in sig_before
            if sig_before[key] != sig_after[key]
        }

    def information_timeline(
        self,
        security_id: str,
        business_date: str | _dt.date,
    ) -> list[dict]:
        """列出与该 (证券, 业务日) 研究相关的每条信息何时对研究系统可见。

        覆盖：原始价格(raw)、修订(revision)、停牌(suspension)、撤销(cancel)、
        来源撤回(withdrawal)、冲突裁决(adjudication)、拆分公告(split)、
        来源选择规则修订(rule)。按收到/生效时间排序，供追溯对照。
        """
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        bd_s = bd.isoformat()
        items: list[dict] = []
        for r in self.conn.execute(
            "SELECT * FROM quote_event WHERE security_id=? AND business_date=?"
            " ORDER BY received_at, seq",
            (security_id, bd_s),
        ):
            items.append({
                "type": "event",
                "kind": r["kind"],
                "visible_at": _parse_dt(r["received_at"]),
                "source": r["source"],
                "source_record": r["source_record"],
                "close": r["close"],
                "target": [r["target_source"], r["target_record"]]
                if r["kind"] == "withdrawal" else None,
                "reason": r["reason"],
            })
        for r in self.conn.execute(
            "SELECT * FROM adjudication WHERE security_id=? AND business_date=?"
            " ORDER BY decision_received_at, seq",
            (security_id, bd_s),
        ):
            items.append({
                "type": "adjudication",
                "kind": "adjudication",
                "visible_at": _parse_dt(r["decision_received_at"]),
                "decision_id": r["decision_id"],
                "chosen": [r["chosen_source"], r["chosen_record"]],
                "reason": r["reason"],
            })
        for r in self.conn.execute(
            "SELECT ex_date, ratio, received_at FROM corp_split"
            " WHERE security_id=? AND ex_date > ? ORDER BY received_at",
            (security_id, bd_s),
        ):
            items.append({
                "type": "split",
                "kind": "split",
                "visible_at": _parse_dt(r["received_at"]),
                "ex_date": _dt.date.fromisoformat(r["ex_date"]),
                "ratio": r["ratio"],
            })
        for r in self.conn.execute(
            "SELECT * FROM source_rule ORDER BY received_at, seq"
        ):
            items.append({
                "type": "rule",
                "kind": "rule",
                "visible_at": _parse_dt(r["received_at"]),
                "rule_id": r["rule_id"],
                "priority": {s: int(v) for s, v in json.loads(r["priority_json"])},
                "restores_rule_id": r["restores_rule_id"],
                "reason": r["reason"],
            })
        return sorted(items, key=lambda x: x["visible_at"])

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
        for table in ("security", "trading_session", "corp_split",
                      "quote_event", "adjudication", "source_rule"):
            counts[table] = self.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        hashes = (
            r[0] for r in self.conn.execute(
                "SELECT content_hash FROM quote_event ORDER BY seq"
            )
        )
        rule_payload = (
            r[0] for r in self.conn.execute(
                "SELECT rule_id || '|' || received_at || '|' || priority_json"
                " || '|' || COALESCE(restores_rule_id,'')"
                " FROM source_rule ORDER BY seq"
            )
        )
        digest = hashlib.sha256(
            ("".join(hashes) + "".join(rule_payload)).encode("utf-8")
        ).hexdigest()
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
