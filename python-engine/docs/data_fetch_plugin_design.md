# DataManager 数据层工具整理 & 获取数据插件设计

> 背景：把 ds_agents 核心功能移入 DSH harness。第一步从"获取数据"入手，
> 本文先整理现有 `src/data_manager.py` + `src/tools.py` 设计的数据访问工具面，
> 再给出第一个 Cordis 插件（data-fetch）的设计草图。

## 1. 总体架构（三层）

```
┌────────────────────────────────────────────────────────────┐
│ Lota API v2 (http://deepdata.lota.tv/predictions/api/v2)    │
│   认证: X-API-Key（~/.claude/settings.json → lota.api_key） │
│   /matches     比赛列表（date / lota_id / 日期范围，分页）   │
│   /compact-fet 单场原始数据（~10KB 自描述文本）              │
└────────────────────────────────────────────────────────────┘
              │ 网络层（src/tools.py / data_manager.py _get）
┌─────────────▼──────────────────────────────────────────────┐
│ 本地缓存 lota_data/（JSON 文件，原子写入 .tmp+replace）      │
│   matches/{date}.json     按足球日分文件的比赛列表           │
│   features/{lota_id}.json compact-fet 原始缓存（含 _cached_at）│
│   tags/{lota_id}.json     切分后的 {slug: 段落}             │
│   predicts/ orders/ blacklist.json                          │
└─────────────────────────────┬──────────────────────────────┘
                              │ tag 提取层（src/tools.py）
┌─────────────────────────────▼──────────────────────────────┐
│ compact_fet_to_tags: 文本 → {slug: 段落}                    │
│   _SECTION_RULES: 19 个 slug（fair-odds/asian-handicap-*…） │
│   extract_odds: Pinnacle 终盘 → eu/asian/ou                 │
│   Factor 按 slug 引用数据段，避免整坨文本进 LLM             │
└────────────────────────────────────────────────────────────┘
```

## 2. DataManager 方法清单（工具面）

### 2.1 比赛 Match
| 方法 | 方向 | 说明 |
|---|---|---|
| `fetch_matches_by_date(date, lottery_type, is_jingcai)` | API | 某日比赛列表；`is_jingcai=true` 附带竞彩让球 `jc_hhad` |
| `fetch_matches_by_date_range(start, end, ...)` | API | 日期范围全量（分页 `limit=2000` 拉完） |
| `fetch_match_by_id(lota_id)` | API | 单场详情（含比分） |
| `get_cached_matches(date, lottery_type)` | 本地 | 某日缓存，按 lottery_type 过滤 |
| `get_cached_jc_matches(date)` | 本地 | 竞彩场次（`jingcai_number` 非空），供串关 |
| `save_matches_cache(date, matches)` | 写 | 原子写入 |
| `refresh_matches_cache(date, with_jc_odds)` | 刷 | 全量刷新；可选把 `jc_hhad` 按 lota_id 合并进全量（避免覆盖非竞彩） |
| `refresh_matches_range(start, end, with_jc_odds)` | 刷 | 一次范围拉取，按足球日窗口 `[D 12:01, D+1 12:00]` 切分写盘 |
| `get_cached_match(lota_id)` | 本地 | features 优先，再扫 30 天 matches 文件 |
| `refresh_score_match(lota_id)` | 刷 | 单场比分状态机刷新（见 §3 规则） |
| `refresh_scores()` | 刷 | 批量：扫所有已开赛未完场，逐场查比分，0.05s 限速 |
| `get_match(lota_id, refresh)` | 复合 | 缓存优先；完场(state==6)本地权威，否则走 API 回退缓存 |

### 2.2 Compact-fet（核心）
| 方法 | 方向 | 说明 |
|---|---|---|
| `fetch_compact_fet(lota_id)` | API | 原始请求 |
| `get_cached_compact_fet(lota_id)` | 本地 | 先 `lota_data/features/`（JS 主缓存），再 Python 自有目录 |
| `save_compact_fet_cache(lota_id, data)` | 写 | 打 `_cached_at` 时间戳 |
| **`get_compact_fet(lota_id, refresh)`** | **复合** | **缓存策略核心方法**（见 §3），绝大多数调用方走这里 |
| `get_compact_fet_text(lota_id)` | 复合 | 取文本（供 tag 提取） |

### 2.3 Tags / 段落
| 方法 | 说明 |
|---|---|
| `get_tags(lota_id)` | tags 缓存优先；无则从 compact-fet 即时切分并落缓存 |
| `get_sections(lota_id, slugs)` | 按 slug 列表拼 prompt 片段（`[section:{slug}]\n...`） |
| `_parse_tags_from_text(text)` | 纯文本切分兜底 |

### 2.4 赔率
| 方法 | 说明 |
|---|---|
| `get_odds(lota_id)` | `extract_odds` → Pinnacle 终盘 `{eu, asian, ou}`，缺失 section 为 None |

### 2.5 关联查询（一键全貌）
| 方法 | 说明 |
|---|---|
| `get_predictions(lota_id)` | 本地 predicts 列表 |
| `get_orders(lota_id)` | 本地 orders 列表 |
| `get_match_context(lota_id)` | match 基础信息 + score + odds + predictions + orders + tags_summary |

