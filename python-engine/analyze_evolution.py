#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进化曲线分析：在 eval_log_score 的逐笔 Δ 基础上做两层分析。

1. 持续上升段扫描：
   - 正窗口连跑：滚动 7 天窗口 ΣΔ > 0 连续 >= min_run 天
   - 最大累计回升（drawup）：实盘期内累计 Δ 从阶段低点到之后高点的最大涨幅，
     附日期区间

2. 因子事件对齐：
   - 因子评审事件：解析 data/sessions/<agent>/*_factor_review_*.md，
     取评审窗口结束日 D（实盘期内），比较 [D-6, D] 与 [D+1, D+7] 的平均 Δ
   - 新增因子事件：memory/factor_memory.json 中 first_seen >= live_start 的因子，
     按 first_seen 日期同样做前后 7 天对比
   - 每个 agent 汇总：平均改善、改善占比、配对 t 检验

用法：
  python3 analyze_evolution.py                      # 全部 7 个实盘 agent
  python3 analyze_evolution.py --agents 梭哈2狗,跟风狗
  python3 analyze_evolution.py --w-mode day-stake
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from eval_log_score import REPORTS_DIR, ROLES_DIR, evaluate, t_one_sided_p


SESSIONS_DIR = Path(__file__).parent / "data" / "sessions"
AGENTS = ["梭哈2狗", "梭哈3狗", "alpha2狗", "alpha狗", "均注狗", "平局狗", "跟风狗"]


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _d(s: str) -> datetime.date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# ─────────────────────────────────────────────
# 因子事件
# ─────────────────────────────────────────────

def review_events(agent: str, live_start: str) -> list[dict]:
    """解析 factor_review 会话文件，返回实盘期内的事件（同日合并）。"""
    sess = SESSIONS_DIR / agent
    events = []
    if not sess.exists():
        return events
    for fp in sorted(sess.glob("*factor_review_*.md")):
        text = fp.read_text(encoding="utf-8")
        m = re.search(r"\| 日期 \|\s*(\d{4}-\d{2}-\d{2})", text)
        if not m:
            m = re.search(r"factor_review_(\d{4}-\d{2}-\d{2})\.md", fp.name)
        if not m:
            continue
        dstr = m.group(1)
        if dstr < live_start:
            continue
        def _names(key: str) -> list[str]:
            mm = re.search(rf'"{key}":\s*(\[[^\]]*\])', text)
            return re.findall(r'"([^"]+)"', mm.group(1)) if mm else []
        events.append({
            "date": dstr,
            "retired": _names("retired"),
            "dormant": _names("dormant"),
            "file": fp.name,
        })
    events.sort(key=lambda e: e["date"])
    merged: dict[str, dict] = {}
    for e in events:
        key = e["date"]
        if key not in merged:
            merged[key] = {"date": key, "retired": [], "dormant": [],
                           "files": []}
        m = merged[key]
        m["retired"] = sorted(set(m["retired"]) | set(e["retired"]))
        m["dormant"] = sorted(set(m["dormant"]) | set(e["dormant"]))
        m["files"].append(e["file"])
    return list(merged.values())


def new_factors_by_date(agent: str, live_start: str) -> dict[str, int]:
    """first_seen >= live_start 的因子按日计数。"""
    mem = ROLES_DIR / agent / "memory" / "factor_memory.json"
    data = _load_json(mem)
    if not data:
        return {}
    by_date: dict[str, int] = collections.defaultdict(int)
    for v in (data.get("factor_perf") or {}).values():
        fs = (v.get("first_seen") or "")[:10]
        if fs and fs >= live_start:
            by_date[fs] += 1
    return dict(sorted(by_date.items()))


# ─────────────────────────────────────────────
# Δ 日序列与上升段
# ─────────────────────────────────────────────

def daily_series(orders: list[dict]) -> list[dict]:
    by_day: dict[str, list[float]] = collections.defaultdict(list)
    for r in orders:
        by_day[r["date"]].append(r["delta"])
    out = []
    cum = 0.0
    for d in sorted(by_day):
        vals = by_day[d]
        s = sum(vals)
        cum += s
        out.append({"date": d, "n": len(vals), "sum_delta": s,
                    "mean_delta": s / len(vals), "cum_delta": cum})
    return out


def positive_runs(rolling: list[dict], min_run: int) -> list[dict]:
    """滚动 7 天窗口 ΣΔ > 0 的连续段。"""
    runs, cur = [], []
    for r in rolling:
        if r["n"] >= 3 and r["sum_delta"] > 0:
            cur.append(r)
        else:
            if len(cur) >= min_run:
                runs.append(cur)
            cur = []
    if len(cur) >= min_run:
        runs.append(cur)
    return [{
        "start": r[0]["end"],
        "end": r[-1]["end"],
        "windows": len(r),
        "max_rolling_sum": max(x["sum_delta"] for x in r),
    } for r in runs]


def max_drawup(daily: list[dict]) -> dict | None:
    """累计 Δ 的最大回升：阶段低点后的最大涨幅及区间。"""
    if not daily:
        return None
    cum = [x["cum_delta"] for x in daily]
    lo_i, lo_v, best = 0, cum[0], 0.0
    best_seg = None
    for i in range(1, len(cum)):
        if cum[i] < lo_v:
            lo_i, lo_v = i, cum[i]
        if cum[i] - lo_v > best:
            best = cum[i] - lo_v
            best_seg = {"start": daily[lo_i]["date"], "end": daily[i]["date"],
                        "rise": best, "low": lo_v, "high": cum[i]}
    return best_seg


# ─────────────────────────────────────────────
# 事件对齐
# ─────────────────────────────────────────────

def _window_mean(daily_map: dict[str, float], center: datetime.date,
                 lo: int, hi: int) -> float | None:
    vals = []
    for off in range(lo, hi + 1):
        v = daily_map.get((center + timedelta(days=off)).isoformat())
        if v is not None:
            vals.append(v)
    if len(vals) < 3:
        return None
    return sum(vals) / len(vals)


def align_events(daily: list[dict], events: list[dict], window: int = 7) -> list[dict]:
    """每个事件日期前后 window 天平均 Δ 的对比。"""
    daily_map = {x["date"]: x["mean_delta"] for x in daily}
    aligned = []
    for ev in events:
        d = _d(ev["date"])
        before = _window_mean(daily_map, d, -(window - 1), 0)
        after = _window_mean(daily_map, d, 1, window)
        if before is None or after is None:
            continue
        aligned.append({**ev, "before": before, "after": after,
                        "diff": after - before})
    return aligned


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "avg_before": 0.0, "avg_after": 0.0, "avg_diff": 0.0,
                "pos": 0, "t": float("nan"), "p_improve": float("nan")}
    n = len(rows)
    avg_b = sum(r["before"] for r in rows) / n
    avg_a = sum(r["after"] for r in rows) / n
    diffs = [r["diff"] for r in rows]
    avg_diff = sum(diffs) / n
    pos = sum(1 for x in diffs if x > 0)
    sd = math.sqrt(sum((x - avg_diff) ** 2 for x in diffs) / (n - 1)) if n > 1 else 0.0
    t = avg_diff / (sd / math.sqrt(n)) if sd > 0 else (float("inf") if avg_diff > 0 else float("-inf"))
    p_improve = t_one_sided_p(t, n - 1)[0]
    return {"n": n, "avg_before": avg_b, "avg_after": avg_a,
            "avg_diff": avg_diff, "pos": pos, "t": t, "p_improve": p_improve}


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def analyze(agent: str, w_mode: str, live_start: str, min_run: int) -> dict:
    res = evaluate(agent, 7, "exact-kelly", "direction", w_mode,
                   live_start, live_only=True)
    daily = daily_series(res["orders"])
    runs = positive_runs(res["rolling"], min_run)
    for r in runs:
        r["cum_gain"] = sum(x["sum_delta"] for x in daily
                            if r["start"] <= x["date"] <= r["end"])
    drawup = max_drawup(daily)

    rev_events = review_events(agent, live_start)
    rev_aligned = align_events(daily, rev_events)
    rev_agg = aggregate(rev_aligned)

    new_by_date = new_factors_by_date(agent, live_start)
    new_events = [{"date": d, "retired": [], "dormant": [],
                   "new_count": c} for d, c in new_by_date.items()]
    new_aligned = align_events(daily, new_events)
    new_agg = aggregate(new_aligned)

    return {
        "agent": agent,
        "w_mode": w_mode,
        "live_start": live_start,
        "overall": res["overall"],
        "trend": res["trend"],
        "daily": daily,
        "positive_runs": runs,
        "max_drawup": drawup,
        "review_events": rev_aligned,
        "review_agg": rev_agg,
        "new_factor_days": new_events,
        "new_factor_agg": new_agg,
    }


def _fmt_p(x: float) -> str:
    if x != x:
        return "-"
    return f"{x:.3f}"


def print_report(a: dict) -> None:
    s = a["overall"]
    print(f"═══ {a['agent']}（{a['w_mode']} / exact-kelly / 实盘期）═══")
    print(f"实盘 {s['n']} 笔 | 命中 {s['wins']} ({s['wins']/s['n']*100:.1f}%) | "
          f"ΣΔ {s['sum_delta']:+.2f} | 平均Δ {s['mean_delta']:+.4f} | t {s['t']:.2f}")

    print("─ 持续上升段（滚动7天ΣΔ>0连续≥7天）")
    if not a["positive_runs"]:
        print("  无：实盘期内没有出现连续 7 天滚动窗口全为正的段")
    for r in a["positive_runs"]:
        print(f"  {r['start']}~{r['end']} 持续{r['windows']}天 "
              f"段内累计Δ {r['cum_gain']:+.2f}（滚动窗口峰值 {r['max_rolling_sum']:+.2f}）")
    du = a["max_drawup"]
    if du:
        print(f"  最大累计回升: {du['start']}~{du['end']} 累计Δ "
              f"{du['low']:+.2f}→{du['high']:+.2f}（+{du['rise']:.2f}）")

    print("─ 因子评审事件（退役/休眠 后 7 天 vs 前 7 天）")
    if not a["review_events"]:
        print("  实盘期无可对齐的评审事件")
    else:
        print(f"  {'日期':<12}{'退役':>4}{'休眠':>5}{'前7d':>9}{'后7d':>9}{'改善':>9}")
        for e in a["review_events"]:
            print(f"  {e['date']:<12}{len(e['retired']):>4}{len(e['dormant']):>5}"
                  f"{e['before']:>9.3f}{e['after']:>9.3f}{e['diff']:>+9.3f}")
        ag = a["review_agg"]
        print(f"  汇总: n={ag['n']} 平均 {ag['avg_before']:+.3f}→{ag['avg_after']:+.3f} "
              f"改善 {ag['avg_diff']:+.3f} | 正向 {ag['pos']}/{ag['n']} | "
              f"t {ag['t']:.2f} p(改善) {_fmt_p(ag['p_improve'])}")

    print("─ 新增因子日（first_seen，后 7 天 vs 前 7 天）")
    if not a["new_factor_agg"]["n"]:
        print("  实盘期无可对齐的新增因子日")
    else:
        ag = a["new_factor_agg"]
        top = sorted(a["new_factor_days"], key=lambda x: -x.get("new_count", 0))[:5]
        print(f"  实盘期新增因子日 {len(a['new_factor_days'])} 天，可对齐 {ag['n']} 天；"
              f"平均 {ag['avg_before']:+.3f}→{ag['avg_after']:+.3f} "
              f"改善 {ag['avg_diff']:+.3f} | 正向 {ag['pos']}/{ag['n']} | "
              f"t {ag['t']:.2f} p(改善) {_fmt_p(ag['p_improve'])}")
        if top:
            print("  新增最多: " + ", ".join(
                f"{x['date']}(+{x['new_count']})" for x in top))
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="进化曲线分析：上升段 + 因子事件对齐")
    ap.add_argument("--agents", default=",".join(AGENTS),
                    help="逗号分隔的 agent 列表（默认全部实盘 agent，不含串关2狗）")
    ap.add_argument("--w-mode", choices=["bankroll", "day-stake"], default="bankroll")
    ap.add_argument("--live-start", default="2026-07-11")
    ap.add_argument("--min-run", type=int, default=7, help="正窗口连跑最少天数")
    args = ap.parse_args(argv)

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    results = []
    for a in agents:
        try:
            r = analyze(a, args.w_mode, args.live_start, args.min_run)
        except FileNotFoundError as e:
            print(f"跳过 {a}: {e}")
            continue
        results.append(r)
        print_report(r)

    out_dir = Path(__file__).parent / "data" / "reports"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    latest = out_dir / f"evolution_analysis_{args.w_mode}.json"
    timed = out_dir / f"evolution_analysis_{args.w_mode}_{ts}.json"
    payload = json.dumps(results, ensure_ascii=False, indent=2)
    latest.write_text(payload, encoding="utf-8")
    timed.write_text(payload, encoding="utf-8")
    print(f"已保存: {latest}\n已保存: {timed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
