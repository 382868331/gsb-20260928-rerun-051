"""双时态时点查询。"""

from __future__ import annotations

import dataclasses
import datetime as _dt

from .quote import Quote, QuoteStatus


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
        }
