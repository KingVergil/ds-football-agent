---
name: ds-agents-customer-guide
description: 面向客户的 ds_agents 使用引导。适用于 dsh 会话里用户问「怎么用 / 有哪些功能 / 常见用法 / 新手入门 / 使用说明 / 怎么看比赛 / 怎么分析 / 怎么结算 / 怎么回放 / 怎么练狗 / 怎么发邮件 / 为什么没单 / 因子休眠」等引导类问题时，用本文档的口径介绍系统、给操作路径与关键设定，并路由到对应工具或技能。不适用于直接执行分析/结算本身。
---

# ds_agents 客户使用引导

本系统是足球投注分析平台（7 只单关狗 + 深度足球狗/梭哈北单狗/跟风北单狗 + bc狗）。执行入口只有两条：
「斗狗场」看板表单（`POST /ds-run`、`POST /ds-replay`）与 dsh 工具
（`lota_*` 只读工具、`ds_replay`、`ds_create_dog` 等）。不要用 bash / 文件操作
复刻固定流（薄壳约束）。

## 接待口径

1. 先问一句客户想干什么（看数据 / 分析 / 结算 / 回放 / 练新狗 / 状态），再给对应路径；
2. 给 2-3 步最小操作说明，别一次倒出全部细节；
3. 下单 / 结算 / 转正等写操作：确认客户意图后再执行；
4. 不展示密钥、内部备份路径、后台脚本等非客户信息。

## 功能地图（客户问什么 → 怎么答）

| 客户想 | 引导 |
|---|---|
| 看比赛 / 单场 / 数据段 | 只读工具 `lota_matches(date)`、`lota_match(lota_id)`、`lota_sections(lota_id, slugs)`；或打开斗狗场看板 |
| 看狗状态 / 资金 | `lota_status(dog)` 或看板 |
| 分析下单 | 看板每狗「⚡ 分析」（`POST /ds-run` analyze）；生产习惯：赛前约 1 小时一波，15:00-22:00 一天 2-4 波 |
| 结算 | 看板「🧾 结算」；只认 `state==6` 完场比分，内置因子归纳；生产习惯：次日统一结算前一天 |
| 因子归纳 / 退役 | 看板「🧬 归纳」「🪦 Review」；因子有效期批量刷新用 `python scripts/refresh_factor_time.py` |
| 回放 | `ds_replay`：沙箱模型、线上零影响；半交互暂停给方向建议，可续跑 / `to_end` / `rewind_to` |
| 练新狗 | 走训练模式技能 `ds-agents-training`（创建/选狗 → 回放 → 转正/放弃） |
| 发邮件 | `email-orders`（默认 梭哈2狗 + 跟风狗） |

## 关键设定（回答「为什么 / 怎么配」时用）

- 足球日窗口 `[D 12:01, D+1 12:00]`（北京时间）；`analyze/status/pending` 用窗口起始日，
  `settle` 用窗口结束日（起始日 + 1）。
- 只结算 `state == 6` 的比赛；盘口符号为主队视角（受让 = 正，让球 = 负）；
  订单去重 key `(lota_id, bet_type)`，同一场可同时下亚盘和大小球。
- 不要同一天边分析边结算（仓位预算按天统计，混跑会滚仓）；分析尽量赛前 ~1h 内跑，
  早跑会对未开赛比赛退单重算。
- 引擎解释器必须是 miniconda python（含 langgraph）；homebrew python3 会报
  `ModuleNotFoundError: No module named 'langgraph'`（dsh 配置里 `pythonBin` 指向 miniconda）。
- API 密钥客户自配（`DEEPSEEK_API_KEY` 环境变量 / `.env`），系统不内置。
- 狗列表（live）：`alpha2狗 alpha狗 梭哈2狗 梭哈3狗 平局狗 跟风狗 均注狗`（7 只单关）
  + `深度足球狗 梭哈北单狗 跟风北单狗`（注册表单关）+ `bc狗`（北单 8串1 独立角色）。
- `bc狗` 由 `roles/bc狗/parlay.json` 标记：斗狗场「⚡ 分析 / 🧾 结算」会分流到
  `src.beidan_parlay_dog`，用北单开奖 `result + spvalue` 结算；「🧬 归纳 / 🪦 Review」走其 `factor_memory.json`。
- 回放沙箱写入 `replays/sandboxes/<狗>_<MMDD>/workspace`，线上零影响；
  转正 = 备份线上 → 整目录替换 → 注册表翻 live；放弃 = 删沙箱、线上不动。

## 常见坑（客户遇到时直接给解法）

- 分析 0 单：先确认当天竞彩场次与缓存是否就绪；回放报「角色不存在…先 role-sync」时，
  先核对沙箱 `workspace/` 是否含 `<狗>.json`（平铺布局），不要按嵌套路径补文件。
- 因子全部休眠 / 看不到因子：运行
  `python scripts/refresh_factor_time.py --revive-dormant`（按历史订单刷新因子有效期）。
- 缺 langgraph：换 miniconda 解释器（`pythonBin` 配置）。
- 看板头像不显示：头像文件名必须与狗名一致（`<狗名>.png`，放在 `头像/` 目录）。
