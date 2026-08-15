---
name: factor-dedup
description: 一键因子归纳去重 + 判重 + 退役评估。适用于用户说「因子去重」「因子归纳」「因子退役」「因子判重」时。
---

# 全部因子去重（归纳 + 判重 + 退役）

对 7 只单关狗做因子归纳去重与退役。串关2狗归 Python 侧，跳过。

## 步骤

1. **因子归纳去重**：`ds_factor_induction(user="alpha")`（alpha 跨狗 1 次进全库）+ 非 alpha 4 狗（梭哈2狗/梭哈3狗/平局狗/跟风狗）各自 `ds_factor_induction(user=狗名)`。
2. **因子判重**（可选）：对候选新因子调 `ds_factor_dedup(user, factor_id, desc)` → 返回 create/merge/suppress。
3. **因子退役**（可选）：`ds_factor_review_js(user, end_date, start_date?)` —— 门控 14 天零触发休眠 + 低信息退役，旁路 LLM 结构性评估(retire/dormant/active)。

## 说明

- 归纳把「同模式重复」合并，方向相反一律保留（不硬并，避免把顺向/反打因子误并）。
- 退役 end_date 用评估窗口结束日（如今天），start_date 空 = 近 7 天。
- 若新因子与已有因子方向相反（如「离散冰点顺向」vs「离散冰点背离反打」），`ds_factor_dedup` 应判不重复并 create，而非 merge。
