"""交易日历：开市日/休市日，支持按业务时间定位下一/上一交易日。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from .timeutil import to_date


class TradingCalendar:
    def __init__(self, conn, exchange: str = "SSE"):
        self._conn = conn
        self.exchange = exchange

    def set_day(self, day, is_open: bool, note: Optional[str] = None) -> None:
        d = to_date(day).isoformat()
        self._conn.execute(
            """INSERT INTO trading_calendar(exchange, cal_date, is_open, note)
               VALUES (?,?,?,?)
               ON CONFLICT(exchange, cal_date) DO UPDATE SET
                 is_open=excluded.is_open, note=excluded.note""",
            (self.exchange, d, 1 if is_open else 0, note),
        )
        self._conn.commit()

    def add_holidays(self, days: Iterable, note: str = "节假日休市") -> None:
        for day in days:
            self.set_day(day, False, note)

    def fill_weekdays(self, start, end) -> None:
        """把区间内周一到周五标为开市，周末标为休市（节假日再单独覆盖）。"""
        start, end = to_date(start), to_date(end)
        day = start
        while day <= end:
            is_open = day.weekday() < 5
            self.set_day(day, is_open, None if is_open else "周末休市")
            day += timedelta(days=1)

    def is_open(self, day) -> bool:
        d = to_date(day).isoformat()
        row = self._conn.execute(
            "SELECT is_open FROM trading_calendar WHERE exchange=? AND cal_date=?",
            (self.exchange, d),
        ).fetchone()
        if row is None:
            return to_date(day).weekday() < 5
        return bool(row["is_open"])

    def next_open(self, day) -> date:
        d = to_date(day) + timedelta(days=1)
        return self._shift(d, +1)

    def prev_open(self, day) -> date:
        d = to_date(day) - timedelta(days=1)
        return self._shift(d, -1)

    def trading_days(self, start, end) -> list[date]:
        start, end = to_date(start), to_date(end)
        rows = self._conn.execute(
            """SELECT cal_date FROM trading_calendar
               WHERE exchange=? AND is_open=1 AND cal_date BETWEEN ? AND ?
               ORDER BY cal_date""",
            (self.exchange, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [date.fromisoformat(r["cal_date"]) for r in rows]

    def _shift(self, d: date, step: int) -> date:
        for _ in range(100000):
            if self.is_open(d):
                return d
            d += timedelta(days=step)
        raise RuntimeError("交易日历范围不足")
