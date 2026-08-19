# data_manager 开源插件化设计：可插拔数据源 + 自定义文件格式

> 目标：把 `src/data_manager.py` 从"绑死 Lota API + 私人缓存路径"解耦成
> 可开源发布的形态。发布时只带**本地缓存读取 + 文件格式规范**；
> 网络部分由用户按协议自行实现。设计词汇遵循 codebase-design（seam / adapter / interface / depth）。

## 1. 目标与约束

- 开源发布，**不得**包含私有内容：Lota API 的 `BASE_URL`、`X-API-Key` 读取逻辑（`~/.claude/settings.json`）。
- 三种使用方式（对应用户需求）：
  1. **本地缓存目录读取**（默认发布能力）
  2. **网络 + 本地**（正规模式，网络实现由用户提供）
  3. **数据文件格式自定义**（用户可换盘上格式 / 存储后端）
- 保留现有业务规则：TTL / 负缓存 / live-strict 缓存策略，tag 切分与赔率提取。

## 2. 术语（本设计统一使用）

| 术语 | 含义 | 在本设计中的位置 |
|---|---|---|
| **seam** | 不编辑当前位置就能改变行为的地方（= module 的 interface 所在位置） | 3 个：DataSource / Fetcher / CacheFormat |
| **adapter** | 在 seam 上满足某 interface 的具体实现 | LocalCacheSource、CachingDataSource、DefaultJsonFormat |
| **interface** | caller 正确使用 module 必须知道的一切（签名 + 不变量 + 错误模式 + 配置） | 下文三张 Protocol |
| **depth** | 小 interface 后藏大量行为 | CachingDataSource：缓存决策链全藏在小接口后 |

判断 seam 是否成立的依据：**"一个 adapter 是假设的 seam，两个 adapter 才是真 seam"**。
- Fetcher 有 ≥2 个真 adapter：用户自己的网络实现、测试用 FakeFetcher、文档示例。
- CacheFormat 有 ≥2 个真 adapter：DefaultJsonFormat、用户自定义（DB/parquet/云）。
- DataSource 有 ≥2 个真 adapter：LocalCacheSource（只读）、CachingDataSource（网络+本地）、MemorySource（测试）。

## 3. 分层与三个 seam

```
callers: agent.py / prompt_builder.py / chuan_guan_dog.py / dsfootball_cli.py
        │  （仍然只 import DataManager，方法名不变）
        ▼
DataManager  ── 薄 facade，委托 DataSource
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│  seam #1  DataSource / SyncableSource  （读数据的统一入口）│
│   ├─ LocalCacheSource     adapter：只读缓存目录（发布）      │
│   ├─ CachingDataSource    adapter：网络+本地+策略（发布）    │
│   └─ MemorySource         adapter：测试用                   │
└───────────────────────────┬─────────────────────────────┘
                            │ 内部组合两个内部 seam
        ┌───────────────────┴───────────────────┐
        ▼                                        ▼
┌──────────────────────┐              ┌──────────────────────┐
│ seam #2  Fetcher      │              │ seam #3  CacheFormat │
│ 网络原语（用户实现）    │              │ 盘上格式（用户自定义） │
│  ├─ LotaFetcher(占位) │              │  ├─ DefaultJsonFormat │
│  └─ 用户自己的 API     │              │  └─ 用户自定义/DB      │
└──────────────────────┘              └──────────────────────┘
```

## 4. 三个 interface 定义（Python Protocol）

### 4.1 seam #1 — DataSource（读）/ SyncableSource（网络操作）

```python
class DataSource(Protocol):
    """读数据的统一入口。所有 caller 只依赖这张小接口。"""
    # match
    def get_matches_by_date(self, date: str, lottery_type: str = "jingcai") -> list[dict]: ...
    def get_match(self, lota_id: str, refresh: bool = False) -> dict | None: ...
    # compact-fet
    def get_compact_fet(self, lota_id: str, refresh: bool = False) -> dict | None: ...
    def get_compact_fet_text(self, lota_id: str) -> str: ...
    # tags / 段落
    def get_tags(self, lota_id: str) -> dict[str, str]: ...
    def get_sections(self, lota_id: str, slugs: list[str]) -> str: ...
    # 赔率
    def get_odds(self, lota_id: str) -> dict: ...
    # 关联查询
    def get_predictions(self, lota_id: str) -> list[dict]: ...
    def get_orders(self, lota_id: str) -> list[dict]: ...
    def get_match_context(self, lota_id: str) -> dict: ...
    # 本地运维
    def get_blacklist(self) -> set[str]: ...
    def check_data_freshness(self, day: str | None = None) -> bool: ...

class SyncableSource(Protocol):
    """只有带网络能力的 source 才实现。本地只读模式不承诺这些。"""
    def set_live_mode(self, live: bool) -> None: ...
    def refresh_matches_cache(self, date: str, with_jc_odds: bool = False) -> list[dict]: ...
    def refresh_matches_range(self, start: str, end: str, with_jc_odds: bool = False) -> dict: ...
    def refresh_scores(self) -> None: ...
    def prefetch(self, day: str | None = None, jingcai_only: bool = False,
                 with_jc_odds: bool = False) -> dict: ...
```

