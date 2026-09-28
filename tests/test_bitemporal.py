"""双时态行情核心行为测试，全部使用虚构数据、离线运行。"""

from __future__ import annotations

from datetime import date

import pytest

from market_research.backtest import PointInTimeView
from market_research.dataset import MarketDataset
from market_research.instruments import Instrument


@pytest.fixture()
def ds(tmp_path):
    with MarketDataset(tmp_path / "ds") as dataset:
        cal = dataset.calendar("XSHG")
        cal.fill_weekdays("2026-01-05", "2026-01-16")
        dataset.instruments.register(
            Instrument(figi="BBG000FAKE001", symbol="600FAK",
                       name="云岚科技（虚构）", exchange="XSHG"),
            received_at="2026-01-05T09:00:00+08:00",
            aliases={"600YUN": "2026-01-05"},
        )
        yield dataset


def test_instrument_alias_resolution(ds):
    assert ds.instruments.resolve("600FAK").figi == "BBG000FAKE001"
    assert ds.instruments.resolve("600YUN").figi == "BBG000FAKE001"
    assert ds.instruments.resolve("BBG000FAKE001").symbol == "600FAK"


def test_calendar(ds):
    cal = ds.calendar("XSHG")
    assert cal.is_open("2026-01-05") is True      # 周一
    assert cal.is_open("2026-01-10") is False     # 周六
    cal.add_holidays(["2026-01-09"])
    assert cal.is_open("2026-01-09") is False
    assert cal.next_open("2026-01-09") == date(2026, 1, 12)
    assert cal.prev_open("2026-01-09") == date(2026, 1, 8)


def test_insert_duplicate_revision_chain(ds):
    q = ds.quotes
    a1 = q.upsert("BBG000FAKE001", "2026-01-05", close=10.0, volume=100,
                  source="S", source_record_id="x1",
                  received_at="2026-01-05T18:00:00+08:00")
    a2 = q.upsert("BBG000FAKE001", "2026-01-05", close=10.0, volume=100,
                  source="S", source_record_id="x1",
                  received_at="2026-01-05T19:00:00+08:00")
    a3 = q.upsert("BBG000FAKE001", "2026-01-05", close=10.2, volume=100,
                  source="S", source_record_id="x2",
                  received_at="2026-01-06T09:00:00+08:00")
    assert (a1, a2, a3) == ("inserted", "duplicate", "revised")

    chain = q.history("BBG000FAKE001", "2026-01-05")
    assert len(chain) == 2                 # 重复记录不产生新版本
    assert chain[0].close == 10.0 and chain[0].superseded_at is not None
    assert chain[1].close == 10.2 and chain[1].is_current


def test_as_of_cannot_see_future_revision(ds):
    q = ds.quotes
    q.upsert("BBG000FAKE001", "2026-01-05", close=10.0,
             source="初报", received_at="2026-01-05T18:00:00+08:00")
    q.upsert("BBG000FAKE001", "2026-01-05", close=10.2,
             source="更正", received_at="2026-01-08T09:00:00+08:00")

    assert q.get_as_of("BBG000FAKE001", "2026-01-05",
                       "2026-01-05T19:00:00+08:00").close == 10.0
    assert q.get_as_of("BBG000FAKE001", "2026-01-05",
                       "2026-01-06T12:00:00+08:00").close == 10.0
    assert q.get_as_of("BBG000FAKE001", "2026-01-05",
                       "2026-01-08T10:00:00+08:00").close == 10.2
    # 数据尚未送达时查不到
    assert q.get_as_of("BBG000FAKE001", "2026-01-05",
                       "2026-01-05T17:00:00+08:00") is None


def test_late_arriving_backfill_does_not_pollute_early_backtest(ds):
    q = ds.quotes
    q.upsert("BBG000FAKE001", "2026-01-06", close=10.4, volume=None,
             source="初报", received_at="2026-01-06T18:00:00+08:00")
    early = PointInTimeView(ds, "2026-01-06T19:00:00+08:00")
    bar = early.bar("BBG000FAKE001", "2026-01-06")
    assert bar.volume is None and bar.close == 10.4

    q.upsert("BBG000FAKE001", "2026-01-06", close=10.4, volume=1_200_000,
             source="补数", received_at="2026-01-12T09:00:00+08:00")
    assert early.bar("BBG000FAKE001", "2026-01-06").volume is None
    late = PointInTimeView(ds, "2026-01-12T10:00:00+08:00")
    assert late.bar("BBG000FAKE001", "2026-01-06").volume == 1_200_000


