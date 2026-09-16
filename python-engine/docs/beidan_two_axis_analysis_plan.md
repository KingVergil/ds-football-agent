# 两轴因子接入「北单串关狗 · 按日分析」方案 v2

> 日期：2026-09-15（v2，按用户口径重写）｜状态：**方案待确认，未动代码**
>
> 用户口径（v2 依据）：
> 1. **走 b = 北单串关狗**（`src/beidan_parlay_dog.py`），允许改代码；
> 2. **一个窗口最高 20 场，批次不固定**；
> 3. **方向因子必须走因子机制**（因子迭代的内容），**不许塞进 persona.md**；
> 4. `agent.py` 的因子机制要理解对再用。

---

## 0. 结论速览

0. **未来信息边界（硬约束，先立规矩）**：开奖 SP / 开奖结果 / 命中，都是**分析日之后**才知道的，
   只允许进"因子账本"，**绝不允许进分析 prompt**。而且账本必须**按日打戳**，
   分析第 D 天时只能用 `date < D` 的样本 —— 否则回放乱序就会把当天结果漏进当天分析。
1. **窗口口径已满足**：`beidan_parlay_dog.py:814` 就是 `BATCH_SIZE = 20`，
   按 `match_blocks[i:i+20]` 切，**批次数随当天场次数变化**（不固定）—— 不用改。
2. **方向因子有现成的正确归宿**：`src/p_calibration.py`
   —— `k_f = (Σ hit + a) / (Σ p̂ + a)`，就是"该因子命中的腿，命中率比市场 p̂ 高多少"，
   **正是我们两轴方向轴的判据**（`d = hit − p̂` 的比值形式）。
3. **卡点只有一个**：现有 `_accumulate_p_calibration()` 只吃**已下注的腿**（有 `p_hat` 的 flex 腿）；
   我们的方向因子需要在**当天全部过门腿**上求值（不然又是"只统计自己下过的腿"的选择偏差）。
   ⇒ 需要新增一条**日级方向通道**（约 40 行，北单路径内，门控）。
4. **其次要改一处评分**：`factor_select.factor_profile()` 对方向因子用 P&L 的 `w_return`
   再减波动惩罚（实测把 +0.63 压成 −0.17、正向因子被归成"反向"）；
   两轴因子要改用**方向边际**评分。必须门控（只认带 `axis_stat` 的因子），否则会动到单狗。
5. 不需要碰 persona；不需要 seed 假的注单 history。

---

## 1. 现状（读代码，已逐条核对）

### 1.1 北单串关狗的一天

```
analyze(day)
  ├─ 候选腿 = 引擎门（gl=0 且 x ≥ θ），每场一条 block
  ├─ stage1：batch=20 初筛，逐场输出 {摘要, 推荐(H/D/A/高波动/skip), factors}
  └─ stage2：组票（flex）→ 落 slip 级订单
settle(day, reflect)
  ├─ 结算 → 每腿 actual/hit/profit
  ├─ _accumulate_screen(...)          ← 波动轴日级通道（当天所有被因子标记的场次 × 高波动率）
  ├─ _accumulate_p_calibration(...)   ← 方向轴校准（只吃已下注的 flex 腿）
  └─ reflect → agent.py::node_reflect → reflect_apply（因子归因写回 factor_memory）
```

### 1.2 因子进 prompt 的两条路（都在 `selected_active()` 上）

| 路径 | 取什么 | 渲染 |
|---|---|---|
| 方向 | `selected_active()` 的 **main**（按 `sign` 分顺向/反向） | `_direction_factor_text()`：名字 + `desc` + 近 n 单统计 |
| 波动 | `selected_active(include_volatility=True)` 的 volatility 列表 | `_volatility_factor_text()`：名字 + `desc` + 高波动率 vs 当日基线 |

`selected_active()` 三道门槛（`src/memory.py:576`）：
`factor_profile()==None` → 不可见；`n<2` → 只进观察区（有名字无 desc）；越过 ±0.10 才进 main。

