# 梭哈2狗因子流 Review（2026-09-02）

> 结论先行：过去 7 天的因子流持续、稳定地制造「小样本近期热因子」，同时把真正有样本量的好因子休眠掉。昨天梭哈2狗 -3300 不是某一次判断失误，而是因子迭代闭环本身在奖励过拟合。

## 一、范围与数据来源

本次 review 覆盖 2026-08-26 ~ 2026-09-02 的梭哈2狗因子流，数据来自：

- 因子记忆：`python-engine/data/roles/梭哈2狗/memory/factor_memory.json`
- 每日快照：`python-engine/data/roles/梭哈2狗/history/*__pre-factor/`
- 归纳审计：`python-engine/scripts/factor_induction_audit.jsonl`
- 退役 session：`python-engine/data/sessions/梭哈2狗/2026-08-30T132538_factor_review_2026-08-30.md`
- 分析 / 结算 session：`python-engine/data/sessions/梭哈2狗/2026-09-0*`
- 核心代码：`python-engine/src/factor_select.py`、`memory.py`、`reflect_apply.py`、`factor_induction.py`、`agent.py`

## 二、因子迭代闭环

当前闭环分五步：

1. `analyze`：LLM 根据 prompt 里的因子下注。
2. `settle + reflect`：结算后 LLM 产出 factor JSON。
3. `reflect_apply`：确定性写回因子记忆，并创建新因子。
4. `factor-induction`：每日去重 / 合并。
5. `factor-review`：每周退役 / 休眠评估。

## 三、过去 7 天因子流回顾

### 1. 每日因子库状态变化

来自每日 `__pre-factor` 快照：

| 日期 | 总因子 | active | retired | dormant | 新增 active 因子 |
|---|---:|---:|---:|---:|---|
| 08-24 | 54 | 10 | 22 | 22 | 离散极端低资金方向（3单） |
| 08-25 | 59 | 15 | 22 | 22 | 低水受让保护、必发卖压逆主队、离散资金同向低水、离散资金背离深盘等 5 个 |
| 08-26 | 60 | 16 | 22 | 22 | 离散凝聚深盘顺资金（2单） |
| 08-27 | 62 | 18 | 22 | 22 | 公平盘偏差诱盘、必发资金反向 |
| 08-28 | 63 | 19 | 22 | 22 | 深盘资金反向诱下 |
| 08-29 | 65 | 21 | 22 | 22 | 离散低位客胜诱主、离散极值诱杀 |
| 08-30 | 68 | 19 | 24 | 25 | factor_review 触发，auto-dormant 24 个 |
| 08-31 | 68 | 19 | 24 | 25 | 离散极低资金同向 |
| 09-01 | 67 | 18 | 24 | 25 | 无新增 |

### 2. 归纳阶段实际行为

`factor_induction_audit.jsonl` 中梭哈2狗 08-26 ~ 09-02 的记录：

- 每天约 23~28 次 LLM 判重。
- 绝大多数是 `keep_separate`，即「方向相反 / 模式不同，保留两个」。
- 7 天只发生 2 次 `merge_llm`，而且都合并到了小样本热因子上：
  - 08-31：离散极值资金导向 → 离散极端低资金方向
  - 09-01：离散极低资金同向 → 离散极端低资金方向

归纳阶段只做去重 / 合并，不做统计显著性检验，也不做退役判断。

### 3. 退役评估实际行为

08-30 factor_review 结果：

- `retired`：仅「退盘诱小真大」1 个。
- `auto_dormant`：24 个，其中包括库里样本最足的两个：
  - 退盘离散凝聚上盘：50 单，37 赢 / 3.5 输 / 9.5 走水
  - 离散凝聚低水：45 单，34 赢 / 5.5 输 / 5.5 走水

这两个真正经过较多样本验证的因子，因为超过 14 天未触发，被折叠成「另有 25 个休眠因子」，狗在 prompt 里看不到。

### 4. 结论

过去 7 天，因子流在：

- 惩罚「样本足但最近没出现」的好因子；
- 奖励「样本极小但最近全红」的坏因子。

这是昨天梭哈2狗满仓打 6/6、2/2 因子的底层原因。

## 四、因子迭代过程的核心缺陷

### 缺陷 1：因子出生即 active，没有验证门槛

`reflect_apply.py` 中，reflect 只要在 `alpha_factors` 报出新名字，就立即写入因子库，`record()` 默认：

```python
"status": "active"
```

没有最小样本、置信区间、留出验证或复现次数门槛。

