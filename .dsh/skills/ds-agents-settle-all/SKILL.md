---
name: settle-all
description: 一键结算全部 7 只单关狗（含归因反思）+ 自动因子归纳。适用于用户说「全部结算」「结算7狗」「跑结算」「结算+反思」时。
---

# 全部结算（含归因）+ 因子归纳

对 7 只单关狗（不含串关2狗）结算 → 反思归因 → 因子归纳。串关2狗归 Python 侧，跳过。

## 步骤

1. **结算**：`ds_settle_js(user, day) × 7` —— 取 state==6 完场比分，结算未结算订单，返回含 hit/profit/score/bet_size/reason 的 orders 列表。
2. **归因反思**：把每只狗 `ds_settle_js` 返回的 `orders` 原样作为 `settled`，调 `ds_reflect_js(user, day, settled) × 7` —— 旁路 LLM 因子发现 + 写回 ds_factors/ds_reflections。
3. **因子归纳**：`ds_factor_induction(user="alpha")`（alpha 跨狗 1 次进全库）+ 对非 alpha 4 狗（梭哈2狗/梭哈3狗/平局狗/跟风狗）各自 `ds_factor_induction(user=狗名)`。

## 说明

- 若某狗 `unsettled=0`（无待结算单），`ds_settle_js` 返回空，该狗反思可跳过。
- `ds_reflect_js` 的 settled 参数直接用 `ds_settle_js` 返回的 orders（勿改字段名/顺序）。
- `ds_reflect_js` 只有比分/方向时 LLM 倾向拒绝硬凑因子，属正常；样本厚的狗（均注/跟风）才会给候选因子。
