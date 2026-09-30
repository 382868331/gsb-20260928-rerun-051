"""行情数据结构。行情以“事件”形式永久保存，不做就地覆盖。"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum


class QuoteStatus(str, enum.Enum):
    OK = "ok"              # 该时点已知有效行情
    CANCELLED = "cancelled"  # 记录被发布方撤销（如错挂代码）
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

    kind:
      raw/revision -- 发布/修订一条行情记录；
      suspension   -- 当日停牌；
      cancel       -- 来源撤销自己在该业务日此前发布的记录；
      retraction   -- 来源撤回指定记录（target_source/target_record），
                      target_record 为 "*" 表示撤回该来源当日全部记录。
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
        if self.kind not in {"raw", "revision", "cancel", "suspension", "retraction"}:
            raise ValueError(f"未知事件类型: {self.kind}")
        if self.kind in {"raw", "revision"} and self.quote is None:
            raise ValueError(f"{self.kind} 事件必须携带行情")


@dataclasses.dataclass(frozen=True)
class QuoteCandidate:
    """裁决时一条可见的来源记录及其状态。

    state: active（仍代表该来源的主张）/ superseded（被同来源更新记录取代）
           / retracted（已撤回或撤销）。
    """

    source: str
    source_record: str
    kind: str
    received_at: _dt.datetime
    quote: Quote | None
    state: str
    reason: str = ""
