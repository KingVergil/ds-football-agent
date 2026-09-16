"""让球线换算：把锐市场「不让球 1X2」概率换算到任意让球盘口（负=主让）。

## 为什么

北单是**让球胜平负**，`goal_line` 可能是 -2/-1/0/+1/+2。但锐市场段里只有一部分盘口有
`公平盘「平均欧盘胜/平/负(<line>)」`（**主受让 gl>0 常常没有**）⇒ `x = 市场p̂ × 北单赔率`
算不出来，这些场次会被整批丢掉（实测覆盖只有 ~74%）。本模块用 Poisson 把 **Pinnacle 1X2**
换算到北单的目标让球线，覆盖率提到 ~100%，且用的是同一个时间档位的锐市场报价。

## 方法

1. 去水后的 1X2 概率 `(pH, pD, pA)` → 拟合两队 Poisson 均值 `(λh, λa)`：

       P(k; λ) = λ^k e^{-λ} / k!
       pH = Σ_{i>j} P(i;λh)P(j;λa)   pD = Σ_i P(i;λh)P(i;λa)   pA = Σ_{i<j} …

   目标函数 = 三路平方误差；先粗网格（0.05）再局部细化（0.01），**不依赖 scipy**。

2. 让球线 `L`（北单口径：负=主队让球）下的三路概率：

       H ⇔ (i - j) + L > 0      D ⇔ (i - j) + L = 0      A ⇔ (i - j) + L < 0

   即「让球后的净胜球」。L=0 时退化为普通 1X2。
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

N_GOALS = 12                       # 单队进球上限（Poisson 尾部已可忽略）


def _pmf(lam: float, n: int = N_GOALS) -> np.ndarray:
    lam = max(float(lam), 1e-6)
    k = np.arange(n + 1)
    logp = k * math.log(lam) - lam - np.array([math.lgamma(i + 1) for i in k])
    return np.exp(logp)


def _probs_from_outer(lams: np.ndarray, n: int = N_GOALS) -> np.ndarray:
    """给定候选 λ 网格，返回 (K,K,3) 的 (pH,pD,pA)。"""
    pmf = np.stack([_pmf(l, n) for l in lams])          # (K, n+1)
    outer = pmf[:, None, :, None] * pmf[None, :, None, :]   # (K,K,n+1,n+1)
    i = np.arange(n + 1)[:, None]
    j = np.arange(n + 1)[None, :]
    d = i - j
    masks = [d > 0, d == 0, d < 0]
    # outer: (K,K,n+1,n+1) → 布尔掩码只作用在最后两维 → (K,K,nnz) → 对最后一维求和
    return np.stack([outer[..., m].sum(axis=-1) for m in masks], axis=-1)


def fit_lambdas(p_home: float, p_draw: float, p_away: float,
                n: int = N_GOALS) -> tuple[float, float]:
    """把去水 1X2 概率拟合到 (λh, λa)（最小三路平方误差）。"""
    target = np.array([p_home, p_draw, p_away], dtype=float)
    grid = np.round(np.arange(0.10, 3.61, 0.05), 3)
    probs = _probs_from_outer(grid, n)                  # (K,K,3)
    err = ((probs - target) ** 2).sum(axis=-1)
    k = np.unravel_index(int(np.argmin(err)), err.shape)
    best = (float(grid[k[0]]), float(grid[k[1]]))
    # 局部细化 ±0.05，步长 0.01
    fine = np.arange(-0.05, 0.0501, 0.01)
    fl = np.array([best[0] + d for d in fine])
    fa = np.array([best[1] + d for d in fine])
    errs = np.empty((len(fl), len(fa)))
    for a, lh in enumerate(fl):
        for b, la in enumerate(fa):
            errs[a, b] = ((_pair_probs(float(lh), float(la), n) - target) ** 2).sum()
    a, b = np.unravel_index(int(np.argmin(errs)), errs.shape)
    if errs[a, b] <= err.min():
        best = (float(fl[a]), float(fa[b]))
    return best


def _pair_probs(lam_h: float, lam_a: float, n: int = N_GOALS) -> np.ndarray:
    ph, pa = _pmf(lam_h, n), _pmf(lam_a, n)
    outer = np.outer(ph, pa)
    i = np.arange(n + 1)[:, None]
    j = np.arange(n + 1)[None, :]
    d = i - j
    return np.array([outer[d > 0].sum(), outer[d == 0].sum(), outer[d < 0].sum()])


def handicap_probs(lam_h: float, lam_a: float, line: float,
                   n: int = N_GOALS) -> tuple[float, float, float]:
    """让球线 `line`（北单口径：负=主让）下的 (pH, pD, pA)。"""
    ph, pa = _pmf(lam_h, n), _pmf(lam_a, n)
    outer = np.outer(ph, pa)
    i = np.arange(n + 1)[:, None]
    j = np.arange(n + 1)[None, :]
    d = (i - j) + float(line)
    return (float(outer[d > 0].sum()), float(outer[d == 0].sum()),
            float(outer[d < 0].sum()))


def convert_1x2_to_line(p_home: float, p_draw: float, p_away: float,
                        line: float) -> Optional[tuple[float, float, float]]:
    """1X2 去水概率 → 任意让球线的三路概率。line=0 时原样返回。"""
    tot = float(p_home) + float(p_draw) + float(p_away)
    if tot <= 0:
        return None
    if abs(float(line)) < 1e-9:
        return (p_home / tot, p_draw / tot, p_away / tot)
    lh, la = fit_lambdas(p_home, p_draw, p_away)
    return handicap_probs(lh, la, line)


def convert_1x2_odds(odds_home: float, odds_draw: float, odds_away: float,
                     line: float) -> Optional[tuple[float, float, float]]:
    """1X2 赔率（含水位）→ 任意让球线的三路概率（内部先比例法去水）。"""
    inv = [1.0 / float(o) for o in (odds_home, odds_draw, odds_away) if o and o > 0]
    if len(inv) != 3:
        return None
    tot = sum(inv)
    return convert_1x2_to_line(inv[0] / tot, inv[1] / tot, inv[2] / tot, line)
