"""行情数据结构。行情以“事件”形式永久保存，不做就地覆盖。"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum


class QuoteStatus(str, enum.Enum):
    OK = "ok"              # 该时点已知有效行情
    CANCELLED = "cancelled"  # 记录被发布方撤销（如错挂代码）
    WITHDRAWN = "withdrawn"  # 来源撤回：所有候选记录均被其来源收回
    SUSPENDED = "suspended"  # 当日停牌
    MISSING = "missing"      # 开市日但截至该时点尚未收到任何数据
    NON_SESSION = "non_session"


@dataclasses.dataclass(frozen=True)
class Quote:
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


@dataclasses.dataclass(frozen=True)
class QuoteEvent:
    """一条不可变行情事件。

    kind: raw / revision / cancel / suspension / withdrawal。
    withdrawal（来源撤回）通过 target_source + target_record 精确指向被撤回的
    那条来源原始记录；其它来源的记录不受影响。
    """

    security_id: str
    business_date: _dt.date
    received_at: _dt.datetime
    kind: str = "raw"
    quote: Quote | None = None
    reason: str = ""
    source: str = ""
    source_record: str = ""
    target_source: str = ""
    target_record: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"raw", "revision", "cancel", "suspension", "withdrawal"}:
            raise ValueError(f"未知事件类型: {self.kind}")
        if self.kind in {"raw", "revision"} and self.quote is None:
            raise ValueError(f"{self.kind} 事件必须携带行情")
        if self.kind == "withdrawal" and not (self.target_source and self.target_record):
            raise ValueError("withdrawal 事件必须指定 target_source / target_record")
