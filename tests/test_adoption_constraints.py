import datetime as dt
import unittest

from histquote import (
    AdoptionDecision, CorporateAction, Dataset, HoldingLot, withdraw_decision,
)

D = dt.date
T = dt.datetime


def exchange(action_id, family, from_sid, to_sid, from_ratio, to_ratio,
             effective, received, **kwargs):
    defaults = {
        "action_id": action_id, "family_id": family,
        "action_type": "exchange", "from_security_id": from_sid,
        "to_security_id": to_sid, "from_ratio": from_ratio,
        "to_ratio": to_ratio, "effective_date": effective,
        "received_at": received, "announcement_kind": "revision",
    }
    defaults.update(kwargs)
    return CorporateAction(**defaults)


class AdoptionConstraintTests(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset(":memory:", name="constraints")
        self.holdings = [HoldingLot("lot-a", "old", 4.0, 400.0)]

    def test_chained_exchanges_derived_lot_feeds_next_action(self):
        self.ds.add_corporate_action(exchange(
            "ex1", "f1", "old", "mid", 2, 1, D(2024, 2, 1), T(2024, 2, 1, 9)))
        self.ds.add_corporate_action(exchange(
            "ex2", "f2", "mid", "new", 1, 3, D(2024, 3, 1), T(2024, 3, 1, 9)))
        decisions = [
            AdoptionDecision("d1", "lot-a", "ex1", 4.0, status="confirmed"),
            AdoptionDecision("d2", "lot-a:ex1", "ex2", 2.0),
        ]
        result = self.ds.apply_adoption_constraints(
            self.holdings, decisions, D(2024, 3, 1), T(2024, 3, 1, 10))
        self.assertEqual(result["blocked"], ())
        positions = {p["security_id"]: p for p in result["positions"]}
        self.assertNotIn("old", positions)
        self.assertNotIn("mid", positions)
        self.assertEqual(positions["new"]["quantity"], 6.0)
        self.assertEqual(positions["new"]["cost"], 400.0)
        self.assertEqual(positions["new"]["provenance"], "exchange-proceed")
        self.assertEqual(positions["new"]["origin_lot_id"], "lot-a")
        total_cost = sum(p["cost"] for p in result["positions"])
        self.assertAlmostEqual(total_cost, 400.0)

    def test_fractional_cash_compensation_conservation(self):
        self.ds.add_corporate_action(exchange(
            "ex1", "f1", "old", "new", 1, 1, D(2024, 2, 1), T(2024, 2, 1, 9),
            cash_per_from_share=0.5, cash_cost_portion=0.1,
            fractional_cash_rate=12.0, fractional_destination="cash"))
        holdings = [HoldingLot("lot-a", "old", 3.5, 350.0)]
        decisions = [AdoptionDecision("d1", "lot-a", "ex1", 3.5,
                                      status="confirmed")]
        result = self.ds.apply_adoption_constraints(
            holdings, decisions, D(2024, 2, 1), T(2024, 2, 1, 10))
        adj = result["adjustments"][0]
        self.assertEqual(adj["whole_quantity"], 3.0)
        self.assertEqual(adj["fractional_quantity"], 0.5)
        cash = result["cash_compensations"][0]
        self.assertEqual(cash["compensation"], 1.75)
        self.assertEqual(cash["fractional_cash"], 6.0)
        self.assertEqual(result["cash_total"], 7.75)
        self.assertEqual(result["confirmed_cash_total"], 7.75)
        self.assertEqual(result["pending_cash_total"], 0.0)
        new_cost = sum(p["cost"] for p in result["positions"])
        self.assertAlmostEqual(new_cost + cash["allocated_cost"], 350.0)

    def test_partial_adoption_retain_and_await_split_budget(self):
        self.ds.add_corporate_action(exchange(
            "ex1", "f1", "old", "new", 1, 1, D(2024, 2, 1), T(2024, 2, 1, 9),
            fractional_destination="retain"))
        decisions = [
            AdoptionDecision("d1", "lot-a", "ex1", 1.5),
            AdoptionDecision("d2", "lot-a", "ex1", 1.0, mode="retain"),
            AdoptionDecision("d3", "lot-a", "ex1", 1.5,
                             mode="await_identity"),
        ]
        result = self.ds.apply_adoption_constraints(
            self.holdings, decisions, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(result["blocked"], ())
        positions = {p["security_id"]: p for p in result["positions"]}
        self.assertEqual(positions["new"]["quantity"], 1.5)
        self.assertEqual(positions["old"]["quantity"], 2.5)
        self.assertEqual(result["retained"][0]["quantity"], 1.0)
        self.assertEqual(result["awaiting"][0]["quantity"], 1.5)
        total_qty = sum(p["quantity"] for p in result["positions"])
        self.assertAlmostEqual(total_qty, 4.0)

    def test_over_adoption_and_cycle_are_blocked(self):
        self.ds.add_corporate_action(exchange(
            "ex1", "f1", "old", "new", 1, 1, D(2024, 2, 1), T(2024, 2, 1, 9)))
        decisions = [
            AdoptionDecision("d1", "lot-a", "ex1", 3.0),
            AdoptionDecision("d2", "lot-a", "ex1", 2.0),
        ]
        result = self.ds.apply_adoption_constraints(
            self.holdings, decisions, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(len(result["blocked"]), 2)
        self.assertIn("超额采用", result["blocked"][0]["reason"])
        self.assertEqual(result["adjustments"], ())

        ds2 = Dataset(":memory:", name="cycle")
        ds2.add_corporate_action(exchange(
            "ex-a", "fa", "s1", "s2", 1, 1, D(2024, 2, 1), T(2024, 2, 1, 9)))
        ds2.add_corporate_action(exchange(
            "ex-b", "fb", "s2", "s1", 1, 1, D(2024, 3, 1), T(2024, 3, 1, 9)))
        cyclic = [
            AdoptionDecision("c1", "lot-x", "ex-a", 1.0),
            AdoptionDecision("c2", "lot-x:ex-a", "ex-b", 1.0),
        ]
        outcome = ds2.apply_adoption_constraints(
            [HoldingLot("lot-x", "s1", 1.0, 10.0)],
            cyclic, D(2024, 3, 1), T(2024, 3, 1, 10))
        self.assertEqual(len(outcome["blocked"]), 2)
        self.assertIn("循环引用", outcome["blocked"][0]["reason"])
        self.assertEqual(outcome["adjustments"], ())
        ds2.close()

    def test_existing_new_security_not_mixed_with_exchange_proceeds(self):
        self.ds.add_corporate_action(exchange(
            "ex1", "f1", "old", "new", 2, 1, D(2024, 2, 1), T(2024, 2, 1, 9)))
        holdings = [
            HoldingLot("lot-a", "old", 4.0, 400.0),
            HoldingLot("lot-b", "new", 5.0, 250.0),
        ]
        decisions = [AdoptionDecision("d1", "lot-a", "ex1", 4.0)]
        result = self.ds.apply_adoption_constraints(
            holdings, decisions, D(2024, 2, 1), T(2024, 2, 1, 10))
        new_lots = [p for p in result["positions"] if p["security_id"] == "new"]
        self.assertEqual(len(new_lots), 2)
        by_lot = {p["lot_id"]: p for p in new_lots}
        self.assertEqual(by_lot["lot-b"]["quantity"], 5.0)
        self.assertEqual(by_lot["lot-b"]["cost"], 250.0)
        self.assertEqual(by_lot["lot-b"]["provenance"], "original")
        self.assertEqual(by_lot["lot-a:ex1"]["quantity"], 2.0)
        self.assertEqual(by_lot["lot-a:ex1"]["cost"], 400.0)
        self.assertEqual(by_lot["lot-a:ex1"]["provenance"], "exchange-proceed")

    def test_adoption_order_permutations_are_consistent(self):
        self.ds.add_corporate_action(exchange(
            "ex1", "f1", "old", "mid", 2, 1, D(2024, 2, 1), T(2024, 2, 1, 9),
            cash_per_from_share=0.25))
        self.ds.add_corporate_action(exchange(
            "ex2", "f2", "mid", "new", 1, 1, D(2024, 3, 1), T(2024, 3, 1, 9)))
        decisions = [
            AdoptionDecision("d1", "lot-a", "ex1", 2.0, status="confirmed"),
            AdoptionDecision("d2", "lot-a", "ex1", 1.0, mode="retain"),
            AdoptionDecision("d3", "lot-a:ex1", "ex2", 1.0),
        ]
        comparison = self.ds.compare_adoption_orders(
            self.holdings, decisions, D(2024, 3, 1), T(2024, 3, 1, 10))
        self.assertTrue(comparison["consistent"])
        self.assertEqual(comparison["orderings_checked"], 6)
        result = comparison["result"]
        self.assertEqual(result["cash_total"], 0.5)
        positions = {p["security_id"]: p for p in result["positions"]}
        self.assertEqual(positions["new"]["quantity"], 1.0)
        self.assertEqual(positions["old"]["quantity"], 2.0)

    def test_late_revision_replay_and_withdraw_reallocate(self):
        self.ds.add_corporate_action(exchange(
            "ex1", "f1", "old", "new", 2, 1, D(2024, 2, 1), T(2024, 2, 1, 9),
            cash_per_from_share=1.0, cash_cost_portion=0.2))
        decisions = [
            AdoptionDecision("d1", "lot-a", "ex1", 2.0, status="confirmed"),
            AdoptionDecision("d2", "lot-a", "ex1", 2.0),
        ]
        before = self.ds.apply_adoption_constraints(
            self.holdings, decisions, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(before["cash_total"], 4.0)
        self.assertEqual(before["confirmed_cash_total"], 2.0)

        self.ds.add_corporate_action(exchange(
            "ex1-r", "f1", "old", "new", 1, 1, D(2024, 2, 1),
            T(2024, 2, 5, 9), cash_per_from_share=1.0,
            cash_cost_portion=0.2, supersedes="ex1"))
        replay = self.ds.apply_adoption_constraints(
            self.holdings, decisions, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(replay["positions"], before["positions"])
        self.assertEqual(replay["cash_total"], before["cash_total"])

        updated = withdraw_decision(decisions, "d2")
        reopened = self.ds.apply_adoption_constraints(
            self.holdings, updated, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(reopened["withdrawn_decisions"], ("d2",))
        positions = {p["security_id"]: p for p in reopened["positions"]}
        self.assertEqual(positions["old"]["quantity"], 2.0)
        self.assertEqual(positions["new"]["quantity"], 1.0)
        self.assertEqual(reopened["confirmed_cash_total"], 2.0)
        self.assertEqual(reopened["cash_total"], 2.0)

        updated = updated + [
            AdoptionDecision("d3", "lot-a", "ex1", 2.0, status="confirmed"),
        ]
        final = self.ds.apply_adoption_constraints(
            self.holdings, updated, D(2024, 2, 1), T(2024, 2, 1, 10))
        positions = {p["security_id"]: p for p in final["positions"]}
        self.assertNotIn("old", positions)
        self.assertEqual(positions["new"]["quantity"], 2.0)
        self.assertEqual(final["cash_total"], 4.0)
        total_qty = sum(p["quantity"] for p in final["positions"])
        self.assertAlmostEqual(total_qty, 2.0)
        allocated = sum(c["allocated_cost"]
                        for c in final["cash_compensations"])
        new_cost = sum(p["cost"] for p in final["positions"])
        self.assertAlmostEqual(new_cost + allocated, 400.0)

        with self.assertRaises(ValueError):
            withdraw_decision(updated, "d1")


if __name__ == "__main__":
    unittest.main()
