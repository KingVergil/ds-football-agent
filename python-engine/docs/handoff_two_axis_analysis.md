# 两轴因子 · 分析链路交接（2026-09-15）

> 交接给下一次接手（用户本人在家继续跑）。
> 现状：**代码已固定；沙箱已推进完（08-07~09-01；09-05/09-07 因数据缺口跳过）**。
> **2026-09-15 更名：`95狗` → `94狗`**（原名来自 `9过5` 时代；现在 `ticket_m=4`，票型是 `N过4`）。
> 线上角色目录 = `data/roles/94狗/`；沙箱 = `data/replays/sandboxes/94狗_两轴0807/`。

---

## 0. 一句话

**因子产出（每 7 天一次）→ 因子账本（按日打戳）→ 每日分析（引擎只做排序）→ 结算 → 离线 A/B**。
核心结论到目前为止：**两轴排序（B）在腿级命中率上优于 stage1 排序（A），样本外 +10.0pp（t=2.33）**；
但票级 ROI 脆（样本外 +12.1%，抽掉最好一波变 −46.8%）。

---

## 1. 这一轮做了什么（代码已固定，勿随意改）

| 文件 | 内容 | 为什么要 |
|---|---|---|
| `src/axis_cond.py`（新） | 两轴因子的 `cond` 求值器（引擎与脚本共用一份） | 因子条件必须机器可读才能每天求值 |
| `src/factor_select.py` | 新增 `_axis_profile()`：带 `axis_samples` 的因子改用**方向边际 / 兑现差**评分，样本**只取 `date < 分析日`** | P&L 的腿级方差会把正边际压成负（实测 +0.63 → −0.17） |
| `src/beidan_parlay_dog.py` | ① `_save_axis_legs()`：分析时按**波次**落盘当天过门腿+因子命中标记（只含赛前信息）② `_accumulate_two_axis()`：结算后拼结果→按日样本写进因子账本 ③ `_axis_score()` + **只排序不过滤**（`DS_AXIS_RANK=0` 可关）④ 方向段渲染"方向边际 ±X.Xpp"、波动段渲染"兑现 vs 当日基线 ⇒ 排序靠前/靠后" | 两轴要真正影响选腿，且不得引入未来信息 |
| `scripts/axis_rank_ab.py`（新） | **同候选、只比排序**的 A/B（0 LLM） | 才能量出"排序"本身的贡献 |
| `scripts/axis_day_report.py`（新） | 逐日体检：引擎实际腿 / veto组 / x降序 / 票级结算（0 LLM） | 每推进一天加一行 |

### 1.1 门（按用户口径收敛到两个）

| 门 | 状态 |
|---|---|
| 价格轴：**`gl0` + `glN`（让球盘也放行，2026-09-15 校对修正）** 且某侧 `x ≥ θ`（θ 由池门账本给，1.1~1.2） | **保留**（腿池定义） |
| 腿数 < 5 → 空仓（`ticket_m=4`） | **保留**（唯一允许的门） |
| stage1 veto（LLM 逐场否决） | **关**：`parlay.json: honor_stage1_veto=false` |
| 出票线 `Πv̂ ≤ 1.833` | **关**：`min_ticket_v=0` |
| `x_cap`（剔极端尾部） | **关**：`x_cap=0` |
| 注数护栏（默认 126） | **放大**：`max_combos=5000` |
| 单腿 `v̂` 下限 | **关**：`min_leg_v=0` |

依据：LLM 侧 stage1 veto 实测**没有筛选力**（被 veto 的场 33.6% vs 未 veto 31.9%，n=539），
却剔掉 24% 候选，是小日子空仓的主因；出票线只挡过 1 波（08-17：Πv̂=1.826 vs 线 1.833）。

### 1.2 两轴排序的定义（B 做法）

```
方向分 = Σ 命中的方向因子边际（顺向因子 +，规避类因子 −）
兑现分 = Σ 命中的波动因子兑现差（ratio_med − base_med）
排序键 = (方向分 ↓, 兑现分 ↓, x ↓)
过滤   = 不额外过滤（只排序）；过滤仍由"价格轴门 + LLM veto（本沙箱已关）"负责
```

