"""证券身份主数据。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .timeutil import to_date, to_utc, iso


@dataclass(frozen=True)
class Instrument:
    figi: str
    symbol: str
    name: str
    currency: str = "CNY"
    exchange: str = "SSE"
    lot_size: int = 100
    first_date: Optional[date] = None
    last_date: Optional[date] = None


class InstrumentRegistry:
    def __init__(self, conn):
        self._conn = conn

    def register(
        self,
        instrument: Instrument,
        *,
        received_at,
        aliases: Optional[dict[str, str]] = None,
    ) -> Instrument:
        """登记证券身份。aliases: {曾用代码: 生效起始日}。"""
        cur = self._conn.execute(
            "SELECT figi FROM instruments WHERE figi=?", (instrument.figi,)
        )
        if cur.fetchone():
            raise ValueError(f"证券已存在: {instrument.figi}")
        self._conn.execute(
            """INSERT INTO instruments
               (figi,symbol,name,currency,exchange,lot_size,first_date,last_date,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                instrument.figi, instrument.symbol, instrument.name,
                instrument.currency, instrument.exchange, instrument.lot_size,
                instrument.first_date.isoformat() if instrument.first_date else None,
                instrument.last_date.isoformat() if instrument.last_date else None,
                iso(to_utc(received_at)),
            ),
        )
        if aliases:
            for alias, valid_from in aliases.items():
                self._conn.execute(
                    "INSERT INTO instrument_aliases(figi,alias,valid_from) VALUES (?,?,?)",
                    (instrument.figi, alias, to_date(valid_from).isoformat()),
                )
        self._conn.commit()
        return instrument

    def get(self, figi: str) -> Instrument:
        row = self._conn.execute(
            "SELECT * FROM instruments WHERE figi=?", (figi,)
        ).fetchone()
        if row is None:
            raise KeyError(f"未知证券: {figi}")
        return _row_to_instrument(row)

    def resolve(self, symbol_or_figi: str) -> Instrument:
        """用当前代码或曾用代码/figi 找到身份。"""
        row = self._conn.execute(
            "SELECT * FROM instruments WHERE figi=? OR symbol=?",
            (symbol_or_figi, symbol_or_figi),
        ).fetchone()
        if row is not None:
            return _row_to_instrument(row)
        row = self._conn.execute(
            """SELECT i.* FROM instruments i
               JOIN instrument_aliases a ON a.figi=i.figi
               WHERE a.alias=?""",
            (symbol_or_figi,),
        ).fetchone()
        if row is None:
            raise KeyError(f"无法解析证券代码: {symbol_or_figi}")
        return _row_to_instrument(row)

    def list_all(self) -> list[Instrument]:
        rows = self._conn.execute("SELECT * FROM instruments ORDER BY figi").fetchall()
        return [_row_to_instrument(r) for r in rows]

    def retire(self, figi: str, last_date) -> None:
        self._conn.execute(
            "UPDATE instruments SET last_date=? WHERE figi=?",
            (to_date(last_date).isoformat(), figi),
        )
        self._conn.commit()


def _row_to_instrument(row) -> Instrument:
    return Instrument(
        figi=row["figi"],
        symbol=row["symbol"],
        name=row["name"],
        currency=row["currency"],
        exchange=row["exchange"],
        lot_size=row["lot_size"],
        first_date=date.fromisoformat(row["first_date"]) if row["first_date"] else None,
        last_date=date.fromisoformat(row["last_date"]) if row["last_date"] else None,
    )
