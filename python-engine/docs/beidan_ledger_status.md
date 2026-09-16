# 北单（bc狗 / bcl狗）现状交接文档 · 2026-09-11

> 用途：**换对话继续时先读这一份**。它把「策略形态、代码落点、证据数字、配置现状、运行手册、
> 已验证/未验证、待决事项」一次讲清。相关细节文档在文末链接。

---

## 0. 一句话现状

北单策略已从「LLM 猜比赛 + 填固定票型」改成：

> **引擎用奖池账本找错价（gl0 且 x≥θ）→ stage1 逐场判断（读全量数据，只能同意/否决/归因）
> → 引擎按规则组装（自动多选、N串1）→ 硬门（关数/成本/ROI）放行或空仓。**

`x = 市场p̂(锐市场同盘口去水) × 北单赛前赔率`，**成票判据锁死 x**；LLM 不参与定价。

---

## 1. 架构与数据流

```
                       ┌──────────────── 每天结算时（settle 里自动、与是否下注无关）────────────┐
                       │  src/pool_ledger.py  奖池账本（普查全部已开奖北单场次）               │
                       │  桶 = 盘口类(gl0/glN) × x 区间；记 n / Σz / Σz²（z = SP×1{中}）      │
                       │  幂等(ingested) + as_of 防未来 + 回看窗口(默认 14 天，吸收滞后 SP)      │
                       └──────────────┬───────────────────────────────────────────────────────┘
                                      │ policy(as_of, window)
                                      ▼
  阶段0（引擎，确定性）   θ（x 门限）、m_star（每注最少关数，由 y 的 CI 下沿反解）
                                      │
  阶段1（LLM，batch=20，**全量数据段**） 逐场：推荐(同意哪侧) / veto(仅信息层面) / factors / extra_picks
                                      │
  阶段2（**引擎规则**，ticket_mode="rule"）  ① 腿池 = gl0 且某侧 x≥θ
                                      │      ② veto → 整场剔除（LLM 唯一硬杠杆）
                                      │      ③ 一条腿 = 该场所有过门侧（自动双选/三选）+ extra_picks
                                      │         （按被选侧**平均 x** 判门，摊薄过头自动丢）
                                      │      ④ x_cap（默认 1.45）排除极端尾部
                                      │      ⑤ N = min(腿数, 9) → `N串1`（腿数>9 时按 stage1 顺序保留前 9）
                                      ▼
  硬门（_apply_flex_guardrails）  腿池门(gl/x) + 关数门(M≥m_star) + 成本/注数 + **ROI 门(Πx>出票线)**
                                      ▼
                                  落单 or 空仓（空仓是合法结果）
```

**关键原则**
* 边际来自 **x**（奖池错价），不来自 LLM 的 p̂（p̂ 被封顶 1.25×市场 + 因子校准，且**不参与成票**）。
* `x = p_sharp / p_pool`（北单赔率是公平价，Σ1/o≈1）；池子的概率加权 x 恒等于 1 ⇒ 两侧同时 >1 必须第三侧明显 <1。
* 让球盘（glN）**只观察不下注**（实测无边际），但**照样进候选、照样算 x**。

---

## 2. 代码落点（本会话新增/改动）

