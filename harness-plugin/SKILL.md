---
name: ds-agents-lota-data
description: 使用 ds-agents-lota-data 插件的工具做「效果 A」足球分析：读数据（lota_matches/lota_match/lota_sections）→ 判断 → 输出 ```order``` 区块 → ds_place_orders 确定性下单。适用于用户想做足球投注分析、下单或查询比赛缓存数据时。
---

# ds-agents-lota-data 插件（效果 A）

本插件暴露**读数据 + 下单**两类工具。读数据只读本地缓存（`config.cacheDir`，默认 `data`），不触网、不含密钥；下单走「引擎桥」调私有 Python（`config.engineRoot`）。

## 效果 A 工作流（agent 当大脑）

```
1. 读数据:  lota_matches(day) → 列比赛
            lota_match(id)    → 单场全貌（含终盘赔率）
            lota_sections(id, slugs) → 关键段落（公平盘/亚盘/大小球/必发）
2. 查资金:  ds_capital(user)  → 余额/锁定敞口/全金额/约束（金额按全金额比例算）
3. 判断:    基于数据独立推理，按信心档位定比例，每场输出一个 ```order``` 区块
4. 下单:    ds_place_orders(user, day, orders_text)
            → 确定性 parse_orders + place_orders（去重/资金折算/硬约束/下单）
```

⚠️ 不要调用 dsfootball_cli.py 的 agent analyze/settle——那是旧 LangGraph 流程。本插件只让你自己读数据、自己判断。

## 工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `lota_matches` | `date`(必填), `lottery_type`(可选) | 某足球日的比赛列表 `{date, count, matches}` |
| `lota_match` | `lota_id`(必填) | 单场全貌 `{lota_id, match, score, odds, sections, predictions, orders, cached_at, api_failed}` |
| `lota_sections` | `lota_id`, `slugs`(必填, string[]) | 段落 `{lota_id, sections, text}` |
| `ds_capital` | `user`(狗名) | 资金现状 `{capital, locked_exposure, full_capital, unsettled_count, limits}` |
| `ds_place_orders` | `user`(狗名), `day`(足球日), `orders_text`(```order``` 区块) | 下单结果 `{parsed, placed, skipped, capital, orders}` |
| `ds_settle` | `user`(狗名), `day`(足球日) | 结算结果 `{unsettled, settled, hit, miss, push, pnl, capital, orders}`（orders 含 hit/profit/reason/bet_size，供反思用） |
| `ds_reflect` | `user`(狗名), `day`(足球日), `reflect`(因子JSON), `settled`(结算单列表) | 反思写回 `{ok, attributed, new_factors, summary}` |
| `ds_factor_induction` | `limit`(可选), `roles`(可选), `dry_run`(可选) | 因子归纳（清洗/合并/补定义） |
| `ds_factor_review` | `user`(狗名), `end_date`(必填), `start_date`(可选) | 因子退役评估（auto-dormant + 低信息退役 + LLM 结构性判断） |

## order 区块格式（ds_place_orders 输入）

每场一个 ```order``` 区块，字段：`lota_id` / `类型`(胜平负|亚盘|大小球|skip) / `pick`(H|D|A|over|under) / `赔率` / `金额` / `理由`。不下注的比赛也要输出，`类型: skip`。

## 资金管理（金额语义）⚠️

- **金额不是拍脑袋的绝对额，是「信心比例」**：先 `ds_capital(user)` 拿到 `full_capital`（全金额 = 余额 + 锁定敞口），
  `金额 = 信心比例 × full_capital`，比例按人设档位：最有信心 30–40% / 次之 15–20% / 试探 5–10%（连输 3 场后最大仓位降到 10%）。
- 下游确定性层会二次处理：去重 `(lota_id, bet_type)` → 已开赛跳过 → 资金折算（`scale = 余额 / 全金额`）→ 硬约束（`limits`）→ 破产检查。
- `limits` 约束项：`max_exposure_pct`（单日总仓上限，超限截断/缩放）、`max_orders`（单数上限）、`min_orders`（保底注数，不足按序补回）。未配置 = 不限制，仅破产检查兜底。
- 所以 agent 只需给**方向 + 信心比例**，钱怎么算、能不能下、下多少全由确定性层兜住。

## 结算 + 反思（ds_settle → ds_reflect）⚠️

结算不只是「取比分 → hit/miss/push/pnl」，还包含**反思（reflect）**：从已结算订单中发现可复用投注因子。工作流：

```
1. 结算:    ds_settle(user, day) → orders（含 hit/profit/reason/bet_size）
2. 读数据:  lota_sections(id, slugs) 回看每笔结算单的赛前段落（fair-odds/discrete-odds/betfair-buysell/asian-handicap-pinnacle）
3. 推理:    跨场对比（赢的共性 / 输的异同）→ 提炼因子（≥2-3 场共性才算，单场孤例不算）
            + 资金管理反思（哪注该大哪注该小）→ 产出 factor JSON
4. 写回:    ds_reflect(user, day, reflect, settled) → 确定性写回因子/反思记忆
```

factor JSON 结构（`reflect` 参数）：

```json
{
  "per_match": {"order_0": "信号摘要", "order_1": "..."},
  "alpha_factors": ["因子名≤12字"],
  "key_slugs": ["discrete-odds", "fair-odds"],
  "noise_slugs": ["match-head"],
  "factor_desc": {"因子名": "一句话描述"},
  "factor_attribution": {"order_0": ["因子名"], "order_1": ["因子名"]},
  "money_lesson": "资金管理教训 ≤80字",
  "reflection": "跨场规律总结 ≤200字"
}
```

- 因子名 ≤12 字、禁止写数值（">3"等过拟合）、用方向词。
- `factor_attribution` 的 `order_N` 必须与 `ds_settle` 返回的 `orders` 顺序一致（`order_0`=第 0 单）。
- `ds_reflect` 只做确定性写回（去重/归因/存因子模型/存反思），LLM 因子发现由你（harness agent）完成。

## 缓存格式

盘上格式见 `docs/cache_format_spec.md`（canonical cache layout v1）。

## 引擎桥（私有，不在本开源插件内）

`ds_place_orders` spawn `python3 -m src.place_orders`（cwd=`engineRoot`），stdin 喂 JSON、stdout 收 JSON。`parse_orders` + `place_orders` 复用私有引擎，开源用户按同一 stdin/stdout 协议接入自己的实现。

## 注意

- `lota_match` 的 `odds`（Pinnacle 终盘）解析在 `odds.js`，是**数据专有**（依赖 compact-fet 文本排版）。自定义数据源需改 `odds.js` 与 `python-engine/src/tools.py::extract_odds`。
- 工具注册用 DSH 的 `defineTool`；挂载后如需收紧 `output.schema`，用 `cordis_inspect` 核对。
