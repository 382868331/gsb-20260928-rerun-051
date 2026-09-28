"""时间工具：统一使用带时区的 UTC 时间戳。

样例与测试里允许写 "2026-01-05T18:05:00+08:00" 这样的本地时间，
入库前统一归一化为 UTC，避免夏令时/时区歧义。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Union

TimeLike = Union[datetime, str]
DateLike = Union[date, str]


def to_utc(value: TimeLike) -> datetime:
    """把 datetime 或 ISO 字符串转成带时区的 UTC datetime。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise TypeError(f"不支持的时间类型: {type(value)!r}")
    if dt.tzinfo is None:
        raise ValueError("时间戳必须显式带时区，例如 2026-01-05T18:05:00+08:00")
    return dt.astimezone(timezone.utc)


def to_date(value: DateLike) -> date:
    """把 date 或 YYYY-MM-DD 字符串转成 date。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"不支持的日期类型: {type(value)!r}")


def iso(dt: datetime) -> str:
    return to_utc(dt).isoformat()
