# bc狗 后续开发文档

> 状态：开发计划（数据链路已打通，狗已实现 `src/beidan_parlay_dog.py`，因子测试回测开发中）
> 日期：2026-08-27
> 目标：在 `chuan_guan_dog.py`（竞彩串关狗）基础上，派生一只**bc狗**，主打长串（6+串）+ 每腿 1~2 个选项（双选），并以北单**开奖SP**结算。
> 因子测试的并行回放计划见 [`beidan_factor_parallel_plan.md`](beidan_factor_parallel_plan.md)，通用因子并行化设计见 [`factor_parallelism_plan.md`](factor_parallelism_plan.md)。

---

## 一、数据链路（已打通 ✅）

```
spider_monorepo 北单爬虫 → spdex 库 Beidan* 表（goal_line/赔率历史 + 开奖sp）
        │
        ▼
deepseek_lota v2-api /predictions/api/v2/matches/?is_beidan=true
        │  每场带 beidan_info：goal_line + home/draw/away_odds + result(3/1/0) + spvalue(开奖SP)
        ▼
ds_agents src/data_manager.py
        │  DataManager.refresh_beidan_history(days=60) → data/beidan/{足球日}.json
        ▼
bc狗（本计划，待开发）
```

- **关联键**：`lota_id` → `BeidanMatch.lota_id` → `beidan_id`（`期号_场次`）→ `BeidanOdds`（最新让球胜平负）+ `BeidanDraw`（开奖sp）。
- `beidan_number`（如 `313`）只是场次号，**不能**直接当关联键；必须走 `lota_id → BeidanMatch.beidan_id`。
- 数据只读 `spdex` 库（deepseek_lota `spdex` 别名），竞彩 HHAD 已在 `client_api._get_jc_hhad_map` 用了同样的 `using('spdex')` 模式。
- 缓存刷新：`python3 -m dsfootball_cli beidan [天数]`（默认 60）→ 写 `data/beidan/*.json`。
- 已回填验证（2026-08-27）：60 天 → 61 个足球日 / 2025 场，**2000 场带 `beidan_info`**（含 goal_line、胜平负赔率、result），**1912 场有 `spvalue`**（未开奖/无SP的场次无 spvalue，属正常）。

---

## 二、北单 vs 竞彩（串关差异）

| 维度 | 竞彩串关狗（已有 `chuan_guan_dog`） | bc狗（本计划） |
|---|---|---|
| 数据源 | `matches` 缓存 + `jc_hhad` | `beidan` 缓存 + `beidan_info` |
| 赔率类型 | 固定奖（jc_hhad 胜平负赔率） | **开奖SP**（浮动奖，赛后定） |
| 让球线 | `jc_hhad.goal_line`（整数让球） | `beidan_info.goal_line`（整数让球胜平负 / 亚盘分数） |
| 结算依据 | 实际比分 `score` + goal_line 推 H/D/A | **官方开奖 `beidan_info.result`（0=负/1=平/3=胜）** + `spvalue` |
| 每腿选项 | 单边（隐含概率最高一边） | **支持 1~2 个选项（双选）**，主推长串 6+ |
| 玩法 | 胜平负（让/不让球） | 让球胜平负 / 总进球 / 比分 / 半全场（可扩展） |

北单**过关**结算要点：一张过关单（N串1 / N过M）全额命中所选场次的**开奖结果**才中奖；奖金 = 各命中腿 `spvalue` 连乘 × 投注额（北单为浮动奖，开奖SP即为 SP 值）。每腿双选时，该腿命中任一已选项即算中。

---

## 三、北单数据字段（`beidan_info`）

| 字段 | 含义 | 示例 |
|---|---|---|
| `beidan_id` | `期号_场次` | `26069_49` |
| `goal_line` | 让球数（负=主让，正=主受） | `-1` / `0` / `1` |
| `home_odds/draw_odds/away_odds` | 让球胜平负赔率（最新） | `2.78 / 4.10 / 2.51` |
| `odds_update_time` | 赔率更新时间 | ISO |
| `result` | 开奖结果 `3/1/0`（胜/平/负） | `1` |
| `result_des` | 中文开奖 | `平` |
| `spvalue` | **开奖SP**（唯一中奖项） | `4.1021` |
| `score` | 最终比分 | `1:1` |
| `draw_datetime` | 开奖日期 | `2026-08-25` |

未开奖/未匹配的场次　`result/spvalue` 为空。

---

## 四、参考模板：`chuan_guan_dog.py` 的复用点

