# 工具划分与工作流设计（数据/业务分离 + 三工作工具组）

> 状态：设计稿 v1。后续按此设定修改回放模式（replay）与实际 runtime（工具可见性 / 因子 barrier）。
> 原则：**数据层 = 确定性助手**（无 LLM 决策，只做 取数/落库/TTL/资金），**业务层 = LLM 决策流**（分析/结算/因子）。
> 每个子流只暴露自己的工具组（visibility allowlist），从根上减少 tools 误调。

## 0. 总体分层

```
┌─ 业务层（LLM 决策）──────────────────────────────┐
│  分析流（编排）→ 并行分析子流（每狗）               │
│  结算流（编排）→ 并行结算子流（每狗）               │
│  因子流（时序敏感：非 alpha → barrier → alpha）    │
└───────────────┬────────────────────────────────┘
                │ 只通过助手工具
┌─ 数据层（确定性助手）─────────────────────────────┐
│  比赛数据助手  订单数据助手  角色数据助手            │
└────────────────────────────────────────────────┘
```

## 1. 数据层（确定性助手，无 LLM 决策）

### 1.1 比赛数据助手（match-data assistant）

1. **获取比赛数据**：单日 / 范围。live = 强制刷新（TTL / live-strict，拒绝旧赔率进 prompt）；历史 = 缓存优先，缺了才从 URL 拉一次。
2. **写到本地**：matches / features / tags 原子写盘（私有 `lota_fetcher.js` 单独分发，放插件目录）。
3. **管理 TTL**：负缓存桩（600s）→ 完场永久有效 → 已开赛锁定 → 未开赛 TTL（120s）→ live 刷新失败拒绝旧缓存。

工具：`ds_prepare_day` / `ds_prepare_range`（内部跑 fetcher / Python prefetch）+ `lota_matches` / `lota_match` / `lota_sections`（只读）。

### 1.2 订单数据助手（order-data assistant）

1. **计算赛果对应**：score → hit / profit / 走水 / 半赢半输（settle math，纯函数 `settleOrder`）。
2. **订单生命周期 + 资金状态**（补充你留空的第 2 点）：
   - 创建/落库：`submit_orders`（去重 `(lota_id, bet_type)` → 已开赛保护 → 资金折算 `scale=余额/全金额` → FundManager 硬约束 → 扣资金）
   - 回退：`refresh_orders`（窗口内未开赛退回金额并删除，已开赛保留）
   - 结算写回：`settleDog`（hit / profit / capital / 订单状态更新）
   - 查询：`ds_capital_js`（余额 / 锁定敞口 / 全金额 / 约束）

工具：`ds_capital_js` / `refresh_orders` / `submit_orders` / `ds_settle_js`（可再拆出纯函数 `settleOrder` 供测试与复用）。

### 1.3 角色数据助手（补充）

- 人设：`ds_persona_js`（persona.md 确定性注入上下文，禁止模型 read 文件）。
- 因子记忆 / 反思 / slug：`ds_memory_js`（分析子流读；因子流写）。
- 跨狗因子注册表：alpha 归纳读写的全库因子池。

## 2. 业务层

### 2.1 分析流（编排）

```
1. 管理比赛数据：ds_prepare_day / ds_prepare_range（数据先行）
2. 并行分析子流：fan-out，每狗独立 subagent（parallel ≤ 7）
3. 刷新 dashboard
```

**分析子流（单狗，顺序执行）**

```
1. 回退订单：refresh_orders(dog, day)
2. 比赛数据助手取指定范围数据：候选列表已注入（strip_scores），按需 lota_sections / lota_match
3. 获取对应因子：ds_memory_js(dog, day)（活跃因子 / 已证伪 / 历史反思 / 近期订单）
4. 获取对应人设：ds_persona_js(dog)
5. 创建订单：submit_orders(dog, day, orders)
```

