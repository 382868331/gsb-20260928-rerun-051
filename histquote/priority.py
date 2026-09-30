"""来源优先级规则：append-only 修订链，带历史有效范围。

每条规则记录自己的 received_at（生效起点）。as-of 查询只采用
received_at <= known_at 的最新规则，因此今天调整偏好不会改写
过去研究时点已经能看到的结果。规则回退也是追加一条新修订，
保留本次修订身份（rule_id 与 supersedes 链），不覆盖旧规则。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt


@dataclasses.dataclass(frozen=True)
class PriorityRule:
    """一次来源优先级修订。

    received_at 为 None 表示“全程有效”的基础规则（兼容旧行为）。
    """

    priority: dict[str, int]
    received_at: _dt.datetime | None = None
    reason: str = ""
    rule_id: str = ""
    supersedes: str = ""

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "priority": dict(self.priority),
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "reason": self.reason,
            "supersedes": self.supersedes,
        }
