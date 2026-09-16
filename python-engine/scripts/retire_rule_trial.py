#!/usr/bin/env python3
"""退役规则试算：**ROI 无正负倾向 + 14 天周期 + 样本量门槛**（确定性，0 LLM）。

用户口径（2026-09-13）
───────────────────
* 周期性启动：**14 天**一次（沿用现状，不改）；
* 退役对象 = **ROI 没有明显正负倾向**的因子（在 0 附近徘徊 = 没倾向 = 该退）；
* 同时必须满足**样本量门槛**，否则"无倾向"只是"还没证据"（实测中位样本仅 2）。

判定（自上而下，先到先判）
───────────────────────
    样本 < min_n            → 留（证据不足，不下结论）
    ROI < −eps              → 退（明确负倾向）
    |ROI| ≤ eps             → **退（无倾向）** ← 用户口径的核心
    ROI > +eps 且 样本≥min_n → 留（明确正倾向）
其中 ROI = Σprofit / Σunit_cost（观察虚拟结算与真实样本合并计权）。

用法
───
    python3 -m scripts.retire_rule_trial \
        --factor-memory /tmp/beidan_run0908/memory/factor_memory.json \
        --leg-pool data/leg_pool_full --window-days 14 --eps 0.20 --min-n 5
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

DEFAULT_EPS = 0.20      # 无倾向带：|ROI| ≤ 20% 视为没有明显倾向
DEFAULT_MIN_N = 5       # 样本门槛：低于此不下"无倾向"结论


def _load_scores(fm_path: str, pool_dir: str, theta: float) -> list[dict]:
    """合并「观察虚拟结算」与「真实样本」，算每因子在窗口内的 ROI 与样本量。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.score_observation_factors import score
    res = score(fm_path, pool_dir, theta)
    out = []
    for f in res["factors"]:
        n_obs, n_real = f["obs_scored"], f["real_n"]
        cost = n_obs * 1.0 + n_real * 1.0
        pnl = f["obs_pnl"] + f["real_pnl"]
        out.append({
            "name": f["name"], "type": f["type"], "status": f["status"],
            "n": n_obs + n_real, "n_obs": n_obs, "n_real": n_real,
            "cost": cost, "pnl": round(pnl, 3),
            "roi": (pnl / cost) if cost else None,
        })
    return out


def decide(rows: list[dict], eps: float, min_n: int) -> list[dict]:
    """按用户口径判定：先样本门槛，再负倾向/无倾向退役。"""
    for r in rows:
        n, roi = r["n"], r["roi"]
        if roi is None or n < min_n:
            r["action"], r["why"] = "留", f"样本 {n} < 门槛 {min_n}（证据不足）"
        elif roi < -eps:
            r["action"], r["why"] = "退", f"明确负倾向 ROI {roi:+.1%} < −{eps:.0%}"
        elif abs(roi) <= eps:
            r["action"], r["why"] = "退", f"**无倾向** |ROI {roi:+.1%}| ≤ {eps:.0%}"
        else:
            r["action"], r["why"] = "留", f"明确正倾向 ROI {roi:+.1%}"
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor-memory", required=True)
    ap.add_argument("--leg-pool", default="data/leg_pool_full")
    ap.add_argument("--theta", type=float, default=1.1)
    ap.add_argument("--eps", type=float, default=DEFAULT_EPS)
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)
    pool = args.leg_pool if Path(args.leg_pool).is_absolute() else str(
        Path(__file__).resolve().parents[1] / args.leg_pool)

    rows = _load_scores(args.factor_memory, pool, args.theta)
    decide(rows, args.eps, args.min_n)
    retire = [r for r in rows if r["action"] == "退"]
    keep = [r for r in rows if r["action"] == "留"]
    no_tend = [r for r in retire if "无倾向" in r["why"]]

    o = ["# 退役规则试算（ROI 无倾向 + 14 天周期 + 样本门槛）", ""]
    o.append(f"- 因子来源 `{args.factor_memory}`｜周期 **{args.window_days} 天**"
             f"｜无倾向带 **±{args.eps:.0%}**｜样本门槛 **{args.min_n}**")
    o.append(f"- 可评分因子 **{len(rows)}**｜**退 {len(retire)}**"
             f"（其中无倾向 {len(no_tend)}）｜留 {len(keep)}")
    o.append("")
    o.append("## 退役清单（按确定性规则）")
    o.append("")
    o.append("| 因子 | 类型 | 样本 | 观察/真实 | ROI | 理由 |")
    o.append("|---|---|---|---|---|---|")
    for r in sorted(retire, key=lambda x: (x["roi"] if x["roi"] is not None else 0)):
        o.append(f"| {r['name']} | {r['type']} | {r['n']} | {r['n_obs']}/{r['n_real']} | "
                 f"{(r['roi'] or 0):+.1%} | {r['why']} |")
    o.append("")
    o.append("## 保留清单")
    o.append("")
    o.append("| 因子 | 类型 | 样本 | ROI | 理由 |")
    o.append("|---|---|---|---|---|")
    for r in sorted(keep, key=lambda x: -(x["roi"] or 0)):
        o.append(f"| {r['name']} | {r['type']} | {r['n']} | {(r['roi'] or 0):+.1%} | {r['why']} |")
    text = "\n".join(o)
    print(text)
    if args.md:
        Path(args.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已落盘: {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
