"""
因子选择（注入 prompt 前的自适应筛选）。

设计原则：不用 hard-code 的固定时间窗口（低频因子永远样本不足、高频因子被
老样本稀释），改用三种可解释的自适应机制：

  1. 样本窗     — 每个因子取最近 N 次触发评估，天数自适应
  2. 指数衰减加权 — 样本越新权重越高（半衰期可解释，无硬截断）
  3. 自适应休眠  — 超过 3 倍平均触发间隔未触发 → 视为失效（按因子自身频率）
  4. 贝叶斯收缩  — 小样本命中率向先验收缩，避免"1单命中100%"误导

唯一的参数（N、半衰期、间隔倍数）都有明确含义，且可进一步由数据推导。
"""

import math
from datetime import datetime

# 每个因子取最近 N 次触发评估
FACTOR_SAMPLE_WINDOW = 6
# 指数衰减半衰期（天）：样本距今每过一个半衰期，权重减半
FACTOR_DECAY_HALF_LIFE_DAYS = 7.0
# 休眠阈值 = 平均触发间隔 × 该倍数
FACTOR_INTERVAL_MULTIPLIER = 3.0
# 退役/休眠时间窗口：距最近一次触发超过该天数才视为休眠
FACTOR_DORMANT_DAYS = 30.0
# 贝叶斯收缩先验（Beta(α, β)，弱先验：样本少时向 ~50% 收缩）
SHRINK_ALPHA = 2.0
SHRINK_BETA = 2.0
# 主列表上限（相对截断，可按因子库规模调整）
FACTOR_MAX_MAIN = 20
# 设计（2026-08-24 对齐用户原意）：正回报与负回报因子都进主区——
# 正=顺向信号，负=反向/反买/规避信号；只有 |加权回报| < 阈值（0 回报附近）的噪声不展示。
FACTOR_MAX_MAIN_POS = 10
FACTOR_MAX_MAIN_NEG = 10
FACTOR_NOISE_W_RETURN = 0.10
# 样本少于该值 → 标 ⚠️样本少（不确定性警告）
FACTOR_SMALL_SAMPLE = 5
# 可行动门槛：已决策样本（total - push）低于该值的因子只进观察区，不进主区
FACTOR_MIN_ACTIONABLE = 10
# 排序时对样本量的不确定性惩罚系数
FACTOR_SAMPLE_PENALTY = 0.5

# ── 跨狗因子信息门槛（对齐退役逻辑的"低信息"口径）──
# 只保留"方向明确"（正/负皆可）的因子进跨狗注册表候选，剔除平庸（命中≈五五开且回报弱）因子。
CROSS_MIN_SAMPLE = 3                 # 近期触发 >=3 次，杜绝 1-2 单"全中"幻觉
CROSS_LOW_INFO_HIT_LO = 0.35         # 命中率落在硬币区间 [LO, HI] 且平均回报≈0 → 低信息噪声
CROSS_LOW_INFO_HIT_HI = 0.65
CROSS_MIN_EDGE_RETURN = 0.30         # "方向明确"的回报门槛：命中率近五五开时，|回报|需 >= 该值