### 2.6 运维 / 健康
| 方法 | 说明 |
|---|---|
| `set_live_mode(live)` | live 模式：未开赛场次刷新失败**拒绝**回退旧缓存 |
| `check_data_freshness(day)` | 特征缓存新鲜度（>3h 告警）+ state=-1 已开赛比赛检测（数据管道中断） |
| `get_blacklist()` | 黑名单 lota_id（已完赛/异常，禁下注） |

### 2.7 模块级便捷函数（兼容旧代码）
`get_match / get_compact_fet / get_tags / get_sections / get_odds / get_predictions / get_orders / get_match_context`

## 3. 缓存策略（业务规则，插件必须原样保留）

`get_compact_fet` 的决策链：

1. **负缓存**：`_api_failed=true` 且未超 `NEGATIVE_CACHE_TTL=600s` → 直接 None
2. **已完场**（state==6 或有实际比分）→ 缓存永久有效
3. **已开赛**（match_time ≤ now）→ 赔率已锁定，直接用缓存
4. **未开赛** → `COMPACT_FET_CACHE_TTL=120s` 内直接用；过期走 API
5. **刷新失败**：
   - 有旧有效缓存 + live 模式 + 未开赛 → 拒绝旧缓存，返回 None（旧赔率不许进 prompt）
   - 有旧有效缓存（非 live）→ 保留旧数据不毒化，等下次 TTL 重试
   - 无旧缓存 → 写负缓存桩
6. **`refresh=True`** → 跳过缓存强制 API（保留旧 score）

## 4. prefetch 流水线（"获取数据"的入口）

`python dsfootball_cli.py prefetch [YYYY-MM-DD] [--jingcai] [--jingcai-odds]`

```
1. 足球日窗口: get_football_day(d) → [start, end]；日历日期列表 cal_dates
   （无参数时 12:00 前 → 昨天，与 batch_agents.sh live 语义一致）
2. dm.set_live_mode(True)
3. 对每个日历日期: refresh_matches_cache(cd, with_jc_odds) → 全量比赛列表
4. 过滤候选: 窗口内 && 有 lota_id && 队名非空 && (非 --jingcai 或 jingcai_number)
5. 逐场: get_compact_fet → compact_fet_to_tags → save_tagged_sections
6. 报告: ✅ ok/fail
```

产出 = `features/{lota_id}.json` + `tags/{lota_id}.json`，供并发 analyze 共用。

## 5. Cordis 插件设计草图（data-fetch）

### 5.1 服务 `ctx.lotaData`（方法镜像数据层核心 API）

```ts
declare module '@deepseek-ai/cordis' {
  interface Context {
    lotaData: LotaDataService
  }
  interface Events {
    // 数据刷新完成 → 下游（分析/结算）监听
    'lota/data-refreshed'(payload: { day: string; matches: number; ok: number; fail: number }): void
  }
}

interface LotaDataService {
  matchesByDate(day: string, opts?: { jcOdds?: boolean }): Promise<Match[]>
  refreshMatches(day: string, opts?: { jcOdds?: boolean }): Promise<Match[]>
  getCompactFet(lotaId: string, opts?: { refresh?: boolean }): Promise<CompactFet | null>
  getTags(lotaId: string): Promise<Record<string, string>>
  getSections(lotaId: string, slugs: string[]): Promise<string>
  getOdds(lotaId: string): Promise<Odds>
  getMatchContext(lotaId: string): Promise<MatchContext>
  checkFreshness(day: string): Promise<{ fresh: boolean; warnings: string[] }>
}
```

### 5.2 模型可调工具（挂 `ctx.tools`）

| 工具 | 动作 |
|---|---|
| `lota_prefetch` | 跑 §4 流水线（可带 day / jingcai 参数） |
| `lota_matches` | 某日比赛列表（读缓存/刷新） |
| `lota_match` | 单场全貌 `getMatchContext` |
| `lota_sections` | 按 slug 取 prompt 段落 |
| `lota_freshness` | 数据管道健康检查 |

### 5.3 生命周期与策略
- **定时**：`timer` 插件每日 prefetch（窗口按 football day）
- **事件**：`lota/data-refreshed` 通知下游（先 emit，供 analyze 插件监听）
- **缓存策略**：TTL / 负缓存 / live-strict 三条规则直接搬进服务实现
- **数据落地**：第一期继续写 `lota_data/` JSON（与现有 Python/JS 兼容），后续再决定是否迁入服务持有存储

### 5.4 实现路线（推荐：桥接先行）
1. **桥接版**：插件内 `spawn` 调 `dsfootball_cli.py prefetch` / `data_manager.py <lota_id>`，解析 stdout 结构化结果 → 先打通 harness 集成链路（工具可见、可调度、可监控）
2. **移植版**：把 `get_compact_fet` 缓存决策链 + `compact_fet_to_tags` 移植成 TS 服务方法，网络层换 fetch，落盘逻辑复用
3. 每一步对照 `cordis_inspect` 验证服务/事件/工具注册

## 6. 参考资料

- 数据层实现：`src/data_manager.py`、`src/tools.py`
- prefetch 入口：`dsfootball_cli.py` `cmd == "prefetch"`（619-675 行）
- harness 插件编写：`cordis-plugin-development` skill、`editing-cordis-compositions` skill
- Cordis 概念：`docs/cordis-primer.zh.md`（deepseek-harness 仓库）
