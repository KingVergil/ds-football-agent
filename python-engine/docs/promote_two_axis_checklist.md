# 「两轴排序」转正准备清单（2026-09-15）

> 状态：**已准备就绪，等你拍板**。沙箱已改成 harness 可转正的目录形状，
> 但**尚未执行转正**（转正会替换线上角色，属不可逆动作）。

---

## 0. 转正机制（harness 现状，`harness-plugin/replay.js:293 promoteSandbox`）

```
1. 取沙箱 workspace（必须有 <狗>.json）
2. 把【线上角色目录】整目录备份到 backups/promote_<目标狗名>_<时间戳>/
3. 删掉线上角色目录 → 把 workspace 整个复制过去
4. 角色状态置 live（enabled=true），注册表同步；若改名则 <狗>.json → <目标>.json
```

入口：dashboard 的「转正」按钮 / `ds_promote_sandbox` 工具 / `POST /ds-sandbox/<沙箱>/promote`。

---

## 1. 沙箱现状（要转过去的东西）

```
python-engine/data/replays/sandboxes/94狗_两轴0807/
  session.json          status=done，08-07~09-01，资金 47141
  workspace/            ← 转正会把这个目录整份覆盖线上角色
    95狗.json            scope=beidan, status=live, capital=47141, **订单 20 张**
    parlay.json          ticket_mode=rule, ticket_m=4, max_legs=17, max_per_leg=1
                         ⚠️ 实验开关：honor_stage1_veto=false / min_ticket_v=0 /
                            x_cap=0 / max_combos=5000 / min_leg_v=0
    persona.md           北单「奖池错价」策略狗（✅ 与 ticket_m=4 口径一致）
    memory/factor_memory.json   **只有 10 条两轴因子** + 按日账本（axis_samples）
    factors/fac_*.json    10 个定义
```

### 会被替换掉的线上角色（必须先确认目标）

| 线上角色 | 现状 | 转正后会变成 |
|---|---|---|
| **95狗** | status=live，资金 **5000**，订单 0，**因子 131 条** | 沙箱那份：资金 47141、订单 20、**因子只剩 10 条** |
| bc狗 | status=paused，资金 298.31，订单 16，因子 85 条，`ticket_mode` 未设（走 llm + stage2） | 若转到这里会被整体替换 |

---

## 2. 转正前必须拍板的 4 件事

| # | 事项 | 现状 | 建议 |
|---|---|---|---|
| 1 | **订单与资金** | 沙箱带 20 张实验订单、资金 47141（实验起点 50000，−5.7%） | 转正前**清空 orders、资金设成线上起始额度**（否则线上第一天就带着实验战绩与 4.7 万本金） |
| 2 | **实验开关** | `honor_stage1_veto=false`（忽略 LLM 逐场否决）、`min_ticket_v=0`（无出票线保护）、`x_cap=0`、`max_combos=5000`、`min_leg_v=0` | 你已定"唯一的门是腿数<5" ⇒ 保留；但要清楚这**放弃了 ROI 门与 LLM veto 的兜底** |
| 3 | **因子库** | 只有 10 条两轴因子（线上 95狗 原有 131 条） | 确认这是想要的（旧因子不再参与）。若要保留部分旧因子，转正前手工合并 factor_memory |
| 4 | **目标狗名** | 沙箱狗叫 `95狗` | 转正到 `95狗` = 原样；转到别的名字会重命名角色文件 |

---

## 3. 转正步骤（推荐顺序）

```bash
cd /path/to/ds-agents/python-engine
SB=data/replays/sandboxes/94狗_两轴0807/workspace

# ① 先做一次"干净化"（清订单、资金回线上口径）—— 复制一份待转正的 workspace
cp -R "$SB" /tmp/promote_95狗
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/tmp/promote_95狗/95狗.json'); d = json.loads(p.read_text())
d['orders'] = []                       # 不带实验订单
d['capital'] = float(d.get('initial_capital') or 5000)   # 或线上额度
p.write_text(json.dumps(d, ensure_ascii=False, indent=1))
print('已清理：orders=0 capital=', d['capital'])
PY

# ② dump prompt 复核（0 LLM）：确认 10 条因子、门、票型都对
DS_ROLES_ROOT=/tmp/promote_95狗 DS_BACKTEST_FET=1 \
DS_FET_TXT_ROOT=/path/to/fet_txt \
DS_BACKTEST_WAVES_WEEKDAY=16:45 DS_BACKTEST_WAVES_WEEKEND=16:45,22:30 \
python -m src.beidan_parlay_dog prompt 2026-09-01 --user 94狗 --out /tmp/promote_prompts

# ③ 走 harness 转正（dashboard 按钮 / ds_promote_sandbox / POST /ds-sandbox/<沙箱>/promote）
#    等价手工操作：备份 data/roles/<目标> → 覆盖 → 角色置 live+enabled

# ④ 转正后第一天：dry-run 一天（不下单），对比线上新口径
DS_ROLES_ROOT=$PWD/data/roles/95狗 DS_BACKTEST_FET=1 \
DS_FET_TXT_ROOT=/path/to/fet_txt \
python -m src.beidan_parlay_dog analyze <今天> --llm --dry-run --user 94狗
```

