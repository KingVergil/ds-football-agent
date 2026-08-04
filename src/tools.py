"""
DSFootball Python CLI — 数据获取工具

三层架构:
  1. 网络层 — Lota API 查询（比赛列表、compact-fet）
  2. 本地缓存 — JSON 文件缓存（镜像 JS 版 lota_data/）
  3. Tag 提取 — compact-fet 文本 → {slug: 段落文本}（Factor 按 slug 引用数据段）

核心思路:
  compact-fet 是一大段自描述文本（~10KB），包含欧盘/亚盘/大小球/必发等十几个段落。
  compact_fet_to_tags() 把它切成带 slug 的 section dict，
  Factor.tag 声明自己需要哪些 slug，prompt 组装时只取相关段落，
  避免把整坨文本塞进 LLM。

API: http://deepdata.lota.tv/predictions/api/v2
认证: X-API-Key header
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

import requests

# ═══════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════

BASE_URL = "http://deepdata.lota.tv/predictions/api/v2"
CACHE_ROOT = Path(os.environ.get("DSF_DATA_DIR", Path(__file__).parent.parent / "lota_data"))
MATCHES_DIR = CACHE_ROOT / "matches"
FEATURES_DIR = CACHE_ROOT / "features"       # compact-fet 原始缓存
TAGS_DIR = CACHE_ROOT / "tags"               # 提取后的 tagged features

# ═══════════════════════════════════════════════
# API 客户端
# ═══════════════════════════════════════════════

class LotaAPIError(Exception):
    """Lota API 错误，403 表示 key 过期/超限"""
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.is_fatal = status_code == 403


def _get_api_key() -> str:
    """从配置文件读取 Lota API key"""
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
    """GET 请求，失败返回 None"""
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
# 1. 比赛查询 — 网络 + 本地缓存
# ═══════════════════════════════════════════════

def fetch_match_by_id(lota_id: str) -> Optional[dict]:
    data = _get("/matches", {"lota_id": lota_id})
    if not data:
        return None
    result = data.get("data") or {}
    matches = result.get("matches") if isinstance(result, dict) else result
    if isinstance(matches, list) and matches:
        return matches[0]
    return None

def fetch_matches_by_date(date_str: str, lottery_type: str = "jingcai") -> list[dict]:
    params = {"date": date_str}
    if lottery_type and lottery_type != "all":
        params["type"] = lottery_type
    data = _get("/matches", params)
    if not data: return []
    matches = data.get("data") or data.get("matches") or []
    return matches if isinstance(matches, list) else []

def fetch_matches_by_date_range(start: str, end: str, lottery_type: str = "jingcai") -> list[dict]:
    params = {"start_date": start, "end_date": end}
    if lottery_type and lottery_type != "all":
        params["type"] = lottery_type
    data = _get("/matches", params)
    if not data: return []
    result = data.get("data") or {}
    matches = result.get("matches") if isinstance(result, dict) else result
    return matches if isinstance(matches, list) else []

def fetch_compact_fet(lota_id: str) -> Optional[dict]:
    return _get("/compact-fet", {"lota_id": lota_id})


# ── 本地缓存 ──

def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

def _atomic_write_text(path: Path, text: str) -> None:
    """原子写入：先写临时文件再替换，避免并发进程读到半截文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def get_cached_match(lota_id: str) -> Optional[dict]:
    feat = get_cached_compact_fet(lota_id)
    if feat:
        return feat.get("match")
    for d in _recent_dates(30):
        for m in get_cached_matches(d):
            if m.get("lota_id") == lota_id: return m
    return None


def lookup_match(lota_id: str) -> Optional[dict]:
    """宽松版比赛查找：不过滤 lottery_type，兼容嵌套 data.match 结构。

    与 get_cached_match 的区别：
      - 特征文件中同时检查 feat.match 和 feat.data.match
      - 若无结构化 match，从 compact_fet 文本解析队名
      - matches 扫描使用 lottery_type="all"，避免因缺少 lottery_type 字段而漏掉比赛
    """
    feat = get_cached_compact_fet(lota_id)
    if feat:
        match = feat.get("match") or (feat.get("data") or {}).get("match")
        if match:
            return match
        # fallback: 从 compact_fet 文本解析
        fet_text = feat.get("compact_fet", "")
        if fet_text:
            import re
            vs_m = re.search(r'对战[：:]\s*(.+?)\s*🆚\s*(.+?)(?:\n|$)', fet_text)
            if vs_m:
                lg_m = re.search(r'联赛类型[：:]\s*(.+?)(?:\s*[｜|])', fet_text)
                tm_m = re.search(r'时间[：:]\s*([\d\-:\s]+)', fet_text)
                return {
                    "home_name": vs_m.group(1).strip(),
                    "away_name": vs_m.group(2).strip(),
                    "league_name": lg_m.group(1).strip() if lg_m else "",
                    "match_time": (tm_m.group(1).strip()[:19] if tm_m else ""),
                    "score": feat.get("score", ""),
                    "state": feat.get("state", 0),
                }
    for d in _recent_dates(30):
        for m in get_cached_matches(d, lottery_type="all"):
            if m.get("lota_id") == lota_id:
                return m
    return None

