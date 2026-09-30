"""来源选择规则的历史有效范围与“规则调整前后研究对照”示例（虚构数据）。

场景：2024-03-04 收盘后，A、B 两个来源给出冲突价格，旧偏好取 A。
3-10 研究治理把 B 提权（当天并无任何新行情到达），要求：
1) 重复查询 3-04 的同一研究时点，保持原历史视角，价格仍是 10.0；
2) 用新规则对同一时点“重新研究”，得到 10.5 并可逐字段对照；
3) 之后把配置回退成旧内容，仍是一次带新身份的修订（restores 留痕）；
4) 保存重开、重复查询后，原研究版本保持不变。
"""

import datetime as dt
import tempfile
from pathlib import Path

from histquote import Dataset, Quote, QuoteEvent, Security, SourceRule

D, T = dt.date, dt.datetime
BD = D(2024, 3, 4)


def build() -> Dataset:
    ds = Dataset(":memory:", name="rule-history-demo")
    ds.add_security(Security("s3", "600003", "规则样本"))
    ds.calendar.add_sessions([D(2024, 3, d) for d in (4, 5, 6, 7, 8)])
    ds.set_source_priority({"A": 1, "B": 2})       # 旧偏好（基线配置）
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 30), "raw",
        Quote(10, 10.1, 9.9, 10.0, 100), source="A", source_record="a1"))
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 31), "raw",
        Quote(10, 10.6, 10.1, 10.5, 200), source="B", source_record="b1"))
    return ds


def main() -> None:
    ds = build()
    old_viewpoint = T(2024, 3, 4, 16)

    print("== 1) 旧偏好下的原历史视角（3-04 收盘后）==")
    v = ds.quote_at("s3", BD, old_viewpoint)
    print(f"   close={v.quote.close} 采用={v.source_record} 规则={v.rule_id or '<基线>'}")

    print("== 2) 3-10 才生效的新规则（当时没有任何新行情到达）==")
    ds.add_source_rule(SourceRule(
        "rule-2024-03-10-b-first", T(2024, 3, 10, 9),
        {"B": 1, "A": 2}, reason="B 源质量复核后提权"))
    again = ds.quote_at("s3", BD, old_viewpoint)
    print(f"   重复查询原时点 close={again.quote.close}（今天的偏好不能改写过去）")
    print(f"   3-10 之后的视角 close="
          f"{ds.quote_at('s3', BD, T(2024, 3, 10, 10)).quote.close}")

    print("== 3) 规则调整前后的研究对照（同一研究时点）==")
    cmp_ = ds.research_comparison(
        "s3", BD, old_viewpoint, new_rule_id="rule-2024-03-10-b-first")
    h, r = cmp_["historical"]["view"], cmp_["revised"]["view"]
    print(f"   保持原视角: close={h.quote.close} 采用={h.source_record}"
          f" 规则={h.rule_id or '<基线>'}")
    print(f"   按新规重研: close={r.quote.close} 采用={r.source_record}"
          f" 规则={r.rule_id}")
    print(f"   变化字段  : {list(cmp_['changed_fields'])}")

    print("== 4) 回退旧配置：内容相同，修订身份是新的 ==")
    ds.add_source_rule(SourceRule(
        "rule-2024-03-12-restore", T(2024, 3, 12, 9),
        {"A": 1, "B": 2}, reason="争议复核后回退旧配置",
        restores_rule_id=""))
    for item in ds.source_rule_history():
        back = f"，恢复自 {item['restores_rule_id'] or '基线'}" if item['restores_rule_id'] or item['rule_id'].endswith("restore") else ""
        print(f"   {item['received_at']:%Y-%m-%d} {item['rule_id']} {item['priority']}{back}")
    print(f"   回退后 close={ds.quote_at('s3', BD, T(2024, 3, 12, 10)).quote.close}"
          f" 规则={ds.quote_at('s3', BD, T(2024, 3, 12, 10)).rule_id}")

    print("== 5) 信息可见时间线（价格/撤回/修订/拆分/规则各自何时可见）==")
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 6, 9), "withdrawal", None,
        reason="A 撤回 a1：代码错挂", source="A", source_record="a1-w",
        target_source="A", target_record="a1"))
    from histquote import Split
    ds.add_split(Split("s3", D(2024, 3, 8), 2.0, T(2024, 3, 7, 18)))
    for item in ds.information_timeline("s3", BD):
        label = item.get("source_record") or item.get("rule_id") or item["kind"]
        print(f"   {item['visible_at']:%m-%d %H:%M}  {item['type']:12s} {label}")

    print("== 6) 保存重开与重复查询后，原研究版本保持不变 ==")
    expected = ds.quote_at("s3", BD, old_viewpoint).as_dict()
    info = ds.snapshot_info()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rule_research.db"
        ds.save_as(path)
        ds.close()
        with Dataset.open(path) as reopened:
            assert reopened.snapshot_info() == info
            first = reopened.quote_at("s3", BD, old_viewpoint).as_dict()
            second = reopened.quote_at("s3", BD, old_viewpoint).as_dict()
            assert first == second == expected
            print(f"   指纹一致 {info['counts']}，原版本 close={first['quote']['close']}")


if __name__ == "__main__":
    main()
