# 95狗 逐日回放 —— 进度交接（2026-09-14 收工）

> 目的：回家接着跑剩下的，不用重新摸情况。**照 §2 的命令直接续跑即可。**

---

## 1. 一句话状态

北单串关狗 `95狗`（9过4 / D 路线）从 07-04 逐日跑到 **08-04**，**8 张单全部已结算**，
资金 **5000 → 7996.97（+60%）**，累计 **+2996.97**，因子库 **49 个**，引擎测试 **227 passed**。

**循环已停**（用户 20:30 收工）。续跑从 **08-04** 起（08-04 的 reflect 还没跑）。

---

## 2. 如何续跑（复制即可）

```bash
cd /path/to/ds-agents/python-engine

# ① key（本机非交互 shell 不继承 ~/.zshrc）
export DEEPSEEK_API_KEY=$(grep -m1 'DEEPSEEK_API_KEY=' ~/.zshrc \
  | sed 's/.*DEEPSEEK_API_KEY=//' | tr -d '"'"'"' \r')

# ② 波次用**代码默认**（工作日 22:30 / 周末 16:30,20:30）—— 不要覆盖
unset DS_NO_SKIP_REFLECT DS_BACKTEST_WAVES DS_BACKTEST_WAVES_WEEKDAY DS_BACKTEST_WAVES_WEEKEND

# ③ 沙箱（单狗平铺角色目录）
export WS="$PWD/data/replays/sandboxes/95狗_0703init/workspace"
rm -f "$WS/.d2d.lock"                       # 有残留锁会拒绝启动（单实例保护）
cp data/roles/95狗/parlay.json "$WS/parlay.json"   # 同步线上配置

# ④ 续跑：start=最后处理日，ndays=要跑几天
export D2D_LOG="$WS/day_by_day_review_round07.md"
python -m scripts.replay_day_by_day 2026-08-04 12
```

**纪律（重要，别再犯）**

- **只向前跑，绝不从07-01/07-04 重头跑** —— 因子库就在沙箱里，直接沿用。
  只有「配置修复导致返工」才重跑当天。
- 一次只跑**一个**循环（驱动有 `.d2d.lock` 单实例锁；两个循环会互相覆盖日志）。
- 每天 review 落在 `$D2D_LOG`，**每轮换一个文件名**（别再覆盖）。
- 这是长跑任务，**用后台 job** 跑，不要前台等。

---

## 3. 当前配置（D 路线，线上 `data/roles/95狗/parlay.json` 与沙箱已同步）

| 键 | 值 | 含义 |
|---|---|---|
| `selector` | `llm` | stage1 逐场同意/否决；腿集由 LLM 给 |
| `ticket_mode` | `rule` | **票型由引擎定，LLM 不得覆盖** |
| `ticket_m` | `4` | **固定每注 4 关** ⇒ n 腿 → `n过4`，n≥5 才出票 |
| `ticket_tolerance` | `0` | 让位给 `ticket_m` |
| `max_per_leg` | `1` | 每腿只买 1 侧（规则路径也已生效） |
| `max_stake_pct` | `0` | **= 100%（梭哈，用户口径）** |
| `max_combos` | `0` | = 保守默认 `C(n,4)` |
| `pool_gate.mode` | **`observe`** | **账本门只辅助、不拦票**（用户口径 D） |
| `pool_gate.default_theta` | **`1.11371`** | `= (1/0.65)^(1/4)`，4 关票的每腿平衡线 |

> ⚠️ `1.11371` 是**赛前代数**（只用返奖率 0.65 与关数 4），**不是泄漏**。
> 开奖 SP 那套（`beidan_high_vol` 的 `ratio`）只用于**赛后反思取样**，从不参与下注。

---

## 4. 成绩单（8 张单）

| # | 日期 | 票型 | 成本 | 结果 |
|---|---|---|---|---|
| 1 | 07-04 | 9过4 | 252 | **+2047.1** |
| 2 | 07-04 | 9过4 | 252 | −252.0 |
| 3 | 07-04 | 9过4 | 252 | +79.1 |
| 4 | 07-26 | 9过4 | 252 | **+1776.7** |
| 5 | 07-30 | 8过4 | 140 | −140.0 |
| 6 | 07-31 | 9过4 | 252 | −252.0 |
| 7 | 08-02 | 9过4 | 252 | −252.0 |
| 8 | 08-03 | 5过4 | 10 | −10.0 |

`8过4` / `5过4` 是**正确的自适应票型**（`C(n,4)×2` 对得上）。

---

## 5. 今日修掉的 8 个真 bug（都有回归测试守着）

