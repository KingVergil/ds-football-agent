#!/usr/bin/env python3
"""0-LLM 规则臂回测（baseline）：账本门筛腿 → 按 x 降序取前 N → `N串1` → 用真实开奖结算。

这是「人设臂（LLM）」必须打败的基准。它完全不调 LLM：选腿、排序、票型全由规则决定。

规则（与引擎 `selector="x_top"` 一致）
─────────────────────────────────────
1. 只取 `goal_line == 0` 的场次（账本证明让球盘无边际）；
2. 每腿取 x = p_锐市场(去水) × 北单赛前赔率 ≥ θ 的**那一侧**（只单选，不多选）；
3. 按 x 降序取前 N 条（每场最多一条腿）→ `N串1`（1 注 = 2 元）；
4. 每天用**当天之前**的账本推 θ 与最小关数 m_star（as_of 防未来）；
   腿数 < m_star 或 Πx 不过漂移安全垫线 → 当天空仓。

统计口径（三条，别混）
─────────────────────
* **腿级**：y = E[开奖SP × 1{中}] 与 95%CI（按天聚类）——样本量最大，是主证据；
* **票级·单条路径**：44 天实际跑出来的结果——方差极大（N≥4 时 44 天里常常一次都不中）；
* **票级·bootstrap**：按天重采样出上万条 44 天路径，给出 ROI 分布 / 盈利概率——
  这是"如果这段行情再来一次，我会怎样"的正确答案。

用法
────
    python3 -m scripts.rule_arm_backtest --role bc狗 --start 2026-07-01 --end 2026-09-09 \
        --ns 3,4,5,6,9 --bootstrap 10000 --capital 5000 --md docs/rule_arm_report.md
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.beidan_settlement import result_code_to_pick  # noqa: E402
from src.pool_ledger import (  # noqa: E402
    PoolLedger, TAKEOUT, available_days, collect_day_rows, ledger_path,
)

SIDES = ("H", "D", "A")


def boom(ok: bool) -> str:
    return "✅" if ok else "❌"


def leg_pool(rows: list[dict], gl_classes=("gl0",), theta: float = 1.1):
    """从当日缓存行里取合格腿：(x, lid, side, sp, won)。"""
    out = []
    for r in rows:
        gl = float(r.get("gl") or 0.0)
        cls = "gl0" if abs(gl) < 1e-9 else "glN"
        if cls not in gl_classes:
            continue
        win = result_code_to_pick(r.get("result"))
        for s in SIDES:
            x = float((r.get("x") or {}).get(s) or 0.0)
            if x >= theta:
                out.append({"x": x, "lid": r["lid"], "side": s, "sp": r.get("sp"),
                            "won": (s == win), "day": r.get("date")})
    return out


def top_distinct(pool: list[dict], n: int) -> list[dict]:
    """按 x 降序取前 n 条，**同一场只留一条**（同场多方向互斥，串起来必不中）。"""
    out, seen = [], set()
    for l in sorted(pool, key=lambda x: -x["x"]):
        if l["lid"] in seen:
            continue
        out.append(l)
        seen.add(l["lid"])
        if len(out) >= n:
            break
    return out


def random_distinct(pool: list[dict], n: int, rnd: random.Random) -> list[dict]:
    """随机取 n 条互不同场的腿（每场内部随机挑一个方向）。"""
    by_lid: dict[str, list[dict]] = {}
    for l in pool:
        by_lid.setdefault(l["lid"], []).append(l)
    lids = list(by_lid)
    if len(lids) < n:
        return []
    picks = []
    for lid in rnd.sample(lids, n):
        picks.append(rnd.choice(by_lid[lid]))
    return picks


def settle_ticket(legs: list[dict]) -> tuple[float, bool]:
    """票派彩：全中才有 0.65 × ΠSP；1 注成本 2 元（返回 (派彩, 是否中)）。"""
    if any(l.get("sp") in (None, 0) for l in legs):
        return 0.0, False
    if not all(l["won"] for l in legs):
        return 0.0, False
    prod = 1.0
    for l in legs:
        prod *= float(l["sp"])
    return 2.0 * TAKEOUT * prod, True


def build_days(role: str, start: str, end: str, theta_override=None):
    led = PoolLedger(ledger_path(role)).load()
    days = [d for d in available_days() if start <= d <= end]
    out = []
    for d in days:
        pol = led.policy({"pool_gate": {"mode": "enforce", "gl_classes": ["gl0"],
                                        "min_n": 30, "window_days": 60}}, as_of=d)
        theta = float(theta_override if theta_override is not None else pol["theta"])
        rows = collect_day_rows(d)
        pool = leg_pool(rows, theta=theta)
        pool_all = leg_pool(rows, theta=0.0)      # 对照：不过 x 门（仍是 gl0）
        out.append({"day": d, "rows": len(rows), "pool": pool, "pool_all": pool_all,
                    "theta": theta, "m_star": pol.get("m_star"),
                    "source": pol.get("source")})
    return out


def realized_path(days: list[dict], n: int, capital: float):
    stake = payout = 0.0
    tickets = wins = 0
    legs_used = []
    curve = []
    for d in days:
        pool = top_distinct(d["pool"], n)
        m_star = d.get("m_star") or 1
        if len(pool) < max(n, m_star) or any(l["sp"] in (None, 0) for l in pool):
            curve.append(capital)
            continue
        pay, hit = settle_ticket(pool)
        stake += 2.0
        payout += pay
        tickets += 1
        wins += int(hit)
        legs_used += pool
        capital += pay - 2.0
        curve.append(capital)
    roi = (payout / stake - 1.0) if stake else 0.0
    return {"n": n, "tickets": tickets, "wins": wins, "stake": stake,
            "payout": payout, "roi": roi, "capital_end": capital,
            "legs": legs_used, "curve": curve,
            "max_dd": _max_dd(curve)}


def _max_dd(curve: list[float]) -> float:
    peak, dd = -1e18, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd


def leg_stats(legs: list[dict]) -> dict:
    """腿级 y = E[SP·1{中}]（按天聚类 SE）与命中率。"""
    if not legs:
        return {"n": 0}
    per_day: dict[str, list[float]] = {}
    zs = []
    for l in legs:
        z = float(l["sp"]) if (l["won"] and l.get("sp")) else 0.0
        zs.append(z)
        per_day.setdefault(l["day"], []).append(z)
    y = statistics.mean(zs)
    n = len(zs)
    dev = sum((sum(v) - y * len(v)) ** 2 for v in per_day.values())
    se = math.sqrt(dev) / n if n else 0.0
    wins = sum(1 for l in legs if l["won"])
    return {"n": n, "days": len(per_day), "y": y, "se": se,
            "lo": y - 1.96 * se, "hi": y + 1.96 * se,
            "hit_rate": wins / n, "sp_mean_win": (statistics.mean(
                [float(l["sp"]) for l in legs if l["won"] and l.get("sp")]) if wins else 0.0)}


def bootstrap(days: list[dict], n: int, mode: str, capital: float, iters: int,
              seed: int = 17) -> dict:
    """按天重采样，给出 ROI 分布。

    mode:
      * pool_random      —— 每天在「过门池」里按日种子随机取 n 条（默认规则臂：不在噪声上择优）
      * x_top            —— 每天按 x 降序取前 n 条（对照：x 排序有没有用）
      * random_all_nogate—— 每天在「所有 gl0 场次」里随机取 n 条（对照：x 门有没有用）
    """
    rnd = random.Random(seed)
    key = {"pool_random": "pool", "x_top": "pool", "random_pool": "pool",
           "random_all_nogate": "pool_all"}[mode]
    usable = [d for d in days if len({l["lid"] for l in d.get(key) or []})
              >= max(n, d.get("m_star") or 1)]
    if not usable:
        return {"n": n, "mode": mode, "paths": 0}
    rois, wins_any, ends = [], 0, []
    sel_z: list[float] = []
    for _ in range(iters):
        cap = capital
        stake = payout = 0.0
        won_any = False
        for _ in range(len(usable)):
            d = usable[rnd.randrange(len(usable))]
            pool = d.get(key) or []
            if mode == "x_top":
                pick = top_distinct(pool, n)
            else:                       # pool_random / random_pool / *_nogate
                pick = random_distinct(pool, n, rnd)
            if len(pick) < n or any(l.get("sp") in (None, 0) for l in pick):
                continue
            if mode == "x_top":
                for l in pick:
                    sel_z.append(float(l["sp"]) if l["won"] else 0.0)
            pay, hit = settle_ticket(pick)
            stake += 2.0
            payout += pay
            won_any = won_any or hit
            cap += pay - 2.0
        if stake:
            rois.append(payout / stake - 1.0)
            ends.append(cap)
            wins_any += int(won_any)
    if not rois:
        return {"n": n, "mode": mode, "paths": 0}
    rois.sort()
    ends.sort()
    out = {"n": n, "mode": mode, "paths": len(rois),
           "roi_mean": statistics.mean(rois),
           "roi_med": statistics.median(rois),
           "roi_p5": rois[int(0.05 * len(rois))],
           "roi_p95": rois[int(0.95 * len(rois))],
           "p_profit": sum(1 for r in rois if r > 0) / len(rois),
           "p_win_any": wins_any / len(rois),
           "cap_med": statistics.median(ends),
           "cap_p5": ends[int(0.05 * len(ends))],
           "cap_p95": ends[int(0.95 * len(ends))]}
    if sel_z:      # 理论期望：0.65 × y^n − 1（y 取被选腿的实测均值）
        out["y_sel"] = statistics.mean(sel_z)
        out["exp_roi_theory"] = TAKEOUT * out["y_sel"] ** n - 1.0
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="bc狗", help="账本来源角色")
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--end", default="2026-09-09")
    ap.add_argument("--ns", default="2,3,4,9")
    ap.add_argument("--theta", type=float, default=None, help="覆盖账本门限（默认按 as_of 取）")
    ap.add_argument("--capital", type=float, default=5000.0)
    ap.add_argument("--bootstrap", type=int, default=10000)
    ap.add_argument("--md", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    ns = [int(x) for x in str(args.ns).split(",") if x.strip()]
    days = build_days(args.role, args.start, args.end, theta_override=args.theta)
    pools = [d for d in days if d["pool"]]
    print("=" * 86)
    print(f"0-LLM 规则臂回测：{args.start} ~ {args.end}（{len(days)} 天，{len(pools)} 天有合格腿）")
    print("=" * 86)
    print(f"  账本 θ 来源：{ {d['source'] for d in days} }；"
          f"θ 取值 {sorted({d['theta'] for d in days})}")
    print(f"  每日合格腿数：中位 {statistics.median([len(d['pool']) for d in pools]):.0f}，"
          f"均值 {statistics.mean([len(d['pool']) for d in pools]):.1f}，"
          f"总 {sum(len(d['pool']) for d in days)} 条")

    out = {"days": len(days), "days_with_pool": len(pools), "ns": {}}
    for n in ns:
        real = realized_path(days, n, args.capital)
        bs_top = bootstrap(days, n, "pool_random", args.capital, args.bootstrap)
        bs_x = bootstrap(days, n, "x_top", args.capital, args.bootstrap)
        bs_rnd = bootstrap(days, n, "random_all_nogate", args.capital, args.bootstrap)
        ls = leg_stats(real["legs"])
        out["ns"][str(n)] = {"realized": {k: v for k, v in real.items() if k != "legs"},
                             "leg_stats": ls, "boot_x_top": bs_top,
                             "boot_random": bs_rnd}
        print(f"\n── N={n} 关串1（1 注 2 元/天）──")
        print(f"  腿级（选中腿）：n={ls.get('n', 0)} 命中率 {ls.get('hit_rate', 0):.1%} "
              f"y={ls.get('y', 0):.3f} [{ls.get('lo', 0):.3f},{ls.get('hi', 0):.3f}] "
              f"（β 打平线 9关1.049 / 5关1.090）")
        print(f"  实际路径：出票 {real['tickets']} 张，中 {real['wins']} 张，"
              f"投入 {real['stake']:.0f} 元，派彩 {real['payout']:.0f} 元，"
              f"ROI {real['roi']:+.1%}，期末 {real['capital_end']:.0f} 元，最大回撤 {real['max_dd']:.0%}")
        if bs_top.get("paths"):
            print(f"  bootstrap(池内按日种子随机)：均值 ROI {bs_top['roi_mean']:+.1%}，"
                  f"中位 {bs_top['roi_med']:+.1%}，5%~95% [{bs_top['roi_p5']:+.1%},{bs_top['roi_p95']:+.1%}]，"
                  f"盈利概率 {bs_top['p_profit']:.0%}，至少中 1 张 {bs_top['p_win_any']:.0%}，"
                  f"期末中位 {bs_top['cap_med']:.0f} 元")
        if ls.get("hit_rate"):
            exp_wins = real["tickets"] * ls["hit_rate"] ** n
            print(f"  可读性：{real['tickets']} 张票 × 每腿命中 {ls['hit_rate']:.1%} → "
                  f"期望中票 {exp_wins:.2f} 张"
                  f"{'（<1 张 ⇒ 实际路径基本说明不了问题）' if exp_wins < 1 else ''}")
        if bs_rnd.get("paths"):
            print(f"  bootstrap(对照·不过 x 门随机)：均值 ROI {bs_rnd['roi_mean']:+.1%}，"
                  f"盈利概率 {bs_rnd['p_profit']:.0%}  ← 看 x 门有没有用；"
                  f"对照·x 降序择优 {bs_x.get('roi_mean', 0):+.1%}")
            if bs_top.get("exp_roi_theory") is not None:
                print(f"  理论期望（0.65×y^N−1, y={bs_top['y_sel']:.3f}）："
                      f"{bs_top['exp_roi_theory']:+.1%}")

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n已写出 {args.json}")
    if args.md:
        write_md(Path(args.md), args, days, out)
        print(f"已写出 {args.md}")


def write_md(path: Path, args, days: list[dict], out: dict) -> None:
    L = [f"# 0-LLM 规则臂回测（{args.start} ~ {args.end}）\n"]
    L.append(f"- 账本：`{args.role}`（θ 按每天 as_of 取；覆盖 `--theta` 未用）")
    L.append(f"- 天数：{out['days']} 天，其中有合格腿 {out['days_with_pool']} 天；"
             f"总合格腿 {sum(len(d['pool']) for d in days)} 条")
    L.append(f"- 起点资金 {args.capital:.0f} 元；每票 1 注 2 元；票型 `N串1`\n")
    L.append("| N | 出票 | 中票 | 投入 | 派彩 | 实际ROI | 期末 | 最大回撤 | "
             "bootstrap均值ROI | 盈利概率 | 至少中1张 | 期末中位 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for n, r in out["ns"].items():
        real, bs = r["realized"], r.get("boot_x_top", {})
        L.append(f"| {n} | {real['tickets']} | {real['wins']} | {real['stake']:.0f} | "
                 f"{real['payout']:.0f} | {real['roi']:+.1%} | {real['capital_end']:.0f} | "
                 f"{real['max_dd']:.0%} | {bs.get('roi_mean', 0):+.1%} | "
                 f"{bs.get('p_profit', 0):.0%} | {bs.get('p_win_any', 0):.0%} | "
                 f"{bs.get('cap_med', 0):.0f} |")
    L.append("\n## 腿级证据（主证据）\n")
    L.append("| N | 腿数 | 命中率 | y | 95%CI | 中奖均SP |")
    L.append("|---|---|---|---|---|---|")
    for n, r in out["ns"].items():
        s = r["leg_stats"]
        L.append(f"| {n} | {s.get('n', 0)} | {s.get('hit_rate', 0):.1%} | {s.get('y', 0):.3f} | "
                 f"[{s.get('lo', 0):.3f},{s.get('hi', 0):.3f}] | {s.get('sp_mean_win', 0):.2f} |")
    L.append("\n## 排序 vs 随机（同一腿池）\n")
    L.append("| N | 按 x 取前 N：均值ROI | 池内随机 N：均值ROI | 差值 |")
    L.append("|---|---|---|---|")
    for n, r in out["ns"].items():
        a, b = r.get("boot_x_top", {}), r.get("boot_random", {})
        L.append(f"| {n} | {a.get('roi_mean', 0):+.1%} | {b.get('roi_mean', 0):+.1%} | "
                 f"{a.get('roi_mean', 0) - b.get('roi_mean', 0):+.1%} |")
    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
