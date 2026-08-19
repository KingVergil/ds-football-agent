"""
结算桥 —— 供 harness agent 的 ds_settle 工具调用（确定性，"业务确定"边界）。

职责：加载角色未结算订单 → 取比分（缓存优先，per-ID 补缺）→ settle_order（hit/miss/push/pnl）。
不含 LLM，复用原引擎 Role.settle_order + DataManager。

stdin:  {"user": <狗名>, "day": <足球日>}
stdout: {"user","day","unsettled","settled","hit","miss","push","pnl","capital","orders"} 或 {"error"}
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import date as _date, timedelta

from .data_manager import DataManager
from .role import Role


def settle_orders(user: str, day: str) -> dict:
    dm = DataManager()
    try:
        role = Role.load(user)
    except (FileNotFoundError, ValueError):
        return {"error": f"角色 {user} 不存在"}

    unsettled = [o for o in role.get_orders() if not o.get("settled_at")]
    if not unsettled:
        return {
            "user": user, "day": day, "unsettled": 0, "settled": 0,
            "hit": 0, "miss": 0, "push": 0, "pnl": 0.0, "capital": role.capital,
            "orders": [],
        }

    # ── 取比分：本地 matches 缓存（仅 state==6 完场权威），per-ID 补缺 ──
    lids = {o.get("lota_id", "") for o in unsettled if o.get("lota_id")}
    scores: dict[str, str] = {}
    d = _date.fromisoformat(day) if day else None
    dates = [day] if day else []
    if d:
        dates = [day, (d + timedelta(days=1)).isoformat(), (d - timedelta(days=1)).isoformat()]

    for dt in dates:
        for m in dm.get_cached_matches(dt, lottery_type="all"):
            lid = m.get("lota_id", "")
            sc = m.get("score", "")
            if lid in lids and sc and m.get("state") == 6:
                scores[lid] = sc

    for lid in list(lids - set(scores)):
        try:
            refreshed = dm.refresh_score_match(lid)
            if refreshed:
                sc = refreshed.get("score", "")
                if sc and refreshed.get("state") == 6:
                    scores[lid] = sc
        except Exception:
            pass

    # ── 结算 ──
    hit = miss = push = 0
    pnl = 0.0
    settled_list: list[dict] = []
    for o in unsettled:
        lid = o.get("lota_id", "")
        sc = scores.get(lid)
        if not sc:
            continue
        try:
            settled = role.settle_order(o, sc)
        except Exception:
            continue
        h = settled.get("hit")
        profit = settled.get("profit", 0)
        if h is True or (h is None and profit > 0):
            hit += 1
        elif h is False or (h is None and profit < 0):
            miss += 1
        else:
            push += 1
        pnl += profit
        settled_list.append({
            "lota_id": lid,
            "bet_type": settled.get("bet_type"),
            "pick": settled.get("pick"),
            "odds": o.get("odds", 0),
            "handicap": o.get("handicap"),
            "bet_size": o.get("bet_size", 0),
            "score": sc,
            "hit": h,
            "profit": profit,
            "reason": o.get("reason", ""),
        })

    role.save()
    return {
        "user": user, "day": day,
        "unsettled": len(unsettled),
        "settled": hit + miss + push,
        "hit": hit, "miss": miss, "push": push,
        "pnl": round(pnl, 2),
        "capital": role.capital,
        "orders": settled_list,
    }


if __name__ == "__main__":
    import io as _io

    result: dict
    try:
        data = json.loads(sys.stdin.read() or "{}")
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = settle_orders(data.get("user", "default"), data.get("day", ""))
        sys.stderr.write(buf.getvalue())
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False))
