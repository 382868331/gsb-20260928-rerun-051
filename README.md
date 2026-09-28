# 历史行情研究系统（首轮）

纯 Python（标准库 `sqlite3`，离线、不联网下载行情）实现的双时态历史行情
研究底座。本轮先建立 **证券身份、交易日历、数据版本** 三件事，保证：

- 行情修订采用**追加版本**，旧值永不覆盖，可重复读取任意历史时点；
- 回测只能选择“当时实际知道的数据”，不会误用后来补齐/更正的价格；
- 原始行情与修订记录可合并查询，并能区分两个时间轴；
- 数据集保存后重开，查询结果与数据来源完全一致（哈希校验）。

## 双时态模型

每条行情版本同时挂在两个时间轴上：

| 时间轴 | 字段 | 含义 |
| --- | --- | --- |
| 业务时间 | `trade_date` | 行情实际发生的交易日 |
| 收到时间 | `received_at` / `superseded_at` | 数据送达本系统的时间 / 被后到版本取代的时间 |

历史时点查询（as-of）只选满足以下条件的版本，因此天然看不到“未来修订”：

```sql
received_at <= :as_of
AND (superseded_at IS NULL OR superseded_at > :as_of)
```

## 模块

- `market_research/instruments.py` — 证券身份（figi 主键）、曾用代码别名、代码改名不改身份。
- `market_research/calendar.py` — 按交易所维护交易日历（周末/节假日、前后交易日）。
- `market_research/quotes.py` — 双时态行情：写入（插入/重复/修订）、as-of 查询、版本链、合并查询。
- `market_research/events.py` — 拆股（复权因子同样按收到时间可见性过滤）与停牌/复牌版本。
- `market_research/backtest.py` — `PointInTimeView`：绑定研究时点的回测只读视图，停牌日不出 bar。
- `market_research/dataset.py` — 数据集建库、保存（`manifest.json` 指纹）、重开校验。

### 写入三态

- `inserted`：该业务日首次出现；
- `duplicate`：来源记录号相同或字段完全一致（幂等，重复文件重放不产生新版本）；
- `revised`：字段有变化，旧版本置 `superseded_at`，追加新版本。

## 快速体验

```bash
.venv/bin/python examples/demo.py       # 端到端虚构样例
.venv/bin/python -m pytest -q          # 9 个行为测试
```

最小用法：

```python
from market_research.dataset import MarketDataset, open_dataset
from market_research.backtest import PointInTimeView

with MarketDataset("data/my_ds") as ds:
    ds.quotes.upsert("BBG000FAKE001", "2026-01-05", close=10.0,
                     source="盘后初报", received_at="2026-01-05T18:05+08:00")
    ds.quotes.upsert("BBG000FAKE001", "2026-01-05", close=10.2,
                     source="次日更正", received_at="2026-01-06T09:00+08:00")
    ds.save()

with open_dataset("data/my_ds") as ds2:                    # 自动校验指纹
    view = PointInTimeView(ds2, "2026-01-05T19:00+08:00")
    assert view.bar("BBG000FAKE001", "2026-01-05").raw_close == 10.0
```

## 虚构样例覆盖的场景（见 `examples/sample_data.py`）

- **拆股**：1 拆 2，除权日 2026-01-08；公告送达前回测不复权，送达后旧价 ÷2。
- **停牌**：2026-01-07 盘中临停，通知 13:05 才送达；13:00 的视角不知道停牌。
- **重复记录**：同一盘后文件重放，识别为 `duplicate`，版本链不膨胀。
- **行情修订**：收盘价 10.00 → 10.20，版本链保留两条，来源可区分初报/更正单。
- **迟到补数**：1/6 成交量 1/12 才补齐；1/6 当晚的回测视角仍是缺失。
- **保存重开**：重开后 manifest 哈希一致，合并查询结果与来源逐条相同；库被改动会校验失败。

样例证券（`600FAK` 云岚科技、figi `BBG000FAKE001`）与全部行情均为虚构。

## 数据集布局

```
my_dataset/
  data.mrdb       # SQLite，全部双时态数据
  manifest.json   # dataset_id、schema_version、每表行数与 sha256 指纹
```
