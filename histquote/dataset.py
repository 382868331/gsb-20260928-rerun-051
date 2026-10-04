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
from .identity import (
    CorporateAction, HoldingLot, IdentityAnnouncement, IdentityResolution,
    Security, Split,
)
from .priority import PriorityRule
from .query import QuoteView, SourceCandidate
from .quote import CodeQuoteEvent, Quote, QuoteEvent, QuoteStatus

SCHEMA_VERSION = 5


def _parse_dt(value: str | _dt.datetime) -> _dt.datetime:
    return value if isinstance(value, _dt.datetime) else _dt.datetime.fromisoformat(value)


def _quote_to_tuple(q: Quote | None) -> tuple:
    if q is None:
        return (None, None, None, None, None)
    return (q.open, q.high, q.low, q.close, q.volume)


def _date_from_row(row: sqlite3.Row, key: str) -> _dt.date:
    return _dt.date.fromisoformat(row[key])


def _identity_source_rule(row: sqlite3.Row) -> dict:
    return {
        "source": row["source"],
        "source_record": row["source_record"],
        "announcement_id": row["announcement_id"],
        "action": row["action"],
        "received_at": _parse_dt(row["received_at"]),
        "effective_from": _dt.date.fromisoformat(row["effective_from"]),
        "effective_to": (
            _dt.date.fromisoformat(row["effective_to"])
            if row["effective_to"] else None
        ),
    }


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
CREATE TABLE IF NOT EXISTS corp_action(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL UNIQUE,
    family_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    received_at TEXT NOT NULL,
    from_security_id TEXT NOT NULL DEFAULT '',
    to_security_id TEXT NOT NULL DEFAULT '',
    from_ratio REAL NOT NULL,
    to_ratio REAL NOT NULL,
    cash_per_from_share REAL NOT NULL DEFAULT 0,
    cash_cost_portion REAL NOT NULL DEFAULT 0,
    fractional_cash_rate TEXT NOT NULL DEFAULT '',
    fractional_destination TEXT NOT NULL DEFAULT 'cash',
    announcement_kind TEXT NOT NULL,
    confirmation_status TEXT NOT NULL DEFAULT 'confirmed',
    source TEXT NOT NULL DEFAULT '',
    source_record TEXT NOT NULL DEFAULT '',
    supersedes TEXT NOT NULL DEFAULT '',
    target_action_id TEXT NOT NULL DEFAULT '',
    candidate_security_ids TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_corp_action_asof
    ON corp_action(family_id, effective_date, received_at, seq);
CREATE TABLE IF NOT EXISTS corp_action_adoption(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    research_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    adopted_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    selected_lots TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '',
    source_record TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(research_id, action_id)
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
    display_code TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT '',
    security_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    effective_from TEXT NOT NULL,
    effective_to TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    source_record TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    target_announcement_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_identity_code_asof
    ON identity_announcement(display_code, market, effective_from, received_at, seq);
CREATE TABLE IF NOT EXISTS code_quote_event(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    display_code TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT '',
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
    UNIQUE(display_code, market, business_date, source, source_record)
);
CREATE INDEX IF NOT EXISTS idx_code_quote_asof
    ON code_quote_event(display_code, market, business_date, received_at, seq);
CREATE TABLE IF NOT EXISTS saved_research(
    research_id TEXT PRIMARY KEY,
    saved_at TEXT NOT NULL,
    business_date TEXT NOT NULL,
    known_at TEXT NOT NULL,
    identity_as_of TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    research_kind TEXT NOT NULL DEFAULT 'portfolio'
);
"""

_EXTRA_COLUMNS = {
    "quote_event": {
        "target_source": "TEXT NOT NULL DEFAULT ''",
        "target_record": "TEXT NOT NULL DEFAULT ''",
    },
    "saved_research": {
        "research_kind": "TEXT NOT NULL DEFAULT 'portfolio'",
    },
    "corp_action": {
        "cash_cost_portion": "REAL NOT NULL DEFAULT 0",
    },
}

_DDL_V4_SECURITY_NO_UNIQUE = """
CREATE TABLE security_new(
    security_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL
);
INSERT INTO security_new(security_id, code, name)
SELECT security_id, code, name FROM security;
DROP TABLE security;
ALTER TABLE security_new RENAME TO security;
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
        security_sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='security'"
        ).fetchone()[0]
        if "UNIQUE" in security_sql.upper():
            self.conn.executescript(_DDL_V4_SECURITY_NO_UNIQUE)
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
            "INSERT OR IGNORE INTO security(security_id, code, name) VALUES (?,?,?)",
            (security.security_id, security.code, security.name),
        )
        self.conn.commit()

    def securities(self) -> list[Security]:
        rows = self.conn.execute(
            "SELECT security_id, code, name FROM security ORDER BY security_id"
        )
        return [Security(*r) for r in rows]

    # ---------- 证券身份公告（append-only，带业务有效期与收到时间） ----------

    def add_identity_announcement(
        self, announcement: IdentityAnnouncement
    ) -> tuple[bool, int | None]:
        """追加身份确认、候选、改名或撤回公告。

        公告永不覆盖；撤回通过 target_announcement_id 精确指向旧公告。
        """
        aid = announcement.announcement_id or f"_auto_identity_{uuid.uuid4().hex}"
        if announcement.announcement_id:
            existing = self.conn.execute(
                "SELECT seq FROM identity_announcement WHERE announcement_id=?",
                (announcement.announcement_id,),
            ).fetchone()
            if existing is not None:
                return False, existing["seq"]
        if announcement.source and announcement.source_record:
            existing = self.conn.execute(
                "SELECT seq FROM identity_announcement WHERE source=? AND source_record=?",
                (announcement.source, announcement.source_record),
            ).fetchone()
            if existing is not None:
                return False, existing["seq"]
        cur = self.conn.execute(
            "INSERT INTO identity_announcement(announcement_id, display_code, market,"
            " security_id, name, effective_from, effective_to, action, received_at,"
            " source, source_record, reason, target_announcement_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                aid, announcement.display_code, announcement.market,
                announcement.security_id, announcement.name,
                announcement.effective_from.isoformat(),
                announcement.effective_to.isoformat() if announcement.effective_to else "",
                announcement.action, announcement.received_at.isoformat(),
                announcement.source, announcement.source_record,
                announcement.reason, announcement.target_announcement_id,
            ),
        )
        self.conn.commit()
        return True, cur.lastrowid

    def identity_announcements(self) -> list[IdentityAnnouncement]:
        rows = self.conn.execute(
            "SELECT * FROM identity_announcement ORDER BY seq"
        )
        return [self._identity_from_row(r) for r in rows]

    @staticmethod
    def _identity_from_row(row: sqlite3.Row) -> IdentityAnnouncement:
        return IdentityAnnouncement(
            display_code=row["display_code"],
            security_id=row["security_id"],
            effective_from=_dt.date.fromisoformat(row["effective_from"]),
            received_at=_parse_dt(row["received_at"]),
            action=row["action"],
            market=row["market"],
            name=row["name"],
            effective_to=(
                _dt.date.fromisoformat(row["effective_to"])
                if row["effective_to"] else None
            ),
            source=row["source"],
            source_record=row["source_record"],
            announcement_id=row["announcement_id"],
            reason=row["reason"],
            target_announcement_id=row["target_announcement_id"],
        )

    def identity_at(
        self,
        display_code: str,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        market: str = "",
    ) -> IdentityResolution:
        """按业务日和公告收到时间解释显示代码，不使用迟到公告或撤回后的公告。"""
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        rows = list(self.conn.execute(
            "SELECT * FROM identity_announcement"
            " WHERE display_code=? AND market=? AND received_at <= ?"
            " ORDER BY received_at, seq",
            (display_code, market, ka.isoformat()),
        ))
        by_id = {r["announcement_id"]: r for r in rows}
        withdrawn: dict[str, sqlite3.Row] = {}
        for row in rows:
            if row["action"] == "withdraw":
                target = row["target_announcement_id"]
                if target in by_id:
                    withdrawn[target] = row

        effective = [
            r for r in rows
            if r["action"] in {"confirm", "rename", "candidate"}
            and _date_from_row(r, "effective_from") <= bd
            and (not r["effective_to"]
                 or _dt.date.fromisoformat(r["effective_to"]) >= bd)
            and r["announcement_id"] not in withdrawn
        ]
        removed = [
            r for r in rows
            if r["announcement_id"] in withdrawn
            and _date_from_row(r, "effective_from") <= bd
            and (not r["effective_to"]
                 or _dt.date.fromisoformat(r["effective_to"]) >= bd)
        ]

        latest_by_security = {}
        for row in effective:
            latest_by_security[row["security_id"]] = row
        relevant = sorted(
            latest_by_security.values(),
            key=lambda r: (r["received_at"], r["seq"]),
            reverse=True,
        )
        confirms = [r for r in relevant if r["action"] in {"confirm", "rename"}]
        candidates = [r for r in relevant if r["action"] == "candidate"]
        active = [self._identity_from_row(r) for r in relevant]
        removed_items = [self._identity_from_row(r) for r in removed]
        withdrawal_items = [self._identity_from_row(withdrawn[r["announcement_id"]])
                            for r in removed]

        if len(confirms) == 1:
            row = confirms[0]
            status = "confirmed"
            security_id = row["security_id"]
            basis = "rename-announcement" if row["action"] == "rename" else "identity-announcement"
        elif len(confirms) > 1:
            status, security_id, row = "ambiguous", "", None
            basis = "multiple-confirmed-identities"
        elif candidates:
            status, security_id, row = "candidate", "", None
            basis = "insufficient-identity-information"
        else:
            status, security_id, row = "unknown", "", None
            basis = "no-effective-identity-announcement"

        source_rules = tuple(
            _identity_source_rule(r)
            for r in sorted(effective + removed,
                            key=lambda x: (x["received_at"], x["seq"]))
        )
        return IdentityResolution(
            display_code=display_code,
            market=market,
            business_date=bd,
            known_at=ka,
            status=status,
            security_id=security_id,
            name=row["name"] if row is not None else "",
            candidate_security_ids=tuple(r["security_id"] for r in candidates),
            announcement_ids=tuple(r["announcement_id"] for r in relevant),
            withdrawn_announcement_ids=tuple(r["announcement_id"] for r in removed),
            basis=basis,
            announcements=tuple(active),
            withdrawn_announcements=tuple(removed_items + withdrawal_items),
            source_rules=source_rules,
        )

    def add_split(self, split: Split) -> None:
        action_id = (
            f"split:{split.security_id}:{split.ex_date.isoformat()}"
        )
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
        self.add_corporate_action(CorporateAction(
            action_id=action_id,
            family_id=action_id,
            action_type="split",
            effective_date=split.ex_date,
            received_at=split.received_at,
            from_security_id=split.security_id,
            to_security_id=split.security_id,
            from_ratio=1.0,
            to_ratio=split.ratio,
            announcement_kind="revision",
        ))
        self.conn.commit()

    # ---------- 公司行动（版本化公告 + 采用决策） ----------

    def add_corporate_action(
        self, action: CorporateAction
    ) -> tuple[bool, int | None]:
        """追加预告、修订或撤回；同一来源记录重放幂等，冲突时报错。"""
        row = self.conn.execute(
            "SELECT seq, action_type, effective_date, received_at, from_security_id,"
            " to_security_id, from_ratio, to_ratio, cash_per_from_share,"
            " cash_cost_portion, fractional_cash_rate, fractional_destination,"
            " announcement_kind, confirmation_status, source, source_record,"
            " supersedes, target_action_id, candidate_security_ids, reason"
            " FROM corp_action WHERE action_id=?",
            (action.action_id,),
        ).fetchone()
        if row is not None:
            if self._corp_action_content_tuple(action) != tuple(row)[1:]:
                raise ValueError(
                    f"公司行动 {action.action_id} 已存在且内容不同，禁止覆盖"
                )
            return False, row["seq"]
        if action.source and action.source_record:
            dup = self.conn.execute(
                "SELECT seq FROM corp_action WHERE source=? AND source_record=?",
                (action.source, action.source_record),
            ).fetchone()
            if dup is not None:
                return False, dup["seq"]
        cur = self.conn.execute(
            "INSERT INTO corp_action(action_id, family_id, action_type, effective_date,"
            " received_at, from_security_id, to_security_id, from_ratio, to_ratio,"
            " cash_per_from_share, cash_cost_portion, fractional_cash_rate,"
            " fractional_destination, announcement_kind, confirmation_status,"
            " source, source_record, supersedes, target_action_id,"
            " candidate_security_ids, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self._corp_action_insert_tuple(action),
        )
        self.conn.commit()
        return True, cur.lastrowid

    @staticmethod
    def _corp_action_content_tuple(action: CorporateAction) -> tuple:
        return (
            action.action_type, action.effective_date.isoformat(),
            action.received_at.isoformat(), action.from_security_id,
            action.to_security_id, action.from_ratio, action.to_ratio,
            action.cash_per_from_share, action.cash_cost_portion,
            "" if action.fractional_cash_rate is None else str(action.fractional_cash_rate),
            action.fractional_destination, action.announcement_kind,
            action.confirmation_status, action.source, action.source_record,
            action.supersedes, action.target_action_id,
            json.dumps(action.candidate_security_ids, ensure_ascii=False),
            action.reason,
        )

    def _corp_action_insert_tuple(self, action: CorporateAction) -> tuple:
        return (
            action.action_id or f"_auto_ca_{uuid.uuid4().hex}",
            action.family_id or action.action_id, action.action_type,
            action.effective_date.isoformat(), action.received_at.isoformat(),
            action.from_security_id, action.to_security_id,
            action.from_ratio, action.to_ratio, action.cash_per_from_share,
            action.cash_cost_portion,
            "" if action.fractional_cash_rate is None else str(action.fractional_cash_rate),
            action.fractional_destination, action.announcement_kind,
            action.confirmation_status, action.source, action.source_record,
            action.supersedes, action.target_action_id,
            json.dumps(list(action.candidate_security_ids), ensure_ascii=False),
            action.reason,
        )

    def corporate_actions(
        self, family_id: str | None = None,
        known_at: str | _dt.datetime | None = None,
    ) -> list[CorporateAction]:
        sql, args = "SELECT * FROM corp_action", []
        clauses = []
        if family_id is not None:
            clauses.append("family_id=?")
            args.append(family_id)
        if known_at is not None:
            clauses.append("received_at <= ?")
            args.append(_parse_dt(known_at).isoformat())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY effective_date, received_at, seq"
        return [self._corp_action_from_row(r)
                for r in self.conn.execute(sql, args)]

    @staticmethod
    def _corp_action_from_row(row: sqlite3.Row) -> CorporateAction:
        rate_text = row["fractional_cash_rate"]
        return CorporateAction(
            action_id=row["action_id"], family_id=row["family_id"],
            action_type=row["action_type"],
            effective_date=_dt.date.fromisoformat(row["effective_date"]),
            received_at=_parse_dt(row["received_at"]),
            from_security_id=row["from_security_id"],
            to_security_id=row["to_security_id"],
            from_ratio=row["from_ratio"], to_ratio=row["to_ratio"],
            cash_per_from_share=row["cash_per_from_share"],
            cash_cost_portion=row["cash_cost_portion"],
            fractional_cash_rate=float(rate_text) if rate_text else None,
            fractional_destination=row["fractional_destination"],
            announcement_kind=row["announcement_kind"],
            confirmation_status=row["confirmation_status"],
            source=row["source"], source_record=row["source_record"],
            supersedes=row["supersedes"],
            target_action_id=row["target_action_id"],
            candidate_security_ids=tuple(json.loads(row["candidate_security_ids"] or "[]")),
            reason=row["reason"], sequence=row["seq"],
        )

    def corporate_action_scenario(
        self,
        family_id: str,
        known_at: str | _dt.datetime,
        action_id: str = "",
        as_of_date: str | _dt.date | None = None,
    ) -> dict:
        """返回某 family 在指定视角下的公告状态；可显式选择旧公告做方案对照。"""
        ka = _parse_dt(known_at)
        versions = self.corporate_actions(family_id, ka)
        if not versions:
            return {"family_id": family_id, "status": "unknown",
                    "action": None, "versions": (), "withdrawn_by": ""}
        withdrawn_targets = {
            a.target_action_id: a for a in versions if a.announcement_kind == "withdrawal"
        }
        substantive = [a for a in versions if a.announcement_kind != "withdrawal"]
        if action_id:
            chosen = next((a for a in substantive if a.action_id == action_id), None)
            if chosen is None:
                raise ValueError(f"指定视角下不可见公司行动: {action_id}")
            if as_of_date is not None and chosen.effective_date > _dt.date.fromisoformat(
                    str(as_of_date)):
                return {"family_id": family_id, "status": "not-effective",
                        "action": chosen, "versions": tuple(versions),
                        "withdrawn_by": ""}
            withdrawal = withdrawn_targets.get(chosen.action_id)
            return {
                "family_id": family_id,
                "status": "withdrawn" if withdrawal else "selected",
                "action": chosen, "versions": tuple(versions),
                "withdrawn_by": withdrawal.action_id if withdrawal else "",
            }
        visible = [a for a in substantive
                   if as_of_date is None or a.effective_date <= _dt.date.fromisoformat(str(as_of_date))]
        if not visible:
            return {"family_id": family_id, "status": "not-effective",
                    "action": None, "versions": tuple(versions), "withdrawn_by": ""}
        chosen = max(visible, key=lambda a: (a.received_at, a.sequence))
        withdrawal = withdrawn_targets.get(chosen.action_id)
        return {
            "family_id": family_id,
            "status": "withdrawn" if withdrawal
            else ("preview" if chosen.announcement_kind == "preview" else "active"),
            "action": chosen if not withdrawal else None,
            "versions": tuple(versions),
            "withdrawn_by": withdrawal.action_id if withdrawal else "",
        }

    @staticmethod
    def _holding_lot(item, index: int) -> dict:
        if isinstance(item, HoldingLot):
            lot = item
        elif isinstance(item, dict):
            lot = HoldingLot(
                lot_id=str(item.get("lot_id") or item.get("transaction_id")
                             or f"lot-{index}"),
                security_id=item.get("security_id", ""),
                quantity=float(item.get("quantity", item.get("shares", 0))),
                cost=float(item.get("cost", item.get("cost_basis", 0))),
                trade_date=item.get("trade_date"),
                display_code=item.get("display_code", item.get("code", "")),
                market=item.get("market", ""),
                source=item.get("source", ""),
                source_record=item.get("source_record", ""),
            )
        else:
            raise TypeError("持仓必须是 HoldingLot 或 dict")
        return dataclasses.asdict(lot)

    def apply_corporate_actions(
        self,
        holdings,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        selected_action_ids: dict[str, str] | list[str] | tuple[str, ...] | None = None,
        selected_quantities: dict[str, float] | None = None,
        include_previews: bool = False,
        research_id: str = "",
        adopted_at: str | _dt.datetime | None = None,
        persist_adoption: bool = False,
        allow_withdrawn: bool = False,
    ) -> dict:
        """按当时可见行动重放持仓；显式选择旧/修订公告时可做方案对照。"""
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        selected_quantities = selected_quantities or {}
        lots = [self._holding_lot(h, i) for i, h in enumerate(holdings)]
        for lot in lots:
            lot.update({
                "remaining_quantity": lot["quantity"],
                "remaining_cost": lot["cost"],
                "lineage": [lot["lot_id"]],
                "origin_lot_id": lot["lot_id"],
                "created_by_action_id": "",
                "selection_locked_family": "",
            })
        adjustments: list[dict] = []
        undetermined: list[dict] = []

        wanted = self._selected_action_map(selected_action_ids)
        actions = self._actions_for_application(
            bd, ka, wanted, include_previews
        )
        explicit = bool(wanted)
        if explicit and any(s["status"] == "withdrawn" for s in actions):
            withdrawn = next(s for s in actions if s["status"] == "withdrawn")
            if not allow_withdrawn:
                raise ValueError(
                    f"公司行动已撤回，不能作为新决策采用: {withdrawn['action'].action_id}"
                )
        for scenario in actions:
            action = scenario["action"]
            current = [lot for lot in lots
                       if lot["security_id"] == action.from_security_id
                       and lot["remaining_quantity"] > 0]
            for lot in current:
                if (lot.get("selection_locked_family")
                        and lot["selection_locked_family"] != action.family_id):
                    continue
                available = lot["remaining_quantity"]
                requested = self._selected_quantity(
                    selected_quantities, action, lot, available)
                chosen_qty = max(0.0, min(float(requested), available))
                if chosen_qty <= 0:
                    continue
                action_selection = selected_quantities.get(action.action_id)
                family_selection = selected_quantities.get(action.family_id)
                explicit_selection = (
                    isinstance(action_selection, dict)
                    and lot["lot_id"] in action_selection
                ) or (
                    isinstance(family_selection, dict)
                    and lot["lot_id"] in family_selection
                )
                if explicit_selection and chosen_qty < available:
                    lot["selection_locked_family"] = action.family_id
                if action.confirmation_status == "candidate":
                    undetermined.append({
                        "lot_id": lot["lot_id"],
                        "security_id": action.from_security_id,
                        "quantity": chosen_qty,
                        "cost": lot["remaining_cost"] * chosen_qty / available,
                        "action_id": action.action_id,
                        "family_id": action.family_id,
                        "candidate_security_ids": action.candidate_security_ids,
                        "reason": "公司行动新证券身份未确认，不猜测现有证券",
                    })
                    continue
                adjustment = self._apply_one_action(lot, action, chosen_qty, available)
                if adjustment.get("fraction_undetermined"):
                    undetermined.append(adjustment["fraction_undetermined"])
                lot["remaining_quantity"] = round(available - chosen_qty, 12)
                lot["remaining_cost"] = round(
                    lot["remaining_cost"] - adjustment["consumed_cost"], 12
                )
                if adjustment["output_lot"] is not None:
                    lots.append(adjustment["output_lot"])
                adjustments.append(adjustment)

        positions = self._corporate_action_positions(lots)
        cash_items = [a["cash"] for a in adjustments if a["cash"]]
        result = {
            "business_date": bd,
            "known_at": ka,
            "action_ids": tuple(a["action"].action_id for a in actions),
            "actions": tuple(a["action"] for a in actions),
            "original_lots": tuple(self._holding_lot(h, i)
                                    for i, h in enumerate(holdings)),
            "lots": tuple(lots),
            "positions": positions,
            "adjustments": tuple(adjustments),
            "cash_compensations": tuple(cash_items),
            "cash_total": round(sum(i["amount"] for i in cash_items), 6),
            "undetermined": tuple(undetermined),
            "investment_gain_total": 0.0,
            "investment_gain_note": "代码变化、换股和公司行动价格跳变均不确认为投资收益",
        }
        if persist_adoption:
            if not research_id:
                raise ValueError("持久化采用决策必须提供 research_id")
            self._save_corporate_adoption(
                research_id, actions, selected_quantities, ka,
                adopted_at or ka,
            )
        return result

    @staticmethod
    def _selected_action_map(selected) -> dict[str, str]:
        if selected is None:
            return {}
        if isinstance(selected, dict):
            return dict(selected)
        return {"": list(selected)}

    def _actions_for_application(self, bd, ka, wanted, include_previews):
        if wanted and any(isinstance(v, (list, tuple)) for v in wanted.values()):
            ids = next(v for v in wanted.values() if isinstance(v, (list, tuple)))
            scenarios = []
            for aid in ids:
                row = self.conn.execute(
                    "SELECT family_id FROM corp_action"
                    " WHERE action_id=? AND received_at <= ?",
                    (aid, ka.isoformat()),
                ).fetchone()
                if row is None:
                    raise ValueError(f"指定视角下不可见公司行动: {aid}")
                scenarios.append(self.corporate_action_scenario(
                    row["family_id"], ka, aid, bd))
        elif wanted:
            scenarios = [self.corporate_action_scenario(family, ka, aid, bd)
                         for family, aid in wanted.items()]
        else:
            visible = self.conn.execute(
                "SELECT DISTINCT family_id FROM corp_action"
                " WHERE received_at <= ? AND effective_date <= ? ORDER BY family_id",
                (ka.isoformat(), bd.isoformat()),
            )
            scenarios = []
            for row in visible:
                scenario = self.corporate_action_scenario(row[0], ka, "", bd)
                if scenario["action"] and (
                        include_previews
                        or scenario["status"] == "active"):
                    scenarios.append(scenario)
        return sorted(scenarios, key=lambda s: (
            s["action"].effective_date, s["action"].received_at,
            s["action"].sequence,
        ))

    @staticmethod
    def _selected_quantity(selected, action, lot, available):
        if action.action_id in selected:
            action_selection = selected[action.action_id]
            if isinstance(action_selection, dict):
                return float(action_selection.get(lot["lot_id"], 0.0))
        if action.family_id in selected:
            family_selection = selected[action.family_id]
            if isinstance(family_selection, dict):
                return float(family_selection.get(lot["lot_id"], 0.0))
        return float(selected.get(lot["lot_id"], available))

    def _apply_one_action(self, lot, action, chosen_qty, available):
        consumed_cost = round(lot["remaining_cost"] * chosen_qty / available, 12)
        gross_new = chosen_qty * action.to_ratio / action.from_ratio
        whole = int(gross_new)
        fraction = round(gross_new - whole, 12)
        cash_comp = action.cash_per_from_share * chosen_qty
        cash_cost = consumed_cost * action.cash_cost_portion
        security_cost = consumed_cost - cash_cost
        fraction_cash = 0.0
        fraction_cost = 0.0
        fraction_status = "none" if fraction == 0 else action.fractional_destination
        output_qty = float(whole)
        if fraction > 0:
            fraction_cost = security_cost * fraction / gross_new
            if action.fractional_destination == "cash":
                rate = action.fractional_cash_rate
                if rate is None:
                    fraction_status = "undetermined"
                else:
                    fraction_cash = fraction * rate
                    cash_cost += fraction_cost
                    security_cost -= fraction_cost
            elif action.fractional_destination == "retain":
                output_qty = gross_new
                fraction_cost = 0.0
            else:
                fraction_status = "undetermined"
        target_id = action.from_security_id if action.action_type == "split" else action.to_security_id
        fraction_undetermined = None
        if fraction > 0 and fraction_status == "undetermined":
            fraction_undetermined = {
                "lot_id": lot["lot_id"],
                "security_id": "",
                "quantity": fraction,
                "cost": fraction_cost,
                "action_id": action.action_id,
                "family_id": action.family_id,
                "candidate_security_ids": (),
                "reason": "零碎份额去向或作价未确定",
            }
            security_cost -= fraction_cost
        output = None
        if output_qty > 0 and target_id:
            output = {
                "lot_id": f"{lot['lot_id']}:{action.action_id}",
                "security_id": target_id,
                "quantity": round(output_qty, 12),
                "cost": round(security_cost if fraction_status != "retain"
                               else consumed_cost - cash_cost, 12),
                "remaining_quantity": round(output_qty, 12),
                "remaining_cost": round(security_cost if fraction_status != "retain"
                                         else consumed_cost - cash_cost, 12),
                "trade_date": action.effective_date.isoformat(),
                "display_code": "", "market": "",
                "source": action.source, "source_record": action.source_record,
                "lineage": lot["lineage"] + [action.action_id,
                                              f"{lot['lot_id']}:{action.action_id}"],
                "origin_lot_id": lot["origin_lot_id"],
                "created_by_action_id": action.action_id,
                "selection_locked_family": "",
            }
        cash = None
        if cash_comp > 0 or fraction_cash > 0:
            cash = {
                "source_lot_id": lot["lot_id"],
                "action_id": action.action_id,
                "family_id": action.family_id,
                "compensation": round(cash_comp, 6),
                "fractional_cash": round(fraction_cash, 6),
                "amount": round(cash_comp + fraction_cash, 6),
                "allocated_cost": round(cash_cost, 6),
                "investment_gain": None,
            }
        return {
            "action_id": action.action_id,
            "family_id": action.family_id,
            "action_type": action.action_type,
            "source_lot_id": lot["lot_id"],
            "source_security_id": action.from_security_id,
            "output_security_id": target_id,
            "input_quantity": round(chosen_qty, 12),
            "gross_new_quantity": round(gross_new, 12),
            "whole_quantity": output_qty,
            "fractional_quantity": fraction,
            "fractional_destination": fraction_status,
            "consumed_cost": consumed_cost,
            "new_security_cost": round(security_cost if fraction_status != "retain"
                                      else consumed_cost, 6),
            "output_lot": output,
            "cash": cash,
            "lineage": lot["lineage"] + [action.action_id],
            "code_change_is_quantity_change": False,
            "investment_gain": None,
            "fraction_undetermined": fraction_undetermined,
        }

    @staticmethod
    def _corporate_action_positions(lots):
        grouped = {}
        for lot in lots:
            qty = lot["remaining_quantity"]
            if qty <= 0:
                continue
            item = grouped.setdefault(lot["security_id"], {
                "security_id": lot["security_id"], "quantity": 0.0,
                "cost": 0.0, "lot_ids": []
            })
            item["quantity"] += qty
            item["cost"] += lot["remaining_cost"]
            item["lot_ids"].append(lot["lot_id"])
        return tuple(dict(item, quantity=round(item["quantity"], 8),
                          cost=round(item["cost"], 8))
                     for item in sorted(grouped.values(),
                                       key=lambda x: x["security_id"]))

    def _save_corporate_adoption(self, research_id, scenarios, selected_lots,
                                 known_at, adopted_at):
        for scenario in scenarios:
            action = scenario["action"]
            self.conn.execute(
                "INSERT OR IGNORE INTO corp_action_adoption(research_id, action_id,"
                " adopted_at, known_at, selected_lots, source, source_record, note)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (research_id, action.action_id, adopted_at.isoformat(),
                 known_at.isoformat(), json.dumps(selected_lots),
                 action.source, action.source_record, scenario["status"]),
            )
        self.conn.commit()

    def compare_corporate_action_scenarios(
        self,
        holdings,
        business_date,
        known_at,
        family_id: str,
        old_action_id: str,
        revised_action_id: str,
        selected_quantities: dict[str, float] | None = None,
    ) -> dict:
        return {
            "original": self.apply_corporate_actions(
                holdings, business_date, known_at,
                selected_action_ids={family_id: old_action_id},
                selected_quantities=selected_quantities,
                allow_withdrawn=True),
            "revised": self.apply_corporate_actions(
                holdings, business_date, known_at,
                selected_action_ids={family_id: revised_action_id},
                selected_quantities=selected_quantities,
                allow_withdrawn=True),
        }

    def save_corporate_action_research(
        self,
        research_id: str,
        holdings,
        business_date,
        known_at,
        saved_at,
        selected_action_ids=None,
        selected_quantities: dict[str, float] | None = None,
        include_previews: bool = False,
    ) -> dict:
        result = self.apply_corporate_actions(
            holdings, business_date, known_at, selected_action_ids,
            selected_quantities, include_previews, research_id=research_id,
            adopted_at=saved_at, persist_adoption=True,
        )
        payload = {
            "research_id": research_id,
            "holdings": [self._holding_lot(h, i) for i, h in enumerate(holdings)],
            "selected_action_ids": self._json_safe(selected_action_ids or {}),
            "selected_quantities": selected_quantities or {},
            "include_previews": include_previews,
            "result": self._json_safe(result),
        }
        self.conn.execute(
            "INSERT INTO saved_research(research_id, saved_at, business_date,"
            " known_at, identity_as_of, payload, research_kind)"
            " VALUES (?,?,?,?,?,?,?)",
            (research_id, _parse_dt(saved_at).isoformat(),
             result["business_date"].isoformat(), result["known_at"].isoformat(),
             result["known_at"].isoformat(),
             json.dumps(payload, ensure_ascii=False, default=_json_default),
             "corporate_action"),
        )
        self.conn.commit()
        return result

    def corporate_action_adoptions(self, research_id: str) -> tuple[dict, ...]:
        rows = self.conn.execute(
            "SELECT * FROM corp_action_adoption WHERE research_id=? ORDER BY seq",
            (research_id,),
        )
        return tuple({
            "research_id": r["research_id"],
            "action_id": r["action_id"],
            "adopted_at": _parse_dt(r["adopted_at"]),
            "known_at": _parse_dt(r["known_at"]),
            "selected_lots": json.loads(r["selected_lots"]),
            "source": r["source"],
            "source_record": r["source_record"],
            "status": r["note"],
        } for r in rows)

    def corporate_action_adjustments(self, research_id: str) -> tuple[dict, ...]:
        saved = self.saved_research(research_id)
        return tuple(saved["result"].get("adjustments", ()))

    def reopen_corporate_action_research(self, research_id: str) -> dict:
        saved = self.saved_research(research_id)
        if saved.get("research_kind") != "corporate_action" and "selected_action_ids" not in saved:
            raise ValueError(f"不是公司行动研究: {research_id}")
        reopened = self.apply_corporate_actions(
            [HoldingLot(**lot) for lot in saved["holdings"]],
            saved["business_date"], saved["known_at"],
            saved.get("selected_action_ids"),
            saved.get("selected_quantities"),
            saved.get("include_previews", False),
            allow_withdrawn=True,
        )
        old = saved["result"]
        return {
            "saved": saved,
            "reopened": reopened,
            "same_positions": tuple(old["positions"]) == reopened["positions"],
            "same_adjustments": self._adjustment_signatures(old)
            == self._adjustment_signatures(reopened),
            "same_cash_total": old["cash_total"] == reopened["cash_total"],
            "same_undetermined": tuple(old["undetermined"]) == reopened["undetermined"],
            "adoptions": self.corporate_action_adoptions(research_id),
        }

    @staticmethod
    def _adjustment_signatures(result) -> tuple:
        return tuple(
            (a["action_id"], a["source_lot_id"], a["input_quantity"],
             a["output_security_id"], a["whole_quantity"],
             a["fractional_quantity"], a["fractional_destination"],
             a["consumed_cost"], a["new_security_cost"], tuple(a["lineage"]))
            for a in result.get("adjustments", ())
        )

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

    def add_code_event(self, event: CodeQuoteEvent) -> tuple[bool, int | None]:
        """写入按显示代码收到的行情，绝不依据当前代码表自动猜测证券。"""
        h = self._code_content_hash(event)
        existing = self.conn.execute(
            "SELECT seq, content_hash FROM code_quote_event"
            " WHERE display_code=? AND market=? AND business_date=?"
            " AND source=? AND source_record=?",
            (
                event.display_code, event.market,
                event.business_date.isoformat(),
                event.source, event.source_record,
            ),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != h:
                raise ValueError(
                    f"来源记录 {event.source}/{event.source_record} 内容冲突，"
                    f"({event.display_code}/{event.market}, {event.business_date}) "
                    "已存在不同数据"
                )
            return False, existing["seq"]
        o, hi, lo, c, v = _quote_to_tuple(event.quote)
        cur = self.conn.execute(
            "INSERT INTO code_quote_event(display_code, market, business_date,"
            " received_at, kind, open, high, low, close, volume, reason, source,"
            " source_record, target_source, target_record, content_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.display_code, event.market, event.business_date.isoformat(),
                event.received_at.isoformat(), event.kind, o, hi, lo, c, v,
                event.reason, event.source, event.source_record,
                event.target_source, event.target_record, h,
            ),
        )
        self.conn.commit()
        return True, cur.lastrowid

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

    @staticmethod
    def _code_content_hash(event: CodeQuoteEvent) -> str:
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

    def _visible_code_events(
        self,
        display_code: str,
        market: str,
        business_date: str,
        known_at: _dt.datetime,
    ) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM code_quote_event"
            " WHERE display_code=? AND market=? AND business_date=?"
            " AND received_at <= ? ORDER BY received_at, seq",
            (display_code, market, business_date, known_at.isoformat()),
        ))

    @staticmethod
    def _route_code_rows(
        rows: list[sqlite3.Row], security_id: str
    ) -> list[sqlite3.Row]:
        """把按代码收到的候选转换为裁决器内部使用的行，原行不复制不覆盖。"""
        out = []
        for row in rows:
            item = dict(row)
            item["security_id"] = security_id
            item["event_origin"] = "display_code"
            out.append(item)
        return out

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
        routed_code_rows: list[dict] | None = None,
    ) -> dict:
        """合并截至 known_at 可见的全部事件，输出候选与裁决结果。

        - 每条来源原始/修订记录都保留为候选，并标注是否已被其来源撤回；
        - revision 是对事实的更正：最新一条未撤回的修订优先于 raw；
        - 多条 raw 冲突时，优先采用可见裁决指向的记录，否则按来源优先级；
        - suspension / cancel 对全天生效，时间晚于被选行情记录时覆盖行情状态。
        """
        bd_s = bd.isoformat()
        priority, priority_rule_id = self._priority_at(priority_known_at or known_at)
        direct_rows = [dict(r) for r in self._visible_events(security_id, bd_s, known_at)]
        rows = direct_rows + (routed_code_rows or [])
        rows.sort(key=lambda r: (r["received_at"], r["seq"]))

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
        identity: IdentityResolution | None = None,
        routed_code_rows: list[dict] | None = None,
    ) -> QuoteView:
        """as-of 查询：在 known_at 时点，回看 business_date 当天可知的行情。

        priority_as_of 可单独指定优先级规则的生效视角（用于“按新规则
        重新研究同一历史时点”的对照）；缺省时与 known_at 一致，即
        今天的偏好不会改写过去已经能看到的结果。
        """
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        pa = _parse_dt(priority_as_of) if priority_as_of is not None else None
        resolved = self._resolve(security_id, bd, ka, pa, routed_code_rows)
        identity_fields = {
            "display_code": identity.display_code if identity else "",
            "market": identity.market if identity else "",
            "identity_status": identity.status if identity else "",
            "identity_announcement_ids": identity.announcement_ids if identity else (),
            "identity_withdrawn_announcement_ids": (
                identity.withdrawn_announcement_ids if identity else ()
            ),
            "identity_basis": identity.basis if identity else "",
            "identity_candidates": identity.candidate_security_ids if identity else (),
            "identity_source_rules": identity.source_rules if identity else (),
            "identity_name": identity.name if identity else "",
        }
        chosen = resolved["chosen"]
        status_events = resolved["status_events"]

        saw_records = resolved["saw_records"]
        if chosen is None and not status_events:
            if saw_records:
                status = QuoteStatus.WITHDRAWN
                return dataclasses.replace(QuoteView(
                    security_id, bd, ka, status, None, None, "", "", None, "withdrawal",
                    candidates=resolved["candidates"],
                    selection_basis=resolved["basis"],
                ), **identity_fields)
            if self.calendar.is_session(bd):
                status = QuoteStatus.MISSING
            else:
                status = QuoteStatus.NON_SESSION
            return dataclasses.replace(QuoteView(
                security_id, bd, ka, status, None, None, "", "", None, "none",
                candidates=resolved["candidates"],
            ), **identity_fields)

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

        return dataclasses.replace(QuoteView(
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
            **identity_fields
        ))

    def quote_for_code(
        self,
        display_code: str,
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        market: str = "",
        identity_as_of: str | _dt.datetime | None = None,
    ) -> QuoteView:
        """查询显示代码行情，并说明采用的证券身份、公告与来源规则。

        identity_as_of 用于身份映射调整前后对照；缺省时等于 known_at，
        即公告迟到和撤回均按原研究视角解释。
        """
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        ia = _parse_dt(identity_as_of) if identity_as_of is not None else ka
        identity = self.identity_at(display_code, bd, ia, market)
        routed = self._route_code_rows(
            self._visible_code_events(display_code, market, bd.isoformat(), ka),
            identity.security_id,
        )
        if identity.status != "confirmed":
            status = {
                "candidate": QuoteStatus.IDENTITY_CANDIDATE,
                "unknown": QuoteStatus.IDENTITY_UNKNOWN,
                "ambiguous": QuoteStatus.IDENTITY_AMBIGUOUS,
            }[identity.status]
            unresolved = self._resolve("", bd, ka, routed_code_rows=routed)
            return QuoteView(
                security_id="", business_date=bd, known_at=ka, status=status,
                quote=None, adjusted_close=None, source="", source_record="",
                received_at=None, event_kind="identity",
                candidates=unresolved["candidates"],
                display_code=display_code, market=market,
                identity_status=identity.status,
                identity_announcement_ids=identity.announcement_ids,
                identity_withdrawn_announcement_ids=identity.withdrawn_announcement_ids,
                identity_basis=identity.basis,
                identity_candidates=identity.candidate_security_ids,
                identity_source_rules=identity.source_rules,
            )
        return self.quote_at(
            identity.security_id, bd, ka, identity=identity,
            routed_code_rows=routed,
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

    # ---------- 组合研究与身份映射调整对照 ----------

    def evaluate_portfolio(
        self,
        holdings: list[dict],
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        identity_as_of: str | _dt.datetime | None = None,
    ) -> dict:
        """按某研究时点解释组合；同一稳定证券的多笔持仓只取一次行情。"""
        bd = business_date if isinstance(business_date, _dt.date) else _dt.date.fromisoformat(business_date)
        ka = _parse_dt(known_at)
        ia = _parse_dt(identity_as_of) if identity_as_of is not None else ka
        grouped: dict[str, dict] = {}
        unresolved = []

        for pos, holding in enumerate(holdings):
            weight = float(holding.get("weight", 1.0))
            label = holding.get("label", holding.get("display_code")
                              or holding.get("security_id", str(pos)))
            if holding.get("security_id"):
                sid = holding["security_id"]
                view = self.quote_at(sid, bd, ka)
                entry = self._portfolio_entry(label, sid, weight, view, holding, pos)
            else:
                code = holding.get("display_code") or holding.get("code")
                market = holding.get("market", "")
                view = self.quote_for_code(code, bd, ka, market, ia)
                entry = self._portfolio_entry(code or label, view.security_id,
                                              weight, view, holding, pos,
                                              market=market)
                if view.identity_status != "confirmed":
                    entry["resolved_security_id"] = ""
                    unresolved.append(entry)
                    continue
                sid = view.security_id

            if sid not in grouped:
                entry["included"] = True
                entry["weight"] = weight
                grouped[sid] = entry
            else:
                grouped[sid]["weight"] += weight
                grouped[sid]["duplicate_positions"].append({
                    "position": pos,
                    "label": label,
                    "weight": weight,
                })

        positions = sorted(grouped.values(), key=lambda x: x["position"])
        unresolved.sort(key=lambda x: x["position"])
        value = sum(
            p["weight"] * p["close"]
            for p in positions
            if p["close"] is not None
        )
        deployed_weight = sum(
            p["weight"] for p in positions if p["close"] is not None
        )
        return {
            "business_date": bd,
            "known_at": ka,
            "identity_as_of": ia,
            "positions": positions,
            "unresolved_positions": unresolved,
            "portfolio_value": round(value, 6),
            "deployed_weight": deployed_weight,
            "security_ids": tuple(p["security_id"] for p in positions),
        }

    @staticmethod
    def _portfolio_entry(label, sid, weight, view, holding, position,
                         market="") -> dict:
        return {
            "position": position,
            "label": label,
            "display_code": getattr(view, "display_code", "") or holding.get("display_code", ""),
            "market": market,
            "security_id": sid,
            "name": view.identity_name,
            "weight": weight,
            "close": view.quote.close if view.quote else None,
            "adjusted_close": view.adjusted_close,
            "status": view.status.value,
            "identity_status": view.identity_status,
            "identity_basis": view.identity_basis,
            "announcement_ids": list(view.identity_announcement_ids),
            "source": view.source,
            "source_record": view.source_record,
            "view": view,
            "included": False,
            "duplicate_positions": [],
        }

    def compare_identity_adjustment(
        self,
        holdings: list[dict],
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        revised_identity_as_of: str | _dt.datetime,
        original_identity_as_of: str | _dt.datetime | None = None,
        revised_known_at: str | _dt.datetime | None = None,
    ) -> dict:
        """对照身份映射调整前后，并归类价格、证券范围和名称变化。"""
        ka = _parse_dt(known_at)
        original_ia = _parse_dt(original_identity_as_of) if original_identity_as_of else ka
        revised_ia = _parse_dt(revised_identity_as_of)
        revised_ka = _parse_dt(revised_known_at) if revised_known_at else ka
        original = self.evaluate_portfolio(holdings, business_date, ka, original_ia)
        revised = self.evaluate_portfolio(holdings, business_date, revised_ka, revised_ia)

        old_by_pos = {p["position"]: p for p in original["positions"]
                      + original["unresolved_positions"]}
        new_by_pos = {p["position"]: p for p in revised["positions"]
                      + revised["unresolved_positions"]}
        changes = []
        for pos in sorted(set(old_by_pos) | set(new_by_pos)):
            before, after = old_by_pos.get(pos), new_by_pos.get(pos)
            kinds = self._classify_portfolio_change(before, after)
            if kinds:
                changes.append({
                    "position": pos,
                    "kinds": kinds,
                    "before": self._comparable_position(before),
                    "after": self._comparable_position(after),
                })

        old_ids = set(original["security_ids"])
        new_ids = set(revised["security_ids"])
        return {
            "business_date": original["business_date"],
            "original": original,
            "revised": revised,
            "changes": changes,
            "added_security_ids": tuple(sorted(new_ids - old_ids)),
            "removed_security_ids": tuple(sorted(old_ids - new_ids)),
            "original_value": original["portfolio_value"],
            "revised_value": revised["portfolio_value"],
            "value_delta": round(
                revised["portfolio_value"] - original["portfolio_value"], 6
            ),
        }

    @staticmethod
    def _classify_portfolio_change(before: dict | None, after: dict | None) -> list[str]:
        kinds: list[str] = []
        if before is None or after is None:
            return ["security-scope-change"]
        if (before.get("security_id") != after.get("security_id")
                or before.get("identity_status") != after.get("identity_status")):
            return ["security-scope-change"]
        if before.get("close") != after.get("close"):
            kinds.append("true-price-change")
        if (before.get("announcement_ids") != after.get("announcement_ids")
                or before.get("identity_basis") != after.get("identity_basis")):
            kinds.append("name-only-change")
        elif before.get("name") != after.get("name"):
            kinds.append("name-only-change")
        return kinds

    @staticmethod
    def _comparable_position(position: dict | None) -> dict | None:
        if position is None:
            return None
        return {key: value for key, value in position.items() if key != "view"}

    def save_portfolio_research(
        self,
        research_id: str,
        holdings: list[dict],
        business_date: str | _dt.date,
        known_at: str | _dt.datetime,
        saved_at: str | _dt.datetime,
        identity_as_of: str | _dt.datetime | None = None,
    ) -> dict:
        """保存组合研究及其当时身份解释；后续公告不改写该研究。"""
        result = self.evaluate_portfolio(holdings, business_date, known_at, identity_as_of)
        payload = {
            "research_id": research_id,
            "holdings": holdings,
            "result": self._json_safe(result),
        }
        self.conn.execute(
            "INSERT INTO saved_research(research_id, saved_at,"
            " business_date, known_at, identity_as_of, payload)"
            " VALUES (?,?,?,?,?,?)",
            (
                research_id,
                _parse_dt(saved_at).isoformat(),
                result["business_date"].isoformat(),
                result["known_at"].isoformat(),
                result["identity_as_of"].isoformat(),
                json.dumps(payload, ensure_ascii=False, default=_json_default),
            ),
        )
        self.conn.commit()
        return result

    def saved_research(self, research_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM saved_research WHERE research_id=?", (research_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"未找到已保存研究: {research_id}")
        payload = json.loads(row["payload"])
        payload["saved_at"] = _parse_dt(row["saved_at"])
        payload["business_date"] = _dt.date.fromisoformat(row["business_date"])
        payload["known_at"] = _parse_dt(row["known_at"])
        payload["identity_as_of"] = _parse_dt(row["identity_as_of"])
        payload["research_kind"] = row["research_kind"]
        return payload

    def reopen_saved_research(self, research_id: str) -> dict:
        """按保存时的 known_at 与 identity_as_of 重新解释已保存研究。"""
        saved = self.saved_research(research_id)
        reopened = self.evaluate_portfolio(
            saved["holdings"], saved["business_date"], saved["known_at"],
            saved["identity_as_of"],
        )
        return {
            "saved": saved,
            "reopened": reopened,
            "same_value": saved["result"]["portfolio_value"]
            == reopened["portfolio_value"],
            "same_security_ids": tuple(saved["result"]["security_ids"])
            == reopened["security_ids"],
        }

    @classmethod
    def _json_safe(cls, value):
        if isinstance(value, dict):
            return {k: cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(v) for v in value]
        if hasattr(value, "as_dict"):
            return value.as_dict()
        if dataclasses.is_dataclass(value):
            return dataclasses.asdict(value)
        if isinstance(value, _dt.datetime):
            return value.isoformat()
        if isinstance(value, _dt.date):
            return value.isoformat()
        return value

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
        for r in self.conn.execute(
            "SELECT * FROM corp_action WHERE effective_date <= ?"
            " AND received_at > ? AND received_at <= ? ORDER BY received_at, seq",
            (bd.isoformat(), s.isoformat(), e.isoformat()),
        ):
            events.append({
                "type": "corporate_action", "kind": r["action_type"],
                "action_id": r["action_id"], "family_id": r["family_id"],
                "announcement_kind": r["announcement_kind"],
                "received_at": _parse_dt(r["received_at"]),
                "effective_date": _dt.date.fromisoformat(r["effective_date"]),
                "source": r["source"], "source_record": r["source_record"],
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
            "SELECT * FROM corp_action"
            " WHERE effective_date <= ? AND received_at <= ? ORDER BY received_at, seq",
            (bd.isoformat(), end),
        ):
            items.append({
                "type": "corporate_action", "kind": r["action_type"],
                "action_id": r["action_id"], "family_id": r["family_id"],
                "announcement_kind": r["announcement_kind"],
                "confirmation_status": r["confirmation_status"],
                "from_security_id": r["from_security_id"],
                "to_security_id": r["to_security_id"],
                "ratio": [r["from_ratio"], r["to_ratio"]],
                "effective_date": _dt.date.fromisoformat(r["effective_date"]),
                "received_at": _parse_dt(r["received_at"]),
                "source": r["source"], "source_record": r["source_record"],
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
                      "corp_action", "corp_action_adoption", "quote_event",
                      "adjudication", "priority_rule", "identity_announcement",
                      "code_quote_event", "saved_research"):
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


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"不可 JSON 序列化的对象: {type(obj)!r}")


def _enumerate_dates(start, end) -> list[_dt.date]:
    s = start if isinstance(start, _dt.date) else _dt.date.fromisoformat(start)
    e = end if isinstance(end, _dt.date) else _dt.date.fromisoformat(end)
    out, cur = [], s
    while cur <= e:
        out.append(cur)
        cur += _dt.timedelta(days=1)
    return out
