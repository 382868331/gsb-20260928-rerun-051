"""演示显示代码复用、身份公告迟到/撤回与组合研究版本保留。"""

import datetime as dt
import os
import tempfile

from histquote import (
    CodeQuoteEvent, Dataset, IdentityAnnouncement, Quote, Security,
)

D = dt.date
T = dt.datetime


def main() -> None:
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "identity.db")
    with Dataset(path, name="identity-demo") as ds:
        ds.add_security(Security("old", "000001", "旧公司"))
        ds.add_security(Security("new", "000001", "新公司"))
        ds.calendar.add_sessions([D(2024, 1, 31), D(2024, 2, 1)])

        # 1 月 31 日的旧证券报价，在 2 月 1 日上午才送达。
        ds.add_code_event(CodeQuoteEvent(
            "000001", D(2024, 1, 31), T(2024, 2, 1, 9, 30), "raw",
            Quote(10, 10, 10, 10.0, 100), source="feed", source_record="old-late"))
        unknown = ds.quote_for_code("000001", D(2024, 1, 31), T(2024, 2, 1, 10))
        print("公告不足:", unknown.status.value, "报价数:", len(unknown.candidates))

        # 公告确认 1 月代码属于旧公司；迟到报价此时才可解释。
        ds.add_identity_announcement(IdentityAnnouncement(
            "000001", "old", D(2024, 1, 1), T(2024, 2, 1, 10, 30),
            action="confirm", name="旧公司", effective_to=D(2024, 1, 31),
            source="exch", source_record="old-map", announcement_id="old-map"))
        old_view = ds.quote_for_code(
            "000001", D(2024, 1, 31), T(2024, 2, 1, 11))
        print("迟到报价解释:", old_view.security_id, old_view.quote.close,
              old_view.identity_announcement_ids)

        # 2 月 1 日代码再次分配给新公司；新旧报价不串接。
        ds.add_identity_announcement(IdentityAnnouncement(
            "000001", "new", D(2024, 2, 1), T(2024, 2, 1, 8),
            action="confirm", name="新公司",
            source="exch", source_record="new-map", announcement_id="new-map"))
        ds.add_code_event(CodeQuoteEvent(
            "000001", D(2024, 2, 1), T(2024, 2, 1, 15), "raw",
            Quote(20, 20, 20, 20.0, 100), source="feed", source_record="new-q"))
        new_view = ds.quote_for_code(
            "000001", D(2024, 2, 1), T(2024, 2, 1, 16))
        print("代码重分配:", new_view.security_id, new_view.quote.close,
              [c.source_record for c in new_view.candidates])

        # 组合中同一稳定证券通过代码和 ID 各持一笔：只取一次行情。
        holdings = [
            {"display_code": "000001", "weight": 1.0, "label": "代码持仓"},
            {"security_id": "old", "weight": 1.0, "label": "ID持仓"},
        ]
        saved = ds.save_portfolio_research(
            "p1", holdings, D(2024, 1, 31), T(2024, 2, 1, 11),
            T(2024, 2, 1, 11))
        print("保存组合:", saved["security_ids"], saved["portfolio_value"])

        # 撤回旧映射不会改写已保存研究。
        ds.add_identity_announcement(IdentityAnnouncement(
            "000001", "", D(2024, 1, 1), T(2024, 2, 1, 17),
            action="withdraw", target_announcement_id="old-map",
            source="exch", source_record="withdraw-old-map",
            announcement_id="withdraw-old-map"))
        check = ds.reopen_saved_research("p1")
        print("重开保存研究:", check["same_value"], check["same_security_ids"],
              check["reopened"]["positions"][0]["security_id"])


if __name__ == "__main__":
    main()