| 文件 | 作用 |
|---|---|
| `src/pool_ledger.py` **新增** | 奖池账本：`PoolLedger`（ingest/stats/policy/report）+ `collect_day_rows` + `update()`；CLI `python3 -m src.pool_ledger update|report` |
| `src/line_convert.py` **新增** | Poisson 让球线换算（1X2 → 任意 goal_line 三路概率），覆盖 27%→100%；`fit_lambdas/handicap_probs/convert_1x2_odds` |
| `src/beidan_parlay_dog.py` 改动 | **波次边界修复**（`_filter_beidan_matches` 按分钟比较）+ flex 护栏 + 两道门 + `gate_basis="x"` + `ticket_mode="rule"` + `_assemble_legs_rule` + `_select_legs_x_top`(0-LLM 规则臂) + stage1 重构 + `dump_prompts`(CLI `prompt`) + settle 挂钩账本 + 段序 `source_order` |
| `src/data_manager.py` 改动 | `get_sections(..., source_order=False)`（北单 stage1 传 True → 忠实按原始 txt 段序） |
| `src/chuan_guan_dog.py` 改动 | `_match_sections_text(..., source_order=False)` 透传 |
| `scripts/mispricing_buckets.py` **新增** | 错价分桶实验（离线、0 LLM）→ `docs/mispricing_bucket_report.md` + `.json` |
| `scripts/line_convert_check.py` **新增** | 换算精度/覆盖率校验 + 用换算后 p̂ 重跑分桶 |
| `scripts/rule_arm_backtest.py` **新增** | 0-LLM 规则臂基线（多选腿口径、bootstrap）→ `docs/rule_arm_report.md` + `.json` |
| `scripts/run_sandbox_day.py` **新增** | **在临时沙箱真 LLM 跑一天**（stage1→组装→可选结算），prompt/回复落盘 `docs/prompts/llm_run_<day>/` |
| `tests/test_pool_ledger.py`(16) / `test_pool_gate.py`(10) / `test_rule_arm.py`(11) / `test_line_convert.py`(5) | 账本、两道门、规则组装、换算；**全套 142 passed** |
| `data/roles/bcl狗/` **新增** | 沙盒狗（persona.md / parlay.json / memory/pool_ledger.json 已回填 1341 场） |

文档：`docs/pool_ledger_gate.md`（账本+门）、`docs/line_convert.md`（换算与让球盘结论）、
`docs/mispricing_bucket_report.md`（错价实验）、`docs/rule_arm_report.md`（规则臂基线）、
`docs/persona_sandbox_bc_ledger_draft.md`（沙盒人设草案）、`docs/prompts/README.md`（prompt 演进+泄漏审计）。

---

## 3. 证据链（关键数字，别丢）

### 3.1 错价存在，且只在不让球盘（普查 1576 场 / 4728 腿，y = E[SP×1{中}]）
| 档位 | n | y | 95%CI | 判定 |
|---|---|---|---|---|
| **gl0 且 x≥1.10** | 791 | **1.341** | [1.194, 1.489] | ✅ 可用（两口径互证：锐市场法 1.180 / 实测法 1.185） |
| gl=0 且 x≥1.0 | 1406 | 1.183 | [1.092, 1.273] | ✅ |
| gl≠0 且 x≥1.10 | 496 | 0.959 | [0.811, 1.107] | ❌ |
| gl=−1 | 308 | 0.916 | [0.733, 1.099] | ❌ |
| gl=+1 | 108 | 1.066 | [0.719, 1.413] | ⚠️ 未定论（仅换算参考） |

* 打平线：`x* = (1/0.65)^(1/M)` → 2关1.240 / 3关1.154 / 5关1.090 / 8关1.055 / **9关1.049**。
* 池子的钱在哪：gl=−1 时 H 超买 **+5.1pp**、A 低买 **−3.3pp**（但 3~5pp 命中率偏差 ≈ 价格 9%，抵不过 0.65）。
* 口径校验：官方 `result` **就是让球后结果**（比分+goal_line 反推对照 2001 场 **100% 一致**）。
* 换算精度：Poisson 换算 vs 书商同盘口「平均欧盘」三路概率 **平均绝对误差 0.026**。

### 3.2 回放数据拼接（用户重点关注的正确性）
2026-07-11 两波：**16:30 波 21 场 → pass_6 15 / pass_12 4 / pass_1day 2；20:30 波 12 场 → 10/1/1**；
`prompt 内联数据段 == 该波次切片` **21/21、12/12**；同一场跨波会换档（Lota4459810: pass_1day→pass_12）；**0 回退**。

### 3.3 选腿方式（0-LLM 规则臂，2026-07-01~09-09，44 个可用日）
| 选腿 | 腿级 y | 44 天票级 |
|---|---|---|
| **x 降序取前 N（= max Πx 的贪心）** | 1.34~1.41 | **35 张 0 中** |
| x 居中（靠门限） | 1.13~1.16 | — |
| 池内随机 / 不择优 | **1.45~1.61** | N=3 ROI +170%（n 小） |
| 不过 x 门的对照 | — | ROI **−14%~−73%** |

⇒ **门有效；在 x 上再择优有害（赢家诅咒）**，所以规则组装**不按 x 排序**。

