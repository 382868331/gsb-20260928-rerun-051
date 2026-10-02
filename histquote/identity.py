"""证券身份与公司行为（拆分）。

拆分同样按收到时间追踪：在公告实际送达之前，回测不应预先使用该除权因子。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import Any


@dataclasses.dataclass(frozen=True)
class Security:
    security_id: str
    code: str
    name: str


@dataclasses.dataclass(frozen=True)
class Split:
    security_id: str
    ex_date: _dt.date
    ratio: float          # 每股拆成 ratio 股，例如 2.0 表示 1 拆 2
    received_at: _dt.datetime

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError("拆分比例必须为正数")


@dataclasses.dataclass(frozen=True)
class IdentityAnnouncement:
    """显示代码与稳定证券身份之间关系的不可变公告。

    action:
      confirm  - 确认代码在有效期内指向 security_id
      rename   - 仅确认名称变化，security_id 不变
      candidate- 资料不足时保留为候选，不自动确认
      withdraw - 撤回 target_announcement_id 指向的映射
    """

    display_code: str
    security_id: str
    effective_from: _dt.date
    received_at: _dt.datetime
    action: str = "confirm"
    market: str = ""
    name: str = ""
    effective_to: _dt.date | None = None
    source: str = ""
    source_record: str = ""
    announcement_id: str = ""
    reason: str = ""
    target_announcement_id: str = ""

    def __post_init__(self) -> None:
        if self.action not in {"confirm", "rename", "candidate", "withdraw"}:
            raise ValueError(f"未知身份公告类型: {self.action}")
        if not self.display_code:
            raise ValueError("身份公告必须包含显示代码")
        if self.action == "withdraw":
            if not self.target_announcement_id:
                raise ValueError("身份撤回必须指定目标公告")
        elif not self.security_id:
            raise ValueError("身份公告必须指定稳定证券身份")
        if (self.effective_to is not None
                and self.effective_to < self.effective_from):
            raise ValueError("身份有效期结束日不能早于开始日")


@dataclasses.dataclass(frozen=True)
class IdentityResolution:
    """某显示代码在业务日、某公告收到视角下的身份解释。"""

    display_code: str
    market: str
    business_date: _dt.date
    known_at: _dt.datetime
    status: str  # confirmed / candidate / unknown / ambiguous
    security_id: str = ""
    name: str = ""
    candidate_security_ids: tuple[str, ...] = ()
    announcement_ids: tuple[str, ...] = ()
    withdrawn_announcement_ids: tuple[str, ...] = ()
    basis: str = ""
    announcements: tuple[IdentityAnnouncement, ...] = ()
    withdrawn_announcements: tuple[IdentityAnnouncement, ...] = ()
    source_rules: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict:
        return {
            "display_code": self.display_code,
            "market": self.market,
            "business_date": self.business_date.isoformat(),
            "known_at": self.known_at.isoformat(),
            "status": self.status,
            "security_id": self.security_id,
            "name": self.name,
            "candidate_security_ids": list(self.candidate_security_ids),
            "announcement_ids": list(self.announcement_ids),
            "withdrawn_announcement_ids": list(self.withdrawn_announcement_ids),
            "basis": self.basis,
            "announcements": [dataclasses.asdict(a) for a in self.announcements],
            "withdrawn_announcements": [
                dataclasses.asdict(a) for a in self.withdrawn_announcements
            ],
            "source_rules": list(self.source_rules),
        }