因子权重来自 `factor_memory.json` 的 `axis_samples`（**按日**样本，只累计 `date < 分析日`）。

---

## 2. 沙箱状态（继续跑就用它）

```
python-engine/data/replays/sandboxes/94狗_两轴0807/   ← 工作目录（已从 /tmp 挪进仓库持久化）
  95狗.json          资金 47434.66，订单 30+ 张（08-07~08-26）
  persona.md         角色人设
  parlay.json        ticket_mode=rule + 上面那些门开关
  memory/
    factor_memory.json   10 条两轴因子 + axis_samples（按日账本）
    axis_legs_<day>_<HHMM>.json   每波过门腿 + 因子命中标记（赛前信息）
    leg_decision_<day>_<HHMMSS>.json 每波有序候选（A/B 的数据源）
    stage1_resp_*.json / screen_*.json
  factors/fac_*.json  10 条因子的定义
```

**剩 8 天**：`08-27, 08-28, 08-29, 08-30, 08-31, 09-01, 09-05, 09-07`
（08-29 有 287 条腿、09-05 有 302 条腿，是最大盘的两天）

---

## 3. 怎么继续跑（照抄即可）

### 3.1 每天：分析 → 结算（不跑因子反思）

```bash
cd /path/to/ds-agents/python-engine
export DS_ROLES_ROOT=$PWD/data/replays/sandboxes/94狗_两轴0807/workspace
export DS_SESSIONS_ROOT=$PWD/data/replays/sandboxes/94狗_两轴0807/sessions
export DS_BACKTEST_FET=1
export DS_FET_TXT_ROOT=/path/to/fet_txt
export DS_BACKTEST_WAVES_WEEKDAY=16:45      # 约定：周一~周五 16:45 一波
export DS_BACKTEST_WAVES_WEEKEND=16:45,22:30 # 约定：周六/周日 两波

for d in 2026-08-27 2026-08-28 2026-08-29 2026-08-30 2026-08-31 2026-09-01 2026-09-05 2026-09-07; do
  python -m src.beidan_parlay_dog analyze $d --llm --user 94狗
  python -c "
import sys; sys.path.insert(0,'.')
from src.beidan_parlay_dog import BeidanParlayDog
d=BeidanParlayDog(user='95狗'); r=d.settle('$d', reflect=False)
print('结算', {k:r.get(k) for k in ('settled','hit','miss','pnl')}, '资金', d._ensure_role().capital)"
done
```

> 注意：`python3`（homebrew）没有 `langgraph`，**必须用 `python`**。

### 3.2 每次推进后：出 A/B（0 LLM）

```bash
python -m scripts.axis_rank_ab \
  --sandbox data/replays/sandboxes/94狗_两轴0807 \
  --days 2026-08-07,2026-08-08,...,2026-09-07 \
  --md docs/ab_rank_to_0907.md
```

### 3.3 因子账本（每 7 天一次，可选）

本轮只做过一次因子产出（30 天窗口）。若要按 7 天节奏再挖：
`python3 -m src.axis_miner --axis both --chunk-legs 200 --parallel 4 ...`（见 `docs/prompts/factor_produce_two_axis.md`）。
**注意**：新因子要用**分析日之前**的账本，不能含未来样本（`_axis_profile` 已按 `date < as_of` 过滤）。

---

## 4. 结果（截至 09-01，**全部推完**，31 波）

### 4.1 排序 A/B（同候选，只换排序；成本按 `C(n,4)×2元`）

| 组 | 臂 | 腿 | 腿命中率 | 票数 | 中票 | 票命中率 | 成本 | 回收 | ROI |
|---|---|---|---|---|---|---|---|---|---|
| 全部 31 波 | A stage1 序 | 272 | 26.5% | 31 | 5 | 16.1% | 7380 | 6867 | **−6.9%** |
| 全部 31 波 | **B 两轴序** | 272 | **39.0%** | 31 | 15 | **48.4%** | 7380 | 16699 | **+126.3%** |
| 样本内 3 波 | A | 27 | 25.9% | 3 | 0 | 0.0% | 756 | 0 | −100% |
| 样本内 3 波 | **B** | 27 | 48.1% | 3 | 3 | 100% | 756 | 1523 | +101.5% |
| **样本外 28 波** | A | 245 | 26.5% | 28 | 5 | 17.9% | 6624 | 6867 | **+3.7%** |
| **样本外 28 波** | **B** | 245 | **38.0%** | 28 | 12 | **42.9%** | 6624 | 15176 | **+129.1%** |

