"""波动 v2 口径守卫（2026-09-14 用户口径）。

- 工具 1：把 Pinnacle 1X2（goal_line=0）**归一化到该场让球线**的公平概率；
- 工具 2：比例 = 开奖SP / Pinnacle价格；只有 `SP×p̂ > (1/0.65)^(1/4) = 1.11371`
  （**每腿线**；整票线是 `Π(SP·p̂) > 1/0.65`，0.65 只在整票收一次）入选，
  多于 10 条按比例降序截断。
- 样本打分按**引擎 gated 侧**（不由开奖反推）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_high_vol import (  # noqa: E402
    HIGH_VOL_TOP,
    actual_side,
    overlay_of_match,
    pinnacle_probs_at_line,
    select_high_vol,
)

# Pinnacle 1X2 逐行快照：取**最后一行**（最接近开赛）
PIN = [2.10, 3.40, 3.60]          # H 偏热 → 去水后 pH 最大
TAGS = {"eu-odds-pinnacle": "初 2.30/3.30/3.20\n终 2.10/3.40/3.60"}


def _match(lid="L1", result="3", sp=5.0, goal_line=0):
    """result: 北单 result 码（3=主胜，1=平，0=客胜；见 beidan_settlement）。"""
    return {"lota_id": lid,
            "beidan_info": {"result": result, "spvalue": sp, "goal_line": goal_line}}


# ── 工具 1：归一化 ──

def test_probs_sum_to_one_at_any_line():
    for gl in (-2, -1, 0, 1, 2):
        p = pinnacle_probs_at_line(PIN, gl)
        assert p is not None
        assert abs(sum(p) - 1.0) < 0.02, f"gl={gl} 三路概率和 {sum(p)}"


def test_goal_line_zero_equals_devigged_1x2():
    p = pinnacle_probs_at_line(PIN, 0)
    inv = [1.0 / x for x in PIN]
    tot = sum(inv)
    devig = [x / tot for x in inv]
    for got, want in zip(p, devig):
        assert abs(got - want) < 0.01, f"gl=0 应退化为去水 1X2: {p} vs {devig}"


def test_home_giving_a_goal_shifts_probability_to_away_side():
    """主队让 1 球 ⇒ 客胜侧(A)覆盖更多赛果 ⇒ pA 必增大、pH 必减小。"""
    p0 = pinnacle_probs_at_line(PIN, 0)
    p1 = pinnacle_probs_at_line(PIN, -1)
    assert p1[2] > p0[2], f"pA 应增大: {p0} → {p1}"
    assert p1[0] < p0[0], f"pH 应减小: {p0} → {p1}"


def test_invalid_pinnacle_returns_none():
    assert pinnacle_probs_at_line(None, 0) is None
    assert pinnacle_probs_at_line([1.0, 3.4, 3.6], 0) is None      # 有赔率 ≤1
    assert pinnacle_probs_at_line(["x", "y", "z"], 0) is None


# ── 工具 2：比例与阈值 ──

def test_ratio_is_sp_over_pinnacle_price():
    row = overlay_of_match(_match(sp=6.0), TAGS)
    assert row is not None
    assert abs(row["ratio"] - 6.0 * row["phat"]) < 1e-9
    assert abs(row["pinnacle_odds"] - 1.0 / row["phat"]) < 1e-9


def test_threshold_is_four_leg_gap_line():
    """阈值 = (1/0.65)^(1/4)（而每注实际 5 关）→ 故意留 gap。"""
    from src.beidan_high_vol import (HIGH_VOL_GAP_LEGS, HIGH_VOL_RATIO_THRESHOLD,
                                     high_vol_threshold)
    assert HIGH_VOL_GAP_LEGS == 4
    assert abs(HIGH_VOL_RATIO_THRESHOLD - (1 / 0.65) ** 0.25) < 1e-12
    assert abs(HIGH_VOL_RATIO_THRESHOLD - 1.11371) < 1e-4
    # 4 关线必须严于 5 关严格平衡线（gap 的由来）
    assert HIGH_VOL_RATIO_THRESHOLD > high_vol_threshold(5)
    # 整串（5 关）留有余量
    assert HIGH_VOL_RATIO_THRESHOLD ** 5 > 1 / 0.65


def test_threshold_boundary_is_inclusive_exclusive():
    """恰好等于阈值不算入选；略超才算。"""
    from src.beidan_high_vol import HIGH_VOL_RATIO_THRESHOLD as TH
    base = overlay_of_match(_match(sp=1.0), TAGS)
    phat = base["phat"]
    at = TH / phat          # ratio 恰好 == TH
    assert overlay_of_match(_match(sp=at), TAGS)["high_vol"] is False
    assert overlay_of_match(_match(sp=at * 1.001), TAGS)["high_vol"] is True


def test_select_drops_non_qualifying():
    from src.beidan_high_vol import HIGH_VOL_RATIO_THRESHOLD as TH
    phat = overlay_of_match(_match(sp=1.0), TAGS)["phat"]
    ms = [_match(lid="hi", sp=10.0), _match(lid="lo", sp=0.5 * TH / phat)]
    got = select_high_vol(ms, tags_of=lambda lid: TAGS)
    assert [r["lota_id"] for r in got] == ["hi"]


def test_missing_data_excluded():
    assert overlay_of_match(_match(sp=5.0), {}) is None            # 无 Pinnacle
    assert overlay_of_match(_match(sp=0.0), TAGS) is None          # 无 SP
    assert overlay_of_match({"lota_id": "L9", "beidan_info": {}}, TAGS) is None


def test_actual_side_reads_beidan_result():
    assert actual_side(_match(result="3")) == "H"
    assert actual_side(_match(result="1")) == "D"
    assert actual_side(_match(result="0")) == "A"
    assert actual_side({"beidan_info": {}}) is None


def test_select_sorts_desc_and_caps_at_top():
    # 造 12 场都必然入选（SP 很大），比例随 SP 递减
    ms = [_match(lid=f"L{i}", sp=100.0 - i) for i in range(12)]
    got = select_high_vol(ms, tags_of=lambda lid: TAGS)
    assert len(got) == HIGH_VOL_TOP == 10, f"应截断到 10，实际 {len(got)}"
    ratios = [r["ratio"] for r in got]
    assert ratios == sorted(ratios, reverse=True), "必须按比例降序"
    assert got[0]["lota_id"] == "L0", "比例最大的应排第一"


def test_select_without_tags_getter_yields_nothing():
    """拿不到 tags ⇒ 拿不到 Pinnacle ⇒ 不入选（不静默放行）。"""
    assert select_high_vol([_match(sp=100.0)]) == []


# ── 接线：波动侧用 v2、方向侧不动 ──

def test_volatility_pass_uses_v2_selection():
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "select_high_vol(" in src, "波动侧必须走 v2 筛选"
    assert "by_strata=True), True)" not in src, "旧全包分层抽样必须已下线"
    # 方向侧仍是随机 6 场
    assert "_unplaced_completed(day_date, False, 6), False)" in src
    # 样本打分走 gated 侧（is_cover=False）
    assert "_samples(\n            vol_matches, False," in src


def test_candidate_pool_is_single_day_not_rolling():
    """2026-09-14 用户口径：**单日**池，不滚动。

    滚动窗口下每天 top10 与前一日重复 8~10 条（实测），等于天天对同一批
    反复反思，白烧 token ⇒ 明确禁止再引入滚动池。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "_high_vol_pool" not in src, "不得再引入滚动池"
    assert "HIGH_VOL_LOOKBACK_DAYS" not in src, "滚动窗口常量必须已移除"
    assert "vol_pool = self._unplaced_completed(day_date, False, 999)" in src, \
        "波动侧必须用单日池"
    hv = (ROOT / "src" / "beidan_high_vol.py").read_text(encoding="utf-8")
    assert "HIGH_VOL_LOOKBACK_DAYS" not in hv

