"""演示：来源优先级规则的历史有效范围与规则调整前后的研究对照。

虚构数据：A、B 两个来源对同一业务日报出不同价格。3 月 5 日把偏好
从 A 调整为 B，再于 3 月 6 日回退。演示：
1. 规则调整不改写过去研究时点已经能看到的结果；
2. compare_rule_adjustment 对照“保持原历史视角”与“按新规则重新研究”；
3. information_timeline 追溯价格、撤回、修订、拆分公告各自何时可见；
4. 回退是保留身份的新修订；保存重开后各研究版本不变。
"""

import datetime as dt
import os
import tempfile

from histquote import Dataset, PriorityRule, Quote, QuoteEvent, Security, Split

D = dt.date
T = dt.datetime
BD = D(2024, 3, 4)


def main() -> None:
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "priority.db")
    ds = Dataset(path, name="priority-demo")
    ds.add_security(Security("s3", "600003", "优先级样本"))
    ds.calendar.add_sessions([D(2024, 3, d) for d in (4, 5, 6)])

    ds.add_priority_rule(PriorityRule(
        {"A": 1, "B": 2}, T(2024, 3, 4, 9, 0), "初始规则：A 优先", "r1"))
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 30), "raw",
        Quote(10, 10.1, 9.9, 10.0, 100), source="A", source_record="a1"))
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 31), "raw",
        Quote(10, 10.6, 10.1, 10.5, 200), source="B", source_record="b1"))

    ka = T(2024, 3, 4, 16)
    print("== 研究时点 2024-03-04 16:00 ==")
    v = ds.quote_at("s3", BD, ka)
    print(f"  采用 {v.source}/{v.source_record} close={v.quote.close}"
          f" 规则={v.priority_rule_id}")

    # 次日调整偏好：B 优先。当时并没有新收到可见行情。
    ds.set_source_priority_at(
        {"B": 1, "A": 2}, T(2024, 3, 5, 9, 0), "B 源质量更稳", "r2")
    again = ds.quote_at("s3", BD, ka)
    print(f"  规则调整后同一时点复算: close={again.quote.close}"
          f" 规则={again.priority_rule_id}（历史视角不变）")

    print("\n== 规则调整前后的研究对照 ==")
    cmp = ds.compare_rule_adjustment("s3", BD, ka)
    print(f"  保持原历史视角: close={cmp['original'].quote.close}"
          f" 规则={cmp['original_priority_rule_id']}")
    print(f"  按新规则重新研究: close={cmp['revised'].quote.close}"
          f" 规则={cmp['revised_priority_rule_id']}")
    for field, ch in cmp["changed_fields"].items():
        print(f"  变化 {field}: {ch['before']} -> {ch['after']}")

    print("\n== 信息可见时间线 ==")
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 5, 10, 0), "revision",
        Quote(10, 10.5, 10.0, 10.4, 180), source="B",
        source_record="b2", reason="迟到修订"))
    ds.add_split(Split("s3", D(2024, 3, 6), 2.0, T(2024, 3, 5, 11, 0)))
    for item in ds.information_timeline("s3", BD):
        label = item.get("source_record") or item.get("rule_id") or item["type"]
        print(f"  {item['received_at']}  {item['kind']:<13} {label}")

    print("\n== 回退到旧配置（保留修订身份） ==")
    ds.rollback_source_priority("r1", T(2024, 3, 6, 9, 0),
                                "回退到旧配置", "r3")
    for r in ds.priority_rules():
        print(f"  {r.rule_id}: {r.priority} 生效={r.received_at}"
              f" 取代={r.supersedes or '-'} ({r.reason})")

    ds.save_as(os.path.join(tmp, "snapshot.db"))
    ds.close()
    with Dataset.open(os.path.join(tmp, "snapshot.db")) as ds2:
        v2 = ds2.quote_at("s3", BD, ka)
        print(f"\n保存重开后原研究时点: close={v2.quote.close}"
              f" 规则={v2.priority_rule_id}（版本不变）")


if __name__ == "__main__":
    main()
