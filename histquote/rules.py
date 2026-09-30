"""来源选择规则（优先级配置）的双时态修订。

规则和行情一样是“在某时刻才收到”的事实：新偏好生效之前的历史视角
看不到它，因此不能用今天的偏好改写过去已经能看到的裁决结果。
每次规则调整（包括把配置回退成与旧版本完全相同的内容）都生成一个
全新的修订身份 rule_id，原修订永不被覆盖。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt


@dataclasses.dataclass(frozen=True)
class SourceRule:
    """一版来源优先级规则修订。

    priority: 来源名 -> 优先级序号（越小越优先），与 set_source_priority 同义。
    received_at: 该版本规则实际生效（被研究系统收到）的时刻。
    rule_id: 本次修订的唯一身份；重放同一身份视为幂等。
    restores_rule_id: 本次修订在内容上恢复了哪份旧配置（仅留痕，身份仍独立）。
    """

    rule_id: str
    received_at: _dt.datetime
    priority: dict[str, int]
    reason: str = ""
    restores_rule_id: str = ""

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("来源规则修订必须指定 rule_id")
        if not isinstance(self.priority, dict) or not self.priority:
            raise ValueError("来源规则修订必须携带非空优先级配置")
        for source, rank in self.priority.items():
            if not source or not isinstance(rank, int):
                raise ValueError("优先级配置必须是 来源名(str) -> 序号(int)")

    def priority_items(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self.priority.items()))