- **按波配对（样本外）：B−A = +11.4pp，t = 3.21**
- 脆弱性（样本外 B）：抽掉最好 1 波 ROI **+59.0%**｜抽掉最好 2 波 **+19.0%**｜抽掉最好 3 波 **−10.8%**
  ⇒ 仍然依赖少数高赔命中，但比早期（抽 1 波就转负）稳很多。

### 4.2 沙箱实盘（真下单）

资金 **50000 → 47141（−2859，−5.7%）**。
逐日：08-08 −504、08-09 −30、08-13 −10、08-14 −70、08-15 −187、08-16 −504、08-22 −504、08-23 −504、
08-24 −252、08-27 −30、08-28 −252、08-29 −215、08-30 −252、**09-01 +455**；其余天腿数<5 空仓。

> ⚠️ 实盘与 4.1 的模拟口径不同（实盘票型随腿数变、且受"腿数≥5"约束），**判断排序优劣看 4.1**。

### 4.3 ⚠️ 09-05 / 09-07 跑不了（数据缺口，不是代码问题）

这两天的 `data/matches/<day>.json` 里**北单三路赔率全缺**（09-05：61 场 0 有；09-07：27 场仅 2 有），
而 `_beidan_odds()` 要求三路齐全（缺了就排除，**不再用 Pinnacle 兜底** —— 那个兜底会让 `x ≡ 1.00` 静默失效）。
补数据后可直接重跑这两天的命令。

## 5. 还没证实的（别当结论）

1. **票级 ROI 不稳**：+12.1% 靠一两波高赔命中；需要更多大盘日。
2. **样本外只有 20 波 / 173 腿**，且与挖掘窗口同季（06-28~08-08 vs 08-09~08-26）。
3. **优势来自哪几条因子还没归因**（可能由单条因子撑起）。
4. 因子条件里还留着少量"名次类"写法（`rank_mp == 2` 等，如 `中档排名弱势`），
   它们依赖"该场三侧全量"求值（引擎已按全量建环境），但**下一轮挖掘建议改成三侧直读**。
5. ⚠️ **有一条因子的名字与条件不符**：`主队水位走低` 的条件是 `side_is == H 且 ah.h0 < ah.h1`
   —— 末水位**高于**首水位（升水），名字却说"走低"。引擎按**条件**执行（正确），
   但 prompt 里显示的名字会误导 LLM。下一轮挖掘时按条件重命名（或改条件）。
6. `cond` 求值器支持三种等价写法：`side` / `self` / `side_is`（2026-09-15 补齐 `side`）。

---

## 6. 相关文件

| 文件 | 内容 |
|---|---|
| `docs/beidan_two_axis_analysis_plan.md` | 方案 v2（未来信息边界、门控设计、代码改动清单） |
| `docs/ab_rank_0807_0826.md` | 23 波逐波 A/B |
| `docs/beidan_axis_two_stage.md` | 两轴结构与可行性证据（更早的一轮） |
| `docs/prompts/factor_produce_two_axis.md` | 因子产出 prompt（首轮 / 周期共用） |
| `data/factor_mine_out/final_w30.json` | 当前 10 条两轴因子 |
| `data/factor_mine_out/*.md` | 挖掘 / 合并 / 归纳的中间产物 |

---

*生成时间 2026-09-15。代码改动全部在北单路径或新文件；单狗路径未改。*
*测试：`pytest tests/ -q` → **265 passed**（含 `tests/test_two_axis_rank.py` 9 条两轴守卫、`tests/test_beidan_axis.py` 24 条）。*

---

## 7. 2026-09-15 校对修正（线上 ≡ 沙箱 B，除数据访问）

转正后做了一次"线上 vs 沙箱"逐项校对，发现并修了 3 处：

