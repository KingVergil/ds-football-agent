# 因子生产/消费链路梳理与激活计划（2026-08-05）

## 执行状态（2026-08-05 全部落地）

| 阶段 | 内容 | 状态 | commit |
|---|---|---|---|
| 1 | join 加固（fac_id/slugs 冗余 + 注册表优先 fac_id + 迁移回填 586 条，47% 带 slugs） | ✅ | e55f022 |
| 2 | 激活 per-match 因子 slugs → 数据段（node_build_prompt 传 active 因子） | ✅ | 912b80f |
| 3 | 因子归纳独立步骤（alpha 跨狗 1 次 / 非 alpha 各自；bit 距离≤2+名字下限候选；LLM 判重；同名确定性合并；孤儿补定义；record 只记不判） | ✅ | 57cbf2f |
| 4 | factor_review 低信息退役预筛（n≥5 且 \|avg\|<0.15 且命中率 0.35–0.65） | ✅ | b26cbb0 |
| 5 | reflect prompt 引导 key_slugs（18 个合法 slug 白名单，依赖非默认段才上报） | ✅ | fd55463 |

首次归纳实跑（alpha 池）：17 个跨角色同名合并 + 7 个 LLM 合并（1 对方向相反正确保留）+ 2 个孤儿补定义。审计 `scripts/factor_induction_audit.jsonl`。

**待办**：① ✅ 已接 batch（`./batch_agents.sh factor-induction`，**已写入 ds-agents-launch 日常触发 skill：结算后自动跑 `--limit 30`**）；② ✅ 全量 init 已跑（2026-08-05：alpha 跨狗 1 次 + 非 alpha 4 狗各自，5 scope 共 138 次 LLM 判重，71 合并 / 75 保留方向相反，因子条目 586→498，补 fac 定义 4 个）；③ 退役阈值跑一轮 factor_review 后精调；④ 串关狗按同方案接入。

## 一、现状架构（代码事实，2026-08-05 核对）

### 生产者

**只有 1 个真正的生产者：每日结算反思（settle/reflect）**

位置：`src/agent.py` 结算流程

1. LLM 对已结算订单反思 → 输出 `alpha_factors` + `key_slugs`
2. 按订单归因：`FactorMemory.record()` 写 `roles/<狗>/memory/factor_memory.json`（**统计+历史，不含 slugs**）
3. 新因子去重（轻量名字包含判断）→ `save_factor()` 写 `lota_data/factors/fac_<name>.json`（**含 slugs**，白名单校验 17 个合法 slug）
4. `record()` 内对新名字还会调 `_consolidate_candidate()`（LLM 判重：create/merge/suppress + 确定性兜底）——这是**内嵌的、零散的"归纳"**，每出现一个新因子名就调一次 LLM

### 消费者

1. **analyze prompt**（7 狗，alpha 狗读跨狗注册表）：
   - 跨狗注册表：`factor_registry.py` 聚合所有角色的 factor_memory，按名字 join `fac_*.json` 取 slugs/描述
   - 角色自身因子段：`perf_text()`（统计）+ `factor_desc_text()`（按名字 join fac 定义展示 `[slugs: ...]`）
2. **factor_review（因子退役）**：消费 factor_memory + 近 7 天反思 → 退役/休眠。**不生产新因子**
3. **分析决策本身**：LLM 看到因子名+命中率+（若 join 成功）slugs，用于下单判断

### 隐藏的"归纳"逻辑（散落四处，非独立步骤）

| 位置 | 做什么 | 状态 |
|---|---|---|
| `memory.py _consolidate_candidate` | 单因子名 LLM 判重（merge/create/suppress） | 每日 record 时内嵌调用 |
| `agent.py` 结算 dedup | 名字包含/相等判断 | 每日运行 |
| `scripts/factor_dedup.py` | 全库 LLM 判重原型（as-if 回放） | 手工脚本，未接入流程 |
| `scripts/fix_factor_stats.py` | 名字清洗 + 半盘口径迁移（本次修复） | 一次性迁移 |

## 二、对架构理解的修正

> 用户假设：生产者 2 个（每日复盘 + 因子退役），因子归纳是隐藏消费者，应独立成步骤，负责对每日因子做"kmeans"，同时它也是消费者之一。

**结论：大方向正确，一个细节要修正。**

