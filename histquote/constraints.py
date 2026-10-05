"""公司行动组合采用的持仓约束决策模型。

研究者对同一原始持仓可分别选择部分采用换股（exchange）、保留旧证券
（retain）或等待身份核实（await_identity）。决策本身不可变，撤回以
status="withdrawn" 标记，原记录保留；仅 pending 决策可撤回，已确认
（confirmed）决策及其补偿现金作为既有事实保留。
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class AdoptionDecision:
    """一笔持仓对一家公司行动的采用决策。

    mode:
      exchange       - 按行动比例采用换股/拆股，数量 quantity
      retain         - 明确保留旧证券，不参与该行动
      await_identity - 新证券身份未核实，暂挂等待
    status: pending / confirmed / withdrawn
    lot_id 可指向原始批次，也可指向前一行动派生批次（lot:action），
    从而把前一行动产生的新证券送入后一行动。
    """

    decision_id: str
    lot_id: str
    action_id: str
    quantity: float
    mode: str = "exchange"
    status: str = "pending"
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("采用决策必须包含 decision_id")
        if not self.lot_id:
            raise ValueError("采用决策必须指定持仓批次")
        if self.mode not in {"exchange", "retain", "await_identity"}:
            raise ValueError(f"未知采用方式: {self.mode}")
        if self.status not in {"pending", "confirmed", "withdrawn"}:
            raise ValueError(f"未知决策状态: {self.status}")
        if self.quantity < 0:
            raise ValueError("采用数量不能为负数")
        if self.mode == "exchange" and not self.action_id:
            raise ValueError("采用换股必须指定公司行动")


def withdraw_decision(decisions, decision_id: str):
    """撤回尚未确定的决策，返回新决策列表（原决策标记 withdrawn）。

    已确认决策不可撤回：其补偿现金与数量调整是既有事实。
    """
    result = []
    found = False
    for item in decisions:
        decision = item if isinstance(item, AdoptionDecision) else AdoptionDecision(**item)
        if decision.decision_id == decision_id:
            found = True
            if decision.status == "confirmed":
                raise ValueError(
                    f"已确认决策不可撤回，补偿现金为既有事实: {decision_id}"
                )
            if decision.status == "withdrawn":
                raise ValueError(f"决策已撤回，不能重复撤回: {decision_id}")
            decision = dataclasses.replace(decision, status="withdrawn")
        result.append(decision)
    if not found:
        raise ValueError(f"未知采用决策: {decision_id}")
    return result
