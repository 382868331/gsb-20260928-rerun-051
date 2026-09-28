"""回测视角：在某个研究时点，只能看到当时实际知道的数据。

所有方法都强制要求 as_of（收到时间），内部统一做双时态过滤，
避免回测代码直接查“当前最新价”而误用未来修订。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .timeutil import DateLike, TimeLike, to_date


@dataclass(frozen=True)
class PointInTimeBar:
    figi: str
    trade_date: date
    close: Optional[float]
    raw_close: Optional[float]
    volume: Optional[float]
    halted: bool
    source: str
    received_at: datetime
    remark: Optional[str] = None


class PointInTimeView:
    """绑定一个研究时点 as_of 的只读行情视图。"""

    def __init__(self, dataset, as_of: TimeLike):
        self._ds = dataset
        self.as_of = as_of

    def bar(self, figi: str, trade_date: DateLike) -> Optional[PointInTimeBar]:
        td = to_date(trade_date)
        qv = self._ds.quotes.get_as_of(figi, td, self.as_of)
        if qv is None:
            return None
        halted = self._ds.events.status_as_of(figi, td, self.as_of) == "HALTED"
        adj = self._ds.events.adjustment_factor(figi, td, self.as_of)
        adj_close = None if qv.close is None else qv.close / adj
        return PointInTimeBar(
            figi=figi, trade_date=td,
            close=adj_close, raw_close=qv.close, volume=qv.volume,
            halted=halted, source=qv.source,
            received_at=qv.received_at, remark=qv.remark,
        )

    def series(self, figi: str, start: DateLike, end: DateLike) -> list[PointInTimeBar]:
        """交易日历驱动的序列；停牌日/当时尚未收到数据的日子不产bar。"""
        inst = self._ds.instruments.get(figi)
        cal = self._ds.calendar(inst.exchange)
        bars = []
        for d in cal.trading_days(start, end):
            if self._ds.events.status_as_of(figi, d, self.as_of) == "HALTED":
                continue
            bar = self.bar(figi, d)
            if bar is not None:
                bars.append(bar)
        return bars