### 1.3 两个池子里已有但对不上的东西

| 机制 | 现状口径 | 我们的需求 | 差距 |
|---|---|---|---|
| `_accumulate_screen` → `record_screen(f, n, high, base_sum)` | **当天所有被该因子标记的场次**，`high = SP ≥ HIGH_VOL_SP` | 波动轴因子：当天所有被 cond 命中的腿，看 `ā = SP/赔率` | **腿 vs 场次**、**绝对 SP vs ā** |
| `_accumulate_p_calibration` → `record_leg(factors, mode, {side: p̂}, actual)` | **只统计已下注的 flex 腿** | 方向轴因子：当天**全部过门腿** | **只差样本范围**（口径完全一致） |

---

## 2. 目标架构

```
两轴因子（cond 机器可读）
   │
   │  ① 每天【结算之后】，引擎在【当天全部过门腿】上按 cond 求值
   ▼
┌─ 方向因子 ────────────────────────────────┐
│ 追加【按日样本】：                          │
│ {date, n, sum_p, sum_hit}                 │   ← 累计口径 = 命中率 − 市场p̂（方向边际）
└───────────────────────────────────────────┘
┌─ 波动因子 ────────────────────────────────┐
│ 追加【按日样本】：                          │
│ {date, n, ratio_med, base_med}            │   ← 只统计"预测命中且有开奖结果"的腿
└───────────────────────────────────────────┘
   │
   ▼  账本文件：<角色>/memory/axis_stats.json（新增，带日期）
selected_active(as_of=分析日)：只累计 date < 分析日 的样本 → 算边际 → 进 main 顺向/反向
   │
   ▼
stage1/stage2 prompt 的因子段（方向：顺向/反向；波动：volatility 列表）
```

**为什么不用 `p_calibration.json`**：那个文件是**无日期的累计量**（`k()` 全量求和），
回放时无法按 `date < 分析日` 过滤 ⇒ 天然有未来信息泄漏风险。两轴统计另开带日期的账本，
`p_calibration` 保持原样不动。

---

## 3. 需要的代码改动（3 处，全部在北单路径 + 门控）

### 改动 1（必须）：新增日级方向通道

**位置**：`src/beidan_parlay_dog.py`，紧挨 `_accumulate_p_calibration` 新增
`_accumulate_two_axis_direction(role, day_date)`。

**逻辑**（在**结算之后**执行，绝不在分析之前）：

```python
1. 取当天全部「过门腿」（gl=0 且 x ≥ θ，含未下注的）→ [(lota_id, side, 市场p̂)]
2. 取当天开奖结果 actual（既有的 result 反查链路）
3. for 每个带 cond 的方向因子 f:
       hits = [腿 for 腿 in 过门腿 if eval_cond(f.cond, 腿)]
       # 关键：样本是"全部过门腿"，不是"我下过的腿"
       sample = {"date": day_date, "n": len(hits),
                 "sum_p": Σ 市场p̂, "sum_hit": Σ 1{中}}
4. 把 sample **追加**到 axis_stats.json（不是覆盖标量！）
```

**同时扩展 `PCalibration.record_leg` 的调用方式**：`claims={side: p̂}` 语义不变，
`actual` 传该腿开奖方向 —— 与现有 flex 腿完全一致，只是样本来源换了。

### 改动 2（必须）：两轴方向因子改用"方向边际"评分（按 as_of 过滤）

**位置**：`src/factor_select.py::factor_profile`

```python
axis = axis_stats.factor_samples(name, before=as_of)   # ← 只取 date < 分析日
if axis:
    # 两轴方向因子：方向边际才是它的判据；P&L 的腿级方差会把正 edge 压成负数
    edge = Σsum_hit/Σn − Σsum_p/Σn            # = d（比值形式）
    rank_score = edge * AXIS_EDGE_SCALE         # 例：×10 ⇒ d=+5pp → +0.50
else:
    ...（原逻辑，逐字节不变）
```

