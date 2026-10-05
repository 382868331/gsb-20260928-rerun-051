# 历史行情研究系统（首轮）

> 第二轮新增：**跨来源冲突裁决**与**来源撤回**，并支持在多个历史视角复算、
> 逐字段归因结果差异（`examples/demo_conflict.py`，`tests/test_conflict_withdrawal.py`）。
>
> 第三轮新增：**来源优先级规则的历史有效范围**与**规则调整前后的研究对照**
> （`examples/demo_priority_rules.py`，`tests/test_priority_rules.py`）。
>
> 第四轮新增：**证券身份公告、显示代码复用/跨市场转板、映射撤回、组合身份调整对照**
> （`examples/demo_identity.py`，`tests/test_identity_portfolio.py`）。
>
> 第五轮新增：**拆股、换股合并、现金补偿的版本化公司行动账本**
> （`tests/test_corporate_actions.py`）。
>
> 第六轮新增：**公司行动组合的份额级持仓约束研究**，支持同批原始份额在
> 换股、保留旧证券、等待身份核实之间互斥分配；允许撤回未定选择并释放份额，
> 阻断循环引用和超额采用，并比较不同采用顺序的数量、成本与现金守恒
> （`tests/test_action_research.py`）。

纯 Python 标准库 + SQLite，离线运行，不联网下载任何行情。核心解决一个问题：
**回测站在某个历史时点，只能看到当时实际已经收到的数据；后来补齐的价格、迟到的修订、事后才公告的拆分因子都不能被提前使用。**

## 双时态模型

- **业务时间（business_date）**：行情实际所属的交易日。
- **收到时间（received_at）**：数据实际到达本地的时刻。
- 查询接口 `quote_at(sec, business_date, known_at)` 只取
  `received_at <= known_at` 的最后一条事件。明天才到的修订，今天看不到。
- 行情以**不可变事件**追加保存（`raw` / `revision` / `cancel` / `suspension`），
  永不就地覆盖，因此同一业务日在不同 `known_at` 下可以重放出当时看到的不同数字。

## 模块

- `histquote/identity.py`：证券身份 `Security`；拆分 `Split`（除权日 + 因子 + 收到时间）。
- `histquote/identity.py`：`IdentityAnnouncement`（确认/候选/改名/撤回）与
  `IdentityResolution`（查询采用的身份、公告和来源规则）。
- `histquote/identity.py`：`CorporateAction`（预告/修订/撤回、拆股/换股、现金补偿、
  零碎份额去向）与 `HoldingLot`（逐笔持仓、数量和成本）。
- `histquote/calendar.py`：交易日历，区分开市日与非交易日。
- `histquote/quote.py`：`Quote`、稳定证券事件 `QuoteEvent`、显示代码事件
  `CodeQuoteEvent`、`QuoteStatus`。
- `histquote/dataset.py`：SQLite 存储、事件写入、as-of 合并查询、保存/重开、数据指纹。
- `histquote/query.py`：查询结果 `QuoteView`（含状态、复权收盘、来源、来源记录号）。
- `examples/demo.py`：虚构样例演示。
- `tests/test_bitemporal.py`：基础双时态语义测试。

## 状态语义（回测必须区分）

- `ok`：截至该时点已知有效行情。
- `missing`：开市日但尚未收到数据（**不能当成收盘价，更不能用未来补齐值**）。
- `suspended`：当日停牌，已收到停牌事件。
- `cancelled`：记录被发布方撤销（如错挂代码）。
- `non_session`：非交易日。

## 重复记录处理

- 同一 `(证券, 业务日, 来源, 来源记录号)` 重放且内容一致：幂等忽略。
- 来源记录号相同但内容不同：抛 `ValueError`，绝不静默覆盖。

## 复权

`adjusted_close = close / 因子积`，因子只统计「除权日晚于该业务日」且
「公告收到时间 <= known_at」的拆分。公告送达前，回测看不到复权调整。

## 跨来源冲突裁决

