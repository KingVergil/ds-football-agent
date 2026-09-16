#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**只比排序**的 A/B（0 LLM，同一批候选）。

从 `leg_decision_<day>_<hhmmss>.json` 取引擎当天 stage1 的真实候选（含 rank / 推荐），
用两套排序各取前 N 条腿，比较命中率与票级回收：

  臂 A = stage1 的 rank 序（现状）
  臂 B = 两轴序（方向分 → 兑现分 → x）

过滤在两边完全一致（LLM veto / 池门 / 本日已下单），所以差异**只来自排序**。

用法
    python3 -m scripts.axis_rank_ab --sandbox /private/tmp/axis_sb2/95狗 \
        --factors data/factor_mine_out/final_w30.json \
        --days 2026-08-07,2026-08-08,2026-08-09
"""
from __future__ import annotations

import argparse
import itertools
import math
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_cond_audit import build_eval_env, eval_cond   # noqa: E402
from scripts.axis_two_stage_backtest import load_legs          # noqa: E402
from src.beidan_axis import LEG_LINE4, features_of, odds_span  # noqa: E402


def combo_stats(legs: list[dict], need: int = 4) -> tuple[float, int, int]:
    """逐注（C(n,4) 个 4 串 1 组合）统计。

    返回 (回收金额, 总注数, 中奖注数)。
    注级命中率 = 中奖注数 / 总注数；ROI 按「回报 / 投入」算（投入 = 注数 × 2 元）。
    """
    pay = 0.0
    combos = 0
    wins = 0
    for combo in itertools.combinations(legs, need):
        combos += 1
        if all(l["hit"] for l in combo):
            prod = 1.0
            for l in combo:
                prod *= float(l["sp"] or 0)
            pay += 2 * 0.65 * prod
            wins += 1
    return pay, combos, wins


def payout(legs: list[dict], need: int = 4) -> float:
    return combo_stats(legs, need)[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", required=True)
    ap.add_argument("--factors", default="data/factor_mine_out/final_w30.json")
    ap.add_argument("--days", required=True)
    ap.add_argument("--top", type=int, default=9)
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)

    factors = json.loads(Path(args.factors).read_text(encoding="utf-8"))
    # 因子权重 = 账本（只吃挖掘窗口，与分析的 as_of 口径一致）
    mem = json.loads((Path(args.sandbox) / "memory" / "factor_memory.json")
                     .read_text(encoding="utf-8"))
    W = {}
    for f in factors:
        st = (mem.get("factor_perf") or {}).get(f["name"]) or {}
        s = st.get("axis_samples") or []
        if f["axis"] == "volatility":
            dl = [float(x.get("ratio_med") or 0) - float(x.get("base_med") or 0) for x in s]
            w = sum(dl) / len(dl) if dl else 0.0
        else:
            n = sum(int(x.get("n") or 0) for x in s)
            w = ((sum(float(x.get("sum_hit") or 0) for x in s)
                  - sum(float(x.get("sum_p") or 0) for x in s)) / n) if n else 0.0
        W[f["name"]] = {"axis": f["axis"], "w": w, "cond": f["cond"]}

    pool = {(l["day"], l["lota_id"], l["side"]): l for l in load_legs("latest")}
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    L, A = [], lambda s: L.append(s)
    A("| 天/波 | 候选 | 臂A(stage1序) 命中 | 臂B(两轴序) 命中 | 两臂选腿重合 | A 回收 | B 回收 |")
    A("|---|---|---|---|---|---|---|")
    tot = {"A": [0, 0, 0.0], "B": [0, 0, 0.0]}
    agg = {k: {"cost": 0.0, "pay": 0.0, "tickets": 0, "wins": 0, "curve": [], "cap0": 50000.0,
               "combos": 0, "win_combos": 0}
           for k in ("A", "B")}
    for day in days:
        for f in sorted((Path(args.sandbox) / "memory").glob(f"leg_decision_{day}_*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            cands = d.get("candidates") or []
            drop = " | ".join(d.get("dropped") or [])
            legs = []
            for c in cands:
                lid = c.get("lota_id")
                if f"{lid}: stage1 veto" in drop or f"{lid}: 本日已下单" in drop:
                    continue
                # 该场过门侧（与引擎一致：x ≥ θ 的侧）
                sides = [pool[(day, lid, s)] for s in ("H", "D", "A")
                         if (day, lid, s) in pool and pool[(day, lid, s)]["x"] >= LEG_LINE4]
                if not sides:
                    continue
                # 引擎会买的那一侧 = 该场 x 最大且过线的一侧
                legs.append({"lota_id": lid, "rec": max(sides, key=lambda r: r["x"]),
                             "rank": c.get("rank"), "input_idx": c.get("input_idx")})
            if not legs:
                continue
            daylegs = [v for (dd, _l, _s), v in pool.items() if dd == day]
            env = build_eval_env(daylegs)
            for l in legs:
                l["f"] = [n for n, meta in W.items()
                          if meta["cond"] and _safe(eval_cond, meta["cond"], l["rec"], env)]
            def dirv(l):
                return sum(float(W[n]["w"]) for n in l["f"] if W[n]["axis"] == "directional")
            def volv(l):
                return sum(float(W[n]["w"]) for n in l["f"] if W[n]["axis"] == "volatility")
            armA = sorted(legs, key=lambda l: (l["rank"] is None, l["rank"] or 0,
                                               l["input_idx"] or 0))[:args.top]
            armB = sorted(legs, key=lambda l: (-dirv(l), -volv(l), -l["rec"]["x"]))[:args.top]
            pa = [l["rec"] for l in armA]
            pb = [l["rec"] for l in armB]
            ha = sum(1 for r in pa if r["hit"])
            hb = sum(1 for r in pb if r["hit"])
            va, ca, wa = combo_stats(pa)
            vb, cb, wb = combo_stats(pb)
            A(f"| {day[5:]} {f.stem[-6:]} | {len(legs)} | {ha}/{len(pa)} | {hb}/{len(pb)} | "
              f"{len(set(x['lota_id'] for x in armA) & set(x['lota_id'] for x in armB))} | "
              f"{va:.0f} | {vb:.0f} |")
            tot["A"][0] += len(pa); tot["A"][1] += ha; tot["A"][2] += va
            tot["B"][0] += len(pb); tot["B"][1] += hb; tot["B"][2] += vb
            for k, legs_, h, cw, wc in (("A", pa, ha, ca, wa), ("B", pb, hb, cb, wb)):
                c = math.comb(len(legs_), 4) * 2
                pp = payout(legs_)
                g = agg[k]
                g["cost"] += c; g["pay"] += pp; g["tickets"] += 1
                g["wins"] += 1 if h >= 4 else 0
                g["combos"] += cw; g["win_combos"] += wc
                g["cap0"] += pp - c
                g["curve"].append(g["cap0"])
    A("")
    A("| 臂 | 腿数 | 腿命中率 | 票数 | 中票 | **票命中率** | **注数** | **中奖注** | **注级命中率** | 投入 | 回报 | **ROI(回报/投入)** | **最大回撤** | 期末资金 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k, label in (("A", "A stage1 序（现状）"), ("B", "B 两轴序")):
        n, h, v = tot[k]
        g = agg[k]
        peak, mdd = 50000.0, 0.0
        for c in g["curve"]:
            peak = max(peak, c)
            mdd = max(mdd, peak - c)
        ror = (g["pay"] / g["cost"]) if g["cost"] else 0.0
        A(f"| {label} | {n} | {h / n * 100 if n else 0:.1f}% | {g['tickets']} | {g['wins']} | "
          f"**{g['wins'] / g['tickets'] * 100 if g['tickets'] else 0:.1f}%** | "
          f"{g['combos']} | {g['win_combos']} | "
          f"**{g['win_combos'] / g['combos'] * 100 if g['combos'] else 0:.2f}%** | "
          f"{g['cost']:.0f} | {g['pay']:.0f} | **{ror:.3f}（{ror - 1:+.1%}）** | "
          f"**{mdd:.0f} 元（{mdd / peak * 100:.1f}%）** | {g['cap0']:.0f} |")
    A("")
    A("| 臂 | 腿数 | 腿命中率 | 票数 | 中票 | **票命中率** | 成本 | 回收 | **ROI(净)** | **最大回撤** | 期末资金 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")
    for k, label in (("A", "A stage1 序（现状）"), ("B", "B 两轴序")):
        n, h, v = tot[k]
        g = agg[k]
        peak, mdd = 50000.0, 0.0
        for c in g["curve"]:
            peak = max(peak, c)
            mdd = max(mdd, peak - c)
        roi = (g["pay"] / g["cost"] - 1) if g["cost"] else 0.0
        A(f"| {label} | {n} | {h / n * 100 if n else 0:.1f}% | {g['tickets']} | {g['wins']} | "
          f"**{g['wins'] / g['tickets'] * 100 if g['tickets'] else 0:.1f}%** | {g['cost']:.0f} | "
          f"{g['pay']:.0f} | **{roi:+.1%}** | **{mdd:.0f} 元（{mdd / peak * 100:.1f}%）** | {g['cap0']:.0f} |")
    out = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text(out, encoding="utf-8")
        print(f"已写 {args.md}")
    print(out)
    return 0


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:                                       # noqa: BLE001
        return False


if __name__ == "__main__":
    raise SystemExit(main())
