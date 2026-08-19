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
   开源参考实现             开源规范                 开源（harness-plugin）
   （python 引擎）
```

- **Fetcher**：负责抓数据（Lota API 或任何自建源），按 `cache_format_spec.md` 的格式写进 `cacheDir`。
  参考实现 = python 引擎 `python-engine/src/data_manager.py`（随仓库发布），
  密钥一律走环境变量 `LOTA_API_KEY`，不写进代码。
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

参考实现（python 引擎 `src/data_manager.py`）接的是 Lota，端点如下（本协议只描述形状，不保证长期稳定）：

| 端点 | 参数 | 返回 |
|---|---|---|
| `GET /matches` | `date` 或 `start_date`+`end_date`、`type`（jingcai/beidan/all）、`lota_id`、`is_jingcai` | `{data:{matches:[...], total}}` |
| `GET /compact-fet` | `lota_id` | compact-fet 原始 JSON |

- 鉴权：请求头 `X-API-Key`。
- `/matches` 服务器有 limit（默认 500），范围拉取需 `limit`+`offset` 分页拉完。

> ⚠️ 这节只是「参考实现」说明。**密钥不随仓库发布**：`LOTA_API_KEY` 由用户/维护者通过
> 环境变量注入（插件 `bridge.js` 会直读 `~/.zshrc` 注入子进程 env）。

## 4. 参考实现（python 引擎）接入位置

参考实现随仓库发布：`python-engine/src/data_manager.py`（DataManager）+ `src/tools.py`。
数据获取入口是引擎的桥 `prepare`（`POST /ds-run {func:"prepare"}`，live 强制刷新 /
replay 缓存优先），也等价于 `dsfootball_cli.py dashboard` 的刷新逻辑。

接入方式：

1. **配 key**：

   ```bash
   export LOTA_API_KEY=...                        # 数据源密钥（找维护者要）
   ```

2. **触发**（二选一）：

   ```bash
   echo '{"func":"prepare","day":"2026-08-14","opts":{"mode":"live","jingcai_only":true}}' \
     | python3 -m src.bridge                                  # 桥 prepare（在 python-engine/ 下）
   python3 dsfootball_cli.py dashboard                        # CLI 刷新 + 看板
   ```

3. **效果**：`prepare` 跑完，`cacheDir` 里就有了 `matches/`、`features/`、`tags/`，插件的 `lota_*` 工具即可读。

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
| 要抓真实数据 | python 引擎数据层 + `LOTA_API_KEY` | 按 §4 配好 key，跑桥 prepare / CLI dashboard |
| 自有数据源 | 无 | 按 `cache_format_spec.md` 自己写缓存 |
