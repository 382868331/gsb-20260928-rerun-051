"""双时态时点查询。"""

from __future__ import annotations

import dataclasses
import datetime as _dt

from .quote import Quote, QuoteStatus


@dataclasses.dataclass(frozen=True)
class SourceCandidate:
    """某历史视角下，同一 (证券, 业务日) 的一条候选来源记录。"""

    source: str
    source_record: str
    kind: str
    received_at: _dt.datetime
    close: float | None
    active: bool          # 截至 known_at 是否未被该来源撤回
    withdrawn_by: str     # 撤回它的来源记录号（未撤回为 ""）


@dataclasses.dataclass(frozen=True)
class QuoteView:
    """as-of 查询结果：某证券在某业务日、截至 known_at 的可见状态。"""

    security_id: str
    business_date: _dt.date
    known_at: _dt.datetime
    status: QuoteStatus
    quote: Quote | None
    adjusted_close: float | None
    source: str
    source_record: str
    received_at: _dt.datetime | None
    event_kind: str
    candidates: tuple[SourceCandidate, ...] = ()
    adjudication_id: str = ""
    decision_received_at: _dt.datetime | None = None
    decision_reason: str = ""
    selection_basis: str = ""   # single-source / default-priority / adjudicated
    priority_rule_id: str = ""  # 本次裁决实际采用的优先级规则（空 = 内存默认配置）
    display_code: str = ""
    market: str = ""
    identity_status: str = ""
    identity_announcement_ids: tuple[str, ...] = ()
    identity_withdrawn_announcement_ids: tuple[str, ...] = ()
    identity_basis: str = ""
    identity_candidates: tuple[str, ...] = ()
    identity_source_rules: tuple[dict, ...] = ()
    identity_name: str = ""

    def as_dict(self) -> dict:
        q = dataclasses.asdict(self.quote) if self.quote else None
        return {
            "security_id": self.security_id,
            "business_date": self.business_date.isoformat(),
            "known_at": self.known_at.isoformat(),
            "status": self.status.value,
            "quote": q,
            "adjusted_close": self.adjusted_close,
            "source": self.source,
            "source_record": self.source_record,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "event_kind": self.event_kind,
            "candidates": [dataclasses.asdict(c) for c in self.candidates],
            "adjudication_id": self.adjudication_id,
            "decision_received_at": (
                self.decision_received_at.isoformat()
                if self.decision_received_at else None
            ),
            "decision_reason": self.decision_reason,
            "selection_basis": self.selection_basis,
            "priority_rule_id": self.priority_rule_id,
            "display_code": self.display_code,
            "market": self.market,
            "identity_status": self.identity_status,
            "identity_announcement_ids": list(self.identity_announcement_ids),
            "identity_withdrawn_announcement_ids": list(
                self.identity_withdrawn_announcement_ids),
            "identity_basis": self.identity_basis,
            "identity_candidates": list(self.identity_candidates),
            "identity_name": self.identity_name,
            "identity_source_rules": [
                dict(r, received_at=(r["received_at"].isoformat()
                                     if r.get("received_at") else None),
                     effective_from=(r["effective_from"].isoformat()
                                     if r.get("effective_from") else None),
                     effective_to=(r["effective_to"].isoformat()
                                   if r.get("effective_to") else None))
                for r in self.identity_source_rules
            ],
        }
