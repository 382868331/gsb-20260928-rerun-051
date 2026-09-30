"""跨来源冲突裁决。

裁决本身也是双时态事实：在 decision_received_at 之前的历史视角看不到它。
裁决只决定“采用哪条来源记录”，永不删除或改写任何来源原记录。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt


@dataclasses.dataclass(frozen=True)
class Adjudication:
    security_id: str
    business_date: _dt.date
    chosen_source: str
    chosen_record: str
    decision_received_at: _dt.datetime
    reason: str = ""
    decision_id: str = ""

    def __post_init__(self) -> None:
        if not (self.chosen_source and self.chosen_record):
            raise ValueError("裁决必须指定被采用的来源与来源记录号")