**门控**：只有 `axis_stats.json` 里**存在该因子样本**的因子走新分支。
单狗/其它狗的因子不会出现在这个文件里 ⇒ 行为完全不变（这一点必须有测试守）。

### 改动 3（必须）：波动轴改成"只统计预测命中且有开奖结果的腿"

**原理**：波动因子回答的是"这条腿的赛前赔率值不值得连乘"。所以它的账应该这样记 ——

```
每天结算后：
  ① 对当天所有腿求值 cond → 得到该因子"预测会兑现"的腿
  ② 只保留其中【已经有开奖结果】的 → 样本（没开奖/延期的不算）
  ③ 算这些腿的实际兑现（开奖SP / 赛前赔率），与【当天全部已开奖腿】的同一指标比 → 差值
  ④ 差值写进该因子记录；排序时正差值排前、负差值排后
```

**为什么替掉现存的 `screen_<day>.json` 通道**：那套是"LLM 在 stage1 报的因子名 + 场次级 +
`SP ≥ 固定阈值`"，与两轴的定义（腿级、兑现相对赔率、相对当日基线）不是一回事。
两轴波动因子改走上面这条腿级通道；旧的 screen 通道保留给原有因子，不删不改。

**落库字段**（波动因子，同样是按日样本）：

```json
{"date": "2026-08-09", "n": 21, "ratio_med": 1.12, "base_med": 0.91}
```

`factor_select` 里对波动因子：`rank_score = 累计(ratio_med) − 累计(base_med)`，
同样**只累计 `date < as_of` 的样本**（正排前、负排后）。

---

## 4. 因子怎么落库（走因子机制，不碰 persona）

### 4.1 `factor_perf[name]` 新增字段（两轴专用）

```json
{
  "desc": "主队侧且末水位高于首水位时，命中率比市场 p̂ 低 4~6pp（两窗验证）",
  "type": "directional",
  "status": "testing",
  "cond": "side_is == H 且 ah.h0 < ah.h1",        ← 机器可读条件（新增）
  "role": "veto",                                  ← select / veto / rank（新增）
  "origin": "offline_mine_2026-09-15",             ← 来源标注（新增，防误读成真实战绩）
  "slugs": ["asian-handicap-crown", "discrete-odds"]
}
```

**统计另存**（不放在 factor_perf 里，因为需要按日过滤）：

`<角色>/memory/axis_stats.json`

```json
{"factors": {
  "主队水位走高否决": [
    {"date": "2026-08-09", "n": 21, "sum_p": 6.24, "sum_hit": 4.0},
    {"date": "2026-08-10", "n": 13, "sum_p": 3.90, "sum_hit": 6.0}
  ],
  "热门低赔保真": [
    {"date": "2026-08-09", "n": 18, "ratio_med": 1.14, "base_med": 0.92}
  ]
}}
```

分析第 D 天时只累计 `date < D` 的样本 —— 与 `factor_profile` 对 `history` 的日期过滤同源。

波动因子：`"type": "volatility"`，`"role": "rank"`，`"expect": "+"/"-"`。

### 4.2 `factors/fac_<id>.json`

```json
{"id": "fac_主队水位走高否决",
 "slugs": ["asian-handicap-crown", "discrete-odds"],
 "content": "[两轴·方向·veto] H 侧且 ah.h0 < ah.h1 时命中率低于市场 4~6pp（拟合 n=211 / 留出 n=425）。命中即不选该腿。来源：离线两窗验证。"}
```

### 4.3 不要做的事

- ❌ 不要 seed 假的注单 history（实测会被 P&L 的波动惩罚翻成"反向因子"，还污染飞轮语义）
- ❌ 不要把因子条件写进 `persona.md` / system prompt（那是人设，不是因子）

---

## 5. 按日分析的执行路径

