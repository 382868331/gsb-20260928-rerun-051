"""证券身份映射、跨市场转板与组合研究对照测试。"""

import datetime as dt
import os
import tempfile
import unittest

from histquote import (
    CodeQuoteEvent, Dataset, IdentityAnnouncement, Quote, QuoteStatus, Security,
)

D = dt.date
T = dt.datetime


class IdentityPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset(":memory:", name="identity")
        self.ds.add_security(Security("old", "000001", "旧公司"))
        self.ds.add_security(Security("new", "000001", "新公司"))
        self.ds.calendar.add_sessions([D(2024, 1, 31), D(2024, 2, 1)])

    def announce(self, action, sid, effective, received, aid, **kwargs):
        self.ds.add_identity_announcement(IdentityAnnouncement(
            "000001", sid, effective, received, action=action,
            source="exch", source_record=aid, announcement_id=aid,
            name=kwargs.get("name", ""),
            target_announcement_id=kwargs.get("target", ""),
            effective_to=kwargs.get("effective_to"),
            reason=kwargs.get("reason", ""),
        ))

    def test_reassigned_code_late_quote_and_candidate(self):
        ds = self.ds
        bd = D(2024, 1, 31)
        ds.add_code_event(CodeQuoteEvent(
            "000001", bd, T(2024, 2, 1, 9, 30), "raw",
            Quote(1, 2, 1, 10.0, 10), source="feed", source_record="old-late"))

        unknown = ds.quote_for_code("000001", bd, T(2024, 2, 1, 10))
        self.assertEqual(unknown.status, QuoteStatus.IDENTITY_UNKNOWN)
        self.assertIsNone(unknown.quote)

        self.announce("candidate", "old", D(2024, 1, 1),
                      T(2024, 2, 1, 11), "c-old", name="旧公司候选")
        candidate = ds.quote_for_code("000001", bd, T(2024, 2, 1, 12))
        self.assertEqual(candidate.status, QuoteStatus.IDENTITY_CANDIDATE)
        self.assertEqual(candidate.identity_candidates, ("old",))
        self.assertIsNone(candidate.quote)

        self.announce("confirm", "old", D(2024, 1, 1),
                      T(2024, 2, 1, 13), "a-old", name="旧公司",
                      effective_to=D(2024, 1, 31))
        confirmed = ds.quote_for_code("000001", bd, T(2024, 2, 1, 14))
        self.assertEqual(confirmed.security_id, "old")
        self.assertEqual(confirmed.quote.close, 10.0)
        self.assertEqual(confirmed.identity_announcement_ids, ("a-old",))
        self.assertEqual(confirmed.source_record, "old-late")

        self.announce("confirm", "new", D(2024, 2, 1),
                      T(2024, 2, 1, 8), "a-new", name="新公司")
        ds.add_code_event(CodeQuoteEvent(
            "000001", D(2024, 2, 1), T(2024, 2, 1, 15), "raw",
            Quote(1, 2, 1, 20.0, 10), source="feed", source_record="q-new-code"))
        new_day = ds.quote_for_code("000001", D(2024, 2, 1), T(2024, 2, 1, 14))
        self.assertEqual(new_day.security_id, "new")
        self.assertEqual(new_day.status, QuoteStatus.MISSING)
        new_day_after = ds.quote_for_code(
            "000001", D(2024, 2, 1), T(2024, 2, 1, 16))
        self.assertEqual(new_day_after.security_id, "new")
        self.assertEqual(new_day_after.quote.close, 20.0)
        self.assertNotIn("old-late",
                         [c.source_record for c in new_day_after.candidates])

    def test_withdrawal_preserves_earlier_and_later_interpretations(self):
        ds = self.ds
        bd = D(2024, 1, 31)
        ds.add_code_event(CodeQuoteEvent(
            "000001", bd, T(2024, 1, 31, 15), "raw",
            Quote(1, 2, 1, 10.0, 10), source="feed", source_record="q-old"))
        self.announce("confirm", "old", D(2024, 1, 1),
                      T(2024, 1, 31, 14), "a1", name="旧公司")
        before = ds.quote_for_code("000001", bd, T(2024, 2, 1, 9))
        self.assertEqual(before.security_id, "old")
        self.assertEqual(before.quote.close, 10.0)

        self.announce("withdraw", "", D(2024, 1, 1),
                      T(2024, 2, 1, 10), "w1", target="a1")
        after = ds.quote_for_code("000001", bd, T(2024, 2, 1, 11))
        self.assertEqual(after.status, QuoteStatus.IDENTITY_UNKNOWN)
        self.assertEqual(after.identity_withdrawn_announcement_ids, ("a1",))
        self.assertIsNone(after.quote)

        replay = ds.quote_for_code("000001", bd, T(2024, 2, 1, 9))
        self.assertEqual(replay.security_id, "old")
        self.assertEqual(replay.quote.close, 10.0)
        self.assertEqual(replay.identity_announcement_ids, ("a1",))

    def test_portfolio_dedupes_same_security_once_and_classifies_changes(self):
        ds = self.ds
        bd = D(2024, 1, 31)
        ds.add_code_event(CodeQuoteEvent(
            "000001", bd, T(2024, 1, 31, 15), "raw",
            Quote(1, 2, 1, 10.0, 10), source="feed", source_record="q-old"))
        ds.add_event(__import__("histquote").QuoteEvent(
            "new", bd, T(2024, 1, 31, 15), "raw",
            Quote(1, 2, 1, 20.0, 10), source="feed", source_record="q-new"))
        self.announce("confirm", "old", D(2024, 1, 1),
                      T(2024, 1, 31, 14), "a-old", name="旧公司")

        holdings = [
            {"display_code": "000001", "weight": 1.0, "label": "代码仓"},
            {"security_id": "old", "weight": 1.0, "label": "稳定ID仓"},
            {"security_id": "new", "weight": 1.0, "label": "新公司"},
        ]
        result = ds.evaluate_portfolio(holdings, bd, T(2024, 1, 31, 16))
        self.assertEqual(result["security_ids"], ("old", "new"))
        self.assertEqual(result["portfolio_value"], 40.0)
        old_pos = result["positions"][0]
        self.assertEqual(old_pos["duplicate_positions"],
                         [{"position": 1, "label": "稳定ID仓", "weight": 1.0}])

        ds.add_code_event(CodeQuoteEvent(
            "000001", bd, T(2024, 2, 1, 9, 30), "revision",
            Quote(1, 2, 1, 11.0, 10), source="feed", source_record="q-old-2"))
        ds.identity_announcements  # 原公告仍保留
        ds.add_identity_announcement(IdentityAnnouncement(
            "000001", "old", D(2024, 1, 1), T(2024, 2, 1, 10),
            action="rename", name="旧公司改名", announcement_id="rename-old",
            source="exch", source_record="rename-old",
            effective_to=D(2024, 1, 31)))
        cmp = ds.compare_identity_adjustment(
            holdings, bd,
            known_at=T(2024, 1, 31, 16),
            revised_known_at=T(2024, 2, 1, 11),
            original_identity_as_of=T(2024, 1, 31, 16),
            revised_identity_as_of=T(2024, 2, 1, 11),
        )
        kinds = {kind for c in cmp["changes"] for kind in c["kinds"]}
        self.assertIn("true-price-change", kinds)
        self.assertIn("name-only-change", kinds)
        self.assertNotIn("security-scope-change", kinds)

    def test_identity_change_is_security_scope_not_price_series(self):
        ds = self.ds
        bd = D(2024, 2, 1)
        ds.add_code_event(CodeQuoteEvent(
            "000001", bd, T(2024, 2, 1, 15), "raw",
            Quote(1, 2, 1, 20.0, 10), source="feed", source_record="q-new"))
        self.announce("confirm", "old", D(2024, 1, 1),
                      T(2024, 1, 31, 14), "a-old", name="旧公司",
                      effective_to=D(2024, 1, 31))
        self.announce("confirm", "new", D(2024, 2, 1),
                      T(2024, 2, 1, 14), "a-new", name="新公司")
        cmp = ds.compare_identity_adjustment(
            [{"display_code": "000001", "weight": 1.0}],
            bd, T(2024, 2, 1, 16),
            original_identity_as_of=T(2024, 1, 31, 16),
            revised_identity_as_of=T(2024, 2, 1, 16),
        )
        self.assertEqual(cmp["changes"][0]["kinds"], ["security-scope-change"])
        self.assertEqual(cmp["added_security_ids"], ("new",))
        self.assertIsNone(cmp["changes"][0]["before"]["close"])
        self.assertEqual(cmp["changes"][0]["after"]["close"], 20.0)

    def test_saved_research_reopens_with_identity_as_of(self):
        ds = self.ds
        bd = D(2024, 1, 31)
        ds.add_code_event(CodeQuoteEvent(
            "000001", bd, T(2024, 1, 31, 15), "raw",
            Quote(1, 2, 1, 10.0, 10), source="feed", source_record="q"))
        self.announce("confirm", "old", D(2024, 1, 1),
                      T(2024, 1, 31, 14), "a1", name="旧公司")
        holdings = [{"display_code": "000001", "weight": 1.0}]
        ds.save_portfolio_research(
            "r1", holdings, bd, T(2024, 1, 31, 16), T(2024, 1, 31, 16))
        self.announce("withdraw", "", D(2024, 1, 1),
                      T(2024, 2, 1, 9), "w1", target="a1")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "research.db")
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as reopened_ds:
                check = reopened_ds.reopen_saved_research("r1")
                self.assertTrue(check["same_value"])
                self.assertTrue(check["same_security_ids"])
                self.assertEqual(
                    check["reopened"]["positions"][0]["security_id"], "old")
                self.assertEqual(
                    check["reopened"]["positions"][0]["close"], 10.0)


if __name__ == "__main__":
    unittest.main()