### 3.4 票级是彩票，验证要看腿级
9 关票中奖率 ≈ `0.3⁹ ≈ 5e-5`；44 天里 9 关票期望中票 ≈ 0。
实测：08-15 两张 `9串1`（各 4 注 8 元）分别命中 **2/9、3/9 → 均不中**（当日 −16 元）；
07-11 `4串1`（2 注 4 元）与 LLM 组装版逐条一致。**单日盈亏不构成策略判据。**

### 3.6 波次边界（2026-09-11 修复）

**问题**：`match_time` 是 19 字符（含秒），`as_of`（波次时刻）是 16 字符，直接字符串比较时
`"2026-08-15 20:30:00" > "2026-08-15 20:30"` ⇒ **正好在波次整点开赛的场次会被误当成"未开赛"进入该波**
（此刻已无法下注，且该场拿不到切片 → 只有"数据段缺失"占位，白烧 token）。

* 影响面：缓存里 **83 场**开赛时刻正好落在波次整点（22:30 ×63、20:30 ×15、16:30 ×5）。
  例：08-15 的 `Lota4569646`（20:30 开赛）此前会进 20:30 波。
* 修复：`_filter_beidan_matches` 一律**按分钟比较**（`mt[:16] <= now[:16]`），
  即"同一分钟内 :00 / :59 都算已开赛，晚一分钟才算未开赛"（与 `chuan_guan_dog.py` 既有写法一致）。
* 复核：08-15 波次 16:30 / 20:30 保留 146 / 112 场，其中"已开赛/整点开赛"误入 **0 场**。
* 测试：`tests/test_wave_boundary.py`（3 条：整点排除、三个常用波次、live 模式）。

### 3.5 真 LLM 运行记录
| 日期 | 模式 | 调用 | 结果 |
|---|---|---|---|
| 07-11 | LLM 组装(stage2) | 4 次 / 275~384s | `4串1` 2 注（含 LLM 主动双选 H+D）；波2 3串1 一次被拒一次被 p̂ 抬高后放行（→ 促成锁 x） |
| 07-11 | **rule 组装** | **2 次 / 121s（0 stage2）** | 与 LLM 组装**逐条一致** ⇒ stage2 冗余被证实 |
| 08-15 | rule 组装 | 13 次 / 276s | 门切 `[ledger]`：θ=1.1、**m_star=4**；波1 45 腿→保留前 9；两波各 `9串1` 4 注（含自动双选 `D/A`） |
| 08-15 | 中途失败 | — | **DeepSeek 余额不足**（`Insufficient Balance`）→ 该波 stage1 全失败 → flex 狗不套模板 → 空仓（充值后重跑正常） |

### 3.7 单日完整 loop / 因子飞轮实跑（2026-09-11，bcl狗 7.11）

入口：`python3 -m scripts.run_beidan_loop --day 2026-07-11 --dog bcl狗`（新脚本，见 §5）。
沙箱 `/tmp/beidan_loop_bcl狗_20260711`，线上零影响；prompt 全文 `docs/prompts/beidan_loop_2026-07-11/`。

| 阶段 | 实测 |
|---|---|
| ① 分析 | 波1 16:30（21 场，1 块 batch=20）→ **4串1 2 注 4 元**（Πv̂=2.053，估 ROI +33.5%，自动双选 `Lota4467503 H/D`）；波2 20:30（12 场）→ 3串1 被 **ROI 门拒**（Πv̂=1.749 ≤ 出票线 1.791）；**共 4 次 LLM / 168s** |
| ② 结算+反思 | 一票否决命中：`Lota4468146` 买 A 实际 H → 4串1 命中 2/4 不中，**PnL −4.00**；概率校准记 5 组 (p̂,结果)；**2 次反思**（directional + volatility，共 236s） |
| ③ 因子归纳 | 合并 0、补定义 0（首日只有 2 个因子，无可判重） |
| ④ 飞轮终态 | 因子 **0 → 2**：`离散上升追强`（directional，样本 2/命中 2/盈亏 +5.87）、`离散极低防冷全包`（volatility，样本 1/命中 0/−0.37）；反思 2 条；资金 5000 → 4996 |