```
① 写因子（§4）+ 三处代码改动（§3）
② dump prompt（0 LLM）验证：
   - 方向因子出现在「📈 顺向 / 🔄 反向」段并带 desc
   - 波动因子出现在「🌊 波动型因子」段
   - 每场数据段含因子 slugs
③ 跑 1 天真分析（stage1 batch=20 + stage2），量 token/耗时
   （参照：北单串关狗一天 130k~264k input tokens）
④ 结算 → 改动 1 写 axis_stat → 看方向因子的 d_pp/k 是否与离线一致
⑤ 再推进多天；每批看 axis_stat 的累计 d_pp 漂移
```

---

## 6. 判据

| 层 | 指标 | 通过标准 |
|---|---|---|
| prompt | 10 条因子出现且带 desc；slugs 生效 | 100% |
| 通道 | 引擎算出的 `d_pp` 与离线 `final_w30.json` 的 hold 值差 | ≤ 3pp |
| 分析 | **veto 后全池** vs **过门池** 命中率差 | ≥ +5pp 且跨批稳定 |
| 票级 | 4 关全中 | 只记录（30~40% 腿命中率下中票概率 1~3%） |

---

## 7. 已实测确认的事实（本方案依据）

| # | 事实 | 证据 |
|---|---|---|
| 1 | stage1 batch=20、批次数随场次数变化（不用改） | `beidan_parlay_dog.py:814,948` |
| 2 | 方向因子进 prompt 需进 `selected_active` 的 main | `memory.py:576-640`、`_direction_factor_text` |
| 3 | 无 history/screen ⇒ 因子完全不可见 | 实测 |
| 4 | seed 腿级 P&L history ⇒ **正向方向因子被翻成"反向"**（+0.63 → −0.17） | 实测；根因 `rank_score = w_return − 1.0×σ/√n`，腿级 SP 方差巨大 |
| 5 | 波动因子（type=volatility）不受该惩罚（rank_score=裸 w_return） | 实测，4 条落点正确 |
| 6 | `p_calibration` 的 k=(Σhit+a)/(Σp̂+a) 就是方向边际 | `p_calibration.py:100-145` |
| 7 | `record_screen` 已是"当天所有被标记场次"日级通道（仅波动） | `beidan_parlay_dog.py:3410-3441` |
| 8 | `as_of` 回放时=当日；`last_seen` 超 30 天判休眠 → 线上直接跑今天会看不见 | `agent.py:526`、`factor_select.FACTOR_DORMANT_DAYS` |
| 9 | 真·北单串关狗一天 130k~264k input tokens | `data/sessions/*/…_analyze_…md` |

---

## 8. 待确认（1 条）

**门控写法**：两轴统计放在新文件 `<角色>/memory/axis_stats.json` 里（带日期、按 as_of 过滤），
`factor_select` 只对"在这个文件里有样本"的因子改用方向边际/兑现差值评分。
单狗与其它狗的因子永远不会出现在这个文件里 ⇒ 打分逻辑对一个字节都不变（会加守卫测试）。
这样可以吗？

> 波动轴口径已按你的要求定死：**只统计"预测命中、且有开奖结果"的腿**，
> 与当天全部已开奖腿比兑现差值，按差值排序 —— 不再讨论阈值口径。

---

## 9. 未来信息边界（写成守卫）

| 位置 | 允许 | 禁止 |
|---|---|---|
| 分析 prompt（stage1/stage2，第 D 天） | 赛前赔率、盘口、离散、市场 p̂、x、span、**以及 `date < D` 的因子账本** | 开奖 SP、开奖结果、比分、`date ≥ D` 的任何账本样本 |
| 因子账本 `axis_stats.json` | 开奖 SP / 命中（**结算之后**写入，带 date） | 不许无日期累计（`p_calibration.json` 就是无日期累计，故两轴统计不放在那里） |
| 现有护栏 | `_assert_prompt_redacted()` 已在 stage1(`:835`)/stage2(`:2184`) 调用 ✓ | 新增的账本注入点必须同样过这条护栏 |

守卫测试（新增）：
1. 第 D 天的 prompt 里不出现 `date ≥ D` 的账本数字；
2. 把 `axis_stats.json` 里 D 日之后的样本删掉/保留，第 D 天 prompt 逐字节不变。
