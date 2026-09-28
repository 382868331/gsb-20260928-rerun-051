"""端到端演示：构建样例数据集 -> 保存 -> 重开 -> 不同时点查询对比。"""

from __future__ import annotations

import sys
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_research.backtest import PointInTimeView
from market_research.dataset import MarketDataset, open_dataset

from sample_data import FIGI, build

CN = ZoneInfo("Asia/Shanghai")


def fmt(dt):
    return dt.astimezone(CN).strftime("%m-%d %H:%M")


def show(title, bar):
    if bar is None:
        print(f"{title}: <当时无数据>")
    else:
        print(f"{title}: 收盘={bar.raw_close} 复权后={bar.close:.4f}"
              f" 停牌={bar.halted} 来源={bar.source} 收到={fmt(bar.received_at)}")


def main(tmp_dir: str = "/tmp/mr_demo_dataset") -> None:
    shutil.rmtree(tmp_dir, ignore_errors=True)
    # 第一次：构建并保存
    with MarketDataset(tmp_dir) as ds:
        build(ds)
        print("\n--- 修订版本链 2026-01-05 ---")
        for v in ds.quotes.history(FIGI, "2026-01-05"):
            print(f"  close={v.close} 来源={v.source} 收到={fmt(v.received_at)}"
                  f" 被取代={v.superseded_at is not None}")
        ds.save()

    # 重开并校验来源一致
    with open_dataset(tmp_dir, verify=True) as ds2:
        print("\n重开数据集，manifest 校验通过。")

        early = PointInTimeView(ds2, "2026-01-05T18:30:00+08:00")
        later = PointInTimeView(ds2, "2026-01-06T15:00:00+08:00")
        now = PointInTimeView(ds2, "2026-01-12T10:00:00+08:00")

        print("\n--- 同一业务日，不同收到时点看到的 1/5 收盘 ---")
        show("1/5 18:30 视角(初值)", early.bar(FIGI, "2026-01-05"))
        show("1/6 15:00 视角(已更正)", later.bar(FIGI, "2026-01-05"))

        print("\n--- 停牌：通知送达前后 ---")
        show("1/7 在 13:00 视角", PointInTimeView(ds2, "2026-01-07T13:00:00+08:00")
             .bar(FIGI, "2026-01-07"))
        b = later.bar(FIGI, "2026-01-07")
        print("1/7 次日视角:", "<无bar，已知停牌>" if b is None else b)

        print("\n--- 迟到补数：1/6 成交量 ---")
        evening = PointInTimeView(ds2, "2026-01-06T19:00:00+08:00")
        show("1/6 当晚视角(缺量)", evening.bar(FIGI, "2026-01-06"))
        show("1/12 视角", now.bar(FIGI, "2026-01-06"))

        print("\n--- 复权：1/5 旧价在拆股公告前后 ---")
        pre = PointInTimeView(ds2, "2026-01-06T09:30:00+08:00")
        post = PointInTimeView(ds2, "2026-01-06T12:00:00+08:00")
        show("公告送达前", pre.bar(FIGI, "2026-01-05"))
        show("公告送达后", post.bar(FIGI, "2026-01-05"))

        print("\n--- 合并查询（1/5~1/8，1/6 15:00 视角）--")
        for v in ds2.quotes.merged_query(FIGI, "2026-01-05", "2026-01-08",
                                         as_of="2026-01-09T12:00:00+08:00"):
            print(f"  {v.trade_date} close={v.close} vol={v.volume}"
                  f" 修订={v.is_correction} 来源={v.source}")


if __name__ == "__main__":
    main()
