---
name: factor-dedup
description: 因子归纳/退役的执行入口是斗狗场（dashboard）的「🧬 归纳」「🪦 Review」按钮（POST /ds-run → python 桥）。适用于用户说「因子去重」「因子归纳」「因子退役」「因子判重」时——引导用户去斗狗场点按钮。
---

# 全部因子去重（归纳 + 判重 + 退役）

薄壳架构下，因子流**没有 LLM 工具**：执行入口只有斗狗场表单
（「🧬 归纳」→ `POST /ds-run {dog, func:"factor-induction"}`；「🪦 Review」→
`POST /ds-run {dog, func:"factor-review", end, start}`；回放内周期退役由 /ds-replay 编排）。
判重/合并已并入 python 的 `factor-induction`（确定性清洗合并 + LLM 判重 + 补定义）。

## 你该做什么

1. 用户要「因子归纳/去重/退役」时，引导他在斗狗场点对应按钮（或在回放暂停卡片上编辑方向）。
2. 归纳结果看任务记录：合并数 / 补定义数 / LLM 判重次数；退役结果含 活跃/退役/休眠 计数与
   本周期变化（cycle_changes）。
3. 半交互回放暂停时，你的职责是起草「下一轮方向建议」供用户编辑（这是唯一 LLM 触达点）。
