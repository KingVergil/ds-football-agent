---
name: analyze-all
description: 一键并行跑全部 7 只单关狗（不含串关2狗）的足球投注分析下单：ds_analyze_all_parallel fan-out 7 个独立 subagent，每狗执行 refresh_orders → lota 读数据 → 读人设/记忆/资金 → 独立判断 → submit_orders。适用于用户说「全部分析」「跑全部狗」「分析7狗」「全量分析」时。
---

# 全部分析（7 只单关狗）

对 7 只单关狗跑完整分析工作流。串关2狗归 Python 侧 chuan_guan_dog 管，跳过。

## 目标狗

alpha2狗、alpha狗、梭哈2狗、梭哈3狗、均注狗、平局狗、跟风狗（共 7 只）。

## 步骤（subagent fan-out 并行，每狗独立会话）

1. **定足球日**：北京时间 ≥12:00 用今天，<12:00 用昨天。
2. **并行 fan-out**：调 `ds_analyze_all_parallel(day=<足球日>, parallel=7)`。该工具会为每只狗启动一个独立 subagent，并发执行 refresh_orders → 读数据 → 读人设 → 读记忆/资金 → 独立判断 → submit_orders。
3. **汇总检查**：工具返回每狗 `ok/stopReason/text`；若某狗 `ok=false`，单独重跑该狗（`ds_analyze_all_parallel(dogs=["<狗名>"], parallel=1)` 或按步骤手动分析该狗）。

> 不要父 agent 自己顺序逐狗分析——把并发交给 `ds_analyze_all_parallel`。

## 关键约定

- 金额 = 信心比例 × `ds_capital_js` 返回的 `full_capital`。比例档位以 persona.md 为准（最有信心 30–40% / 次之 15–20% / 试探 5–10%）。
- handicap 用「主队视觉：负=主让、正=主受让」（主让半一写 -0.75，主受半球写 +0.5，平手写 0）。
- 每单字段：lota_id / bet_type(亚盘|大小球|胜平负|让球胜平负) / pick(H|A|D|over|under) / odds / handicap / bet_size / reason。
- 平局狗无干净信号就休息（0 注）；均注狗每场必下；梭哈2/3狗必下 2–4 注；alpha系凯利负期望就 skip。