结论：**飞轮能自举**（α 因子从 0 起步：反思输出的 `alpha_factors` + `factor_attribution`
既建因子条目又记样本），但 1 天只有 2 因子 / 3 样本，离"因子是否改变 veto/选腿"还很远。

审计：stage1 prompt（分析用）`result`/`spvalue`/`比分=` **0 命中**（无后视泄漏）；
stage2 prompt 含开奖属**反思用**（结算后），符合预期。

#### 薄壳回放（ds_replay）3 天实测（2026-09-11，修复 `_is_parlay_dog` 之后）

`ds_replay(dog="bcl狗", start="2026-07-11", end="2026-07-13", mode="interactive", factor_review_every=3)`
—— 与引擎脚本链路一致，飞轮逐日迭代可见：

| 日 | 下单 | 结算 PnL | 因子库 | 关键机制触发 |
|---|---|---|---|---|
| 07-11 | 1 单 `4串1`（成本 4） | −4.00 | 0 → 4 | 2 次反思（directional + volatility）产出新因子 |
| 07-12 | 2 单（`6串1` + `4串1`） | −8.00 | 4 → 8 | 反思再产出 4 个（directional 3 / volatility 3） |
| 07-13 | 0 单（门内候选不足） | 0 | 8 → 9 | **因子退役首次触发：休眠 2 个**（volatility，样本 1 / 盈亏 0） |

- 资金 5000 → **4988**（3 张票各 4 元，全未中——票级方差大属预期）。
- 反思记录 4 条、因子 9 个；directional 项 `样本=命中`（浅盘离散发散赢盘 3/3 +5.31、
  主队优势市场冷落 2/2 +5.87、离散客低非主胜 2/2 +5.87、客队ELO高主让浅盘 2/2 +4.10）。
- **迭代方式（就是本轮要观察的东西）**：`分析(带因子清单+校准) → 结算反思(归因既有因子 +
  发现新因子) → 归纳(去重/补定义) → 周期退役(样本不足/无收益的转 dormant)`；
  每多跑一天，因子条数、样本数、反思条数单调累积，退役在周期边界切掉弱因子。

踩坑（已修）：
* **`src/store.py::FACTORS_DIR` 写死 `data/factors`**，不认 `DS_FACTORS_ROOT` →
  沙箱反思新发现的因子定义漏写线上全局（本次实测漏了 2 个）。已改为与
  `factor_induction.FACTORS_DIR` 同口径（`DS_FACTORS_ROOT` 优先），回归测试
  `tests/test_sandbox_factor_isolation.py`（3 条）。
* **`bridge._is_parlay_dog` 不认平铺沙箱**（最隐蔽、最像策略问题的一个）：
  它只查 `<role_root>/<狗>/parlay.json`，而沙箱 role_root 是**单狗平铺**目录
  （`parlay.json` 在根下）→ 回放里串关狗被判成普通狗，走通用竞彩 Agent：
  **analyze 拿到 21 场却 0 单、settle 打印「reflect 跳过: settled=0」、一个因子都不产出**。
  症状与"冷启动无因子"极像（本次一度误判为模型 skip 率高 / key 失效）。
  已按 `_ensure_dog` 同口径加平铺兜底；回归测试 `tests/test_bridge_parlay_detect.py`（4 条）。
* harness `ds_replay` 原先写死 `jingcai_only: true` → 北单狗被当竞彩跑
  （7.11 实测返回 13 场竞彩 + 拆 4 窗）。已按 `dogs.json` 的 scope 分派；
  北单**不走 prepare/prepare-range、不拆窗**（引擎 DataManager + fet_txt 切片自管）。
  隔离测试 `harness-plugin/tests/replayLottery.test.mjs`（4 条）钉住"单狗路径不变"；
  等价性契约测试 `harness-plugin/tests/replayBridgePlan.test.mjs`（2 条）钉住
  「ds_replay 的桥调用计划 == 引擎脚本的调用序列」（北单：analyze→settle→factor-induction，
  无 prepare；竞彩：prepare-range→prepare→analyze(jingcai_only)→settle→induction）。
  ⚠️ DSH 启动时 import 插件、Node 模块缓存不会因改盘上文件失效 → **改完 replay.js 必须重启
  DSH 会话**才生效（本次踩过：旧模块把北单跑成了竞彩）。

