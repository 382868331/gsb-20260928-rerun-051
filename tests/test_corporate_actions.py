"""公司行动版本、持仓身份延续与成本分配测试。"""

import datetime as dt
import os
import tempfile
import unittest

from histquote import CorporateAction, Dataset, Security

D = dt.date
T = dt.datetime


class CorporateActionTests(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset(":memory:", name="corp")
        for sid, code, name in [
            ("A", "000001", "原公司"),
            ("B", "000002", "新公司"),
            ("C", "000003", "最终公司"),
        ]:
            self.ds.add_security(Security(sid, code, name))

    def action(self, **kwargs):
        defaults = {
            "action_id": "ca",
            "announcement_id": "a1",
            "kind": "split",
            "from_security_id": "A",
            "effective_date": D(2024, 2, 1),
            "received_at": T(2024, 1, 20),
            "ratio": 2.0,
            "source": "exch",
            "source_record": "r1",
        }
        defaults.update(kwargs)
        return CorporateAction(**defaults)

    def test_split_revision_is_quantity_change_not_code_change(self):
        ds = self.ds
        ds.add_corporate_action(self.action())
        ds.add_corporate_action(self.action(
            announcement_id="a2", source_record="r2", ratio=2.5,
            received_at=T(2024, 1, 25), status="revised", supersedes="a1",
        ))
        holdings = [{"security_id": "A", "quantity": 10, "cost_basis": 100}]

        old = ds.apply_corporate_actions(
            holdings, [{"announcement_id": "a1"}], T(2024, 1, 24)
        )
        revised = ds.apply_corporate_actions(
            holdings, [{"announcement_id": "a2"}], T(2024, 1, 26)
        )

        self.assertEqual(old["positions"], ({
            "security_id": "A", "quantity": 20.0, "cost_basis": 100.0,
            "lot_ids": ["lot-0->ca:a1"],
        },))
        self.assertEqual(revised["adjustments"][0]["gross_result_quantity"], 25.0)
        self.assertEqual(revised["positions"][0]["security_id"], "A")
        self.assertEqual(revised["positions"][0]["cost_basis"], 100.0)
        self.assertEqual(revised["adjustments"][0]["investment_income"], 0.0)
        self.assertEqual(ds.corporate_actions_at(T(2024, 1, 26))[0].ratio, 2.5)

    def test_partial_split_conserves_cost_basis_across_batches(self):
        ds = self.ds
        ds.add_corporate_action(self.action())
        result = ds.apply_corporate_actions(
            [{"security_id": "A", "quantity": 3, "cost_basis": 90}],
            [{"announcement_id": "a1", "quantity": 2}], T(2024, 1, 24)
        )
        position = result["positions"][0]
        self.assertEqual(position["quantity"], 5.0)
        self.assertEqual(position["cost_basis"], 90.0)
        lots = {lot["lot_id"]: lot for lot in result["lots"]}
        self.assertEqual(lots["lot-0"]["quantity"], 1.0)
        self.assertEqual(lots["lot-0"]["cost_basis"], 30.0)
        self.assertEqual(lots["lot-0->ca:a1"]["cost_basis"], 60.0)

    def test_exchange_allocates_cost_and_records_cash_without_price_gain(self):
        ds = self.ds
        ds.add_corporate_action(self.action(
            kind="exchange", to_security_id="B", ratio=0.5,
            cash_per_share=2.0, new_unit_value=18.0,
            announcement_id="m1", source_record="mr1",
        ))
        result = ds.apply_corporate_actions(
            [{"security_id": "A", "quantity": 10, "cost_basis": 110}],
            [{"announcement_id": "m1"}], T(2024, 1, 24)
        )

        adjustment = result["adjustments"][0]
        self.assertEqual(adjustment["new_security_id"], "B")
        self.assertEqual(adjustment["selected_quantity"], 10.0)
        self.assertEqual(adjustment["whole_quantity"], 5)
        self.assertEqual(adjustment["cash_compensation"], 20.0)
        self.assertEqual(adjustment["investment_income"], 0.0)
        self.assertEqual(result["positions"], ({
            "security_id": "B", "quantity": 5.0, "cost_basis": 90.0,
            "lot_ids": ["lot-0->ca:m1"],
        },))
        allocation = adjustment["allocations"][0]
        self.assertEqual(allocation["new_cost_basis"], 90.0)
        self.assertEqual(allocation["cash_cost_basis"], 20.0)

    def test_fractional_share_cash_or_undetermined_disposition(self):
        ds = self.ds
        ds.add_corporate_action(self.action(ratio=2.5))
        holdings = [{"security_id": "A", "quantity": 3, "cost_basis": 100}]

        cash = ds.apply_corporate_actions(
            holdings,
            [{"announcement_id": "a1", "fractional_policy": "cash"}],
            T(2024, 1, 24),
        )
        self.assertEqual(cash["positions"][0]["quantity"], 7.0)
        self.assertEqual(cash["adjustments"][0]["fractional_quantity"], 0.5)
        self.assertIn("fractional-cash-price",
                      cash["adjustments"][0]["undetermined"])

        ds.add_corporate_action(self.action(
            announcement_id="a2", source_record="r2", ratio=2.5,
            fractional_price=12.0, received_at=T(2024, 1, 25),
            status="revised", supersedes="a1",
        ))
        priced = ds.apply_corporate_actions(
            holdings, [{"announcement_id": "a2"}], T(2024, 1, 26)
        )
        self.assertEqual(priced["adjustments"][0]["cash_in_lieu"], 6.0)
        self.assertEqual(priced["positions"][0]["quantity"], 7.0)

    def test_chained_actions_and_partial_adoption_do_not_double_count(self):
        ds = self.ds
        ds.add_corporate_action(self.action())
        ds.add_corporate_action(self.action(
            action_id="merge", announcement_id="m1", kind="exchange",
            from_security_id="A", to_security_id="B", ratio=0.5,
            effective_date=D(2024, 2, 5), received_at=T(2024, 1, 26),
            source_record="mr1",
        ))
        ds.add_corporate_action(self.action(
            action_id="final", announcement_id="f1", kind="exchange",
            from_security_id="B", to_security_id="C", ratio=1.0,
            effective_date=D(2024, 2, 10), received_at=T(2024, 1, 27),
            source_record="fr1",
        ))
        result = ds.apply_corporate_actions(
            [{"security_id": "A", "quantity": 3, "cost_basis": 90}],
            [{"announcement_id": "a1", "quantity": 2},
             {"announcement_id": "m1", "quantity": 4,
              "lot_ids": ["lot-0->ca:a1"]},
             {"announcement_id": "f1"}],
            T(2024, 1, 28),
        )

        positions = {p["security_id"]: p for p in result["positions"]}
        self.assertEqual(positions["A"]["quantity"], 1.0)
        self.assertEqual(positions["C"]["quantity"], 2.0)
        self.assertNotIn("B", positions)
        consumed = [a["selected_quantity"] for a in result["adjustments"]]
        self.assertEqual(consumed, [2.0, 4.0, 2.0])

    def test_cash_compensation_preserves_shares_and_allocates_cost(self):
        ds = self.ds
        ds.add_corporate_action(self.action(
            action_id="cash", announcement_id="pay1",
            kind="cash_compensation", ratio=1.0, cash_per_share=3.0,
            cash_value=3.0, effective_date=D(2024, 2, 2),
            received_at=T(2024, 1, 26), source_record="pay-r1",
        ))
        result = ds.apply_corporate_actions(
            [{"security_id": "A", "quantity": 10, "cost_basis": 100}],
            [{"announcement_id": "pay1", "quantity": 4}],
            T(2024, 1, 27),
        )
        self.assertEqual(result["positions"][0]["quantity"], 10.0)
        adjustment = result["adjustments"][0]
        self.assertEqual(adjustment["cash_compensation"], 12.0)
        self.assertEqual(adjustment["investment_income"], 0.0)
        allocation = adjustment["allocations"][0]
        self.assertEqual(allocation["new_security_id"], "A")
        self.assertEqual(allocation["new_quantity"], 4.0)
        self.assertEqual(allocation["cash_cost_basis"], 40.0)

    def test_undetermined_source_identity_keeps_candidates(self):
        ds = self.ds
        ds.add_corporate_action(CorporateAction(
            action_id="cand", announcement_id="c1", kind="split",
            from_security_id="", candidate_from_security_ids=("A", "B"),
            effective_date=D(2024, 2, 1), received_at=T(2024, 1, 20),
            source="x", source_record="x1",
        ))
        result = ds.apply_corporate_actions(
            [{"security_id": "A", "quantity": 1}],
            [{"announcement_id": "c1"}], T(2024, 1, 24)
        )
        self.assertEqual(result["positions"][0]["security_id"], "A")
        self.assertEqual(result["unresolved_decisions"][0]["candidate_security_ids"],
                         ["A", "B"])

    def test_saved_research_preserves_old_announcement_and_lineage(self):
        ds = self.ds
        ds.add_corporate_action(self.action())
        holdings = [{"security_id": "A", "quantity": 10, "cost_basis": 100}]
        decisions = [{"announcement_id": "a1"}]
        ds.save_portfolio_research(
            "r1", holdings, D(2024, 2, 1), T(2024, 1, 24, 12),
            T(2024, 1, 24, 12),
            corporate_action_decisions=decisions,
        )
        ds.add_corporate_action(self.action(
            announcement_id="a2", source_record="r2", ratio=3.0,
            received_at=T(2024, 1, 25), status="revised", supersedes="a1",
        ))
        ds.add_corporate_action(self.action(
            announcement_id="w1", source_record="w1", ratio=2.0,
            received_at=T(2024, 1, 26), status="withdrawn",
            target_announcement_id="a1",
        ))

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "research.db")
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as reopened:
                check = reopened.reopen_saved_research("r1")

        self.assertTrue(check["same_corporate_actions"])
        actions = check["reopened_corporate_actions"]
        self.assertEqual(actions["positions"][0]["quantity"], 20.0)
        lineage = actions["lots"][1]
        self.assertEqual(lineage["source_lot_id"], "lot-0")
        self.assertEqual(lineage["announcement_ids"], ["a1"])
        self.assertEqual(actions["original_holdings"][0]["quantity"], 10)


if __name__ == "__main__":
    unittest.main()
