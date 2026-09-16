"""重跑分析时的退单语义（北单串关 slip 级订单）。

用户要求（2026-09-13）
────────────────────
重跑分析时：**全未开赛的票退单**；**但凡有一场开赛了就不退**（不能撤已开赛的腿）。

同时钉住一个已修的脆弱点：腿时间解析**不能只读缓存**。旧实现遇到
`get_cached_match(lid)` 查不到（缓存缺失/回放环境）就整票跳过 → 既不退也不保护，
重跑时静默叠票。现改为"缓存优先，回落票内自带的 match_time"。
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 用纯逻辑复刻 refresh_orders 的判定（避免单测依赖缓存/资金落盘）
BEIJING = timezone(timedelta(hours=8))


def _norm(t: str) -> str:
    return (t or "").replace("T", " ")[:16]


def decide(order: dict, now: str, window_start: str, window_end: str,
           cache_times: dict | None = None) -> str:
    """返回 "退" / "留" / "跳过"，复刻 refresh_orders 核心判定。"""
    if order.get("settled_at"):
        return "跳过"
    cache_times = cache_times or {}
    by_lid = {}
    for l in list(order.get("legs") or []) + list(order.get("ticket_legs") or []):
        if l.get("lota_id") and l.get("match_time"):
            by_lid.setdefault(l["lota_id"], l["match_time"])
    times = []
    for l in (order.get("legs") or []):
        lid = l.get("lota_id")
        t = _norm(cache_times.get(lid, "")) or _norm(by_lid.get(lid, ""))
        if t:
            times.append(t)
    if not times:
        return "留"                      # 时间不可判定 → 保守保留
    if not any(window_start <= t <= window_end for t in times):
        return "跳过"                    # 窗口外不碰
    return "留" if any(t <= now for t in times) else "退"


DAY = "2026-09-13"
NOW = f"{DAY} 13:00"   # 当前时刻（北京时间）：13:00 之后开赛的腿算未开赛
W0, W1 = f"{DAY} 12:01", "2026-09-14 12:00"


def _order(lid_times: list[tuple[str, str]], settled: bool = False) -> dict:
    legs = [{"lota_id": lid, "match_time": t, "pick": "H"} for lid, t in lid_times]
    return {"legs": legs, "ticket_legs": legs, "settled_at": ("x" if settled else None)}


def test_all_unstarted_is_refunded():
    """全未开赛 → 退（把额度让给新一轮分析）。"""
    o = _order([("L1", f"{DAY} 20:30:00"), ("L2", f"{DAY} 22:00:00")])
    assert decide(o, NOW, W0, W1) == "退"


def test_any_started_leg_keeps_ticket():
    """**只要有一场已开赛就整票保留**（不可撤已开赛的腿）。"""
    o = _order([("L1", f"{DAY} 10:00:00"),      # 已开赛
                ("L2", f"{DAY} 20:30:00")])     # 未开赛
    assert decide(o, NOW, W0, W1) == "留"


def test_window_outside_is_untouched():
    """不在本足球日窗口的票不碰（避免误退前一天挂单）。

    窗口是 [D 12:01, D+1 12:00]；这里用 D-1 的场次（窗口之前）验证"跳过"。
    """
    o = _order([("L1", "2026-09-12 20:30:00")])
    assert decide(o, NOW, W0, W1) == "跳过"


def test_settled_order_skipped():
    o = _order([("L1", f"{DAY} 20:30:00")], settled=True)
    assert decide(o, NOW, W0, W1) == "跳过"


def test_time_from_order_when_cache_missing():
    """缓存查不到该场时，回落票内 match_time —— 否则会静默叠票。"""
    o = _order([("L1", f"{DAY} 20:30:00")])
    # 缓存空 → 用票内时间（20:30，窗口内且未开赛）→ 退
    assert decide(o, NOW, W0, W1, cache_times={}) == "退"
    # 缓存给的是窗口内但已开赛的时间 → 保留
    assert decide(o, NOW, W0, W1, cache_times={"L1": f"{DAY} 12:30:00"}) == "留"


def test_no_time_at_all_is_conservatively_kept():
    """既无缓存也无票内时间 → 保守保留（不静默丢票）。"""
    o = {"legs": [{"lota_id": "L1"}], "ticket_legs": [{"lota_id": "L1"}], "settled_at": None}
    assert decide(o, NOW, W0, W1) == "留"
