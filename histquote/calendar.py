"""交易日历：标记哪些日期是实际开市日。"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable


def _as_date(value: str | _dt.date) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(value)


class TradingCalendar:
    def __init__(self, conn) -> None:
        self._conn = conn

    def add_sessions(self, dates: Iterable[str | _dt.date]) -> None:
        rows = [(d.isoformat() if isinstance(d, _dt.date) else str(d),) for d in dates]
        self._conn.executemany(
            "INSERT OR IGNORE INTO trading_session(session_date) VALUES (?)", rows
        )
        self._conn.commit()

    def is_session(self, date: str | _dt.date) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trading_session WHERE session_date = ?",
            (_as_date(date).isoformat(),),
        ).fetchone()
        return row is not None

    def sessions(
        self,
        start: str | _dt.date | None = None,
        end: str | _dt.date | None = None,
    ) -> list[_dt.date]:
        sql = "SELECT session_date FROM trading_session"
        cond, params = [], []
        if start is not None:
            cond.append("session_date >= ?")
            params.append(_as_date(start).isoformat())
        if end is not None:
            cond.append("session_date <= ?")
            params.append(_as_date(end).isoformat())
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY session_date"
        return [_dt.date.fromisoformat(r[0]) for r in self._conn.execute(sql, params)]

    def previous_session(self, date: str | _dt.date) -> _dt.date | None:
        row = self._conn.execute(
            "SELECT MAX(session_date) FROM trading_session WHERE session_date < ?",
            (_as_date(date).isoformat(),),
        ).fetchone()
        return _dt.date.fromisoformat(row[0]) if row and row[0] else None
