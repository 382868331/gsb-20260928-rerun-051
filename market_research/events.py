"""公司行为（拆股）与停牌：同样按双时态版本化管理。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from .timeutil import DateLike, TimeLike, iso, to_date


@dataclass(frozen=True)
class Split:
    figi: str
    ex_date: date
    factor: float
    source: str
    received_at: datetime
    superseded_at: Optional[datetime] = None
    remark: Optional[str] = None


@dataclass(frozen=True)
class Halt:
    figi: str
    halt_date: date
    status: str          # HALTED / RESUMED
    source: str
    received_at: datetime
    superseded_at: Optional[datetime] = None
    remark: Optional[str] = None


class EventStore:
    def __init__(self, conn):
        self._conn = conn

    # ------------------------------------------------------------ 拆股
    def record_split(self, figi, ex_date: DateLike, factor: float, *,
                     source: str, received_at: TimeLike,
                     source_record_id: Optional[str] = None,
                     remark: Optional[str] = None) -> str:
        exd = to_date(ex_date).isoformat()
        recv = iso(received_at)
        active = self._conn.execute(
            """SELECT * FROM corporate_action_versions
               WHERE figi=? AND action_type='SPLIT' AND ex_date=?
                 AND superseded_at IS NULL""",
            (figi, exd),
        ).fetchone()
        if active is not None:
            if float(active["factor"]) == float(factor):
                return "duplicate"
            self._conn.execute(
                "UPDATE corporate_action_versions SET superseded_at=? WHERE id=?",
                (recv, active["id"]),
            )
            action = "revised"
        else:
            action = "inserted"
        self._conn.execute(
            """INSERT INTO corporate_action_versions
               (figi,action_type,ex_date,factor,source,source_record_id,
                received_at,remark)
               VALUES (?,?,?,?,?,?,?,?)""",
            (figi, "SPLIT", exd, factor, source, source_record_id, recv, remark),
        )
        self._conn.commit()
        return action

    def splits_as_of(self, figi: str, as_of: TimeLike) -> list[Split]:
        """在 as_of 时点已知的全部拆股（按除权日排序）。

        关键点：as-of 过滤同时要求 received_at<=as_of 且当时未被取代，
        回测不会使用尚未公告/尚未送达的拆股因子去复权历史价格。
        """
        ao = iso(as_of)
        rows = self._conn.execute(
            """SELECT * FROM corporate_action_versions
               WHERE figi=? AND action_type='SPLIT' AND received_at<=?
                 AND (superseded_at IS NULL OR superseded_at>?)
               ORDER BY ex_date""",
            (figi, ao, ao),
        ).fetchall()
        return [Split(
            figi=r["figi"], ex_date=date.fromisoformat(r["ex_date"]),
            factor=r["factor"], source=r["source"],
            received_at=datetime.fromisoformat(r["received_at"]),
            superseded_at=datetime.fromisoformat(r["superseded_at"]) if r["superseded_at"] else None,
            remark=r["remark"],
        ) for r in rows]

    def adjustment_factor(self, figi: str, trade_date: DateLike,
                          as_of: TimeLike) -> float:
        """回测视角的累计复权因子。

        只统计：在 as_of 已知、且除权日晚于 trade_date 的拆股。
        即把 trade_date 的旧价折算到“as_of 当时认知中的股本口径”。
        factor=2 表示 1 拆 2，旧价除以 2。
        """
        td = to_date(trade_date)
        factor = 1.0
        for split in self.splits_as_of(figi, as_of):
            if split.ex_date > td:
                factor *= split.factor
        return factor

    # ------------------------------------------------------------ 停牌
    def record_halt(self, figi, halt_date: DateLike, status: str, *,
                    source: str, received_at: TimeLike,
                    source_record_id: Optional[str] = None,
                    remark: Optional[str] = None) -> str:
        if status not in ("HALTED", "RESUMED"):
            raise ValueError("status 必须是 HALTED 或 RESUMED")
        hd = to_date(halt_date).isoformat()
        recv = iso(received_at)
        active = self._conn.execute(
            """SELECT * FROM halt_versions
               WHERE figi=? AND halt_date=? AND superseded_at IS NULL""",
            (figi, hd),
        ).fetchone()
        if active is not None:
            if active["status"] == status:
                return "duplicate"
            self._conn.execute(
                "UPDATE halt_versions SET superseded_at=? WHERE id=?",
                (recv, active["id"]),
            )
            action = "revised"
        else:
            action = "inserted"
        self._conn.execute(
            """INSERT INTO halt_versions
               (figi,halt_date,status,source,source_record_id,received_at,remark)
               VALUES (?,?,?,?,?,?,?)""",
            (figi, hd, status, source, source_record_id, recv, remark),
        )
        self._conn.commit()
        return action

    def status_as_of(self, figi: str, day: DateLike, as_of: TimeLike) -> Optional[str]:
        """as_of 时点对 day 当天停牌状态的最新认知；无记录返回 None（正常交易）。"""
        hd = to_date(day).isoformat()
        ao = iso(as_of)
        row = self._conn.execute(
            """SELECT * FROM halt_versions
               WHERE figi=? AND halt_date=? AND received_at<=?
                 AND (superseded_at IS NULL OR superseded_at>?)
               ORDER BY received_at DESC, id DESC LIMIT 1""",
            (figi, hd, ao, ao),
        ).fetchone()
        return None if row is None else row["status"]

    def halt_history(self, figi: str, day: DateLike) -> list[Halt]:
        hd = to_date(day).isoformat()
        rows = self._conn.execute(
            """SELECT * FROM halt_versions WHERE figi=? AND halt_date=?
               ORDER BY received_at, id""",
            (figi, hd),
        ).fetchall()
        return [Halt(
            figi=r["figi"], halt_date=date.fromisoformat(r["halt_date"]),
            status=r["status"], source=r["source"],
            received_at=datetime.fromisoformat(r["received_at"]),
            superseded_at=datetime.fromisoformat(r["superseded_at"]) if r["superseded_at"] else None,
            remark=r["remark"],
        ) for r in rows]