def get_cached_matches(date_str: str, lottery_type: str = "jingcai") -> list[dict]:
    path = MATCHES_DIR / f"{date_str}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            matches = data if isinstance(data, list) else data.get("matches", [])
            if lottery_type and lottery_type != "all":
                matches = [m for m in matches if m.get("lottery_type", "") == lottery_type]
            return matches
        except Exception: pass
    return []

def save_matches_cache(date_str: str, matches: list[dict]) -> None:
    _ensure_dir(MATCHES_DIR)
    (MATCHES_DIR / f"{date_str}.json").write_text(
        json.dumps(matches, ensure_ascii=False), encoding="utf-8")

def get_cached_compact_fet(lota_id: str) -> Optional[dict]:
    path = FEATURES_DIR / f"{lota_id}.json"
    if path.exists():
        try: return json.loads(path.read_text())
        except Exception: pass
    js_features = Path(__file__).parent.parent / "lota_data" / "features" / f"{lota_id}.json"
    if js_features.exists():
        try: return json.loads(js_features.read_text())
        except Exception: pass
    return None

def save_compact_fet_cache(lota_id: str, data: dict) -> None:
    _ensure_dir(FEATURES_DIR)
    data["_cached_at"] = datetime.now().isoformat()
    _atomic_write_text(
        FEATURES_DIR / f"{lota_id}.json",
        json.dumps(data, ensure_ascii=False),
    )

