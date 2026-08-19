"""
DSFootball Python CLI — 订单日志工具

把 Order + Prediction + Match 转换成 LLM 可读的投注日志片段，
供 agent 分析历史投注表现。
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

from .tools import score2_1x2

# ═══════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════

ORDERS_DIR = Path(__file__).parent.parent / "data" / "orders"
PREDICTS_DIR = Path(__file__).parent.parent / "data" / "predicts"
FEATURES_DIR = Path(__file__).parent.parent / "data" / "features"


# ═══════════════════════════════════════════════
# 1. 加载 & 关联数据
# ═══════════════════════════════════════════════

def _load_orders(lota_id: str = None) -> list[dict]:
    orders = []
    files = [ORDERS_DIR / f"{lota_id}.json"] if lota_id else sorted(ORDERS_DIR.glob("*.json"))
    for fpath in files:
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(data, list):
                orders.extend(data)
        except Exception:
            pass
    return orders


def _load_prediction(pred_id: str) -> dict | None:
    """按 predict_id 查找预测"""
    for fpath in sorted(PREDICTS_DIR.glob("*.json")):
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for p in data:
                    if p.get("id") == pred_id:
                        return p
        except Exception:
            pass
    return None


def _load_match_info(lota_id: str) -> dict:
    """从 features 缓存提取比赛基础信息"""
    fpath = FEATURES_DIR / f"{lota_id}.json"
    if not fpath.exists():
        return {}
    try:
        d = json.loads(fpath.read_text(encoding="utf-8"))
        data = d.get("data") or {}
        fet = data.get("compact_fet", "")
        score = data.get("score", "")

        # 从 compact_fet 第一行提取队名/联赛/时间
        home = away = league = match_time = ""
        for line in fet.split("\n"):
            if "🆚" in line:
                # "⚔️对战: 墨西哥 🆚 南非"
                parts = line.split("🆚")
                if len(parts) == 2:
                    home = parts[0].split(":")[-1].strip() if ":" in parts[0] else parts[0].strip()
                    away = parts[1].strip()
                break

        for line in fet.split("\n")[:3]:
            if "联赛类型:" in line:
                league = line.split(":")[1].split("｜")[0].strip() if ":" in line else ""
            if "时间:" in line:
                m = line
                if ":" in m:
                    time_part = m.split(":", 1)[1].strip() if ":" in m else ""
                    match_time = time_part.split("｜")[0].strip() if "｜" in time_part else time_part

        return {
            "home": home,
            "away": away,
            "league": league,
            "match_time": match_time,
            "score": score,
        }
    except Exception:
        return {}


# ═══════════════════════════════════════════════
# 2. 格式化
# ═══════════════════════════════════════════════

def _fmt_pick(bet_type: str, pick: str, handicap: float | None) -> str:
    """投注选择 → 人类可读"""
    if bet_type in ("胜平负", "让球胜平负"):
        m = {"H": "主胜", "D": "平局", "A": "客胜"}
        label = m.get(pick, pick)
        if bet_type == "让球胜平负" and handicap is not None and handicap != 0:
            hc_str = f"{handicap:+.0f}" if float(handicap).is_integer() else f"{handicap:+.2f}".rstrip('0').rstrip('.')
            return f"{label}({hc_str})"
        return label
    elif bet_type == "亚盘":
        side = "主队" if pick == "H" else "客队"
        if handicap is not None and handicap != 0:
            hc_str = f"{handicap:+.2f}".rstrip('0').rstrip('.')
            return f"{side}{hc_str}"
        return f"{side}平手"
    elif bet_type == "大小球":
        d = "大" if pick == "over" else "小"
        if handicap is not None:
            hc_str = f"{handicap:.2f}".rstrip('0').rstrip('.')
            return f"{d}{hc_str}"
        return d
    return pick


def _fmt_emoji(hit: bool | None) -> str:
    if hit is True: return "✅"
    if hit is False: return "❌"
    return "➖"


# ═══════════════════════════════════════════════
# 3. 主函数
# ═══════════════════════════════════════════════

def order_log(lota_id: str = None, bet_type: str = None,
              limit: int = 0, compact: bool = True) -> str:
    """
    生成投注日志，可直接输入 LLM。

    Args:
        lota_id:   单场比赛，None=全部
        bet_type:  过滤类型（胜平负/亚盘/大小球），None=全部
        limit:     最多返回条数，0=全部
        compact:   True=紧凑单行格式, False=多行详细格式

    Returns:
        投注日志文本
    """
    orders = _load_orders(lota_id)
    if bet_type:
        orders = [o for o in orders if o.get("bet_type") == bet_type]

    if not orders:
        return "(无订单)"

    # 按比赛时间排序（通过 lota_id 分组取时间）
    match_cache = {}
    for o in orders:
        lid = o.get("lota_id", "")
        if lid not in match_cache:
            match_cache[lid] = _load_match_info(lid)

    # 按时间排序
    def _sort_key(o):
        m = match_cache.get(o.get("lota_id", ""), {})
        return m.get("match_time", "9999") + o.get("created_at", "")

    orders.sort(key=_sort_key)

    if limit > 0:
        orders = orders[:limit]

    lines = []
    if compact:
        lines.append(_header_compact())
        for o in orders:
            lines.append(_row_compact(o, match_cache.get(o.get("lota_id", ""), {})))
    else:
        lines.append(_header_detailed())
        for o in orders:
            lines.append(_row_detailed(o, match_cache.get(o.get("lota_id", ""), {})))

    # 汇总
    total_bet = sum(o.get("bet_size", 0) for o in orders)
    total_return = sum(o.get("return_amount", 0) for o in orders)
    total_profit = total_return - total_bet
    hits = sum(1 for o in orders if o.get("hit") is True)
    misses = sum(1 for o in orders if o.get("hit") is False)
    pushes = sum(1 for o in orders if o.get("hit") is None)
    settled = hits + misses + pushes

    roi = f"{total_profit/total_bet*100:+.1f}%" if total_bet > 0 else "-"

    lines.append(f"\n📊 汇总: {len(orders)}单 | "
                 f"✅{hits} ❌{misses} ➖{pushes} | "
                 f"命中率 {hits/(settled-pushes)*100:.1f}%" if settled > pushes else f"命中率 -" + f" | "
                 f"投注 {total_bet:.0f} → 返还 {total_return:.0f} | "
                 f"盈亏 {total_profit:+.0f} | ROI {roi}")

    return "\n".join(lines)


def _header_compact() -> str:
    return (
        f"{'时间':<12} {'比赛':<20} {'类型':<6} {'买':<12} "
        f"{'赔率':>5} {'金额':>5} {'比分':>5} {'结果':>4} {'收益':>8}\n"
        + "-" * 90
    )


def _row_compact(order: dict, match: dict) -> str:
    t = match.get("match_time", "")[5:16] or "?"  # "06-12 03:00"
    teams = f"{match.get('home','?')[:8]}vs{match.get('away','?')[:8]}"
    bt = order.get("bet_type", "")
    hc = order.get("goal_line") if order.get("goal_line") is not None else order.get("handicap")
    pick_text = _fmt_pick(bt, order.get("pick", ""), hc)
    odds = f"{order.get('odds', 0):.2f}"
    bet = f"{order.get('bet_size', 0):.0f}"
    score = match.get("score", "?")
    hit = order.get("hit")
    emoji = _fmt_emoji(hit)
    profit = f"{order.get('profit', 0):+.0f}"

    return f"{t:<12} {teams:<20} {bt:<6} {pick_text:<12} {odds:>5} {bet:>5} {score:>5} {emoji:>4} {profit:>8}"


def _header_detailed() -> str:
    return "═════ 投注日志 ═════\n"


def _row_detailed(order: dict, match: dict) -> str:
    bt = order.get("bet_type", "")
    hc = order.get("goal_line") if order.get("goal_line") is not None else order.get("handicap")
    pick_text = _fmt_pick(bt, order.get("pick", ""), hc)
    emoji = _fmt_emoji(order.get("hit"))
    profit = order.get("profit", 0)

    lines = [
        f"🏟 {match.get('league','?')} | {match.get('match_time','?')}",
        f"⚔ {match.get('home','?')} vs {match.get('away','?')}",
        f"🎯 {bt} → 买 {pick_text} @{order.get('odds',0):.2f}  投注 {order.get('bet_size',0):.0f}",
        f"📊 最终比分 {match.get('score','?')}  {emoji} 收益 {profit:+.0f}",
        "",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════
# 4. 按类型分组统计（给 agent 的摘要）
# ═══════════════════════════════════════════════

def order_summary() -> str:
    """按 bet_type 分组统计，生成快速摘要"""
    orders = _load_orders()
    if not orders:
        return "(无订单)"

    by_type = defaultdict(lambda: {"total": 0, "hit": 0, "miss": 0, "push": 0,
                                    "bet": 0.0, "return": 0.0})
    for o in orders:
        bt = o.get("bet_type", "其他")
        by_type[bt]["total"] += 1
        if o.get("hit") is True:    by_type[bt]["hit"] += 1
        elif o.get("hit") is False: by_type[bt]["miss"] += 1
        else:                        by_type[bt]["push"] += 1
        by_type[bt]["bet"] += o.get("bet_size", 0)
        by_type[bt]["return"] += o.get("return_amount", 0)

    lines = ["📊 订单摘要\n"]
    for bt in ["胜平负", "亚盘", "大小球"]:
        s = by_type.get(bt)
        if not s or s["total"] == 0:
            continue
        settled = s["total"] - s["push"]
        rate = f"{s['hit']/settled*100:.1f}%" if settled > 0 else "-"
        profit = s["return"] - s["bet"]
        roi = f"{profit/s['bet']*100:+.1f}%" if s["bet"] > 0 else "-"
        lines.append(
            f"  {bt:<6} {s['total']:>3}单  "
            f"✅{s['hit']} ❌{s['miss']} ➖{s['push']}  "
            f"命中率 {rate:<6}  "
            f"投{s['bet']:.0f}→返{s['return']:.0f}  盈亏{profit:+.0f}  ROI {roi}"
        )

    total = by_type["胜平负"]["total"] + by_type["亚盘"]["total"] + by_type["大小球"]["total"]
    total_bet = sum(s["bet"] for s in by_type.values())
    total_return = sum(s["return"] for s in by_type.values())
    total_profit = total_return - total_bet
    total_roi = f"{total_profit/total_bet*100:+.1f}%" if total_bet > 0 else "-"
    lines.append(f"\n  合计 {total}单 | 总盈亏 {total_profit:+.0f} | ROI {total_roi}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Order log generator")
    p.add_argument("--lota-id", type=str, default=None, help="单场比赛")
    p.add_argument("--type", type=str, default=None, help="胜平负/亚盘/大小球")
    p.add_argument("--limit", type=int, default=0, help="最多条数")
    p.add_argument("--detailed", action="store_true", help="多行详细格式")
    p.add_argument("--summary", action="store_true", help="只显示摘要")
    args = p.parse_args()

    if args.summary:
        print(order_summary())
    else:
        print(order_log(
            lota_id=args.lota_id,
            bet_type=args.type,
            limit=args.limit,
            compact=not args.detailed,
        ))
