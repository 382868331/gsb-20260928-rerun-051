"""虚构样例：演示拆股、停牌、重复记录与行情修订。

所有证券与数据均为虚构（FIGI 以 BBG000FAKE 开头，代码 600FAK），
不对应任何真实证券，全程离线。

故事线（交易所 XSHG 虚构股票 云岚科技 YUNLAN）：

- 2026-01-05（周一）收盘后，数据商 18:05 发来当天行情，收盘 10.00。
- 19:30 重发了同一批文件（字段相同），系统识别为重复记录，忽略。
- 次日 09:00 数据商发来更正：收盘价应为 10.20（原 10.00 系录入错误）。
- 2026-01-06 公司公告 1 拆 2，除权日 2026-01-08；公告 10:00 才送达。
- 2026-01-07 盘中临时停牌，当天无成交；停牌通知 13:05 才送达。
- 2026-01-08 除权后，旧价口径需除以 2。
- 2026-01-12（下周一）数据商补齐了 1 月 6 日缺失的成交量（迟到数据）。
"""

from __future__ import annotations

from datetime import date

from market_research.instruments import Instrument
from market_research.dataset import MarketDataset

FIGI = "BBG000FAKE001"
EXCHANGE = "XSHG"


def build(ds: MarketDataset) -> MarketDataset:
    cal = ds.calendar(EXCHANGE)
    cal.fill_weekdays("2026-01-05", "2026-01-12")
    cal.add_holidays(["2026-01-01"], "元旦")

    ds.instruments.register(
        Instrument(
            figi=FIGI, symbol="600FAK", name="云岚科技（虚构样例）",
            currency="CNY", exchange=EXCHANGE, lot_size=100,
            first_date=date(2026, 1, 5),
        ),
        received_at="2026-01-05T09:00:00+08:00",
        aliases={"600YUN": "2026-01-05"},
    )

    q = ds.quotes
    # 1/5：初值 10.00，随后完全相同的重放被判为重复，当晚更正为 10.20
    print("1/5 初报:", q.upsert(FIGI, "2026-01-05", open=9.9, high=10.1, low=9.8,
          close=10.0, volume=1_000_000, source="虚构数据商/盘后文件A",
          source_record_id="A-0105-1",
          received_at="2026-01-05T18:05:00+08:00"))
    print("1/5 重复重放:", q.upsert(FIGI, "2026-01-05", open=9.9, high=10.1, low=9.8,
          close=10.0, volume=1_000_000, source="虚构数据商/盘后文件A",
          source_record_id="A-0105-1",
          received_at="2026-01-05T19:30:00+08:00"))
    print("1/5 晚间更正:", q.upsert(FIGI, "2026-01-05", open=9.9, high=10.25, low=9.8,
          close=10.20, volume=1_000_000, source="虚构数据商/更正单R1",
          source_record_id="R1-0105",
          received_at="2026-01-06T09:00:00+08:00", remark="收盘价录入错误更正"))

    # 1/6：成交量缺失（None），1/12 才补齐
    print("1/6 初报(缺成交量):", q.upsert(FIGI, "2026-01-06", open=10.3, high=10.5,
          low=10.1, close=10.4, volume=None, source="虚构数据商/盘后文件B",
          source_record_id="B-0106-1",
          received_at="2026-01-06T18:05:00+08:00", remark="成交量缺失"))

    # 拆股公告：1 拆 2，除权日 1/8，1/6 上午 10:00 送达
    print("拆股公告:", ds.events.record_split(
        FIGI, "2026-01-08", 2.0, source="虚构交易所/公司行为公告",
        received_at="2026-01-06T10:00:00+08:00"))

    # 1/7 停牌，13:05 才通知到系统
    print("1/7 停牌:", ds.events.record_halt(
        FIGI, "2026-01-07", "HALTED", source="虚构交易所/临时停牌通知",
        received_at="2026-01-07T13:05:00+08:00", remark="盘中临时停牌"))

    # 1/8 除权后行情（价格已在拆股后口径）
    print("1/8 行情:", q.upsert(FIGI, "2026-01-08", open=5.2, high=5.35, low=5.1,
          close=5.30, volume=2_500_000, source="虚构数据商/盘后文件C",
          source_record_id="C-0108-1",
          received_at="2026-01-08T18:05:00+08:00", remark="1拆2除权日"))

    # 1/12 补齐 1/6 成交量（迟到数据，不能污染 1/6 当晚的回测）
    print("1/6 成交量补齐:", q.upsert(FIGI, "2026-01-06", open=10.3, high=10.5,
          low=10.1, close=10.4, volume=1_200_000, source="虚构数据商/补数文件D",
          source_record_id="D-0106-FIX",
          received_at="2026-01-12T09:30:00+08:00", remark="迟到补成交量"))

    return ds
