"""
DSFootball Python CLI — 模型存储 & 业务逻辑

每个写操作包含检查逻辑，确保数据一致性。
不包含回测（backtest 单独一个文件）。
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from .models import (
    Factor, model_to_dict,
)

# ═══════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════

DATA_ROOT = Path(__file__).parent.parent / "data"
MATCHES_DIR = DATA_ROOT / "matches"
PREDICTS_DIR = DATA_ROOT / "predicts"
# 因子定义目录与 factor_induction.FACTORS_DIR **必须同口径**：
# 沙箱回放（DS_ROLES_ROOT 存在，见 src/role.py::_flat_role_root）时由 bridge/编排脚本
# 一并设置 DS_FACTORS_ROOT 指向沙箱 <root>/factors。这里若写死 data/factors，
# 反思新发现的因子定义会漏回线上全局库（沙箱因子记忆引用 fac_id 但线上多出垃圾定义），
# 破坏「沙箱零线上影响」——2026-09-11 bcl狗 7.11 loop 实测踩到。
FACTORS_DIR = Path(os.environ.get("DS_FACTORS_ROOT") or DATA_ROOT / "factors")
BACKTESTS_DIR = DATA_ROOT / "backtests"

for d in [MATCHES_DIR, PREDICTS_DIR, FACTORS_DIR, BACKTESTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, data: dict | list) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════
# Match
# ═══════════════════════════════════════════════

def save_factor(factor: Factor) -> Factor:
    """
    写入决策因子。检查逻辑:
      1. slugs 非空
      2. slugs 必须在已知 section slug 白名单内
      3. content 非空
    """
    if not factor.slugs:
        raise ValueError("Factor.slugs 不能为空")
    if not factor.content.strip():
        raise ValueError("Factor.content 不能为空")

    # ── 白名单: tools._SECTION_RULES 中的 slug ──
    valid_slugs = _get_valid_section_slugs()
    invalid = [s for s in factor.slugs if s not in valid_slugs]
    if invalid:
        raise ValueError(f"无效 slug: {invalid}，合法值: {valid_slugs}")

    path = FACTORS_DIR / f"{factor.id}.json"
    d = model_to_dict(factor)
    _write_json(path, d)
    return factor


def _get_valid_section_slugs() -> set[str]:
    """从 tools._SECTION_RULES 获取合法 slug 列表"""
    try:
        from src.tools import _SECTION_RULES
        return {slug for slug, _ in _SECTION_RULES}
    except Exception:
        return set()


def settle_order(order_data: dict, score: str) -> dict:
    """
    用比分结算订单。支持赢半/输半（quarter-ball 盘口拆两半结算）。

    盘口类型:
      - 整数 (0, 1, 2...): 可走水
      - 半球 (0.5, 1.5...): 无走水
      - quarter (0.25, 0.75, 1.25...): 拆成 hc±0.25 两半各自结算

    Returns: 更新后的 order dict
    """
    import re
    from src.tools import score2goal_diff, score2goal_sum, score2_1x2

    if not re.match(r'^\d+:\d+$', score):
        raise ValueError(f"比分格式错误: {score}")

    hg, ag = map(int, score.split(":"))
    diff = hg - ag
    total = hg + ag

    bet_type = order_data.get("bet_type", "")
    pick = order_data.get("pick", "")
    handicap = float(order_data.get("handicap") or 0)  # 负=主让 正=主受(主队视觉), adj=diff+handicap直接判定
    odds = float(order_data.get("odds") or 0)
    bet_size = float(order_data.get("bet_size") or 100)

    # ── 胜平负 / 让球胜平负（固定赔率，无 quarter-ball / 无走水）──
    if bet_type in ("胜平负", "让球胜平负"):
        if bet_type == "让球胜平负":
            # 竞彩让球胜平负：goal_line 负=主让、正=主受（主队视角），
            # 调整后净胜球 diff+goal_line 判 H/D/A，无走水
            gl_raw = order_data.get("goal_line")
            gl = float(gl_raw if gl_raw is not None else (order_data.get("handicap") or 0))
            adj = diff + gl
            if adj > 0:
                actual = "H"
            elif adj < 0:
                actual = "A"
            else:
                actual = "D"
        else:
            actual = score2_1x2(score)
        hit = (pick == actual) if pick in ("H", "D", "A") else None

        if hit is None:
            return_amount, profit = bet_size, 0.0
        elif hit is True:
            return_amount = bet_size * odds
            profit = return_amount - bet_size
        else:
            return_amount, profit = 0.0, -bet_size

    # ── 亚盘 / 大小球（港赔水位，支持 quarter-ball）──
    else:
        hit, return_amount, profit = _settle_hk_quarter(
            bet_type=bet_type,
            pick=pick,
            handicap=handicap,
            odds=odds,
            bet_size=bet_size,
            diff=diff,
            total=total,
        )

    order_data["hit"] = hit
    order_data["return_amount"] = round(return_amount, 2)
    order_data["profit"] = round(profit, 2)
    order_data["score"] = score
    order_data["settled_at"] = datetime.now().isoformat()

    return order_data


def _settle_hk_quarter(bet_type: str, pick: str, handicap: float,
                        odds: float, bet_size: float,
                        diff: int, total: int) -> tuple:
    """
    港赔 quarter-ball 结算（亚盘 & 大小球通用）。

    quarter-ball (hc % 0.5 != 0): 拆成 hc-0.25 和 hc+0.25 两半，
    各自独立结算后合并返还。

    Returns: (hit, return_amount, profit)
      hit: True=全赢, False=全输, None=走水/半赢半输
    """
    is_quarter = abs(handicap % 0.5) > 0.001

    if not is_quarter:
        # ── 整数 / 半球：简单判定 ──
        if bet_type == "亚盘":
            adj = diff + handicap  # hc<0=主让(主队视觉), adj>0=主队赢盘
            if adj == 0:
                return None, bet_size, 0.0  # push
            if pick == "H":
                win = adj > 0
            else:
                win = adj < 0
        else:  # 大小球
            if total == handicap:
                return None, bet_size, 0.0  # push
            if pick == "over":
                win = total > handicap
            else:
                win = total < handicap

        if win:
            return True, bet_size * (1 + odds), bet_size * odds
        else:
            return False, 0.0, -bet_size

    # ── quarter-ball: 拆两半 ──
    hc1 = handicap - 0.25
    hc2 = handicap + 0.25

    def _half_result(hc: float) -> str:
        """单一半的结算结果: 'win' | 'push' | 'lose'"""
        if bet_type == "亚盘":
            adj = diff + hc
            if adj == 0:
                return "push"
            if pick == "H":
                return "win" if adj > 0 else "lose"
            else:
                return "win" if adj < 0 else "lose"
        else:  # 大小球
            if total == hc:
                return "push"
            if pick == "over":
                return "win" if total > hc else "lose"
            else:
                return "win" if total < hc else "lose"

    r1 = _half_result(hc1)
    r2 = _half_result(hc2)

    half_bet = bet_size / 2
    ret = 0.0
    for r in [r1, r2]:
        if r == "win":
            ret += half_bet * (1 + odds)
        elif r == "push":
            ret += half_bet
        # lose: +0

    profit = ret - bet_size

    # hit 判定
    wins = (r1 == "win") + (r2 == "win")
    losses = (r1 == "lose") + (r2 == "lose")
    if wins == 2:
        hit = True
    elif losses == 2:
        hit = False
    else:
        hit = None  # mixed: 赢半/输半/双走水

    return hit, ret, profit