- 同一 (证券, 业务日) 可同时存在多个来源的报价；每条来源原记录都作为
  **候选**（`QuoteView.candidates`）永久保留，绝不因裁决而删除。
- 无裁决时按 `set_source_priority({"A":1,"B":2})` 的来源优先级决胜，
  `selection_basis` 标明 single-source / default-priority / revision / adjudicated。
- 裁决用 `Adjudication` 追加：只引用“采用哪条来源记录”并写明原因；
  裁决自身有 decision_received_at，在该时点之前的历史视角看不到它。
- 裁决是 append-only：后来改判（新 Adjudication）不会抹掉旧版本，
  用旧 known_at 复算仍能看到当时的结论与所依据的裁决号。
- 已被有效裁决锁定的记录，不会被之后到达的新原始报价自动翻转。

## 来源撤回

- kind="withdrawal" 事件通过 target_source / target_record **精确撤回**
  某条来源记录，不影响其他来源；撤回同样受收到时间约束。
- 被撤回记录仍出现在候选列表中，active=False、withdrawn_by 指向撤回通知。
- 所有候选都被撤回时状态为 withdrawn；撤回通知到达前，旧结论照常可复算。

## 多历史视角复算与差异归因

- `recompute_viewpoints(sec, date, [known_at, ...])` 在多个历史视角重放同一业务日。
- `explain_viewpoint_diff(sec, date, earlier, later)` 返回：
  - changed_fields：逐字段的 before/after（状态、价格、复权、采用记录、
    依据、裁决号、活跃候选集合）；
  - new_information：区间内实际到达的行情事件 / 撤回 / 裁决 / 拆分公告。
- 因此即使记录数量不变（仅裁决改选、或仅拆分公告送达导致复权值变化），
  也能区分内容变化，并指认它具体来自哪一次新增信息。

## 来源优先级规则的历史有效范围

- `add_priority_rule(PriorityRule(priority, received_at, reason, rule_id))`
  追加一条优先级修订，自 `received_at` 起生效；规则 append-only，
  之前的历史视角仍按当时已生效的规则裁决，**今天的偏好不会改写过去
  已经能看到的结果**。未登记规则时沿用 `set_source_priority` 的内存默认配置。
- `rollback_source_priority(rule_id, received_at, ...)` 回退到旧配置，
  但回退本身是一次**保留身份的新修订**（新 rule_id + supersedes 链），
  旧规则原样保留；规则随库保存，重开与重复查询后各研究版本不变。
- `compare_rule_adjustment(sec, date, known_at, rules_as_of=None)` 输出
  规则调整前后的研究对照：`original` 保持原历史视角，`revised` 按新规则
  重新研究同一时点（数据可见性不变），并给出逐字段 `changed_fields`。
- `information_timeline(sec, date)` 按收到时间列出价格、撤回、修订、
  拆分公告与优先级规则修订，各自何时可见一目了然。
- `QuoteView.priority_rule_id` 标明本次裁决实际采用的规则。

## 证券身份、代码复用与跨市场转板

- 稳定键始终是 `security_id`；显示代码、市场和名称只作为带业务有效期的公告事实。
- `add_identity_announcement()` 只追加 `confirm` / `candidate` / `rename` /
  `withdraw` 公告；`withdraw` 精确引用旧公告 ID，不删除原公告。
- `quote_for_code(code, business_date, known_at, market=...)` 同时按业务日期和
  收到时间解释身份：迟到公告不能提前使用，映射撤回后旧视角仍能复算。
- 资料不足返回 `identity_candidate`，没有公告返回 `identity_unknown`，多份确认冲突
  返回 `identity_ambiguous`；这些状态下报价只保留为候选，不会自动接到任何证券上。
- `CodeQuoteEvent` 保存按外部代码收到的原报价；只有唯一 `confirm/rename` 身份
  才路由到对应 `security_id`，旧代码迟到报价不会和新证券价格接成一条历史。
- `QuoteView` 会返回采用的 `security_id`、名称、公告 ID、撤回公告 ID、候选身份和
  来源规则；原代码报价记录始终保留。

