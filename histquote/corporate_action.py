"""公司行动：拆股、换股合并与现金补偿。

公司行动公告与行情一样按双时态处理：预告、修订（supersedes 旧版本）
与撤回（withdraw 精确指向目标公告）都只追加、永不覆盖。某个历史视角
只能看到当时已送达且未被撤回的公告版本；已保存的持仓研究继续按当时
可见的资料解释。

关键原则：
- 显示代码变化本身不是数量变更；换股后的价格跳变是公司行动结果，
  不能被解释为投资收益。
- 新证券身份未确认时保留候选，绝不猜测属于哪个现有证券。
- 部分采用不能把同一份额同时计入两个证券。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt


@dataclasses.dataclass(frozen=True)
class CorporateAction:
    """一条不可变公司行动公告。

    kind:
      split             - 拆股：每 1 股拆成 ratio 股，证券身份不变
      merger            - 换股合并：每 1 股原证券换 ratio 股新证券
      cash_compensation - 现金补偿：每股补偿 cash_per_share
      withdraw          - 撤回 target_action_id 指向的公告版本

    修订通过追加一条 supersedes 指向旧 action_id 的新公告完成；
    同一修订链在任一历史视角只生效当时可见的最新版本。
    """

    security_id: str
    ex_date: _dt.date
    received_at: _dt.datetime
    kind: str
    action_id: str = ""
    ratio: float = 1.0
    new_security_id: str = ""
    new_security_candidates: tuple[str, ...] = ()
    cash_per_share: float = 0.0
    fraction_policy: str = "cash"        # cash / carry / round
    fraction_cash_price: float | None = None
    supersedes: str = ""
    reason: str = ""
    source: str = ""
    source_record: str = ""
    target_action_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"split", "merger", "cash_compensation", "withdraw"}:
            raise ValueError(f"未知公司行动类型: {self.kind}")
        if self.kind == "withdraw":
            if not self.target_action_id:
                raise ValueError("公司行动撤回必须指定目标公告")
            return
        if self.kind in {"split", "merger"} and self.ratio <= 0:
            raise ValueError("换股/拆股比例必须为正数")
        if self.kind == "cash_compensation" and self.cash_per_share < 0:
            raise ValueError("现金补偿不能为负数")
        if self.fraction_policy not in {"cash", "carry", "round"}:
            raise ValueError(f"未知零碎份额处理方式: {self.fraction_policy}")

    def as_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "security_id": self.security_id,
            "kind": self.kind,
            "ex_date": self.ex_date.isoformat(),
            "received_at": self.received_at.isoformat(),
            "ratio": self.ratio,
            "new_security_id": self.new_security_id,
            "new_security_candidates": list(self.new_security_candidates),
            "cash_per_share": self.cash_per_share,
            "fraction_policy": self.fraction_policy,
            "fraction_cash_price": self.fraction_cash_price,
            "supersedes": self.supersedes,
            "reason": self.reason,
            "source": self.source,
            "source_record": self.source_record,
            "target_action_id": self.target_action_id,
        }
