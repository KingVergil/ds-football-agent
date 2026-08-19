# 回放模式改造设计：沙箱复制目录 + 转正 + 老狗 diff（2026-08-19 v2，已定稿决策）

> 本文是**修改文档**。基于「回放 = 创建一个角色复制目录（沙箱），新狗为空、老狗复制到某一天，
> 转正 = 替换线上，enable 设计错误，老狗回放需要 diff（依赖 dsh 前端载体）」。
> v2 已吸收用户决策：① 转正=整目录替换；② 老狗复制到「D 结算后、因子归纳前」的状态；
> ③ diff 沙箱只产事实（订单/因子），总结交给 dsh；④ 沙箱按狗命名 `<狗>_<D>`，复制「开始→D」全量数据；
> ⑤ 转正用 dsh 文件原语。

---

## 1. 现状与问题

当前回放是「**写穿线上**」模型：

- `replay.js` 逐日把 prepare/analyze/settle/induction/review 跑在**线上角色文件**上，
  订单/因子直接落 `roles/<狗>/`；只有 `restore_after=true` 才在结束时还原起点（模拟跑）。
- 范围冲突靠 `findRangeOrderConflicts` **拒绝**「范围内已有订单」的回放（因为会污染线上）。
- `enable` 布尔同时承担「进默认列表 / 观察期 / 新狗待激活」三件事，且与回放状态无关。

问题：

1. **写穿即污染**：老狗回放一旦启动就动线上数据，逼出「拒绝已有订单」「restore_after」补丁。
2. **enable 语义错误**：新狗创建默认 `enabled=false`，「观察」被固化成属性，而不是「未转正」的临时状态。
3. **没有「回放规划对比」**：老狗改人设/参数后回放，无法和线上真实轨迹做 diff。

---

## 2. 新模型总览

> 回放 = 在沙箱里跑一只狗的「平行人生」，看事实对比，决定要不要转正。

```
                    沙箱 replays/sandboxes/<狗>_<D>/         线上 roles/<狗>/
  ┌──────────────────────────────────────────┐             （不动）
  │ snapshot/     ← 起点复制（新狗=空骨架；    │
  │                 老狗=复制「开始→D」到      │
  │                 D 结算后/因子归纳前）       │
  │ workspace/    ← 回放运行的工作副本         │
  │                 （python 桥经 role_root 写这里）│
  │ checkpoints/  ← 沙箱内逐日检查点           │
  │ trajectory.json / factor_reviews.json    │
  │ facts.json    ← 事实订单/因子/资金曲线      │
  └──────────────────────────────────────────┘
      │ 转正（dsh 文件原语：备份线上 → 整目录替换）
      ▼
  线上 roles/<狗>/ ← workspace 成为新的线上状态
```

三个动作：

- **创建沙箱**：新狗 = 空骨架（persona/limits/初始资金复制，订单/记忆/资金曲线为空）；
  老狗 = 复制「开始→D」全量数据到 D 结算后、因子归纳前的状态（§3.3）。
- **运行回放**：全部桥调用写沙箱 `workspace/`，线上零影响；逐日检查点、暂停续跑、
  方向建议都在沙箱内。
- **转正 / 放弃**：转正 = dsh 文件原语（先备份线上 → workspace 整目录替换）；
  放弃 = 删沙箱，线上不动。

---

## 3. 沙箱生命周期与目录结构

### 3.1 沙箱身份：`<狗>_<D>`

沙箱按狗 + 起始日区分（决策 ④）：

- `梭哈2狗_0718` = 梭哈2狗 从 0718 开始重规划的沙箱；
- 名称即身份，创建幂等（同名已存在则复用/续跑）；
- 一次沙箱内可多次 run（同一基底反复实验），转正或放弃后释放名称。

### 3.2 目录

```
replays/sandboxes/<狗>_<D>/
  session.json                      ← 最近一次运行的会话状态
  snapshot/                         ← 起点复制（整目录）
  workspace/                        ← 回放工作副本（运行期唯一写盘根）
  checkpoints/<day>__pre-factor/    ← 沙箱内逐日「结算后/因子前」检查点
  checkpoints/<day>__post-day/      ← 沙箱内逐日终态检查点
  trajectory.json                   ← 该狗逐日轨迹（事实）
  factor_reviews.json               ← 该狗因子退役记录（事实）
  facts.json                        ← 事实订单/因子/资金曲线（供 dsh 总结）
```

`workspace/` 与线上 `roles/<狗>/` **同构**：`<狗>.json` + `memory/` + `predicts/` +
`capital_history.json` + `persona.md`。python 引擎经「角色根覆盖」读写它。

