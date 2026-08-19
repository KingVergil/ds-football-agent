"""
DSFootball Python CLI — 回测脚本

从 features 缓存读实际比分，逐条判定预测，输出完整过程。
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from .tools import score2goal_diff, score2goal_sum, score2_1x2
from .models import PredictType

# ═══════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════

PREDICTS_DIR = Path(__file__).parent.parent / "data" / "predicts"
FEATURES_DIR = Path(__file__).parent.parent / "data" / "features"


# ═══════════════════════════════════════════════
# 1. 加载实际比分（从 features 缓存）
# ═══════════════════════════════════════════════

def load_scores(features_dir: Path = None) -> dict[str, dict]:
    """
    从 data/features/{lota_id}.json 加载比分+比赛信息。

    数据路径: data.score（如 "2:0"），data.compact_fet 第一行有队名。
    """
    if features_dir is None:
        features_dir = FEATURES_DIR

    scores = {}
    for fpath in sorted(features_dir.glob("*.json")):
        try:
            d = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        data = d.get("data") or {}
        score = data.get("score", "")
        if not score:
            continue
        # 从 compact_fet 第一行提取队名
        fet = data.get("compact_fet", "")
        home = away = ""
        m = None
        for line in fet.split("\n"):
            m = line
            if "🆚" in line:
                break
        if m:
            parts = m.split("🆚")
            if len(parts) == 2:
                home = parts[0].split(":")[-1].strip() if ":" in parts[0] else parts[0].strip()
                away = parts[1].strip()

        lid = fpath.stem
        scores[lid] = {"score": score, "home": home, "away": away}

    return scores


# ═══════════════════════════════════════════════
# 2. 加载预测
# ═══════════════════════════════════════════════

def load_predictions_by_lota(predicts_dir: Path = None) -> dict[str, list[dict]]:
    """按 lota_id 分组加载预测"""
    if predicts_dir is None:
        predicts_dir = PREDICTS_DIR

    grouped = defaultdict(list)
    for fpath in sorted(predicts_dir.glob("*.json")):
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(data, list):
                grouped[fpath.stem] = data
        except Exception as e:
            print(f"[backtest] 读取失败 {fpath.name}: {e}")

    return dict(grouped)


# ═══════════════════════════════════════════════
# 3. 判定逻辑（纯函数）
# ═══════════════════════════════════════════════

def evaluate_prediction(pred: dict, score: str) -> tuple[bool | None, str]:
    """返回 (hit, detail_str)。quarter-ball 盘口拆两半结算。"""
    ptype = pred.get("type", "")
    value = pred.get("value", {})

    try:
        hg, ag = map(int, score.split(":"))
    except Exception:
        return None, f"比分格式错误: {score}"

    diff = hg - ag
    total = hg + ag

    if ptype == "胜平负":
        want = value.get("result", "")
        actual = score2_1x2(score)
        if want in ("H", "D", "A"):
            return (actual == want, f"预测{want} vs 实际{actual}")

    elif ptype == "亚盘":
        try:
            hc = float(value.get("handicap", 0))
        except (ValueError, TypeError):
            return None, f"无效盘口: {value.get('handicap')}"
        d = (value.get("direction", "")).strip().upper()

        return _eval_quarter(bet_type="亚盘", pick=d, handicap=hc, diff=diff, total=total)

    elif ptype == "大小球":
        try:
            th = float(value.get("threshold", 0))
        except (ValueError, TypeError):
            return None, f"无效盘口: {value.get('threshold')}"
        d = (value.get("direction", "")).strip().lower()
        if d in ("大", "大球"): d = "over"
        if d in ("小", "小球"): d = "under"

        return _eval_quarter(bet_type="大小球", pick=d, handicap=th, diff=diff, total=total)

    elif ptype == "比分":
        scores = value.get("scores") or []
        if not scores:
            h = int(value.get("home", -1))
            a = int(value.get("away", -1))
            if h >= 0 and a >= 0:
                scores = [{"home": h, "away": a}]
            else:
                return None, "无候选比分"
        hit = any(s.get("home") == hg and s.get("away") == ag for s in scores)
        cand = "/".join(f"{s.get('home','')}:{s.get('away','')}" for s in scores)
        return (hit, f"候选[{cand}] ← 实际{score}")

    elif ptype == "进球数":
        try:
            g = int(value.get("goals", -1))
        except (ValueError, TypeError):
            return None, f"无效进球数: {value.get('goals')}"
        if g >= 0:
            return (total == g, f"预测{g}球 vs 实际{total}球")

    return None, f"未知类型: {ptype}"


def _eval_quarter(bet_type: str, pick: str, handicap: float,
                  diff: int, total: int) -> tuple[bool | None, str]:
    """
    亚盘/大小球 quarter-ball 判定。

    .25 / .75 盘口拆成 hc±0.25 两半，各自结算后合并。
    """
    is_quarter = abs(handicap % 0.5) > 0.001

    if not is_quarter:
        # 整数/半球
        if bet_type == "亚盘":
            adj = diff - handicap
            if adj == 0:
                return None, f"走水 diff={diff} hc={handicap}"
            win = (adj > 0) if pick == "H" else (adj < 0)
            return (win, f"diff={diff} hc={handicap} adj={adj:+.1f}")
        else:  # 大小球
            if total == handicap:
                return None, f"走水 total={total}=={handicap}"
            win = (total > handicap) if pick == "over" else (total < handicap)
            return (win, f"total={total} vs {handicap}")

    # quarter-ball: 拆 hc-0.25 和 hc+0.25
    hc1 = handicap - 0.25
    hc2 = handicap + 0.25

    def _half(hc: float) -> str:
        if bet_type == "亚盘":
            adj = diff - hc
            if adj == 0: return "push"
            if pick == "H":
                return "win" if adj > 0 else "lose"
            else:
                return "win" if adj < 0 else "lose"
        else:
            if total == hc: return "push"
            if pick == "over":
                return "win" if total > hc else "lose"
            else:
                return "win" if total < hc else "lose"

    r1, r2 = _half(hc1), _half(hc2)
    wins = (r1 == "win") + (r2 == "win")
    losses = (r1 == "lose") + (r2 == "lose")

    detail = f"{handicap:+.2f}→[{hc1:+.2f}] {r1} | [{hc2:+.2f}] {r2}"

    if wins == 2: return True, f"全赢 {detail}"
    if losses == 2: return False, f"全输 {detail}"
    if wins == 1 and losses == 0: return None, f"赢半 {detail}"
    if losses == 1 and wins == 0: return None, f"输半 {detail}"
    return None, f"双走水 {detail}"


# ═══════════════════════════════════════════════
# 4. 格式化输出
# ═══════════════════════════════════════════════

def fmt_value(pred: dict) -> str:
    """把预测 value 转成可读字符串"""
    v = pred.get("value", {})
    t = pred.get("type", "")
    if t == "胜平负":
        m = {"H": "主胜", "D": "平", "A": "客胜"}
        return m.get(v.get("result", ""), str(v))
    if t == "亚盘":
        hc = v.get("handicap", 0)
        d = "主" if v.get("direction") == "H" else "客"
        return f"{'+' if hc>=0 else ''}{hc} {d}"
    if t == "大小球":
        d = "大" if v.get("direction") == "over" else "小"
        return f"{d}{v.get('threshold','')}"
    if t == "比分":
        scores = v.get("scores") or []
        return "/".join(f"{s.get('home','')}:{s.get('away','')}" for s in scores)
    if t == "进球数":
        return f"{v.get('goals','')}球"
    return str(v)[:30]


def _icon(hit: bool | None) -> str:
    if hit is True: return "🔴"
    if hit is False: return "⚫"
    return "⬜"


# ═══════════════════════════════════════════════
# 5. 主流程
# ═══════════════════════════════════════════════

def run_backtest(predicts_dir: Path = None, features_dir: Path = None, dry_run: bool = False):
    scores = load_scores(features_dir)
    grouped = load_predictions_by_lota(predicts_dir)

    if not scores:
        print("[backtest] 没有找到比分数据")
        return None
    if not grouped:
        print("[backtest] 没有找到预测数据")
        return None

    # 只处理有比分的比赛
    common = sorted(set(grouped.keys()) & set(scores.keys()))
    print(f"有比分的比赛: {len(scores)}, 有预测的比赛: {len(grouped)}, 交集: {len(common)}\n")

    stats = defaultdict(lambda: {"total": 0, "hit": 0, "miss": 0, "push": 0})
    all_results = []

    for lid in common:
        info = scores[lid]
        sc = info["score"]
        preds = grouped[lid]

        hg, ag = (int(x) for x in sc.split(":"))
        diff = hg - ag
        total = hg + ag
        home = info.get("home", "?")
        away = info.get("away", "?")

        print(f"━━━ {home} vs {away} ━━━")
        print(f"    比分 {sc}  |  diff={diff:+d}  total={total}  |  {len(preds)}条预测")
        print()

        for p in preds:
            ptype = p.get("type", "?")
            hit, detail = evaluate_prediction(p, sc)
            icon = _icon(hit)

            print(f"  {icon} {ptype:<6} {fmt_value(p):<20} │ {detail}")

            stats[ptype]["total"] += 1
            stats["__all__"]["total"] += 1
            if hit is True:
                stats[ptype]["hit"] += 1; stats["__all__"]["hit"] += 1
            elif hit is False:
                stats[ptype]["miss"] += 1; stats["__all__"]["miss"] += 1
            else:
                stats[ptype]["push"] += 1; stats["__all__"]["push"] += 1

            all_results.append({"pred": p, "score": sc, "hit": hit, "detail": detail})

        print()

    # ── 汇总 ──
    type_names = ["胜平负", "亚盘", "大小球", "比分", "进球数"]
    print("═" * 60)
    print(f"{'类型':<8} {'总数':>4}  {'🔴命中':>4}  {'⚫未中':>4}  {'⬜走水':>4}  {'命中率':>8}")
    print("─" * 60)

    for t in type_names:
        s = stats.get(t)
        if not s or s["total"] == 0:
            continue
        denom = s["total"] - s["push"]
        rate = f"{s['hit']/denom*100:.1f}%" if denom > 0 else "-"
        print(f"{t:<8} {s['total']:>4}  {s['hit']:>4}  {s['miss']:>4}  {s['push']:>4}  {rate:>8}")

    s = stats["__all__"]
    denom = s["total"] - s["push"]
    rate = f"{s['hit']/denom*100:.1f}%" if denom > 0 else "-"
    print("─" * 60)
    print(f"{'合计':<8} {s['total']:>4}  {s['hit']:>4}  {s['miss']:>4}  {s['push']:>4}  {rate:>8}")
    print("═" * 60)

    # ── 写盘 ──
    if dry_run:
        print("\n[dry_run] 未写盘")
    else:
        written = _write_results(predicts_dir or PREDICTS_DIR, all_results)
        print(f"\n已更新 {written} 条预测")

    return {"stats": dict(stats), "results": all_results}


def _write_results(predicts_dir: Path, results: list) -> int:
    by_lota = defaultdict(list)
    for r in results:
        by_lota[r["pred"]["lota_id"]].append(r)

    written = 0
    for lid, items in by_lota.items():
        fpath = predicts_dir / f"{lid}.json"
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue

        lut = {item["pred"].get("id", ""): item for item in items}
        for p in data:
            r = lut.get(p.get("id", ""))
            if r:
                p["hit"] = r["hit"]
                p["result"] = r["score"]
                p["checked_at"] = datetime.now().isoformat()
                written += 1

        fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return written


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    run_backtest(dry_run=dry_run)