未归因到订单的新因子还会走 `factors.record(fn, None, 0, ...)`，`hit=None, profit=0` 会被记成 push，进一步污染统计池。

### 缺陷 2：狗看到的是原始命中率，不是收缩命中率

`factor_select.py` 已计算贝叶斯收缩：

```python
shrunk_rate = (hits + SHRINK_ALPHA) / (n + SHRINK_ALPHA + SHRINK_BETA)
```

但 `memory.py` 的 `perf_text()` 打印的是原始 `hits/n` 和 `w_return`：

```python
f"{fid} [近{p0['n']}单 命中{p0['hits']}/{p0['n']} 加权回报{p0['w_return']:+.2f}{small}]"
```

`shrunk_rate` 没有进狗自己的 prompt，只在跨狗注册表里展示。

### 缺陷 3：「近 N 单」对年轻因子等于全历史，且 N=6 太小

```python
FACTOR_SAMPLE_WINDOW = 6
recent = hist_sorted[-FACTOR_SAMPLE_WINDOW:]
```

总样本不足 6 的因子，「近 6 单」就是它的整个生命周期。

样本少警告阈值：

```python
FACTOR_SMALL_SAMPLE = 5
small = ... if p0["n"] < 5
```

6 单全红的因子不会触发「样本少」警告。

### 缺陷 4：排序只看近期加权回报，小样本满分天然霸榜

```python
main_pos.sort(key=lambda x: -x[2]["w_return"])
```

胜单 `return_ratio` 约 +0.8~+1.0，输单 -1.0，因此：

- 6/6 → `w_return ≈ +0.97`
- 2/2 → `w_return ≈ +0.96`
- 23 单、含 5 次亏损的「离散凝聚下盘」近 6 单 4/6 → `w_return ≈ +0.52`

排序结果必然是样本最少的满分因子排第一。

### 缺陷 5：退役逻辑系统性保护「近期盈利的小样本因子」

低信息退役硬门槛：

```python
LOW_INFO_MIN_SAMPLES = 5
if len(hist) < LOW_INFO_MIN_SAMPLES:
    continue
```

同时要求平均回报接近 0，且命中率落在 35%~65%。因此 6/6、2/2 的因子永远不会被低信息退役规则清掉。

LLM 退役还有护栏：

```python
if _recent and _profitable:
    continue  # 健康因子，护栏拦下：不准 LLM 休眠
```

「近 7 天触发过且累计盈利」的小样本热因子，LLM 想休眠都做不到。

### 缺陷 6：factor-induction 只去重，不检验

`factor_induction.py` 的核心是候选合并和 LLM 判重，不涉及统计显著性、最小样本或退役判断。因此它不会阻断未验证因子进入 prompt，反而可能把同义因子合并到热因子上，让热因子更显眼。

### 缺陷 7：auto-dormant 惩罚大样本好因子

14 天未触发即自动休眠，不看样本量或历史强度。结果把 50 单、45 单的强因子休眠隐藏，而 2 单、6 单的新因子继续活跃。

## 五、修复方案

### A. 因子流修复：修展示与排序

改动范围：`factor_select.py`、`memory.py`。

1. **展示收缩命中率**

   在 `perf_text()` 中把 `shrunk_rate` 显示出来：

   ```python
   f"{fid} [近{n}单 命中{hits}/{n} 收缩命中{shrunk_rate:.0%} 加权回报{w_return:+.2f}{small}]"
   ```

2. **排序加入样本惩罚或下置信界**

   将纯 `w_return` 排序改为带不确定性的评分：

   ```python
   score = w_return - Z * volatility / sqrt(n)
   # 或简化：score = w_return * (n / (n + N0))
   ```

3. **「样本少」升级为真门控**

   - 设 `FACTOR_MIN_ACTIONABLE = 10`（或 12/15）。
   - `n < FACTOR_MIN_ACTIONABLE` 的因子进观察区，不进主区。
   - 未验证因子禁止重仓。

4. **展示全样本统计**

   在 `perf_text()` 中补充 `total / hit / miss / push` 和首末日期，避免近 6 单掩盖历史亏损。

### B. 因子迭代过程改动：修生命周期

改动范围：`reflect_apply.py`、`memory.py`、`agent.py`。

1. **新因子默认 `testing`，不直接 `active`**

   `record()` 中新因子默认状态改为 `testing`，只进观察区。

2. **加确定性晋升门槛**

   `testing` → `active` 必须同时满足：

   - 已决策样本 ≥ 10；
   - 命中率下置信界 > 50%；
   - 平均回报 > 0。

