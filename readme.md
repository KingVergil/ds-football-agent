# ds_agents — 多智能体足球投注决策系统

基于 LLM 的多 Agent 架构，每个 Agent（"狗"）独立分析比赛、产出订单，日终统一结算并反馈因子表现。

## Agent 列表

| Agent | 说明 |
|-------|------|
| alpha2狗 | - |
| alpha狗 | - |
| 梭哈2狗 | - |
| 梭哈3狗 | - |
| 平局狗 | - |
| 跟风狗 | - |
| 均注狗 | - |

## 命令

### `analyze` — 分析比赛并下单

```bash
./batch_agents.sh analyze live          # 当前足球日
./batch_agents.sh analyze 2026-07-22    # 指定日期
```

**运行时机**：尽量在比赛开赛前 1 小时内跑。例如当天有 18:30 / 22:00 / 01:00 三波比赛：

- **18:00** 跑 → 只看 18:30 的比赛
- **21:30** 跑 → 只看 22:00 的比赛
- 以此类推

> **注意**：早跑可能会提前产出后续波次的订单，但下一波再跑时会对未开赛比赛**退单重算**。比赛一旦开赛，analyze 不再退单。

### `settle` — 结算已完成比赛

```bash
./batch_agents.sh settle 2026-07-21    # 结算指定日期
./batch_agents.sh settle live          # 结算上一个足球日
```

**核心原则**：
- 只有 `state == 6`（完场）的比赛才会被结算
- **统一第二天结算前一天**，不要当天边分析边结算——仓位预算是按天统计的，混着来会滚仓

### `dashboard` — 刷新数据并打开看板

```bash
./batch_agents.sh dashboard
```

拉取最新数据后自动打开 `lota_data/dashboard.html`。

### `factor-review` — 退役因子

```bash
./batch_agents.sh factor-review 2026-07-21
```

**运行频率**：每周一次，或连黑后立即执行。用于检查因子表现并退役失效因子。

## 足球日约定

本项目使用**足球日**而非自然日作为时间边界：

- **窗口**：`[D 12:01, D+1 12:00]`
- 12:00 之前的比赛属于**前一个**足球日
- 这是比赛分组、结算窗口、仓位预算的统一基准

## 操作流程

### 实盘日常

```
20:00  → ./batch_agents.sh analyze live   （第一波，靠近赛前）
22:30  → ./batch_agents.sh analyze live   （第二波，数据更新后）
第二天 → ./batch_agents.sh settle live     （统一结算前一天）
```

### 回测

```bash
./batch_agents.sh analyze 2026-07-20
./batch_agents.sh settle 2026-07-20
```

## 关键注意事项

- **时区**：所有 `match_time` 均为北京时间 (UTC+8)，内部通过 `_now_bj()` 统一获取当前北京时间
- **盘口符号**：Prompt 和结算层统一使用主队视角（受让=正，让球=负）
- **订单去重**：以 `(lota_id, bet_type)` 为 key，同一场可同时下亚盘和大小球
- **Live 数据刷新**：未开始比赛的 compact-fet 数据需 `force=True` 强制刷新，避免使用数小时前的旧赔率
