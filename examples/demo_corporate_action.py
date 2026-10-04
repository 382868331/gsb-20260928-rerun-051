"""公司行动演示：拆股预告→修订→撤回、换股合并、现金补偿与持仓采用。"""

import datetime as dt

from histquote import CorporateAction, Dataset, Security

D = dt.date
T = dt.datetime


def main() -> None:
    ds = Dataset(":memory:", name="corp-action-demo")
    ds.add_security(Security("old", "000001", "旧公司"))
    ds.add_security(Security("new", "000002", "新公司"))
    ds.calendar.add_sessions([D(2024, 3, 1), D(2024, 3, 4)])

    # 1) 拆股预告 1 拆 2，之后修订为 1 拆 3
    ds.add_corporate_action(CorporateAction(
        "old", D(2024, 3, 4), T(2024, 3, 1, 9), "split",
        action_id="split-v1", ratio=2.0, reason="拆股预告"))
    ds.add_corporate_action(CorporateAction(
        "old", D(2024, 3, 4), T(2024, 3, 2, 9), "split",
        action_id="split-v2", ratio=3.0, supersedes="split-v1",
        reason="修订比例为 1 拆 3"))

    holding = {"quantity": 100.0, "cost": 900.0, "holding_id": "h1"}
    cmp = ds.compare_action_plans("split-v1", "split-v2", holding)
    print("== 拆股：继续采用旧公告 vs 改用修订公告 ==")
    for field, change in cmp["changed_fields"].items():
        print(f"  {field}: {change['before']} -> {change['after']}")

    # 2) 换股合并 + 现金补偿，新证券身份先未确认（保留候选）
    ds.add_corporate_action(CorporateAction(
        "old", D(2024, 3, 4), T(2024, 3, 1, 10), "merger",
        action_id="merger-v1", ratio=0.4,
        new_security_candidates=("new", "other"),
        cash_per_share=0.5, fraction_policy="cash",
        fraction_cash_price=12.5, reason="换股合并预告"))
    plan = ds.plan_position_adjustment(
        "merger-v1", {"quantity": 103.0, "cost": 2060.0})
    print("\n== 换股合并（身份未确认）==")
    print(f"  新股数量: {plan['new_quantity']}  零碎份额: {plan['fractional_shares']}"
          f"  零碎折现: {plan['fraction_cash']}  现金补偿: {plan['cash_compensation']}")
    print(f"  候选新证券: {plan['new_security_candidates']}"
          f"  未确定: {plan['undetermined']}")
    print(f"  收益解释: {plan['return_interpretation']}"
          "（换股价格跳变不是投资收益）")

    # 3) 用户采用：部分采用不能把同一份额计入两个证券
    adopted = ds.adopt_corporate_action(
        "merger-v1", "h1", holding_quantity=103.0, quantity=60.0,
        cost=1200.0, adopted_at=T(2024, 3, 4, 10), reason="先采用一部分")
    print(f"\n== 采用 60 股 ==  剩余未采用: {adopted['remaining_quantity']}")
    try:
        ds.adopt_corporate_action(
            "merger-v1", "h1", holding_quantity=103.0, quantity=50.0,
            cost=1000.0, adopted_at=T(2024, 3, 4, 11))
    except ValueError as exc:
        print(f"  超额采用被拒绝: {exc}")

    # 4) 沿证券身份与公司行动追踪每一笔调整
    print("\n== 持仓调整追踪 ==")
    for item in ds.holding_adjustments(holding_id="h1"):
        print(f"  {item['adjustment_id'][:18]}  行动={item['action_id']}"
              f"  {item['security_id']}->{item['new_security_id'] or '(候选)'}"
              f"  数量={item['quantity']}")


if __name__ == "__main__":
    main()
