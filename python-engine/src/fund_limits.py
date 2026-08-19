"""
资金管理硬约束（按狗配置）— 从 node_place_orders 抽出的独立执行器。

设计定稿（grill）：
- 内层（人设）：LLM 按「因子匹配数」降序输出 order 块，理由写「因子匹配数: N」；
- 外层（代码，本模块）：只做硬约束，严格按 LLM 输出顺序处理，不重排、不独立打分。

约束项（全部可空/可关）：
- max_exposure_pct: 单日新增总仓上限（% × 当前可用余额；锁定仓位不占额度）
- truncate: 超限时按序整单丢弃（True）/ 等比缩放到上限（False）
- max_orders: 单数上限（None = 不限）
- min_orders: 保底注数（截断丢弃后不足时按序补回；None = 不保底）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

ROLES_DIR = Path(__file__).resolve().parent.parent / "data" / "roles"


def _read_role_json(agent_name: str) -> Optional[dict]:
    path = ROLES_DIR / agent_name / f"{agent_name}.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


class OrderLimits:
    """单只 agent 的资金管理约束配置。"""

    __slots__ = ("max_exposure_pct", "truncate", "max_orders", "min_orders")

    def __init__(self, max_exposure_pct: Optional[float] = None,
                 truncate: bool = False,
                 max_orders: Optional[int] = None,
                 min_orders: Optional[int] = None):
        self.max_exposure_pct = max_exposure_pct
        self.truncate = truncate
        self.max_orders = max_orders
        self.min_orders = min_orders

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "OrderLimits":
        d = d or {}
        return cls(
            max_exposure_pct=d.get("max_exposure_pct"),
            truncate=bool(d.get("truncate", False)),
            max_orders=d.get("max_orders"),
            min_orders=d.get("min_orders"),
        )

    @property
    def enabled(self) -> bool:
        """是否有任一约束生效（None/False/0 视为不启用）。"""
        return any(v not in (None, False, 0) for v in
                   (self.max_exposure_pct, self.max_orders, self.min_orders))


class FundManager:
    """按配置对 LLM 订单依次应用：单数上限 → 总仓上限（截断/缩放）→ 保底注数。

    严格按 LLM 输出顺序处理（顺序即优先级，不重排、不独立打分），供审计。
    """

    def __init__(self, limits: Optional[OrderLimits | dict] = None):
        self.limits = limits if isinstance(limits, OrderLimits) else OrderLimits.from_dict(limits)

    def apply(self, orders: list[dict], capital: float) -> tuple[list[dict], list[dict]]:
        """返回 (保留订单, 丢弃订单)。不修改传入列表。"""
        if not self.limits.enabled:
            return list(orders), []

        max_pct = self.limits.max_exposure_pct
        truncate = self.limits.truncate
        max_orders = self.limits.max_orders
        min_orders = self.limits.min_orders
        dropped: list[dict] = []
        keep = list(orders)

        # 1. 单数上限：按序保留前 max_orders 单
        if max_orders and len(keep) > max_orders:
            dropped.extend(keep[max_orders:])
            keep = keep[:max_orders]
            print(f"  🔢 单数上限 {max_orders}: 丢弃 {len(dropped)} 单")

        # 2. 总仓上限：基数 = 当前可用余额（锁定仓位不占额度）
        if max_pct and keep:
            cap = capital * max_pct / 100.0
            total = sum(float(o.get("bet_size", 0)) for o in keep)
            if total > cap:
                if truncate:
                    acc, cut = 0.0, None
                    for i, o in enumerate(keep):
                        s = float(o.get("bet_size", 0))
                        if acc + s > cap:
                            cut = i
                            break
                        acc += s
                    if cut is not None:
                        dropped.extend(keep[cut:])
                        keep = keep[:cut]
                        print(f"  ✂️ 超仓截断: 保留 {len(keep)} 单 ¥{acc:,.0f} ≤ "
                              f"{max_pct:.0f}%×余额 ¥{cap:,.0f} | 丢弃 {len(dropped)} 单")
                else:
                    scale = cap / total
                    for o in keep:
                        o["bet_size"] = int(o["bet_size"] * scale)
                    print(f"  📐 超仓缩放: ¥{total:,.0f} → ¥{cap:,.0f} ×{scale:.2f}")

        # 3. 保底注数：截断后不足时，从丢弃单按序补回
        if min_orders and len(keep) < min_orders and dropped:
            need = min_orders - len(keep)
            restored = dropped[:need]
            keep.extend(restored)
            dropped = dropped[need:]
            print(f"  🔧 保底 {min_orders} 注: 补回 {len(restored)} 单")
            if max_pct:
                cap = capital * max_pct / 100.0
                total = sum(float(o.get("bet_size", 0)) for o in keep)
                if total > cap:
                    scale = cap / total
                    for o in keep:
                        o["bet_size"] = int(o["bet_size"] * scale)
                    print(f"  📐 保底后缩放 ×{scale:.2f} → ¥{cap:,.0f}")

        if min_orders and len(keep) < min_orders:
            print(f"  ⚠️ 保底 {min_orders} 注未满足: 仅 {len(keep)} 单")

        return keep, dropped


# ── 按狗配置注册表（缺省 = 不启用，保持旧行为）──
AGENT_LIMITS: dict[str, dict] = {
    # alpha2狗：限制版（实验组）——40%×余额 + 按序整单截断，无单数上限
    "alpha2狗": {"max_exposure_pct": 40.0, "truncate": True,
                 "max_orders": None, "min_orders": None},
    # alpha狗：无限制对照组（不配置 = 保持旧行为）
    "梭哈2狗": {"max_exposure_pct": None, "truncate": False,
                "max_orders": None, "min_orders": 2},
    "梭哈3狗": {"max_exposure_pct": None, "truncate": False,
                "max_orders": None, "min_orders": 2},
}


def order_limits_for(agent_name: str) -> OrderLimits:
    """按 agent 名取约束配置：

    1) roles/<狗>/<狗>.json 的 limits（设计定稿：LangGraph 优先读文件）；
    2) AGENT_LIMITS 硬编码默认（旧行为）；
    3) 未配置 = 空约束，不启用。
    """
    role = _read_role_json(agent_name)
    if role and isinstance(role.get("limits"), dict):
        return OrderLimits.from_dict(role["limits"])
    return OrderLimits.from_dict(AGENT_LIMITS.get(agent_name))
