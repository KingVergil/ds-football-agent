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

from datetime import datetime

# 每个因子取最近 N 次触发评估
FACTOR_SAMPLE_WINDOW = 6
# 指数衰减半衰期（天）：样本距今每过一个半衰期，权重减半
FACTOR_DECAY_HALF_LIFE_DAYS = 7.0
# 休眠阈值 = 平均触发间隔 × 该倍数
FACTOR_INTERVAL_MULTIPLIER = 3.0
# 贝叶斯收缩先验（Beta(α, β)，弱先验：样本少时向 ~50% 收缩）
SHRINK_ALPHA = 2.0
SHRINK_BETA = 2.0
# 主列表上限（相对截断，可按因子库规模调整）
FACTOR_MAX_MAIN = 12
# 样本少于该值 → 标 ⚠️样本少（不确定性警告）
FACTOR_SMALL_SAMPLE = 5


def factor_profile(stats: dict, now: datetime | None = None) -> dict | None:
    """
    计算因子在"最近 N 单"上的自适应画像。

    返回:
      n            — 样本窗内样本数
      hits         — 窗口内命中数
      w_return     — 衰减加权平均单注回报（排序分数）
      shrunk_rate  — 贝叶斯收缩命中率（展示用，消除小样本"100%"幻觉）
      dormant      — 超过 3×平均触发间隔未触发
      interval_days — 该因子历史平均触发间隔（天）
      last_age_days — 距最近一次触发多少天
      first/last_seen_recent — 窗口内首末触发日期

    无历史 → 返回 None。
    """
    now = now or datetime.now()
    hist = stats.get("history") or []
    if not hist:
        return None
    hist_sorted = sorted(hist, key=lambda h: h.get("date", ""))
    recent = hist_sorted[-FACTOR_SAMPLE_WINDOW:]
    now_d = now.date()

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
    # 半赢(hit=0.5)按 0.5 命中计，半输(hit=-0.5)不计命中
    hits = sum(
        1.0 if h.get("hit") is True else
        (0.5 if h.get("hit") == 0.5 else 0.0)
        for h in recent
    )
    n = len(recent)
    shrunk_rate = (hits + SHRINK_ALPHA) / (n + SHRINK_ALPHA + SHRINK_BETA)

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
    last_seen = stats.get("last_seen") or ""
    if last_seen:
        try:
            last_age_days = (
                now_d - datetime.strptime(last_seen[:10], "%Y-%m-%d").date()
            ).days
        except ValueError:
            last_age_days = None
    if last_age_days is not None:
        base = interval_days if interval_days else FACTOR_DECAY_HALF_LIFE_DAYS
        dormant = last_age_days > base * FACTOR_INTERVAL_MULTIPLIER

    return {
        "n": n,
        "hits": hits,
        "w_return": w_return,
        "shrunk_rate": shrunk_rate,
        "dormant": dormant,
        "interval_days": interval_days,
        "last_age_days": last_age_days,
        "first_seen_recent": recent[0].get("date", "")[:10],
        "last_seen_recent": recent[-1].get("date", "")[:10],
    }