def _recent_dates(days: int = 30) -> list[str]:
    return [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


# ═══════════════════════════════════════════════
# 3. compact-fet 段落切分 & tag 操作
# ═══════════════════════════════════════════════

_SECTION_RULES: list[tuple[str, str]] = [
    ("match-head",        r"▋联赛类型"),
    ("match-history",     r"交手统计:"),
    ("rank-info",         r"\[核心积分\]"),
    ("home-recent",       r"主队◆战绩分析"),
    ("away-recent",       r"客队◆战绩分析"),
    ("lineup",            r"🎯阵容数据"),
    ("betfair-eu",        r"必发欧盘积累:"),
    ("fair-odds",         r"公平盘数据:"),
    ("discrete-odds",     r"离散指数"),
    ("betfair-buysell",   r"必发欧盘买卖盘统计"),
    ("eu-odds-pinnacle",  r"欧盘:Pinnacle"),
    ("asian-handicap-crown", r"亚盘:Crown"),
    ("asian-handicap-macau", r"亚盘:澳门"),
    ("asian-handicap-pinnacle", r"亚盘:Pinnacle"),
    ("over-under-crown",  r"大小球:Crown"),
    ("over-under-macau",  r"大小球:澳门"),
    ("goal-bonus",        r"🎯进球数据"),
    ("score-bonus",       r"🎯比分数据"),
]


def compact_fet_to_tags(lota_id: str, data: dict = None) -> dict[str, str]:
    text = _extract_compact_fet_text(data, lota_id)
    if not text: return {}
    hits: list[tuple[str, int]] = []
    for slug, pattern in _SECTION_RULES:
        m = re.search(pattern, text)
        if m: hits.append((slug, m.start()))
    hits.sort(key=lambda x: x[1])
    sections: dict[str, str] = {}
    for i, (slug, start) in enumerate(hits):
        end = hits[i + 1][1] if i + 1 < len(hits) else len(text)
        sections[slug] = text[start:end].strip()
    return sections

def _extract_compact_fet_text(data: dict | None, lota_id: str) -> str:
    if data is None: data = get_cached_compact_fet(lota_id)
    if not data: return ""
    inner = data.get("data") or {}
    return inner.get("compact_fet") or data.get("compact_fet") or ""

def save_tagged_sections(lota_id: str, sections: dict[str, str]) -> None:
    _ensure_dir(TAGS_DIR)
    _atomic_write_text(
        TAGS_DIR / f"{lota_id}.json",
        json.dumps(
            {"lota_id": lota_id, "generated_at": datetime.now().isoformat(), "sections": sections},
            ensure_ascii=False,
        ),
    )

def load_tagged_sections(lota_id: str) -> Optional[dict]:
    path = TAGS_DIR / f"{lota_id}.json"
    if path.exists():
        try: return json.loads(path.read_text())
        except Exception: pass
    return None

def get_sections_by_slugs(lota_id: str, slugs: list[str]) -> str:
    cached = load_tagged_sections(lota_id)
    if not cached: return ""
    sections = cached.get("sections") or {}
    parts = [f"[section:{s}]\n{sections[s]}" for s in slugs if sections.get(s)]
    return "\n\n".join(parts)

def fetch_and_cache_compact_fet(lota_id: str) -> Optional[dict]:
    cached = get_cached_compact_fet(lota_id)
    if cached: return cached
    data = fetch_compact_fet(lota_id)
    if data: save_compact_fet_cache(lota_id, data)
    return data

def ensure_sections(lota_id: str) -> dict[str, str]:
    cached = load_tagged_sections(lota_id)
    if cached: return cached.get("sections", {})
    data = fetch_and_cache_compact_fet(lota_id)
    if data:
        sections = compact_fet_to_tags(lota_id, data)
        save_tagged_sections(lota_id, sections)
        return sections
    return {}


# ═══════════════════════════════════════════════
# 6. Score 计算 & 判定（agent 用）
# ═══════════════════════════════════════════════

# —— 盘口映射（与 builder.py hadicap_map 一致）——
_HANDICAP_MAP = {
    "受平/半": -0.25, "平/半": 0.25, "半球": 0.5, "受半球": -0.5,
    "半/一": 0.75, "受半/一": -0.75, "一球": 1, "受一球": -1,
    "一/球半": 1.25, "一/半": 1.25, "受一/球半": -1.25, "受一/半": -1.25,
    "球半": 1.5, "受球半": -1.5,
    "半/二": 1.75, "受半/二": -1.75, "二球": 2, "受二球": -2,
    "二/球半": 2.25, "二/半": 2.25, "受二/球半": -2.25, "受二/半": -2.25,
    "二球半": 2.5, "受二球半": -2.5,
    "半/三": 2.75, "受半/三": -2.75, "三球": 3, "受三球": -3,
    "三/球半": 3.25, "三/半": 3.25, "受三/球半": -3.25, "受三/半": -3.25,
    "三球半": 3.5, "受三球半": -3.5,
    "半/四": 3.75, "受半/四": -3.75, "四球": 4, "受四球": -4,
    "四/球半": 4.25, "四/半": 4.25, "受四/球半": -4.25, "受四/半": -4.25,
    "四球半": 4.5, "受四球半": -4.5,
    "平手": 0,
}


def _parse_score(score: str) -> tuple[int, int]:
    """解析比分 "2:1" → (2, 1)"""
    s = score.strip().replace("-", ":").replace("：", ":")
    parts = s.split(":")
    h = int(parts[0]) if parts and parts[0].lstrip("-").isdigit() else 0
    a = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0
    return h, a


def score2goal_diff(score: str) -> int:
    """比分 → 净胜球 (home - away)"""
    h, a = _parse_score(score)
    return h - a


def score2goal_sum(score: str) -> int:
    """比分 → 总进球"""
    h, a = _parse_score(score)
    return h + a


def score2_1x2(score: str) -> str:
    """比分 → 胜平负结果: "H"=主胜, "D"=平, "A"=客胜"""
    diff = score2goal_diff(score)
    if diff > 0: return "H"
    if diff < 0: return "A"
    return "D"


# ═══════════════════════════════════════════════
# 7. 赔率提取（Pinnacle 基准，用于 Order 创建 & 结算）
# ═══════════════════════════════════════════════

def _parse_handicap_text(text: str) -> float:
    """盘口文本 → 数值，如 '一球'→1.0, '半/一'→0.75, '2/2.5'→2.25"""
    text = text.strip()
    # 先查映射表
    if text in _HANDICAP_MAP:
        return _HANDICAP_MAP[text]
    # 复合盘口 "2/2.5" → (2+2.5)/2
    if "/" in text:
        parts = text.split("/")
        try:
            return (float(parts[0]) + float(parts[1])) / 2
        except ValueError:
            pass
    # 纯数字
    try:
        return float(text)
    except ValueError:
        return 0.0


def _extract_section_last_line(text: str, section_start_marker: str, end_markers: list[str]) -> str | None:
    """
    从 compact-fet 文本中提取某个 section 的最后一条赔率行。

    每个 section 格式（Δ 行没有 = 号）:
      欧盘:Pinnacle t=Δt±m odds=h/d/a/r(rrr%)
      OPt-xxxm=1.52/4.07/6.14(r93.77%)          ← 开盘
      Δt+262646m↑↓↓↑1.61/3.74/5.65/93.85        ← 后续更新（无=号）
      Δt+46m→↓→→1.45/4.50/9.00/97.75            ← 最后这条是最新赔率

    Returns: 最后一条数据行（以 Δ 或 OP 开头）
    """
    start = text.find(section_start_marker)
    if start < 0:
        return None

    # 确定 section 结束位置
    end = len(text)
    for marker in end_markers:
        p = text.find(marker, start + len(section_start_marker))
        if p > start and p < end:
            end = p

    section = text[start:end]
    lines = [l.strip() for l in section.split("\n") if l.strip()]
    # 从后往前找第一条以 Δ 或 OP 开头的数据行
    for line in reversed(lines):
        if line.startswith("Δ") or line.startswith("OP"):
            return line
    return None


def _split_odds_line(line: str) -> list[str]:
    """
    拆分赔率行中的 / 分隔字段。

    行格式: Δt+46m→↓→→1.45/4.50/9.00/97.75
    odds 部分: 1.45/4.50/9.00/97.75

    Returns: ["1.45", "4.50", "9.00", "97.75"]
    """
    # 找到第一个数字开始的位置（跳过 Δt±m 和箭头）
    m = re.search(r"[\d.]+/", line)
    if not m:
        return []
    odds_part = line[m.start():]
    return odds_part.split("/")


def extract_odds(lota_id: str, data: dict = None) -> dict:
    """
    从 compact-fet 提取 Pinnacle 最新赔率（三种）。

    以 Pinnacle 为基准:
      - 欧盘: Pinnacle 最后一条
      - 亚盘: Pinnacle 最后一条
      - 大小球: Pinnacle 最后一条

    Args:
        lota_id: 比赛 ID
        data: compact-fet 原始数据（None 则从缓存加载）

    Returns:
        {
          "eu": {"h": float, "d": float, "a": float},
          "asian": {"h": float, "handicap": float, "handicap_text": str, "a": float},
          "ou": {"over": float, "threshold": float, "threshold_text": str, "under": float},
        }
        如果某 section 不存在，对应 key 为 None
    """
    if data is None:
        data = get_cached_compact_fet(lota_id)
    if not data:
        return {}

    text = _extract_compact_fet_text(data, lota_id)
    if not text:
        return {}

    result = {}

    # ── 欧盘 Pinnacle ──
    eu_line = _extract_section_last_line(
        text,
        "欧盘:Pinnacle",
        ["亚盘:", "大小球:", "必发欧盘", "公平盘", "离散指数", "阵容数据", "进球数据", "比分数据"],
    )
    if eu_line:
        # 格式: Δt+46m→↓→→1.45/4.50/9.00/97.75  →  h/d/a/r
        parts = _split_odds_line(eu_line)
        if parts and len(parts) >= 4:
            result["eu"] = {
                "h": float(parts[0]),
                "d": float(parts[1]),
                "a": float(parts[2]),
            }

    # ── 亚盘 Pinnacle ──
    as_line = _extract_section_last_line(
        text,
        "亚盘:Pinnacle",
        ["欧盘:", "大小球:", "必发欧盘", "公平盘", "离散指数", "阵容数据", "进球数据", "比分数据"],
    )
    if as_line:
        # 格式: Δt+14m↑→↓↓0.82/一球/1.11/97.72  →  h/handicap/a/r
        # 或:  Δt+14m↓→↑↓0.78/半/一/1.06/95.49  →  h/半/一/a/r
        parts = _split_odds_line(as_line)
        if parts and len(parts) >= 4:
            # 中间部分是 handicap 文本（可能被 / 拆开）
            hc_parts = parts[1:-2]  # [handicap...]
            hc_text = "/".join(hc_parts) if hc_parts else ""
            result["asian"] = {
                "h": float(parts[0]),
                "handicap_text": hc_text,
                "handicap": _parse_handicap_text(hc_text),
                "a": float(parts[-2]),
            }

    # ── 大小球 Pinnacle ──
    ou_line = _extract_section_last_line(
        text,
        "大小球:Pinnacle",
        ["进球数据", "比分数据", "阵容数据"],
    )
    if ou_line:
        # 格式: Δt+22m↑→↓↑1.03/2/2.5/0.88/97.61  →  over/threshold/under/r
        # 或:  Δt+61m↑→↓↓0.99/2.5/0.89/96.94        →  over/threshold/under/r
        parts = _split_odds_line(ou_line)
        if parts and len(parts) >= 4:
            # 中间部分是 threshold 文本
            th_parts = parts[1:-2]
            th_text = "/".join(th_parts) if th_parts else ""
            result["ou"] = {
                "over": float(parts[0]),
                "threshold_text": th_text,
                "threshold": _parse_handicap_text(th_text),
                "under": float(parts[-2]),
            }

    return result
