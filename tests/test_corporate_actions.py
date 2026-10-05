import datetime as dt
import os
import tempfile
import unittest

from histquote import CorporateAction, Dataset, HoldingLot, Split

D = dt.date
T = dt.datetime


def action(**kwargs):
    defaults = {
        "action_id": "a1", "family_id": "f1", "action_type": "split",
        "effective_date": D(2024, 2, 1), "received_at": T(2024, 2, 1, 9),
        "from_security_id": "old", "to_security_id": "old",
        "from_ratio": 1, "to_ratio": 2, "announcement_kind": "revision",
    }
    defaults.update(kwargs)
    return CorporateAction(**defaults)


class CorporateActionTests(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset(":memory:", name="corp")
        self.holdings = [HoldingLot("lot-a", "old", 3.0, 100.0)]

    def test_split_preserves_identity_original_lot_and_cost(self):
        self.ds.add_corporate_action(action())
        result = self.ds.apply_corporate_actions(
            self.holdings, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(result["positions"], (
            {"security_id": "old", "quantity": 6.0, "cost": 100.0,
             "lot_ids": ["lot-a:a1"], "available_quantity": 6.0,
             "reserved_quantity": 0.0},
        ))
        adjustment = result["adjustments"][0]
        self.assertEqual(adjustment["source_security_id"], "old")
        self.assertEqual(adjustment["output_security_id"], "old")
        self.assertEqual(adjustment["input_quantity"], 3.0)
        self.assertEqual(adjustment["whole_quantity"], 6.0)
        self.assertEqual(result["original_lots"][0]["quantity"], 3.0)
        self.assertEqual(result["investment_gain_total"], 0.0)

    def test_legacy_split_feeds_position_ledger(self):
        self.ds.add_split(Split("old", D(2024, 2, 1), 2.0, T(2024, 2, 1, 9)))
        result = self.ds.apply_corporate_actions(
            self.holdings, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(result["positions"][0]["quantity"], 6.0)
        self.assertEqual(result["adjustments"][0]["output_security_id"], "old")

    def test_exchange_cash_fraction_and_undetermined_identity(self):
        self.ds.add_corporate_action(action(
            action_type="exchange", to_security_id="new", from_ratio=2,
            to_ratio=1, cash_per_from_share=1, cash_cost_portion=0.25))
        result = self.ds.apply_corporate_actions(
            self.holdings, D(2024, 2, 1), T(2024, 2, 1, 10))
        adj = result["adjustments"][0]
        self.assertEqual(adj["input_quantity"], 3.0)
        self.assertEqual(adj["gross_new_quantity"], 1.5)
        self.assertEqual(adj["whole_quantity"], 1.0)
        self.assertEqual(adj["fractional_quantity"], 0.5)
        self.assertEqual(adj["fractional_destination"], "undetermined")
        self.assertEqual(adj["output_security_id"], "new")
        self.assertEqual(result["cash_total"], 3.0)
        self.assertEqual(result["cash_compensations"][0]["allocated_cost"], 25.0)
        self.assertEqual(result["undetermined"][0]["quantity"], 0.5)
        self.assertIsNone(adj["investment_gain"])
        self.assertFalse(adj["code_change_is_quantity_change"])

    def test_preview_revision_withdrawal_and_saved_as_of_replay(self):
        ds = self.ds
        ds.add_corporate_action(action(
            action_id="preview", announcement_kind="preview", to_ratio=2))
        saved = ds.apply_corporate_actions(
            self.holdings, D(2024, 2, 1), T(2024, 2, 1, 10),
            selected_action_ids={"f1": "preview"}, include_previews=True)
        self.assertEqual(saved["positions"][0]["quantity"], 6.0)

        ds.add_corporate_action(action(
            action_id="revised", announcement_kind="revision", to_ratio=3,
            received_at=T(2024, 2, 2, 9)))
        comparison = ds.compare_corporate_action_scenarios(
            self.holdings, D(2024, 2, 1), T(2024, 2, 2, 10),
            "f1", "preview", "revised")
        self.assertEqual(comparison["original"]["positions"][0]["quantity"], 6.0)
        self.assertEqual(comparison["revised"]["positions"][0]["quantity"], 9.0)

        ds.add_corporate_action(action(
            action_id="cancel-revised", family_id="f1", action_type="split",
            effective_date=D(2024, 2, 1), received_at=T(2024, 2, 3, 9),
            announcement_kind="withdrawal", target_action_id="revised"))
        latest = ds.corporate_action_scenario("f1", T(2024, 2, 3, 10))
        self.assertEqual(latest["status"], "withdrawn")
        self.assertEqual(latest["withdrawn_by"], "cancel-revised")
        old_still = ds.apply_corporate_actions(
            self.holdings, D(2024, 2, 1), T(2024, 2, 3, 10),
            selected_action_ids={"f1": "preview"})
        self.assertEqual(old_still["positions"][0]["quantity"], 6.0)

    def test_identity_candidate_remains_undetermined(self):
        self.ds.add_corporate_action(action(
            action_type="exchange", to_security_id="",
            candidate_security_ids=("maybe-new", "other-new"),
            confirmation_status="candidate"))
        result = self.ds.apply_corporate_actions(
            self.holdings, D(2024, 2, 1), T(2024, 2, 1, 10))
        self.assertEqual(result["positions"], (
            {"security_id": "old", "quantity": 3.0, "cost": 100.0,
             "lot_ids": ["lot-a"], "available_quantity": 0.0,
             "reserved_quantity": 3.0},
        ))
        pending = result["undetermined"][0]
        self.assertEqual(pending["quantity"], 3.0)
        self.assertEqual(pending["candidate_security_ids"],
                         ("maybe-new", "other-new"))

    def test_waiting_identity_reserves_shares_from_later_action_and_rejects_overuse(self):
        self.ds.add_corporate_action(action(
            action_id="waiting", action_type="exchange", to_security_id="",
            confirmation_status="candidate",
            candidate_security_ids=("maybe-new",)))
        self.ds.add_corporate_action(action(
            action_id="later", family_id="f2", action_type="exchange",
            to_security_id="other-new",
            effective_date=D(2024, 3, 1), received_at=T(2024, 3, 1, 9),
            announcement_kind="revision"))
        with self.assertRaisesRegex(ValueError, "超额采用"):
            self.ds.apply_corporate_actions(
                self.holdings, D(2024, 3, 1), T(2024, 3, 1, 10),
                selected_quantities={
                    "waiting": {"lot-a": 2.0},
                    "later": {"lot-a": 2.0},
                })
        result = self.ds.apply_corporate_actions(
            self.holdings, D(2024, 3, 1), T(2024, 3, 1, 10),
            selected_quantities={
                "waiting": {"lot-a": 2.0},
                "later": {"lot-a": 1.0},
            })
        positions = {p["security_id"]: p for p in result["positions"]}
        self.assertEqual(positions["old"]["quantity"], 2.0)
        self.assertEqual(positions["other-new"]["quantity"], 2.0)
        self.assertEqual(
            result["adjustments"][0]["input_quantity"], 1.0)
        self.assertEqual(result["undetermined"][0]["quantity"], 2.0)
        costs = positions["old"]["cost"] + positions["other-new"]["cost"]
        self.assertEqual(costs, 100.0)
        self.assertAlmostEqual(result["undetermined"][0]["cost"],
                               positions["old"]["cost"])

    def test_chained_partial_adoption_does_not_double_count(self):
        self.holdings.append(HoldingLot("lot-b", "old", 1.0, 40.0))
        self.ds.add_corporate_action(action(
            action_id="split", to_ratio=2, effective_date=D(2024, 2, 1)))
        self.ds.add_corporate_action(action(
            action_id="merge", family_id="f2", action_type="exchange",
            to_security_id="new", from_ratio=1, to_ratio=1,
            effective_date=D(2024, 3, 1), received_at=T(2024, 3, 1, 9),
            announcement_kind="revision"))
        result = self.ds.apply_corporate_actions(
            self.holdings, D(2024, 3, 1), T(2024, 3, 1, 10),
            selected_quantities={"split": {"lot-a": 2.0}})
        old_qty = sum(p["quantity"] for p in result["positions"]
                      if p["security_id"] == "old")
        new_qty = sum(p["quantity"] for p in result["positions"]
                      if p["security_id"] == "new")
        self.assertEqual(old_qty, 1.0)
        self.assertEqual(new_qty, 5.0)
        self.assertEqual(old_qty + new_qty, 6.0)

    def test_save_reopen_preserves_holdings_quotes_adjustments_and_sources(self):
        ds = self.ds
        ds.add_corporate_action(action())
        ds.save_corporate_action_research(
            "r1", self.holdings, D(2024, 2, 1), T(2024, 2, 1, 10),
            T(2024, 2, 1, 11))
        ds.add_corporate_action(action(
            action_id="revised", announcement_kind="revision", to_ratio=3,
            received_at=T(2024, 2, 2, 9)))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "research.db")
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as reopened:
                check = reopened.reopen_corporate_action_research("r1")
                self.assertTrue(check["same_positions"])
                self.assertTrue(check["same_adjustments"])
                self.assertTrue(check["same_cash_total"])
                self.assertEqual(
                    check["reopened"]["positions"][0]["quantity"], 6.0)
                self.assertEqual(
                    check["adoptions"][0]["action_id"], "a1")

    def test_late_revision_keeps_saved_partial_cash_and_cost_conservation(self):
        ds = self.ds
        ds.add_corporate_action(action(
            action_id="exchange-1", action_type="exchange",
            to_security_id="new", from_ratio=2, to_ratio=1,
            cash_per_from_share=1.0, cash_cost_portion=0.25,
            fractional_destination="cash", fractional_cash_rate=7.0))
        ds.save_corporate_action_research(
            "cash-research", self.holdings, D(2024, 2, 1),
            T(2024, 2, 1, 10), T(2024, 2, 1, 11))
        ds.add_corporate_action(action(
            action_id="exchange-revised", action_type="exchange",
            to_security_id="new", from_ratio=1, to_ratio=3,
            cash_per_from_share=1.0, cash_cost_portion=0.25,
            fractional_destination="cash", fractional_cash_rate=7.0,
            received_at=T(2024, 2, 2, 9),
            announcement_kind="revision"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "research.db")
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as reopened:
                check = reopened.reopen_corporate_action_research("cash-research")
                result = check["reopened"]
                self.assertTrue(check["same_positions"])
                self.assertTrue(check["same_adjustments"])
                self.assertTrue(check["same_cash_total"])
                self.assertEqual(result["positions"], (
                    {"security_id": "new", "quantity": 1.0, "cost": 50.0,
                     "lot_ids": ["lot-a:exchange-1"],
                     "available_quantity": 1.0,
                     "reserved_quantity": 0.0},
                ))
                self.assertEqual(result["cash_total"], 6.5)
                self.assertAlmostEqual(
                    result["positions"][0]["cost"]
                    + result["cash_compensations"][0]["allocated_cost"],
                    100.0)


if __name__ == "__main__":
    unittest.main()
