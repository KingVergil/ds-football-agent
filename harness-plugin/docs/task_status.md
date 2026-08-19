# 任务状态设计（task status）

> 状态：设计稿 v1。目标：让**每个工具调用**都有可查询的状态记录，长任务（并行分析 / 回放 / 数据准备 / 因子归纳）运行中持续更新阶段与进度，其他 UI 能实时展示"正在做什么、做到哪、成败"。
> 与工具分组正交：工具分组管**可见性**，任务状态管**可观测性**。

## 1. 背景

现状：`ds_analyze_dog` / `ds_replay` / `ds_prepare_day` / `ds_factor_induction` 这类任务可能跑几分钟到几十分钟，但工具只在**结束**时返回结果，运行中没有任何中间状态；其他 UI（斗狗场 / 外部看板）拿不到"正在进行什么、第几步、x/y"。

## 2. 状态模型

每条任务记录：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 唯一标识：`<type>_<ts>_<rand>` |
| `type` | string | 任务类型（见 §4），UI 分组键 |
| `title` | string | 人类可读标题，如"回放 2026-07-25" |
| `params` | object | 入参摘要（day / dogs / start / end …），可能为 null |
| `status` | string | `running` / `completed` / `failed` / `interrupted` |
| `phase` | string | 当前阶段文案，如"第 2/7 天 结算 梭哈2狗" |
| `done` / `total` | number | 进度计数（per-dog、per-day 等），0/0 = 无进度 |
| `detail` | string | 附加说明（失败原因 / 汇总摘要） |
| `result_summary` | string | 成功时的精简结果（如 PnL、下单数） |
| `started_at` / `updated_at` / `finished_at` | ISO | 时间戳 |

示例：

```json
{
  "id": "replay_1786934244597_a1b2c",
  "type": "replay",
  "title": "回放 2026-07-25 ~ 2026-07-25",
  "params": { "start": "2026-07-25", "end": "2026-07-25", "dogs": ["梭哈2狗"] },
  "status": "completed",
  "phase": "完成",
  "done": 1, "total": 1,
  "detail": "竞彩 11/146，下单 4，结算 PnL -168.5",
  "result_summary": "1000 → 831.5",
  "started_at": "2026-08-17T02:37:24Z",
  "updated_at": "2026-08-17T02:47:10Z",
  "finished_at": "2026-08-17T02:47:10Z"
}
```

## 3. 生命周期

```
start({type, title, params})  → status=running, phase=启动
    ↓
update({phase, done, total, detail}) ×N   ← 长任务内部 onProgress 上报
    ↓
finish({ok, detail, result_summary})      → completed / failed
```

- 统一由 `withTask(registry, meta, execute)` 包装器包住每个工具 execute：start → 调业务 → finish，异常也标记 `failed`（不吞错误）。
- 长任务通过注入的 `progress(patch)` 回调持续更新；短任务只有 start/finish 两态。
- 读取时兜底：`running` 且 `updated_at` 超过 10 分钟视为 `interrupted`（进程被杀/超时遗留），UI 不再显示"进行中"。

## 4. 覆盖与任务类型（映射三工作工具组）

| type | 工具 | 进度粒度 |
|---|---|---|
| `data-prep` | `ds_prepare_day` / `ds_prepare_range` | 阶段：拉比赛 → 补 features/tags → 过滤竞彩 |
| `analyze` | `ds_analyze_dog` | 单狗：`done=0→1`，detail=狗名+ok/fail（并行由父 agent 并列调用决定） |
| `replay` | `ds_replay` | 每天 + 阶段：数据准备 → 第 x/y 天 分析/结算/反思/因子归纳/退役 |
| `settle` | `ds_settle_js` | 粗粒度（短） |
| `reflect` | `ds_reflect_js` | 粗粒度 |
| `factor-induction` | `ds_factor_induction`（含 alpha 跨狗） | 粗粒度 + phase 标注 `alpha barrier` 阶段 |
| `factor-review` | `ds_factor_review_js` | 粗粒度 |
| `factor-dedup` | `ds_factor_dedup` | 粗粒度 |
| `order` | `refresh_orders` / `submit_orders` | 粗粒度 |
| `role` | `ds_persona_js` / `ds_memory_js` / `ds_capital_js` | 粗粒度（短） |
| `match-read` | `lota_matches` / `lota_match` / `lota_sections` | 粗粒度 |

## 5. 对外契约（UI 消费）

1. **持久化文件**：`<cacheDir>/tasks/status.json`，结构 `{ "tasks": [...] }`，原子写（tmp + rename）。外部 UI 可直接轮询该文件，无需经过 dsh web。
2. **HTTP 端点**（web 模式）：`GET /ds-tasks` 返回同一结构；`GET /ds-dashboard` 响应附带 `tasks` 字段（现有看板轮询一次拿全）。
3. 字段稳定，UI 按 `type` / `status` / `phase` 分组展示；长任务展示进度条 `done/total`。

## 6. 保留策略

- 最多保留 50 条；`completed` / `failed` 超过 24h 清理；`running` 常驻（读取时按 §3 的 10 分钟兜底转 `interrupted`）。
- 进程重启后只展示已落盘的记录，不做运行中恢复（v1 不承诺跨进程续跑）。

## 7. 与回放 / runtime 改造的关系

- 后续回放按 分析流 → 结算流 → 因子流（0 反思 → A→B→C barrier）重排时，进度上报随阶段同步（每阶段一个 phase）。
- 因子流 alpha barrier 期间，`factor-induction` 的 phase 明确标注 `阶段A 非alpha` / `阶段B alpha barrier`，UI 能看到"等待非 alpha 完成"。
- 工具可见性（tool groups）与任务状态正交，互不影响。

## 8. 实现清单（后续执行）

> ✅ 已实现并提交（commit 11e5687）：taskStatus.js、全工具 withTask 包装、fanout/dataflow/replay 的 onProgress、/ds-tasks 端点 + dashboard tasks 字段、taskStatus 单测（10 个测试全过）、dsh 安装目录已同步。
