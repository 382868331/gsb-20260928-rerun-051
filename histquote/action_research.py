"""公司行动组合的份额级采用约束与可撤回研究账本。"""

from __future__ import annotations

import dataclasses

from .identity import CorporateAction, HoldingLot


def _money(value: float) -> float:
    return round(value + 0.0, 6)


def _qty(value: float) -> float:
    return round(value + 0.0, 12)


@dataclasses.dataclass(frozen=True)
class AllocationDecision:
    action_id: str
    lot_id: str
    quantity: float
    disposition: str = "exchange"
    decision_id: str = ""
    status: str = "confirmed"

    def __post_init__(self) -> None:
        if self.disposition not in {"exchange", "retain", "wait"}:
            raise ValueError(f"未知公司行动去向: {self.disposition}")
        if self.status not in {"confirmed", "pending", "withdrawn"}:
            raise ValueError(f"未知采用状态: {self.status}")
        if self.quantity < 0:
            raise ValueError("采用数量不能为负数")


class CorporateActionPortfolioResearch:
    """逐笔跟踪原始份额、换股所得、保留份额和待核实份额。"""

    def __init__(self, holdings):
        self.holdings = {}
        self.actions = {}
        self.decisions = {}
        self.adjustments = []
        self.cash_total = 0.0
        for holding in holdings:
            lot = dataclasses.asdict(holding)
            lot.update({
                "available_quantity": lot["quantity"],
                "available_cost": lot["cost"],
                "origin_lot_id": lot["lot_id"],
                "lineage": (lot["lot_id"],),
                "lineage_security_ids": (lot["security_id"],),
                "acquisition": "existing",
                "created_by_action_id": "",
                "reserved": [],
            })
            self.holdings[lot["lot_id"]] = lot

    def register_actions(self, actions) -> None:
        for action in actions:
            self.register_action(action)

    def register_action(self, action: CorporateAction) -> None:
        existing = self.actions.get(action.action_id)
        if existing is not None and existing != action:
            raise ValueError(f"公司行动 {action.action_id} 内容冲突，禁止覆盖")
        self.actions[action.action_id] = action

    def decide(self, decision: AllocationDecision) -> AllocationDecision:
        if decision.action_id not in self.actions:
            raise ValueError(f"未知公司行动: {decision.action_id}")
        action = self.actions[decision.action_id]
        lot = self.holdings.get(decision.lot_id)
        if lot is None and not self._is_expected_generated_lot(
                decision.lot_id, action) and not self._is_self_generated_lot(
                decision.lot_id, action):
            raise ValueError(f"未知持仓批次: {decision.lot_id}")
        if lot is not None and lot["security_id"] != action.from_security_id:
            raise ValueError(f"批次 {decision.lot_id} 不是行动 {decision.action_id} 的原证券")
        key = decision.decision_id or f"{decision.action_id}:{decision.lot_id}"
        decision = dataclasses.replace(decision, decision_id=key)
        committed = sum(
            d.quantity for d in self.decisions.values()
            if d.status != "withdrawn"
            and d.lot_id == decision.lot_id
        )
        capacity = (
            lot["available_quantity"] if lot is not None
            else self._expected_generated_capacity(decision.lot_id, action)
        )
        if decision.quantity + committed > capacity + 1e-10:
            raise ValueError(
                f"批次 {decision.lot_id} 的互斥去向合计超额："
                f"请求合计 {decision.quantity + committed}，可用 {capacity}")
        self.decisions[key] = decision
        return decision

    def _is_expected_generated_lot(self, lot_id: str, action: CorporateAction) -> bool:
        for producer in self.actions.values():
            output_id = producer.from_security_id if producer.action_type == "split" else producer.to_security_id
            suffix = f":{producer.action_id}"
            if not lot_id.endswith(suffix) or output_id != action.from_security_id:
                continue
            parent_id = lot_id[:-len(suffix)]
            parent = self.holdings.get(parent_id)
            if parent is not None:
                return parent["security_id"] == producer.from_security_id
            if self._is_expected_generated_lot(parent_id, producer):
                return True
        return False

    @staticmethod
    def _is_self_generated_lot(lot_id: str, action: CorporateAction) -> bool:
        return lot_id.endswith(f":{action.action_id}")

    def _expected_generated_capacity(
            self, lot_id: str, action: CorporateAction) -> float:
        for producer in self.actions.values():
            output_id = producer.from_security_id if producer.action_type == "split" else producer.to_security_id
            suffix = f":{producer.action_id}"
            if not lot_id.endswith(suffix) or output_id != action.from_security_id:
                continue
            parent_id = lot_id[:-len(suffix)]
            parent = self.holdings.get(parent_id)
            if parent is not None and parent["security_id"] == producer.from_security_id:
                return parent["available_quantity"] * producer.to_ratio / producer.from_ratio
        return float("inf")

    def withdraw(self, decision_id: str) -> AllocationDecision:
        if decision_id not in self.decisions:
            raise KeyError(f"未找到采用选择: {decision_id}")
        old = self.decisions[decision_id]
        if old.status == "confirmed":
            raise ValueError("已确认并完成的公司行动选择不能撤回；现金补偿保留为既有事实")
        for lot in self.holdings.values():
            kept = []
            for reservation in lot["reserved"]:
                if reservation["decision_id"] == decision_id:
                    lot["available_quantity"] = _qty(
                        lot["available_quantity"] + reservation["quantity"])
                    lot["available_cost"] = _money(
                        lot["available_cost"] + reservation["cost"])
                else:
                    kept.append(reservation)
            lot["reserved"] = kept
        withdrawn = dataclasses.replace(old, status="withdrawn")
        self.decisions[decision_id] = withdrawn
        return withdrawn

    def apply(self, action_order=None) -> dict:
        ordered = self._ordered_action_ids(action_order)
        active_decisions = [
            d for d in self.decisions.values()
            if d.status != "withdrawn" and d.disposition != "retain"
        ]
        for action_id in ordered:
            action = self.actions[action_id]
            for decision in [d for d in active_decisions if d.action_id == action_id]:
                lot = self.holdings[decision.lot_id]
                if decision.quantity > lot["available_quantity"] + 1e-10:
                    raise ValueError(
                        f"行动 {action_id} 超额采用：请求 {decision.quantity}，"
                        f"可用 {lot['available_quantity']}")
                if action_id in lot["lineage"]:
                    raise ValueError(f"检测到循环引用: {action_id} 已存在于批次血缘中")
                if lot["security_id"] != action.from_security_id:
                    raise ValueError(f"批次 {decision.lot_id} 不能用于 {action_id}")
                output_id = action.from_security_id if action.action_type == "split" else action.to_security_id
                if output_id in lot.get("lineage_security_ids", ()):
                    raise ValueError("检测到循环引用：新证券会回到该批次已持有的证券")
                if decision.disposition == "wait" or decision.status == "pending":
                    self._reserve(lot, decision, action)
                else:
                    self._exchange(lot, decision, action)
        return self.result(tuple(ordered))

    def _ordered_action_ids(self, action_order):
        if action_order is None:
            return tuple(sorted(
                self.actions,
                key=lambda aid: (
                    self.actions[aid].effective_date,
                    self.actions[aid].received_at,
                    self.actions[aid].sequence,
                    aid,
                ),
            ))
        ordered = tuple(action_order)
        if not set(ordered).issubset(self.actions):
            raise ValueError("行动顺序包含未登记行动")
        return ordered

    def _reserve(self, lot, decision, action):
        cost = lot["available_cost"] * decision.quantity / lot["available_quantity"]
        lot["available_quantity"] = _qty(lot["available_quantity"] - decision.quantity)
        lot["available_cost"] = _money(lot["available_cost"] - cost)
        lot["reserved"].append({
            "decision_id": decision.decision_id,
            "action_id": action.action_id,
            "family_id": action.family_id,
            "quantity": _qty(decision.quantity),
            "cost": _money(cost),
            "status": decision.status,
            "disposition": decision.disposition,
        })

    def _exchange(self, lot, decision, action):
        if action.confirmation_status == "candidate" and not action.to_security_id:
            self._reserve(lot, dataclasses.replace(decision, disposition="wait"), action)
            return
        qty = min(decision.quantity, lot["available_quantity"])
        consumed_cost = lot["available_cost"] * qty / lot["available_quantity"]
        gross = qty * action.to_ratio / action.from_ratio
        whole = int(gross)
        fraction = _qty(gross - whole)
        cash_comp = action.cash_per_from_share * qty
        cash_cost = consumed_cost * action.cash_cost_portion
        security_cost = consumed_cost - cash_cost
        fraction_cash = 0.0
        fraction_status = "none" if fraction == 0 else action.fractional_destination
        output_qty = float(whole)
        if fraction > 0 and action.fractional_destination == "cash":
            if action.fractional_cash_rate is None:
                fraction_status = "undetermined"
            else:
                fraction_cash = fraction * action.fractional_cash_rate
                fraction_cost = security_cost * fraction / gross
                cash_cost += fraction_cost
                security_cost -= fraction_cost
        elif fraction > 0 and action.fractional_destination == "retain":
            output_qty = gross
        lot["available_quantity"] = _qty(lot["available_quantity"] - qty)
        lot["available_cost"] = _money(lot["available_cost"] - consumed_cost)
        output_id = action.from_security_id if action.action_type == "split" else action.to_security_id
        output_lot = None
        if output_qty > 0 and output_id:
            output_lot = {
                "lot_id": f"{lot['lot_id']}:{action.action_id}",
                "security_id": output_id,
                "quantity": _qty(output_qty),
                "cost": _money(security_cost),
                "trade_date": action.effective_date.isoformat(),
                "display_code": "",
                "market": "",
                "source": action.source,
                "source_record": action.source_record,
                "available_quantity": _qty(output_qty),
                "available_cost": _money(security_cost),
                "origin_lot_id": lot["origin_lot_id"],
                "lineage": lot["lineage"] + (action.action_id, f"{lot['lot_id']}:{action.action_id}"),
                "lineage_security_ids": lot.get("lineage_security_ids", (lot["security_id"])) + (output_id,),
                "acquisition": f"corporate-action:{action.action_id}",
                "created_by_action_id": action.action_id,
                "reserved": [],
            }
            if output_lot["lot_id"] in self.holdings:
                raise ValueError(f"循环引用会重复生成批次: {output_lot['lot_id']}")
            self.holdings[output_lot["lot_id"]] = output_lot
        cash = _money(cash_comp + fraction_cash)
        self.cash_total = _money(self.cash_total + cash)
        self.adjustments.append({
            "action_id": action.action_id,
            "family_id": action.family_id,
            "source_lot_id": lot["lot_id"],
            "source_acquisition": lot["acquisition"],
            "input_quantity": _qty(qty),
            "output_security_id": output_id,
            "output_lot_id": output_lot["lot_id"] if output_lot else "",
            "whole_quantity": _qty(output_qty),
            "fractional_quantity": fraction,
            "fractional_destination": fraction_status,
            "cash_amount": cash,
            "cash_allocated_cost": _money(cash_cost),
            "consumed_cost": _money(consumed_cost),
            "new_security_cost": _money(security_cost),
            "lineage": output_lot["lineage"] if output_lot else lot["lineage"],
        })

    def positions(self) -> tuple:
        grouped = {}
        for lot in self.holdings.values():
            available = lot["available_quantity"]
            reserved_qty = sum(r["quantity"] for r in lot.get("reserved", []))
            qty = _qty(available + reserved_qty)
            reserved_cost = sum(r["cost"] for r in lot.get("reserved", []))
            cost = _money(lot["available_cost"] + reserved_cost)
            if qty <= 0:
                continue
            item = grouped.setdefault(lot["security_id"], {
                "security_id": lot["security_id"], "quantity": 0.0,
                "cost": 0.0, "lot_ids": [], "available_quantity": 0.0,
                "reserved_quantity": 0.0,
            })
            item["quantity"] += qty
            item["cost"] += cost
            item["available_quantity"] += available
            item["reserved_quantity"] += reserved_qty
            item["lot_ids"].append(lot["lot_id"])
        return tuple(dict(item, quantity=_qty(item["quantity"]),
                          cost=_money(item["cost"]),
                          available_quantity=_qty(item["available_quantity"]),
                          reserved_quantity=_qty(item["reserved_quantity"]))
                     for item in sorted(grouped.values(), key=lambda x: x["security_id"]))

    def result(self, action_order=()) -> dict:
        return {
            "action_order": tuple(action_order),
            "positions": self.positions(),
            "lots": tuple(dict(lot) for lot in self.holdings.values()),
            "adjustments": tuple(self.adjustments),
            "decisions": tuple(dataclasses.asdict(d) for d in self.decisions.values()),
            "cash_total": self.cash_total,
        }


def compare_adoption_order_consistency(holdings, actions, decisions, orders) -> dict:
    """用相同份额选择按不同行动顺序复算，并比较经济结果是否一致。"""
    results = {}
    signatures = {}
    for order in orders:
        book = CorporateActionPortfolioResearch(holdings)
        book.register_actions(actions)
        for decision in decisions:
            book.decide(decision)
        result = book.apply(order)
        key = tuple(order)
        results[key] = result
        signatures[key] = _result_signature(result)
    return {
        "consistent": len(set(signatures.values())) == 1,
        "signatures": signatures,
        "results": results,
    }


def _result_signature(result: dict) -> tuple:
    return (
        tuple(sorted((p["security_id"], p["quantity"], p["cost"],
                      p["available_quantity"], p["reserved_quantity"])
                     for p in result["positions"])),
        tuple(sorted((a["action_id"], a["source_lot_id"], a["input_quantity"],
                      a["output_security_id"], a["whole_quantity"],
                      a["fractional_quantity"], a["cash_amount"],
                      a["consumed_cost"], a["new_security_cost"])
                     for a in result["adjustments"])),
        result["cash_total"],
    )
