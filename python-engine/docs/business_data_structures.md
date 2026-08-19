# 业务数据结构与结算函数（供 JS 迁移校对）

> 本文整理 ds_agents 当前（LangGraph/Python）的业务数据结构和结算语义，
> 供 deepseek-harness JS 重构时逐项校对。字段名、判定公式以本文为准。
> 缓存文件格式（matches/features/tags）另见
> `harness-plugin/docs/cache_format_spec.md`（公开契约，本文不重复）。

---

## 1. 数据文件地图

```
data/
├─ roles/<dog>/<dog>.json           # 角色：资金 + 订单 + 配置（本文 §2/§3）
├─ roles/<dog>/persona.md           # 人设文本（LLM 用，非结构化）
├─ roles/<dog>/capital_history.json # 资金曲线（§6）
├─ roles/<dog>/memory/
│   ├─ factor_memory.json           # 因子统计（§7）
│   ├─ reflection_memory.json       # 反思记录（§8）
│   └─ slug_memory.json             # 数据段使用统计（§9）
├─ factors/fac_*.json               # 跨狗因子注册表（§10）
├─ matches/ features/ tags/ predicts/ orders/ blacklist.json  # 缓存（见 cache_format_spec）
├─ sessions/*/…                     # 会话日志（JS 迁移后由 session-persistence 取代）
├─ reports/ email_snapshots/ dashboard.html
```

---

## 2. 角色 Role（`roles/<dog>/<dog>.json`）

顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 角色名（狗名） |
| `capital` | float | 当前可用余额 |
| `initial_capital` | float | 初始资金（reset 用） |
| `system_prompt_name` | str | 策略名（如 `baseline-v1`） |
| `alpha_mode` | bool | 是否 alpha（读跨狗因子） |
| `cross_factor_exclude` | list[str] | alpha 模式下排除的角色 |
| `updated_at` | str | ISO 时间 |
| `orders` | list[Order] | 全部订单（含已结算） |

对应 JS 迁移：settings/role 配置 + storage domain `ds-roles`。

---

## 3. 订单 Order 字段

### 3.1 公共字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | `ord_<ms>_<hex>`（`_uid("ord_")`） |
| `lota_id` | str | 比赛 ID |
| `bet_type` | str | 彩种（见 §4） |
| `pick` | str | 选择（H/D/A/over/under/串关串选） |
| `odds` | float | 赔率（港赔水位 / 串关=相乘总赔） |
| `bet_size` | float | 下注金额 |
| `reason` | str | LLM 理由（含"因子匹配数: N"） |
| `created_at` | str | ISO 时间 |
| `settled_at` | str \| None | 结算时间；None=未结算 |
| `hit` | bool \| None | 结算结果（见 §5.5） |
| `profit` | float | 盈亏 = return_amount - bet_size |
| `return_amount` | float | 返还金额 |
| `score` | str \| None | 结算时的比分 `"H:A"` |

### 3.2 各彩种特有字段

| 彩种 | 特有字段 | 说明 |
|---|---|---|
| 亚盘 | `handicap` | float，**负=主让、正=主受（主队视角）** |
| 大小球 | `handicap` | float，即盘口阈值（如 2.5） |
| 胜平负 | — | pick ∈ {H,D,A}，无盘口 |
| 让球胜平负 | `goal_line` | float，负=主让、正=主受；缺失时回退 `handicap` |
| 串关 | `slip_id` `slip_type` `slip_index` `ticket_type` `legs` | 见 §3.3 |

### 3.3 串关订单（`bet_type: "串关"`）

```jsonc
{
  "id": "ord_…", "slip_id": "slip_…", "slip_type": "3串1", "slip_index": 1,
  "lota_id": "Lota4602544", "bet_type": "串关", "ticket_type": "3串1",
  "pick": "H+A+H", "odds": 10.2598, "bet_size": 34.85,
  "legs": [
    { "lota_id": "Lota4602544", "pick": "H", "goal_line": -1.0, "odds": 2.17,
      "score": "2:1", "actual": "D", "hit": false },
    …
  ]
}
```

语义：
- 每张**票单**（slip）由若干张**子单**（order，`slip_index` 区分）组成；
- `N串1`：一张 slip 只有 1 张子单，全腿命中才中；
- `N过M`：一张 slip 有 C(N,M) 张子单，命中 ≥ M 张子单即过关；
- 每腿是**让球胜平负**判定：`adj = 主净胜 + goal_line` → H/D/A（无走水）。

---

## 4. 彩种类型

代码支持 5 种（`order_utils` / `store.settle_order` / `chuan_guan_dog`）：

| bet_type | pick 取值 | 结算方式 | 当前数据量 |
|---|---|---|---|
| 亚盘 | H / A | 港赔 quarter-ball（§5.3） | 2286 单（主力） |
| 大小球 | over / under | 港赔 quarter-ball（§5.3） | 144 单 |
| 胜平负 | H / D / A | 固定赔率，无走水（§5.2） | 143 单 |
| 让球胜平负 | H / D / A | 固定赔率 + goal_line（§5.2） | 代码支持，数据暂无 |
| 串关 | 如 "H+A+H" | 逐腿让球判定，全中才中（§5.4） | 37 单 |