def test_halt_only_known_after_notification(ds):
    ds.events.record_halt("BBG000FAKE001", "2026-01-07", "HALTED",
                          source="交易所", received_at="2026-01-07T13:05:00+08:00")
    e = ds.events
    assert e.status_as_of("BBG000FAKE001", "2026-01-07",
                          "2026-01-07T12:00:00+08:00") is None
    assert e.status_as_of("BBG000FAKE001", "2026-01-07",
                          "2026-01-07T14:00:00+08:00") == "HALTED"
    # 停牌修订：HALTED -> RESUMED
    ds.events.record_halt("BBG000FAKE001", "2026-01-07", "RESUMED",
                          source="交易所", received_at="2026-01-07T14:30:00+08:00")
    assert e.status_as_of("BBG000FAKE001", "2026-01-07",
                          "2026-01-07T14:00:00+08:00") == "HALTED"
    assert e.status_as_of("BBG000FAKE001", "2026-01-07",
                          "2026-01-07T15:00:00+08:00") == "RESUMED"


def test_split_adjustment_uses_only_known_factor(ds):
    q, e = ds.quotes, ds.events
    q.upsert("BBG000FAKE001", "2026-01-05", close=10.0,
             source="S", received_at="2026-01-05T18:00:00+08:00")
    e.record_split("BBG000FAKE001", "2026-01-08", 2.0,
                   source="公告", received_at="2026-01-06T10:00:00+08:00")
    before = PointInTimeView(ds, "2026-01-06T09:00:00+08:00")
    after = PointInTimeView(ds, "2026-01-06T11:00:00+08:00")
    assert before.bar("BBG000FAKE001", "2026-01-05").close == 10.0
    assert after.bar("BBG000FAKE001", "2026-01-05").close == 5.0
    # 除权日及之后的价格不再前复
    assert e.adjustment_factor("BBG000FAKE001", "2026-01-08",
                               "2026-01-09T00:00:00+08:00") == 1.0


def test_save_reopen_verify_and_identical_queries(ds, tmp_path):
    q = ds.quotes
    q.upsert("BBG000FAKE001", "2026-01-05", close=10.0,
             source="初报", received_at="2026-01-05T18:00:00+08:00")
    q.upsert("BBG000FAKE001", "2026-01-05", close=10.2,
             source="更正", received_at="2026-01-06T09:00:00+08:00")
    root = ds.root
    expected = [
        (v.trade_date.isoformat(), v.close, v.source)
        for v in q.merged_query("BBG000FAKE001", "2026-01-01", "2026-01-31",
                                as_of="2026-01-07T00:00:00+08:00")
    ]
    ds.save()
    ds.close()

    from market_research.dataset import open_dataset
    with open_dataset(root, verify=True) as ds2:
        got = [
            (v.trade_date.isoformat(), v.close, v.source)
            for v in ds2.quotes.merged_query(
                "BBG000FAKE001", "2026-01-01", "2026-01-31",
                as_of="2026-01-07T00:00:00+08:00")
        ]
        assert got == expected == [("2026-01-05", 10.2, "更正")]


def test_verify_detects_tampering(ds):
    ds.quotes.upsert("BBG000FAKE001", "2026-01-05", close=10.0,
                     source="S", received_at="2026-01-05T18:00:00+08:00")
    ds.save()
    ds.close()
    import sqlite3
    conn = sqlite3.connect(ds.db_path)
    conn.execute("UPDATE quote_versions SET close=999 WHERE trade_date='2026-01-05'")
    conn.commit()
    conn.close()
    from market_research.dataset import open_dataset
    with pytest.raises(ValueError, match="不一致"):
        open_dataset(ds.root, verify=True)