### 3.3 老狗「复制到 D」的状态（决策 ②）

沙箱起点 = 线上狗「开始→D」的全量数据，落在 **D 结算后、因子归纳前**（`D__pre-factor`）：

- 订单：含 D 当天已结算结果，D 之后无；
- 资金：= D 结算后的余额（资本曲线到 D）；
- 因子/反思/slug 记忆：= **D 因子归纳前**的快照（线上当日归纳尚未发生/被丢弃）；
- persona/limits/scope/alpha_mode：复制线上当前值。

线上 live 狗因此需要维护**每日 `D__pre-factor` 检查点**（结算后自动落盘一次），
沙箱创建即取对应检查点。

回放从 D 起的行为：

- D 当天：不再重复分析/结算（线上已发生且已复制），从 **D 的因子归纳**继续；
- D+1 起：完整日管线（prepare→analyze→settle→induction→review）。

> 语义即「把 0718 之后的因子流和后续交易，在改过方向/人设的沙箱里重跑一遍」。

### 3.4 生命周期

| 阶段 | 动作（落点） | 线上影响 |
|---|---|---|
| create | dsh 文件原语建沙箱：空骨架 / 复制到 D__pre-factor | 无 |
| run | 逐日桥调用写 workspace（`role_root`） | 无 |
| inspect | dashboard 看轨迹 + 事实 | 无 |
| promote（转正） | dsh 文件原语：备份线上 → workspace 整目录替换（决策 ①） | 有（可回滚） |
| abort（放弃） | dsh 文件原语删沙箱 | 无 |

---

## 4. enable 重构：dog.status 状态机

### 4.1 enable 错在哪

`enabled` 把三件不同的事压成一个布尔：进默认列表 / 观察期 / 新狗待激活。
后果：新狗默认 `enabled=false` 不进列表；「观察」被固化而非临时状态。

### 4.2 新模型

```
dog.status ∈ { live, sandbox, archived }
```

| status | 含义 | 默认列表 | dashboard 展示 |
|---|---|---|---|
| `live` | 线上在用（已转正） | ✅ 进 | 正常 |
| `sandbox` | 有沙箱在跑/待转正 | ❌ 不进 | 「沙箱/待转正」徽章 |
| `archived` | 退役/归档 | ❌ 不进 | 归档区（可查不可跑） |

- 新狗创建 → `status=sandbox`（空沙箱）→ 首跑/回放 → **转正** → `live`；
- 观察期 = `sandbox` 未转正；默认列表 = `status==live`（派生，不存第二份）。

### 4.3 迁移

`role_registry sync` 扩展：`enabled=true → status=live`；`enabled=false → status=sandbox`；
`batch_agents.sh`、dashboard `enabledFor`、`roles.enabledDogs` 改读 `status`。

---

## 5. 老狗回放 diff：沙箱只产事实，dsh 负责总结（决策 ③）

### 5.1 分工

- **沙箱（引擎/插件）**：只产出**事实数据**——回放的订单、因子变化、资金曲线；
- **dsh（前端 + agent）**：负责**对比与总结**——双曲线渲染、逐单对比、重合率/盈亏结论，
  由斗狗场 + 会话 agent 给出「回放比线上好/差」的判断。

「老狗回放规划 diff 依赖 dsh 前端载体」由此成立：没有 dsh，沙箱只有事实，没有结论。

### 5.2 facts.json（沙箱产出，纯事实）

```jsonc
{
  "dog": "梭哈2狗",
  "sandbox": "梭哈2狗_0718",
  "start": "2026-07-18",
  "end": "2026-07-25",
  "orders": [                        // 回放实际落单（事实）
    { "day": "2026-07-19", "lota_id": "L1", "bet_type": "亚盘", "pick": "H",
      "handicap": -0.75, "odds": 0.9, "bet_size": 600, "profit": null }
  ],
  "factor_changes": [                // 因子事实（新增/退役/休眠，按天）
    { "day": "2026-07-18", "action": "retire", "factor": "低水诱杀" }
  ],
  "capital_curve": [ { "day": "2026-07-18", "capital": 10400 } ],
  "trajectory": [ { "day": "2026-07-18", "placed": 1, "settled": 2, "pnl": 400, "active_factors": 7 } ]
}
```

**不包含**：重合率、PnL 对比、ROI 差等任何「结论」——这些由 dsh 计算/总结。

### 5.3 dsh 呈现与总结

- 双资金曲线（线上事实 vs 沙箱事实，同轴）；
- 逐日订单对比表（同 lota_id 并排，差异高亮）；
- 因子变化表 + 会话 agent 用 LLM 总结「方向调整后比线上更好/更差」；
- 摘要卡 + 「转正」「放弃」按钮。