其他枚举（models.py `PredictType`）：`比分`、`进球数` 属于预测类型，**不是订单彩种**。

比赛状态（models.py `MatchState`）：`0=未开赛` `1=进行中` `6=完场`——**只有 state==6
的比分才是结算的权威比分**。

---

## 5. 结算函数

### 5.1 入口 `store.settle_order(order_data, score)`

`score` 必须匹配 `^\d+:\d+$`，否则抛 `ValueError`。流程：

1. `hg, ag = map(int, score.split(":"))`；`diff = hg - ag`；`total = hg + ag`；
2. 胜平负/让球胜平负 → 固定赔率结算（§5.2）；
3. 其余（亚盘/大小球）→ `_settle_hk_quarter`（§5.3）；
4. 写回 `hit / return_amount / profit / score / settled_at`，金额 round 2 位。

### 5.2 胜平负 / 让球胜平负（固定赔率）

```text
让球胜平负: adj = diff + goal_line（缺省回退 handicap）
            actual = H(adj>0) / A(adj<0) / D(adj==0)      # 无走水
胜平负:     actual = score2_1x2(score) = H(diff>0)/A(diff<0)/D(diff==0)

hit = (pick == actual)
hit True  → return = bet_size × odds,  profit = return - bet_size
hit False → return = 0,               profit = -bet_size
pick 不在 {H,D,A} → hit=None，返还本金（不输不赢）
```

### 5.3 亚盘 / 大小球（港赔 quarter-ball）`_settle_hk_quarter`

**整数 / 半球（`|handicap % 0.5| ≤ 0.001`）：**

```text
亚盘:   adj = diff + handicap（hc<0=主让）;  adj==0 → push（返还本金）
        pick==H → win = adj>0;  pick==A → win = adj<0
大小球: total==handicap → push
        pick==over → win = total>handicap;  pick==under → win = total<handicap

win  → hit=True,  return = bet_size × (1+odds),  profit = bet_size × odds
lose → hit=False, return = 0,                    profit = -bet_size
push → hit=None,  return = bet_size,             profit = 0
```

**quarter-ball（`|handicap % 0.5| > 0.001`，如 0.25/0.75/1.25）：**

```text
hc1 = handicap - 0.25;  hc2 = handicap + 0.25
每半独立判定（亚盘 adj=diff+hc；大小球 total vs hc）→ win/push/lose
half_bet = bet_size / 2
return = Σ(win: half_bet×(1+odds) | push: half_bet | lose: 0)
profit = return - bet_size

hit: 两半都 win → True；两半都 lose → False；其余（赢半/输半/双走水）→ None
```

### 5.4 串关 `_settle_one` + 票单汇总

```text
每腿: adj = (主净胜) + goal_line → actual = H/A/D（无走水）
      leg.hit = (leg.pick == actual)；任一腿无比分 → 整单跳过（等下一轮）
子单: hit = 所有腿 hit；return = bet_size × odds(相乘总赔) if hit else 0
票单: _parse_ticket_spec(slip_type) → (N, M, 串|过)
      N串1 → 全部子单命中；N过M → 命中 ≥ M 张子单
```

### 5.5 hit 语义与统计口径（`node_settle_orders` / `stats()`）

```text
profit > 0            → 记 hit  ✅（含 hit=None 的半赢）
profit < 0            → 记 miss ❌（含 hit=None 的半输）
profit == 0           → 记 push ➖（真正的走水）

stats 按 bet_type 分组累计 {total, hit, miss, push, profit, bet}
```

> 注意：**hit=None 不等于走水**。赢半/输半（profit≠0）按方向计入命中/未中；
> 只有 profit==0（整数盘走水/双走水）才算 push。

### 5.6 调用链

```text
agent.py node_settle_orders
  → role.settle_order(order, score)      # 资金变动 + 落盘
    → store.settle_order(order, score)   # 纯结算公式（§5.2/§5.3）

chuan_guan_dog.py settle
  → _fetch_scores (只取 state==6) → _settle_one（§5.4）→ 票单汇总 → _reflect_settled
```

---

## 6. 资金

- `role.capital`：当前余额；`role.initial_capital`：初始值；
- `place_order`：`withdraw(bet_size)`（资金不足抛 `ValueError`）→ 落订单；
- `settle_order`：`deposit(return_amount)` → 写回订单 → save；
- `capital_history.json`：`[{"date": "YYYY-MM-DD", "capital": float, "pnl": float}, …]`；
- `soft_reset`：清订单/预测/资金曲线、重置 capital，**保留因子记忆**；
- `reset`：全部清空（含 memory），恢复 initial_capital。

下单时的资金折算（`node_place_orders`）：

```text
full_amount = capital + locked_exposure（全部未结算 bet_size 之和）
实下金额 = LLM分配金额 × (capital / full_amount)
```

