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
class Split:
    security_id: str
    ex_date: _dt.date
    ratio: float          # 每股拆成 ratio 股，例如 2.0 表示 1 拆 2
    received_at: _dt.datetime

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError("拆分比例必须为正数")
