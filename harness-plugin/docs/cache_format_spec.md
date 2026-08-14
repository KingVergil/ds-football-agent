# 缓存文件格式规范 v1（canonical cache layout）

> 这是 ds_agents 数据层的**盘上缓存格式**规范，随开源代码一起发布。
> 默认实现：`src/cache_format.py::DefaultJsonFormat`。
> 用户若要自定义格式 / 更换存储后端，只需实现 `CacheFormat` 的
> `read` / `write` / `keys` 三个方法（或继承 `DefaultJsonFormat` 覆写），
> 并保证**归一化后的内存表示**与本文一致。

## 1. 缓存根目录与布局

默认根目录 `data/`（可通过 `DefaultJsonFormat(root=...)` 或环境变量配置）。
每个 `kind` 对应一个子目录，`key` 是文件名（不含 `.json` 后缀）：

| kind | key 示例 | 文件路径 | 内容类型 |
|---|---|---|---|
| `matches` | `2026-06-10` | `matches/2026-06-10.json` | `list[match]` |
| `features` | `Lota4579740` | `features/Lota4579740.json` | `dict`（读时归一化，见 §2） |
| `tags` | `Lota4459717` | `tags/Lota4459717.json` | `dict`（见 §4） |
| `predicts` | `Lota4579740` | `predicts/Lota4579740.json` | `list[dict]` |
| `orders` | `Lota4579740` | `orders/Lota4579740.json` | `list[dict]` |
| `blacklist` | （忽略） | `blacklist.json` | `list[str]` |

- 所有文件 UTF-8、JSON。
- 写入必须**原子**（临时文件 + rename），避免并发读到半截文件。
- `read` 未命中返回 `None`；`keys(kind)` 返回该 kind 下全部 key（按文件名排序）。
- `blacklist` 是根目录单文件，`keys` 恒为空列表，`read('blacklist', any)` 返回 `list[str]`。

## 2. features 归一化契约（重点）

`features` 目录历史上存在**三种形状**，`DefaultJsonFormat.read('features', id)`
必须把它们归一化成同一种内存表示，caller 永远看不到形状差异。

三种历史形状：

```jsonc
// 形状 A：字段在顶层
{ "success": true, "lota_id": "Lota4579740", "lang": "zh", "score": "2:1",
  "compact_fet": "▋联赛类型: …", "metadata": {…}, "api_info": {…}, "_cached_at": "…" }

// 形状 B：字段在 data 子对象下
{ "data": { "compact_fet": "…", "score": "…", "match": {…} }, "_cached_at": "…" }

// 形状 C：负缓存桩（网络失败占位）
{ "_api_failed": true, "lota_id": "Lota4579740", "_cached_at": "…" }
```

归一化后的内存表示（`normalize_feature` 的输出）：

```jsonc
{
  "lota_id": "Lota4579740",
  "compact_fet": "…",        // 文本；形状 C 为空串 ""
  "score": "2:1",            // 文本；可能为空串 ""
  "_cached_at": "…",         // 缺失时为 None
  "match": {…},              // 仅在存在时出现
  "_api_failed": true,       // 仅负缓存桩出现
  // 形状 A 的透传字段（read→write 往返不丢信息）：
  "success": true, "lang": "zh", "metadata": {…}, "api_info": {…}
}
```

规则：
- 取字段优先级：顶层 > `data` 子对象（`raw.get("compact_fet") or inner.get("compact_fet")`）。
- 负缓存桩（`_api_failed` 为真）直接短路，只保留 `lota_id / compact_fet="" / score="" / _cached_at / _api_failed`。
- `write` 不做归一化，原样落盘；`read` 时归一化。因此「读到 B 再写回」会把 B 规范化成 A，这是预期行为。

## 3. matches 文件

`list[match]`，每个 match 至少包含（字段可扩展，多余字段保留）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `lota_id` | str | 比赛唯一 ID |
| `home_name` / `away_name` | str | 主/客队名 |
| `league_name` | str | 联赛 |
| `match_time` | str | 开赛时间 `YYYY-MM-DD HH:MM:SS`（北京时间） |
| `week` | str | 周几 |
| `venues_name` | str | 球场 |
| `score` | str | 比分 `"2:1"` |
| `match_type` / `state` / `state_name` | str/int/str | 赛事类型 / 状态码 / 状态名 |
| `beidan_number` / `jingcai_number` | str | 北单 / 竞彩编号 |
| `poly_slug` | str | 多语言 slug |

## 4. tags 文件

```jsonc
{
  "lota_id": "Lota4459717",
  "generated_at": "…",            // ISO 时间戳
  "sections": { "fair-odds": "…", "asian-handicap-crown": "…", "…": "…" }
}
```

`tags` 是 compact-fet 文本按 `_SECTION_RULES` 切出的段落（`slug → 段落文本`）。
已定义的 slug（`src/tools.py::_SECTION_RULES`）：

```
match-head, match-history, rank-info, home-recent, away-recent, lineup,
betfair-eu, fair-odds, discrete-odds, betfair-buysell, eu-odds-pinnacle,
asian-handicap-crown, asian-handicap-macau, asian-handicap-pinnacle,
over-under-crown, over-under-macau, goal-bonus, score-bonus
```

## 5. 自定义格式（CacheFormat seam）

用户不需要理解上述布局的任意部分，只需保证三个方法行为一致：

```python
from typing import Any, Protocol

class CacheFormat(Protocol):
    def read(self, kind: str, key: str) -> Any | None: ...   # 未命中 → None
    def write(self, kind: str, key: str, value: Any) -> None: ...
    def keys(self, kind: str) -> list[str]: ...              # 未命中 → []
```

- 自定义后端（parquet / SQLite / 对象存储）实现这三个方法即可。
- 若只改 `features` 的读取来源（其余仍用 JSON），继承 `DefaultJsonFormat` 只覆写 `read`。
- 无论后端如何，`read('features', id)` 返回的必须是 §2 的归一化表示。

## 6. 与网络层的边界

本文只定义**盘上格式**。数据从哪来（Lota API / 自建源）由 `Fetcher` 负责，
不在本规范范围内。Fetcher 的职责、接入位置、缓存产出见
[`fetcher_protocol.md`](fetcher_protocol.md)（数据获取层接入协议）。
开源仓库只发布读缓存工具 + 本规范 + Fetcher 协议；网络实现（`lota_fetcher.js`）单独分发。
