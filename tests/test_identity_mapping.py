"""证券更名、代码复用、跨市场转板与映射撤回的交错资料测试。"""

import datetime as dt
import os
import sqlite3
import tempfile
import unittest

from histquote import (
    Dataset,
    IdentityAnnouncement,
    PortfolioPosition,
    Quote,
    QuoteEvent,
    QuoteStatus,
    Security,
)

D = dt.date
T = dt.datetime
CODE = "900001"


def code_event(security_id, date, received_at, close, source, record):
    return QuoteEvent(
        security_id, date, received_at, "raw",
        Quote(close, close, close, close, 100), source=source,
        source_record=record, reported_code=CODE)


def build():
    ds = Dataset(":memory:", name="identity")
    ds.add_security(Security("old_sid", "100001", "旧市场甲证券"))
    ds.add_security(Security("new_sid", "999999", "新市场乙证券"))
    ds.calendar.add_sessions([D(2024, 4, d) for d in (8, 9, 10, 11, 12)])
    return ds


class IdentityMappingTests(unittest.TestCase):
    def test_legacy_v3_database_opens_and_old_security_remains_interpretable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v3.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE security(security_id TEXT PRIMARY KEY,"
                " code TEXT NOT NULL UNIQUE, name TEXT NOT NULL);"
                "INSERT INTO security VALUES ('old_sid','100001','旧名');"
                "CREATE TABLE trading_session(session_date TEXT PRIMARY KEY);"
                "INSERT INTO trading_session VALUES ('2024-04-09');"
                "CREATE TABLE corp_split(security_id TEXT, ex_date TEXT,"
                " ratio REAL, received_at TEXT);"
                "CREATE TABLE quote_event(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " security_id TEXT, business_date TEXT, received_at TEXT,"
                " kind TEXT, open REAL, high REAL, low REAL, close REAL,"
                " volume REAL, reason TEXT DEFAULT '', source TEXT,"
                " source_record TEXT, target_source TEXT DEFAULT '',"
                " target_record TEXT DEFAULT '', content_hash TEXT);"
                "CREATE TABLE adjudication(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " security_id TEXT, business_date TEXT, chosen_source TEXT,"
                " chosen_record TEXT, decision_received_at TEXT, reason TEXT,"
                " decision_id TEXT);"
                "CREATE TABLE priority_rule(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " rule_id TEXT, received_at TEXT, priority TEXT, reason TEXT,"
                " supersedes TEXT);"
            )
            conn.commit()
            conn.close()
            with Dataset.open(path) as reopened:
                self.assertEqual(reopened.securities()[0].code, "100001")
                view = reopened.quote_at("old_sid", D(2024, 4, 9),
                                         T(2024, 4, 9, 16))
                self.assertEqual(view.status, QuoteStatus.MISSING)

    def test_code_reassignment_and_late_announcements_do_not_splice_history(self):
        ds = build()
        old_before = D(2024, 4, 8)
        new_after = D(2024, 4, 9)
        # 4/9 当天先收到 900001 报价，但公告尚未到达。
        ds.add_event(code_event("new_sid", new_after, T(2024, 4, 9, 15, 30),
                                20.0, "new-feed", "n1"))
        early = ds.quote_by_code(CODE, new_after, T(2024, 4, 9, 16))
        self.assertEqual(early.status, QuoteStatus.AMBIGUOUS)
        self.assertEqual(early.identity_status, "ambiguous")
        self.assertEqual(early.identity_candidates, ("new_sid",))
        self.assertEqual(early.candidates[0].security_id, "new_sid")
        self.assertIsNone(early.quote)

        # 4/10 才收到：900001 自 4/9 起从旧证券改配给新证券。
        ds.add_identity_announcement(IdentityAnnouncement(
            "a-old", CODE, "old_sid", D(2024, 4, 1),
            T(2024, 4, 10, 8), name="旧市场甲证券",
            source="old-exchange", source_record="old-map"))
        ds.add_identity_announcement(IdentityAnnouncement(
            "a-new", CODE, "new_sid", D(2024, 4, 9),
            T(2024, 4, 10, 8, 1), name="新市场乙证券",
            source="new-exchange", source_record="new-map",
            supersedes="a-old"))

        old_day = ds.quote_by_code(CODE, old_before, T(2024, 4, 10, 9))
        new_day = ds.quote_by_code(CODE, new_after, T(2024, 4, 10, 9))
        self.assertEqual(old_day.security_id, "old_sid")
        self.assertEqual(new_day.security_id, "new_sid")
        self.assertIsNone(old_day.quote)  # 旧业务日没有旧证券报价，不能接新价
        self.assertEqual(new_day.quote.close, 20.0)
        self.assertEqual(new_day.identity_announcement_ids, ("a-new",))

        # 4/11 才迟到的旧代码报价，业务日在旧映射期，仍属于旧证券。
        ds.add_event(code_event("old_sid", old_before, T(2024, 4, 11, 9),
                                10.0, "old-feed", "o1"))
        old_late = ds.quote_by_code(CODE, old_before, T(2024, 4, 11, 10))
        new_unchanged = ds.quote_by_code(CODE, new_after, T(2024, 4, 11, 10))
        self.assertEqual(old_late.security_id, "old_sid")
        self.assertEqual(old_late.quote.close, 10.0)
        self.assertEqual(new_unchanged.quote.close, 20.0)
        self.assertEqual([c.source_record for c in old_late.candidates], ["o1"])

    def test_mapping_withdrawal_preserves_old_interpretation(self):
        ds = build()
        date = D(2024, 4, 9)
        ds.add_event(code_event("new_sid", date, T(2024, 4, 9, 15, 30),
                                20.0, "new-feed", "n1"))
        ds.add_identity_announcement(IdentityAnnouncement(
            "a-new", CODE, "new_sid", D(2024, 4, 9),
            T(2024, 4, 10, 8), name="新市场乙证券",
            source="new-exchange", source_record="new-map"))
        before_withdraw = T(2024, 4, 10, 9)
        confirmed = ds.quote_by_code(CODE, date, before_withdraw)
        self.assertEqual(confirmed.status, QuoteStatus.OK)
        self.assertEqual(confirmed.quote.close, 20.0)

        ds.add_identity_announcement(IdentityAnnouncement(
            "w-new", CODE, "new_sid", D(2024, 4, 9),
            T(2024, 4, 11, 8), action="withdraw", supersedes="a-new",
            source="new-exchange", source_record="new-map-w",
            reason="公告依据不足，撤回映射"))
        withdrawn = ds.quote_by_code(CODE, date, T(2024, 4, 11, 9))
        self.assertEqual(withdrawn.status, QuoteStatus.AMBIGUOUS)
        self.assertIsNone(withdrawn.quote)
        self.assertNotIn("a-new", withdrawn.identity_announcement_ids)
        self.assertEqual(withdrawn.candidates[0].source_record, "n1")
        timeline = ds.information_timeline("new_sid", date)
        self.assertEqual(
            [(i["type"], i["announcement_id"]) for i in timeline
             if i["type"] == "identity_announcement"],
            [("identity_announcement", "a-new"),
             ("identity_announcement", "w-new")])
        # 旧研究视角与原解释原样保留。
        again = ds.quote_by_code(CODE, date, before_withdraw)
        self.assertEqual(again.quote.close, 20.0)
        self.assertEqual(again.identity_announcement_ids, ("a-new",))

    def test_portfolio_dedupes_same_security_and_classifies_adjustment(self):
        ds = build()
        ds.add_identity_announcement(IdentityAnnouncement(
            "a-old-rename", "100001", "old_sid", D(2024, 4, 1),
            T(2024, 4, 10, 7, 59), name="甲证券股份有限公司",
            source="old-exchange", source_record="rename"))
        ds.add_identity_announcement(IdentityAnnouncement(
            "a-old-code", CODE, "old_sid", D(2024, 4, 1),
            T(2024, 4, 10, 8), name="甲证券股份有限公司",
            source="old-exchange", source_record="old-map"))
        ds.add_identity_announcement(IdentityAnnouncement(
            "a-new-code", CODE, "new_sid", D(2024, 4, 9),
            T(2024, 4, 10, 8, 1), name="新市场乙证券",
            source="new-exchange", source_record="new-map",
            supersedes="a-old-code"))
        ds.add_event(QuoteEvent(
            "old_sid", D(2024, 4, 9), T(2024, 4, 9, 15, 30), "raw",
            Quote(10, 10, 10, 10, 100), source="old", source_record="o",
            reported_code="100001"))
        ds.add_event(code_event("new_sid", D(2024, 4, 9), T(2024, 4, 9, 15, 31),
                                20.0, "new", "n"))
        positions = [
            PortfolioPosition("100001", "old_sid", 1),
            PortfolioPosition(CODE, "old_sid", 1),  # 同一稳定证券的旧/新显示代码
            PortfolioPosition(CODE, "new_sid", 1),
        ]
        result = ds.research_portfolio(
            positions, D(2024, 4, 9), T(2024, 4, 10, 9))
        self.assertEqual(result["security_ids"], ("old_sid", "new_sid"))
        old_entry = next(p for p in result["positions"] if p["security_id"] == "old_sid")
        self.assertEqual(old_entry["quantity"], 2)
        self.assertEqual(old_entry["duplicate_codes"], ["100001", CODE])
        self.assertEqual(result["total_close_value"], 40.0)

        # 名称公告改变名称但不改价格；新代码映射改变组合证券范围。
        before_announcements = ds.research_portfolio(
            positions, D(2024, 4, 9), T(2024, 4, 9, 16))
        cmp = ds.compare_identity_adjustment(
            positions, D(2024, 4, 9), T(2024, 4, 9, 16), T(2024, 4, 10, 9))
        self.assertEqual(before_announcements["security_ids"],
                         ("old_sid", "new_sid"))
        self.assertEqual(
            {c["change"] for c in cmp["security_range_changes"]}, set())
        self.assertEqual(cmp["price_changes"], ())
        self.assertEqual(
            [n["security_id"] for n in cmp["name_only_changes"]], ["old_sid"])

        display_positions = [
            PortfolioPosition("100001"),
            PortfolioPosition(CODE),
        ]
        scope_cmp = ds.compare_identity_adjustment(
            display_positions, D(2024, 4, 9), T(2024, 4, 9, 16),
            T(2024, 4, 10, 9))
        self.assertEqual(
            scope_cmp["original"]["security_ids"], ("old_sid",))
        self.assertEqual(
            scope_cmp["revised"]["security_ids"], ("old_sid", "new_sid"))
        self.assertEqual(
            scope_cmp["security_range_changes"][0]["security_id"], "new_sid")
        self.assertEqual(
            scope_cmp["security_range_changes"][0]["change"], "added")

    def test_save_reopen_keeps_raw_quotes_and_saved_identity_interpretation(self):
        ds = build()
        date = D(2024, 4, 9)
        ds.add_event(code_event("new_sid", date, T(2024, 4, 9, 15, 30),
                                20.0, "new-feed", "n1"))
        ds.add_identity_announcement(IdentityAnnouncement(
            "a-new", CODE, "new_sid", D(2024, 4, 9),
            T(2024, 4, 10, 8), name="新市场乙证券",
            source="new-exchange", source_record="new-map"))
        old_ka = T(2024, 4, 10, 9)
        expected = ds.quote_by_code(CODE, date, old_ka).as_dict()
        info = ds.snapshot_info()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "identity.db")
            ds.save_as(path)
            ds.close()
            with Dataset.open(path) as reopened:
                self.assertEqual(reopened.snapshot_info(), info)
                self.assertEqual(
                    reopened.quote_by_code(CODE, date, old_ka).as_dict(), expected)


if __name__ == "__main__":
    unittest.main()
