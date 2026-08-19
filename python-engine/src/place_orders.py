"""
效果 A 的确定性下单桥 —— 供 harness agent 的 ds_place_orders 工具调用。

职责（"业务确定"边界，等价于数据边界，复用原引擎逻辑）：
  agent 输出的 ```order``` 文本 → parse_orders（确定性）→ place_orders（确定性）

stdin:  {"user": <狗名>, "day": <足球日>, "orders_text": "```order ...```"}
stdout: {"user","day","parsed","placed","skipped","capital","orders"} 或 {"error"}

运行:  echo '<json>' | python3 -m src.place_orders
"""

from __future__ import annotations

import json
import sys
from datetime import date as _date, datetime, timedelta, timezone

from .data_manager import DataManager
from .environment import get_football_day
from .fund_limits import FundManager, order_limits_for
from .order_utils import parse_orders
from .role import Role

_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_bj() -> str:
    """当前北京时间(UTC+8)字符串，与 match_time 同时区。"""
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def place_orders_from_response(user: str, day: str, response_text: str) -> dict:
    """把 agent 的判断结果落成真实订单（确定性，无 LLM）。"""
    dm = DataManager()
    try:
        role = Role.load(user)
    except (FileNotFoundError, ValueError):
        role = Role(name=user, capital=10000)
        role.save()

    # 当天比赛（供 parse_orders 校验 lota_id + 补赔率）；缓存优先，缺失再走 API
    safe = dm.get_cached_matches(day, lottery_type="all")
    if not safe:
        safe = dm.fetch_matches_by_date(day, lottery_type="all")

    orders = parse_orders(response_text, safe, dm)

    # ── 去重：已有未结算订单的 (lota_id, bet_type) 不重复下单 ──
    pending_markets = {
        (o.get("lota_id"), o.get("bet_type"))
        for o in role.get_orders()
        if not o.get("settled_at") and o.get("lota_id")
    }

    # ── 已开赛跳过（对齐 node_place_orders 的 live 语义）──
    # 仅当 now 仍落在该足球日窗口内时，跳过已开赛比赛（维持原仓），
    # 避免 live 重跑下到已开赛场次；回测 / now 已过窗口时不触发。
    started_lids: set[str] = set()
    try:
        _d = _date.fromisoformat(day)
        _start, _end = get_football_day(_d)
        _now = _now_bj()
        if _start <= _now <= _end:
            started_lids = {
                m.get("lota_id", "") for m in safe
                if m.get("match_time") and m.get("match_time") <= _now
            }
    except (ValueError, TypeError):
        pass

    new_orders: list[dict] = []
    skipped = 0
    for o in orders:
        if o.get("skip"):
            skipped += 1
            continue
        lid = o.get("lota_id", "")
        if lid in started_lids:
            skipped += 1
            print(f"  🔒 跳过已开赛: {lid}（维持原仓）")
            continue
        if (lid, o.get("bet_type")) in pending_markets:
            skipped += 1
            continue
        new_orders.append(o)

    # ── 资金折算（与 node_place_orders 一致）──
    capital = role.capital
    locked_exposure = sum(
        o.get("bet_size", 0) for o in role.get_orders() if not o.get("settled_at")
    )
    limits = order_limits_for(role.name)
    if limits.enabled:
        new_orders, _dropped = FundManager(limits).apply(new_orders, capital)
    else:
        full_amount = capital + locked_exposure
        new_total = sum(o.get("bet_size", 0) for o in new_orders)
        if new_total > 0 and full_amount > 0:
            scale = capital / full_amount
            for o in new_orders:
                o["bet_size"] = int(o["bet_size"] * scale)

    # ── 下单 ──
    placed = 0
    placed_orders: list[dict] = []
    for o in new_orders:
        try:
            role.place_order(o)
            placed += 1
            placed_orders.append({
                "lota_id": o.get("lota_id"),
                "bet_type": o.get("bet_type"),
                "pick": o.get("pick"),
                "odds": o.get("odds"),
                "bet_size": o.get("bet_size"),
                "reason": (o.get("reason") or "")[:40],
            })
        except ValueError:
            break

    role.save()
    return {
        "user": user,
        "day": day,
        "parsed": len(orders),
        "placed": placed,
        "skipped": skipped,
        "capital": role.capital,
        "orders": placed_orders,
    }


if __name__ == "__main__":
    import contextlib
    import io

    result: dict
    try:
        data = json.loads(sys.stdin.read() or "{}")
        # 函数内部的 print 诊断信息重定向到 stderr，stdout 只留纯 JSON
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = place_orders_from_response(
                data.get("user", "default"),
                data.get("day", ""),
                data.get("orders_text", ""),
            )
        sys.stderr.write(buf.getvalue())
    except Exception as e:  # noqa: BLE001 —— 桥接层把异常转成 JSON，恒返回合法 JSON
        result = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False))
