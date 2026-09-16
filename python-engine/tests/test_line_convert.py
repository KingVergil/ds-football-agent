"""让球线换算测试：往返、拟合精度、让球线单调性、与同盘口平均欧盘的一致性（合成用例）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.line_convert import (  # noqa: E402
    convert_1x2_odds, convert_1x2_to_line, fit_lambdas, handicap_probs,
)


def test_line_zero_round_trip():
    p = convert_1x2_odds(2.50, 3.40, 2.80, 0.0)
    inv = [1 / 2.50, 1 / 3.40, 1 / 2.80]
    tot = sum(inv)
    assert all(abs(a - b / tot) < 1e-9 for a, b in zip(p, inv))


def test_fit_recovers_probabilities():
    ph, pd, pa = convert_1x2_odds(2.50, 3.40, 2.80, 0.0)
    lh, la = fit_lambdas(ph, pd, pa)
    fit = handicap_probs(lh, la, 0.0)
    assert max(abs(a - b) for a, b in zip((ph, pd, pa), fit)) < 5e-3
    assert 0.2 < lh < 3.5 and 0.2 < la < 3.5


def test_handicap_line_monotonic():
    """主队让球越多（L 越负），H 概率越低、A 概率越高。"""
    ph, pd, pa = convert_1x2_to_line(0.38, 0.28, 0.34, 0.0)
    hs = [handicap_probs(0.5, 0.9, L)[0] for L in (2, 1, 0, -1, -2)]
    assert all(hs[i] > hs[i + 1] for i in range(len(hs) - 1))
    as_ = [handicap_probs(0.5, 0.9, L)[2] for L in (2, 1, 0, -1, -2)]
    assert all(as_[i] < as_[i + 1] for i in range(len(as_) - 1))


def test_probabilities_sum_to_one_for_any_line():
    for L in (-2.5, -2, -1, -0.5, 0, 0.5, 1, 2):
        p = convert_1x2_to_line(0.36, 0.29, 0.35, L)
        assert abs(sum(p) - 1.0) < 1e-9, p


def test_strong_home_favourite_vs_main_line():
    """1.30/5.0/9.0（主队大热）→ 主让-1 后主胜概率明显低于不让球。"""
    p0 = convert_1x2_odds(1.30, 5.00, 9.00, 0.0)
    p1 = convert_1x2_odds(1.30, 5.00, 9.00, -1.0)
    assert p1[0] < p0[0] and p1[2] > p0[2]
