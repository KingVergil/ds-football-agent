#!/usr/bin/env python3
"""高错价腿（x 门槛）捕捞 + 票级实测（0.65 只乘一次）。

背景（2026-09-15）
────────────────
官方口径（北京体彩网·帮助中心）：
    开奖SP = 单场总投注注数 / 中奖彩票总投注注数          （= 1/奖池份额，公平价）
    过关奖金 = 2元 × SP1×SP2×…×SPn × 65%                 （0.65 只乘一次，与串长无关）

于是：单腿每元期望 = 0.65·y − 1（y = E[SP·1{中}]），M 关票 = 0.65·y^M − 1。
腿级看「命中率 vs 1/(0.65·赔率)」永远难看（0.65 被算在每条腿上），
但**整票只收一次 0.65** —— 真正的判据是「每条腿的 y 是否 > (1/0.65)^(1/M)」。

本脚本回答两件事：
  A. x 门槛扫描：腿级 n / 命中率 / y / CI / 单腿 ROI / 所需最少关数 m_star；
  B. 票级实测：在 x>θ 的当日同波候选池里组 M 关票，用真实 SP 结算，
     拿 realized ROI 对照 predicted ROI = 0.65·Πx − 1（校准检查）。

用法
───
    python3 -m scripts.leg_high_x_harvest --pool data/leg_pool_all --md docs/leg_high_x_harvest.md
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import random
import statistics as st
from collections import defaultdict
from itertools import combinations
from pathlib import Path

RATE = 0.65
BREAKEVEN = 1.0 / RATE          # 1.5385
UNIT = 2.0                      # 北单每注 2 元


def load_legs(pool_dir: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(Path(pool_dir) / "*.json"))):
        day = Path(f).stem
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            if not r.get("settled"):
                continue
            try:
                sp = float(r.get("sp") or 0)
                odds = float(r.get("beidan_odds") or 0)
                x = float(r.get("x") or 0)
                mp = float(r.get("market_p") or 0)
            except (TypeError, ValueError):
                continue
            if sp <= 0 or odds <= 0 or x <= 0:
                continue
            out.append({
                "day": day, "wave": r.get("wave") or "", "lid": r.get("lota_id"),
                "gl": r.get("goal_line"), "side": r.get("side"), "x": x,
                "odds": odds, "mp": mp, "sp": sp,
                "hit": str(r.get("actual") or "") == str(r.get("side") or ""),
                "z": sp if str(r.get("actual") or "") == str(r.get("side") or "") else 0.0,
                "disp": r.get("disp"),
            })
    return out


def leg_stats(legs: list[dict], label: str) -> dict:
    n = len(legs)
    if n == 0:
        return {"label": label, "n": 0}
    zs = [l["z"] for l in legs]
    y = st.mean(zs)
    se = st.pstdev(zs) / math.sqrt(n) if n > 1 else 0.0
    lo, hi = y - 1.96 * se, y + 1.96 * se
    hit = sum(1 for l in legs if l["hit"]) / n
    xbar = st.mean(l["x"] for l in legs)
    m_star = None
    if y > 1.0:
        m_star = math.ceil(math.log(BREAKEVEN) / math.log(y))
    return {"label": label, "n": n, "hit": hit, "xbar": xbar, "y": y, "se": se,
            "lo": lo, "hi": hi, "single_roi": RATE * y - 1.0, "m_star": m_star,
            "mp": st.mean(l["mp"] for l in legs),
            "odds": st.mean(l["odds"] for l in legs)}


def fmt_row(s: dict) -> str:
    if not s.get("n"):
        return f"| {s['label']} | 0 | — | — | — | — | — | — |"
    return (f"| {s['label']} | {s['n']} | {s['hit']*100:.1f}% | {s['mp']*100:.1f}% | "
            f"{s['xbar']:.3f} | {s['y']:.3f} [{s['lo']:.3f},{s['hi']:.3f}] | "
            f"{s['single_roi']:+.1%} | {s['m_star'] if s['m_star'] else '—'} |")


def build_tickets(legs: list[dict], theta: float, M: int, top_k: int,
                  rand_per_wave: int, seed: int = 7) -> list[list[dict]]:
    """在 (day, wave) 内组票：top_k 按 x 降序取组合 + 随机组合基线。"""
    rnd = random.Random(seed)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for l in legs:
        if l["x"] > theta:
            groups[(l["day"], l["wave"])].append(l)
    out = []
    for _, g in sorted(groups.items()):
        # 同一场只能一条腿进票
        by_lid: dict[str, dict] = {}
        for l in sorted(g, key=lambda z: -z["x"]):
            by_lid.setdefault(l["lid"], l)
        pool = list(by_lid.values())
        if len(pool) < M:
            continue
        top = pool[:top_k]
        for c in combinations(top, M):
            out.append(list(c))
        seen = {tuple(sorted(l["lid"] for l in c)) for c in out[-0:]}
        for _ in range(rand_per_wave):
            c = rnd.sample(pool, M)
            key = tuple(sorted(l["lid"] for l in c))
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


def ticket_stats(tickets: list[list[dict]], label: str) -> dict:
    n = len(tickets)
    if n == 0:
        return {"label": label, "n": 0}
    cost = n * UNIT
    payout = win = 0.0
    prod_pred = 0.0
    for c in tickets:
        if all(l["hit"] for l in c):
            p = UNIT
            for l in c:
                p *= l["sp"]
            payout += p * RATE
            win += 1
        q = 1.0
        for l in c:
            q *= l["x"]
        prod_pred += RATE * q
    return {"label": label, "n": n, "hit": win / n,
            "realized": payout / cost - 1.0,
            "predicted": prod_pred / cost - 1.0,
            "cost": cost, "payout": payout}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="data/leg_pool_all")
    ap.add_argument("--md", default="")
    ap.add_argument("--M", type=int, default=4)
    ap.add_argument("--top-k", type=int, default=12)
    ap.add_argument("--rand-per-wave", type=int, default=300)
    args = ap.parse_args(argv)

    legs = load_legs(args.pool)
    days = sorted({l["day"] for l in legs})
    L: list[str] = []
    L.append("# 高错价腿（x 门槛）捕捞 + 票级实测")
    L.append("")
    L.append(f"- 腿池：`{args.pool}`｜已结算腿 **{len(legs)}** 条（侧）｜足球日 **{len(days)}** "
             f"（{days[0]} ~ {days[-1]}）")
    L.append(f"- 公式：`z = SP·1{{中}}`，`y = E[z]`；单腿每元 = `0.65y−1`；"
             f"{args.M} 关票每元 = `0.65·E[Πz]−1`（**0.65 只乘一次**）")
    L.append("- 打平线：单腿 `y>1.5385`；{} 关票 `y>(1/0.65)^(1/{})={:.5f}`".format(
        args.M, args.M, BREAKEVEN ** (1.0 / args.M)))
    L.append("")

    L.append("## A. x 门槛扫描（腿级）")
    L.append("")
    L.append("| 门槛 | n | 命中率 | 锐市场p̂ | x̄ | y=E[z] [95%CI] | 单腿ROI | 所需最少关数 |")
    L.append("|---|---|---|---|---|---|---|---|")
    L.append(fmt_row(leg_stats(legs, "全部（x≥1.0 池）")))
    thetas = (1.0, 1.05, 1.10, 1.11371, 1.15, 1.20, 1.30, 1.40, 1.5385, 1.70)
    for t in thetas:
        L.append(fmt_row(leg_stats([l for l in legs if l["x"] > t], f"x > {t}")))
    L.append("")

    L.append("## B. 票级实测（同波候选池内组票，真实 SP 结算）")
    L.append("")
    L.append(f"| 门槛 | 票数 | 中票率 | **realized ROI** | predicted `0.65Πx−1` | 成本 | 回收 |")
    L.append("|---|---|---|---|---|---|---|")
    for t in thetas:
        ts = build_tickets(legs, t, args.M, args.top_k, args.rand_per_wave)
        s = ticket_stats(ts, f"x>{t}")
        if not s.get("n"):
            L.append(f"| x > {t} | 0 | — | — | — | — | — |")
            continue
        L.append(f"| x > {t} | {s['n']} | {s['hit']*100:.2f}% | **{s['realized']:+.1%}** | "
                 f"{s['predicted']:+.1%} | {s['cost']:.0f} | {s['payout']:.0f} |")
    L.append("")

    # x>1.5385 的腿单独列出来
    hi = sorted([l for l in legs if l["x"] > BREAKEVEN], key=lambda z: -z["x"])
    L.append(f"## C. x > 1.5385 的腿（{len(hi)} 条，全列）")
    L.append("")
    L.append("| day | wave | 场次 | gl | 侧 | x | 赔率 | 锐市场p̂ | SP | 中 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for l in hi:
        L.append(f"| {l['day']} | {l['wave'][-5:]} | {l['lid']} | {l['gl']} | {l['side']} | "
                 f"{l['x']:.3f} | {l['odds']:.2f} | {l['mp']*100:.1f}% | {l['sp']:.2f} | "
                 f"{'✅' if l['hit'] else '❌'} |")
    L.append("")

    # 分布：高 x 腿集中在哪些场/日/盘中
    L.append("## D. 高 x 腿的集中度（x>1.11371）")
    L.append("")
    sub = [l for l in legs if l["x"] > 1.11371]
    by_day = defaultdict(int)
    for l in sub:
        by_day[l["day"]] += 1
    L.append(f"- 共 {len(sub)} 条，覆盖 {len(by_day)} 天；每天中位 {st.median(by_day.values()):.0f} 条，"
             f"最少 {min(by_day.values())} 条，最多 {max(by_day.values())} 条")
    gl0 = [l for l in sub if float(l["gl"] or 0) == 0]
    gln = [l for l in sub if float(l["gl"] or 0) != 0]
    L.append("")
    L.append("| 子集 | n | 命中率 | x̄ | y | 单腿ROI |")
    L.append("|---|---|---|---|---|---|")
    for name, ss in (("gl=0", gl0), ("gl≠0", gln),
                     ("赔率<3", [l for l in sub if l["odds"] < 3]),
                     ("赔率3~4", [l for l in sub if 3 <= l["odds"] < 4]),
                     ("赔率≥4", [l for l in sub if l["odds"] >= 4]),
                     ("锐市场p̂<0.25（弱侧）", [l for l in sub if l["mp"] < 0.25]),
                     ("锐市场p̂<0.15", [l for l in sub if l["mp"] < 0.15]),
                     ("主队侧 H", [l for l in sub if l["side"] == "H"]),
                     ("客队侧 A", [l for l in sub if l["side"] == "A"]),
                     ("平局侧 D", [l for l in sub if l["side"] == "D"])):
        s = leg_stats(ss, name)
        L.append(f"| {name} | {s.get('n',0)} | {s['hit']*100:.1f}% | {s['xbar']:.3f} | "
                 f"{s['y']:.3f} | {s['single_roi']:+.1%} |" if s.get("n")
                 else f"| {name} | 0 | — | — | — | — |")
    L.append("")

    text = "\n".join(L) + "\n"
    print(text)
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
        print(f"✅ 已写 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
