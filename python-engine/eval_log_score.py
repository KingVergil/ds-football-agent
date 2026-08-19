#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评估 Agent 是否在「进化」——相对市场的对数得分（log score）框架。

核心思想（用户方案）：
  1. 从仓位 w 恢复到 Agent 的主观概率 q：
       - 默认 exact-kelly（凯利满注反推）：q = p + w * (1 - p)，
         由 f = (q·o-1)/(o-1) 代入公平赔率 o=1/p 化简而来，保证 q ∈ [p, 1)
       - 可选 user 近似变体：q = p + w
       - 跳过/走水的事件 q = p，Δ = 0
  2. w 的两种口径：
       - bankroll（默认）：w = 下注额 / 下注时资金余额（凯利仓位）
       - day-stake：当日全部下注合计视为 1，w = 单笔下注额 / 当日总下注额
         （衡量当天内部对每场比赛的「信心分配」，总和恒为 1）
  2. 单次二元事件的对数得分增益：
       Δ = o·log(q/p) + (1-o)·log((1-q)/(1-p))
  3. 按 7 天（可配置）滚动/分桶累计，用单边 t 检验判断是否显著优于市场。

市场概率 p：取 Pinnacle 终盘双方（或三向）赔率的去水隐含概率，即
  p_side = (1/(1+odds_side)) / Σ(1/(1+odds_i))

结算口径：亚盘/大小球有走水与赢半/输半。
  - 走水（return == bet_size）按跳过处理（Δ=0）
  - 默认 direction 模式：赢半按命中(o=1)、输半按未中(o=0) 计入
  - strict 模式：只有全赢/全输计入，半赢半输跳过

事件口径：每笔订单按其下注方向计为 1 个二元事件（市场概率 p = 所选方向概率）。
若严格按用户描述的 s_yes/s_no 成对计，总分恰好 ×2，符号与相对比较不变。

资金口径：从 role.json 的最终 capital 反向回放全部订单事件，得到每次下注时的
准确资金余额；w = bet_size / 下注时资金。w >= 1（下注超过全部资金）时凯利
反推不成立，默认剔除并单独计数。

日期口径：所有日期按「足球日」归集——足球日 D = [D 12:01, D+1 12:00]，
由比赛开赛时间（matches 缓存，缺则 compact-fet 文本）映射；既用于 day-stake
的当日总下注额，也用于 7 天分桶/滚动窗口。

阶段口径：足球日 < live-start 的订单视为「回填期」（结果已知后补录/拟合），
>= live-start 为「实盘期」。默认 live-start = 2026-07-11（梭哈2狗实际开始
实盘运行的足球日），可用 --live-start 覆盖；两期分开统计以识别过拟合信号。
--live-only 时只保留实盘期订单，并额外输出「前半段 vs 后半段」的改善检验，
用于直接回答实盘期内 agent 是否在进化。

记录功能：每次运行默认把快照追加到 data/reports/log_score_history.jsonl
（同一 agent+参数+同一数据截止日+相同 ΣΔ 的重复运行会覆盖当条，避免刷屏），
可用 --no-record 关闭、--note 加备注（如「改 prompt 后」）。
`python3 eval_log_score.py 梭哈2狗 --history` 查看该 agent 的历史轨迹。

用法示例：
  python3 eval_log_score.py 梭哈2狗
  python3 eval_log_score.py 梭哈2狗 --window 7 --q-mode user --outcome direction
  python3 eval_log_score.py 梭哈2狗 --w-mode day-stake
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

from src.data_manager import DataManager


PROJECT_ROOT = Path(__file__).parent
ROLES_DIR = PROJECT_ROOT / "data" / "roles"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
HISTORY_PATH = REPORTS_DIR / "log_score_history.jsonl"
EPS = 1e-9
MATCHES_DIR = PROJECT_ROOT / "data" / "matches"


def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _match_time_map() -> dict[str, str]:
    """lota_id → 开赛时间（来自 matches 足球日缓存）。"""
    m: dict[str, str] = {}
    if not MATCHES_DIR.exists():
        return m
    for fp in sorted(MATCHES_DIR.glob("*.json")):
        try:
            data = _load_json(fp)
        except Exception:
            continue
        for match in data:
            lid = match.get("lota_id")
            mt = match.get("match_time")
            if lid and mt:
                m.setdefault(lid, mt)
    return m