3. **修复退役逻辑对近期小样本盈利因子的反向保护**

   只有 `total >= 最小样本` 且命中足够时，才允许「近期盈利」成为保护理由；否则小样本近期盈利因子降级为 `testing` 或强制限仓。

4. **auto-dormant 不能隐藏大样本强因子**

   - 大样本且历史命中率显著 > 50% 的因子，即使休眠也保留摘要。
   - 休眠判定加入样本量权重。

5. **把 reflect 的「低样本」结论写进因子状态**

   reflect 已经会输出「低样本，结论待验证」，应将其写回因子，并在选择时强制降入观察区。

6. **限制未验证因子下单仓位**

   `testing` / 低样本因子最大仓位 5%~10%，不能进入 30%~40% 档。

## 六、建议落地顺序

1. 先做 A1 + A2 + A4：改展示和排序，风险最低，见效最快。
2. 再做 A3：把样本少变成真门控。
3. 然后做 B1 + B2：`testing` 状态 + 确定性晋升门槛，是根上的生命周期修复。
4. 最后做 B3 + B4 + B5 + B6：退役、休眠、仓位限制，并配合回归测试。

## 七、一句话定性

梭哈2狗过拟合的根因是：因子生命周期缺少「统计显著性 / 最小样本 / 收缩后评分」这道闸门，反而在展示和退役两端都对近期小样本盈利因子做了加强保护。

## 八、新增项目：竞彩 / 北单 / 串关 因子池隔离

### 需求

1. 竞彩范围（`scope=jc`）狗的因子池与北单（`scope=beidan`）狗的因子池相互独立。
2. 串关狗（串关2狗 / bc狗）的因子不进入 alpha 池统计，但可以被看到；反过来也一样，alpha 池因子不进入串关狗统计，但串关狗可以查看。

### 现状

- `role_registry.py` 已有 `scope` 字段和 `alpha_mode`，但部分角色 `scope` 为空或不一致。
- `factor_induction.py` 阶段 B 的 alpha 池按 `alpha_mode=True` 收集角色，当前 `alpha_agents()` 会把「bc狗」（`scope=beidan, alpha_mode=True`）也收进 alpha 池，导致北单串关因子与 jc alpha 因子混池合并。
- `factor_registry.FactorRegistry.refresh()` 扫描所有角色目录，不区分 scope。
- 串关狗通过 `cross_factor_exclude` / `exclude_roles` 做了部分隔离，但口径是「排除角色名」，不是「scope」。

### 目标设计

1. 每个角色明确 `scope`：
   - 串关2狗 → `jc`
   - 北单双选串关狗 → `beidan`
2. 因子归纳 alpha 池按 scope 分桶：
   - jc alpha 池：alpha狗 + alpha2狗 + 均注狗
   - beidan alpha 池：bc狗
   - 两个池各自 `induct_scope`，互不合并。
3. `FactorRegistry` 增加 scope 过滤：
   - 统计聚合只使用同 scope 因子；
   - 跨 scope 因子以「参考区」只读展示，不参与命中率 / 回报 / 信任权重计算。
4. 串关狗 alpha 模式保持只读查看：
   - 串关狗可以查看另一 scope 的因子 / 订单共识；
   - 串关狗自己的因子不写入对方 scope 的统计池。
5. bc狗从 jc alpha 池排除，jc alpha 狗从 beidan alpha 池排除。

### 改动点

- `role_registry.py`：增加 `scope` 派生 / 校验，`alpha_agents()` 支持按 scope 返回。
- `factor_induction.py`：阶段 B 按 scope 分组建立多个 alpha 池。
- `factor_registry.py`：`refresh()` / `get_all_factors()` 支持 `include_scopes` 和 `reference_scopes`。
- `prompt_builder.py`：跨池因子注入为只读参考块，明确标注「仅供参考，不参与统计」。
- `chuan_guan_dog.py` / `beidan_parlay_dog.py`：`ALPHA_DOGS` 改为按 scope 过滤，`cross_factor_exclude` 对齐 scope 规则。

### 验证口径

- 跑一次 `factor_induction` 后，确认bc狗的因子不再出现在 jc alpha 池的 `factor_perf` 中。
- `FactorRegistry` 对 jc alpha 狗只返回 jc scope 因子的统计；北单 scope 因子仅作为参考区出现。
- 串关狗仍能读到 jc 因子 / 订单共识，但其自身因子不污染 jc alpha 统计。