- ✅ 因子归纳（统一去重/聚类/补 slug）确实应该独立成步骤——现在它是散在 record() 里的内嵌 LLM 判重，不可调度、不可审计、每天重复调 LLM
- ✅ 归纳步骤是消费者（消费每日因子流）——同时它维护的因子库又是 analyze 的输入，所以它处在"生产 → 归纳 → 消费"的中间枢纽
- ❌ **因子退役不是生产者**。代码里 `factor_review` 只做退役/休眠（消费者），不产出新因子。真正的生产者只有一个：每日结算反思
- ⚠️ 你提的"kmeans"语义成立：归纳要做的是把每日涌现的因子名做**语义聚类**（方向相反必须分簇，同模式合并，簇中心命名/归并），这正是现有 `_consolidate_candidate` 的规则集，只是缺一个统一的调度外壳

## 三、现状量化（决定方案的关键数据）

1. **名字 join 命中率只有 47%**：全角色 586 条 factor_perf，仅 275 条能匹配到 fac 定义；554 个 fac 定义、537 个唯一因子名。一半因子在 prompt 里只有名字+命中率，没有 slugs/描述
2. **fac 定义里的 slugs 全部落在 7 个默认段内**（asian-handicap-pinnacle/fair-odds/eu-odds-pinnacle/discrete-odds/betfair-buysell/over-under-crown/match-head），**没有用到另外 10 个合法段**（asian-crown/macau、betfair-eu、home/away-recent、lineup、match-history、rank-info、goal/score-bonus、over-under-macau）
3. 含义：**现在直接激活"按因子 slugs 动态取数据段"，对 prompt 零影响**（并集还是那 7 个默认段）。要让激活真正改变数据段，必须让归纳/反思产出非默认 slugs（见阶段 4）

## 四、激活计划（分阶段）

### 阶段 1：加固因子↔slugs join（低成本，先做）

- `FactorMemory.record/merge`：条目里冗余存 `fac_id`（和 aliases 一起）；merge 时把目标因子的 fac_id 继承过来
- `factor_registry.py` / `memory.py factor_desc_text`：优先用条目里的 `fac_id`，匹配不到再退回名字 join
- 迁移脚本：为现有 586 条回填 fac_id（名字匹配的 275 条直接填；其余 261 条孤儿交给阶段 3 归纳处理）
- 验收：注册表/定义段覆盖率 47% → 接近 100%（孤儿先不算）

### 阶段 2：激活 per-match factor slugs → 数据段

- `node_build_prompt`：把该角色当前 active 因子的 slugs（经阶段 1 解析）合并进每个比赛的 sections（`PromptBuilder` 已支持 `factors[].slugs` 追加，目前没人传）
- 兼容：并集仍以 default_slugs 为底，因子 slugs 只增不减
- token 影响：阶段 4 之前零增长（见量化第 2 点）；之后按新增段数线性增长，单场每加一个段约 +300~600 token，需在 TOKENS_PER_MATCH 预算内

### 阶段 3：因子归纳独立步骤（核心，新模块）

新增 `src/factor_induction.py`（CLI：`python dsfootball_cli.py factor-induction <end_date>`）：

1. **输入**：所有角色 factor_memory + fac 定义 + 窗口内反思（默认近 7 天）
2. **清洗**：复用 `fix_factor_stats.clean_name`（去引号/emoji/后缀）
3. **候选筛选（已定：不做 kmeans，用 bit 距离）**：因子→slugs 是天然 one-hot 向量，**bit 距离（对称差）≤1–2 的因子对**进入 LLM 合并候选；方向相反（上盘 vs 下盘等）强制分簇，不合并
4. **LLM 判重**：候选对用 `_consolidate_candidate` 的 JUDGE 规则（同模式 merge、方向相反 create、retired suppress）→ 簇中心命名、合并 aliases
5. **补定义**：孤儿因子（无 fac 定义）按反思 key_slugs 历史生成/修复 fac 文件；允许把 slug 扩展到非默认段（前提：历史反思/数据支持）
6. **写回**：factor_memory（合并统计、aliases、fac_id）+ fac 定义
7. **职责归位**：阶段 3 落地后，`record()` 里的内嵌 `_consolidate_candidate` 关掉（只记不判），避免每天重复调 LLM

**调度（已定）**：每日 settle 后跑。**alpha 因子无论多少只狗，1 次归纳统一进全库**；**非 alpha 因子各自归纳**（per-role）。

⚠️ bit 距离预筛依赖 slugs 完整：阶段 1 之前 53% 因子无 fac 定义（空 slug 集合），距离会全为 0 退化成全量候选——**阶段 1 必须先行**。

### 阶段 4：因子退役标准改造（独立于归纳，已定）

**退役不并入归纳**；`factor_review` 保持独立步骤，但判定标准从"命中率/亏损"改为**信息量**：

- **要退役**：既不赚钱也不亏大钱、来回小幅震荡的"废物"因子——低净收益 + 低波动
- **要保留**：波动大的因子（无论当前盈亏）——有信息，可能是待细化的真模式