### 5.4 范围冲突约束消失

沙箱隔离后不再污染线上 → **删除 `findRangeOrderConflicts` 拒绝逻辑**。

---

## 6. python 引擎改造

1. **角色根覆盖（核心）**：`Role` / `DataManager` / `memory` 支持 `DS_ROLES_ROOT`
   （桥 `opts.role_root`）指向沙箱 `workspace/`；analyze/settle/induction/review/
   refresh/reset 全部写沙箱，`status`/`prepare` 只读不受影响。
2. **不做沙箱生命周期 func**（决策 ⑤）：create/promote/abort 由 dsh 文件原语实现，
   桥只多一个 `role_root` 透传 + 校验（沙箱路径必须在 replays/sandboxes 下）。
3. **线上每日检查点**：live 狗结算后自动落 `D__pre-factor` 快照（供沙箱复制）。
4. **role_registry**：`status` 字段 + `enabled → status` 迁移（§4.3）。

---

## 7. dsh 侧改造

| 文件 | 改动 |
|---|---|
| `bridge.js` | 透传 `role_root` + 沙箱路径白名单校验 |
| `replay.js` | 沙箱生命周期（create→run→promote/abort，文件原语）；桥调用带 `role_root`；删除冲突拒绝；产出 `facts.json` |
| `dashboard.js` | `/ds-sandbox`（create/promote/abort）、沙箱列表按 `<狗>_<D>`、`/ds-dashboard` 增 facts 字段 |
| `client.js` | 沙箱管理 UI + diff 可视化（双曲线/逐单对比/因子表）+ 转正确认 |
| `tools/replayTool.js` | `ds_replay` 参数加 `sandbox`（`<狗>_<D>`）/ `promote_after` / `facts` 开关 |
| `index.js` | 装配透传 role_root；默认列表读 status |
| `.dsh/skills/*` | 文案更新（回放=沙箱+转正） |

---

## 8. 兼容与迁移

- 旧回放目录（写穿产物）保留只读，不迁移；
- `restore_after` 语义被「沙箱 + abort」取代，从表单移除或标注废弃；
- `enabled → status` 一次迁移（`role_registry sync`），`batch_agents.sh` 跟随；
- 旧 `replays/<run_id>/dogs/<狗>/` 结构被 `replays/sandboxes/<狗>_<D>/` 取代；
  迁移期可保留旧 run 目录只读。

---

## 9. 遗留开放问题

1. **线上每日检查点粒度**：只在结算后落 `D__pre-factor`，还是也保留 `post-day`（因子后）？
   ——沙箱复制只需 pre-factor；post-day 仅调试用，默认只落 pre-factor。
2. **diff 重合率口径**（dsh 总结层决定）：同 lota_id + 同 bet_type + 同方向（H/A/over/under）。
3. **沙箱 vs 线上同日并发**：线上 live 当日与沙箱跑同一日并存时，facts 标 `replay_date`，
   由 dashboard 加「并行」角标，不做互斥。
4. **沙箱数据量**：复制「开始→D」全量 = 老狗全历史；建议 D 可默认今天、范围上限仍 60 天。

---

## 10. 修改落点清单（文件级）

**python-engine**
- `src/bridge.py`：`opts.role_root` 透传 + 沙箱路径校验
- `src/role.py`：`copy_to` / `restore_from`（dsh 也可用文件原语，二选一）
- `src/role_registry.py`：`status` 字段 + `enabled → status` 迁移
- `src/agent.py` / `src/data_manager.py` / `src/memory.py`：角色根覆盖
- `src/store.py` / 结算链路：live 狗结算后落 `D__pre-factor` 检查点
- `batch_agents.sh`：默认列表读 `status==live`

**harness-plugin**
- `replay.js`：沙箱生命周期 + facts 产出 + 删除冲突拒绝
- `bridge.js` / `index.js`：role_root 透传
- `dashboard.js` / `client.js`：沙箱管理 + diff 可视化 + 转正确认
- `tools/replayTool.js`：`sandbox` / `promote_after` / `facts` 参数
- `docs/bridge.md` / `docs/replay_mode.md`：同步新结论

**验证**
- 新狗：创建沙箱（空）→ skip_llm 回放 → 转正 → 进默认列表
- 老狗：`梭哈2狗_0718` 复制到 D__pre-factor → 回放 → `facts.json` → dashboard diff → 转正/放弃
- 回归：`node --test` 13/13；`py_compile`；桥冒烟（status/prepare）
