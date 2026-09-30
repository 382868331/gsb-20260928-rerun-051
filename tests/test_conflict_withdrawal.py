"""跨来源冲突裁决、来源撤回与多历史视角差异归因测试。"""

import datetime as dt
import unittest

from histquote import (
    Adjudication, Dataset, Quote, QuoteEvent, QuoteStatus, Security, Split,
)

D = dt.date
T = dt.datetime
BD = D(2024, 2, 5)


def build():
    ds = Dataset(":memory:", name="conflict")
    ds.add_security(Security("s2", "600002", "冲突样本"))
    ds.calendar.add_sessions([D(2024, 2, d) for d in (5, 6, 7, 8)])
    ds.set_source_priority({"A": 1, "B": 2})
    # 同一业务日，两个来源给出不同价格
    ds.add_event(QuoteEvent(
        "s2", BD, T(2024, 2, 5, 15, 30), "raw",
        Quote(10, 10.1, 9.9, 10.0, 100), source="A", source_record="a1"))
    ds.add_event(QuoteEvent(
        "s2", BD, T(2024, 2, 5, 15, 31), "raw",
        Quote(10, 10.6, 10.1, 10.5, 200), source="B", source_record="b1"))
    return ds


class ConflictAndWithdrawalTests(unittest.TestCase):
    def test_conflict_keeps_all_records_and_default_priority(self):
        ds = build()
        v = ds.quote_at("s2", BD, T(2024, 2, 5, 16))
        self.assertEqual(v.status, QuoteStatus.OK)
        self.assertEqual(v.quote.close, 10.0)            # 默认采用高优先级来源 A
        self.assertEqual(v.selection_basis, "default-priority")
        keys = {(c.source, c.source_record) for c in v.candidates}
        self.assertEqual(keys, {("A", "a1"), ("B", "b1")})
        self.assertTrue(all(c.active for c in v.candidates))

    def test_withdrawal_keeps_original_version_visible_at_earlier_viewpoint(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s2", BD, T(2024, 2, 6, 9, 0), "withdrawal", None,
            reason="A 源撤回 a1：代码错挂", source="A",
            source_record="a1-w", target_source="A", target_record="a1"))

        before = ds.quote_at("s2", BD, T(2024, 2, 5, 16))
        after = ds.quote_at("s2", BD, T(2024, 2, 6, 9, 30))
        self.assertEqual(before.quote.close, 10.0)       # 撤回到达前版本不变
        self.assertEqual(after.quote.close, 10.5)       # 改采 B
        a1 = next(c for c in after.candidates if c.source_record == "a1")
        self.assertFalse(a1.active)
        self.assertEqual(a1.withdrawn_by, "a1-w")       # 原记录保留且标注撤回

    def test_withdraw_all_candidates_is_explicit_status(self):
        ds = build()
        for src, rec, w in (("A", "a1", "wa"), ("B", "b1", "wb")):
            ds.add_event(QuoteEvent(
                "s2", BD, T(2024, 2, 6, 9, 0), "withdrawal", None,
                source=src, source_record=w,
                target_source=src, target_record=rec))
        v = ds.quote_at("s2", BD, T(2024, 2, 6, 10))
        self.assertEqual(v.status, QuoteStatus.WITHDRAWN)
        self.assertIsNone(v.quote)
        early = ds.quote_at("s2", BD, T(2024, 2, 5, 16))
        self.assertEqual(early.status, QuoteStatus.OK)

    def test_adjudication_is_bitemporal_and_append_only(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s2", BD, T(2024, 2, 6, 9, 0), "withdrawal", None,
            source="A", source_record="a1-w",
            target_source="A", target_record="a1"))
        ds.add_adjudication(Adjudication(
            "s2", BD, "B", "b1", T(2024, 2, 6, 10, 0),
            reason="交易所核对后以 B 为准", decision_id="d1"))
        # A 撤回后、裁决前：仅剩 B，单源采用
        v0930 = ds.quote_at("s2", BD, T(2024, 2, 6, 9, 30))
        self.assertEqual(v0930.selection_basis, "single-source")
        # 裁决到达后：记录数不变（无新行情），依据变为裁决
        v1030 = ds.quote_at("s2", BD, T(2024, 2, 6, 10, 30))
        self.assertEqual(v1030.selection_basis, "adjudicated")
        self.assertEqual(v1030.adjudication_id, "d1")
        # A 后来补发新价，但既有裁决仍指向有效记录 b1，不被新原始记录翻转
        ds.add_event(QuoteEvent(
            "s2", BD, T(2024, 2, 6, 11, 0), "raw",
            Quote(10, 10.3, 10.0, 10.2, 150), source="A", source_record="a2"))
        v1200 = ds.quote_at("s2", BD, T(2024, 2, 6, 12))
        self.assertEqual(v1200.source_record, "b1")
        # 新裁决改采 a2；旧视角看不到，历史版本保持可复算
        ds.add_adjudication(Adjudication(
            "s2", BD, "A", "a2", T(2024, 2, 6, 14, 0),
            reason="A 补发并经二次核对", decision_id="d2"))
        v1500 = ds.quote_at("s2", BD, T(2024, 2, 6, 15))
        self.assertEqual(v1500.quote.close, 10.2)
        self.assertEqual(v1500.adjudication_id, "d2")
        self.assertEqual(
            ds.quote_at("s2", BD, T(2024, 2, 6, 12)).source_record, "b1")
        # 所有来源原记录与两次裁决都仍可查
        self.assertEqual(
            {c.source_record for c in v1500.candidates},
            {"a1", "b1", "a2"})

    def test_revision_supersedes_after_it_arrives(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s2", BD, T(2024, 2, 8, 10, 0), "revision",
            Quote(10, 10.5, 10.0, 10.4, 180), source="B",
            source_record="b2", reason="成交回报修订"))
        before = ds.quote_at("s2", BD, T(2024, 2, 8, 9))
        after = ds.quote_at("s2", BD, T(2024, 2, 8, 11))
        self.assertEqual(before.quote.close, 10.0)
        self.assertEqual(after.quote.close, 10.4)
        self.assertEqual(after.selection_basis, "revision")

    def test_suspension_interleaves_by_received_time(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s2", D(2024, 2, 6), T(2024, 2, 6, 9, 20), "suspension", None,
            reason="盘中停牌", source="ann", source_record="sus1"))
        before = ds.quote_at("s2", D(2024, 2, 6), T(2024, 2, 6, 9, 10))
        after = ds.quote_at("s2", D(2024, 2, 6), T(2024, 2, 6, 10))
        self.assertEqual(before.status, QuoteStatus.MISSING)
        self.assertEqual(after.status, QuoteStatus.SUSPENDED)

    def test_split_announcement_changes_content_without_record_change(self):
        ds = build()
        before_ka = T(2024, 2, 7, 8)
        after_ka = T(2024, 2, 7, 10)
        v_before = ds.quote_at("s2", BD, before_ka)
        ds.add_split(Split("s2", D(2024, 2, 8), 2.0, T(2024, 2, 7, 9)))
        v_after = ds.quote_at("s2", BD, after_ka)
        self.assertEqual(v_before.adjusted_close, 10.0)
        self.assertEqual(v_after.adjusted_close, 5.0)
        # 行情记录数量完全相同，差异纯粹来自拆分公告这次新增信息
        self.assertEqual(
            [c.source_record for c in v_before.candidates],
            [c.source_record for c in v_after.candidates])
        diff = ds.explain_viewpoint_diff("s2", BD, before_ka, after_ka)
        self.assertEqual(
            diff["changed_fields"]["adjusted_close"],
            {"before": 10.0, "after": 5.0})
        self.assertEqual([i["type"] for i in diff["new_information"]], ["split"])

    def test_diff_explains_each_new_information_event(self):
        ds = build()
        diff = ds.explain_viewpoint_diff(
            "s2", BD, T(2024, 2, 5, 15, 30, 30), T(2024, 2, 5, 16, 30))
        # 采用记录仍是 a1（内容未翻转），但活跃候选集合发生了变化
        self.assertNotIn("source_record", diff["changed_fields"])
        self.assertEqual(
            diff["changed_fields"]["active_records"]["after"],
            (("A", "a1"), ("B", "b1")))
        # 15:30:30 时 b1 尚未到达；该区间唯一新增信息是来源 B 的原始记录
        self.assertEqual(
            [(i["type"], i["source_record"]) for i in diff["new_information"]],
            [("event", "b1")])

    def test_recompute_multiple_viewpoints_is_deterministic(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s2", BD, T(2024, 2, 6, 9, 0), "withdrawal", None,
            source="A", source_record="a1-w",
            target_source="A", target_record="a1"))
        ds.add_adjudication(Adjudication(
            "s2", BD, "B", "b1", T(2024, 2, 6, 10, 0),
            reason="裁决", decision_id="d1"))
        viewpoints = [T(2024, 2, 5, 16), T(2024, 2, 6, 9, 30),
                      T(2024, 2, 6, 10, 30)]
        first = [(v["view"].source_record, v["view"].selection_basis)
                 for v in ds.recompute_viewpoints("s2", BD, viewpoints)]
        second = [(v["view"].source_record, v["view"].selection_basis)
                  for v in ds.recompute_viewpoints("s2", BD, viewpoints)]
        self.assertEqual(first, second)
        self.assertEqual(first, [("a1", "default-priority"),
                                 ("b1", "single-source"),
                                 ("b1", "adjudicated")])


if __name__ == "__main__":
    unittest.main()
