"""数据集门面：建库、保存、重开与来源一致性校验。

数据集目录结构::

    my_dataset/
      data.mrdb          SQLite 数据库（全部数据）
      manifest.json      数据集身份、表行数与内容哈希

重开时会重算哈希与 manifest 比对；不一致则报错，防止拿到被改动过的数据。
本系统完全离线，不联网下载任何行情。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .calendar import TradingCalendar
from .events import EventStore
from .instruments import InstrumentRegistry
from .quotes import QuoteStore
from .storage import SCHEMA_VERSION, connect, init_db

TABLES = (
    "instruments", "instrument_aliases", "trading_calendar",
    "quote_versions", "corporate_action_versions", "halt_versions",
    "ingest_batches",
)


class MarketDataset:
    """一个自包含、可重开、可校验的历史行情数据集。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.db_path = self.root / "data.mrdb"
        self.manifest_path = self.root / "manifest.json"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.instruments = InstrumentRegistry(self.conn)
        self.quotes = QuoteStore(self.conn)
        self.events = EventStore(self.conn)
        self._calendars: dict[str, TradingCalendar] = {}

    def calendar(self, exchange: str = "SSE") -> TradingCalendar:
        if exchange not in self._calendars:
            self._calendars[exchange] = TradingCalendar(self.conn, exchange)
        return self._calendars[exchange]

    # ------------------------------------------------------------ 保存/校验
    def _fingerprint(self) -> dict:
        """对每张表的全量行做稳定哈希（行排序后拼接）。"""
        fp = {}
        for table in TABLES:
            cols = self.conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
            col_names = [c["name"] for c in cols if c["name"] != "id"]
            order_col = "id" if any(c["name"] == "id" for c in cols) else col_names[0]
            rows = self.conn.execute(
                f"SELECT {','.join(col_names)} FROM {table} ORDER BY {order_col}"
            ).fetchall()
            h = hashlib.sha256()
            for row in rows:
                h.update(repr(tuple(row)).encode())
                h.update(b"\n")
            fp[table] = {"rows": len(rows), "sha256": h.hexdigest()}
        return fp

    def save(self) -> dict:
        """持久化（SQLite 已即时落盘），并写出/更新 manifest。"""
        self.conn.commit()
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {
                "dataset_id": uuid.uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "schema_version": SCHEMA_VERSION,
            }
        manifest["saved_at"] = datetime.now(timezone.utc).isoformat()
        manifest["fingerprint"] = self._fingerprint()
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.manifest_path)
        return manifest

    def verify(self) -> None:
        """核对当前数据库内容与保存时的 manifest 哈希是否一致。"""
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"缺少 manifest，数据集未保存过: {self.manifest_path}")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        saved = manifest["fingerprint"]
        current = self._fingerprint()
        if saved != current:
            changed = [t for t in TABLES if saved.get(t) != current.get(t)]
            raise ValueError(f"数据集内容与保存时不一致，变更表: {changed}")

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_dataset(root: str | Path, *, verify: bool = True) -> MarketDataset:
    """重开数据集；verify=True 时先校验来源一致性再返回。"""
    ds = MarketDataset(root)
    if verify and ds.manifest_path.exists():
        ds.verify()
    return ds
