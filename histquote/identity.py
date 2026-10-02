"""证券身份与公司行为（拆分）。

拆分同样按收到时间追踪：在公告实际送达之前，回测不应预先使用该除权因子。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt


@dataclasses.dataclass(frozen=True)
class Security:
    security_id: str
    code: str
    name: str


@dataclasses.dataclass(frozen=True)
class IdentityAnnouncement:
    """显示代码与稳定证券身份之间关系的一次公告。

    action="confirm" 确认 code 自 effective_date 起指向 security_id；
    action="withdraw" 撤回既有公告（supersedes 指向被撤回公告）。
    公告只追加，按 received_at 决定历史研究者何时可见。
    """

    announcement_id: str
    code: str
    security_id: str
    effective_date: _dt.date
    received_at: _dt.datetime
    action: str = "confirm"
    name: str = ""
    source: str = ""
    source_record: str = ""
    reason: str = ""
    supersedes: str = ""

    def __post_init__(self) -> None:
        if self.action not in {"confirm", "withdraw"}:
            raise ValueError("身份公告只能是 confirm 或 withdraw")
        if not (self.announcement_id and self.code and self.security_id):
            raise ValueError("身份公告必须提供公告号、显示代码和稳定证券身份")
        if self.action == "withdraw" and not self.supersedes:
            raise ValueError("撤回身份公告必须指向被撤回的公告号")


@dataclasses.dataclass(frozen=True)
class PortfolioPosition:
    """组合持仓输入；code 是研究者看到的显示代码，security_id 可显式指定。"""

    code: str
    security_id: str = ""
    quantity: float = 1.0


@dataclasses.dataclass(frozen=True)
class Split:
    security_id: str
    ex_date: _dt.date
    ratio: float          # 每股拆成 ratio 股，例如 2.0 表示 1 拆 2
    received_at: _dt.datetime

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError("拆分比例必须为正数")
