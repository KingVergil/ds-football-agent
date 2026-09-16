"""北单两轴（方向 / 波动）选腿引擎的守卫。

三件事钉死：
  1. 三轴分工 —— x 只在阶段 0 当门，不参与排序；方向由 directional 规则判；
     波动由 span/赔率排序（**升序**，低波动优先）。
  2. 口径 —— 腿级线 `(1/0.65)^(1/4)`；`axis_report` 的方向轴是 `命中率 - 市场 p̂`，
     不是 y；y 只做最后验收。
  3. 已被证伪的打法不得回潮 —— 「最凝聚侧」「升盘」在方向轴上必须是**负**先验
     （单关狗旗舰策略在北单门内无增量，见 docs/beidan_axis_two_stage.md）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_axis import (  # noqa: E402
    DIRECTION_RULES, LEG_LINE4, SPAN_T1, SPAN_T2, axis_report, direction_gate,
    direction_score, features_of, handicap_val, odds_span, two_stage_select,
    volatility_key, volatility_tier,
)


def _leg(side="A", mp=0.28, x=1.25, odds=4.0, span=0.45, feats=None, hit=False, sp=0.0,
         **kw):
    leg = {"side": side, "market_p": mp, "x": x, "beidan_odds": odds,
           "span": span, "feats": feats or {}, "hit": hit, "sp": sp,
           "settled": True, "day": "2026-09-01", "lota_id": "L1"}
    leg.update(kw)
    return leg


# ── ① 口径 ───────────────────────────────────────────────────────

def test_leg_line_is_per_leg_of_four():
    assert LEG_LINE4 == pytest.approx((1 / 0.65) ** 0.25, abs=1e-9)
    assert LEG_LINE4 < 1 / 0.65


# ── ② 解析 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("tok,want", [
    ("平手", 0.0), ("半球", 0.5), ("半/一", 0.75), ("受半/一", -0.75),
    ("一球", 1.0), ("受一球", -1.0), ("两球半", 2.5), ("", None), ("???", None),
])
def test_handicap_val(tok, want):
    assert handicap_val(tok) == want


def test_features_of_reads_sections():
    secs = {
        "asian-handicap-crown": (
            "亚盘:Crown t=Δt±m odds=h/handicap/a/r(rrr%)\n"
            "OPt-4807m=0.85/受半/一/0.97(r95.41%)\n"
            "Δt+2401m↑→↓↑0.98/受半/一/0.89/96.70\n"
        ),
        "eu-odds-pinnacle": (
            "OPt-5168m=4.96/3.65/1.69(r93.69%)\n"
            "Δt+5167m↑↑↑↑4.50/3.80/1.85/95.22\n"
        ),
        "discrete-odds": "OPt-1439m=16.43/26.96/3.68\nΔt+1298m↑↓↓17.20/25.72/3.28\n",
        "fair-odds": "主客进球和: 2.40\n主客实力差: 0.30\n",
    }
    f = features_of(secs)
    assert f["ah_crown"]["line1"] == pytest.approx(-0.75)     # 受半/一 带斜杠也要解析
    assert f["ah_crown"]["h1"] == pytest.approx(0.98)
    assert f["ah_crown"]["dline"] == 0.0                      # 盘口不动
    assert f["eu1"] == [4.50, 3.80, 1.85]                     # 末行 = 最新
    assert f["eu_move"]["H"] < 0                              # 主队赔率下沉
    assert f["goals_sum"] == pytest.approx(2.40)
    assert f["disp_min_side"] == "A"                          # 3.28 最小


def test_odds_span():
    assert odds_span(3.0, 3.0, 3.0) == pytest.approx(0.0)
    assert odds_span(2.0, 3.0, 4.0) == pytest.approx(2 / 3)
    assert odds_span(None, 3.0, 4.0) is None
    assert odds_span(0, 3.0, 4.0) is None


# ── ③ 方向轴 ─────────────────────────────────────────────────────

def test_cold_side_and_weak_side_pass():
    feats = {"eu1": [1.60, 3.60, 5.20], "disp_max_side": "A"}
    ok, score, rules, why = direction_gate(feats, "A", 0.20)
    assert ok and score >= 2.0
    assert any("冷门侧" in r for r in rules)


def test_water_rising_is_a_veto():
    """主队水位走高（≥1.02）= 方向轴为负 → 即使同时命中弱侧也不放行。"""
    feats = {"ah_crown": {"h1": 1.05, "h0": 0.95, "dline": 0}}
    ok, score, rules, why = direction_gate(feats, "H", 0.30)
    assert not ok
    assert "否决" in why


def test_hot_side_has_no_edge():
    feats = {"eu1": [1.50, 3.90, 6.00]}
    score, hits = direction_score(feats, "H", 0.60)
    assert score < 2.0


def test_discarded_playstyles_stay_negative():
    """已被证伪的两条不得回潮：「最凝聚侧」「升盘」。"""
    tbl = {name: delta for name, delta, _, _ in DIRECTION_RULES}
    assert tbl["我方=最凝聚侧（本门内无效）"] < 0
    assert tbl["升盘（让球线加大）"] < 0


# ── ④ 波动轴 ─────────────────────────────────────────────────────

def test_volatility_key_prefers_concentrated_structure():
    tight = _leg(span=0.30, odds=3.4)
    loose = _leg(span=0.90, odds=5.0)
    assert volatility_key(tight) < volatility_key(loose)
    assert volatility_tier(SPAN_T1 - 0.01) == "T1"
    assert volatility_tier(SPAN_T2 + 0.01) == "T3"
    assert volatility_tier(None) == "T?"


def test_volatility_key_missing_span_is_worst():
    a = _leg(span=None, odds=3.0)
    assert volatility_key(a) >= (9.99, 0.0)


# ── ⑤ 两阶段 ─────────────────────────────────────────────────────

def test_two_stage_price_gate_and_order():
    feats = {"eu1": [1.60, 3.60, 5.20]}       # A 是冷门侧
    low = _leg(side="A", x=0.90, span=0.20, feats=feats)
    tight = _leg(side="A", x=1.30, span=0.30, feats=feats, lota_id="L2")
    loose = _leg(side="A", x=1.60, span=0.80, feats=feats, lota_id="L3")
    out = two_stage_select([low, loose, tight])
    assert [l["lota_id"] for l in out["ranked"]] == ["L2", "L3"]   # 波动升序，不按 x
    assert out["stage1_dropped"] == 1
    assert "价格轴" in out["rejected"][0][1]


def test_two_stage_direction_gate_drops_cold_favorite():
    feats = {"eu1": [1.55, 3.80, 5.60]}
    hot = _leg(side="H", mp=0.62, x=1.40, span=0.40, feats=feats)
    cold = _leg(side="A", mp=0.18, x=1.20, span=0.40, feats=feats, lota_id="L9")
    out = two_stage_select([hot, cold])
    assert [l["lota_id"] for l in out["ranked"]] == ["L9"]


def test_max_span_cuts_high_volatility_bucket():
    feats = {"eu1": [1.60, 3.60, 5.20]}
    out = two_stage_select([_leg(feats=feats, span=0.80)], max_span=0.695)
    assert not out["ranked"] and out["stage2_dropped"] == 1


def test_top_truncates():
    feats = {"eu1": [1.60, 3.60, 5.20]}
    legs = [_leg(feats=feats, span=0.30 + i * 0.01, lota_id=f"L{i}") for i in range(5)]
    assert len(two_stage_select(legs, top=2)["ranked"]) == 2


# ── ⑥ 因子审计：两轴分开报 ───────────────────────────────────────

def test_axis_report_separates_axes():
    legs = [
        _leg(hit=True, sp=6.0, mp=0.30, odds=4.0),
        _leg(hit=True, sp=4.0, mp=0.30, odds=4.0),
        _leg(hit=False, sp=0.0, mp=0.30, odds=4.0),
        _leg(hit=False, sp=0.0, mp=0.30, odds=4.0),
    ]
    r = axis_report(legs, "t")
    assert r["n"] == 4
    assert r["hit"] == pytest.approx(0.5)
    assert r["ddir_pp"] == pytest.approx(20.0)          # 命中率 − 市场 p̂，不是 y
    assert r["y"] == pytest.approx((6.0 + 4.0) / 4)     # y 只在结算轴上
    assert r["a_med"] == pytest.approx(1.25)
    assert r["passes_leg_line"] in (True, False)


def test_axis_report_empty():
    assert axis_report([], "t")["n"] == 0