之后按狗套用 FundManager（§下）：

| 狗 | 约束 |
|---|---|
| alpha2狗 | max_exposure_pct=40（%×余额），truncate=整单截断 |
| 梭哈2狗 / 梭哈3狗 | min_orders=2（保底注数） |
| 其他 | 无硬约束 |

---

## 7. 因子记忆 `factor_memory.json`

```jsonc
{
  "updated_at": "…",
  "factor_perf": {
    "低水保护强盘": {
      "total": 3, "hit": 3.0, "miss": 0.0, "push": 0.0, "profit": 768.0,
      "total_return": 2.54, "status": "dormant", "desc": "…",
      "first_seen": "2026-06-12", "last_seen": "2026-06-20",
      "history": [
        { "date": "2026-06-12", "hit": true, "profit": 246.0,
          "return_ratio": 0.82, "lota_id": "…", "bet_size": 300.0 }
      ]
    }
  }
}
```

- `status`：`active` / `dormant` / `retired`；
- `record(factor_id, hit, profit, desc, date, lota_id, bet_size)` 由结算反思写入；
- `selected_active()`：分析时按状态/统计选出活跃因子 + slugs；
- 代码门控：14 天零触发 → dormant；低信息（样本≥5、|avg return|<0.15、命中率
  0.35~0.65）→ retired；LLM 结构性评估 → retire/dormant（名字须逐字匹配，否则忽略）。

---

## 8. 反思 `reflection_memory.json`

```jsonc
{
  "updated_at": "…",
  "reflections": [
    { "date": "2026-07-29",
      "reflection": "…（可追加 📡有效slug / 🔇噪声slug / 📐因子归因 / 💰资金教训）",
      "sample_count": 4 }
  ]
}
```

由 `run_reflect` 写入：`add_reflection(day_date, summary, sample_count=len(seen_lids))`。

---

## 9. slug 记忆 `slug_memory.json`

```jsonc
{
  "updated_at": "…",
  "slug_stats": {
    "match-head": { "appearances": 28, "profitable_days": 17,
                    "loss_days": 11, "flat_days": 0 },
    …
  }
}
```

`record_day_slugs(date, slugs)` / `record_day_pnl(date, pnl)` 每日更新；
分析时用活跃因子的 slugs 决定加载哪些数据段。

---

## 10. 跨狗因子注册表 `factors/fac_*.json`

```jsonc
{
  "id": "fac_asian-handicap-pinnacle",
  "slugs": ["fair-odds", "discrete-odds", "eu-odds-pinnacle", "asian-handicap-pinnacle", "betfair-buysell"],
  "content": "因子定义文本…"
}
```

- `FactorRegistry` 聚合所有 `fac_*.json`，alpha 狗读取（`format_for_prompt`），
  非 alpha 狗不读；
- 新因子由结算反思落盘（`save_factor`），id = `fac_<名>[:40]`；
- factor-induction：alpha 跨狗合并判重 1 次，非 alpha 各自判重。

---

## 11. 缓存数据

见 `harness-plugin/docs/cache_format_spec.md`（公开契约）：

| kind | 内容 |
|---|---|
| `matches/<date>.json` | list[match]（含 match_time/state/score/jc_hhad/goal_line） |
| `features/<lota_id>.json` | compact-fet 原始文本 + 归一化（三种历史形状） |
| `tags/<lota_id>.json` | `{sections: {slug: 文本}}`（19+ slug 切分） |
| `predicts/ orders/ blacklist.json` | 预测/订单/黑名单 |

---

## 12. 足球日窗口语义

```text
足球日 D = [D 12:01:00, D+1 12:00:00]（北京时间）
analyze 标签 = 窗口起始日；settle 传窗口结束日（起始日 + 1）
当前 < 12:00 → 标签 = 昨天；≥ 12:00 → 标签 = 今天
live 标注：窗口内 match_time ≤ now → _live_started（已开赛订单只保留不更新）
所有 now 判定必须用北京时间（_now_bj），宿主机时区一律忽略
```

---

## 13. 给 JS 迁移的核对清单

- [ ] `store.settle_order` 的 5 种彩种分支 + quarter-ball 拆半公式（§5.2/5.3）逐行对照
- [ ] hit 语义：赢半/输半按 profit 符号计入 hit/miss，不是按 hit 字段（§5.5）
- [ ] 串关：逐腿让球判定 + N串1/N过M 票单汇总 + 任一腿无比分跳过（§5.4）
- [ ] `goal_line` 缺省回退 `handicap`（让球胜平负）
- [ ] 只有 state==6 的比分才能结算（fetch_scores 双层过滤）
- [ ] 资金折算公式 + 各狗 FundManager 配置（§6）
- [ ] 北京时间时区钉死（§12）
- [ ] 因子名清洗（图标/⇒后缀/括号，LLM 输出与 factor_perf key 逐字匹配才生效）
- [ ] capital_history 每日记录；soft_reset 保留记忆 / reset 全清
