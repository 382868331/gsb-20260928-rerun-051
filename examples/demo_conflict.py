"""跨来源冲突、来源撤回与裁决的交错资料重放示例（虚构数据，离线）。

同一业务日（2024-02-05）先后发生：两源冲突报价 → A 撤回 → 人工裁决 →
A 补发 → 二次裁决；另有拆分公告在另一视角送达。多个历史视角逐一复算，
并说明每一步结果差异来自哪次新增信息。
"""

import datetime as dt

from histquote import Adjudication, Dataset, Quote, QuoteEvent, Security

D, T = dt.date, dt.datetime
BD = D(2024, 2, 5)


def main() -> None:
    ds = Dataset(":memory:", name="conflict-demo")
    ds.add_security(Security("s2", "600002", "冲突样本"))
    ds.calendar.add_sessions([D(2024, 2, d) for d in range(5, 9)])
    ds.set_source_priority({"A": 1, "B": 2})

    ds.add_event(QuoteEvent(
        "s2", BD, T(2024, 2, 5, 15, 30), "raw",
        Quote(10, 10.1, 9.9, 10.0, 100), source="A", source_record="a1"))
    ds.add_event(QuoteEvent(
        "s2", BD, T(2024, 2, 5, 15, 31), "raw",
        Quote(10, 10.6, 10.1, 10.5, 200), source="B", source_record="b1"))
    ds.add_event(QuoteEvent(
        "s2", BD, T(2024, 2, 6, 9, 0), "withdrawal", None,
        reason="A 源撤回 a1：代码错挂", source="A",
        source_record="a1-w", target_source="A", target_record="a1"))
    ds.add_adjudication(Adjudication(
        "s2", BD, "B", "b1", T(2024, 2, 6, 10, 0),
        reason="交易所核对后以 B 为准", decision_id="d1"))
    ds.add_event(QuoteEvent(
        "s2", BD, T(2024, 2, 6, 11, 0), "raw",
        Quote(10, 10.3, 10.0, 10.2, 150), source="A", source_record="a2"))
    ds.add_adjudication(Adjudication(
        "s2", BD, "A", "a2", T(2024, 2, 6, 14, 0),
        reason="A 补发并经二次核对", decision_id="d2"))
    from histquote import Split
    ds.add_split(Split("s2", D(2024, 2, 8), 2.0, T(2024, 2, 7, 9)))

    viewpoints = [
        T(2024, 2, 5, 15, 30, 30),  # 只看到 a1
        T(2024, 2, 5, 16),          # 两源冲突，默认优先级取 A
        T(2024, 2, 6, 9, 30),       # A 撤回，仅剩 B
        T(2024, 2, 6, 10, 30),      # 裁决 d1 采用 B
        T(2024, 2, 6, 12),          # A 补发 a2，裁决 d1 仍有效
        T(2024, 2, 6, 15),          # 二次裁决 d2 改采 A/a2
        T(2024, 2, 7, 8),           # 拆分公告送达前
        T(2024, 2, 7, 10),          # 1 拆 2 公告送达，仅复权值变化
    ]
    previous = None
    for ka in viewpoints:
        v = ds.quote_at("s2", BD, ka)
        close = v.quote.close if v.quote else "-"
        print(f"{ka:%m-%d %H:%M}  状态={v.status.value:9s} 收盘={close!s:5s}"
              f" 复权={v.adjusted_close!s:5s} 采用={v.source_record or '-':4s}"
              f" 依据={v.selection_basis:16s} 裁决={v.adjudication_id or '-'}")
        if previous is not None:
            diff = ds.explain_viewpoint_diff("s2", BD, previous, ka)
            causes = [
                f"{i['type']}:{i.get('source_record') or i.get('decision_id') or i['kind']}"
                for i in diff["new_information"]
            ]
            fields = list(diff["changed_fields"])
            print(f"             ← 变化字段={fields or '无'} 新增信息={causes or '无'}")
        previous = ka

    # 来源原记录永不抹除：最终视角下三条报价候选都还在，a1 标记为已撤回
    final = ds.quote_at("s2", BD, T(2024, 2, 7, 10))
    print("\n候选留痕：")
    for c in final.candidates:
        flag = f"由 {c.withdrawn_by} 撤回" if not c.active else "有效"
        print(f"  {c.source}/{c.source_record} kind={c.kind} close={c.close} [{flag}]")


if __name__ == "__main__":
    main()
