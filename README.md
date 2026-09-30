# 历史行情研究系统（双时态行情研究）

纯 Python 标准库 + SQLite，离线运行，不联网下载任何行情。核心解决一个问题：
**回测站在某个历史时点，只能看到当时实际已经收到的数据；后来补齐的价格、迟到的修订、事后才公告的拆分因子都不能被提前使用。**

## 双时态模型

- **业务时间（business_date）**：行情实际所属的交易日。
- **收到时间（received_at）**：数据实际到达本地的时刻。
- 查询接口 `quote_at(sec, business_date, known_at)` 重放
  `received_at <= known_at` 的全部事件并裁决。明天才到的修订、撤回、拆分公告或
  来源优先级调整，今天都看不到。
- 行情以**不可变事件**追加保存（`raw` / `revision` / `cancel` /
  `suspension` / `retraction`），永不就地覆盖；同一业务日在不同 `known_at`
  下可重放当时看到的不同数字。

## 跨来源冲突裁决与撤回

- `SourceInfo(source_id, name, priority)` 登记来源；`priority` 越大越权威。
- 同一证券、同一业务日、同一 `known_at` 存在多个未失效来源的不同 `Quote`
  时，系统不会覆盖任何原记录：`QuoteView.candidates` 保留各来源原记录，
  `decision_reason` 说明为何采用中选记录，`conflict=True` 标明价格冲突。
- 裁决顺序为：截至该视角已知的来源优先级（高者胜）→ `received_at`（晚者胜）
  → 存储送达序号。未知来源默认优先级 0。
- 来源优先级调整用 `add_source(..., valid_from=...)` 保存为新事实；旧优先级
  仍用于早期视角，后来改判不会抹掉或修饰早期研究版本。
- `retraction` 可撤回指定来源记录，或用 `target_record="*"` 撤回该来源当日
  全部主张；撤回本身也有 `received_at`，送达前旧价格仍可见、可用。
- 撤回后又发布的同来源修订是新候选；被撤回、被修订取代的记录都保留在
  `candidates` 中，状态分别为 `retracted` / `superseded`。

## 模块

- `histquote/identity.py`：证券身份 `Security`；拆分 `Split`（除权日 + 因子 + 收到时间）。
- `histquote/source.py`：来源及权威级别 `SourceInfo`。
- `histquote/calendar.py`：交易日历，区分开市日与非交易日。
- `histquote/quote.py`：`Quote`、`QuoteEvent`、`QuoteStatus`。
- `histquote/dataset.py`：SQLite 存储、事件写入、as-of 合并查询、保存/重开、数据指纹。
- `histquote/query.py`：查询结果 `QuoteView`（状态、复权收盘、中选来源、裁决原因、全部候选）。
- `examples/demo.py`：虚构样例演示冲突、撤回、修订与拆分交错。
- `tests/test_bitemporal.py`：9 个首轮语义测试。
- `tests/test_cross_source.py`：10 个跨来源裁决、撤回、改判与归因测试。

## 状态语义（回测必须区分）

- `ok`：截至该时点已知有效行情。
- `missing`：开市日但尚未收到数据（**不能当成收盘价，更不能用未来补齐值**）。
- `suspended`：当日停牌，已收到停牌事件。
- `cancelled`：记录被发布方撤销（如错挂代码）。
- 当所有已知价格主张都被撤回/撤销时为 `cancelled`；从未收到任何记录才是
  `missing`，两者不可混淆。
- `non_session`：非交易日。

## 重复记录处理

- 同一 `(证券, 业务日, 来源, 来源记录号)` 重放且内容一致：幂等忽略。
- 来源记录号相同但内容不同：抛 `ValueError`，绝不静默覆盖。

## 复权

`adjusted_close = close / 因子积`，因子只统计「除权日晚于该业务日」且
「公告收到时间 <= known_at」的拆分。公告送达前，回测看不到复权调整。

## 多视角复算与差异归因

```python
old = ds.run_research("s1", date(2024, 1, 8), date(2024, 1, 12),
                      datetime(2024, 1, 9, 9))
change = ds.explain_change("s1", date(2024, 1, 8), date(2024, 1, 12),
                           datetime(2024, 1, 9, 9), datetime(2024, 1, 13, 9))
new = change["new_research"]
change["content_changed"]          # 记录数量不变但裁决内容变了也为 True
change["new_quote_events"]         # 窗内新增修订/停牌/撤回
change["new_split_announcements"]  # 窗内新增拆分公告
change["source_priority_changes"]  # 窗内来源权威级别调整
change["views_diff"]               # 每个业务日裁决前后的差异
ds.recompute_research(old, datetime(2024, 1, 13, 9))  # 同窗口新视角复算
```

## 快速开始

```bash
.venv/bin/python -m examples.demo
.venv/bin/python -m unittest discover -s tests
```

```python
from histquote import Dataset, Security, Quote, QuoteEvent
import datetime as dt

ds = Dataset("research.db")
ds.add_security(Security("s1", "000001", "样例"))
ds.calendar.add_sessions([dt.date(2024, 1, 8)])
ds.add_event(QuoteEvent(
    "s1", dt.date(2024, 1, 8), dt.datetime(2024, 1, 8, 15, 30),
    kind="raw", quote=Quote(10, 10.2, 9.9, 10.1, 100),
    source="feed", source_record="r1"))

# 修订（次日才收到）
ds.add_event(QuoteEvent(
    "s1", dt.date(2024, 1, 8), dt.datetime(2024, 1, 9, 18),
    kind="revision", quote=Quote(10, 10.2, 9.8, 9.8, 100),
    source="corr", source_record="r2"))

ds.quote_at("s1", dt.date(2024, 1, 8), dt.datetime(2024, 1, 9, 9)).quote.close  # 10.1
ds.quote_at("s1", dt.date(2024, 1, 8), dt.datetime(2024, 1, 10)).quote.close   # 9.8

ds.series("s1", dt.date(2024, 1, 8), dt.date(2024, 1, 12),
          known_at=dt.datetime(2024, 1, 10))          # 按业务时间回放
ds.received_log("s1", dt.datetime(2024, 1, 8),
                dt.datetime(2024, 1, 10))              # 按收到时间追踪
ds.save_as("snapshot/research.db")                    # 保存
ds.snapshot_info()                                     # 指纹：重开后应一致
```
