import datetime as dt
import tempfile
import unittest
from pathlib import Path

from histquote import Dataset, Quote, QuoteEvent, QuoteStatus, Security, SourceInfo, Split


D = dt.date
T = dt.datetime


class CrossSourceTests(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset(":memory:", name="t2")
        self.ds.add_security(Security("s1", "000001", "样例"))
        self.ds.calendar.add_sessions([D(2024, 1, d) for d in (8, 9, 10)])
        self.ds.add_source(SourceInfo("exchange", "交易所", 10), T(2024, 1, 1))
        self.ds.add_source(SourceInfo("vendor", "转发商", 5), T(2024, 1, 1))
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 8, 15, 30), "raw",
            Quote(10, 10.2, 9.9, 10.1, 100),
            source="exchange", source_record="ex1"))
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 8, 16), "raw",
            Quote(10, 10.1, 9.8, 10.0, 90),
            source="vendor", source_record="v1"))

    def test_conflict_arbitration_keeps_all_originals_and_reason(self):
        view = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 8, 17))
        self.assertEqual(view.status, QuoteStatus.OK)
        self.assertEqual(view.quote.close, 10.1)
        self.assertEqual((view.source, view.source_record), ("exchange", "ex1"))
        self.assertTrue(view.conflict)
        self.assertIn("优先级最高", view.decision_reason)
        states = {(c.source, c.source_record): c.state for c in view.candidates}
        self.assertEqual(states, {("exchange", "ex1"): "active",
                                  ("vendor", "v1"): "active"})

    def test_targeted_retraction_is_temporal_and_preserves_old_version(self):
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 10), "retraction",
            None, reason="官方撤回错误记录", source="exchange",
            source_record="w1", target_source="exchange", target_record="ex1"))

        before = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 9, 9))
        after = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 9, 11))
        self.assertEqual(before.source_record, "ex1")
        self.assertEqual(after.status, QuoteStatus.OK)
        self.assertEqual((after.source, after.source_record), ("vendor", "v1"))
        self.assertIn("官方撤回错误记录",
                      [c.reason for c in after.candidates if c.source_record == "w1"][0])
        ex1 = [c for c in after.candidates if c.source_record == "ex1"][0]
        self.assertEqual(ex1.state, "retracted")
        self.assertEqual(ex1.quote.close, 10.1)

    def test_revision_after_retraction_does_not_erase_history(self):
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 10), "retraction", None,
            source="exchange", source_record="w1",
            target_source="exchange", target_record="ex1"))
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 15), "revision",
            Quote(10, 10.1, 9.8, 10.0, 90),
            source="exchange", source_record="ex2"))

        old = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 8, 17))
        middle = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 9, 11))
        newest = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 9, 16))
        self.assertEqual(old.source_record, "ex1")
        self.assertEqual(middle.source_record, "v1")
        self.assertEqual(newest.source_record, "ex2")
        self.assertEqual(len(newest.candidates), 4)

    def test_priority_change_cannot_rewrite_earlier_research(self):
        before = self.ds.run_research(
            "s1", D(2024, 1, 8), D(2024, 1, 8), T(2024, 1, 9, 16))
        self.assertEqual(before["views"][0]["source_record"], "ex1")

        # 01-10 才改变裁决：转发商历史质量被认定高于当时推送。
        self.ds.add_source(SourceInfo("vendor", "转发商", 20), T(2024, 1, 10))
        explanation = self.ds.explain_change(
            "s1", D(2024, 1, 8), D(2024, 1, 8),
            T(2024, 1, 9, 16), T(2024, 1, 10, 12))
        after = explanation["new_research"]

        self.assertEqual(before["content_fingerprint"] != after["content_fingerprint"], True)
        self.assertTrue(explanation["content_changed"])
        self.assertEqual(after["views"][0]["source_record"], "v1")
        self.assertEqual(explanation["new_quote_events"], [])
        self.assertEqual(explanation["source_priority_changes"][0]["source"], "vendor")
        self.assertEqual(len(before["views"]), len(after["views"]))
        # 早期冻结视角仍是交易所记录，不被新裁决抹掉。
        self.assertEqual(before["views"][0]["source"], "exchange")

    def test_priority_history_rejects_backdated_change(self):
        with self.assertRaises(ValueError):
            self.ds.add_source(SourceInfo("vendor", "转发商", 99), T(2023, 12, 31))

    def test_record_received_after_earlier_retraction_is_not_auto_revived(self):
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 10), "retraction", None,
            source="vendor", source_record="w1",
            target_source="vendor", target_record="late-v1"))
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 11), "raw",
            Quote(1, 1, 1, 1, 1),
            source="vendor", source_record="late-v1"))
        view = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 9, 12))
        self.assertEqual(view.source_record, "ex1")
        self.assertEqual(
            [c.state for c in view.candidates if c.source_record == "late-v1"],
            ["retracted"],
        )

    def test_all_active_records_retracted_becomes_cancelled(self):
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 10), "retraction", None,
            source="exchange", source_record="w1",
            target_source="exchange", target_record="ex1"))
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 11), "retraction", None,
            source="vendor", source_record="w2",
            target_source="vendor", target_record="v1"))
        view = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 9, 12))
        self.assertEqual(view.status, QuoteStatus.CANCELLED)
        self.assertIsNone(view.quote)
        self.assertEqual({c.state for c in view.candidates if c.quote}, {"retracted"})

    def test_known_suspension_takes_precedence_over_conflicting_quotes(self):
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 8, 17, 5), "suspension", None,
            reason="临时停牌", source="exchange", source_record="sus1"))
        view = self.ds.quote_at("s1", D(2024, 1, 8), T(2024, 1, 8, 18))
        self.assertEqual(view.status, QuoteStatus.SUSPENDED)
        self.assertIsNone(view.quote)
        self.assertEqual(len([c for c in view.candidates if c.quote]), 2)

    def test_interleaved_research_recomputation_and_attribution(self):
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 10), "retraction", None,
            reason="错误价格撤回", source="exchange", source_record="w1",
            target_source="exchange", target_record="ex1"))
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 15), "revision",
            Quote(10, 10.1, 9.8, 10.0, 90),
            reason="官方修订", source="exchange", source_record="ex2"))
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 10), T(2024, 1, 10, 9, 20), "suspension", None,
            reason="重大事项停牌", source="exchange", source_record="sus1"))
        self.ds.add_split(Split("s1", D(2024, 1, 11), 2.0, T(2024, 1, 12, 9)))

        explanation = self.ds.explain_change(
            "s1", D(2024, 1, 8), D(2024, 1, 10),
            T(2024, 1, 8, 17), T(2024, 1, 12, 12))
        self.assertTrue(explanation["content_changed"])
        self.assertEqual(
            [e["kind"] for e in explanation["new_quote_events"]],
            ["retraction", "revision", "suspension"],
        )
        self.assertEqual(
            explanation["new_split_announcements"][0]["received_at"],
            "2024-01-12T09:00:00",
        )
        changed = {d["business_date"]: d for d in explanation["views_diff"]}
        self.assertIn("2024-01-08", changed)
        self.assertIn("2024-01-10", changed)
        self.assertEqual(
            changed["2024-01-08"]["before"]["adjusted_close"], 10.1)
        self.assertEqual(
            changed["2024-01-08"]["after"]["adjusted_close"], 5.0)
        self.assertEqual(
            changed["2024-01-10"]["after"]["status"], QuoteStatus.SUSPENDED.value)
        self.assertEqual(
            changed["2024-01-08"]["caused_by_quote_events"][0]["kind"], "retraction")
        self.assertEqual(
            changed["2024-01-08"]["caused_by_split_announcements"][0]["ratio"], 2.0)

    def test_save_reopen_preserves_adjudication_history(self):
        self.ds.add_event(QuoteEvent(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 10), "retraction", None,
            source="exchange", source_record="w1",
            target_source="exchange", target_record="ex1"))
        expected = self.ds.quote_at(
            "s1", D(2024, 1, 8), T(2024, 1, 9, 11)).as_dict()
        info = self.ds.snapshot_info()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v2.db"
            self.ds.save_as(path)
            self.ds.close()
            with Dataset.open(path) as reopened:
                self.assertEqual(reopened.snapshot_info(), info)
                self.assertEqual(
                    reopened.quote_at(
                        "s1", D(2024, 1, 8), T(2024, 1, 9, 11)).as_dict(),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
