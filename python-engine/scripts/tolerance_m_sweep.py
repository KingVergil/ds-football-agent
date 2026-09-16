#!/usr/bin/env python3
"""容错票型 M 的稳健性扫描（单口径 = 每场只买 x 最高侧）。

为什么单独写
───────────
`sim_ticket_plans.py` 的 `ways = comb(n, n−m)` **没有建模多选的笛卡尔积**，
对单口径（每腿 1 个 pick）恰好等价，但对多选会低估成本。
本脚本用**引擎自己的 `parlay_leg_combinations`** 展开真实子注，避免该口径问题。

输出
───
对每个 M：全窗口 ROI、逐日盈亏、**leave-one-day-out 的最低 ROI**（抽掉任意单日后的最差值）、
以及"抽掉最好日"后的 ROI。判据：**抽掉任意单日仍为正**才算稳健。

用法
───
    python3 -m scripts.tolerance_m_sweep --pool data/leg_pool_all --theta 1.1 \
        --ms 4,5,6,7 --top-n 9 --md docs/tolerance_m_sweep.md
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_settlement import parse_ticket_spec, parlay_leg_combinations  # noqa: E402

UNIT = 2.0
RATE = 0.65


def load_days(pool_dir: str, theta: float) -> dict[str, list[dict]]:
    """每场只取 x 最高侧 → 全单选腿；只保留可结算（有 result+SP）的场次。"""
    out: dict[str, list[dict]] = {}
    for f in sorted(glob.glob(str(Path(pool_dir) / "*.json"))):
        day = Path(f).stem
        best: dict[str, dict] = {}
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            x = float(r.get("x") or 0)
            if x < theta:
                continue
            lid = r.get("lota_id")
            if not lid:
                continue
            if lid not in best or x > best[lid]["x"]:
                best[lid] = {
                    "lota_id": lid, "side": r.get("side"), "x": x,
                    "actual": r.get("actual") or "", "sp": float(r.get("sp") or 0),
                    "settled": bool(r.get("settled")),
                }
        legs = [l for l in best.values() if l["settled"] and l["actual"] and l["sp"] > 0]
        if legs:
            out[day] = legs
    return out


def ticket_pnl(legs: list[dict], n_legs: int, m: int) -> tuple[float, float, int]:
    """按引擎口径展开 N过M 子注并结算；返回 (投注, 派彩, 中奖注数)。"""
    chosen = legs[:n_legs]
    n = len(chosen)
    if m >= n or m < 2:
        return 0.0, 0.0, 0          # m=n 即 N串1（本扫描不关心），m<2 非法
    spec = parse_ticket_spec(f"{n}过{m}")
    if spec is None:
        return 0.0, 0.0, 0
    stake = 0.0
    payout = 0.0
    wins = 0
    for combo in parlay_leg_combinations(chosen, spec):
        stake += UNIT                      # 单口径：每条腿 1 个 pick → 每组合恰 1 注
        if all(l["side"] == l["actual"] for l in combo):
            o = 1.0
            for l in combo:
                o *= l["sp"]
            payout += RATE * o * UNIT
            wins += 1
    return stake, payout, wins


def run(pool_dir: str, theta: float, ms: list[int], top_n: int,
        start: str = "", end: str = "") -> list[dict]:
    days = load_days(pool_dir, theta)
    rows = []
    for m in ms:
        per: dict[str, tuple[float, float, int]] = {}
        for day, legs in sorted(days.items()):
            if start and day < start:
                continue
            if end and day > end:
                continue
            if len(legs) < top_n:
                continue
            s, p, w = ticket_pnl(legs, top_n, m)
            if s > 0:
                per[day] = (s, p, w)
        if not per:
            continue
        tot_s = sum(v[0] for v in per.values())
        tot_p = sum(v[1] for v in per.values())
        tot_w = sum(v[2] for v in per.values())
        roi = (tot_p - tot_s) / tot_s if tot_s else 0.0
        # leave-one-day-out：抽掉任意一天后的最低 ROI
        loo = None
        loo_day = ""
        for d in per:
            s2 = tot_s - per[d][0]
            p2 = tot_p - per[d][1]
            r = (p2 - s2) / s2 if s2 else 0.0
            if loo is None or r < loo:
                loo, loo_day = r, d
        # 抽掉"最好日"（贡献最大的一天）
        best_day = max(per, key=lambda d: per[d][1] - per[d][0])
        s2 = tot_s - per[best_day][0]
        p2 = tot_p - per[best_day][1]
        roi_wo_best = (p2 - s2) / s2 if s2 else 0.0
        prof_days = sum(1 for d in per if per[d][1] > per[d][0])
        rows.append({
            "m": m, "label": f"{top_n}过{m}", "days": len(per),
            "stake": tot_s, "payout": tot_p, "pnl": tot_p - tot_s, "roi": roi,
            "wins": tot_w, "win_days": prof_days,
            "loo_roi": loo, "loo_day": loo_day, "roi_wo_best": roi_wo_best,
            "best_day": best_day,
            "per_day": {d: {"stake": v[0], "payout": v[1]} for d, v in per.items()},
        })
    return rows


def render(rows: list[dict], theta: float, top_n: int, window: str) -> str:
    o = ["# 容错票型 M 的稳健性扫描（单口径）", ""]
    o.append(f"- 腿池门限 θ={theta}｜每票取前 {top_n} 条腿（按 stage1 顺序，不做 x 择优）")
    o.append(f"- 窗口：{window}｜单口径 = 每场只买 x 最高侧（每腿 1 注基准）")
    o.append(f"- 判据：**抽掉任意单日（leave-one-day-out）后 ROI 仍为正** 才算稳健")
    o.append("")
    o.append("| 票型 | 天 | 投注 | 派彩 | 盈亏 | ROI | 中奖注/盈利天 | 抽最好日 | **抽任意单日最低** |")
    o.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        o.append(f"| {r['label']} | {r['days']} | {r['stake']:.0f} | {r['payout']:.0f} | "
                 f"{r['pnl']:+.0f} | {r['roi']:+.1%} | {r['wins']}/{r['win_days']} | "
                 f"{r['roi_wo_best']:+.1%} | {r['loo_roi']:+.1%}（抽 {r['loo_day'][5:]}） |")
    o.append("")
    ok = [r for r in rows if r["loo_roi"] > 0]
    if ok:
        o.append(f"✅ **通过稳健性检验（抽任意单日仍为正）**：{', '.join(r['label'] for r in ok)}")
    else:
        o.append("❌ **没有任何票型通过稳健性检验**：每一种抽掉某个单日后都会变负。")
    return "\n".join(o)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="data/leg_pool_all")
    ap.add_argument("--theta", type=float, default=1.1)
    ap.add_argument("--ms", default="4,5,6")
    ap.add_argument("--top-n", type=int, default=9)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--md", default="")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)
    ms = [int(x) for x in str(args.ms).split(",") if x.strip()]
    rows = run(args.pool, args.theta, ms, args.top_n, args.start, args.end)
    window = f"{args.start or '最早'} ~ {args.end or '最晚'}"
    text = render(rows, args.theta, args.top_n, window)
    print(text)
    if args.md:
        Path(args.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已落盘: {args.md}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已落盘: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
