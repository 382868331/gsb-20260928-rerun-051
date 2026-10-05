import unittest

from histquote import (
    AllocationDecision, CorporateAction, CorporateActionPortfolioResearch,
    HoldingLot, compare_adoption_order_consistency,
)


D = __import__("datetime").date
T = __import__("datetime").datetime


def ca(**kwargs):
    defaults = {
        "action_id": "a1",
        "family_id": "f1",
        "action_type": "exchange",
        "effective_date": D(2024, 2, 1),
        "received_at": T(2024, 2, 1, 9),
        "from_security_id": "old",
        "to_security_id": "mid",
        "from_ratio": 1.0,
        "to_ratio": 1.0,
        "announcement_kind": "revision",
    }
    defaults.update(kwargs)
    return CorporateAction(**defaults)


class ActionResearchTests(unittest.TestCase):
    def test_consecutive_exchanges_keep_lot_provenance_and_existing_new_separate(self):
        holdings = [
            HoldingLot("old-lot", "old", 3.0, 100.0, source="ledger"),
            HoldingLot("existing-mid", "mid", 2.0, 80.0, source="existing"),
        ]
        actions = [
            ca(action_id="old-to-mid", family_id="f1",
               fractional_destination="cash", fractional_cash_rate=10.0),
            ca(action_id="mid-to-new", family_id="f2",
               effective_date=D(2024, 3, 1),
               received_at=T(2024, 3, 1, 9),
               from_security_id="mid", to_security_id="new",
               from_ratio=2.0, to_ratio=1.0,
               cash_per_from_share=4.0, cash_cost_portion=0.25),
        ]
        book = CorporateActionPortfolioResearch(holdings)
        book.register_actions(actions)
        book.decide(AllocationDecision("old-to-mid", "old-lot", 3.0))
        book.decide(AllocationDecision(
            "mid-to-new", "old-lot:old-to-mid", 3.0))
        first = book.apply(["old-to-mid", "mid-to-new"])

        self.assertEqual(first["positions"], (
            {"security_id": "mid", "quantity": 2.0, "cost": 80.0,
             "available_quantity": 2.0, "reserved_quantity": 0.0,
             "lot_ids": ["existing-mid"]},
            {"security_id": "new", "quantity": 1.0, "cost": 75.0,
             "available_quantity": 1.0, "reserved_quantity": 0.0,
             "lot_ids": ["old-lot:old-to-mid:mid-to-new"]},
        ))
        adj = {a["action_id"]: a for a in first["adjustments"]}
        self.assertEqual(
            adj["old-to-mid"]["source_acquisition"], "existing")
        self.assertEqual(
            adj["mid-to-new"]["source_lot_id"], "old-lot:old-to-mid")
        self.assertEqual(
            adj["mid-to-new"]["source_acquisition"],
            "corporate-action:old-to-mid")
        self.assertAlmostEqual(first["cash_total"], 12.0)

        remaining_cost = 80.0 + 75.0 + 45.0
        self.assertAlmostEqual(remaining_cost, 200.0)

    def test_wait_reserves_shares_and_blocks_another_mutually_exclusive_destination(self):
        action = ca(confirmation_status="candidate", to_security_id="",
                   candidate_security_ids=("maybe-new",))
        book = CorporateActionPortfolioResearch([HoldingLot("old-lot", "old", 2.0, 50.0)])
        book.register_action(action)
        book.decide(AllocationDecision(
            "a1", "old-lot", 1.5, disposition="wait",
            decision_id="pending-1", status="pending"))
        with self.assertRaisesRegex(ValueError, "超额"):
            book.decide(AllocationDecision(
                "a1", "old-lot", 1.0, disposition="exchange",
                decision_id="over"))

        result = book.apply()
        self.assertEqual(result["positions"][0]["available_quantity"], 0.5)
        self.assertEqual(result["positions"][0]["reserved_quantity"], 1.5)
        self.assertEqual(result["cash_total"], 0.0)

    def test_retain_and_exchange_also_share_the_same_exclusive_capacity(self):
        action = ca()
        book = CorporateActionPortfolioResearch([HoldingLot("old-lot", "old", 2.0, 50.0)])
        book.register_action(action)
        book.decide(AllocationDecision(
            "a1", "old-lot", 1.0, disposition="retain", decision_id="keep"))
        with self.assertRaisesRegex(ValueError, "互斥去向合计超额"):
            book.decide(AllocationDecision(
                "a1", "old-lot", 2.0, disposition="exchange", decision_id="over"))

    def test_withdraw_pending_choice_reallocates_shares_but_keeps_confirmed_cash(self):
        actions = [
            ca(action_id="wait-action", to_security_id="",
               confirmation_status="candidate",
               candidate_security_ids=("maybe-new",)),
            ca(action_id="cash-action", family_id="f2",
               effective_date=D(2024, 2, 2),
               received_at=T(2024, 2, 2, 9),
               to_security_id="new", cash_per_from_share=5.0),
        ]
        book = CorporateActionPortfolioResearch([HoldingLot("old-lot", "old", 4.0, 100.0)])
        book.register_actions(actions)
        book.decide(AllocationDecision(
            "wait-action", "old-lot", 2.0, disposition="wait",
            decision_id="wait-1", status="pending"))
        book.apply()
        book.withdraw("wait-1")
        book.decide(AllocationDecision("cash-action", "old-lot", 2.0))
        result = book.apply(["wait-action", "cash-action"])

        self.assertEqual(result["positions"], (
            {"security_id": "new", "quantity": 2.0, "cost": 50.0,
             "available_quantity": 2.0, "reserved_quantity": 0.0,
             "lot_ids": ["old-lot:cash-action"]},
            {"security_id": "old", "quantity": 2.0, "cost": 50.0,
             "available_quantity": 2.0, "reserved_quantity": 0.0,
             "lot_ids": ["old-lot"]},
        ))
        self.assertEqual(result["cash_total"], 10.0)
        with self.assertRaisesRegex(ValueError, "已确认"):
            book.withdraw("cash-action:old-lot")

    def test_cycle_is_blocked_from_lineage(self):
        actions = [
            ca(action_id="a-to-b", to_security_id="b"),
            ca(action_id="b-to-a", family_id="f2",
               effective_date=D(2024, 2, 2),
               received_at=T(2024, 2, 2, 9),
               from_security_id="b", to_security_id="old"),
        ]
        book = CorporateActionPortfolioResearch([HoldingLot("old-lot", "old", 1.0, 10.0)])
        book.register_actions(actions)
        book.decide(AllocationDecision("a-to-b", "old-lot", 1.0))
        book.apply(["a-to-b"])
        book.decide(AllocationDecision("b-to-a", "old-lot:a-to-b", 1.0))
        with self.assertRaisesRegex(ValueError, "循环引用"):
            book.apply(["b-to-a"])

    def test_same_action_cannot_consume_its_own_generated_lot(self):
        action = ca(to_security_id="b")
        book = CorporateActionPortfolioResearch([HoldingLot("old-lot", "old", 1.0, 10.0)])
        book.register_action(action)
        book.decide(AllocationDecision("a1", "old-lot", 1.0, decision_id="first"))
        book.decide(AllocationDecision(
            "a1", "old-lot:a1", 1.0, decision_id="repeat"))
        with self.assertRaisesRegex(ValueError, "循环引用"):
            book.apply(["a1"])

    def test_feasible_adoption_orders_have_consistent_economic_result(self):
        holdings = [
            HoldingLot("old-a", "old", 2.0, 20.0),
            HoldingLot("old-b", "old", 2.0, 20.0),
        ]
        actions = [
            ca(action_id="to-mid", to_security_id="mid"),
            ca(action_id="to-new", family_id="f2",
               effective_date=D(2024, 2, 2),
               received_at=T(2024, 2, 2, 9),
               from_security_id="old", to_security_id="new"),
        ]
        decisions = [
            AllocationDecision("to-mid", "old-a", 2.0),
            AllocationDecision("to-new", "old-b", 2.0),
        ]
        comparison = compare_adoption_order_consistency(
            holdings, actions, decisions,
            [["to-mid", "to-new"], ["to-new", "to-mid"]])
        self.assertTrue(comparison["consistent"])


if __name__ == "__main__":
    unittest.main()
