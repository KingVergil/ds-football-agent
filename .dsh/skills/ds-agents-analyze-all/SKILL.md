---
name: analyze-all
description: 一键跑全部 7 只单关狗（不含串关2狗）的足球投注分析下单：refresh_orders → lota 读数据 → 读人设/记忆/资金 → 独立判断 → submit_orders。适用于用户说「全部分析」「跑全部狗」「分析7狗」「全量分析」时。
---

# 全部分析（7 只单关狗）

对 7 只单关狗跑完整分析工作流。串关2狗归 Python 侧 chuan_guan_dog 管，跳过。

## 目标狗

alpha2狗、alpha狗、梭哈2狗、梭哈3狗、均注狗、平局狗、跟风狗（共 7 只）。

## 步骤（共用同一份赛前数据，每只狗独立判断）

1. **定足球日**：北京时间 ≥12:00 用今天，<12:00 用昨天。
2. **refresh_orders(user, day) × 7**：退回窗口内未开赛旧单（已开赛的保留）。
3. **读数据**：`lota_matches(day, strip_scores=true)` 列比赛。≤50 场必须逐场读全关键段落；>50 场按联赛/时间粗筛主流竞彩场次。逐场 `lota_sections(id, slugs=["fair-odds","asian-handicap-pinnacle","over-under-crown","betfair-buysell","discrete-odds"])`。
4. **读人设**：`read python-engine/data/roles/<狗>/persona.md`。
5. **读记忆 + 资金**：`ds_memory_js(user, day)` + `ds_capital_js(user)` × 7。
6. **独立判断**：结合活跃因子 + 已证伪护栏（勿只按直觉解读离散凝聚，历史「离散极低」可能是诱杀），按人设档位定信心比例。
7. **submit_orders(user, day, orders) × 7** 结构化下单。

## 关键约定

- 金额 = 信心比例 × `ds_capital_js` 返回的 `full_capital`。比例档位以 persona.md 为准（最有信心 30–40% / 次之 15–20% / 试探 5–10%）。
- handicap 用「主队视觉：负=主让、正=主受让」（主让半一写 -0.75，主受半球写 +0.5，平手写 0）。
- 每单字段：lota_id / bet_type(亚盘|大小球|胜平负|让球胜平负) / pick(H|A|D|over|under) / odds / handicap / bet_size / reason。
- 平局狗无干净信号就休息（0 注）；均注狗每场必下；梭哈2/3狗必下 2–4 注；alpha系凯利负期望就 skip。
