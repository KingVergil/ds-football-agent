"""
DSFootball Python CLI — 统一数据访问层

封装 match / compact-fet / tags 的缓存和 API 访问，
提供按 lota_id 关联查询预测、订单、赔率的便捷方法。

规则:
  - 已完场比赛: 优先本地缓存, 没有再走 API
  - 未开赛/进行中: 先查缓存, 可强制刷新
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone

import requests

from .tools import (
    _SECTION_RULES,
    _parse_handicap_text,
    extract_odds,
    compact_fet_to_tags as _compact_fet_to_tags,
)


# ═══════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════

BASE_URL = "http://deepdata.lota.tv/predictions/api/v2"

# 数据根目录
PROJECT_ROOT = Path(__file__).parent.parent
DATA_ROOT = PROJECT_ROOT / "lota_data"

MATCHES_DIR = DATA_ROOT / "matches"
FEATURES_DIR = DATA_ROOT / "features"
TAGS_DIR = Path(__file__).parent.parent / "lota_data" / "tags"
PREDICTS_DIR = Path(__file__).parent.parent / "lota_data" / "predicts"
ORDERS_DIR = Path(__file__).parent.parent / "lota_data" / "orders"

# 确保目录存在
for d in [MATCHES_DIR, FEATURES_DIR, TAGS_DIR, PREDICTS_DIR, ORDERS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════

class LotaAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.is_fatal = status_code == 403


# ═══════════════════════════════════════════════
# API 客户端
# ═══════════════════════════════════════════════

def _get_api_key() -> str:
    settings_dir = Path.home() / ".claude" / "settings.json"
    if settings_dir.exists():
        try:
            cfg = json.loads(settings_dir.read_text())
            return cfg.get("lota", {}).get("api_key", "") or cfg.get("lotaKey", "")
        except Exception:
            pass
    return os.environ.get("LOTA_API_KEY", "")


def _headers() -> dict:
    return {"X-API-Key": _get_api_key(), "Content-Type": "application/json"}


def _get(path: str, params: dict = None) -> dict | list | None:
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        if resp.status_code == 403:
            raise LotaAPIError(403, "Lota API key 过期或超限")
        if resp.status_code != 200:
            print(f"[lota] {url} → {resp.status_code}")
            return None
        return resp.json()
    except requests.RequestException as e:
        print(f"[lota] {url} → {e}")
        return None


# ═══════════════════════════════════════════════
# DataManager
# ═══════════════════════════════════════════════

class DataManager:
    """统一数据访问层 — 单例"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ═══════════════════════════════════════════
    # Blacklist
    # ═══════════════════════════════════════════

    def get_blacklist(self) -> set[str]:
        """读取黑名单 lota_id（已完赛/异常比赛，禁止下注）"""
        path = DATA_ROOT / "blacklist.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    # ═══════════════════════════════════════════
    # Match
    # ═══════════════════════════════════════════

    def fetch_matches_by_date(self, date_str: str, lottery_type: str = "jingcai") -> list[dict]:
        """API 查询某日比赛列表"""
        params = {"date": date_str}
        if lottery_type and lottery_type != "all":
            params["type"] = lottery_type
        data = _get("/matches", params)
        if not data:
            return []
        result = data.get("data") or {}
        if isinstance(result, dict):
            matches = result.get("matches") or result.get("match") or []
        else:
            matches = result if isinstance(result, list) else []
        return matches if isinstance(matches, list) else []

    def fetch_matches_by_date_range(self, start: str, end: str, lottery_type: str = "jingcai") -> list[dict]:
        """API 查询日期范围内的比赛"""
        params = {"start": start, "end": end}
        if lottery_type and lottery_type != "all":
            params["type"] = lottery_type
        data = _get("/matches/range", params)
        if not data:
            return []
        matches = data.get("data") or data.get("matches") or []
        return matches if isinstance(matches, list) else []

    def fetch_match_by_id(self, lota_id: str) -> Optional[dict]:
        """API 查询单场比赛详情（含比分）"""
        data = _get(f"/matches/{lota_id}")
        return data.get("data") if data else None

    def fetch_compact_fet(self, lota_id: str) -> Optional[dict]:
        """API 查询 compact-fet"""
        return _get("/compact-fet", {"lota_id": lota_id})

    def get_cached_matches(self, date_str: str, lottery_type: str = "jingcai") -> list[dict]:
        """本地缓存: 某日比赛列表"""
        path = MATCHES_DIR / f"{date_str}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                matches = data if isinstance(data, list) else data.get("matches", [])
                if lottery_type and lottery_type != "all":
                    matches = [m for m in matches if m.get("lottery_type", "") == lottery_type]
                return matches
            except Exception:
                pass
        return []

    def save_matches_cache(self, date_str: str, matches: list[dict]) -> None:
        """写入比赛列表缓存"""
        (MATCHES_DIR / f"{date_str}.json").write_text(
            json.dumps(matches, ensure_ascii=False), encoding="utf-8"
        )

    def get_cached_match(self, lota_id: str) -> Optional[dict]:
        """从本地缓存查找单场比赛（扫描 matches + features）"""
        # 1. 从 features 缓存中获取（含 match 基础信息）
        feat = self.get_cached_compact_fet(lota_id)
        if feat:
            match = feat.get("match") or (feat.get("data") or {}).get("match")
            if match:
                return match
        # 2. 扫描 matches 目录（lottery_type="all" 避免默认 "jingcai" 过滤掉 None 值）
        for d in self._recent_dates(30):
            for m in self.get_cached_matches(d, lottery_type="all"):
                if m.get("lota_id") == lota_id:
                    return m
        return None
    
    def refresh_score_match(self, lota_id: str) -> Optional[dict]:
        """
        通过 API 查询单场比赛最新比分/状态，更新写回 matches + features 缓存。
        仅对已完场(state==6)的比赛请求 API。返回 API match dict 或 None。
        """
        # 先查本地缓存判断是否需要请求 API
        cached = self.get_cached_match(lota_id)
        if cached:
            state = cached.get("state", 0)
            score = cached.get("score", "")
            if state == 6 and score and score != ":" and len(score) >= 3:
                return cached  # 已有完场比分，无需 API
            if state not in (6, 0):  # 未开赛/进行中(state 0-5)，不请求 API
                return None

        fetched = self.fetch_match_by_id(lota_id)
        if not fetched:
            return None

        state = fetched.get("state", 0)
        score = (
            fetched.get("score") or
            f"{fetched.get('home_score', '')}:{fetched.get('away_score', '')}"
        )

        # 更新 matches 日期文件缓存
        for date_file in sorted(MATCHES_DIR.glob("*.json")):
            try:
                raw = json.loads(date_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            if isinstance(raw, dict):
                matches = raw.get("matches", [])
            elif isinstance(raw, list):
                matches = raw
            else:
                continue

            dirty = False
            for m in matches:
                if m.get("lota_id") == lota_id:
                    if state == 6:
                        m["state"] = 6
                        if score and score != ":":
                            m["score"] = score
                    elif state != m.get("state", 0):
                        m["state"] = state
                    dirty = True
                    break

            if dirty:
                if isinstance(raw, dict):
                    raw["matches"] = matches
                else:
                    raw = matches
                date_file.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                break  # 只会在一个日期文件中

        # 同步更新 features 缓存中的 score
        if state == 6 and score and score != ":":
            feat = self.get_cached_compact_fet(lota_id)
            if feat:
                data = feat.get("data") or {}
                data["score"] = score
                feat["data"] = data
                self.save_compact_fet_cache(lota_id, feat)

        return fetched

    def refresh_scores(self):
        """
        扫描本地 matches 缓存中所有已开始但未标记完场的比赛，
        通过 API 查询最新比分/状态，更新写回缓存文件。
        """
        # match_time 是北京时间(UTC+8)，now 也钉死北京时间，避免宿主机时区不一致导致误判
        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        total_updated = 0
        total_checked = 0

        date_files = sorted(MATCHES_DIR.glob("*.json"))
        if not date_files:
            print("[refresh_scores] 无缓存文件")
            return

        print(f"[refresh_scores] 扫描 {len(date_files)} 个日期文件...")

        for date_file in date_files:
            try:
                raw = json.loads(date_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            if isinstance(raw, dict):
                matches = raw.get("matches", [])
            elif isinstance(raw, list):
                matches = raw
            else:
                continue

            if not isinstance(matches, list):
                continue

            dirty = False
            for m in matches:
                lid = m.get("lota_id", "")
                if not lid:
                    continue
                state = m.get("state", 0)
                if state == 6:
                    continue  # 已完场

                # 检查比赛是否已开始（match_time < now）
                mt = m.get("match_time", "")
                if mt:
                    try:
                        mt_clean = mt.replace("T", " ")[:16]
                        if datetime.strptime(mt_clean, "%Y-%m-%d %H:%M") > now:
                            continue  # 未开始
                    except ValueError:
                        pass

                total_checked += 1
                fetched = self.fetch_match_by_id(lid)
                if not fetched:
                    continue

                new_state = fetched.get("state", 0)
                if new_state == 6:
                    score = (
                        fetched.get("score") or
                        f"{fetched.get('home_score', '')}:{fetched.get('away_score', '')}"
                    )
                    m["state"] = 6
                    if score and score != ":":
                        m["score"] = score
                    dirty = True
                    total_updated += 1
                    print(f"  ✅ {lid} {m.get('home_name', '?')} vs {m.get('away_name', '?')}: {score}")

                    # 同步更新 features 缓存中的 score
                    feat = self.get_cached_compact_fet(lid)
                    if feat and score and score != ":":
                        data = feat.get("data") or {}
                        data["score"] = score
                        feat["data"] = data
                        self.save_compact_fet_cache(lid, feat)

                elif new_state != state:
                    m["state"] = new_state
                    dirty = True

                time.sleep(0.05)  # 温和限速

            # 写回日期文件
            if dirty:
                try:
                    if isinstance(raw, dict):
                        raw["matches"] = matches
                    else:
                        raw = matches
                    date_file.write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception as e:
                    print(f"  ❌ 写回 {date_file.name} 失败: {e}")

        print(f"[refresh_scores] 完成: 检查 {total_checked}, 更新 {total_updated} 场")



    def get_match(self, lota_id: str, refresh: bool = False) -> Optional[dict]:
        """
        获取比赛信息。已完场优先本地, 没有再走 API。

        规则:
          - 先查本地缓存
          - 如果缓存命中且 state==6（完场），直接返回（除非 refresh=True）
          - 否则尝试 API 获取最新数据并更新缓存
        """
        cached = self.get_cached_match(lota_id)
        if cached and not refresh:
            state = cached.get("state", 0)
            if state == 6:  # 完场，本地是权威数据
                return cached

        # 走 API
        fetched = self.fetch_match_by_id(lota_id)
        if fetched:
            return fetched
        return cached  # API 失败则返回缓存

    def _recent_dates(self, days: int = 30) -> list[str]:
        return [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    # ═══════════════════════════════════════════
    # Compact-fet（原始 JSON + 缓存）
    # ═══════════════════════════════════════════

    def get_cached_compact_fet(self, lota_id: str) -> Optional[dict]:
        """读取本地 compact-fet 缓存（先 Python CLI 目录，再 JS 项目目录）"""
        # 1. JS 项目 features（主要缓存）
        path = FEATURES_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # 2. Python CLI 自有目录
        py_feat = Path(__file__).parent.parent / "lota_data" / "features" / f"{lota_id}.json"
        if py_feat.exists():
            try:
                return json.loads(py_feat.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def save_compact_fet_cache(self, lota_id: str, data: dict) -> None:
        """写入 compact-fet 缓存"""
        data["_cached_at"] = datetime.now().isoformat()
        (FEATURES_DIR / f"{lota_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def get_compact_fet(self, lota_id: str, refresh: bool = False) -> Optional[dict]:
        """
        获取 compact-fet。优先本地缓存。如果缓存不存在或 refresh=True，走 API。

        失败时写入 negative cache（_api_failed=True），避免后续重复请求同一 ID。
        """
        if not refresh:
            cached = self.get_cached_compact_fet(lota_id)
            if cached:
                if cached.get("_api_failed"):
                    return None  # 已知 API 失败，不再重试
                return cached

        data = self.fetch_compact_fet(lota_id)
        if data:
            self.save_compact_fet_cache(lota_id, data)
        else:
            # 写入 negative cache，避免后续 get_compact_fet / get_tags
            # 等路径再次触发无效 API 请求
            self.save_compact_fet_cache(lota_id, {
                "_api_failed": True,
                "lota_id": lota_id,
            })
        return data

    def get_compact_fet_text(self, lota_id: str) -> str:
        """获取 compact-fet 文本（用于 tag 提取）"""
        data = self.get_compact_fet(lota_id)
        if not data:
            return ""
        inner = data.get("data") or {}
        return inner.get("compact_fet") or data.get("compact_fet") or ""

    # ═══════════════════════════════════════════
    # Tags（compact-fet → 语义段落）
    # ═══════════════════════════════════════════

    def get_tags(self, lota_id: str) -> dict[str, str]:
        """
        获取 tagged sections。优先本地 tags 缓存，没有则从 compact-fet 即时切分。
        """
        # 1. 已缓存的 tags
        cached = self._load_cached_tags(lota_id)
        if cached:
            return cached.get("sections", {})

        # 2. 从 compact-fet 切分
        text = self.get_compact_fet_text(lota_id)
        if not text:
            return {}

        sections = _compact_fet_to_tags(lota_id, None)  # 直接用文本（tools 函数）
        # 先尝试用 tools 的 compact_fet_to_tags（它接受 data dict）
        data = self.get_compact_fet(lota_id)
        if data:
            sections = _compact_fet_to_tags(lota_id, data)
        else:
            # fallback: 直接对文本切分
            sections = self._parse_tags_from_text(text)

        self._save_cached_tags(lota_id, sections)
        return sections

    def _parse_tags_from_text(self, text: str) -> dict[str, str]:
        """纯文本 → tagged sections（不依赖 compact-fet JSON）"""
        hits: list[tuple[str, int]] = []
        for slug, pattern in _SECTION_RULES:
            m = re.search(pattern, text)
            if m:
                hits.append((slug, m.start()))
        hits.sort(key=lambda x: x[1])

        sections: dict[str, str] = {}
        for i, (slug, start) in enumerate(hits):
            end = hits[i + 1][1] if i + 1 < len(hits) else len(text)
            sections[slug] = text[start:end].strip()
        return sections

    def get_sections(self, lota_id: str, slugs: list[str]) -> str:
        """
        按 slug 列表获取指定段落，拼接为 prompt 可用的文本。

        用法:
          context = dm.get_sections(lota_id, ["fair-odds", "asian-handicap-crown"])
          # → "[section:fair-odds]\n公平盘数据:\n..."
        """
        sections = self.get_tags(lota_id)
        parts = []
        for slug in slugs:
            text = sections.get(slug)
            if text:
                parts.append(f"[section:{slug}]\n{text}")
        return "\n\n".join(parts)

    def _load_cached_tags(self, lota_id: str) -> Optional[dict]:
        path = TAGS_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def _save_cached_tags(self, lota_id: str, sections: dict[str, str]) -> None:
        payload = {
            "lota_id": lota_id,
            "generated_at": datetime.now().isoformat(),
            "sections": sections,
        }
        (TAGS_DIR / f"{lota_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    # ═══════════════════════════════════════════
    # Odds（Pinnacle 终盘）
    # ═══════════════════════════════════════════

    def get_odds(self, lota_id: str) -> dict:
        """获取 Pinnacle 终盘赔率（欧赔/亚盘/大小球）"""
        return extract_odds(lota_id, self.get_compact_fet(lota_id))

    # ═══════════════════════════════════════════
    # 关联查询（match → predictions + orders）
    # ═══════════════════════════════════════════

    def get_predictions(self, lota_id: str) -> list[dict]:
        """查询某场比赛的所有预测"""
        path = PREDICTS_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except Exception:
                pass
        return []

    def get_orders(self, lota_id: str) -> list[dict]:
        """查询某场比赛的所有订单"""
        path = ORDERS_DIR / f"{lota_id}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except Exception:
                pass
        return []

    def get_match_context(self, lota_id: str) -> dict:
        """
        一键获取比赛全貌: 基础信息 + 赔率 + 预测 + 订单。

        Returns:
          {
            "lota_id": str,
            "match": dict,         # 比赛基础信息（队名/联赛/时间/比分）
            "score": str,          # 实际比分
            "odds": dict,          # Pinnacle 终盘赔率
            "predictions": list,   # 预测列表
            "orders": list,        # 订单列表
            "tags_summary": str,   # 各 section 的简短摘要
          }
        """
        # 比赛基础信息（从 compact-fet 提取）
        match_info = self._extract_match_info(lota_id)
        score = match_info.get("score", "")

        return {
            "lota_id": lota_id,
            "match": match_info,
            "score": score,
            "odds": self.get_odds(lota_id),
            "predictions": self.get_predictions(lota_id),
            "orders": self.get_orders(lota_id),
            "tags_summary": self._tags_summary(lota_id),
        }

    def _extract_match_info(self, lota_id: str) -> dict:
        """从 compact-fet 文本提取比赛基础信息。

        多级回退:
          1. compact-fet 文本解析（最详细）
          2. features 缓存（含 score）
          3. matches 列表缓存（队名/联赛/时间）  ← 新增兜底
        """
        text = self.get_compact_fet_text(lota_id)
        if not text:
            return self._fallback_match_info(lota_id)

        home = away = league = match_time = ""
        for line in text.split("\n")[:5]:
            if "🆚" in line:
                parts = line.split("🆚")
                if len(parts) == 2:
                    home = parts[0].split(":")[-1].strip() if ":" in parts[0] else parts[0].strip()
                    away = parts[1].strip()
            if "联赛类型:" in line:
                league = line.split(":")[1].split("｜")[0].strip() if ":" in line else ""
            if "时间:" in line and ":" in line:
                time_part = line.split(":", 1)[1].strip() if ":" in line else ""
                match_time = time_part.split("｜")[0].strip() if "｜" in time_part else time_part

        # compact-fet 文本可能没有队名（某些比赛格式不同），用 match list 补全
        if not home or not away:
            fallback = self._fallback_match_info(lota_id)
            if not home:
                home = fallback.get("home", "")
            if not away:
                away = fallback.get("away", "")
            if not league:
                league = fallback.get("league", "")
            if not match_time:
                match_time = fallback.get("match_time", "")

        # score from features
        score = ""
        feat = self.get_cached_compact_fet(lota_id)
        if feat:
            score = (feat.get("data") or {}).get("score", "")

        return {
            "home": home,
            "away": away,
            "league": league,
            "match_time": match_time,
            "score": score,
        }

    def _fallback_match_info(self, lota_id: str) -> dict:
        """三级回退: features 缓存 → matches 列表 → 空 dict"""
        # 1. features 缓存（可能含 score 和 match 子对象）
        feat = self.get_cached_compact_fet(lota_id)
        if feat:
            data = feat.get("data") or {}
            match = feat.get("match") or data.get("match") or {}
            if match:
                return {
                    "home": match.get("home_name", match.get("home", "")),
                    "away": match.get("away_name", match.get("away", "")),
                    "league": match.get("league_name", match.get("league", "")),
                    "match_time": match.get("match_time", ""),
                    "score": data.get("score", feat.get("score", "")),
                }
            return {
                "home": "", "away": "", "league": "", "match_time": "",
                "score": data.get("score", feat.get("score", "")),
            }

        # 2. matches 列表缓存（有完整的队名/联赛/时间）
        match = self.get_cached_match(lota_id)
        if match:
            return {
                "home": match.get("home_name", match.get("home", "")),
                "away": match.get("away_name", match.get("away", "")),
                "league": match.get("league_name", match.get("league", "")),
                "match_time": match.get("match_time", ""),
                "score": match.get("score", ""),
            }

        return {}

    def _tags_summary(self, lota_id: str) -> str:
        """所有 tag section 的简短摘要（用于快速浏览）"""
        sections = self.get_tags(lota_id)
        lines = []
        for slug, text in sorted(sections.items()):
            # 取第一行作为摘要
            first_line = text.split("\n")[0][:120]
            lines.append(f"  [{slug}] {first_line}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════
# 模块级便捷函数（兼容旧代码）
# ═══════════════════════════════════════════════

_dm = DataManager()

get_match = _dm.get_match
get_compact_fet = _dm.get_compact_fet
get_tags = _dm.get_tags
get_sections = _dm.get_sections
get_odds = _dm.get_odds
get_predictions = _dm.get_predictions
get_orders = _dm.get_orders
get_match_context = _dm.get_match_context


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python data_manager.py <lota_id>")
        print("      python data_manager.py --tags <lota_id>")
        sys.exit(1)

    if sys.argv[1] == "--tags":
        lid = sys.argv[2]
        dm = DataManager()
        sections = dm.get_tags(lid)
        for slug, text in sorted(sections.items()):
            print(f"\n{'='*60}")
            print(f"[{slug}] ({len(text)} chars)")
            print(f"{'='*60}")
            print(text[:500])
    else:
        lid = sys.argv[1]
        dm = DataManager()
        ctx = dm.get_match_context(lid)
        print(json.dumps({
            "lota_id": ctx["lota_id"],
            "match": ctx["match"],
            "score": ctx["score"],
            "odds": ctx["odds"],
            "predictions_count": len(ctx["predictions"]),
            "orders_count": len(ctx["orders"]),
        }, ensure_ascii=False, indent=2))
        print(f"\n--- Tags ---")
        print(ctx["tags_summary"][:2000])
