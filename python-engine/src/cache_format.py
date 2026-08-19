"""
CacheFormat seam —— 数据文件格式抽象 + 默认 JSON 实现。

设计目标（见 docs/data_manager_open_source_design.md）：
  开源发布只带「本地缓存读取 + 文件格式规范」；
  网络层（Fetcher）与文件格式（CacheFormat）都是可插拔 seam。

CacheFormat 是文件格式 seam：用户自定义盘上格式 / 存储后端时，
只需满足 read / write / keys 三个方法，或直接继承 DefaultJsonFormat 覆写。
DefaultJsonFormat 把以下复杂度全部藏在这三个小方法之后（deep module）：
  - data/ 的路径布局
  - 原子写入（.tmp + os.replace）
  - features 三种历史形状的归一化（见 normalize_feature）

kind 取值（常量见下）:
  matches / features / tags / predicts / orders / blacklist
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CacheFormat",
    "DefaultJsonFormat",
    "normalize_feature",
    "KIND_MATCHES", "KIND_FEATURES", "KIND_TAGS",
    "KIND_PREDICTS", "KIND_ORDERS", "KIND_BLACKLIST",
    "ALL_KINDS",
]

# ── kind 常量 ──────────────────────────────────────────────
KIND_MATCHES = "matches"
KIND_FEATURES = "features"
KIND_TAGS = "tags"
KIND_PREDICTS = "predicts"
KIND_ORDERS = "orders"
KIND_BLACKLIST = "blacklist"

ALL_KINDS = (
    KIND_MATCHES, KIND_FEATURES, KIND_TAGS,
    KIND_PREDICTS, KIND_ORDERS, KIND_BLACKLIST,
)


# ═══════════════════════════════════════════════════════════
# 文件格式 seam
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class CacheFormat(Protocol):
    """文件格式 seam：读 / 写 / 枚举三种能力。

    read  未命中（文件不存在 / 无该 key）返回 None。
    write 幂等覆盖；实现负责创建目录与原子性。
    keys  返回该 kind 下全部 key（未命中则空列表）。

    kind 语义见模块 docstring；value 是 JSON 可序列化的 Python 对象。
    """

    def read(self, kind: str, key: str) -> Any | None: ...

    def write(self, kind: str, key: str, value: Any) -> None: ...

    def keys(self, kind: str) -> list[str]: ...


# ═══════════════════════════════════════════════════════════
# features 形状归一化
# ═══════════════════════════════════════════════════════════

def normalize_feature(raw: dict) -> dict:
    """把 features 缓存的三种历史形状归一化成同一种内存表示。

    三种形状（见 docs/cache_format_spec.md）:
      A 顶层:  {success, lota_id, lang, score, compact_fet, metadata, api_info, _cached_at}
      B 子对象: {data: {compact_fet, score, match}, _cached_at}
      C 负缓存: {_api_failed, lota_id, _cached_at}

    归一化后（caller 永远只看到这一种）:
      {
        lota_id, compact_fet, score, _cached_at,
        match?: dict,             # 存在时
        _api_failed?: True,       # 负缓存桩
        success?, lang?, metadata?, api_info?,  # 形状 A 的透传字段
      }
    """
    if raw.get("_api_failed"):
        return {
            "lota_id": raw.get("lota_id"),
            "compact_fet": "",
            "score": "",
            "_cached_at": raw.get("_cached_at"),
            "_api_failed": True,
        }

    inner = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    match = raw.get("match") or inner.get("match")
    compact_fet = raw.get("compact_fet") or inner.get("compact_fet") or ""
    score = raw.get("score") or inner.get("score") or ""

    out: dict[str, Any] = {
        "lota_id": raw.get("lota_id"),
        "compact_fet": compact_fet,
        "score": score,
        "_cached_at": raw.get("_cached_at"),
    }
    if match:
        out["match"] = match
    # 透传形状 A 的其余顶层字段，避免 read→write 往返丢失信息
    for k in ("success", "lang", "metadata", "api_info"):
        if k in raw:
            out[k] = raw[k]
    return out


# ═══════════════════════════════════════════════════════════
# 默认 JSON 实现（canonical data/ 布局）
# ═══════════════════════════════════════════════════════════

class DefaultJsonFormat:
    """data/ 目录下的 JSON 文件布局（canonical cache layout v1）。

    kind → 目录映射:
      matches  → <root>/matches/<date>.json        (list[dict])
      features → <root>/features/<lota_id>.json    (dict, 读时归一化)
      tags     → <root>/tags/<lota_id>.json        ({lota_id, generated_at, sections})
      predicts → <root>/predicts/<lota_id>.json    (list[dict])
      orders   → <root>/orders/<lota_id>.json      (list[dict])
      blacklist→ <root>/blacklist.json             (list[str], 单文件)
    """

    _DIR_FOR_KIND = {
        KIND_MATCHES: "matches",
        KIND_FEATURES: "features",
        KIND_TAGS: "tags",
        KIND_PREDICTS: "predicts",
        KIND_ORDERS: "orders",
    }
    _BLACKLIST_FILENAME = "blacklist.json"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ── 路径解析 ──
    def _path_for(self, kind: str, key: str) -> Path:
        if kind == KIND_BLACKLIST:
            return self.root / self._BLACKLIST_FILENAME
        subdir = self._DIR_FOR_KIND.get(kind)
        if subdir is None:
            raise ValueError(f"unknown kind: {kind!r} (expected one of {ALL_KINDS})")
        return self.root / subdir / f"{key}.json"

    def _dir_for(self, kind: str) -> Path:
        if kind == KIND_BLACKLIST:
            return self.root
        subdir = self._DIR_FOR_KIND.get(kind)
        if subdir is None:
            raise ValueError(f"unknown kind: {kind!r} (expected one of {ALL_KINDS})")
        return self.root / subdir

    # ── CacheFormat 实现 ──
    def read(self, kind: str, key: str) -> Any | None:
        path = self._path_for(kind, key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if kind == KIND_FEATURES and isinstance(raw, dict):
            return normalize_feature(raw)
        return raw

    def write(self, kind: str, key: str, value: Any) -> None:
        path = self._path_for(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(value, ensure_ascii=False, indent=2)
        self._atomic_write(path, text)

    def keys(self, kind: str) -> list[str]:
        if kind == KIND_BLACKLIST:
            return []
        subdir = self._dir_for(kind)
        if not subdir.exists():
            return []
        return sorted(p.stem for p in subdir.glob("*.json"))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """原子写入：先写临时文件再 os.replace，避免并发读到半截文件。"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