设计理由：把只读面（DataSource）与网络操作面（SyncableSource）分开——本地模式不会向 caller 承诺它做不到的事（refresh 会明确抛 `ReadOnlySourceError`），caller 面更小。

### 4.2 seam #2 — Fetcher（网络原语，**用户实现**）

```python
class Fetcher(Protocol):
    """网络层原语。开源发布只给这张协议 + 示例，不给 Lota 实现。"""
    def fetch_matches_by_date(self, date: str, lottery_type: str = "jingcai",
                              is_jingcai: bool = False) -> list[dict]: ...
    def fetch_matches_by_date_range(self, start: str, end: str, lottery_type: str = "jingcai",
                                    is_jingcai: bool = False) -> list[dict]: ...
    def fetch_match_by_id(self, lota_id: str) -> dict | None: ...
    def fetch_compact_fet(self, lota_id: str) -> dict | None: ...
```

CachingDataSource 只依赖这 4 个方法做缓存与刷新；用户的网络实现可以是 Lota、自建 API、爬虫、任意数据源，只要满足这张协议。

### 4.3 seam #3 — CacheFormat（盘上格式，**用户可自定义**）

```python
# kind ∈ {'matches', 'features', 'tags', 'predicts', 'orders', 'blacklist'}
class CacheFormat(Protocol):
    """文件格式 seam：3 个方法，深度藏在 DefaultJsonFormat 内部。"""
    def read(self, kind: str, key: str) -> object | None: ...
    def write(self, kind: str, key: str, value: object) -> None: ...
    def keys(self, kind: str) -> list[str]: ...
```

刻意选**小而深**的 3 方法接口：用户自定义格式只需重写 3 个方法（读/写/枚举），原子写入、路径布局、**形状归一化**、时间戳全部藏在 adapter 内部。

## 5. 文件格式规范 v1（canonical cache layout）

`DefaultJsonFormat` 落地在缓存根目录（默认 `lota_data/`，可配置）：

| kind | key | 文件路径 | 内容 schema |
|---|---|---|---|
| `matches` | `2026-06-10` | `matches/2026-06-10.json` | `list[match]`；match 字段：`lota_id, home_name, away_name, league_name, match_time, week, venues_name, score, match_type, state, state_name, beidan_number, jingcai_number, poly_slug`（可扩展） |
| `features` | `Lota4579740` | `features/Lota4579740.json` | compact-fet 原始 JSON（见下） |
| `tags` | `Lota4459717` | `tags/Lota4459717.json` | `{"lota_id", "generated_at", "sections": {slug: text}}` |
| `predicts` | `Lota4579740` | `predicts/Lota4579740.json` | `list[prediction]` |
| `orders` | `Lota4579740` | `orders/Lota4579740.json` | `list[order]` |
| `blacklist` | `*`（忽略 key） | `blacklist.json` | `list[lota_id]` |

**features 的两种历史形状（归一化契约）**——实测存在两种：

```jsonc
// 形状 A：字段在顶层（真实文件 Lota4579740.json）
{ "success": true, "lota_id": "Lota4579740", "lang": "zh", "score": "2:1",
  "compact_fet": "▋联赛类型: …", "metadata": {…}, "api_info": {…}, "_cached_at": "…" }

// 形状 B：字段在 data 子对象下（历史代码写过）
{ "data": { "compact_fet": "…", "score": "…", "match": {…} }, "_cached_at": "…" }

// 形状 C：负缓存桩
{ "_api_failed": true, "lota_id": "Lota4579740", "_cached_at": "…" }
```