---

## 4. 转正后要盯的三件事

1. **每天的 A/B 继续跑**（`scripts/axis_rank_ab.py`，0 LLM）：线上口径 = B 臂，stage1 序 = A 臂，随时比对。
2. **因子账本**：每天结算后自动写入 `axis_samples`（带日期）。每 7 天按约定做一次因子产出。
3. **门的行为**：现在只剩"腿数<5"，小日子会频繁空仓（实测 31 波里 10 波空仓）—— 这是设定，不是故障。

---

## 5. 风险提示（转正前看一眼）

- **不可逆**：转正是"整目录替换"+备份，备份在 `data/backups/promote_*`；要回滚就从备份还原。
- **单狗红线**：本轮所有代码改动都在北单路径或新文件；`factor_select._axis_profile()` 只对带
  `axis_samples` 的因子生效 ⇒ 其它狗行为不变。转正**不会**影响其它狗。
- **票级样本仍小**：样本外 28 票（B 中 12），ROI +129% 里最好一波占 33%（抽掉它 +59%）。
  建议线上先**小注额跑 2 周**再谈放量。

---

*生成时间 2026-09-15。相关：`docs/handoff_two_axis_analysis.md`（链路与结果）、
`docs/beidan_data_gaps.md`（数据缺口）。*

---

## 6. ✅ 已执行（2026-09-15 21:45）

| 项 | 结果 |
|---|---|
| 清洁化 | 订单 **20 → 0**；资金 **47141 → 5000**（`initial_capital=5000`）；predicts 清空；**因子 10 条 + 按日账本全部继承** |
| 备份（清洁化前） | `data/backups/two_axis_ws_before_promote_20260915_214541` |
| 转正（harness） | `promoteSandbox(cacheDir=python-engine/data, sandbox=94狗_两轴0807, dog=95狗, to=95狗)` → `ok:true` |
| 备份（线上旧角色） | `data/backups/promote_95狗_2026-09-15T13-45-46` ← **回滚从这里还原** |
| 转正后线上角色 | `data/roles/95狗`：资金 5000 / 订单 0 / status=live / enabled / 因子 10 条（10 条带账本）/ B 方案配置 / 北单 persona |
| 注册表 | `data/dogs.json` 的 95狗 已同步（enabled=true） |

### 数据源验证（live vs 回测）

| | 线上 95狗 | 沙箱（回测） |
|---|---|---|
| 数据来源 | **live**（不带 `DS_BACKTEST_FET` / `DS_FET_TXT_ROOT`） | 本地切片目录 `deepseek_lota/data/runtime/fet_txt` |
| 运行特征 | 无 `[backtest-fet] 切片源…` 行 | 有 `[backtest-fet] 切片源…` 行 |
| 实测 | 2026-09-15 dry-run：北单 34 场、2 个 stage1 batch、两轴排序生效、腿数 4 → 空仓 | 同结构 |

**分析 md 结构对比（线上 vs 沙箱）**：章节完全一致
（人设 → 因子清单（方向/波动）→ 本场判据 → 输出 → 本块场次 → stage1 → Summary），
仅资金与 token 数不同 ⇒ **和转正前没有区别**。

> 观察：`最发散侧优势` 的累计方向边际随样本增长变成 **−1.5pp（465 腿）**，
> 于是被账本放进「反向因子」组（它原本是 select）。这正是账本机制在起作用 ——
> 该因子在更多样本上没能守住，排序时会被当负分。名字与角色的标注不一致问题记在 §5。
