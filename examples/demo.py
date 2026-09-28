"""虚构样例：拆分、停牌、重复记录、迟到修订；全程离线、无网络访问。

运行：python -m examples.demo
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

from histquote import Dataset, Quote, QuoteEvent, QuoteStatus, Security, Split


def build() -> Dataset:
    ds = Dataset(":memory:", name="fictional-2024")

    ds.add_security(Security("sec_x", "600XXX", "虚构银行"))
    ds.add_security(Security("sec_y", "000YYY", "虚构科技"))

    # 2024-01-08(周一) 至 2024-01-12(周五) 为交易日，01-10 为异常日。
    ds.calendar.add_sessions(
        [dt.date(2024, 1, d) for d in (8, 9, 10, 11, 12)]
    )

    # --- sec_x：原始 -> 重复 -> 修订（迟到更正）---
    ds.add_event(QuoteEvent(
        "sec_x", dt.date(2024, 1, 8), dt.datetime(2024, 1, 8, 15, 30),
        kind="raw", quote=Quote(10.0, 10.2, 9.9, 10.1, 1_000_000),
        source="exchange-feed", source_record="x-20240108-v1",
    ))
    # 重放/重复推送：同来源同记录号、同内容 -> 幂等忽略，不产生第二条
    ds.add_event(QuoteEvent(
        "sec_x", dt.date(2024, 1, 8), dt.datetime(2024, 1, 8, 15, 30),
        kind="raw", quote=Quote(10.0, 10.2, 9.9, 10.1, 1_000_000),
        source="exchange-feed", source_record="x-20240108-v1",
    ))
    # 次日盘后收到官方修订：01-08 收盘价应为 9.8（原 10.1 系统计错误）
    ds.add_event(QuoteEvent(
        "sec_x", dt.date(2024, 1, 8), dt.datetime(2024, 1, 9, 18, 0),
        kind="revision", quote=Quote(10.0, 10.2, 9.8, 9.8, 1_000_000),
        reason="收盘统计更正", source="exchange-correction",
        source_record="x-20240108-v2",
    ))

    # --- sec_x：01-10 停牌 ---
    ds.add_event(QuoteEvent(
        "sec_x", dt.date(2024, 1, 10), dt.datetime(2024, 1, 10, 9, 20),
        kind="suspension", quote=None, reason="重大事项停牌",
        source="exchange-announce", source_record="x-susp-20240110",
    ))

    # --- sec_y：01-08 行情 01-11 才补齐（未来信息陷阱）---
    ds.add_event(QuoteEvent(
        "sec_y", dt.date(2024, 1, 8), dt.datetime(2024, 1, 11, 10, 0),
        kind="raw", quote=Quote(20.0, 20.5, 19.8, 20.2, 500_000),
        reason="数据源延迟补送", source="vendor-batch",
        source_record="y-20240108-v1",
    ))

    # --- sec_y：01-10 除权（1 拆 2），公告 01-12 才送达；01-09 收盘 40 是除权前价格 ---
    ds.add_split(Split(
        "sec_y", dt.date(2024, 1, 10), 2.0, dt.datetime(2024, 1, 12, 9, 0)
    ))
    ds.add_event(QuoteEvent(
        "sec_y", dt.date(2024, 1, 9), dt.datetime(2024, 1, 9, 15, 30),
        kind="raw", quote=Quote(40.0, 40.4, 39.6, 40.0, 800_000),
        source="exchange-feed", source_record="y-20240109-v1",
    ))

    return ds


def main() -> None:
    ds = build()

    print("== 1) 修订前（2024-01-09 09:00 回测 01-08 收盘）==")
    v = ds.quote_at("sec_x", dt.date(2024, 1, 8), dt.datetime(2024, 1, 9, 9, 0))
    print(f"   close={v.quote.close}  来源={v.source}/{v.source_record}")

    print("== 2) 修订后（2024-01-10 回看，同一业务日）==")
    v = ds.quote_at("sec_x", dt.date(2024, 1, 8), dt.datetime(2024, 1, 10, 9, 0))
    print(f"   close={v.quote.close}  来源={v.source}/{v.source_record}")

    print("== 3) 停牌日不是缺失数据 ==")
    v = ds.quote_at("sec_x", dt.date(2024, 1, 10), dt.datetime(2024, 1, 10, 16, 0))
    print(f"   status={v.status.value}  reason 已记录在事件日志")

    print("== 4) 迟到补齐：01-10 时 sec_y 的 01-08 仍不可知 ==")
    v = ds.quote_at("sec_y", dt.date(2024, 1, 8), dt.datetime(2024, 1, 10, 9, 0))
    print(f"   status={v.status.value}（不能当作 20.2 交易）")
    v = ds.quote_at("sec_y", dt.date(2024, 1, 8), dt.datetime(2024, 1, 11, 12, 0))
    print(f"   补送后 status={v.status.value}, close={v.quote.close}")

    print("== 5) 拆分因子也按收到时间生效 ==")
    before = ds.quote_at("sec_y", dt.date(2024, 1, 9), dt.datetime(2024, 1, 11, 9, 0))
    after = ds.quote_at("sec_y", dt.date(2024, 1, 9), dt.datetime(2024, 1, 12, 12, 0))
    print(f"   公告前 adjusted_close={before.adjusted_close}（=40.0，因子未知）")
    print(f"   公告后 adjusted_close={after.adjusted_close}（40/2，1拆2已收到）")

    print("== 6) 按收到时间追踪原始/修订事件 ==")
    for row in ds.received_log("sec_x", dt.datetime(2024, 1, 8), dt.datetime(2024, 1, 10)):
        print("   ", row["received_at"], row["kind"], "close=", row["close"], row["source_record"])

    print("== 7) 保存数据集并重开，查询与来源一致 ==")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "research.db"
        saved = build()
        info_before = saved.snapshot_info()
        saved.save_as(path)
        saved.close()
        with Dataset.open(path) as reopened:
            assert reopened.snapshot_info() == info_before
            v = reopened.quote_at("sec_x", dt.date(2024, 1, 8),
                                  dt.datetime(2024, 1, 10, 9, 0))
            print(f"   重开后 close={v.quote.close} 来源={v.source}/{v.source_record}")
            print(f"   指纹一致：{info_before['counts']}")


if __name__ == "__main__":
    main()
