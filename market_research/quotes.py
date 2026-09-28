"""双时态行情存储与查询。

写入规则
--------
1. 每条到来的记录按 (figi, trade_date) 找到该业务日的当前活动版本
   （superseded_at IS NULL）。
2. 若关键字段完全相同且来源记录已见过，则视为“重复记录”，直接忽略，
   不产生新版本（幂等重放）。
3. 若关键字段不同，则追加新版本，并把旧版本的 superseded_at 置为本次
   received_at —— 旧值永不删除，可被更早 as-of 的查询读到。

查询规则
--------
``get_as_of(figi, trade_date, as_of)`` 返回在 ``as_of`` 这个收到时点
系统里实际能看到的那个版本（``received_at <= as_of`` 且当时未被取代）。
因此在 T 日收盘后做回测，不会读到 T+3 才送达的更正价或补齐价。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from .timeutil import DateLike, TimeLike, iso, to_date, to_utc

FIELDS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Quote:
    figi: str
    trade_date: date
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]
    source: str
    received_at: datetime
    remark: Optional[str] = None


@dataclass(frozen=True)
class QuoteVersion(Quote):
    """一个行情版本：含版本链信息。"""
    version_id: int = 0
    superseded_at: Optional[datetime] = None
    is_correction: bool = False

    @property
    def is_current(self) -> bool:
        return self.superseded_at is None


def _payload_hash(fields: dict) -> str:
    raw = "|".join("" if fields[k] is None else repr(fields[k]) for k in FIELDS)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class QuoteStore:
    def __init__(self, conn):
        self._conn = conn

    # ---------------------------------------------------------------- 写入
    def upsert(
        self,
        figi: str,
        trade_date: DateLike,
        *,
        received_at: TimeLike,
        source: str,
        source_record_id: Optional[str] = None,
        open: Optional[float] = None,
        high: Optional[float] = None,
        low: Optional[float] = None,
        close: Optional[float] = None,
        volume: Optional[float] = None,
        remark: Optional[str] = None,
    ) -> str:
        """写入一条行情记录。返回动作：inserted / revised / duplicate。"""
        td = to_date(trade_date).isoformat()
        recv = iso(received_at)
        fields = {"open": open, "high": high, "low": low, "close": close, "volume": volume}

        active = self._conn.execute(
            """SELECT * FROM quote_versions
               WHERE figi=? AND trade_date=? AND superseded_at IS NULL""",
            (figi, td),
        ).fetchone()

        # 重复记录：同一来源记录号，或字段内容与活动版本完全一致
        same_source = (
            active is not None
            and source_record_id is not None
            and active["source_record_id"] == source_record_id
        )
        same_payload = active is not None and all(
            _eq(active[k], fields[k]) for k in FIELDS
        )
        if same_source or same_payload:
            return "duplicate"

        action = "inserted" if active is None else "revised"
        if active is not None:
            self._conn.execute(
                "UPDATE quote_versions SET superseded_at=? WHERE id=?",
                (recv, active["id"]),
            )

        self._conn.execute(
            """INSERT INTO quote_versions
               (figi,trade_date,open,high,low,close,volume,source,
                source_record_id,received_at,superseded_at,remark)
               VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)""",
            (figi, td, open, high, low, close, volume, source,
             source_record_id, recv, remark),
        )
        self._conn.commit()
        return action

    def ingest_batch(self, records: Iterable[dict], *, source: str, received_at,
                     batch_id: str, note: Optional[str] = None) -> dict:
        """批量摄入。records 为 dict 列表，键同 upsert 参数（figi/trade_date 必填）。"""
        counts = {"inserted": 0, "revised": 0, "duplicate": 0}
        h = hashlib.sha256()
        n = 0
        for rec in records:
            rec = dict(rec)
            h.update(repr(sorted(rec.items())).encode())
            n += 1
            action = self.upsert(rec.pop("figi"), rec.pop("trade_date"),
                                 source=source, received_at=received_at, **rec)
            counts[action] += 1
        self._conn.execute(
            """INSERT OR REPLACE INTO ingest_batches
               (batch_id,source,received_at,payload_sha,n_rows,note)
               VALUES (?,?,?,?,?,?)""",
            (batch_id, source, iso(received_at), h.hexdigest(), n, note),
        )
        self._conn.commit()
        return counts

    # ---------------------------------------------------------------- 查询
    def get_as_of(self, figi: str, trade_date: DateLike,
                  as_of: TimeLike) -> Optional[QuoteVersion]:
        """在收到时点 as_of，对业务日 trade_date 实际可见的版本。

        选取条件：received_at <= as_of，且
        (superseded_at IS NULL 或 superseded_at > as_of)。
        取其中 received_at 最大的一条。
        """
        td = to_date(trade_date).isoformat()
        ao = iso(as_of)
        row = self._conn.execute(
            """SELECT * FROM quote_versions
               WHERE figi=? AND trade_date=? AND received_at<=?
                 AND (superseded_at IS NULL OR superseded_at>?)
               ORDER BY received_at DESC, id DESC LIMIT 1""",
            (figi, td, ao, ao),
        ).fetchone()
        return None if row is None else _row_to_version(row, False)

    def get_current(self, figi: str, trade_date: DateLike) -> Optional[QuoteVersion]:
        td = to_date(trade_date).isoformat()
        row = self._conn.execute(
            """SELECT * FROM quote_versions
               WHERE figi=? AND trade_date=? AND superseded_at IS NULL""",
            (figi, td),
        ).fetchone()
        return None if row is None else _row_to_version(row, False)

    def history(self, figi: str, trade_date: DateLike) -> list[QuoteVersion]:
        """该业务日的全部修订版本链（原始记录 + 每次修订），按收到时间排列。"""
        td = to_date(trade_date).isoformat()
        rows = self._conn.execute(
            """SELECT * FROM quote_versions WHERE figi=? AND trade_date=?
               ORDER BY received_at, id""",
            (figi, td),
        ).fetchall()
        result = []
        for i, row in enumerate(rows):
            result.append(_row_to_version(row, is_correction=i > 0))
        return result

    def merged_query(self, figi: str, start: DateLike, end: DateLike,
                     as_of: Optional[TimeLike] = None) -> list[QuoteVersion]:
        """合并查询：区间内每个业务日一条“当时可见”版本。

        as_of 给定时按收到时间过滤（回测视角）；不给定时返回当前最新版本。
        每条结果都带来源 source，可追溯到原始行情还是某次修订。
        """
        s, e = to_date(start).isoformat(), to_date(end).isoformat()
        if as_of is None:
            rows = self._conn.execute(
                """SELECT * FROM quote_versions v
                       WHERE figi=? AND trade_date BETWEEN ? AND ?
                         AND superseded_at IS NULL
                       ORDER BY trade_date""",
                (figi, s, e),
            ).fetchall()
            return [_row_to_version(r, _was_revised(self._conn, r)) for r in rows]
        ao = iso(as_of)
        rows = self._conn.execute(
            """SELECT * FROM quote_versions v
                   WHERE figi=? AND trade_date BETWEEN ? AND ? AND received_at<=?
                     AND (superseded_at IS NULL OR superseded_at>?)
                   ORDER BY trade_date, received_at DESC, id DESC""",
            (figi, s, e, ao, ao),
        ).fetchall()
        picked: dict[str, object] = {}
        for r in rows:
            picked.setdefault(r["trade_date"], r)
        return [_row_to_version(r, _was_revised(self._conn, r, ao))
                for r in picked.values()]


def _eq(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return float(a) == float(b)


def _was_revised(conn, row, as_of_iso: Optional[str] = None) -> bool:
    """判断该版本是否为修订版：存在更早收到的同业务日版本。"""
    q = "SELECT COUNT(*) c FROM quote_versions WHERE figi=? AND trade_date=? AND id<?"
    n = conn.execute(q, (row["figi"], row["trade_date"], row["id"])).fetchone()["c"]
    return n > 0


def _parse_dt(value):
    return datetime.fromisoformat(value) if value else None


def _row_to_version(row, is_correction: bool) -> QuoteVersion:
    return QuoteVersion(
        version_id=row["id"],
        figi=row["figi"],
        trade_date=date.fromisoformat(row["trade_date"]),
        open=row["open"], high=row["high"], low=row["low"],
        close=row["close"], volume=row["volume"],
        source=row["source"],
        received_at=datetime.fromisoformat(row["received_at"]),
        remark=row["remark"],
        superseded_at=_parse_dt(row["superseded_at"]),
        is_correction=is_correction,
    )