两条等价入口（同一沙箱模型、同一引擎调用）：
```bash
# 引擎脚本（改完即生效，无需重启）
python3 -m scripts.run_beidan_loop --day 2026-07-11 --dog bcl狗
# 斗狗场薄壳：ds_replay（需重启 DSH 以加载新 replay.js）
#   ds_replay(dog="bcl狗", start="2026-07-11", end="2026-07-11", mode="interactive")
```
唯一差异：ds_replay 在周期边界（`factor_review_every`，默认 7 天）多做一次**因子退役**，
属额外能力，不改变单日分析/结算/归纳结果。

---

## 4. 配置现状（**live 与沙盒不同，注意**）

| 键 | live `bc狗` | 沙盒 `bcl狗` |
|---|---|---|
| mode / max_legs / max_combos / max_stake_pct | flex / 17 / 512 / 10% | 同 |
| `pool_gate.mode` | **enforce**（已生效！） | enforce |
| `pool_gate.gl_classes` | `["gl0"]` | `["gl0"]` |
| `pool_gate.observe_gl_classes` | 走默认 `["glN"]`（深合并） | `["glN"]` |
| `gate_basis` | 走默认 **`"x"`**（新默认） | `"x"` |
| `selector` | 默认 `"llm"` | `"llm"` |
| `ticket_mode` | 默认 **`"llm"`（仍会调 stage2）** | **`"rule"`** |
| `x_cap` | 无（0） | `1.45` |
| 资金 | 298.31（小，10% = 29.8 元） | 5000 |
| persona | 只加了「奖池门」事实一节，**策略未重写** | 沙盒版草案（已含"盘口不是纪律/多选授权/x 非越高越好"） |

⚠️ **live bc狗 现在是"门已生效 + 成票锁 x + 仍走 stage2"的状态**；沙盒验证用的 `ticket_mode="rule"` 还没同步到 live。
这是当前最大的"线上/沙盒不一致"，要么把 live 也切 `rule`，要么先把 live 门降级为 `shadow`。

---

## 5. 运行手册

```bash
cd ds_agents/python-engine

# 账本
python3 -m src.pool_ledger update --role bcl狗 --days 14    # 日常（settle 里也会自动跑）
python3 -m src.pool_ledger update --role bcl狗 --all        # 回填/补漏
python3 -m src.pool_ledger report --role bcl狗              # 各桶 y / CI / 打平关数

# 只看 prompt（不调 LLM，逐段 review 用）
python3 -m src.beidan_parlay_dog prompt 2026-08-15 --user bcl狗 --out docs/prompts

# 真 LLM 沙箱跑一天（stage1 → 规则组装 → 可选结算）
export DEEPSEEK_API_KEY="$(grep -E '^export DEEPSEEK_API_KEY=' ~/.zshrc | tail -1 | sed 's/^export DEEPSEEK_API_KEY=//' | tr -d '"'"'"' ')"
python3 -m scripts.run_sandbox_day 2026-08-15 --settle          # 默认狗 bcl狗，沙箱零线上影响

# 真 LLM 沙箱跑「单日完整 loop」（分析 → 结算含反思 → 因子归纳），观察因子飞轮
python3 -m scripts.run_beidan_loop --day 2026-07-11 --dog bcl狗
python3 -m scripts.run_beidan_loop --day 2026-07-12 --dog bcl狗 --sandbox /tmp/beidan_loop_bcl狗   # 复用沙箱续跑

# 离线实验（0 LLM）
python3 -m scripts.mispricing_buckets --md docs/mispricing_bucket_report.md
python3 -m scripts.line_convert_check
python3 -m scripts.rule_arm_backtest --ns 3,5,9 --md docs/rule_arm_report.md

# 测试
python3 -m pytest tests/ -q                                  # 142 passed
```

产物位置：真跑的 prompt+回复在 `docs/prompts/llm_run_<day>/`；账本在 `data/roles/<狗>/memory/pool_ledger.json`。

---

## 6. 已验证 / 未验证 / 已知限制