## 组合研究与身份调整对照

- `evaluate_portfolio()` 按显示代码或稳定 ID 解释持仓；同一 `security_id` 的多笔
  新旧持仓只裁决和纳入一次行情，重复持仓会在 `duplicate_positions` 中说明，
  权重仍可聚合。
- `compare_identity_adjustment()` 固定行情可见时点或另给修订后视角，对照身份映射
  调整前后：
  - `true-price-change`：同一稳定证券的价格或来源记录变化；
  - `security-scope-change`：证券身份新增、移除或指向变化；
  - `name-only-change`：稳定证券不变，仅改名公告/身份解释变化。
- `save_portfolio_research()` 保存持仓、研究时点、`identity_as_of` 和当时结果；
  后续代码重分配、公告迟到或撤回后，`reopen_saved_research()` 仍按当时身份解释。

## 公司行动与持仓调整

- `add_corporate_action()` 只追加 `preview` / `revision` / `withdrawal`；同一行动族用
  `family_id` 串联，撤回精确指向 `target_action_id`，不覆盖预告或旧修订。
- `apply_corporate_actions()` 以 `business_date` 和 `known_at` 重放当时可见资料；
  预告默认不采用，显式选择或 `include_previews=True` 时可用于方案测算。
- `compare_corporate_action_scenarios()` 可继续采用旧公告，也可改用修订公告，逐笔列出
  输入数量、新证券数量、成本分配、现金补偿、零碎份额和未确定部分。
- 拆股保持同一 `security_id`，仅新增调整批次；换股合并才产生新 `security_id`。
  显示代码变化本身不是数量变化，换股后的新旧价格跳变不记为投资收益。
- 现金补偿和零碎股份现金分别列示；`cash_cost_portion` 指出现金承担的原成本，
  剩余成本分配给新证券。零碎份额作价或去向未知时进入 `undetermined`。
- 新证券身份未确认时保留 `candidate_security_ids`，不自动猜测为任何现有证券。
- 部分采用按批次扣减，剩余原证券和取得证券分开保存；同一来源份额不会同时留在
  两个证券中。连续公司行动沿 `lineage` 串联，原 `HoldingLot`、报价和行动均不覆盖。
- `save_corporate_action_research()` 保存研究视角、采用决策和调整结果；行动后来修订
  或撤回后，`reopen_corporate_action_research()` 仍按保存时的可见公告重放，并用
  `corporate_action_adjustments()` 逐笔追溯来源行动。

## 份额级持仓约束研究

- CorporateActionPortfolioResearch 以 HoldingLot 为最小账本单元：既有新证券与
  换股所得新证券保留独立批次号、来源、成本和血缘，不会混入同一来源。
- AllocationDecision 明确每份原始份额的去向：exchange 采用换股、retain
  保留旧证券、wait 等待新证券身份核实；同一批次去向数量合计超额时立即拒绝。
- 前一行动产生的新批次可由后一行动继续采用，例如 original:exchange-1:exchange-2；
  若新证券会沿血缘回到该批次已持有证券，明确报出循环引用。
- wait 或 pending 选择会同时预留数量和成本，后续行动只能使用剩余份额；
  withdraw() 仅允许撤回尚未确认的选择并释放预留，已确认现金补偿仍保留为事实。
- compare_adoption_order_consistency() 使用同一批决策按多个可行行动顺序复算，
  比较持仓数量、逐批成本、零碎份额现金和补偿现金是否一致。
- apply_corporate_actions() 的 Dataset 重放路径同样区分实际持仓、可再分配份额与
  待核实预留；显式申请超过剩余份额时报错，不再静默截断。

## 快速开始

```bash
.venv/bin/python -m examples.demo
.venv/bin/python -m examples.demo_conflict
.venv/bin/python -m examples.demo_priority_rules
.venv/bin/python -m examples.demo_identity
.venv/bin/python -m unittest tests.test_corporate_actions
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