**指标草案（阈值可调）**，基于 history 的 `return_ratio`：
```
retire 当且仅当:
  n >= 5
  |avg_return_ratio| < 0.15        # 每单平均回报≈0（不赚钱也不亏大钱）
  0.35 <= hit_rate <= 0.65         # 来来回回（≈掷硬币）
```
实现：在 `node_factor_review` 的 LLM 评估前加确定性预筛（类似现有 auto_dormant），低信息因子直接 retire；LLM 只评估剩余候选。

**实测校准（2026-08-05，全角色活跃因子 n≥5 共 62 个）**：
- swing（max-min return_ratio）不是判别轴——胜 +0.8~1.1 / 负 -1.0 的上界让几乎所有因子 swing≈2.0
- 判别轴是每单平均回报 `|avg_return_ratio|`：阈值 0.15 命中 4 个（约 6%），0.20 命中 6 个
- 边界注意：`离散凝聚深盘顺向`（alpha狗 0.171/22单）在 0.20 阈值边缘，这类跨狗关键因子要保留，最终阈值以"别误杀主因子"为准

### 阶段 5：引导反思产出非默认 slugs（可选增强，让激活真正生效）

- reflect prompt 里列出 17 个合法 slug 白名单，要求 LLM 在因子确实依赖某段时上报非默认段（如亚盘澳门/皇冠背离、近期状态、首发等）
- 配合阶段 2：这些段才会真正进比赛 prompt
- 风险：prompt 变长、token 变多；每场新增段数需做预算控制（TOKENS_PER_MATCH=3000 不变，超出截断）

## 五、依赖与顺序

```
阶段1(join加固) → 阶段2(激活) → 阶段3(归纳步骤) → 阶段4(退役标准) → 阶段5(丰富slugs)
```

- 阶段 1、2 改动小、低风险，可先落地验证
- 阶段 3 是架构级改动，建议单独排期；它依赖阶段 1（否则归纳写的 fac_id 无处消费）
- 阶段 2 单独做是"结构性激活"（无可见 prompt 变化），配合阶段 5 才有实际数据段变化
- 阶段 4 与阶段 3 并行（各自独立），可与阶段 1、2 一起排
- 串关狗接入排在本轮之后（方案见"六、已定决策"第 4 条）

## 六、已定决策（2026-08-05）

1. **归纳调度**：每日 settle 后；alpha 因子 1 次归纳进全库，非 alpha 因子各自归纳
2. **合并候选策略**：不做 kmeans，用 one-hot slug 的 bit 距离（对称差 ≤1–2）做候选预筛，LLM 判合不判全
3. **退役标准**：不并入归纳；低信息因子（不赚不亏 + 小幅震荡）退役，波动大的保留
4. **串关狗接入方案**（本轮不动）：
   - **当前不启用 alpha 模式**：analyze 不注入跨狗因子注册表/7 狗倾向，保持现有人设 + 竞彩赔率选腿
   - **不独立生产因子**：按腿归因 = 单场方向逻辑，与单关因子重复；串关价值在组合（票型/容错/仓位），不在因子类型
   - **将来接入 = 纯消费**：复用 7 狗归纳好的因子库做腿筛选，腿方向以触发因子为准
   - **腿门槛**：因子**整体命中率**（factor_perf total/hit）≥65% 且样本 ≥5 才允许当腿（先按整体，不细分窗口/收缩口径）
   - 进全库时加入 factor_induction 的 `ALL_ROLES`（消费端，不占 alpha 池）

## 七、待定小问题（实现时定，不影响排期）

1. 阶段 3 合并后统计是否严格按 aliases 累加、簇中心名字 LLM 定还是保留最老名字
2. 阶段 4 阈值（0.10 / 0.80）先用草案，跑一轮看分布再调

## 八、废弃/遗留目录清单（已核实）

| 目录 | 状态 | 说明 |
|---|---|---|
| `lota_data/orders` | ✅ 已删（2026-08-05，commit 940521f） | 空目录；`data_manager.get_orders` 读它但永远返回空 |
| `lota_data/predicts` | ✅ 已删（同上） | 空目录；`data_manager.get_predictions` 读它但永远返回空 |
| `lota_data/recipes` | ✅ 已删（同上） | 空目录；全代码零引用 |
| `lota_data/agent_memory` | 🏷 标记废弃（2026-08-05） | 空目录；仅 memory.py 的"兼容旧代码"fallback（无 role_name 时）和 CLI 调试 `prompt_builder build --memory` 会用到；live 流程一律走 `roles/<狗>/memory`，可删除 |

归档：`src/order_log.py`、`src/backtest.py` → `scripts/legacy/`（git mv，保留原文）