**规范要求**：`DefaultJsonFormat.read('features', id)` 必须把 A/B 归一化成**同一种内存表示**
`{ lota_id, score, compact_fet, match, _cached_at?, _api_failed? }`，caller 永远看不到形状差异；
`write` 只写规范形状 A。这样"用户自定义格式"只需保证归一化后的内存表示一致，盘上怎么存随意。

**tags 的 sections slug**（切分规则见 `src/tools.py::_SECTION_RULES`，19 个）：
`match-head, match-history, rank-info, home-recent, away-recent, lineup, betfair-eu, fair-odds,
discrete-odds, betfair-buysell, eu-odds-pinnacle, asian-handicap-crown, asian-handicap-macau,
asian-handicap-pinnacle, over-under-crown, over-under-macau, goal-bonus, score-bonus`

## 6. 三种模式用法（开源后 README 直接照抄）

```python
# ── 模式 1：本地缓存目录读取（开源默认，零网络）──
from ds_agents.data import DataManager, LocalCacheSource

source = LocalCacheSource(cache_dir="lota_data")   # 只读；refresh 抛 ReadOnlySourceError
dm = DataManager(source)
dm.get_match_context("Lota4579740")

# ── 模式 2：网络 + 本地（用户提供 Fetcher）──
from ds_agents.data import DataManager, CachingDataSource, Fetcher

class MyFetcher(Fetcher):            # 用户自己的数据源
    def fetch_compact_fet(self, lota_id):
        return call_my_api(lota_id)  # ← 私有 URL/key 留在这里
    # …实现其余 3 个方法

source = CachingDataSource(fetcher=MyFetcher(), cache_dir="lota_data", live_strict=True)
dm = DataManager(source)
dm.prefetch(day="2026-08-03")        # 网络 + 落盘 + 缓存策略

# ── 模式 3：自定义文件格式 ──
from ds_agents.data import LocalCacheSource, DefaultJsonFormat

class ParquetFormat(DefaultJsonFormat):   # 或直接实现 CacheFormat 3 方法
    def read(self, kind, key): ...        # 从 parquet / DB / 云读

source = LocalCacheSource(cache_dir=..., format=ParquetFormat())
```

## 7. 开源发布清单

**发布**：
- 三个 interface：`DataSource` / `SyncableSource` / `Fetcher` / `CacheFormat`
- 三个 adapter：`LocalCacheSource`、`CachingDataSource`、`DefaultJsonFormat`
- 缓存策略实现（TTL / 负缓存 / live-strict，从现有 `get_compact_fet` 决策链搬）
- 纯函数：`compact_fet_to_tags`、`extract_odds`、`_parse_handicap_text`（无网络依赖）
- `MemorySource` / `FakeFetcher`（测试用）+ 文件格式规范文档 + 示例 Fetcher

**不发布**（移出核心包）：
- Lota 网络实现 → 放 `examples/fetchers/lota.py` 或 `fetchers/`，URL/key 走环境变量占位，README 说明"自行填充"
- `~/.claude/settings.json` 的 key 读取逻辑

## 8. 迁移步骤（从现有 data_manager.py 拆）

1. **抽 CacheFormat**：把 `matches/features/tags/predicts/orders/blacklist` 的读写入 `DefaultJsonFormat`，实现 §5 归一化。
2. **抽 Fetcher**：把 `_get` / `fetch_matches_by_date` / `_by_date_range` / `fetch_match_by_id` / `fetch_compact_fet` 移入 `LotaFetcher`（放 examples，不发布）。
3. **抽缓存策略**：把 `get_compact_fet` 决策链（TTL/负缓存/live-strict/score 保留）移入 `CachingDataSource`。
4. **LocalCacheSource**：只读，直接走 CacheFormat；refresh 方法抛 `ReadOnlySourceError`。
5. **DataManager 瘦身**：保留现有全部方法名，变成委托 DataSource 的 facade，模块级便捷函数不变 → 现有 caller 无感知。
6. **加测试**：`FakeFetcher` + 临时缓存目录，覆盖三种模式 + 归一化 + 负缓存。

## 9. 关键设计取舍（供 review）

- **DataSource 拆只读面 / 网络面**：本地模式不承诺 refresh。代价是多一张 Protocol；收益是 caller 不会在本地模式踩到静默空行为。
- **CacheFormat 用 3 个泛型方法而非 12 个类型化方法**：换深度（小接口 + 归一化藏在内部）。代价是失去编译期类型检查；收益是用户自定义格式成本降到 3 个方法。
- **DataManager 保留为 facade**：迁移零破坏，老 caller 和脚本（`dsfootball_cli.py`）不动。
