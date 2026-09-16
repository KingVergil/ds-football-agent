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
# 方向因子排序时对单注回报波动的惩罚系数
FACTOR_VOL_PENALTY = 1.0

# ── 跨狗因子信息门槛（对齐退役逻辑的"低信息"口径）──
# 只保留"方向明确"（正/负皆可）的因子进跨狗注册表候选，剔除平庸（命中≈五五开且回报弱）因子。
CROSS_MIN_SAMPLE = 3                 # 近期触发 >=3 次，杜绝 1-2 单"全中"幻觉
CROSS_LOW_INFO_HIT_LO = 0.35         # 命中率落在硬币区间 [LO, HI] 且平均回报≈0 → 低信息噪声
CROSS_LOW_INFO_HIT_HI = 0.65
CROSS_MIN_EDGE_RETURN = 0.30         # "方向明确"的回报门槛：命中率近五五开时，|回报|需 >= 该值

# 北单返奖率：用于把 return_ratio 反推成开奖 SP（sp = (rr + unit_cost) / 0.65）
BEIDAN_RETURN_RATE = 0.65
# 样本注数基数（unit_cost）：单选腿 1 注、全包/覆盖腿 3 注
UNIT_COST_DIRECTIONAL = 1.0
UNIT_COST_COVER = 3.0


def sample_unit_cost(hist: list[dict]) -> float | None:
    """因子级注数基数：**只在证据一致时**返回（1 = 单注腿，3 = 全包/覆盖腿）；否则 None。

    只认确定性证据，不做"按因子类型猜"（猜错同样是 (3−1)/0.65 ≈ 3.08 的系统性偏差）：
      · 样本自带 `unit_cost`（2026-09-11 起新样本都写）；
      · **输的样本**：方向腿输 = −1、全包腿输 = −3（赢的样本两种口径都 > 0，无法区分）。
    注意基数是**样本级**的：同一因子的历史可能混着 1 注与 3 注样本（实测
    `亚盘盘口反复横跳` = [1,3,3]）→ 混合时返回 None，调用方应逐样本读 `unit_cost`，
    读不到就不计（宁缺勿错）。
    """
    bases: set[float] = set()
    for h in (hist or []):
        uc = h.get("unit_cost")
        if uc is not None:
            try:
                bases.add(float(uc))
            except (TypeError, ValueError):
                pass
            continue
        try:
            rr = float(h.get("return_ratio"))
        except (TypeError, ValueError):
            continue
        if rr <= -2.5:
            bases.add(UNIT_COST_COVER)
        elif -1.5 < rr <= -0.5:
            bases.add(UNIT_COST_DIRECTIONAL)
    return bases.pop() if len(bases) == 1 else None


def sp_from_return_ratio(return_ratio: float, unit_cost: float,
                          hit: bool | None) -> float | None:
    """从单注回报反推开奖 SP（只在**命中**的样本上成立）。

    rr = 0.65 · sp − unit_cost  →  sp = (rr + unit_cost) / 0.65
    单选腿 unit_cost=1（rr = 0.65sp − 1）；北单全包腿 unit_cost=3（rr = 0.65sp − 3）。
    旧实现固定用 1，导致全包腿反推的 SP 系统性低 (3−1)/0.65 ≈ 3.08。
    """
    if hit is not True or return_ratio is None:
        return None
    try:
        rr = float(return_ratio)
    except (TypeError, ValueError):
        return None
    if rr <= 0:      # 未命中/走水不携带 SP 信息
        return None
    return (rr + float(unit_cost or UNIT_COST_DIRECTIONAL)) / BEIDAN_RETURN_RATE


# 北单全包/覆盖腿的成本线：一条腿买 3 注（3 × 2 元），
# 只有当开奖 SP > 3 / 0.65 ≈ 4.615 时这条覆盖腿才不亏。
COVER_BREAKEVEN_SP = 3.0 / BEIDAN_RETURN_RATE


