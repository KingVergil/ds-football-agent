# 数据获取层（Fetcher）接入协议 v1

> 这是 ds_agents 数据层的**网络侧协议**：数据怎么进缓存、缓存怎么被插件读到。
> 与 [`cache_format_spec.md`](cache_format_spec.md)（盘上格式）配套，两者合起来
> 构成完整的「数据协议」。
>
> 一句话：**Fetcher 只负责写缓存，插件只负责读缓存，二者只通过 `cacheDir` 目录解耦。**

## 1. 数据流与职责边界

```
               写缓存                     读缓存
  ┌─────────┐  ────────►  ┌──────────┐  ────────►  ┌────────────────┐
  │ Fetcher │             │ cacheDir │             │ 插件 lota_* 工具 │
  │（网络） │             │ (JSON)   │             │（只读、不触网）   │
  └─────────┘             └──────────┘             └────────────────┘
   私有/自建                开源规范                 开源（harness-plugin）
```

- **Fetcher**：负责抓数据（Lota API 或任何自建源），按 `cache_format_spec.md` 的格式写进 `cacheDir`。含网络与密钥，**不入开源仓库**。
- **插件**：`lota_matches` / `lota_match` / `lota_sections` 只读 `cacheDir`，永远不触网、不含密钥。
- **cacheDir**：唯一契约点。插件通过 `config.cacheDir` 指向它；Fetcher 通过自己的 `DATA_ROOT` 指向同一个目录。

## 2. Fetcher 必须产出的三样东西

Fetcher 抓完数据后，必须把下面三类文件写进 `cacheDir`（格式细节见 `cache_format_spec.md`）：

| 产出 | 路径 | 内容 | 何时写 |
|---|---|---|---|
| 比赛列表 | `matches/<足球日>.json` | `list[match]` | 刷新某日/某范围 |
| compact-fet | `features/<lota_id>.json` | `dict`（三种形状归一化） | 抓单场特征 |
| 段落 | `tags/<lota_id>.json` | `{lota_id, generated_at, sections}` | 由 compact-fet 文本切分 |

`predicts/`、`orders/`、`blacklist.json` 是可选的派生缓存，插件只做计数/关联，不影响分析主流程。

### 足球日窗口（关键）

`matches/<日期>.json` 的 `日期` 是**足球日**，不是自然日。开赛时间 `match_time`（北京时间）落在
`[D 12:01, D+1 12:00]` 的比赛，归入 `D.json`。切分规则：

```
足球日 = (match_time - 12h01m) 的日期部分
```

## 3. Lota API（参考实现）

私有 Fetcher 的参考实现接的是 Lota，端点如下（本协议只描述形状，不保证长期稳定）：

| 端点 | 参数 | 返回 |
|---|---|---|
| `GET /matches` | `date` 或 `start_date`+`end_date`、`type`（jingcai/beidan/all）、`lota_id`、`is_jingcai` | `{data:{matches:[...], total}}` |
| `GET /compact-fet` | `lota_id` | compact-fet 原始 JSON |

- 鉴权：请求头 `X-API-Key`。
- `/matches` 服务器有 limit（默认 500），范围拉取需 `limit`+`offset` 分页拉完。

> ⚠️ 这节只是「参考实现」说明，让拿到私有 Fetcher 的人知道它抓什么。**开源仓库不含
> Lota API 地址、密钥或任何抓取代码。**

## 4. 私有 Fetcher（`lota_fetcher.js`）接入位置

参考实现是单文件 `lota_fetcher.js`（Node ≥ 18，全局 `fetch`），**单独分发给有数据源的用户**，不随开源仓库发布。

拿到它之后：

1. **放哪**：任意目录，推荐放在 `cacheDir` 的父目录下（这样默认 `data/` 就对齐 `cacheDir`）。也可放别处，用环境变量指回：

   ```bash
   export LOTA_DATA_ROOT=/path/to/cacheDir        # 默认 ./data
   export LOTA_API_KEY=...                        # 或写在 ~/.claude/settings.json
   ```

2. **怎么用**：

   ```bash
   node lota_fetcher.js refresh-range 2026-08-01 2026-08-14   # 范围拉比赛 + 切分写盘
   node lota_fetcher.js prefetch 2026-08-14                   # 抓 compact-fet + 切 sections
   node lota_fetcher.js match Lota4579740                     # 单场调试
   ```

3. **效果**：`refresh-range` + `prefetch` 跑完，`cacheDir` 里就有了 `matches/`、`features/`、`tags/`，插件的 `lota_*` 工具即可读。

## 5. 自建数据源（不用 Lota 也行）

Fetcher 协议不绑定 Lota。只要你能产出 `cache_format_spec.md` 定义的缓存文件，插件就能跑：

1. `matches/<足球日>.json`：比赛列表（至少含 `lota_id`/`home_name`/`away_name`/`league_name`/`match_time`/`score`）。
2. `features/<lota_id>.json`：含 `compact_fet` 文本（形状 A 或 B 均可）。
3. `tags/<lota_id>.json`：`sections` 段落，或让插件回退用 `compact_fet` 切分。

**唯一的数据专有点**：`lota_match` 的赔率提取（`odds.js`）依赖 compact-fet 文本里的 Pinnacle/Crown 盘口排版。自建源的 compact-fet 排版不同时，需改 `odds.js`（见其文件头注释）。

## 6. 一句话接入清单

| 你是什么角色 | 需要什么 | 怎么接 |
|---|---|---|
| 只想跑分析 | 缓存数据 | 拿 seed 数据包，`config.cacheDir` 指向它 |
| 要抓真实数据 | 私有 `lota_fetcher.js` + API key | 按 §4 放好、跑 refresh + prefetch |
| 自有数据源 | 无 | 按 `cache_format_spec.md` 自己写缓存 |
