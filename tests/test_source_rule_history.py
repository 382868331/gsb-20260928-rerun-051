"""来源选择规则的历史有效范围、修订身份与规则调整前后研究对照测试。"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from histquote import Dataset, Quote, QuoteEvent, QuoteStatus, Security, SourceRule

D = dt.date
T = dt.datetime
BD = D(2024, 3, 4)


def build():
    ds = Dataset(":memory:", name="rule-history")
    ds.add_security(Security("s3", "600003", "规则样本"))
    ds.calendar.add_sessions([D(2024, 3, d) for d in (4, 5, 6, 7)])
    ds.set_source_priority({"A": 1, "B": 2})  # 旧偏好（基线）
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 30), "raw",
        Quote(10, 10.1, 9.9, 10.0, 100), source="A", source_record="a1"))
    ds.add_event(QuoteEvent(
        "s3", BD, T(2024, 3, 4, 15, 31), "raw",
        Quote(10, 10.6, 10.1, 10.5, 200), source="B", source_record="b1"))
    return ds


class SourceRuleHistoryTests(unittest.TestCase):
    def test_future_rule_does_not_rewrite_historical_viewpoint(self):
        ds = build()
        # 3-05 才收到“B 优先于 A”的新规则；当天没有任何新行情到达
        ds.add_source_rule(SourceRule(
            "rule-b-first", T(2024, 3, 5, 9),
            {"B": 1, "A": 2}, reason="B 源质量复核后提权"))
        # 规则生效前的同一研究时点，重复查询仍是旧视角的价格 10.0
        historical = ds.quote_at("s3", BD, T(2024, 3, 4, 16))
        self.assertEqual(historical.quote.close, 10.0)
        self.assertEqual(historical.source_record, "a1")
        self.assertEqual(historical.selection_basis, "default-priority")
        self.assertEqual(historical.rule_id, "")  # 当时只有基线配置
        # 规则生效后的视角才看到 10.5
        current = ds.quote_at("s3", BD, T(2024, 3, 5, 10))
        self.assertEqual(current.quote.close, 10.5)
        self.assertEqual(current.rule_id, "rule-b-first")

    def test_rerun_past_with_new_rule_shows_comparison(self):
        ds = build()
        ds.add_source_rule(SourceRule(
            "rule-b-first", T(2024, 3, 10, 9),
            {"B": 1, "A": 2}, reason="事后提权"))
        cmp_ = ds.research_comparison(
            "s3", BD, T(2024, 3, 4, 16), new_rule_id="rule-b-first")
        # 保持原历史视角：价格不变
        self.assertEqual(cmp_["historical"]["view"].quote.close, 10.0)
        self.assertEqual(cmp_["historical"]["rule_id"], "")
        # 按新规则重新研究同一时点：仅裁决来源翻转，可见行情集合不变
        self.assertEqual(cmp_["revised"]["view"].quote.close, 10.5)
        self.assertEqual(cmp_["revised"]["rule_id"], "rule-b-first")
        fields = cmp_["changed_fields"]
        self.assertIn("close", fields)
        self.assertIn("source", fields)
        self.assertIn("rule_id", fields)
        self.assertEqual(fields["close"], {"before": 10.0, "after": 10.5})
        # 重研不能看到当时未收到的信息（修订/撤回/拆分仍按 known_at 截断）
        ds.add_event(QuoteEvent(
            "s3", BD, T(2024, 3, 5, 18), "revision",
            Quote(10, 10.1, 9.9, 9.7, 100), source="A",
            source_record="a2", reason="次日才修订"))
        rerun = ds.quote_at("s3", BD, T(2024, 3, 4, 16),
                            rule_id="rule-b-first")
        self.assertEqual(rerun.source_record, "b1")  # 修订当时还不存在

    def test_revert_to_old_config_keeps_new_revision_identity(self):
        ds = build()
        ds.add_source_rule(SourceRule(
            "rule-v2", T(2024, 3, 5, 9), {"B": 1, "A": 2}, reason="提权 B"))
        # 回退：内容与基线 {A:1,B:2} 完全相同，但必须是一次新的修订身份
        inserted, _ = ds.add_source_rule(SourceRule(
            "rule-v3-restore", T(2024, 3, 8, 9),
            {"A": 1, "B": 2}, reason="复核后回退旧配置",
            restores_rule_id=""))
        self.assertTrue(inserted)
        history = ds.source_rule_history()
        self.assertEqual([r["rule_id"] for r in history],
                         ["rule-v2", "rule-v3-restore"])
        v2 = ds.quote_at("s3", BD, T(2024, 3, 6))
        v3 = ds.quote_at("s3", BD, T(2024, 3, 9))
        self.assertEqual(v2.quote.close, 10.5)
        self.assertEqual(v2.rule_id, "rule-v2")
        self.assertEqual(v3.quote.close, 10.0)
        self.assertEqual(v3.rule_id, "rule-v3-restore")  # 身份不回退
        # 不能复用旧身份写入不同内容；同一身份重放必须幂等
        with self.assertRaises(ValueError):
            ds.add_source_rule(SourceRule(
                "rule-v2", T(2024, 3, 9, 9), {"C": 1}, reason="试图覆盖"))
        again, _seq = ds.add_source_rule(SourceRule(
            "rule-v2", T(2024, 3, 5, 9), {"B": 1, "A": 2}, reason="提权 B"))
        self.assertFalse(again)

    def test_save_reopen_and_repeated_query_keep_versions(self):
        ds = build()
        ds.add_source_rule(SourceRule(
            "rule-v2", T(2024, 3, 5, 9), {"B": 1, "A": 2}, reason="提权 B"))
        ds.add_source_rule(SourceRule(
            "rule-v3", T(2024, 3, 8, 9), {"A": 1, "B": 2}, reason="回退"))
        old_before = ds.quote_at("s3", BD, T(2024, 3, 4, 16)).as_dict()
        info = ds.snapshot_info()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.db"
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as reopened:
                # 指纹与规则修订链一致
                self.assertEqual(reopened.snapshot_info(), info)
                self.assertEqual(
                    [r["rule_id"] for r in reopened.source_rule_history()],
                    ["rule-v2", "rule-v3"])
                # 保存重开 + 重复查询后，原研究版本完全不变
                self.assertEqual(
                    reopened.quote_at("s3", BD, T(2024, 3, 4, 16)).as_dict(),
                    old_before)
                self.assertEqual(
                    reopened.quote_at("s3", BD, T(2024, 3, 4, 16)).as_dict(),
                    old_before)
                self.assertEqual(
                    reopened.quote_at("s3", BD, T(2024, 3, 4, 16)).quote.close,
                    10.0)
                self.assertEqual(
                    reopened.quote_at("s3", BD, T(2024, 3, 9)).rule_id,
                    "rule-v3")

    def test_comparison_series_and_information_timeline(self):
        ds = build()
        ds.add_event(QuoteEvent(
            "s3", BD, T(2024, 3, 6, 9, 0), "withdrawal", None,
            reason="A 撤回 a1", source="A", source_record="a1-w",
            target_source="A", target_record="a1"))
        from histquote import Split
        ds.add_split(Split("s3", D(2024, 3, 7), 2.0, T(2024, 3, 6, 18)))
        ds.add_source_rule(SourceRule(
            "rule-v2", T(2024, 3, 7, 9), {"B": 1, "A": 2}, reason="提权 B"))
        series = ds.research_comparison_series(
            "s3", BD,
            [T(2024, 3, 4, 16), T(2024, 3, 6, 12), T(2024, 3, 6, 20)],
            new_rule_id="rule-v2")
        self.assertEqual(len(series), 3)
        # 撤回后只剩 B：换规则也不再改变结果（规则不复活已撤回记录）
        self.assertEqual(series[1]["historical"]["view"].source_record, "b1")
        self.assertEqual(series[1]["revised"]["view"].source_record, "b1")
        self.assertNotIn("source_record", series[1]["changed_fields"])
        # 时间线可追溯价格、撤回、拆分公告、规则各自何时可见
        timeline = ds.information_timeline("s3", BD)
        kinds = [(t["type"], t.get("source_record") or t.get("rule_id"))
                 for t in timeline]
        self.assertIn(("event", "a1"), kinds)
        self.assertIn(("event", "b1"), kinds)
        self.assertIn(("event", "a1-w"), kinds)
        self.assertIn(("split", None), kinds)
        self.assertIn(("rule", "rule-v2"), kinds)
        self.assertEqual(
            [t["visible_at"] for t in timeline],
            sorted(t["visible_at"] for t in timeline))

    def test_unknown_rule_id_raises(self):
        ds = build()
        with self.assertRaises(KeyError):
            ds.quote_at("s3", BD, T(2024, 3, 4, 16), rule_id="missing")


if __name__ == "__main__":
    unittest.main()