def volatility_lifecycle(stats: dict, now: datetime | None = None) -> dict:
    """波动型因子的确定性生命周期建议（**方向因子不适用**）。

    波动路径不预测方向，只回答"这条腿值不值得覆盖"。判据用**窗口平均单样本回报**
    `w_return`（对全包腿就是 `0.65·SP − 3` 的均值，输的样本按 −3 计入），
    等价于"覆盖的平均 SP 有没有打过 3 注成本线 `COVER_BREAKEVEN_SP ≈ 4.615`"：

    | 情况 | 建议 |
    |---|---|
    | 窗口内没样本、且久未触发（> `FACTOR_DORMANT_DAYS`） | `dormant` |
    | 有样本且 `w_return > 0`（覆盖平均是赚的） | `keep` |
    | `w_return ≤ 0` 但样本不足（< 3） | `observe`（样本不够，不下结论） |
    | `w_return ≤ 0` 且样本 ≥ 3 | `retire`（覆盖长期倒亏） |

    注意：`avg_sp` 只由**命中**样本反推（输的样本反推不出 SP）→ 它是"中奖时平均 SP"，
    天然偏高，不能单独当验收依据；这里只用它做展示与说明。
    """
    prof = factor_profile(stats, now=now)
    if not prof:
        return {"action": "observe", "reason": "无历史样本", "avg_sp": None,
                "w_return": 0.0, "n": 0}
    n = int(prof.get("n") or 0)
    avg_sp = prof.get("avg_sp")
    wr = float(prof.get("w_return") or 0.0)
    # 久未触发优先判休眠（大样本且覆盖仍在赚的除外，对齐方向路径的 strong_large 例外）
    if prof.get("dormant") and not (float(prof.get("decided") or 0) >= 20 and wr > 0):
        return {"action": "dormant",
                "reason": f"近 {FACTOR_DORMANT_DAYS:.0f} 天无触发",
                "avg_sp": avg_sp, "w_return": wr, "n": n}
    n_eff = int(prof.get("vol_n") or 0) or n
    if n <= 0 and n_eff <= 0:
        return {"action": "observe", "reason": "窗口内无样本", "avg_sp": avg_sp,
                "w_return": 0.0, "n": 0}
    # 优先用**粗筛口径**：命中场次的高波动率比当日基线高多少（pp）
    edge = prof.get("vol_edge")
    prec = prof.get("vol_precision")
    base = prof.get("vol_base")
    if edge is not None:
        pp = float(edge) * 100.0
        desc = (f"命中场次高波动率 {float(prec):.0%} vs 当日基线 {float(base):.0%}"
                f"（{pp:+.0f}pp）")
        if pp >= 10:
            return {"action": "keep", "reason": desc + "：有筛选力",
                    "avg_sp": avg_sp, "w_return": wr, "n": n}
        if n_eff < 3:
            return {"action": "observe", "reason": desc + "：样本不足", "avg_sp": avg_sp,
                    "w_return": wr, "n": n_eff}
        if pp <= 0:
            return {"action": "retire", "reason": desc + "：没有筛选力（不如当日平均）",
                    "avg_sp": avg_sp, "w_return": wr, "n": n}
        return {"action": "observe", "reason": desc + "：区分度不足（<10pp）",
                "avg_sp": avg_sp, "w_return": wr, "n": n}
    if wr > 0:
        return {"action": "keep",
                "reason": (f"窗口均回报 {wr:+.2f}/样本（覆盖平均赚；等价均 SP "
                           f"{COVER_BREAKEVEN_SP:.2f} 之上）"),
                "avg_sp": avg_sp, "w_return": wr, "n": n}
    if n < 3:
        return {"action": "observe",
                "reason": f"样本不足（{n}）且窗口均回报 {wr:+.2f}，不下结论",
                "avg_sp": avg_sp, "w_return": wr, "n": n}
    return {"action": "retire",
            "reason": (f"窗口均回报 {wr:+.2f}/样本 ≤ 0（覆盖长期打不过 3 注成本线 "
                       f"SP≈{COVER_BREAKEVEN_SP:.2f}），n={n}"),
            "avg_sp": avg_sp, "w_return": wr, "n": n}


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
      avg_sp       — 波动因子专用：窗口内开奖 SP 的衰减加权均值
      sp_basis     — avg_sp 用到的注数基数（1/3；None = 无证据、按口径不明处理）
      sp_samples   — 实际参与 avg_sp 的样本数（口径不明的样本会被排除）
      dormant      — 超过 3×平均触发间隔未触发
      interval_days — 该因子历史平均触发间隔（天）
      last_age_days — 距最近一次触发多少天
      first/last_seen_recent — 窗口内首末触发日期

    无历史 → 返回 None。

    例外：带 `axis_samples`（两轴因子，见 docs/beidan_two_axis_analysis_plan.md）
    的因子走 `_axis_profile()` 分支 —— 方向轴用「命中率 − 市场 p̂」，波动轴用
    「兑现 − 当日基线」。**样本严格取 `date < 分析日`**，避免当天结果泄漏。
    其它因子的记录里没有该字段 ⇒ 行为逐字节不变。
    """
    now = now or datetime.now()
    if stats.get("axis_samples"):
        prof = _axis_profile(stats, now)
        if prof is not None:
            return prof
    now_d = now.date()
    hist = stats.get("history") or []
    if not hist:
        # 只有日级粗筛统计、还没下过注的因子：也要能被看见（这是飞轮的早期信号）
        sc0 = stats.get("screen") or {}
        n_sc = int(sc0.get("n") or 0)
        if n_sc <= 0:
            return None
        prec = float(sc0.get("high", 0)) / n_sc
        base = float(sc0.get("base_sum", 0.0)) / n_sc
        return {"n": 0, "hits": 0.0, "w_return": 0.0,
                "factor_type": stats.get("type", "directional"),
                "avg_sp": None, "sp_basis": None, "sp_samples": 0,
                "vol_precision": prec, "vol_base": base,
                "vol_edge": prec - base, "vol_n": n_sc,
                "decided": 0.0, "strong_large": False, "full_hit_rate": 0.0,
                "rank_score": 0.0, "volatility": 0.0, "shrunk_rate": 0.5,
                "dormant": False, "interval_days": None, "last_age_days": None,
                "first_seen_recent": "", "last_seen_recent": ""}
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
    if factor_type == "volatility":
        # 波动因子不参与方向命中率/单注回报排序，rank_score 保持裸回报即可
        rank_score = w_return
    else:
        vol_penalty = FACTOR_VOL_PENALTY * volatility / math.sqrt(n)
        sample_penalty = (
            FACTOR_SAMPLE_PENALTY / math.sqrt(decided)
            if decided > 0 else 0.0
        )
        rank_score = w_return - vol_penalty - sample_penalty
    # ── 波动路径：粗筛口径（"命中场次是不是更容易出高波动"）──
    # 粗标签直接复用 hit（全包腿 hit ⇔ profit>0 ⇔ sp > 覆盖成本线），
    # 只跟「当日高波动场次占比」基线比一个差值，**不做 SP 分布/分位数计算**。
    vol_precision = None
    vol_base = None
    vol_edge = None
    vol_n = None
    if factor_type == "volatility":
        # 优先用**日级粗筛统计**（当天被该因子标记的所有场次，含没下注的），
        # 避免"只统计自己下过的腿"的选择偏差；没有则回落"下注腿"口径（不带权简单比例）。
        sc = stats.get("screen") or {}
        if int(sc.get("n") or 0) > 0:
            vol_precision = float(sc.get("high", 0)) / float(sc["n"])
            vol_base = float(sc.get("base_sum", 0.0)) / float(sc["n"])
            vol_edge = vol_precision - vol_base
        if vol_edge is None:
            # 回落："下注腿"口径（窗口内不带权简单比例；可解释、跟人对得上）
            based = [h for h in recent if h.get("vol_base") is not None]
            if based:
                vol_n = len(based)
                vol_base = sum(float(h["vol_base"]) for h in based) / len(based)
                vol_precision = sum(
                    1.0 if h.get("hit") is True else (0.5 if h.get("hit") == 0.5 else 0.0)
                    for h in based) / len(based)
                vol_edge = vol_precision - vol_base
            elif n > 0:
                vol_precision = hits / n

    avg_sp = None
    sp_basis = None
    sp_samples = 0
    if factor_type == "volatility":
        # 全包/覆盖腿的 rr = 0.65·sp − 3（3 注成本），不是单注口径 −1。
        # 口径优先级：样本自带 unit_cost > 「输的样本」证据 > 因子级默认。
        # ⚠️ 隔离红线：**只有 path=beidan 的因子**才允许出现"口径不明就不算"；
        # 其它狗（单关狗等）缺省按单注口径 1 反推，与历史行为逐位一致。
        sp_basis = sample_unit_cost(recent)
        is_beidan = str(stats.get("path") or "") == "beidan"
        basis_fallback = sp_basis
        if basis_fallback is None:
            d = stats.get("unit_cost_default")
            if d is not None:
                try:
                    basis_fallback = float(d)
                except (TypeError, ValueError):
                    basis_fallback = None
            elif not is_beidan:
                basis_fallback = UNIT_COST_DIRECTIONAL      # 兼容旧行为（非北单狗）
        used_bases: set[float] = set()
        sps_w = []
        for h, w in zip(recent, weights):
            sp = h.get("sp")
            if sp is None:
                b = h.get("unit_cost")
                if b is not None:
                    try:
                        b = float(b)
                    except (TypeError, ValueError):
                        b = None
                if b is None:
                    b = basis_fallback
                if b:
                    used_bases.add(float(b))
                    sp = sp_from_return_ratio(h.get("return_ratio"), b, h.get("hit"))
                # 口径不明（只可能是 beidan 因子）→ 该样本不计入，宁缺勿错
            if sp is not None:
                sps_w.append((float(sp), w))
        sp_samples = len(sps_w)
        # 汇报"实际用到"的口径：一致才给数值，混用/不明 → None（便于排查）
        sp_basis = used_bases.pop() if len(used_bases) == 1 else None
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
        "sp_basis": sp_basis,
        "sp_samples": sp_samples,
        "vol_precision": vol_precision,
        "vol_base": vol_base,
        "vol_edge": vol_edge,
        "vol_n": vol_n,
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
def _axis_profile(stats: dict, now: datetime) -> dict | None:
    """两轴因子专用画像：只累计【分析日之前】的按日样本。"""
    cutoff = now.date().isoformat()
    samples = [s for s in (stats.get("axis_samples") or [])
               if str(s.get("date") or "")[:10] < cutoff]
    if not samples:
        return None
    axis = stats.get("type") or "directional"
    last_age = None
    try:
        last = max(str(s.get("date") or "")[:10] for s in samples)
        last_age = (now.date() - datetime.strptime(last, "%Y-%m-%d").date()).days
    except Exception:                                        # noqa: BLE001
        pass
    base = {"n": 0, "hits": 0.0, "w_return": 0.0, "rank_score": 0.0,
            "factor_type": axis, "avg_sp": None, "sp_basis": None, "sp_samples": 0,
            "vol_precision": None, "vol_base": None, "vol_edge": None, "vol_n": 0,
            "decided": 0.0, "strong_large": False, "full_hit_rate": 0.0,
            "volatility": 0.0, "shrunk_rate": 0.5,
            "dormant": bool(last_age is not None and last_age > FACTOR_DORMANT_DAYS),
            "interval_days": None, "last_age_days": last_age,
            "first_seen_recent": "", "last_seen_recent": "",
            "axis_kind": axis, "axis_samples_n": len(samples)}

    if axis == "volatility":
        ratios = [float(s.get("ratio_med") or 0) for s in samples]
        bases = [float(s.get("base_med") or 0) for s in samples]
        delta = sum(r - b for r, b in zip(ratios, bases)) / len(samples)
        ratio = sum(ratios) / len(ratios)
        base.update({
            "n": len(samples),
            "avg_sp": ratio,                     # 供 selected_active 波动列表排序
            "axis_ratio": ratio,
            "axis_base": sum(bases) / len(bases),
            "axis_delta": delta,
            # 波动轴按「兑现 − 基线」排序；×10 放进与方向轴同量纲的分数
            "rank_score": delta * 10.0, "w_return": delta,
            "shrunk_rate": 0.5,
        })
        return base

    n = sum(int(s.get("n") or 0) for s in samples)
    if n <= 0:
        return None
    sum_p = sum(float(s.get("sum_p") or 0.0) for s in samples)
    sum_hit = sum(float(s.get("sum_hit") or 0.0) for s in samples)
    edge = (sum_hit - sum_p) / n                  # = 命中率 − 市场 p̂（比值形式）
    base.update({
        "n": n, "hits": sum_hit,
        "w_return": edge,                          # 展示口径：方向边际
        "rank_score": edge * 10.0,                 # d=+5pp → +0.50
        "decided": float(n),
        "shrunk_rate": (sum_hit + 2.0) / (n + 2.0),
        "axis_edge_pp": edge * 100.0,
        "axis_sum_p": sum_p,
    })
    return base

