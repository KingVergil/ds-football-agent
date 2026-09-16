"""两轴"分析链路"守卫（2026-09-15 固定下来的 B 做法）。

钉死四件事，防止后续误改：
  1. `cond` 求值器（`src/axis_cond.py`）的字段语义与三侧直读写法；
  2. `factor_select._axis_profile()` —— 方向边际 / 兑现差的算法，以及
     **只吃 `date < 分析日` 的样本**（未来信息边界）；
  3. 没有 `axis_samples` 的因子**必须走原逻辑**（单狗/其它狗零影响）；
  4. 两轴排序只排序、不过滤（负边际 = 负分沉底，而不是被剔除）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.axis_cond import build_eval_env, eval_cond          # noqa: E402
from src.factor_select import _axis_profile, factor_profile  # noqa: E402


def _leg(side="A", mp=0.30, od=4.0, disp=None, day="2026-08-09", lid="L1", ah=None, **kw):
    feats = {}
    if disp is not None:
        feats["disp_last"] = disp
    if ah:
        feats["ah_crown"] = ah
    leg = {"day": day, "lota_id": lid, "side": side, "market_p": mp,
           "beidan_odds": od, "x": mp * od, "span": 0.5, "goal_line": 0.0,
           "feats": feats, "hit": False, "sp": 0.0}
    leg.update(kw)
    return leg


# ── ① cond 求值器 ────────────────────────────────────────────────

def test_cond_three_side_direct_read():
    """`disp >= disp.h 且 disp.d 且 disp.a` —— 本侧离散 ≥ 另外两侧（无 rank_* 变量）。"""
    legs = [_leg(side="H", disp=[3.0, 9.0, 5.0], lid="L1"),
            _leg(side="D", disp=[3.0, 9.0, 5.0], lid="L1"),
            _leg(side="A", disp=[3.0, 9.0, 5.0], lid="L1")]
    env = build_eval_env(legs)
    cond = "disp >= disp.h 且 disp >= disp.d 且 disp >= disp.a"
    assert [eval_cond(cond, l, env) for l in legs] == [False, True, False]


def test_cond_side_and_water():
    """`side == H 且 ah.h0 < ah.h1`（主队水位走高）只命中主队侧。"""
    ah = {"h0": 0.90, "h1": 1.05, "a0": 1.0, "a1": 0.95, "line0": 0.0, "line1": 0.0}
    legs = [_leg(side="H", ah=ah), _leg(side="A", ah=ah)]
    env = build_eval_env(legs)
    cond = "side == H 且 ah.h0 < ah.h1"
    assert eval_cond(cond, legs[0], env) is True
    assert eval_cond(cond, legs[1], env) is False


def test_cond_missing_field_is_false_and_bad_syntax_raises():
    leg = _leg(disp=None)
    env = build_eval_env([leg])
    assert eval_cond("disp >= 5", leg, env) is False        # 字段缺失 ⇒ 不成立
    with pytest.raises(ValueError):
        eval_cond("这不是条件", leg, env)                    # 解析失败必须抛，别静默


def test_cond_env_rank_uses_all_three_sides():
    """名次类条件必须按该场三侧全量算（含未过门侧）。"""
    legs = [_leg(side="H", mp=0.50, lid="L1"), _leg(side="D", mp=0.30, lid="L1"),
            _leg(side="A", mp=0.20, lid="L1")]
    env = build_eval_env(legs)
    # rank_mp：1 = 市场 p̂ 最低，3 = 最高 ⇒ H(0.50) 排第 3
    hits = [l["side"] for l in legs if eval_cond("rank_mp == 3", l, env)]
    assert hits == ["H"]
    lowest = [l["side"] for l in legs if eval_cond("rank_mp == 1", l, env)]
    assert lowest == ["A"]


# ── ② _axis_profile：算法 + 未来信息边界 ────────────────────────

def test_axis_profile_direction_edge_and_asof_gate():
    """方向边际 =（Σ命中 − Σ市场p̂）/ 腿数；且 **只吃 date < 分析日** 的样本。"""
    stats = {"type": "directional", "desc": "x", "axis_samples": [
        {"date": "2026-08-08", "n": 100, "sum_p": 30.0, "sum_hit": 40.0},   # 吃
        {"date": "2026-08-09", "n": 100, "sum_p": 0.0, "sum_hit": 100.0},   # 分析日当天：不吃
    ]}
    prof = _axis_profile(stats, datetime(2026, 8, 9))
    assert prof is not None
    assert prof["n"] == 100
    assert prof["axis_edge_pp"] == pytest.approx(10.0)       # (40−30)/100
    assert prof["rank_score"] == pytest.approx(1.0)          # ×10
    assert prof["axis_kind"] == "directional"


def test_axis_profile_volatility_delta():
    stats = {"type": "volatility", "axis_samples": [
        {"date": "2026-08-08", "n": 20, "ratio_med": 1.10, "base_med": 0.85},
        {"date": "2026-08-07", "n": 20, "ratio_med": 1.00, "base_med": 0.85},
    ]}
    prof = _axis_profile(stats, datetime(2026, 8, 9))
    assert prof["axis_delta"] == pytest.approx(0.20)         # 均值(0.25, 0.15)
    assert prof["rank_score"] > 0                            # 正 ⇒ 排序靠前


def test_axis_profile_excludes_all_future_samples():
    """只有未来样本时必须返回 None（不能拿未来数据打分）。"""
    stats = {"type": "directional", "axis_samples": [
        {"date": "2026-08-09", "n": 10, "sum_p": 3.0, "sum_hit": 9.0}]}
    assert _axis_profile(stats, datetime(2026, 8, 9)) is None


# ── ③ 没有 axis_samples ⇒ 原逻辑 ────────────────────────────────

def test_legacy_factor_untouched():
    """单狗/其它狗的因子（无 axis_samples）必须走原来那套，行为不变。"""
    stats = {"total": 5, "hit": 3, "miss": 2, "push": 0, "profit": 1.0,
             "type": "directional",
             "history": [{"date": "2026-08-01", "hit": True, "profit": 0.5,
                          "return_ratio": 0.5, "sp": 2.0, "unit_cost": 1.0}],
             "screen": None}
    prof = factor_profile(stats, now=datetime(2026, 8, 9))
    assert prof is not None
    assert "axis_kind" not in prof          # 走的是旧画像


def test_axis_samples_take_precedence():
    stats = {"type": "directional", "total": 1, "hit": 1, "miss": 0, "push": 0,
             "profit": 0.0, "axis_samples": [
                 {"date": "2026-08-01", "n": 50, "sum_p": 15.0, "sum_hit": 17.5}]}
    prof = factor_profile(stats, now=datetime(2026, 8, 9))
    assert prof["axis_kind"] == "directional"
    assert prof["n"] == 50
