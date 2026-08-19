"""
效果 A 的资金查询桥 —— 供 harness agent 的 ds_capital 工具调用。

职责（只读，无副作用）：返回某只狗的当前资金现状，供 agent 产出 order 前
执行「查资金 → 定信心比例 → 出金额」，对齐 LangGraph node_build_prompt 的
资金上下文注入（全金额 = 余额 + 锁定敞口）。

stdin:  {"user": <狗名>}
stdout: {"user","capital","locked_exposure","full_capital","unsettled_count","limits"}
        或 {"error"}

运行:  echo '{"user":"梭哈2狗"}' | python3 -m src.capital_query
"""

from __future__ import annotations

import json
import sys

from .fund_limits import order_limits_for
from .role import Role


def _limits_to_dict(limits) -> dict:
    """OrderLimits → 可 JSON 的 dict（对齐 fund_limits.py 的约束项）。"""
    return {
        "max_exposure_pct": limits.max_exposure_pct,
        "truncate": limits.truncate,
        "max_orders": limits.max_orders,
        "min_orders": limits.min_orders,
    }


def capital_query(user: str) -> dict:
    """读取某只狗的资金现状（只读）。"""
    try:
        role = Role.load(user)
    except (FileNotFoundError, ValueError):
        role = Role(name=user, capital=10000)
        role.save()

    unsettled = [o for o in role.get_orders() if not o.get("settled_at")]
    locked = sum(float(o.get("bet_size", 0)) for o in unsettled)
    capital = float(role.capital)

    return {
        "user": user,
        "capital": round(capital, 2),
        "locked_exposure": round(locked, 2),
        "full_capital": round(capital + locked, 2),
        "unsettled_count": len(unsettled),
        "limits": _limits_to_dict(order_limits_for(user)),
    }


if __name__ == "__main__":
    result: dict
    try:
        data = json.loads(sys.stdin.read() or "{}")
        result = capital_query(data.get("user", "default"))
    except Exception as e:  # noqa: BLE001 —— 桥接层把异常转成 JSON，恒返回合法 JSON
        result = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False))
