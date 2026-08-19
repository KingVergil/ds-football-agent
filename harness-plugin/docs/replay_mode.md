# 回放模式（replay mode）

把旧 Python `dsfootball_cli.py agent <狗> runall <start> <end> --jingcai` 的日维度流程迁到
DSH harness：**数据准备先行 → 逐日（分析 → 结算 → 反思 → 因子归纳 → 周期性因子退役）**，
记录每狗每日轨迹，出一份可对比的报告。

> 2026-08-19（薄壳落地）：执行入口改为斗狗场表单（`POST /ds-replay` / `POST /ds-replay/<run_id>`），
> 由插件侧逐日逐 func 调 python 桥（见 `docs/bridge.md`）。下方 `ds_replay(...)` 命令示例仅说明语义，
> 实际不再写入会话输入框。

## 数据获取边界（所有流程共同的前置）

无论分析 / 结算 / 因子 / 回放，都先走 `ds_prepare_day` / `prepareRange`，角色只消费结果：

| 模式 | 行为 |
|---|---|
| `live` | 强制刷新（python 引擎 prepare 的 live-strict 语义：未开赛场次刷新失败拒绝旧缓存，旧赔率不进 prompt） |
| `replay`（历史） | **缓存优先**：matches 有缓存直接读；缺了才由 python 引擎数据层从 URL 拉一次；features/tags 同理，只按竞彩场次补齐（已有有效缓存跳过） |

竞彩边界在 LLM 之前确定：
- 足球日窗口 `[D 12:01, D+1 12:00]`（跨两个日历日期）；
- 只保留 `jingcai_number` 非空的场次，北单 / 无号在进入任何 prompt 之前就被排除；
- 返回的比赛列表已 `strip_scores`（防后视）。

实现：`harness-plugin/dataflow.js`。分析框架（system prompt `ds-agents-analyze`）已改为
先调 `ds_prepare_day(day, mode="live")`，禁止再用 `lota_matches` 拉全量。

## 回放工具

```
ds_replay(
  start: "YYYY-MM-DD",
  end:   "YYYY-MM-DD",
  dogs?: ["梭哈2狗", ...],          // 空 = 7 只真狗
  parallel?: 4,                     // 分析并发（默认 = 狗数）
  model?: "deepseek-v4-flash",      // 旁路 LLM（反思/退役），默认 flash 省 token
  user_notes?: "近期连败，退役标准收紧…",   // 用户调整意见 → 注入周期性因子退役评估
  persona_overrides?: { "梭哈2狗": "…" }, // 人设覆盖（分析/反思/退役共用）
  factor_review_every?: 7,          // 每隔 N 天做一次因子退役（默认 7）
  reset?: "none" | "zero",          // zero = 从初始资金 + 空记忆开始
  restore_after?: true,             // 跑完还原起点状态（默认 true，不污染线上角色）
  run_id?: "replay_20260713_2",

  // ── 半交互（可选）──
  mode?: "auto" | "interactive",    // auto=一路到底（默认）；interactive=每个退役周期暂停
  interactive?: false,              // 等价 mode="interactive"
  // 续跑一个已暂停会话（给了此参数即续跑，忽略上面 start/end/dogs 等全新参数）
  resume_run_id?: "replay_20260713_2",
  induction_notes?: "下轮收紧退役…",  // 续跑：用户编辑后的下一轮因子归纳/退役方向 → 注入下一周期退役评估
  rewind_to?: "YYYY-MM-DD",          // 续跑：回退到某天开始状态（恢复该天前的线上状态，截断其后轨迹）
  to_end?: false                     // 续跑：本次一路跑到底，不再周期性暂停
)
```

## 运行方式：一路到底 / 半交互续跑

- **一路到底（默认）**：不传 `mode`/`interactive`，跑完整段 `[start, end]` 出报告（行为与旧版一致）。
- **半交互**：`mode="interactive"`（或 `interactive=true`）→ 每个因子退役周期（`factor_review_every` 天）结束就**暂停**，返回：
  - `status: "paused"`、`run_id`、`next_day`、`remaining_days`；
  - `direction_suggestion`：本周期退役/盈亏摘要生成的「**下一轮因子归纳/退役方向建议**」（启发式，若 LLM 可用则润色）——交给用户编辑；
  - `factor_reviews`（本周期退役明细）、`trajectory_tail`、`checkpoints`（可回退点）。

  暂停期间**不还原**线上状态（供续跑）。会话状态落 `<cacheDir>/replays/<run_id>/session.json`。

  用户拿到暂停结果后，用 `resume_run_id` 续跑（三选一，可组合 rewind + 方向/续跑）：

  | 意图 | 调用 |
  |---|---|
  | 采纳/编辑下一轮方向后继续 | `ds_replay(resume_run_id, induction_notes="<编辑后的方向>")` |
  | 回到某天状态重跑 | `ds_replay(resume_run_id, rewind_to="YYYY-MM-DD")` |
  | 剩余一路到底 | `ds_replay(resume_run_id, to_end=true)` |

  - `induction_notes` 会成为**下一周期**因子退役评估的 `user_notes`（即"因子下一轮归纳方向"落在退役/评估这一步）。
  - `rewind_to=D` 恢复到"D 当天开始"的线上状态（= D-1 的当日终态快照 `<D-1>__post-day`，首日则用起点快照），并截断 D 及其后的轨迹/检查点；纯 rewind（未同时给 `induction_notes`/`to_end`）只回退并把控制权交回用户，不自动续跑。
  - 全程结束（跑到 `end` 或 `to_end`）才走终态检查点 + 报告 + 可选还原；`restore_after` 语义不变。

