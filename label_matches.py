#!/usr/bin/env python3
"""
标注历史比赛所有盘口结果 — train mode 数据注入脚本。

用法:
  python label_matches.py 2026-06-11 2026-06-30    # 标注并保存
  python label_matches.py 2026-06-11 2026-06-30 --inject  # 注入到 agent prompt

生成格式 (per lota_id):
  {
    "lota_id": "Lota4459820",
    "home": "墨西哥", "away": "南非",
    "score": "2:0",
    "odds": {"eu": {...}, "asian": {...}, "ou": {...}},
    "outcomes": {
      "1x2": {"H": true, "D": false, "A": false},
      "asian": {
        "-2.00": {"H": false, "A": true},
        "-1.50": {"H": false, "A": true},
        "-1.00": {"H": true, "A": false},   ← 2:0, H-1.0=赢
        "-0.75": {"H": true, "A": false},
        ...
        "+0.00": {"H": true, "A": false},
        "+0.50": {"H": true, "A": false},
        ...
      },
      "ou": {
        "2.00": {"over": false, "under": true},   ← total=2
        "2.25": {"over": false, "under": true},   ← 赢半
        "2.50": {"over": false, "under": true},
        ...
      }
    }
  }
"""

import json
import sys
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from src.data_manager import DataManager
from src.store import settle_order

LOTA_DATA = Path(__file__).parent / "lota_data"
LABELS_DIR = LOTA_DATA / "labels"
LABELS_DIR.mkdir(exist_ok=True)

# 常见盘口线
ASIAN_LINES = [
    -4.0, -3.5, -3.0, -2.75, -2.5, -2.25, -2.0, -1.75, -1.5, -1.25,
    -1.0, -0.75, -0.5, -0.25, 0.0,
    +0.25, +0.5, +0.75, +1.0, +1.25, +1.5, +1.75, +2.0, +2.25, +2.5, +2.75, +3.0, +3.5, +4.0,
]
OU_LINES = [
    1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.5, 5.0,
]


def label_match(lota_id: str, score: str, odds: dict) -> dict:
    """对一场比赛标注所有盘口结果 — 返回 ¥1 利润率"""
    outcomes = {}

    # ── 1X2 ──
    eu = odds.get("eu", {})
    hg, ag = map(int, score.split(":"))
    outcomes["1x2"] = {
        "H": _profit_rate(True, False, eu.get("h", 0)),   # win full
        "D": _profit_rate(hg == ag, False, eu.get("d", 0)),
        "A": _profit_rate(hg < ag, False, eu.get("a", 0)),
    }

    # ── 亚盘 ──
    asian = odds.get("asian", {})
    asian_outcomes = {}
    for hc in ASIAN_LINES:
        r_h = settle_order({
            "bet_type": "亚盘", "pick": "H", "handicap": hc,
            "odds": asian.get("h", 1.0), "bet_size": 1.0,
        }, score)
        r_a = settle_order({
            "bet_type": "亚盘", "pick": "A", "handicap": hc,
            "odds": asian.get("a", 1.0), "bet_size": 1.0,
        }, score)
        asian_outcomes[f"{hc:+.2f}"] = {
            "H": round(r_h.get("profit", 0), 4),
            "A": round(r_a.get("profit", 0), 4),
        }
    outcomes["asian"] = asian_outcomes

    # ── 大小球 ──
    ou = odds.get("ou", {})
    ou_outcomes = {}
    for th in OU_LINES:
        r_o = settle_order({
            "bet_type": "大小球", "pick": "over", "handicap": th,
            "odds": ou.get("over", 1.0), "bet_size": 1.0,
        }, score)
        r_u = settle_order({
            "bet_type": "大小球", "pick": "under", "handicap": th,
            "odds": ou.get("under", 1.0), "bet_size": 1.0,
        }, score)
        ou_outcomes[f"{th:.2f}"] = {
            "over": round(r_o.get("profit", 0), 4),
            "under": round(r_u.get("profit", 0), 4),
        }
    outcomes["ou"] = ou_outcomes

    return outcomes


def _profit_rate(win: bool, is_half: bool, odds: float) -> float:
    """计算 ¥1 利润率: 赢全=+odds, 赢半=+odds/2, 走水=0, 输半=-0.5, 输全=-1"""
    if win is True:
        return odds
    elif win is False:
        return -1.0
    else:  # None = mixed (push or half)
        return 0.0  # push/half handled by settle_order directly


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("start", help="开始日期 YYYY-MM-DD")
    p.add_argument("end", nargs="?", help="结束日期")
    p.add_argument("--compact", action="store_true", help="压缩输出(仅胜负)")
    args = p.parse_args()

    dm = DataManager()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start

    all_labels = {}

    d = start
    while d <= end:
        day_str = d.isoformat()
        print(f"📅 {day_str}...", end=" ", flush=True)

        cache_path = LOTA_DATA / "matches" / f"{day_str}.json"
        if not cache_path.exists():
            print("无缓存")
            d += timedelta(days=1)
            continue

        try:
            matches = json.loads(cache_path.read_text())
        except Exception:
            print("读取失败")
            d += timedelta(days=1)
            continue

        day_count = 0
        for m in (matches if isinstance(matches, list) else []):
            lid = m.get("lota_id", "")
            # 从 compact-fet 取比分
            from src.tools import get_cached_compact_fet
            fet = get_cached_compact_fet(lid)
            sc = (fet or {}).get("score", "")
            if not sc or ":" not in str(sc):
                continue

            ctx = dm.get_match_context(lid)
            odds = ctx.get("odds", {})
            if not odds.get("asian"):
                continue

            outcomes = label_match(lid, sc, odds)
            all_labels[lid] = {
                "lota_id": lid,
                "home": m.get("home", "?"),
                "away": m.get("away", "?"),
                "league": m.get("league", ""),
                "match_time": m.get("match_time", ""),
                "score": sc,
                "odds": odds,
                "outcomes": outcomes,
            }
            day_count += 1

        print(f"{day_count} 场")
        d += timedelta(days=1)

    # ── 保存 ──
    out_path = LABELS_DIR / f"labels_{args.start}_{end.isoformat()}.json"
    out_path.write_text(json.dumps(all_labels, ensure_ascii=False, indent=2))
    print(f"\n✅ 保存: {out_path} ({len(all_labels)} 场)")

    # ── 摘要 ──
    total = len(all_labels)
    h_wins = sum(1 for l in all_labels.values() if l["outcomes"]["1x2"]["H"])
    print(f"  主胜: {h_wins}/{total} ({h_wins/max(total,1)*100:.0f}%)")


if __name__ == "__main__":
    main()
