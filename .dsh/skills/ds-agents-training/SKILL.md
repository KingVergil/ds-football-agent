---
name: ds-agents-training
description: 训练模式：创建新狗或选狗进入沙箱回放，跑完转正/放弃。适用于用户说「训练 / 练狗 / 训练模式 / 创建新狗 / 新狗 / 选狗进回放 / 跑回放」时——用 ds_list_dogs / ds_create_dog / ds_replay / ds_sandbox_list / ds_promote_sandbox / ds_abort_sandbox 纯对话驱动斗狗场流程，用户不用填表单。
---

# 训练模式（创建新狗 / 选狗 → 沙箱回放 → 转正 / 放弃）

训练 = 在沙箱里跑一只狗的「平行人生」：新狗从空骨架起跑，老狗复制到起始日
结算后/因子归纳前；回放写 `replays/sandboxes/<狗>_<MMDD>/workspace`，线上零影响；
跑完看 facts 对比，用户决定转正（替换线上）或放弃。

## 工具与职责

| 工具 | 用途 |
|---|---|
| `ds_list_dogs` | 列出全部可选狗（live / 观察 / 有沙箱），选狗入口 |
| `ds_create_dog` | 语言描述创建新狗（注册表 + Python 角色幂等补建） |
| `ds_replay` | 进入/续跑沙箱回放（既有工具，interactive 半交互暂停） |
| `ds_sandbox_list` | 沙箱列表：paused / running / finished，续跑与汇总用 |
| `ds_promote_sandbox` | 转正：备份线上 → workspace 整目录替换 → 注册表翻 live |
| `ds_abort_sandbox` | 放弃：删沙箱，线上不动 |

不要用 bash / 文件操作绕过这些工具去复刻流程（薄壳约束）。

## 入口判断

先判断用户意图：

- 创建新狗 → 走「创建分支」；
- 选狗/已有狗进回放 → 走「选狗分支」；
- 已有暂停沙箱要继续 → `ds_sandbox_list` 找 paused，走「续跑」。

## 创建分支

1. **狗名**：描述里没给，先问（1-24 字符，中英文/数字/下划线/短横线）。
2. **核心风格/人设**：描述里没给，先问一句（会写进 persona.md）。
3. **比赛范围必问**：`jc`（竞彩）/ `beidan`（北单）/ `all`（全部）——不要默认，
   用户很可能要「全部」。
4. 其余全部默认补齐，不再问：初始资金 10000、alpha 关、观察期
   `enabled=false`、限额默认 `max_exposure_pct=40`、人设默认模板兜底。
5. 描述完整（名字 + 风格 + 范围都给了）→ 直接 `ds_create_dog` 并汇报摘要；
   要素不全 → 先列一份补齐后的 spec 一句话确认再创建。
6. 汇报摘要（名字 / 范围 / 初始资金 / 观察期）→ **问「要现在进回放吗？」——
   不自动进回放**；用户说「先不跑/只建狗」就停在这步。

> 用户说「像 XX 那样」时用 `copy_from="XX"`（从其 persona.md 拷贝），不必抄全文。

## 选狗分支

1. `ds_list_dogs` 列出全部狗（live / 观察 / 沙箱状态），问用户选哪只。
2. 用户没指定时给 2-3 个候选；别把 7 只真狗全念一遍。
3. 确认狗后 → 问「要现在进回放吗？」（不自动），区间也一起问，没说用默认窗口：
   - `end` = 最近一个已完赛足球日（北京时间今天 12:00 前 = 前天，12:00 后 = 昨天）；
   - `start` = `end` 往前 6 天（7 天已完赛窗口，训练能出真实下单/结算/PnL）。

## 进入回放

用户确认后调用：

```text
ds_replay(
  dog="<狗名>",
  start="<用户给的或默认窗口>",
  end="<用户给的或默认窗口>",
  mode="interactive",        # 每 factor_review_every 天暂停一次
  factor_review_every=7,
  restore_after=false        # 沙箱保留，待转正/放弃
)
```

新狗 = 空骨架，从起始日跑完整日管线；老狗 = 复制到起始日结算后/因子归纳前。

## 半交互暂停（周期边界）

`mode="interactive"` 每 7 天暂停一次，返回 `direction_suggestion`：

1. **必须把方向建议原样呈现给用户**（附本周期盈亏/退役摘要）；
2. 用户确认/编辑后 → `ds_replay(sandbox="<沙箱名>", induction_notes="<编辑后的方向>")` 续跑；
3. 用户也可以选择：`to_end=true`（剩余一路到底）或 `rewind_to="YYYY-MM-DD"`（回退重跑）。

## 跑完：转正 / 放弃

1. 汇总 facts（`ds_sandbox_list` 或 `ds_replay` 返回）：订单数 / 资金曲线终点 /
   因子变化摘要，别只报「跑完了」；
2. **转正/放弃必须用户明确表态**，不自动；用户可提前授权（如开头说「好就转正」）；
3. 转正 → `ds_promote_sandbox(sandbox="<沙箱名>")`，汇报：线上已替换、备份路径
   `backups/promote_<狗>_<时间>`、狗已翻 live 进默认列表；
4. 放弃 → `ds_abort_sandbox(sandbox="<沙箱名>")`，汇报：线上未动。
