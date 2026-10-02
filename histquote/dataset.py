"""数据集：SQLite 持久化 + 双时态合并查询。

事件（quote_event）永不就地更新；同一 (证券, 业务日) 下，
as-of 查询只取 received_at <= known_at 的最后一条有效事件。
重复记录（同业务时间、同收到时间、同来源记录号）被视为同一事实，只存一次。
"""

from __future__ import annotations

import contextlib
import dataclasses
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
from .identity import IdentityAnnouncement, PortfolioPosition
from .priority import PriorityRule
from .query import QuoteView, SourceCandidate
from .quote import Quote, QuoteEvent, QuoteStatus

SCHEMA_VERSION = 4


def _parse_dt(value: str | _dt.datetime) -> _dt.datetime:
    return value if isinstance(value, _dt.datetime) else _dt.datetime.fromisoformat(value)


def _quote_to_tuple(q: Quote | None) -> tuple:
    if q is None:
        return (None, None, None, None, None)
    return (q.open, q.high, q.low, q.close, q.volume)


def identity_security_id(view: QuoteView) -> str:
    return view.security_id or (view.identity_candidates[0]
                                if len(view.identity_candidates) == 1 else "")


_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS security(
    security_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
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
    reported_code TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS priority_rule(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    supersedes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS identity_announcement(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    security_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    received_at TEXT NOT NULL,
    action TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_record TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    supersedes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_identity_code
    ON identity_announcement(code, effective_date, received_at, seq);
"""

_EXTRA_COLUMNS = {
    "quote_event": {
        "target_source": "TEXT NOT NULL DEFAULT ''",
        "target_record": "TEXT NOT NULL DEFAULT ''",
        "reported_code": "TEXT NOT NULL DEFAULT ''",
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
        indexes = {r[1] for r in self.conn.execute("PRAGMA index_list(security)")}
        if "sqlite_autoindex_security_1" in indexes:
            self.conn.executescript(
                "CREATE TABLE security_new("
                "security_id TEXT PRIMARY KEY, code TEXT NOT NULL,"
                " name TEXT NOT NULL);"
                "INSERT INTO security_new(security_id, code, name)"
                " SELECT security_id, code, name FROM security;"
                "DROP TABLE security;"
                "ALTER TABLE security_new RENAME TO security;"
            )
        for table, columns in _EXTRA_COLUMNS.items():
            existing = {
                r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for column, decl in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.conn.commit()

    def set_source_priority(self, priority: dict[str, int]) -> None:
        """设置来源优先级；未列出的来源排在其后并按来源名字典序决胜。"""
        self.source_priority = dict(priority)

    # ---------- 来源优先级规则（append-only，带历史有效范围） ----------

    def add_priority_rule(self, rule: PriorityRule) -> tuple[bool, int | None]:
        """追加一条来源优先级修订。规则自 received_at 起生效，
        之前的历史视角仍按当时已生效的规则裁决，不被新偏好改写。

        同一 rule_id 重放视为幂等。规则只追加、永不就地修改。
        """
        if rule.rule_id:
            existing = self.conn.execute(
                "SELECT seq FROM priority_rule WHERE rule_id=?", (rule.rule_id,)
            ).fetchone()
            if existing is not None:
                return False, existing["seq"]
        cur = self.conn.execute(
            "INSERT INTO priority_rule(rule_id, received_at, priority, reason,"
            " supersedes) VALUES (?,?,?,?,?)",
            (
                rule.rule_id or f"_auto_{uuid.uuid4().hex}",
                rule.received_at.isoformat() if rule.received_at else "",
                json.dumps(rule.priority, sort_keys=True),
                rule.reason,
                rule.supersedes,
            ),
        )
        self.conn.commit()
        self.source_priority = dict(rule.priority)
        return True, cur.lastrowid

    def set_source_priority_at(
        self,
        priority: dict[str, int],
        received_at: _dt.datetime,
        reason: str = "",
        rule_id: str = "",
    ) -> tuple[bool, int | None]:
        """便捷入口：登记一条自 received_at 起生效的优先级修订。"""
        latest = self._latest_rule_row()
        return self.add_priority_rule(PriorityRule(
            dict(priority), received_at, reason, rule_id,
            supersedes=latest["rule_id"] if latest else "",
        ))

    def rollback_source_priority(
        self,
        rule_id: str,
        received_at: _dt.datetime,
        reason: str = "",
        new_rule_id: str = "",
    ) -> tuple[bool, int | None]:
        """回退到某条历史规则的配置。回退本身是一次新修订：
        保留自己的 rule_id 与 received_at，旧规则原样保留。
        """
        row = self.conn.execute(
            "SELECT * FROM priority_rule WHERE rule_id=?", (rule_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未知优先级规则: {rule_id}")
        latest = self._latest_rule_row()
        return self.add_priority_rule(PriorityRule(
            json.loads(row["priority"]), received_at,
            reason or f"回退到规则 {rule_id}",
            new_rule_id,
            supersedes=latest["rule_id"] if latest else "",
        ))

    def _latest_rule_row(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM priority_rule ORDER BY seq DESC LIMIT 1"
        ).fetchone()

    def priority_rules(self) -> list[PriorityRule]:
        """全部优先级修订（按追加顺序），回退与改判都保留身份。"""
        rows = self.conn.execute(
            "SELECT * FROM priority_rule ORDER BY seq"
        ).fetchall()
        return [
            PriorityRule(
                json.loads(r["priority"]),
                _dt.datetime.fromisoformat(r["received_at"]) if r["received_at"] else None,
                r["reason"], r["rule_id"], r["supersedes"],
            )
            for r in rows
        ]

    def _priority_at(self, known_at: _dt.datetime) -> tuple[dict[str, int], str]:
        """截至 known_at 已生效的最新优先级规则；无规则时用内存默认配置。"""
        row = self.conn.execute(
            "SELECT * FROM priority_rule WHERE received_at <= ?"
            " ORDER BY seq DESC LIMIT 1",
            (known_at.isoformat(),),
        ).fetchone()
        if row is None:
            return self.source_priority, ""
        return json.loads(row["priority"]), row["rule_id"]

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

    # ---------- 身份公告（append-only，带历史有效范围） ----------

    def add_identity_announcement(
        self, announcement: IdentityAnnouncement
    ) -> tuple[bool, int | None]:
        if announcement.announcement_id:
            existing = self.conn.execute(
                "SELECT seq FROM identity_announcement WHERE announcement_id=?",
                (announcement.announcement_id,),
            ).fetchone()
            if existing is not None:
                return False, existing["seq"]
        cur = self.conn.execute(
            "INSERT INTO identity_announcement(announcement_id, code, security_id,"
            " effective_date, received_at, action, name, source, source_record,"
            " reason, supersedes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                announcement.announcement_id,
                announcement.code,
                announcement.security_id,
                announcement.effective_date.isoformat(),
                announcement.received_at.isoformat(),
                announcement.action,
                announcement.name,
                announcement.source,
                announcement.source_record,
                announcement.reason,
                announcement.supersedes,
            ),
        )
        self.conn.commit()
        return True, cur.lastrowid

    def identity_announcements(
        self, code: str | None = None, known_at: str | _dt.datetime | None = None
    ) -> list[IdentityAnnouncement]:
        return [self._identity_from_row(r) for r in
                self._identity_rows(code, known_at)]

    def _identity_rows(
        self, code: str | None = None, known_at: str | _dt.datetime | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM identity_announcement"
        cond, params = [], []
        if code is not None:
            cond.append("code=?")
            params.append(code)
        if known_at is not None:
            cond.append("received_at <= ?")
            params.append(_parse_dt(known_at).isoformat())
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY received_at, seq"
        return list(self.conn.execute(sql, params))

    @staticmethod
    def _identity_from_row(r: sqlite3.Row) -> IdentityAnnouncement:
        return IdentityAnnouncement(
            r["announcement_id"], r["code"], r["security_id"],
            _dt.date.fromisoformat(r["effective_date"]),
            _parse_dt(r["received_at"]), r["action"], r["name"],
            r["source"], r["source_record"], r["reason"], r["supersedes"],
        )

    def resolve_code(
        self,
        code: str,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
    ) -> dict:
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        rows = [
            r for r in self._identity_rows(code, ka)
            if _dt.date.fromisoformat(r["effective_date"]) <= bd
        ]
        withdrawn = {
            r["supersedes"]
            for r in rows
            if r["action"] == "withdraw" and r["supersedes"]
        }
        superseded_at = {}
        for r in rows:
            if (r["action"] == "confirm" and r["supersedes"]
                    and r["announcement_id"] not in withdrawn):
                prior_date = _dt.date.fromisoformat(r["effective_date"])
                old_at = superseded_at.get(r["supersedes"])
                if old_at is None or prior_date < old_at:
                    superseded_at[r["supersedes"]] = prior_date
        latest_by_sid: dict[str, sqlite3.Row] = {}
        for r in rows:
            if r["action"] != "confirm" or r["announcement_id"] in withdrawn:
                continue
            end = superseded_at.get(r["announcement_id"])
            if end is not None and bd >= end:
                continue
            sid = r["security_id"]
            old = latest_by_sid.get(sid)
            if old is None or (r["received_at"], r["seq"]) > (old["received_at"], old["seq"]):
                latest_by_sid[sid] = r
        candidates = tuple(sorted(latest_by_sid))
        active = [latest_by_sid[sid] for sid in candidates]
        if len(candidates) == 1:
            row = active[0]
            return {
                "status": "confirmed",
                "code": code,
                "security_id": candidates[0],
                "candidates": candidates,
                "announcement_ids": (row["announcement_id"],),
                "name": row["name"],
                "source_rule": f"identity-announcement:{row['source']}/{row['source_record']}",
            }
        if len(candidates) > 1:
            return {
                "status": "ambiguous", "code": code, "security_id": "",
                "candidates": candidates,
                "announcement_ids": tuple(r["announcement_id"] for r in active),
                "name": "", "source_rule": "multiple-identity-announcements",
            }
        master = self.conn.execute(
            "SELECT * FROM security WHERE code=? ORDER BY security_id", (code,)
        ).fetchall()
        if not latest_by_sid:
            observed = self.conn.execute(
                "SELECT DISTINCT security_id FROM quote_event"
                " WHERE reported_code=? AND received_at <= ? ORDER BY security_id",
                (code, ka.isoformat()),
            ).fetchall()
            observed_sids = tuple(r["security_id"] for r in observed)
            if len(master) == 1 and (
                not observed_sids or observed_sids == (master[0]["security_id"],)
            ):
                return {
                    "status": "direct", "code": code,
                    "security_id": master[0]["security_id"],
                    "candidates": (master[0]["security_id"],),
                    "announcement_ids": (), "name": master[0]["name"],
                    "source_rule": "security-master-code",
                }
            if observed:
                return {
                    "status": "ambiguous", "code": code, "security_id": "",
                    "candidates": observed_sids,
                    "announcement_ids": (), "name": "",
                    "source_rule": "reported-code-awaits-identity-announcement",
                }
        if len(master) == 1:
            return {
                "status": "direct", "code": code,
                "security_id": master[0]["security_id"],
                "candidates": (master[0]["security_id"],),
                "announcement_ids": (), "name": master[0]["name"],
                "source_rule": "security-master-code",
            }
        return {
            "status": "ambiguous" if master else "unmapped", "code": code,
            "security_id": "", "candidates": tuple(r["security_id"] for r in master),
            "announcement_ids": (), "name": "",
            "source_rule": "insufficient-identity-information",
        }

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
            " reported_code, target_source, target_record, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.security_id,
                event.business_date.isoformat(),
                event.received_at.isoformat(),
                event.kind,
                o, hi, lo, c, v,
                event.reason,
                event.source,
                event.source_record,
                event.reported_code,
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
                "reported_code": event.reported_code,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ---------- 双时态查询 ----------

    def _visible_events(
        self, security_id: str, business_date: str, known_at: _dt.datetime,
        reported_code: str | None = None,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM quote_event"
            " WHERE security_id=? AND business_date=? AND received_at <= ?"
        )
        params: list = [security_id, business_date, known_at.isoformat()]
        if reported_code is not None:
            sql += " AND (reported_code=? OR reported_code='')"
            params.append(reported_code)
        sql += " ORDER BY received_at, seq"
        return list(self.conn.execute(sql, params))

    def _visible_code_events(
        self, code: str, security_ids: tuple[str, ...],
        business_date: str, known_at: _dt.datetime,
    ) -> list[sqlite3.Row]:
        sids = tuple(dict.fromkeys((*security_ids, "")))
        placeholders = ",".join("?" for _ in sids)
        return list(self.conn.execute(
            f"SELECT * FROM quote_event WHERE security_id IN ({placeholders})"
            " AND business_date=? AND received_at <= ?"
            " AND (reported_code=? OR reported_code='')"
            " ORDER BY received_at, seq",
            (*sids, business_date, known_at.isoformat(), code),
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
        self, security_id: str, bd: _dt.date, known_at: _dt.datetime,
        priority_known_at: _dt.datetime | None = None,
        reported_code: str | None = None,
    ) -> dict:
        """合并截至 known_at 可见的全部事件，输出候选与裁决结果。

        - 每条来源原始/修订记录都保留为候选，并标注是否已被其来源撤回；
        - revision 是对事实的更正：最新一条未撤回的修订优先于 raw；
        - 多条 raw 冲突时，优先采用可见裁决指向的记录，否则按来源优先级；
        - suspension / cancel 对全天生效，时间晚于被选行情记录时覆盖行情状态。
        """
        bd_s = bd.isoformat()
        priority, priority_rule_id = self._priority_at(priority_known_at or known_at)
        rows = self._visible_events(security_id, bd_s, known_at, reported_code)

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
                security_id=r["security_id"],
                reported_code=r["reported_code"],
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
            "priority_rule_id": priority_rule_id,
        }

    @staticmethod
    def _choose_raw(raws: list[sqlite3.Row], priority: dict[str, int]) -> sqlite3.Row:
        def rank(row: sqlite3.Row) -> tuple:
            return (priority.get(row["source"], 10**9), row["source"], -int(row["seq"]))
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
        priority_as_of: str | _dt.datetime | None = None,
        reported_code: str | None = None,
        identity_context: dict | None = None,
    ) -> QuoteView:
        """as-of 查询：在 known_at 时点，回看 business_date 当天可知的行情。

        priority_as_of 可单独指定优先级规则的生效视角（用于“按新规则
        重新研究同一历史时点”的对照）；缺省时与 known_at 一致，即
        今天的偏好不会改写过去已经能看到的结果。
        """
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        pa = _parse_dt(priority_as_of) if priority_as_of is not None else None
        resolved = self._resolve(security_id, bd, ka, pa, reported_code)
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
                    queried_code=reported_code or "",
                    **self._identity_view_fields(identity_context),
                )
            if self.calendar.is_session(bd):
                status = QuoteStatus.MISSING
            else:
                status = QuoteStatus.NON_SESSION
            return QuoteView(
                security_id, bd, ka, status, None, None, "", "", None, "none",
                candidates=resolved["candidates"],
                queried_code=reported_code or "",
                **self._identity_view_fields(identity_context),
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
            priority_rule_id=resolved["priority_rule_id"],
            queried_code=reported_code or "",
            **self._identity_view_fields(identity_context),
        )

    @staticmethod
    def _identity_view_fields(context: dict | None) -> dict:
        if not context:
            return {}
        return {
            "identity_status": context.get("status", ""),
            "identity_announcement_ids": context.get("announcement_ids", ()),
            "identity_candidates": context.get("candidates", ()),
            "identity_name": context.get("name", ""),
            "identity_source_rule": context.get("source_rule", ""),
        }

    def quote_by_code(
        self,
        code: str,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        priority_as_of: str | _dt.datetime | None = None,
        identity_as_of: str | _dt.datetime | None = None,
    ) -> QuoteView:
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        identity = self.resolve_code(
            code, bd, _parse_dt(identity_as_of) if identity_as_of is not None else ka)
        status_value = identity["status"]
        if status_value in {"confirmed", "direct"}:
            view = self.quote_at(
                identity["security_id"], bd, ka, priority_as_of,
                reported_code=code, identity_context=identity,
            )
            all_rows = self._visible_code_events(
                code, (identity["security_id"],), bd.isoformat(), ka
            )
            withdrawn = {
                (r["target_source"], r["target_record"]): r["source_record"]
                for r in all_rows if r["kind"] == "withdrawal"
            }
            raw_rows = [r for r in all_rows
                        if r["kind"] not in {"withdrawal", "suspension", "cancel"}]
            extra = tuple(
                SourceCandidate(
                    source=r["source"], source_record=r["source_record"], kind=r["kind"],
                    received_at=_parse_dt(r["received_at"]), close=r["close"],
                    active=False if r["security_id"] != identity["security_id"]
                    else (r["source"], r["source_record"]) not in withdrawn,
                    withdrawn_by=withdrawn.get((r["source"], r["source_record"]), ""),
                    security_id=r["security_id"], reported_code=r["reported_code"],
                )
                for r in raw_rows
            )
            known_keys = {
                (c.source, c.source_record) for c in view.candidates
            }
            merged = tuple(view.candidates) + tuple(
                c for c in extra if (c.source, c.source_record) not in known_keys
            )
            return dataclasses.replace(view, candidates=merged)

        rows = self._visible_code_events(code, identity["candidates"], bd.isoformat(), ka)
        withdrawn = {
            (r["target_source"], r["target_record"]): r["source_record"]
            for r in rows if r["kind"] == "withdrawal"
        }
        quote_rows = [r for r in rows if r["kind"] not in {"withdrawal", "suspension", "cancel"}]
        candidates = tuple(
            SourceCandidate(
                source=r["source"], source_record=r["source_record"], kind=r["kind"],
                received_at=_parse_dt(r["received_at"]), close=r["close"],
                active=(r["source"], r["source_record"]) not in withdrawn,
                withdrawn_by=withdrawn.get((r["source"], r["source_record"]), ""),
                security_id=r["security_id"], reported_code=r["reported_code"],
            )
            for r in quote_rows
        )
        saw_identity = bool(identity["candidates"])
        status = QuoteStatus.AMBIGUOUS if (saw_identity or candidates) else QuoteStatus.MISSING
        if not candidates and not saw_identity and not self.calendar.is_session(bd):
            status = QuoteStatus.NON_SESSION
        return QuoteView(
            "", bd, ka, status, None, None, "", "", None, "identity",
            candidates=candidates, selection_basis="identity-retained",
            queried_code=code,
            identity_status=status_value,
            identity_announcement_ids=identity["announcement_ids"],
            identity_candidates=identity["candidates"],
            identity_source_rule=identity["source_rule"],
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

    # ---------- 组合研究：身份解析、去重与调整对照 ----------

    @staticmethod
    def _position(position) -> PortfolioPosition:
        if isinstance(position, PortfolioPosition):
            return position
        if isinstance(position, str):
            return PortfolioPosition(position)
        if isinstance(position, dict):
            return PortfolioPosition(
                position["code"],
                position.get("security_id", ""),
                position.get("quantity", 1.0),
            )
        code, security_id, quantity = position
        return PortfolioPosition(code, security_id or "", quantity or 1.0)

    def research_portfolio(
        self,
        positions,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        identity_as_of: str | _dt.datetime | None = None,
    ) -> dict:
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        ia = _parse_dt(identity_as_of) if identity_as_of is not None else ka
        included: dict[str, dict] = {}
        excluded = []
        unresolved = []
        for raw_position in positions:
            pos = self._position(raw_position)
            if pos.security_id:
                identity = {
                    "status": "direct", "code": pos.code,
                    "security_id": pos.security_id,
                    "candidates": (pos.security_id,), "announcement_ids": (),
                    "name": self._security_name(pos.security_id),
                    "source_rule": "portfolio-supplied-security-id",
                }
                name_rows = [
                    r for r in self.conn.execute(
                        "SELECT * FROM identity_announcement WHERE security_id=?"
                        " AND effective_date <= ? AND received_at <= ?"
                        " ORDER BY received_at, seq",
                        (pos.security_id, bd.isoformat(), ia.isoformat()),
                    )
                ]
                withdrawn_ids = {
                    r["supersedes"] for r in name_rows
                    if r["action"] == "withdraw"
                }
                active_names = [
                    r for r in name_rows
                    if r["action"] == "confirm" and r["announcement_id"] not in withdrawn_ids
                ]
                name_row = active_names[-1] if active_names else None
                if name_row is not None:
                    identity["name"] = name_row["name"]
                    identity["announcement_ids"] = (name_row["announcement_id"],)
                    identity["source_rule"] = (
                        f"identity-announcement:{name_row['source']}/"
                        f"{name_row['source_record']}"
                    )
                view = self.quote_at(
                    pos.security_id, bd, ka, reported_code=None,
                    identity_context=identity,
                )
            else:
                view = self.quote_by_code(pos.code, bd, ka, identity_as_of=ia)
            entry = {
                "code": pos.code,
                "security_id": view.security_id or identity_security_id(view),
                "quantity": pos.quantity,
                "view": view,
                "name": view.identity_name or self._security_name(
                    view.security_id or identity_security_id(view)
                ),
                "source_rule": view.identity_source_rule,
                "announcement_ids": list(view.identity_announcement_ids),
            }
            if view.status == QuoteStatus.OK and entry["security_id"]:
                prior = included.get(entry["security_id"])
                if prior is None:
                    included[entry["security_id"]] = entry
                else:
                    prior["quantity"] += pos.quantity
                    prior["duplicate_codes"] = sorted(
                        set(prior.get("duplicate_codes", [prior["code"]])) | {pos.code}
                    )
            elif view.status == QuoteStatus.AMBIGUOUS:
                unresolved.append(entry)
            else:
                excluded.append(entry)
        rows = list(included.values())
        return {
            "business_date": bd,
            "known_at": ka,
            "identity_as_of": ia,
            "positions": tuple(rows),
            "unresolved": tuple(unresolved),
            "excluded": tuple(excluded),
            "security_ids": tuple(r["security_id"] for r in rows),
            "total_close_value": round(sum(
                r["quantity"] * r["view"].quote.close
                for r in rows if r["view"].quote and r["view"].quote.close is not None
            ), 6),
        }

    def _security_name(self, security_id: str) -> str:
        if not security_id:
            return ""
        row = self.conn.execute(
            "SELECT name FROM security WHERE security_id=?", (security_id,)
        ).fetchone()
        return row[0] if row else ""

    def compare_identity_adjustment(
        self,
        positions,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        revised_identity_as_of: str | _dt.datetime,
        revised_known_at: str | _dt.datetime | None = None,
    ) -> dict:
        original = self.research_portfolio(positions, business_date, known_at)
        revised = self.research_portfolio(
            positions, business_date,
            revised_known_at if revised_known_at is not None else known_at,
            identity_as_of=revised_identity_as_of,
        )
        before = {r["security_id"]: r for r in original["positions"]}
        after = {r["security_id"]: r for r in revised["positions"]}
        price_changes, name_changes, scope_changes = [], [], []
        for sid in sorted(set(before) & set(after)):
            old_close = before[sid]["view"].quote.close
            new_close = after[sid]["view"].quote.close
            if old_close != new_close:
                price_changes.append({
                    "security_id": sid, "before_close": old_close,
                    "after_close": new_close,
                    "change_type": "true-price-change",
                })
            elif before[sid]["name"] != after[sid]["name"]:
                name_changes.append({
                    "security_id": sid,
                    "before_name": before[sid]["name"],
                    "after_name": after[sid]["name"],
                    "change_type": "name-only",
                })
        for sid in sorted(set(after) - set(before)):
            scope_changes.append({"security_id": sid, "change": "added",
                                  "change_type": "security-range-change"})
        for sid in sorted(set(before) - set(after)):
            scope_changes.append({"security_id": sid, "change": "removed",
                                  "change_type": "security-range-change"})
        return {
            "business_date": original["business_date"],
            "original": original,
            "revised": revised,
            "price_changes": tuple(price_changes),
            "security_range_changes": tuple(scope_changes),
            "name_only_changes": tuple(name_changes),
        }

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
    ) -> list[dict]:
        """在多个历史视角（known_at 列表）下复算同一业务日的行情结论。

        每个视角都只依赖该时点之前实际收到的行情事件、撤回收达和裁决，
        不使用任何后知信息。
        """
        out = []
        for ka in viewpoints:
            view = self.quote_at(security_id, business_date, ka)
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
            "priority_rule_id": view.priority_rule_id,
            "identity_status": view.identity_status,
            "identity_announcement_ids": view.identity_announcement_ids,
            "identity_candidates": view.identity_candidates,
            "identity_name": view.identity_name,
            "identity_source_rule": view.identity_source_rule,
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
        identity_rows = list(self.conn.execute(
            "SELECT * FROM identity_announcement"
            " WHERE (security_id=? OR security_id IN ("
            " SELECT DISTINCT security_id FROM quote_event WHERE reported_code IN ("
            " SELECT code FROM identity_announcement WHERE security_id=?)))"
            " AND received_at > ? AND received_at <= ? ORDER BY received_at, seq",
            (security_id, security_id, s.isoformat(), e.isoformat()),
        ))
        seen_identity = set()
        for r in identity_rows:
            if r["announcement_id"] in seen_identity:
                continue
            seen_identity.add(r["announcement_id"])
            events.append({
                "type": "identity_announcement", "kind": r["action"],
                "announcement_id": r["announcement_id"],
                "code": r["code"], "security_id": r["security_id"],
                "effective_date": _dt.date.fromisoformat(r["effective_date"]),
                "received_at": _parse_dt(r["received_at"]),
                "source": r["source"], "source_record": r["source_record"],
                "reason": r["reason"], "supersedes": r["supersedes"],
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

    def information_timeline(
        self,
        security_id: str,
        business_date: str | _dt.date,
        until: str | _dt.datetime | None = None,
    ) -> list[dict]:
        """影响该业务日的全部信息按收到时间排列：价格、撤回、修订、
        停牌/撤销、拆分公告与优先级规则修订，各自何时可见一目了然。
        """
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        end = _parse_dt(until).isoformat() if until is not None else "9999"
        items: list[dict] = []
        for r in self.conn.execute(
            "SELECT * FROM quote_event"
            " WHERE security_id=? AND business_date=? AND received_at <= ?"
            " ORDER BY received_at, seq",
            (security_id, bd.isoformat(), end),
        ):
            items.append({
                "type": "event", "kind": r["kind"],
                "source": r["source"], "source_record": r["source_record"],
                "close": r["close"],
                "target": [r["target_source"], r["target_record"]]
                if r["kind"] == "withdrawal" else None,
                "received_at": _parse_dt(r["received_at"]),
                "reason": r["reason"],
            })
        for r in self.conn.execute(
            "SELECT * FROM corp_split"
            " WHERE security_id=? AND ex_date > ? AND received_at <= ?"
            " ORDER BY received_at",
            (security_id, bd.isoformat(), end),
        ):
            items.append({
                "type": "split", "kind": "split",
                "ex_date": _dt.date.fromisoformat(r["ex_date"]),
                "ratio": r["ratio"],
                "received_at": _parse_dt(r["received_at"]),
                "source": "", "source_record": "",
            })
        for r in self.conn.execute(
            "SELECT * FROM priority_rule WHERE received_at != ''"
            " AND received_at <= ? ORDER BY received_at, seq",
            (end,),
        ):
            items.append({
                "type": "priority_rule", "kind": "priority_rule",
                "rule_id": r["rule_id"],
                "priority": json.loads(r["priority"]),
                "supersedes": r["supersedes"],
                "received_at": _parse_dt(r["received_at"]),
                "reason": r["reason"],
                "source": "", "source_record": "",
            })
        for r in self.conn.execute(
            "SELECT * FROM identity_announcement WHERE effective_date <= ?"
            " AND received_at <= ? ORDER BY received_at, seq",
            (bd.isoformat(), end),
        ):
            items.append({
                "type": "identity_announcement", "kind": r["action"],
                "announcement_id": r["announcement_id"],
                "code": r["code"], "security_id": r["security_id"],
                "effective_date": _dt.date.fromisoformat(r["effective_date"]),
                "received_at": _parse_dt(r["received_at"]),
                "source": r["source"], "source_record": r["source_record"],
                "reason": r["reason"], "supersedes": r["supersedes"],
            })
        return sorted(items, key=lambda x: x["received_at"])

    def compare_rule_adjustment(
        self,
        security_id: str,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        rules_as_of: str | _dt.datetime | None = None,
    ) -> dict:
        """规则调整前后的研究对照。

        - original：保持原历史视角（数据与规则都截至 known_at）；
        - revised：同一研究时点、同样的数据可见性，但按 rules_as_of
          （缺省为最新规则）生效的优先级重新研究；
        - changed_fields：逐字段 before/after；
        - information_timeline：价格、撤回、修订与拆分公告各自何时可见。
        """
        ka = _parse_dt(known_at)
        ra = _parse_dt(rules_as_of) if rules_as_of is not None else None
        original = self.quote_at(security_id, business_date, ka)
        revised = self.quote_at(
            security_id, business_date, ka,
            priority_as_of=ra or _dt.datetime.max.replace(microsecond=0))
        sig_o, sig_r = self._view_signature(original), self._view_signature(revised)
        changed = {
            key: {"before": sig_o[key], "after": sig_r[key]}
            for key in sig_o
            if sig_o[key] != sig_r[key]
        }
        return {
            "security_id": security_id,
            "business_date": original.business_date,
            "known_at": ka,
            "rules_as_of": ra,
            "original": original,
            "revised": revised,
            "original_priority_rule_id": original.priority_rule_id,
            "revised_priority_rule_id": revised.priority_rule_id,
            "changed_fields": changed,
            "information_timeline": self.information_timeline(
                security_id, business_date, ra
            ),
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
        for table in ("security", "trading_session", "corp_split",
                      "quote_event", "adjudication", "priority_rule",
                      "identity_announcement"):
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
