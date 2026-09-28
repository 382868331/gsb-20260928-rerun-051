import datetime as dt
import tempfile
import unittest
from pathlib import Path

from histquote import Dataset, Quote, QuoteEvent, QuoteStatus, Security, Split


D = dt.date
T = dt.datetime


def make_ds(path=":memory:"):
    ds = Dataset(path, name="t")
    ds.add_security(Security("s1", "000001", "样例"))
    ds.calendar.add_sessions([D(2024, 1, d) for d in (8, 9, 10, 11, 12)])
    ds.add_event(QuoteEvent(
        "s1", D(2024, 1, 8), T(2024, 1, 8, 15, 30), "raw",
        Quote(10, 10.2, 9.9, 10.1, 100), source="feed", source_record="r1"))
    return ds


class BitemporalTests(unittest.TestCase):
    def test_no_future_revision_visible(self):
        ds = make_ds()
        ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 18), "revision",
            Quote(10, 10.2, 9.8, 9.8, 100), source="corr", source_record="r2"))

        before = ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 9, 9))
        after = ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 10, 9))
        self.assertEqual(before.quote.close, 10.1)
        self.assertEqual(after.quote.close, 9.8)
        self.assertEqual(before.source_record, "r1")
        self.assertEqual(after.source_record, "r2")

    def test_missing_vs_non_session(self):
        ds = make_ds()
        missing = ds.quote_at("s1", D(2024, 1, 9), T(2024, 1, 9, 16))
        weekend = ds.quote_at("s1", D(2024, 1, 13), T(2024, 1, 13, 16))
        self.assertEqual(missing.status, QuoteStatus.MISSING)
        self.assertEqual(weekend.status, QuoteStatus.NON_SESSION)

    def test_suspension_is_explicit(self):
        ds = make_ds()
        ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 10), T(2024, 1, 10, 9, 20), "suspension",
            None, reason="停牌", source="ann", source_record="sus1"))
        v = ds.quote_at("s1", D(2024, 1, 10), T(2024, 1, 10, 16))
        self.assertEqual(v.status, QuoteStatus.SUSPENDED)
        self.assertIsNone(v.quote)

    def test_cancel(self):
        ds = make_ds()
        ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 8, 16), "cancel",
            None, reason="错挂代码", source="feed", source_record="c1"))
        v = ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 8, 17))
        self.assertEqual(v.status, QuoteStatus.CANCELLED)
        # 撤销到达前，原始数据仍然可见
        earlier = ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 8, 15, 45))
        self.assertEqual(earlier.status, QuoteStatus.OK)

    def test_duplicate_is_idempotent(self):
        ds = make_ds()
        inserted, seq = ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 8, 15, 30), "raw",
            Quote(10, 10.2, 9.9, 10.1, 100), source="feed", source_record="r1"))
        self.assertFalse(inserted)
        self.assertEqual(ds.snapshot_info()["counts"]["quote_event"], 1)

    def test_duplicate_with_conflicting_content_raises(self):
        ds = make_ds()
        with self.assertRaises(ValueError):
            ds.add_event(QuoteEvent(
                "s1", D(2024, 1, 8), T(2024, 1, 8, 15, 30), "raw",
                Quote(10, 10.2, 9.9, 10.9, 100), source="feed", source_record="r1"))

    def test_split_adjustment_respects_received_time(self):
        ds = make_ds()
        ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 8, 15, 30), "raw",
            Quote(10, 10.2, 9.9, 10.0, 100), source="feed", source_record="r9"))
        ds.add_split(Split("s1", D(2024, 1, 10), 2.0, T(2024, 1, 12, 9)))
        before = ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 11, 9))
        after = ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 12, 12))
        self.assertEqual(before.adjusted_close, 10.0)
        self.assertEqual(after.adjusted_close, 5.0)

    def test_series_and_received_log(self):
        ds = make_ds()
        ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 9), T(2024, 1, 11, 10), "raw",
            Quote(1, 1, 1, 1, 1), source="late", source_record="late1"))
        views = ds.series("s1", D(2024, 1, 8), D(2024, 1, 10), T(2024, 1, 10, 9))
        self.assertEqual([v.status for v in views],
                         [QuoteStatus.OK, QuoteStatus.MISSING, QuoteStatus.MISSING])
        log = ds.received_log("s1", T(2024, 1, 8), T(2024, 1, 12))
        self.assertEqual([r["source_record"] for r in log], ["r1", "late1"])

    def test_reopen_preserves_results_and_sources(self):
        ds = make_ds()
        ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 18), "revision",
            Quote(10, 10.2, 9.8, 9.8, 100), source="corr", source_record="r2"))
        expected = ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 10)).as_dict()
        info = ds.snapshot_info()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.db"
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as d2:
                self.assertEqual(d2.snapshot_info(), info)
                got = d2.quote_at("s1", D(2024, 1, 8), T(2024, 1, 10)).as_dict()
                self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
