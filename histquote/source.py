"""数据来源登记。

跨来源冲突裁决时，priority 越大表示越权威（例如交易所官方行情优先于
第三方转发行情）。优先级只用于裁决“已知”候选；某个来源尚未送达的记录
不会因为其优先级高而被提前使用。
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class SourceInfo:
    source_id: str
    name: str = ""
    priority: int = 0