## 每日管线

```
0. 范围数据一次性准备（prepareRange：单例 + 缓存优先，缺了拉 URL）
1. 并行分析：fan-out 每狗独立 subagent（比赛列表已注入 + 人设已注入上下文）
2. 结算：settleDog（纯 JS，只认 state==6 比分）
3. 反思：旁路 LLM（模型可覆盖，默认 flash）
4. 因子归纳：alpha 跨狗 1 次 + 非 alpha 各自（flash 判重）
5. 每 factor_review_every 天：因子退役评估（代码门控 + 旁路 LLM；user_notes 注入评估 prompt）
```

## 启动次序（单例取数 → 范围正确性 → 替换/还原）

全新回放（`ds_replay(start, end, ...)`）严格按以下次序执行：

1. **范围正确性**（最先，任何副作用之前）：`validateReplayRange(start, end)`
   —— start/end 必填、`YYYY-MM-DD` 且为真实日历日、`start ≤ end`、单段 ≤ `REPLAY_MAX_DAYS`（60 天）；
   不合法直接返回 `{ok:false,error}`，不落快照、不重置、不取数。
2. **快照起点**：`snapshotDomains` 把 5 个 storage 域全量落到 `replays/<run_id>/snapshot/`。
3. **单例取数**：`prepareRange`（dataflow.js）——同 `cacheDir+start+end` 幂等复用 + in-flight 去重
   （与 `prepareDay` 同构）；底层 cache-first，已有缓存永不重拉。
4. **逐日跑**（分析→结算→反思→归纳→周期退役，每日三检查点）。
5. **替换/还原**：终态检查点 + 报告；`restore_after=true`（默认）时 `restoreDomainSnapshot`
   为**真替换**——先删回放期间新增、快照里没有的 key（如跨狗因子注册表新条目），再全量 put 快照值；
   异常路径同样兜底还原起点，绝不留中间态。

## 轨迹对比与隔离

- 每狗每日快照（余额 / 待定 / 当日下单 / 结算 PnL / 活跃因子数）→
  `<cacheDir>/replays/<run_id>/report.json` + `report.md`；
- 范围 matches 文件在准备后**快照进** `<cacheDir>/replays/<run_id>/cache/matches/`，
  逐日只读快照——运行中的 web 刷新器（每 30 分钟重写当前足球日 matches）不会污染回放边界；
- 起点 storage 域全量快照 → `<cacheDir>/replays/<run_id>/snapshot/`；
- **阶段检查点**（目标4）：每天在 结算前（`<day>__pre-settle`，分析+下单后）、因子流前（`<day>__pre-factor`，结算后）与 当日终态（`<day>__post-day`，供半交互 `rewind_to` 回退）各存一份，结束后补 `<end>__post-factor`（终态）。
  回放报告的"检查点"列表列出全部恢复点；用 `ds_replay_restore(run_id, checkpoint)` 可把线上角色恢复到对应阶段（`start` 回起点）。
- `reset="zero"`：指定狗重置为 `initial_capital`（默认 10000）+ 空订单/因子/反思；
- `restore_after`（默认 true）：跑完自动还原起点，线上角色不被回放污染；
- `persona_overrides` / `user_notes` 即"从某一点修改角色的一部分"，比较不同条件下的轨迹时用。

## 一次最小验证

```bash
# headless 跑 1 天、1 只狗、每天退役（验证全管线）
dsh --profile headless \
  "调 ds_replay(start='2026-08-16', end='2026-08-16', dogs=['梭哈2狗'], parallel=1, factor_review_every=1, reset='zero', restore_after=true, user_notes='验证用：因子退役保守，宁可多保留') 并汇报回放目录与结果"
```

## 与旧 runall 的差异

- 数据：旧 runall 每天 live 强制刷新；回放默认历史缓存优先（符合"历史读缓存、缺了读 URL"）。
- 因子归纳：旧 runall 本身不逐日归纳（batch_agents 结算后归纳）；回放按日归纳，符合"日维度"要求。
- 退役：旧 runall 固定 7 天一次、无人工输入；回放周期可配且可注入用户意见。
- 轨迹：旧 runall 只打日志；回放产出结构化 report.json + markdown。

## 注意事项

- **历史日优先**：回放按历史语义（缓存优先、缺了拉 URL）设计，建议回放已过足球日的窗口
  （如回放 07-25 时今天已是 08-17）。回放"当前足球日"时：比赛均已开赛 → 已开赛保护会
  拦下单（0 单），且 web 刷新器可能并发改写缓存——已用快照隔离，但结果仍是 live 语义。
- 旁路 LLM（反思/因子归纳/因子退役）默认 `deepseek-v4-flash` 省 token；主分析轮次模型
  跟随会话配置（headless 默认也是 flash）。
- 竞彩边界统一由 `dataflow.js` 提供：足球日窗口 + `jingcai_number` 非空，
  北单/无号在 LLM 之前排除；fan-out 注入的列表与 `ds_prepare_day` 同一口径。
