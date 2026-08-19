# 北单 Goal Line 数据路径设计（长串北单模式前置）

> 状态：规划文档，暂不实现。
> 日期：2026-08-06
> 数据范围约束：只使用 **2026-06-11 之后**的数据。

---

## 一、目标与背景

计划开放**长串北单模式**（主打 6 串+），相比现有竞彩串关更复杂：agent 需要决策每条腿是否**双选**（在胜/平/负中取 1~2 个选项）。整个模式的第一个前置步骤是打通**北单 goal line（让球线）**的数据路径：

```
spdex 爬虫（已有）→ spdex 库 Beidan* 表（已有）
  → deepseek_lota matches 接口返回北单让球（缺）
  → ds_agents matches 缓存 merge（缺）
  → 长串北单 agent：6+ 串 + 双选（缺）
```

本文只规划数据路径与回测口径，不涉及出票/投注平台侧实现。

---

## 二、现状与实测验证（2026-08-06 只读验证）

### 2.1 数据库拓扑

- **spdex 爬虫**（`spider_monorepo/spdex`，`beidan_spider.py` / `beidan_task.py`）把北单数据写入 **spdex 库**（MySQL `10.23.94.209:33698`，库名 `spdex`）。
- **deepseek_lota**（Django，`football_prediction/settings.py`）以 `spdex` 数据库别名读取**同一个库**（`client_api.py` 中竞彩 HHAD 已用 `.using('spdex')` 查询）。
- 两边数据库连通，无需新增同步链路。

### 2.2 Beidan 表结构与数据量（spdex 库）

| 表 | 内容 | 关键字段 | 行数 |
|---|---|---|---|
| `BeidanMatch` | 北单比赛（让球胜平负口径） | `beidan_id`（`期号_场次`）、`home_name/away_name`、`goal_line`、`lota_id` | 29,132 |
| `BeidanOdds` | 北单**让球胜平负**赔率历史 | `beidan_id`、`goal_line`、`home_odds/draw_odds/away_odds`、`updateLogTime`(UTC) | 3,792,526 |
| `BeidanHDCOdds` | 北单**亚盘让球**赔率历史 | `beidan_id`、`goal_line`、`home_odds/away_odds`、`updateLogTime`(UTC) | 3,103,373 |
| `BeidanASMatch` | 北单亚盘比赛信息 | `beidan_id`、`home/away`、`goal_line`、`game_type` | 33,937 |

- `beidan_id` 形如 `26082_42`：`26082` 为期号（26-08-02），`42` 为场内编号（对应 LOTA 的 `beidan_number`）。
- `goal_line` 为字符串：让球胜平负是整数让球（`'-1'`/`'0'`/`'1'`），亚盘是分数让球（`'-0.5'` 等）。
- 赔率表每次变化插入新行，`updateLogTime` 可做时间旅行取价（回测用）。

### 2.3 覆盖统计（>= 2026-06-11）

| 月份 | 北单场次 | 已关联 Beidan（lota_id） |
|---|---:|---:|
| 2026-06 | 256 | 256（100%） |
| 2026-07 | 585 | 576（98.5%） |
| 2026-08 | 241 | 238（98.8%） |
| **合计** | **1082** | **1070（98.9%）** |

关联到的 Beidan 记录 **100%** 有 `BeidanOdds`（让球胜平负）赔率。

样例（Lota4546335 ↔ 26082_42，科林蒂安 vs 巴西国际）：

```text
让球胜平负: goal_line '-1'  @ 14.49 / 17.96 / 1.14   (update 2026-08-05 15:30:35 UTC)
亚盘让球:   goal_line '-0.5' @ 1.60 / 2.65           (update 2026-08-05 15:30:39 UTC)
```

### 2.4 现状缺口

- `deepseek_lota` matches 接口只返回 `beidan_number`，**没有**北单让球字段。
- `ds_agents` 的 matches 缓存只 merge 竞彩 `jc_hhad`，没有北单字段，`chuan_guan_dog.py` 也只读 `jc_hhad`。

---

## 三、数据路径设计

### 3.1 源/库层（已有，基本不动）

- 确认 `beidan_task.py` 的 hda/as 任务每日覆盖所有北单期号即可，无需改表结构。
- 若 `BeidanMatch.lota_id` 匹配率下降，补跑 `deepseek_lota` 的 `beidan_match_fix` 命令。

### 3.2 关联键

- **主键**：`BeidanMatch.lota_id` ↔ LOTA `Match.lota_id`（6.11 后 98.9% 覆盖）。
- **兜底**：`beidan_number`（如 `42`）+ 比赛日期 → 期号前缀（`26082`）拼出 `beidan_id`，供未匹配场次使用。

