"""公司行动（拆股 / 换股合并 / 现金补偿）与持仓调整测试。"""

import datetime as dt
import os
import tempfile
import unittest

from histquote import (
    CodeQuoteEvent, CorporateAction, Dataset, IdentityAnnouncement, Quote,
    QuoteStatus, Security,
)

D = dt.date
T = dt.datetime


class CorporateActionTests(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset(":memory:", name="corp")
        self.ds.add_security(Security("old", "000001", "旧公司"))
        self.ds.add_security(Security("new", "000002", "新公司"))
        self.ds.calendar.add_sessions([D(2024, 3, 1), D(2024, 3, 4)])

    def add_action(self, kind, sid="old", ex=D(2024, 3, 4),
                   received=T(2024, 3, 1, 9), aid="a1", **kwargs):
        self.ds.add_corporate_action(CorporateAction(
            sid, ex, received, kind, action_id=aid, **kwargs))

    def test_split_announce_revise_withdraw_viewpoints(self):
        ds = self.ds
        self.add_action("split", ratio=2.0, aid="a1",
                        received=T(2024, 3, 1, 9))
        self.add_action("split", ratio=3.0, aid="a2",
                        received=T(2024, 3, 2, 9), supersedes="a1")
        holding = {"quantity": 100.0, "cost": 900.0}

        early = ds.corporate_actions_at("old", T(2024, 3, 1, 12))
        self.assertEqual([e["action"].action_id for e in early], ["a1"])
        plan_early = ds.plan_position_adjustment("a1", holding)
        self.assertEqual(plan_early["new_quantity"], 200.0)
        self.assertEqual(
            plan_early["cost_allocation"]["cost_per_share_after"], 4.5)

        later = ds.corporate_actions_at("old", T(2024, 3, 2, 12))
        self.assertEqual([e["action"].action_id for e in later], ["a2"])
        self.assertEqual(later[0]["chain"], ("a1", "a2"))
        plan_later = ds.plan_position_adjustment("a2", holding)
        self.assertEqual(plan_later["new_quantity"], 300.0)

        ds.add_corporate_action(CorporateAction(
            "old", D(2024, 3, 4), T(2024, 3, 3, 9), "withdraw",
            action_id="w1", target_action_id="a2"))
        restored = ds.corporate_actions_at("old", T(2024, 3, 3, 12))
        self.assertEqual([e["action"].action_id for e in restored], ["a1"])
        self.assertEqual(restored[0]["withdrawn_ids"], ("a2",))
        still = ds.corporate_actions_at("old", T(2024, 3, 2, 12))
        self.assertEqual([e["action"].action_id for e in still], ["a2"])

    def test_merger_fraction_cash_and_unconfirmed_identity(self):
        ds = self.ds
        self.add_action(
            "merger", ratio=0.4, aid="m1",
            new_security_candidates=("new", "other"),
            cash_per_share=0.5, fraction_policy="cash",
            fraction_cash_price=12.5)
        holding = {"quantity": 103.0, "cost": 2060.0}
        plan = ds.plan_position_adjustment("m1", holding)
        self.assertEqual(plan["new_quantity"], 41.0)
        self.assertAlmostEqual(plan["fractional_shares"], 0.2)
        self.assertAlmostEqual(plan["fraction_cash"], 2.5)
        self.assertAlmostEqual(plan["cash_compensation"], 51.5)
        self.assertEqual(plan["new_security_id"], "")
        self.assertEqual(plan["new_security_candidates"], ["new", "other"])
        self.assertIn("new-security-identity-unconfirmed", plan["undetermined"])
        self.assertEqual(plan["return_interpretation"],
                         "corporate-action-not-investment-return")
        cost = plan["cost_allocation"]
        self.assertAlmostEqual(cost["cost_after"], 2060.0 - 51.5 - 2.5)

        self.add_action("merger", ratio=0.4, aid="m2", supersedes="m1",
                        new_security_id="new", cash_per_share=0.5,
                        fraction_policy="cash", fraction_cash_price=12.5,
                        received=T(2024, 3, 2, 9))
        confirmed = ds.plan_position_adjustment("m2", holding)
        self.assertEqual(confirmed["new_security_id"], "new")
        self.assertEqual(confirmed["undetermined"], [])

    def test_compare_original_vs_revised_plan(self):
        ds = self.ds
        self.add_action("merger", ratio=0.4, aid="m1",
                        new_security_id="new", cash_per_share=0.5,
                        fraction_policy="cash", fraction_cash_price=12.5)
        self.add_action("merger", ratio=0.5, aid="m2", supersedes="m1",
                        new_security_id="new", cash_per_share=0.2,
                        fraction_policy="cash", fraction_cash_price=12.5,
                        received=T(2024, 3, 2, 9))
        holding = {"quantity": 100.0, "cost": 2000.0}
        cmp = ds.compare_action_plans("m1", "m2", holding)
        self.assertEqual(cmp["changed_fields"]["new_quantity"],
                         {"before": 40.0, "after": 50.0})
        self.assertEqual(cmp["changed_fields"]["cash_compensation"],
                         {"before": 50.0, "after": 20.0})
        self.assertIn("cost_after", cmp["changed_fields"])
        self.assertEqual(cmp["original"]["action_id"], "m1")
        self.assertEqual(cmp["revised"]["action_id"], "m2")

    def test_partial_adoption_never_double_counts(self):
        ds = self.ds
        self.add_action("merger", ratio=1.0, aid="m1", new_security_id="new")
        first = ds.adopt_corporate_action(
            "m1", "h1", holding_quantity=100.0, quantity=60.0, cost=1200.0,
            adopted_at=T(2024, 3, 4, 10))
        self.assertEqual(first["remaining_quantity"], 40.0)
        second = ds.adopt_corporate_action(
            "m1", "h1", holding_quantity=100.0, quantity=40.0, cost=800.0,
            adopted_at=T(2024, 3, 4, 11))
        self.assertEqual(second["remaining_quantity"], 0.0)
        with self.assertRaises(ValueError):
            ds.adopt_corporate_action(
                "m1", "h1", holding_quantity=100.0, quantity=1.0, cost=20.0,
                adopted_at=T(2024, 3, 4, 12))

        trail = ds.holding_adjustments(holding_id="h1")
        self.assertEqual(len(trail), 2)
        self.assertEqual([t["action_id"] for t in trail], ["m1", "m1"])
        by_new = ds.holding_adjustments(security_id="new")
        self.assertEqual(len(by_new), 2)

    def test_chained_actions_and_late_announcement(self):
        ds = self.ds
        self.add_action("split", ratio=2.0, aid="s1",
                        ex=D(2024, 3, 4), received=T(2024, 3, 1, 9))
        self.add_action("merger", ratio=0.5, aid="m1", new_security_id="new",
                        ex=D(2024, 3, 4), received=T(2024, 3, 1, 10))
        chain = ds.apply_action_chain(
            {"quantity": 100.0, "cost": 1000.0}, ["s1", "m1"])
        self.assertEqual(chain["final"]["quantity"], 100.0)
        self.assertEqual(len(chain["steps"]), 2)
        self.assertEqual(chain["steps"][0]["kind"], "split")
        self.assertEqual(chain["steps"][1]["kind"], "merger")

        before = ds.corporate_actions_at("old", T(2024, 2, 28, 18))
        self.assertEqual(before, [])

    def test_late_old_code_quote_and_price_jump_not_return(self):
        ds = self.ds
        bd = D(2024, 3, 1)
        ds.add_identity_announcement(IdentityAnnouncement(
            "000001", "old", D(2024, 1, 1), T(2024, 1, 2, 9),
            action="confirm", announcement_id="ia1",
            effective_to=D(2024, 3, 1)))
        ds.add_code_event(CodeQuoteEvent(
            "000001", bd, T(2024, 3, 5, 9), "raw",
            Quote(10, 10, 10, 10.0, 100), source="feed",
            source_record="late-old"))
        view = ds.quote_for_code("000001", bd, T(2024, 3, 5, 10))
        self.assertEqual(view.status, QuoteStatus.OK)
        self.assertEqual(view.security_id, "old")
        self.assertEqual(view.quote.close, 10.0)

        self.add_action("merger", ratio=0.5, aid="m1",
                        new_security_id="new")
        plan = ds.plan_position_adjustment(
            "m1", {"quantity": 100.0, "cost": 1000.0})
        self.assertEqual(plan["return_interpretation"],
                         "corporate-action-not-investment-return")
        self.assertEqual(plan["new_quantity"], 50.0)

    def test_adjustments_survive_save_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "research.db")
            ds = Dataset(path)
            ds.add_security(Security("old", "000001", "旧公司"))
            ds.add_corporate_action(CorporateAction(
                "old", D(2024, 3, 4), T(2024, 3, 1, 9), "split",
                action_id="s1", ratio=2.0))
            ds.adopt_corporate_action(
                "s1", "h1", holding_quantity=100.0, quantity=100.0,
                cost=900.0, adopted_at=T(2024, 3, 4, 10))
            before = ds.snapshot_info()
            ds.close()

            with Dataset.open(path) as reopened:
                self.assertEqual(reopened.snapshot_info(), before)
                trail = reopened.holding_adjustments(holding_id="h1")
                self.assertEqual(len(trail), 1)
                self.assertEqual(trail[0]["plan"]["new_quantity"], 200.0)
                plan = reopened.plan_position_adjustment(
                    "s1", {"quantity": 100.0, "cost": 900.0})
                self.assertEqual(plan["new_quantity"], 200.0)


if __name__ == "__main__":
    unittest.main()
