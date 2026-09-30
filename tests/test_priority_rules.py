"""来源优先级规则的历史有效范围、研究对照与回退身份测试。"""

import datetime as dt
import os
import tempfile
import unittest

from histquote import (
    Dataset, PriorityRule, Quote, QuoteEvent, QuoteStatus, Security, Split,
)

D = dt.date
T = dt.datetime
BD = D(2024, 3, 4)


def build():
    ds = Dataset(":memory:", name="priority")
    ds.add_security(Security("s3", "600003", "优先级样本"))
    ds.calendar.add_sessions([D(2024, 3, d) for d in (4, 5, 6, 7, 8)])
    # 初始规则：A 优先（2024-03-04 收盘前已生效）
    ds.add_priority_rule(PriorityRule(
        {"A": 1, "B": 2}, T(2024, 3, 4, 9, 0), "初始规则", "r1"))
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 30), "raw",
        Quote(10, 10.1, 9.9, 10.0, 100), source="A", source_record="a1"))
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 31), "raw",
        Quote(10, 10.6, 10.1, 10.5, 200), source="B", source_record="b1"))
    return ds


class PriorityRuleTests(unittest.TestCase):
    def test_rule_change_does_not_rewrite_history(self):
        ds = build()
        before = ds.quote_at("s3", BD, T(2024, 3, 4, 16))
        self.assertEqual(before.quote.close, 10.0)
        self.assertEqual(before.priority_rule_id, "r1")
        # 3 月 5 日调整为 B 优先；当时并没有新收到可见行情
        ds.set_source_priority_at(
            {"B": 1, "A": 2}, T(2024, 3, 5, 9, 0), "B 源质量更稳", "r2")
        # 同一研究时点（3-04 16:00）复算：结果保持原历史视角
        again = ds.quote_at("s3", BD, T(2024, 3, 4, 16))
        self.assertEqual(again.quote.close, 10.0)
        self.assertEqual(again.priority_rule_id, "r1")
        # 新规则生效后的研究时点才按新偏好
        later = ds.quote_at("s3", BD, T(2024, 3, 5, 10))
        self.assertEqual(later.quote.close, 10.5)
        self.assertEqual(later.priority_rule_id, "r2")

    def test_compare_rule_adjustment(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s3", BD, T(2024, 3, 5, 8, 30), "revision",
            Quote(10, 10.2, 9.8, 9.8, 100), source="corr",
            source_record="c1", reason="迟到修订"))
        ds.set_source_priority_at(
            {"B": 1, "A": 2}, T(2024, 3, 5, 9, 0), "调整", "r2")
        cmp = ds.compare_rule_adjustment("s3", BD, T(2024, 3, 4, 16))
        # 保持原历史视角：仍采 A
        self.assertEqual(cmp["original"].quote.close, 10.0)
        self.assertEqual(cmp["original_priority_rule_id"], "r1")
        # 按新规则重新研究同一时点：数据可见性不变，改采 B
        self.assertEqual(cmp["revised"].quote.close, 10.5)
        self.assertEqual(cmp["revised_priority_rule_id"], "r2")
        self.assertEqual(cmp["revised"].known_at, cmp["original"].known_at)
        self.assertEqual(
            cmp["changed_fields"]["close"], {"before": 10.0, "after": 10.5})
        # 时间线可追溯每条信息何时可见（含迟到修订与规则修订）
        kinds = [(i["type"], i.get("source_record") or i.get("rule_id"))
                 for i in cmp["information_timeline"]]
        self.assertIn(("event", "a1"), kinds)
        self.assertIn(("event", "b1"), kinds)
        self.assertIn(("event", "c1"), kinds)
        self.assertIn(("priority_rule", "r2"), kinds)
        c1 = next(i for i in cmp["information_timeline"]
                  if i.get("source_record") == "c1")
        self.assertEqual(c1["received_at"], T(2024, 3, 5, 8, 30))

    def test_timeline_covers_withdrawal_revision_and_split(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s3", BD, T(2024, 3, 5, 9, 0), "withdrawal", None,
            source="A", source_record="a1-w",
            target_source="A", target_record="a1"))
        ds.add_event(QuoteEvent(
            "s3", BD, T(2024, 3, 5, 10, 0), "revision",
            Quote(10, 10.5, 10.0, 10.4, 180), source="B", source_record="b2"))
        ds.add_split(Split("s3", D(2024, 3, 6), 2.0, T(2024, 3, 5, 11, 0)))
        timeline = ds.information_timeline("s3", BD)
        self.assertEqual(
            [(i["type"], i["kind"]) for i in timeline],
            [("priority_rule", "priority_rule"),
             ("event", "raw"), ("event", "raw"),
             ("event", "withdrawal"), ("event", "revision"),
             ("split", "split")])
        w = timeline[3]
        self.assertEqual(w["target"], ["A", "a1"])

    def test_rollback_preserves_revision_identity(self):
        ds = build()
        ds.set_source_priority_at(
            {"B": 1, "A": 2}, T(2024, 3, 5, 9, 0), "调整", "r2")
        ds.rollback_source_priority("r1", T(2024, 3, 6, 9, 0),
                                    "回退到旧配置", "r3")
        rules = ds.priority_rules()
        self.assertEqual([r.rule_id for r in rules], ["r1", "r2", "r3"])
        self.assertEqual(rules[2].priority, {"A": 1, "B": 2})
        self.assertEqual(rules[2].supersedes, "r2")   # 回退是新修订，身份保留
        # 回退生效后按旧配置裁决；中间时段仍按 r2
        self.assertEqual(
            ds.quote_at("s3", BD, T(2024, 3, 6, 10)).quote.close, 10.0)
        self.assertEqual(
            ds.quote_at("s3", BD, T(2024, 3, 5, 10)).quote.close, 10.5)
        self.assertEqual(
            ds.quote_at("s3", BD, T(2024, 3, 6, 10)).priority_rule_id, "r3")
        # 规则重放幂等
        inserted, _ = ds.add_priority_rule(rules[1])
        self.assertFalse(inserted)

    def test_save_reopen_and_repeat_queries_keep_versions(self):
        ds = build()
        ds.set_source_priority_at(
            {"B": 1, "A": 2}, T(2024, 3, 5, 9, 0), "调整", "r2")
        ka = T(2024, 3, 4, 16)
        expected = ds.quote_at("s3", BD, ka).as_dict()
        info = ds.snapshot_info()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.db")
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as ds2:
                self.assertEqual(ds2.snapshot_info(), info)
                self.assertEqual(
                    [r.rule_id for r in ds2.priority_rules()], ["r1", "r2"])
                for _ in range(3):  # 重复查询结果不变
                    self.assertEqual(ds2.quote_at("s3", BD, ka).as_dict(), expected)
                    self.assertEqual(ds2.quote_at("s3", BD, ka).quote.close, 10.0)

    def test_legacy_inmemory_priority_still_applies_to_all_viewpoints(self):
        ds = Dataset(":memory:")
        ds.add_security(Security("s3", "600003", "兼容"))
        ds.calendar.add_sessions([BD])
        ds.set_source_priority({"B": 1, "A": 2})
        ds.add_event(QuoteEvent(
            "s3", BD, T(2024, 3, 4, 15, 30), "raw",
            Quote(10, 10.1, 9.9, 10.0, 100), source="A", source_record="a1"))
        ds.add_event(QuoteEvent(
            "s3", BD, T(2024, 3, 4, 15, 31), "raw",
            Quote(10, 10.6, 10.1, 10.5, 200), source="B", source_record="b1"))
        v = ds.quote_at("s3", BD, T(2024, 3, 4, 16))
        self.assertEqual(v.status, QuoteStatus.OK)
        self.assertEqual(v.quote.close, 10.5)
        self.assertEqual(v.priority_rule_id, "")


if __name__ == "__main__":
    unittest.main()