### 3.3 deepseek_lota 接口层（新增）

位置：`predictions/views/api_v2/client_api.py`，镜像现有 `_get_jc_hhad_map` 实现 `_get_beidan_odds_map(matches)`：

1. 过滤 `matches` 中 `beidan_number` 非空、且 lota_id 命中 `BeidanMatch` 的场次。
2. 按 `BeidanMatch.lota_id` 批量取 `beidan_id`。
3. 取 `BeidanOdds` 最新一行（`order_by('-updateLogTime')`）→ 让球胜平负。
4. 取 `BeidanHDCOdds` 最新一行 → 亚盘让球。
5. 响应字段建议（matches item 内新增）：

```json
{
  "beidan_hhad": {
    "beidan_id": "26082_42",
    "goal_line": "-1",
    "home_odds": 14.49,
    "draw_odds": 17.96,
    "away_odds": 1.14,
    "update_time": "2026-08-05 15:30:35 UTC"
  },
  "beidan_as": {
    "goal_line": "-0.5",
    "home_odds": 1.60,
    "away_odds": 2.65,
    "update_time": "2026-08-05 15:30:39 UTC"
  }
}
```

- 仅当 `is_beidan=true`（或新增 `with_beidan_odds`）时查询，避免默认请求拖 spdex 库。
- 查询全部使用 `.using('spdex')`。

### 3.4 ds_agents 数据层（新增）

位置：`src/data_manager.py`、`dsfootball_cli.py`。

- `fetch_matches_by_date / fetch_matches_by_date_range`：新增 `is_beidan` 参数。
- `refresh_matches_cache / refresh_matches_range`：新增 `with_beidan_odds`，按 lota_id 把 `beidan_hhad/beidan_as` 合并进 matches 缓存（镜像现有 `jc_hhad` merge 逻辑）。
- 新增 `get_cached_beidan_matches(date_str)`：按 `beidan_number` 非空过滤（镜像 `get_cached_jc_matches`）。
- `dsfootball_cli.py prefetch`：新增 `--beidan-odds` 开关（镜像 `--jingcai-odds`）。

### 3.5 消费层：长串北单 agent（后续设计）

- 复用 `chuan_guan_dog.py` 骨架，腿数据换成 `beidan_hhad`（让球胜平负）。
- **双选**：agent 对不确定的腿取胜/平/负中的 2 个选项；票数 = 双选腿数乘积；命中条件 = 每腿至少命中一个选项。
- 数据层只需保证 `goal_line + home/draw/away 赔率 + update_time` 齐备，双选逻辑不依赖额外字段。

---

## 四、回测方案

- 窗口：**2026-06-11 起**（1082 场，覆盖充分）。
- 取价：用 `BeidanOdds.updateLogTime` 时间旅行，取每场**开赛前最新一行**作为当时可下价格。
- 结算：按北单让球胜平负口径——让球数加到对应方比分后比较（口径待确认，见下）。
- 指标：6+ 串命中率、双选后的票数爆炸控制、凯利仓位、资金曲线。

---

## 五、待确认项

1. 北单让球胜平负结算口径是否与竞彩一致（让球数加到主队/客队比分后比较）。
2. 双选在出票侧的落地方式（拆票 vs 单票多选）——影响消费层，不影响数据路径。
3. `is_beidan` 是否沿用现有参数名，还是新增 `with_beidan_odds` 与 `is_jingcai` 对齐。

---

## 六、实施顺序建议

1. `deepseek_lota`：`_get_beidan_odds_map` + matches 接口返回 `beidan_hhad/beidan_as`。
2. `ds_agents`：data_manager merge + `get_cached_beidan_matches` + `prefetch --beidan-odds`。
3. 回测脚本：6.11 起时间旅行取价 + 让球胜平负结算。
4. 长串北单 agent：6+ 串组合 + 双选决策 + 仓位。

---

## 七、涉及文件清单

| 仓库 | 文件 | 动作 |
|---|---|---|
| spider_monorepo/spdex | `beidan_spider.py` / `beidan_task.py` / `dao.py` | 已有，不动 |
| deepseek_lota | `baseinfov2/models.py`（Beidan*） | 已有，不动 |
| deepseek_lota | `predictions/views/api_v2/client_api.py` | 新增 `_get_beidan_odds_map` + 响应字段 |
| deepseek_lota | `predictions/management/commands/beidan_match_fix.py` | 已有，定期补匹配 |
| ds_agents | `src/data_manager.py` | 新增 beidan merge / 读取 |
| ds_agents | `dsfootball_cli.py` | prefetch 新增 `--beidan-odds` |
| ds_agents | `src/chuan_guan_dog.py`（或新 agent） | 后续长串北单消费层 |
