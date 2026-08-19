"""reflect 用「历史同信号比赛」回顾。

用途: 因子生成（reflect）时，把当前因子相关 slug 在历史完场比赛中的
表现（时间从近到远、含赛果）注入 prompt，让因子思考不只看当天订单。

数据源:
  - data/tags/*.json      已切分的 slug 区块（18 个 section）
  - data/matches/*.json   match_time / score / 队名
  - data/features/*.json  兜底 score / match_time（fet 文本 ⏰时间）
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .prompt_builder import truncate_section


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TAGS_DIR = PROJECT_ROOT / "data" / "tags"
MATCHES_DIR = PROJECT_ROOT / "data" / "matches"
FEATURES_DIR = PROJECT_ROOT / "data" / "features"

_TIME_RE = re.compile(r"时间:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)")

# 卡片里优先展示信息量高的信号段（match-head 只是对阵头，放最后）
_SECTION_PRIORITY = [
    "discrete-odds", "fair-odds", "eu-odds-pinnacle",
    "asian-handicap-pinnacle", "asian-handicap-crown", "asian-handicap-macau",
    "over-under-crown", "over-under-macau", "betfair-buysell", "betfair-eu",
    "match-history", "rank-info", "home-recent", "away-recent", "lineup",
    "goal-bonus", "score-bonus", "match-head",
]


class SlugHistoryIndex:
    """tags + matches + features 的只读索引，一次构建供多天复用。"""

    def __init__(self, tags_dir=None, matches_dir=None, features_dir=None):
        self.tags_dir = Path(tags_dir) if tags_dir else TAGS_DIR
        self.matches_dir = Path(matches_dir) if matches_dir else MATCHES_DIR
        self.features_dir = Path(features_dir) if features_dir else FEATURES_DIR
        self._match_meta: dict[str, dict] = {}   # lota_id → {match_time, score, home, away}
        self._sections: dict[str, dict] = {}     # lota_id → {slug: text}
        self._built = False

    # ── 构建 ──

    def _load_match_meta(self) -> None:
        if not self.matches_dir.exists():
            return
        for fp in sorted(self.matches_dir.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            ms = data if isinstance(data, list) else (data.get("data") or data.get("matches") or [])
            for m in ms:
                lid = m.get("lota_id")
                if not lid:
                    continue
                self._match_meta[lid] = {
                    "match_time": m.get("match_time", "") or "",
                    "score": m.get("score", "") or "",
                    "home": m.get("home_name", "") or "",
                    "away": m.get("away_name", "") or "",
                }

    def _load_features_fallback(self) -> None:
        """matches 缓存缺 score / match_time 时，用 features 缓存兜底。"""
        if not self.features_dir.exists():
            return
        for fp in sorted(self.features_dir.glob("*.json")):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            lid = d.get("lota_id")
            if not lid:
                continue
            rec = self._match_meta.setdefault(lid, {"match_time": "", "score": "",
                                                    "home": "", "away": ""})
            if not rec.get("score") and d.get("score"):
                rec["score"] = d.get("score")
            if not rec.get("match_time"):
                text = d.get("compact_fet") or ""
                if isinstance(text, str):
                    m = _TIME_RE.search(text)
                    if m:
                        rec["match_time"] = m.group(1)

    def _load_tags(self) -> None:
        if not self.tags_dir.exists():
            return
        for fp in sorted(self.tags_dir.glob("*.json")):
            try:
                d = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            lid = d.get("lota_id")
            sections = d.get("sections") or {}
            if lid and sections:
                self._sections[lid] = sections

    def build(self) -> None:
        if self._built:
            return
        self._load_match_meta()
        self._load_features_fallback()
        self._load_tags()
        self._built = True

    # ── 查询 ──

    def find(self, slugs: list[str], before: str, max_matches: int = 8,
             min_slugs: int = 1, history_days: int = 90) -> list[dict]:
        """返回 before 之前、包含目标 slugs 的完场比赛，时间从近到远。

        条件:
          - match_time 与 score 都存在（完场且有赛果）
          - match_time < before（字符串比较，ISO 格式）
          - 时间遗忘窗口: match_time >= before - history_days 天（默认近 3 个月）
          - sections 命中 >= min_slugs 个目标 slug
        """
        self.build()
        if not slugs:
            return []
        cutoff = self._cutoff(before, history_days)
        slug_set = set(slugs)
        pool = []
        for lid, sections in self._sections.items():
            hit = [s for s in slug_set if s in sections]
            if len(hit) < min_slugs:
                continue
            meta = self._match_meta.get(lid) or {}
            mt = meta.get("match_time", "")
            score = meta.get("score", "")
            if not mt or not score or mt >= before or mt < cutoff:
                continue
            pool.append({
                "lota_id": lid,
                "match_time": mt,
                "score": score,
                "home": meta.get("home", ""),
                "away": meta.get("away", ""),
                "sections": sections,
                "hit_slugs": hit,
            })
        pool.sort(key=lambda x: x["match_time"], reverse=True)  # 从近到远
        return pool[:max_matches]

    def pool_size(self, slugs: list[str], before: str, min_slugs: int = 1,
                  history_days: int = 90) -> int:
        """命中池总场数（不截断），用于提示 LLM 历史样本量。"""
        self.build()
        if not slugs:
            return 0
        cutoff = self._cutoff(before, history_days)
        slug_set = set(slugs)
        n = 0
        for lid, sections in self._sections.items():
            if sum(1 for s in slug_set if s in sections) < min_slugs:
                continue
            meta = self._match_meta.get(lid) or {}
            mt = meta.get("match_time", "")
            if not mt or not meta.get("score", "") or mt >= before or mt < cutoff:
                continue
            n += 1
        return n

    @staticmethod
    def _cutoff(before: str, history_days: int) -> str:
        """before - history_days 天（'YYYY-MM-DD'，字符串比较即可）。"""
        if history_days <= 0:
            return ""
        try:
            d = datetime.strptime(before[:10], "%Y-%m-%d") - timedelta(days=history_days)
            return d.strftime("%Y-%m-%d")
        except Exception:
            return ""


_INDEX: SlugHistoryIndex | None = None


def get_index() -> SlugHistoryIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = SlugHistoryIndex()
        _INDEX.build()
    return _INDEX


def _card_text(match: dict, slugs: list[str], budget_tokens: int,
               max_sections: int = 3) -> str:
    """单场紧凑卡片：时间 | 对阵 | 比分 + 目标 slug 段落摘要。"""
    lines = [f"- {match['match_time']} | {match['home']} vs {match['away']} "
             f"| 比分 {match['score']}"]
    shown = 0
    per_section = max(80, budget_tokens // max(1, max_sections))
    ordered = sorted(
        set(slugs),
        key=lambda s: _SECTION_PRIORITY.index(s) if s in _SECTION_PRIORITY else 999,
    )
    for slug in ordered:
        if shown >= max_sections:
            break
        sec = match["sections"].get(slug)
        if not sec:
            continue
        body = truncate_section(sec, per_section)
        lines.append(f"  [{slug}]\n  {body}")
        shown += 1
    return "\n".join(lines)


def build_history_block(slugs: list[str], before: str, max_matches: int = 8,
                        budget_tokens: int = 3200, index: SlugHistoryIndex = None,
                        min_slugs: int = 1, history_days: int = 90) -> str:
    """生成注入 reflect 的历史回顾文本（从近到远）。"""
    idx = index or get_index()
    matches = idx.find(slugs, before, max_matches, min_slugs, history_days)
    if not matches:
        return ""
    pool_n = idx.pool_size(slugs, before, min_slugs, history_days)
    slug_desc = ", ".join(dict.fromkeys(slugs))[:120]
    header = (
        f"### 📜 历史同信号比赛回顾（时间从近到远，赛果已定）\n"
        f"匹配池: {pool_n} 场（近 {history_days} 天、{before} 前、含目标数据段且有比分）；"
        f"目标段: {slug_desc}。以下列出最近 {len(matches)} 场：\n"
    )
    per_match = max(200, budget_tokens // max(1, len(matches)))
    per_match = min(per_match, 800)
    cards = [_card_text(m, slugs, per_match) for m in matches]
    return header + "\n".join(cards)