1. **`x≡1.00` 静默失效** —— `_beidan_odds` 用 Pinnacle 兜底三路，而市场参考取的是**同一个源** ⇒ `x = p̂×(1/p̂) ≡ 1.00`，错价检测整体退化（实测 8 天空仓）。→ 改为「三路缺失即排除该场 + 计数告警」。
2. **912 元膨胀票** —— `max_combos/max_stake_pct=0` 被解释成「无限制」+ 双选腿放大 126→456 注。→ 先削 pick 再删腿；`max_per_leg` 在规则路径也生效。
3. **跨波重复下单** —— 同一天两波各自选腿，重叠 7/9。→ 本日已下单的场次跳过。
4. **票型出成 `9串1`** —— `ticket_m` 不在 `_load_parlay_config` 白名单，被静默丢弃。
5. **票型出成 `4串1`** —— 空票型用 `""` 表示空仓，下游回落成 `N串1`。→ 直接 `return []`。
6. **LLM 覆盖引擎票型** —— stage2 的 `ticket` 字段盖掉 `n过4`。→ `ticket_mode=rule` 时忽略。
7. **只落最后一个 stage1 batch** —— batch 1/2 响应全沙箱无落盘 ⇒ rank 对照只能看 1/3。→ 每批次落盘。
8. **决策落盘静默失败** —— 我加的 `input_idx` 引用了 `_select_legs_llm` 的**局部** `_input_idx` ⇒ `NameError` 被 `except: pass` 吞掉（08-02/08-03 的 `leg_decision` 静默缺失）。→ 自建 `_input_idx` + 失败不再静默。

---

## 6. 未定论 + 下一步

### ① rank 排序有没有用 —— **机制已证，价值未证**

| 层面 | 状态 |
|---|---|
| 机制 | ✅ 引擎腿序 = **rank 升序**（3 张单 `llm_rank` 全非降） |
| 数据 | ✅ stage1 全批次落盘（08-02：46/46 条全带 rank） |
| 价值 | ❌ 唯一可对照的 07-26 显示 **rank 1 的腿是错的**，命中腿 rank=`[2,3,4,6]` ⇒ **不支持"rank 靠前＝更易中"**（n 太小） |

**下一个出单的日子**会同时具备三样（全批次 rank + `leg_decision`(含 `input_idx`) + `llm_rank`），
那时做一次 A/B 即可定论：

```
A 臂：按 rank 升序取前 N（= 实跑）
B 臂：按 input_idx 升序取前 N
在同一批赛果上比 C(n,4) 盈亏
```

> ⚠️ 注意：**`n过4` 里腿序本身不影响盈亏**（C(n,4) 全买），rank 只在**候选 > n** 时才起作用
> —— 所以 A/B 必须在「候选 > n」的日子做。

### ② 观察样本淹没订单反馈

`factor_attribution` 逻辑**存在且单狗同款**（`run_reflect` 里订单→因子→回写 hit/profit→驱动退役）。
但真实订单只有 8 张，而观察样本（虚拟结算）每天几十条 ⇒ 统计被淹没
（如 `主让浅盘顺主` 75 次；`受让加深逆势主胜` 63次/60中=95% 明显虚高）。

**建议**：订单样本与观察样本**分开计数**，否则"迭代有没有改善"永远判不出来。

### ③ 账本口径去向

现 `observe`（不拦）。账本 `y_lo=0.966 < 1.0`，即**证据仍不足以证实正边际**。
要不要以后重新启用、门槛怎么定，未定。

### ④ 循环需**重启**才吃到最新修复

本次跑的驱动进程是**旧内存版本**，所以 08-03 的 `5过4` 被 R1 误报成异常
（R1 已改为按 `n过4` / `C(n,4)×2` 校验）。§2 的命令会启动新进程，自动生效。

---

## 7. 踩过的坑（防再犯）

| 坑 | 教训 |
|---|---|
| 离线复现引擎口径**失败过 3 次** | 引擎用**波次专属 fet 切片**（`backtest_fet.set_access_time`），与缓存 tags 不是一份数据。**别用离线探针预测"哪天能出票"**，以引擎日志为准。 |
| 从 session md 抽 rank **失败过 3 次** | prompt 含示例 JSON（`"lota_id":"Lota..."`）、JSON 是缩进的、花括号配平会被字符串里的 `}` 打断。**正解是引擎落盘**，不是 md 考古。 |
| `PoolLedger(path)` 不 `.load()` | 读到全 0。且注意路径相对基准（少一层 `python-engine/` 就读空）。 |
| 反复从起点重跑 | 白烧 token。**只向前**。 |
| 两个循环并发 | 后完成的会覆盖前者 review 日志。**单实例锁**已加，但记得 `rm .d2d.lock` 只在确认无进程时。 |
| 07-26 下单会话不是 `ls -t head -1` | 是 `191619`（`191856` 没下单）。查会话要**按资金增量**认，不按时间排序。 |

---

## 8. 关键文件

| 路径 | 用途 |
|---|---|
| `python-engine/scripts/replay_day_by_day.py` | 逐日驱动（单实例锁 + 唯一日志 + R1~R3 自动 review + 每波腿数） |
| `python-engine/src/beidan_parlay_dog.py` | 主引擎（今天全部修复都在这里） |
| `python-engine/tests/test_guardrail_defaults.py` | 今日新增的全部守卫（227 passed） |
| `python-engine/docs/ticket_nm_axis_study.md` | N×M 票型研究（含生存口径测算） |
| `python-engine/docs/reflection_split_review.md` | 人设分离 / 结算-反思解耦 review |
| `<WS>/day_by_day_review_D.md` | 本轮逐日 review（含每波腿数） |
| `<WS>/day_by_day_review_round*.md` | 以前各轮存档 |
| `<WS>/memory/leg_decision_<日>.json` | 决策落盘（有序候选 + rank + input_idx + 入选腿 + 剔除原因） |
| `<WS>/memory/stage1_resp_<日>_b<n>.json` | stage1 **每个批次**的原始响应 |
| `<WS>/95狗.json` | 资金 / 订单（腿里带 `llm_rank`） |
