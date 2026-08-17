# Python vs JS 回放行为对比

> 目标3 交付：同一范围分别跑 Python `runall` 与 JS `ds_replay`，对比轨迹/订单/结算/因子流。
> 对比脚本：`python-engine/scripts/compare_replays.py <reportA.json> <reportB.json>`（私有不入库）。

## 运行条件（本次实测）

| 项 | Python | JS |
|---|---|---|
| 命令 | `dsfootball_cli.py agent 梭哈2狗 runall 2026-07-25 2026-07-26 --jingcai --factor-induction-every 1 --factor-review-every 1 --user-notes "…"` | `ds_replay(start='2026-07-25', end='2026-07-25', dogs=['梭哈2狗'], factor_review_every=1, reset='zero', restore_after=true, user_notes="…")` |
| 天数 | 2（次日结算） | 1（settleDog 用缓存比分当日结算） |
| 起点 | reset=zero（1000） | reset=zero（1000） |
| 旁路 LLM | DeepSeek（默认） | deepseek-v4-flash |
| user_notes | 已注入因子退役 | 已注入因子退役 |

## 结果摘要

| 日期 | 引擎 | 下单 | 结算 | PnL | 资金 | 活跃因子 |
|---|---|---|---|---|---|---|
| 07-25 | Python | 4 | 0 | 0 | 0（满仓 4 单 ¥1000） | 0 |
| 07-25 | JS | 4 | 4 | -169.5 | 830.5 | 0 |
| 07-26 | Python | 4 | 4（次日结算） | +170 | 0 | 1 |

因子退役：两边都是 1 个候选、无调整（已应用保守 user_notes）。

## 一致的行为（框架层）

- 数据边界一致：双方都拿到 11/146 场竞彩（排除 135 场北单/无号），全缓存命中。
- 下单数一致：07-25 都是 4 单（同一批 11 场候选）。
- 因子流顺序一致：反思（产因子）→ 非 alpha 归纳 → 周期退役（用户意见注入），退役候选数一致（1）。
- 任务状态：JS 侧每个工具调用/回放阶段都写入 `tasks/status.json`；Python 侧轨迹由 `runall --replay-dir` 报告。

## 差异（预期内）

- **注额/选场不同**：Python 满仓（4 单 ¥1000）vs JS ¥900，具体 pick 也不同 → PnL 不同（+170 vs -169.5）。这是 LLM 决策差异，不是框架问题。
- **结算时点不同**：Python 按日循环"次日结算"（单日跑无法结算当天订单）；JS `settleDog` 结算所有有完场比分的未结算单（当日即可）。对比时 Python 需多跑一天。
- **因子数量**：Python 归纳出 1 个因子（深盘受让保护）；JS 反思后因子数进入 review 候选也是 1。数量一致，具体因子名可能不同。

## 结论

文档设计的流程（数据边界 → 分析 → 结算 → 因子流阶段0/阶段A/阶段C）两端行为对齐；
具体订单与 PnL 由模型决策和结算时点决定，属于预期差异。对比多个日期/多次运行取均值再看方向（参考 retest_runbook 的 ±13~22pp 噪声结论）。
