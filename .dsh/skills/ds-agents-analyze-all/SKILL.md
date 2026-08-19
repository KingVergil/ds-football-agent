---
name: analyze-all
description: 全部分析的执行入口是斗狗场（dashboard）的「⚡ 分析」按钮（POST /ds-run → python 桥，不经 LLM 工具面板）。适用于用户说「全部分析」「跑全部狗」「分析7狗」「全量分析」时——引导用户去斗狗场点按钮。
---

# 全部分析（7 只单关狗）

薄壳架构下，固定流（含分析）**没有 LLM 工具**：执行入口只有斗狗场表单
（每狗行的「⚡ 分析」按钮 → `POST /ds-run {dog, func:"analyze", day}` → 插件直接
spawn `python -m src.bridge`，python 引擎内完成 数据准备→人设/记忆/资金→一次 LLM 决策→下单）。

## 你该做什么

1. 用户要「全部分析」时，告诉他去斗狗场（dsh web 仪表盘）逐狗点「⚡ 分析」；
   狗列表由本地角色派生（`python -m src.role_registry live`，默认 7 只单关狗：
   alpha2狗/alpha狗/梭哈2狗/梭哈3狗/均注狗/平局狗/跟风狗；公开克隆零狗时先创建狗）。
2. 你的工具面板只有只读数据工具（`lota_matches` / `lota_match` / `lota_sections` / `lota_status`），
   可用于回答数据/状态类问题，**不要尝试用 bash/文件操作复刻分析流程**。
3. 进度与结果看任务徽章（/ds-tasks）：每只狗「下单 N 单 / 余额」会出现在任务记录里。