**已验证**
1. 错价档位（gl0×x≥1.1）与让球盘无边际（两口径 + 书商同盘口报价交叉验证）。
2. 让球线换算覆盖 100%、精度 0.026；`result` 口径 100% 正确。
3. 回放切片按场/按波正确（prompt==切片）；波次整点开赛的场次已按分钟粒度排除（§3.6）。
4. 账本：幂等、as_of 防未来、SP 滞后回看补记、滚动窗口、自动降级（无边际→空仓）。
5. 两道门 + 规则组装 + 锁 x：单测 142 条；07-11 真跑与 LLM 组装结果一致。
6. 机械组装**会**双选（07-11 1 例；08-15 抽样 22 例，占过门场次 ≈23%）。

**未验证（重要）**
1. **因子作用完全没看到**：bcl狗是 0 因子。要看因子必须先**连续跑一段**（如 08-01→08-15），
   让狗用自己的结算样本 reflect/归纳出因子与校准 k，再看它是否改变 veto/选腿。
   —— **2026-09-11 已跑通单日 loop（见 §3.7）：飞轮转得起来，但 1 天只有 2 个因子/3 个样本，
   要回答"因子是否改变选腿"仍需连续跑一段。**
2. 规则组装 vs LLM 组装的**量化对照**（同期、同门、同预算）还没跑（只做了两天人工对比）。
3. `x_cap=1.45`、腿数>9 的取舍规则（现为 stage1 顺序）**未做敏感性回测**。
4. 仓位/资金规则未定（现在只有 `max_stake_pct=10%`；每波 1 票）。

**已知限制/坑**
* 票级结果方差极大（9 关 ≈ 5e-5 中奖率）→ 不能用单日/单周盈亏判断策略。
* `stage1` 分块数 = `ceil(场次/20)`：大日子一天 13 次调用、~40 万 input tokens（08-15 实测 276s）。
* DeepSeek 会余额耗尽（本轮踩过）→ 跑前先确认余额。
* 同一场次在两个波次会重复出票（用户已确认：**允许重复**）。
* **时间比较一律按分钟**（19 字符的 `match_time` vs 16 字符的时刻）：新增任何时间过滤都要 `[:16]`，
  否则会重现 §3.6 的整点误入问题。
* 波动型因子现在只在 stage1 作 veto 提示，不再产生方向/多选（已从 stage2 删除）。
* 老的 factor_memory/校准与本文档链路无关（bcl狗 0 因子；live bc狗 有 82 因子但未参与新链路验证）。

---

## 7. 待决 / 下一步（按建议顺序）

1. **live 与沙盒对齐**：bc狗 是否切 `ticket_mode="rule"`？若不切，先把 live `pool_gate.mode` 降 `shadow`
   直到沙盒验证完成（现 live 是 enforce + 锁 x，会明显减少出票）。
2. **人设瘦身**：stage1 只做「逐场判断 + veto + 归因」，人设里 ROI 数学/票型/串长/成本这些属于引擎规则的内容可删。
3. **连续跑一段（最关键）**：例如 `08-01→08-15` 逐日 `run_sandbox_day --settle`，让因子从 0 长起来，
   再回答"因子这一轮产出什么、如何改进分析"。
4. **规则组装 vs LLM 组装的对照**（量化 stage2 是否真的可以永久删除 —— 目前证据是"两天一致"，样本小）。
5. 仓位/资金规则设计（Kelly 一类），以及 `x_cap`、腿数取舍的敏感性回测。
6. 可选：给 stage1 用 fast 模型 / 提高 batch 到 40，压成本。

---

## 8. 隔离与安全（红线）

* 所有新逻辑**只对 `path=="beidan"` 的北单串关生效**；其它狗（竞彩/单关）prompt 与统计逐字节不变。
* `pool_gate.mode="off"` 时行为与改造前一致（有测试守住）。
* 回放/沙箱：临时角色目录 + 切片源 + 账本 `as_of`，**不偷看未来**（07-11 审计：0 个比分/开奖码/SP 出现在 prompt）。
* 线上角色文件本会话只动过：`data/roles/bc狗/parlay.json`（门槛/门配置）、`data/roles/bc狗/persona.md`（加「奖池门」事实一节）、
  `data/roles/bc狗/memory/pool_ledger.json`（账本回填）；**bc狗 的因子库/订单/资金未改**。