直接继承 `ChuanGuanDog`/`Agent`，覆写数据读取与结算即可：

| 方法 | 竞彩实现 | 北单改写 |
|---|---|---|
| `_jc_matches(day_date)` | 读 `matches` 缓存 + `get_cached_jc_matches` | `_beidan_matches(day_date)`：读 `data/beidan/*.json`（`get_cached_beidan_matches`），只留 `beidan_info` 非空 |
| `_jc_hhad_odds(match)` | `match["jc_hhad"]` | `_beidan_odds(match)`：`match["beidan_info"]` → `{goal_line,h,d,a}` |
| `_score_leg(match)` | 单边概率最高 | 支持**双选**：返回 `picks: ["H","D"]` + 对应 `sp（/odds）` |
| `_build_slips(legs, tickets)` | 单边组合 `combinations(legs,m)` | 需处理**双选**的笛卡尔积（每腿可选集合），`N串1` 展开为所有"每腿挑一"的组合 |
| `_settle_one(order, scores)` | 用比分+goal_line 判 H/D/A | **改用 `beidan_info.result`/`result_des` 判定命中**，命中即奖；奖金用 `beidan_info.spvalue` |
| `_fetch_scores(day)` | 从 features/缓存取比分 | 结算已由开奖 `beidan_info.result` 提供，无需再单独取比分 |

---

## 五、开发步骤（TODOs）

1. **数据接入**：`_beidan_matches(day_date)` 读取 `DataManager.get_cached_beidan_matches`（配合 `refresh_beidan_cache` 刷新），过滤 `beidan_info` 为空、已开奖前不纳入未开赛池。
2. **欧赔兜底**：北单让球胜平负赔率缺失时，用 `DataManager.get_odds(lota_id)['eu']` 近似（同竞彩 `_jc_odds` 的 `JC_ODDS_FACTOR` 逻辑，返还率换算）。
3. **双选腿**：`_score_leg` 改为允许每腿保留 1~2 个高概率项（`TOP2` 或按隐式概率阈值 `MIN_CONF` 卡），返回 `picks` 列表；`MIN_ODDS/MAX_ODDS` 过滤沿用。
4. **组票（双选串）**：`_build_slips` 支持 `legs[i]['picks']` 多值，`N串1` 用笛卡尔积展开子单，`N过M` 先取 `C(N,M)` 腿组合再对每组合做笛卡尔积；每子单 `odds` = 所选各腿 `spvalue`（未开奖用当前赔率占位）连乘。
5. **结算**：`_settle_one` 判定条件 `leg['pick'] in {leg_hit_options}`（其中 `leg_hit_options` 由 `beidan_info.result_des` 归一），命中奖金 = `bet_size × prod(spvalue of used legs)`；`draw/走水` 北单一般无，仍需处理 `status` 异常。
6. **资金/仓位**：复用 `ChuanGuanDog` 的 `_martingale_stake`/`ROR` 逻辑与 `Role`（`data/roles/bc狗/`，独立角色/资金/订单）。
7. **回测口径**：北单回测必须用**开奖sp**（`spvalue`）而非开赛前赔率，否则中奖奖金失真；`backtest` 需要「先 analyze(选腿) → 赛后再 settle(用开奖result+sp)」的时序。
8. **数据补全**：对 `beidan_info` 为空（`lota_id` 未关联）的场次，检查 `BeidanMatch.lota_id` 是否需要后台回填（`predictions/management/commands/beidan_match_fix.py` 已有类似逻辑）。

---

## 六、待确认 / 风险

- **北单过关奖金公式**：需要与出票平台口径对齐（浮动奖 SP 连乘、是否含固定奖金基数、2串1 以上是否打折），建议先找一份北单过关开奖实例核对。
- **双选的处理**：北单是否允许同一场双选串关、双选在过关里如何计奖（通常按"命中即中"），需要按平台规则确认。
- **场次覆盖**：2025 场中仅 2000 场有 `beidan_info`（25 场缺 `lota_id` 关联），占比小但会影响长串命中率；建议在狗中加入"仅用可结算场次"的约束。
- **实时 vs 回放**：北单开奖在赛后产生，`live` 模式（未开奖）只能拿到当前赔率占位，`settle` 必须等 `result/spvalue` 就位。

---

## 七、一句话启动方式

```bash
# 1) 刷新 60 天北单缓存（已实现）
python3 -m dsfootball_cli beidan 60

# 2) 参考现有竞彩串关狗（待实现的北单版）
# python3 -m src.beidan_parlay_dog analyze 2026-08-27
```