| # | 问题 | 影响 | 修法 |
|---|---|---|---|
| 1 | **让球盘被静默剔除**（`pool_gate.gl_classes=["gl0"]`） | 08-07~09-01 过门腿里 **264/625 = 42% 是 glN**，22/22 天都有；线上今天 11 gl0 vs 12 glN —— 这些腿**被 stage1 分析过（LLM 付了钱）却从未进票** | `gl_classes` 改为 `["gl0","glN"]`（线上+沙箱同步）；并给 LLM 路径补上"盘口类剔除"日志（规则版路径保持原行为） |
| 2 | **空仓日不记因子样本**（`settle()` 无订单时 early-return） | 31 天里 10 天空仓 ⇒ 那些"分析过、有结果、只是没下注"的腿不进账本 ⇒ **账本只统计下过注的日子**（选择偏差） | 空仓分支里也调 `_accumulate_two_axis` / `_accumulate_vol_screen`；实测 08-10 从"无样本"变为有样本 |
| 3 | **同一天重复计样本**（离线 seed 与当天结算撞日） | 08-08 在账本里出现两次 | 追加样本改为**按日期幂等** |

**校对表（线上 95狗 vs 沙箱 B）**：`95狗.json` / `parlay.json` / `factor_memory.json` / `persona.md` /
`factors/*.json` **逐项一致 ✅**；唯一差异是数据访问（live vs 本地切片）。

✅ **口径澄清（2026-09-15 二次校对）**：§4 的 A/B **本来就含让球盘** ——
`scripts/axis_rank_ab.py` 直接从 `leg_decision` 候选 + 腿池取腿，不做 gl 类过滤，
实测候选腿 = **570 gl0 + 356 glN**。所以：
- B 38.7% vs A 25.9% 就是「gl0 + glN」口径的结果；
- 被挡掉的只是**引擎实际下单**那条路（`gl_classes=["gl0"]`），现已修成 `["gl0","glN"]`
  ⇒ **线上行为与这份 A/B 的证据口径一致了**。

---

## 8. 2026-09-16 结算 / 因子生成**彻底解耦**（北单 + 单狗）

| 层 | 改动 |
|---|---|
| 单狗引擎 `src/agent.py` | ① `AgentState` 补 `reflect: bool`（**TypedDict 没声明的键会被 LangGraph 丢掉** —— 这是第一次改漏掉、reflect 依旧会跑的原因）② `build_settle_graph()` 把 `settle_orders → reflect` 改成**条件边**（`reflect=False` 直接 END）③ `Agent.settle(..., reflect=True)` + 新增 `settle_only()` / `reflect_only(day)`；`reflect_only` 按**足球日**（12:01 切日）取已结算订单，只跑 reflect 节点，不动订单与资金 |
| 桥 `src/bridge.py` | 单狗 `_do_settle` 也按 `opts.stage` 分流：`settle`（只结算、不注入 provider）/ `reflect`（只产因子）/ 缺省 `both`；`_do_reflect` 放开给单狗（原来只允许北单串关狗） |
| UI `client.js` / `lobby.js` | 「🧾 结算」= **只结算**（`stage:"settle"`）；「🧬 产因子」= `func:"settle"` + `stage:"reflect"`（**旧实现发 `func:"reflect"`，而它在桥的 `BRIDGE_FUNCS` 白名单外 ⇒ 点了一直报「未知功能」**）；两个按钮**单狗也显示**；旧的"结算+反思"按钮改名「🧾 结算+产因子」 |
| 测试 | 3 条旧守卫更新（改名 95→94；单狗 reflect 从"必须被拒绝"改为"支持，且无 key 必须显式报错"）+ 新增 1 条解耦守卫；**`pytest tests/ -q` → 266 passed** |

验证（0 LLM）：
```
桥 settle + stage=settle（单狗）→ 只有「全部未结算: 0 单」，无 reflect 行
桥 settle + stage=settle（94狗）→ {"stage":"settle", "settlement":{"settled":0}, "capital":4748}
Agent.settle(reflect=True)  → 出现「reflect 跳过: settled=0 provider=无」
```