def _match_time_from_text(dm: DataManager, lota_id: str) -> str | None:
    """从 compact-fet 文本兜底提取开赛时间。"""
    import re
    text = dm.get_compact_fet_text(lota_id) or ""
    mo = re.search(r"时间:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)", text)
    return mo.group(1) if mo else None


def _football_day(match_time: str) -> str | None:
    """开赛时间 → 足球日（当日 12:01 → 次日 12:00，12:00 前算前一足球日）。"""
    try:
        t = datetime.strptime(match_time[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None
    d = t.date() if (t.hour, t.minute) >= (12, 1) else t.date() - timedelta(days=1)
    return d.isoformat()


# ─────────────────────────────────────────────
# Student-t 单边 p 值（不依赖 scipy）
# ─────────────────────────────────────────────

def _betacf(a: float, b: float, x: float, max_iter: int = 200, eps: float = 3e-12) -> float:
    """Lentz 连分数法求正则化不完全贝塔函数的补余部分。"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """正则化不完全贝塔函数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lnbt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    bt = math.exp(lnbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_one_sided_p(t: float, df: int) -> tuple[float, float]:
    """返回 (P(T>=t), P(T<=t))，T ~ Student-t(df)。"""
    if df <= 0 or not math.isfinite(t):
        return float("nan"), float("nan")
    two = _betai(df / 2.0, 0.5, df / (df + t * t))
    upper = two / 2.0 if t >= 0 else 1.0 - two / 2.0
    return upper, 1.0 - upper


# ─────────────────────────────────────────────
# 数据准备
# ─────────────────────────────────────────────

def _reverse_replay_capital(orders: list[dict], final_capital: float) -> dict[int, float]:
    events = []
    for o in orders:
        events.append((datetime.fromisoformat(o["created_at"]), 0, "bet", o))
        if o.get("settled_at"):
            events.append((datetime.fromisoformat(o["settled_at"]), 1, "settle", o))
    events.sort(key=lambda e: (e[0], e[1]))

    cap = final_capital
    cap_before: dict[int, float] = {}
    for ts, _order, kind, o in reversed(events):
        if kind == "settle":
            cap -= float(o.get("return_amount") or 0.0)
        else:
            cap_before[id(o)] = cap + float(o.get("bet_size") or 0.0)
            cap = cap_before[id(o)]
    return cap_before


def _market_prob(order: dict, odds: dict) -> float | None:
    """Pinnacle 终盘去水隐含概率（所选方向的 p）。"""
    bt = order.get("bet_type", "")
    pick = order.get("pick", "")
    if bt == "亚盘":
        asian = odds.get("asian") or {}
        side = {"H": asian.get("h"), "A": asian.get("a")}
    elif bt == "大小球":
        ou = odds.get("ou") or {}
        side = {"over": ou.get("over"), "under": ou.get("under")}
    elif bt in ("胜平负", "让球胜平负"):
        eu = odds.get("eu") or {}
        side = {"H": eu.get("h"), "D": eu.get("d"), "A": eu.get("a")}
    else:
        return None

    if pick not in side or not side[pick]:
        return None
    implied = []
    for v in side.values():
        if not v or v <= 0:
            return None
        implied.append(1.0 / (1.0 + float(v)))
    return implied[list(side).index(pick)] / sum(implied)


def _classify_outcome(order: dict, strict: bool) -> tuple[int | None, str | None]:
    """返回 (o, exclude_reason)。o ∈ {0,1}；走水/半盘按配置处理。"""
    hit = order.get("hit")
    bet_size = float(order.get("bet_size") or 0.0)
    ret = float(order.get("return_amount") or 0.0)
    if hit is True:
        return 1, None
    if hit is False:
        return 0, None
    # hit is None：走水 或 半赢半输
    if abs(ret - bet_size) < 1e-6:
        return None, "走水"
    if strict:
        return None, "半盘"
    return (1 if ret > bet_size else 0), None


# ─────────────────────────────────────────────
# 统计
# ─────────────────────────────────────────────

def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "wins": 0, "sum_delta": 0.0, "mean_delta": 0.0,
                "sd": 0.0, "t": float("nan"), "p_upper": float("nan"),
                "p_lower": float("nan")}
    wins = sum(1 for r in rows if r["o"] == 1)
    total = sum(r["delta"] for r in rows)
    mean = total / n
    sd = math.sqrt(sum((r["delta"] - mean) ** 2 for r in rows) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else (float("inf") if mean > 0 else float("-inf"))
    p_upper, p_lower = t_one_sided_p(t, n - 1)
    return {"n": n, "wins": wins, "sum_delta": total, "mean_delta": mean,
            "sd": sd, "t": t, "p_upper": p_upper, "p_lower": p_lower}


def _verdict(st: dict) -> str:
    if st["n"] < 20:
        return "样本不足（n<20），暂不下结论"
    if st["mean_delta"] > 0 and st["p_upper"] < 0.05:
        return "进化：相对市场有显著正增益"
    if st["mean_delta"] > 0:
        return "有正增益但不显著（可能是波动）"
    if st["p_lower"] < 0.05:
        return "退化：显著负增益（独立判断有害 / 过度自信）"
    return "零附近：未检测到进化"


def _evolution_trend(rows: list[dict]) -> dict | None:
    """实盘期内前半段 vs 后半段的平均 Δ 改善检验（Welch 两样本 t）。"""
    if len(rows) < 8:
        return None
    mid = len(rows) // 2
    h1 = _stats(rows[:mid])
    h2 = _stats(rows[mid:])
    if h1["sd"] <= 0 and h2["sd"] <= 0:
        return None
    v1 = h1["sd"] ** 2 / h1["n"]
    v2 = h2["sd"] ** 2 / h2["n"]
    se = math.sqrt(v1 + v2) if v1 + v2 > 0 else 0.0
    t = (h2["mean_delta"] - h1["mean_delta"]) / se if se > 0 else 0.0
    df = (v1 + v2) ** 2 / (v1 ** 2 / (h1["n"] - 1) + v2 ** 2 / (h2["n"] - 1))
    df = max(1.0, df)
    p_improve = t_one_sided_p(t, int(round(df)))[0]
    return {"first": h1, "second": h2, "t_improve": t,
            "df": df, "p_improve": p_improve}


def _fmt(x: float, digits: int = 3) -> str:
    if x != x or math.isinf(x):  # NaN / ±inf
        return "-"
    return f"{x:.{digits}f}"


def _record_history(res: dict, note: str) -> None:
    """把本次评估快照追加进历史（同日同数据重跑则覆盖）。"""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    if HISTORY_PATH.exists():
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    o = res["overall"]
    snap = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agent": res["agent"],
        "note": note or "",
        "params": res["params"],
        "live_only": res.get("live_only", False),
        "live_start": res.get("live_start"),
        "data": {"first": res["data"]["first_order"],
                 "last": res["data"]["last_order"],
                 "orders_total": res["data"]["orders_total"]},
        "overall": {"n": o["n"], "wins": o["wins"],
                    "sum_delta": round(o["sum_delta"], 4),
                    "mean_delta": round(o["mean_delta"], 5),
                    "t": round(o["t"], 3),
                    "p_upper": round(o["p_upper"], 4),
                    "p_lower": round(o["p_lower"], 4)},
        "stages": {k: {"n": v["n"], "sum_delta": round(v["sum_delta"], 3)}
                   for k, v in res["stages"].items()},
        "verdict": res["verdict"],
    }
    for i, e in enumerate(entries):
        if (e.get("agent") == snap["agent"]
                and e.get("params") == snap["params"]
                and e.get("live_only") == snap["live_only"]
                and e.get("data", {}).get("last") == snap["data"]["last"]
                and abs(e.get("overall", {}).get("sum_delta", 1e9)
                        - snap["overall"]["sum_delta"]) < 1e-9):
            entries[i] = snap
            break
    else:
        entries.append(snap)
    HISTORY_PATH.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8")


def print_history(agent: str) -> None:
    if not HISTORY_PATH.exists():
        print("暂无历史记录")
        return
    entries = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("agent") == agent:
            entries.append(e)
    if not entries:
        print(f"暂无 {agent} 的历史记录")
        return
    print(f"═══ {agent} 评估历史（{len(entries)} 条）═══")
    print(f"{'#':>2} {'时间':<20}{'模式':<34}{'截止':<12}{'n':>5}{'ΣΔ':>9}"
          f"{'均值Δ':>9}{'t':>7}{'p(Δ>0)':>8}  判定")
    for i, e in enumerate(entries, 1):
        o = e["overall"]
        p = e["params"]
        mode = (f"{p['w_mode']}/{p['q_mode']}"
                + ("/实盘" if e.get("live_only") else "")
                + (f" 注:{e.get('note','')[:10]}" if e.get("note") else ""))
        print(f"{i:>2} {e['ts']:<20}{mode:<34}{e['data']['last']:<12}{o['n']:>5}"
              f"{o['sum_delta']:>9.2f}{o['mean_delta']:>9.4f}{o['t']:>7.2f}"
              f"{o['p_upper']:>8.3f}  {e['verdict']}")
    if len(entries) >= 2:
        a, b = entries[-2], entries[-1]
        print(f"\n最新 vs 上一条: ΣΔ {a['overall']['sum_delta']:+.3f} → "
              f"{b['overall']['sum_delta']:+.3f}（Δ {b['overall']['sum_delta'] - a['overall']['sum_delta']:+.3f}）"
              f" | 判定: {a['verdict']} → {b['verdict']}")


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def evaluate(agent: str, window: int, q_mode: str, outcome_mode: str, w_mode: str,
             live_start: str, live_only: bool = False) -> dict:
    role_path = ROLES_DIR / agent / f"{agent}.json"
    if not role_path.exists():
        raise FileNotFoundError(f"找不到角色文件: {role_path}")
    role = _load_json(role_path)
    orders = role.get("orders") or []
    dm = DataManager()
    mt_map = _match_time_map()

    cap_before = _reverse_replay_capital(orders, float(role.get("capital") or 0.0))
    strict = outcome_mode == "strict"
    excluded = collections.Counter()
    rows = []
    live = datetime.strptime(live_start, "%Y-%m-%d").date()

    # 足球日（由开赛时间映射；缺失时退回下单日）与 day-stake 口径的当日总下注额
    order_day: dict[int, str] = {}
    order_mt: dict[int, str] = {}
    day_total: dict[str, float] = collections.defaultdict(float)
    for o in orders:
        lid = o.get("lota_id", "")
        mt = mt_map.get(lid) or _match_time_from_text(dm, lid)
        order_mt[id(o)] = mt or ""
        fd = _football_day(mt) if mt else None
        day = fd or o.get("created_at", "")[:10]
        if fd is None:
            excluded["开赛时间缺失（按下单日归集）"] += 1
        order_day[id(o)] = day
        day_total[day] += float(o.get("bet_size") or 0.0)

    for o in sorted(orders, key=lambda x: x["created_at"]):
        odds = dm.get_odds(o.get("lota_id", "")) or {}
        p = _market_prob(o, odds)
        if p is None:
            excluded["无赔率数据"] += 1
            continue
        if not o.get("settled_at"):
            excluded["未结算"] += 1
            continue
        cb = cap_before.get(id(o), 0.0)
        bet_size = float(o.get("bet_size") or 0.0)
        if w_mode == "day-stake":
            day = order_day[id(o)]
            dt = day_total.get(day, 0.0)
            w = bet_size / dt if dt > 0 else float("inf")
        else:
            w = bet_size / cb if cb > 0 else float("inf")
        if w_mode == "bankroll" and (cb <= 0 or w >= 1.0):
            excluded["仓位 w>=1（凯利反推失效）"] += 1
            continue
        if w_mode == "day-stake" and w >= 1.0:
            excluded["当日仅 1 注（w=1）"] += 1
            continue
        out, ex = _classify_outcome(o, strict)
        if ex:
            excluded[ex] += 1
            continue
        if q_mode == "exact-kelly":
            q = p + w * (1.0 - p)
        else:
            q = p + w
        q = max(EPS, min(q, 1.0 - EPS))
        pp = max(EPS, min(p, 1.0 - EPS))
        delta = out * math.log(q / pp) + (1 - out) * math.log((1 - q) / (1 - pp))
        rows.append({
            "date": order_day[id(o)],
            "stage": "回填期" if datetime.strptime(order_day[id(o)], "%Y-%m-%d").date() < live
                     else "实盘期",
            "datetime": o["created_at"],
            "match_time": order_mt[id(o)],
            "lota_id": o.get("lota_id", ""),
            "bet_type": o.get("bet_type", ""),
            "pick": o.get("pick", ""),
            "handicap": o.get("handicap"),
            "odds": o.get("odds"),
            "bet_size": bet_size,
            "capital_before": round(cb, 2),
            "w": w,
            "p": p,
            "q": q,
            "o": out,
            "delta": delta,
        })

    rows.sort(key=lambda r: (r["date"], r["match_time"], r["datetime"]))
    if live_only:
        rows = [r for r in rows if r["stage"] == "实盘期"]
    st = _stats(rows)
    stage_stats = {
        stage: _stats([r for r in rows if r["stage"] == stage])
        for stage in ("回填期", "实盘期")
    }

    # 7 天分桶（从首个有效决策日开始）
    buckets = []
    if rows:
        start = datetime.strptime(rows[0]["date"], "%Y-%m-%d").date()
        by_bucket: dict[datetime.date, list[dict]] = collections.defaultdict(list)
        for r in rows:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            idx = (d - start).days // window
            by_bucket[start + timedelta(days=idx * window)].append(r)
        cum = 0.0
        for bstart in sorted(by_bucket):
            b = _stats(by_bucket[bstart])
            cum += b["sum_delta"]
            buckets.append({
                "start": bstart.isoformat(),
                "end": (bstart + timedelta(days=window - 1)).isoformat(),
                **b,
                "cum_delta": cum,
            })

    # 滚动 7 天（每天一步，只看有决策的日期）
    rolling = []
    if rows:
        dates = sorted({r["date"] for r in rows})
        for d in dates:
            end = datetime.strptime(d, "%Y-%m-%d").date()
            beg = end - timedelta(days=window - 1)
            win_rows = [r for r in rows if beg <= datetime.strptime(r["date"], "%Y-%m-%d").date() <= end]
            rolling.append({"end": d, "start": beg.isoformat(), **_stats(win_rows)})

    # 敏感性参考：另一种 q 反推口径下的总体统计
    alt_rows = []
    if q_mode == "user":
        for r in rows:
            alt = dict(r)
            alt["q"] = max(EPS, min(r["p"] + r["w"] * (1.0 - r["p"]), 1.0 - EPS))
            pp = max(EPS, min(r["p"], 1.0 - EPS))
            alt["delta"] = r["o"] * math.log(alt["q"] / pp) + (1 - r["o"]) * math.log((1 - alt["q"]) / (1 - pp))
            alt_rows.append(alt)
        alt_stats = _stats(alt_rows)
    else:
        for r in rows:
            alt = dict(r)
            alt["q"] = max(EPS, min(r["p"] + r["w"], 1.0 - EPS))
            pp = max(EPS, min(r["p"], 1.0 - EPS))
            alt["delta"] = r["o"] * math.log(alt["q"] / pp) + (1 - r["o"]) * math.log((1 - alt["q"]) / (1 - pp))
            alt_rows.append(alt)
        alt_stats = _stats(alt_rows)

    return {
        "agent": agent,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"window_days": window, "q_mode": q_mode,
                   "outcome_mode": outcome_mode, "w_mode": w_mode},
        "data": {
            "orders_total": len(orders),
            "excluded": dict(excluded),
            "first_order": rows[0]["date"] if rows else None,
            "last_order": rows[-1]["date"] if rows else None,
        },
        "overall": st,
        "stages": stage_stats,
        "live_only": live_only,
        "live_start": live_start,
        "trend": _evolution_trend(rows) if live_only else None,
        "q_mode_reference": {"name": "exact-kelly" if q_mode == "user" else "user",
                             **alt_stats},
        "buckets": buckets,
        "rolling": rolling,
        "orders": rows,
        "verdict": _verdict(st),
    }


def _print_report(res: dict) -> None:
    agent = res["agent"]
    p = res["params"]
    print(f"═══ {agent} 进化评估（相对市场对数得分）═══")
    print(f"参数: 窗口 {p['window_days']} 天 | w 口径 {p['w_mode']} | "
          f"q = {p['q_mode']} | 结算口径 {p['outcome_mode']} | 实盘起点 {res['live_start']}"
          + (" | 仅实盘期" if res["live_only"] else ""))
    print(f"数据范围（足球日）: {res['data']['first_order']} ~ {res['data']['last_order']} "
          f"（订单共 {res['data']['orders_total']} 笔）")
    ex = res["data"]["excluded"]
    if ex:
        print("剔除明细:", ", ".join(f"{k}: {v}" for k, v in ex.items()))
    print()

    st = res["overall"]
    print("── 总体 ──")
    if st["n"]:
        print(f"有效决策: {st['n']} 笔 | 方向命中: {st['wins']} ({st['wins'] / st['n'] * 100:.1f}%)")
    else:
        print("无有效决策")
    print(f"累计 Δ: {st['sum_delta']:+.3f} | 平均 Δ: {st['mean_delta']:+.4f} "
          f"| sd: {_fmt(st['sd'])} | t: {_fmt(st['t'], 2)}")
    print(f"单边 p (H1: Δ>0): {st['p_upper']:.4f} | 单边 p (H1: Δ<0): {st['p_lower']:.4f}")
    ref = res["q_mode_reference"]
    print(f"敏感性参考（{ref['name']} 口径）: 累计 Δ {ref['sum_delta']:+.3f} "
          f"| 平均 Δ {ref['mean_delta']:+.4f} | t {_fmt(ref['t'], 2)}")
    print(f"判定: {res['verdict']}")
    print()

    if not res["live_only"]:
        print("── 阶段对比（回填期 vs 实盘期）──")
        print(f"{'阶段':<8}{'n':>5}{'命中':>6}{'ΣΔ':>10}{'均值Δ':>10}{'t':>8}{'p(Δ<0)':>9}")
        for stage in ("回填期", "实盘期"):
            s = res["stages"][stage]
            if s["n"] == 0:
                print(f"{stage:<8}{0:>5}{'-':>6}{'-':>10}{'-':>10}{'-':>8}{'-':>9}")
            else:
                print(f"{stage:<8}{s['n']:>5}{s['wins']:>6}{s['sum_delta']:>10.2f}"
                      f"{s['mean_delta']:>10.3f}{_fmt(s['t'], 2):>8}{s['p_lower']:>9.3f}")
        b = res["stages"]["回填期"]
        lv = res["stages"]["实盘期"]
        if b["n"] >= 20 and lv["n"] >= 20:
            if b["mean_delta"] > 0 and lv["mean_delta"] <= 0:
                print("过拟合信号: 回填期正增益、实盘期转负/归零 —— 典型的拟合后失效模式")
            elif b["mean_delta"] > lv["mean_delta"] + 0.05:
                print(f"过拟合信号: 回填期平均Δ({b['mean_delta']:+.3f}) 明显高于实盘期"
                      f"({lv['mean_delta']:+.3f})")
            elif lv["mean_delta"] > b["mean_delta"] + 0.05:
                print("反向信号: 实盘期平均Δ 高于回填期（未见回填优势）")
            else:
                print("两期平均Δ 接近，未发现回填期明显优于实盘期")
        else:
            print("（回填/实盘样本不足 20 笔，暂不做过拟合对比）")
        print()

    tr = res.get("trend")
    if tr:
        f1, f2 = tr["first"], tr["second"]
        print("── 实盘期内进化趋势（前半段 vs 后半段）──")
        print(f"前半段: n={f1['n']} 平均Δ={f1['mean_delta']:+.4f} "
              f"| 后半段: n={f2['n']} 平均Δ={f2['mean_delta']:+.4f}")
        print(f"改善检验: t={tr['t_improve']:.2f} (df={tr['df']:.0f})，"
              f"p(后半>前半)={tr['p_improve']:.3f}")
        if tr["p_improve"] < 0.05:
            print("→ 后半段显著优于前半段：实盘期内可见进化")
        elif f2["mean_delta"] > f1["mean_delta"]:
            print("→ 后半段略好于前半段，但不显著（继续观察）")
        else:
            print("→ 后半段未改善甚至变差：未见进化迹象")
        print()

    if res["buckets"]:
        print(f"── 每 {p['window_days']} 天分桶 ──")
        print(f"{'窗口':<24}{'阶段':<6}{'n':>4}{'命中':>5}{'ΣΔ':>9}{'均值Δ':>9}{'t':>7}{'累计Δ':>9}")
        for b in res["buckets"]:
            stage = "回填" if b["start"] < res["live_start"] else "实盘"
            print(f"{b['start']}~{b['end']:<11}{stage:<6}{b['n']:>4}{b['wins']:>5}"
                  f"{b['sum_delta']:>9.2f}{b['mean_delta']:>9.3f}{_fmt(b['t'], 2):>7}"
                  f"{b['cum_delta']:>9.2f}")
        print()

    if res["rolling"]:
        print(f"── 滚动 {p['window_days']} 天窗口（每天一步）──")
        print(f"{'截止日期':<12}{'n':>4}{'ΣΔ':>9}{'均值Δ':>9}{'t':>7}{'p(Δ>0)':>8}")
        for r in res["rolling"]:
            if r["n"] < 3:
                print(f"{r['end']:<12}{r['n']:>4}{'-':>9}{'-':>9}{'-':>7}{'-':>8}")
            else:
                print(f"{r['end']:<12}{r['n']:>4}{r['sum_delta']:>9.2f}"
                      f"{r['mean_delta']:>9.3f}{_fmt(r['t'], 2):>7}{r['p_upper']:>8.3f}")


def _save_outputs(res: dict, window_csv: Path, orders_csv: Path, json_path: Path) -> None:
    window_csv.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for b in res["buckets"]:
        rows.append(f"{b['start']},{b['end']},{b['n']},{b['wins']},{b['sum_delta']:.4f},"
                    f"{b['mean_delta']:.4f},{b['t']:.3f},{b['p_upper']:.4f},{b['cum_delta']:.4f}")
    header = "窗口开始,窗口结束,n,命中,ΣΔ,均值Δ,t,单边p,累计Δ"
    window_csv.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    o_header = ("足球日,阶段,开赛时间,下单时间,lota_id,类型,选择,盘口,赔率,下注额,"
                "下注时资金,w(仓位),p(市场概率),q(主观概率),结果o,Δ")
    o_rows = []
    for r in res["orders"]:
        hc = r.get("handicap")
        o_rows.append(
            f"{r['date']},{r['stage']},{r['match_time']},{r['datetime']},{r['lota_id']},"
            f"{r['bet_type']},{r['pick']},{hc if hc is not None else ''},"
            f"{r['odds']},{r['bet_size']:.2f},"
            f"{r['capital_before']:.2f},{r['w']:.4f},{r['p']:.4f},{r['q']:.4f},"
            f"{r['o']},{r['delta']:.4f}"
        )
    orders_csv.write_text(o_header + "\n" + "\n".join(o_rows) + "\n", encoding="utf-8")

    json_path.write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent 进化评估（相对市场对数得分）")
    ap.add_argument("agent", nargs="?", default="梭哈2狗", help="角色名（默认 梭哈2狗）")
    ap.add_argument("--window", type=int, default=7, help="评估窗口天数（默认 7）")
    ap.add_argument("--q-mode", choices=["user", "exact-kelly"], default="exact-kelly",
                    help="主观概率反推口径：exact-kelly=q=p+w*(1-p)（默认，凯利满注反推）；"
                         "user=q=p+w（近似）")
    ap.add_argument("--outcome", choices=["direction", "strict"], default="direction",
                    help="direction=半赢半输按方向计入；strict=仅全赢全输")
    ap.add_argument("--w-mode", choices=["bankroll", "day-stake"], default="bankroll",
                    help="bankroll=下注额/下注时资金；day-stake=单笔/当日总下注额")
    ap.add_argument("--live-start", default="2026-07-11",
                    help="实盘起点足球日（默认 2026-07-11，之前为回填期）")
    ap.add_argument("--live-only", action="store_true",
                    help="只评估实盘期（>= live-start），并做前半段 vs 后半段改善检验")
    ap.add_argument("--history", action="store_true",
                    help="查看该 agent 的历史评估记录并退出")
    ap.add_argument("--note", default="", help="本次运行备注（写进历史，如「改prompt后」）")
    ap.add_argument("--no-record", action="store_true",
                    help="不写入历史记录")
    ap.add_argument("--csv", type=Path, default=None, help="窗口结果 CSV 输出路径")
    ap.add_argument("--json", type=Path, default=None, help="完整 JSON 输出路径")
    args = ap.parse_args(argv)

    if args.history:
        print_history(args.agent)
        return 0

    res = evaluate(args.agent, args.window, args.q_mode, args.outcome, args.w_mode,
                   args.live_start, live_only=args.live_only)
    _print_report(res)

    if not args.no_record:
        _record_history(res, args.note)
        print(f"已记录历史: {HISTORY_PATH}")

    suffix = "_live" if args.live_only else ""
    if args.csv is None:
        args.csv = REPORTS_DIR / f"log_score_windows_{args.agent}{suffix}.csv"
    orders_csv = args.csv.with_name(args.csv.name.replace("windows", "orders"))
    if args.json is None:
        args.json = REPORTS_DIR / f"log_score_{args.agent}{suffix}.json"
    _save_outputs(res, args.csv, orders_csv, args.json)
    print(f"\n已保存: {args.csv}\n已保存: {orders_csv}\n已保存: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
