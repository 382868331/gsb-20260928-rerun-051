"""证券身份公告、代码复用/转板与组合调整对照示例（虚构数据，离线）。"""

from __future__ import annotations

import datetime as dt

from histquote import (
    Dataset,
    IdentityAnnouncement,
    PortfolioPosition,
    Quote,
    QuoteEvent,
    Security,
)

D = dt.date
T = dt.datetime
CODE = "900001"


def quote(sid, date, received, close, record, code=CODE):
    return QuoteEvent(
        sid, date, received, "raw",
        Quote(close, close, close, close, 100),
        source=record.split("-")[0], source_record=record,
        reported_code=code,
    )


def main() -> None:
    ds = Dataset(":memory:", name="identity-demo")
    ds.add_security(Security("old", "100001", "甲证券"))
    ds.add_security(Security("new", "999999", "乙证券"))
    ds.calendar.add_sessions([D(2024, 4, d) for d in (8, 9, 10)])

    # 4/9 新报价先到，但 900001 的归属公告尚未收到，只能保留候选。
    ds.add_event(quote("new", D(2024, 4, 9), T(2024, 4, 9, 15, 30), 20, "new-n1"))
    early = ds.quote_by_code(CODE, D(2024, 4, 9), T(2024, 4, 9, 16))
    print("公告前:", early.status.value, early.identity_source_rule,
          [c.security_id for c in early.candidates])

    # 公告：900001 在 4/9 起从甲转配给乙；新公告取代旧映射但不抹除原记录。
    ds.add_identity_announcement(IdentityAnnouncement(
        "old-map", CODE, "old", D(2024, 4, 1), T(2024, 4, 10, 8),
        name="甲证券", source="old-x", source_record="old-map"))
    ds.add_identity_announcement(IdentityAnnouncement(
        "new-map", CODE, "new", D(2024, 4, 9), T(2024, 4, 10, 8, 1),
        name="乙证券", source="new-x", source_record="new-map",
        supersedes="old-map"))

    # 4/11 迟到的 4/8 旧代码报价属于甲，不能与乙的 4/9 价格接成一条序列。
    ds.add_event(quote("old", D(2024, 4, 8), T(2024, 4, 11, 9), 10, "old-o1"))
    ds.add_event(quote("old", D(2024, 4, 9), T(2024, 4, 9, 15, 32), 10,
                       "old-o2", code="100001"))
    old_day = ds.quote_by_code(CODE, D(2024, 4, 8), T(2024, 4, 11, 10))
    new_day = ds.quote_by_code(CODE, D(2024, 4, 9), T(2024, 4, 11, 10))
    print("公告后:", old_day.security_id, old_day.quote.close, "->",
          new_day.security_id, new_day.quote.close)

    positions = [
        PortfolioPosition("100001", "old", 1),
        PortfolioPosition(CODE, "old", 1),
        PortfolioPosition(CODE, "new", 1),
    ]
    held = ds.research_portfolio(positions, D(2024, 4, 9), T(2024, 4, 11, 10))
    print("组合去重:", held["security_ids"], held["total_close_value"])
    for item in held["positions"]:
        print(" ", item["security_id"], item["name"],
              item["view"].quote.close, "qty=", item["quantity"],
              item["source_rule"])


if __name__ == "__main__":
    main()