子流可见工具：`refresh_orders` / `ds_memory_js` / `ds_persona_js` / `ds_capital_js` / `submit_orders` / `lota_sections` / `lota_match`。
禁止：结算 / 因子 / 回放 / 数据准备类（列表已注入，防止再拉全量）。

### 2.2 结算流（编排）

```
1. 管理比赛数据：确保比分缓存就绪（fetch_scores / 历史日 prepareRange 快照）
2. 并行结算子流：每狗独立 subagent（parallel ≤ 7）
3. 收尾：dashboard / 邮件（可选）
```

**结算子流（单狗）**

```
1. 结算订单：settleDog（只认 state==6 完场比分）
2. 写角色对应结算：orders 状态（hit/profit/score/settled_at）+ capital 更新
3.（补充）反思：ds_reflect_js —— 从已结算订单提炼新因子，写因子/反思记忆
   → 这是“其他因子出现”的主要来源，必须在因子流之前完成
```

子流可见工具：`ds_settle_js` / `ds_reflect_js` / `fetch_scores` / 比赛数据只读（比分）。
禁止：`submit_orders` / 因子归纳 / 因子退役 / 回放。

### 2.3 因子流（时序敏感）

> 用户硬约束：**alpha 模式必须在其他（非 alpha）因子出现之后再做因子操作；alpha 不能与非 alpha 并行。**
> 理由：alpha 跨狗统一归纳读的是“全库已出现的因子池”，非 alpha 因子先落库，alpha 归纳才有输入。

```
阶段 A（非 alpha，可并行）：
  各非 alpha 狗 ds_factor_induction(scope=狗名) —— 清洗/合并/判重，独立互不依赖

阶段 B（barrier，串行）：
  等阶段 A 全部完成 → ds_factor_induction(user='alpha') 跨狗统一归纳一次进全库
  （alpha2狗 / alpha狗 / 均注狗）

阶段 C（周期退役，barrier 同原则）：
  非 alpha 各狗 ds_factor_review_js 先行 → alpha 收尾
  支持 user_notes（用户调整意见）/ persona_overrides 注入评估方向

阶段 D（判重，内联）：
  ds_factor_dedup 随归纳/反思内联执行，不单独并行
```

工具：`ds_factor_induction` / `ds_factor_dedup` / `ds_factor_review_js`。
禁止：下单 / 结算 / 数据准备。

## 3. 工具组映射（visibility）

| 组 | 工具 | 使用场景 |
|---|---|---|
| 数据组 | `ds_prepare_day` / `ds_prepare_range` / `lota_matches` / `lota_match` / `lota_sections` | 流前置准备 + 子流按需读 |
| 角色组 | `ds_persona_js` / `ds_memory_js` / `ds_capital_js` | 分析子流读取角色数据 |
| 分析组 | `refresh_orders` / `submit_orders` | 分析子流（下单生命周期） |
| 结算组 | `ds_settle_js` / `ds_reflect_js` / `fetch_scores` | 结算子流 |
| 因子组 | `ds_factor_induction` / `ds_factor_dedup` / `ds_factor_review_js` | 因子流（alpha barrier） |
| 编排组 | `ds_analyze_all_parallel` / `ds_settle_all`* / `ds_factor_flow`* / `ds_replay` | 流入口（父 agent 专用） |

`*` = 后续按本设计新增的流入口工具。

## 4. 对 replay / runtime 的后续影响（占位）

- **replay 日管线顺序**：数据准备（prepareRange）→ 分析流 → 结算流 → 因子流（A→B→C barrier）→ dashboard。
- **runtime**：每个子流 subagent 的 `toolFilter` 只放该组工具（deny 其余），减少跨组误调；流入口（编排组）只有父 agent 可见。
- **alpha barrier 实现**：阶段 B 必须显式 `await` 阶段 A 全部完成，禁止把 alpha 与非 alpha 归纳放进同一个 `Promise.all`。
- **已知差异**：当前 `replay.js` 的逐日归纳是 alpha 先、非 alpha 后（`inductAlpha` 在前），与新设计相反，后续按 阶段 A → B 顺序修正。
