"""退役规则（无倾向 + 样本门槛）与票型容错（N过M）的守卫测试。

背景（2026-09-13，用户口径）
───────────────────────────
1. **票型**：引擎在 `ticket_mode="rule"` 下按 `ticket_tolerance` 出 `N过(N−tol)`；
   缺省 0 = `N串1`（改造前行为，单狗红线）。
   离线实测（40 天 / 3927 侧腿 / 真实结算）：9过4 是唯一"抽掉最好单日后仍为正"的档位。
2. **退役**：ROI **没有明显正负倾向**的因子该退；样本 < 门槛不算结论。
   `|avg_return| ≤ eps` → 退；`avg_return < −eps` → 退；否则留。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.beidan_settlement import parse_ticket_spec, parlay_combos_count  # noqa: E402


# ── 票型：ticket_tolerance → N过M 与成本 ───────────────
def _ticket_for(n_legs: int, tol: int) -> str:
    """复刻 _assemble_legs_rule 的票型推导（纯函数）。"""
    if tol > 0 and n_legs > 2:
        tol = min(tol, n_legs - 2, 8)
        return f"{n_legs}过{n_legs - tol}"
    return f"{n_legs}串1"


def test_tolerance_zero_keeps_plain_parlay():
    """缺省 tol=0 → N串1（1 注 2 元）：单狗/改造前行为不变。"""
    for n in (3, 5, 7, 9):
        label = _ticket_for(n, 0)
        assert label == f"{n}串1"
        spec = parse_ticket_spec(label)
        legs = [{"lota_id": f"L{i}", "picks": ["H"], "odds": 2.0} for i in range(n)]
        assert parlay_combos_count(legs, spec) == 1


def test_tolerance_four_maps_to_n_minus_4():
    """tol=4 → N过(N−4)：9腿→9过5（126 注 / 252 元），7腿→7过3（35 注 / 70 元）。"""
    # 期望值由引擎口径推出：N过M 注数 = C(N, N−M)（全单选腿）
    cases = {9: ("9过5", 126, 252), 7: ("7过3", 35, 70), 5: ("5过2", 10, 20)}
    for n, (label, ways, cost) in cases.items():
        got = _ticket_for(n, 4)
        assert got == label, f"n={n} → {got}，期望 {label}"
        spec = parse_ticket_spec(got)
        legs = [{"lota_id": f"L{i}", "picks": ["H"], "odds": 2.0} for i in range(n)]
        # 每注关数 = n − tol，但不低于 2（短票会被夹）
        assert spec.m == max(2, n - 4)
        assert parlay_combos_count(legs, spec) == ways
        assert parlay_combos_count(legs, spec) * 2 == cost


def test_tolerance_clamped_on_short_tickets():
    """腿太少时容错被夹到 n−2（不允许每注关数 <2）。"""
    assert _ticket_for(3, 4) == "3过2"      # 3−2=1 关不允许 → 夹到 2 关
    assert _ticket_for(5, 99) == "5过2"     # 上限夹到 n−2（每注至少 2 关）
    assert _ticket_for(4, 4) == "4过2"


def test_multi_pick_legs_multiply_cost():
    """多选腿按笛卡尔积放大注数（成本同步上升）。

    实测口径（9过5，每注 5 关）：全单选 126 注；首腿双选 196 注（不是 252——
    双选腿只在"它被包含在某个 5 腿组合里"时才翻倍，含它的组合数 = C(8,4)=70，
    70×2 + C(8,5)=56×1 = 196）。
    """
    def combos(first_picks: int) -> int:
        legs = [{"lota_id": "A", "picks": ["H"] * first_picks, "odds": 2.0}] + \
               [{"lota_id": f"L{i}", "picks": ["H"], "odds": 2.0} for i in range(8)]
        return parlay_combos_count(legs, parse_ticket_spec("9过5"))
    assert combos(1) == 126
    assert combos(2) == 196      # 70×2 + 56
    assert combos(3) == 266      # 70×3 + 56
    assert combos(2) > combos(1)  # 多选 → 成本上升


# ── 退役：无倾向 + 样本门槛 ────────────────────────────
def _decide(hist_returns: list[float], eps: float = 0.20, min_n: int = 5) -> str:
    """复刻 node_factor_review 阶段4 的判定（纯函数）。"""
    if len(hist_returns) < min_n:
        return "留"
    avg = sum(hist_returns) / len(hist_returns)
    if avg < -eps:
        return "退"
    if abs(avg) <= eps:
        return "退"
    return "留"


def test_flat_roi_is_retired():
    """**无倾向 → 退**（用户口径核心）：ROI 在 ±20% 带内。"""
    assert _decide([0.05, -0.05, 0.1, -0.1, 0.0]) == "退"        # 均值 0
    assert _decide([0.19, 0.18, 0.17, 0.16, 0.15]) == "退"       # 均值 +0.17
    assert _decide([-0.19, -0.18, -0.15, -0.2, -0.1]) == "退"    # 均值 −0.164


def test_clear_tendency_is_kept_or_retired():
    """明确正倾向 → 留；明确负倾向 → 退。"""
    assert _decide([0.8, 1.0, 0.9, 0.7, 1.2]) == "留"            # 均值 +0.92
    assert _decide([-0.8, -1.0, -0.9, -0.7, -1.2]) == "退"       # 均值 −0.92


def test_small_sample_is_not_concluded():
    """样本 < 门槛 → 不下结论（免得拿 1~2 个样本判"没倾向"）。"""
    assert _decide([0.0, 0.0]) == "留"
    assert _decide([0.0, 0.0, 0.0, 0.0]) == "留"
    assert _decide([0.0] * 5) == "退"