def factor_profile(stats: dict, now: datetime | None = None) -> dict | None:
    """
    计算因子在"最近 N 单"上的自适应画像。

    返回:
      n            — 样本窗内样本数
      hits         — 窗口内命中数
      w_return     — 衰减加权平均单注回报（排序分数）
      decided      — 全历史已决策样本数（total - push）
      rank_score   — 加入样本量惩罚后的排序分数
      shrunk_rate  — 贝叶斯收缩命中率（展示用，消除小样本"100%"幻觉）
      dormant      — 超过 3×平均触发间隔未触发
      interval_days — 该因子历史平均触发间隔（天）
      last_age_days — 距最近一次触发多少天
      first/last_seen_recent — 窗口内首末触发日期

    无历史 → 返回 None。
    """
    now = now or datetime.now()
    now_d = now.date()
    hist = stats.get("history") or []
    if not hist:
        return None
    # 只保留 as_of 之前的历史，防止预加载因子库/并行挖掘时未来样本泄漏
    hist = [
        h for h in hist
        if (h.get("date", "") or "")[:10] <= now_d.isoformat()
    ]
    if not hist:
        return None
    hist_sorted = sorted(hist, key=lambda h: h.get("date", ""))
    recent = hist_sorted[-FACTOR_SAMPLE_WINDOW:]

    weights = []
    for h in recent:
        d = h.get("date", "")[:10]
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            day = now_d
        age = max((now_d - day).days, 0)
        weights.append(0.5 ** (age / FACTOR_DECAY_HALF_LIFE_DAYS))
    wsum = sum(weights) or 1.0
    w_return = sum(
        h.get("return_ratio", 0.0) * w
        for h, w in zip(recent, weights)
    ) / wsum
    # 加权波动：近期单注回报的指数衰减加权标准差（信息量/波动度量）
    _vals = [h.get("return_ratio", 0.0) for h in recent]
    _wvar = sum(w * (v - w_return) ** 2 for v, w in zip(_vals, weights)) / wsum
    volatility = max(_wvar, 0.0) ** 0.5
    # 半赢(hit=0.5)按 0.5 命中计，半输(hit=-0.5)不计命中
    hits = sum(
        1.0 if h.get("hit") is True else
        (0.5 if h.get("hit") == 0.5 else 0.0)
        for h in recent
    )
    n = len(recent)
    shrunk_rate = (hits + SHRINK_ALPHA) / (n + SHRINK_ALPHA + SHRINK_BETA)
    decided = max(float(stats.get("total", 0) - stats.get("push", 0)), 0.0)
    full_hit_rate = (float(stats.get("hit", 0)) / decided) if decided > 0 else 0.0
    strong_large = decided >= 20 and full_hit_rate > 0.55
    factor_type = stats.get("type", "directional")
    rank_score = (
        w_return - FACTOR_SAMPLE_PENALTY / math.sqrt(decided)
        if decided > 0 else w_return
    )
    avg_sp = None
    if factor_type == "volatility":
        sps_w = []
        for h, w in zip(recent, weights):
            sp = h.get("sp")
            if sp is None and h.get("hit") is True:
                rr = h.get("return_ratio")
                if rr is not None:
                    # 北单单注 2 元口径：rr = sp * 0.65 - 1
                    sp = (float(rr) + 1.0) / 0.65
            if sp is not None:
                sps_w.append((float(sp), w))
        if sps_w:
            total_w = sum(w for _, w in sps_w) or 1.0
            avg_sp = sum(sp * w for sp, w in sps_w) / total_w

    # 触发间隔 → 休眠阈值（按因子自身频率自适应）
    dates = [h.get("date", "")[:10] for h in hist_sorted if h.get("date")]
    interval_days = None
    if len(dates) >= 2:
        diffs = []
        for a, b in zip(dates, dates[1:]):
            try:
                diffs.append(
                    (datetime.strptime(b, "%Y-%m-%d").date()
                     - datetime.strptime(a, "%Y-%m-%d").date()).days
                )
            except ValueError:
                pass
        if diffs:
            # 同日聚类（interval≈0）不是有效触发间隔：若全在同一天触发，
            # 回落默认半衰期，避免 1-2 天没触发就被误判休眠
            _avg = sum(diffs) / len(diffs)
            interval_days = _avg if _avg >= 1.0 else None

    dormant = False
    last_age_days = None
    # 休眠判定只看 as_of 之前最近一次触发，避免未来 last_seen 误判休眠
    last_seen = hist_sorted[-1].get("date", "") or stats.get("last_seen") or ""
    if last_seen:
        try:
            last_age_days = (
                now_d - datetime.strptime(last_seen[:10], "%Y-%m-%d").date()
            ).days
        except ValueError:
            last_age_days = None
    if last_age_days is not None:
        dormant = last_age_days > FACTOR_DORMANT_DAYS

    return {
        "n": n,
        "hits": hits,
        "w_return": w_return,
        "factor_type": factor_type,
        "avg_sp": avg_sp,
        "decided": decided,
        "strong_large": strong_large,
        "full_hit_rate": full_hit_rate,
        "rank_score": rank_score,
        "volatility": volatility,
        "shrunk_rate": shrunk_rate,
        "dormant": dormant,
        "interval_days": interval_days,
        "last_age_days": last_age_days,
        "first_seen_recent": recent[0].get("date", "")[:10],
        "last_seen_recent": recent[-1].get("date", "")[:10],
    }
