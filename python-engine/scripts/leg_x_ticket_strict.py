#!/usr/bin/env python3
"""x 门槛的票级严格验证（按天聚类 / 单波 / Pinnacle 单源 / 去重 + 对照组）。

对照组设计（谁对谁错的分水岭）：
  A 高 x top-4       ：选 x 最高的 4 条腿
  B 低 x bottom-4    ：选 x 最低的 4 条腿（贴着 1.0）
  C 随机 4           ：同池随机
  若 A ≫ B ≈ C ≈ −35%  ⇒ 「锐市场对、奖池软」被证实（x 有信息）
  若 A ≈ B ≈ C ≈ −35%  ⇒ 池子有效（x 无信息，之前的无边际结论）
"""
from __future__ import annotations
import glob, json, math, random, statistics as st
from collections import defaultdict
from itertools import combinations
from pathlib import Path

RATE, UNIT = 0.65, 2.0


def load(dedup=True, src=("Pinnacle",)):
    seen = {}
    for f in sorted(glob.glob("data/leg_pool_all/*.json")):
        day = Path(f).stem
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            if not r.get("settled"):
                continue
            try:
                sp = float(r.get("sp") or 0); od = float(r.get("beidan_odds") or 0)
                x = float(r.get("x") or 0)
            except (TypeError, ValueError):
                continue
            if sp <= 0 or od <= 0 or x <= 0:
                continue
            if src and r.get("mkt_src") not in src:
                continue
            hit = str(r.get("actual") or "") == str(r.get("side") or "")
            rec = {"day": day, "wave": r.get("wave") or "", "lid": r.get("lota_id"),
                   "side": r.get("side"), "x": x, "odds": od, "sp": sp, "hit": hit}
            if dedup:
                seen.setdefault((day, rec["lid"], rec["side"]), rec)
            else:
                seen[(day, rec["lid"], rec["side"], rec["wave"], len(seen))] = rec
    return list(seen.values())


def per_day_first_wave(legs):
    """每天只留最早一波（周末 16:30 / 工作日 22:30），避免同场多波重复计数。"""
    byday = defaultdict(list)
    for l in legs:
        byday[l["day"]].append(l)
    out = []
    for d, ls in byday.items():
        w = min(l["wave"] for l in ls if l["wave"])
        out.extend(l for l in ls if l["wave"] == w)
    return out


def run(legs, theta, M, mode, seed=11):
    rnd = random.Random(seed)
    byday = defaultdict(list)
    for l in legs:
        if l["x"] > theta:
            byday[l["day"]].append(l)
    rows = []
    for d, g in sorted(byday.items()):
        by = {}
        for l in sorted(g, key=lambda z: -z["x"]):
            by.setdefault(l["lid"], l)
        pool = list(by.values())
        if len(pool) < M:
            continue
        if mode == "top":
            picks = [list(c) for c in combinations(pool[:M], M)]
        elif mode == "bottom":
            picks = [list(c) for c in combinations(pool[-M:], M)]
        else:
            picks = [rnd.sample(pool, M) for _ in range(1)]
        cost = len(picks) * UNIT
        pay = 0.0
        prodx = 0.0
        for c in picks:
            q = 1.0
            for l in c:
                q *= l["x"]
            prodx += q
            if all(l["hit"] for l in c):
                p = UNIT
                for l in c:
                    p *= l["sp"]
                pay += p * RATE
        rows.append({"day": d, "roi": pay / cost - 1.0, "pred": RATE * prodx / cost - 1.0,
                     "n": len(picks)})
    return rows


def summarize(rows, label):
    if not rows:
        return f"| {label} | 0 | — | — | — | — |"
    r = [x["roi"] for x in rows]
    p = [x["pred"] for x in rows]
    n = len(r)
    m = st.mean(r)
    se = st.pstdev(r) / math.sqrt(n) if n > 1 else 0.0
    t = m / se if se else 0.0
    return (f"| {label} | {n} | {m:+.1%} | {st.median(r):+.1%} | "
            f"[{m-1.96*se:+.1%},{m+1.96*se:+.1%}] | t={t:+.2f} | {st.mean(p):+.1%} | "
            f"{sum(1 for v in r if v>0)}/{n} |")


def main():
    raw = load()
    first = per_day_first_wave(raw)
    print(f"腿样本：去重后 {len(raw)} 条；每天只留最早一波 {len(first)} 条；"
          f"覆盖 {len({l['day'] for l in first})} 天（仅 Pinnacle 源）\n")
    print("| 组 | 天数 | 平均ROI | 中位ROI | 95%CI | t | 预测0.65Πx−1 | 正收益天数 |")
    print("|---|---|---|---|---|---|---|---|")
    for mode, tag in (("top", "A 最高x top-4"), ("bottom", "B 最低x bottom-4"),
                      ("one", "C 随机4")):
        for theta in (1.0, 1.11371, 1.3, 1.5385):
            rows = run(first, theta, 4, mode)
            print(summarize(rows, f"{tag} @ x>{theta}"))
    print()
    # 全部源（含让球欧盘）对照
    allsrc = per_day_first_wave(load(src=()))
    print(f"【全部源】样本 {len(allsrc)} 条")
    print("| 组 | 天数 | 平均ROI | 中位ROI | 95%CI | t | 预测0.65Πx−1 | 正收益天数 |")
    print("|---|---|---|---|---|---|---|---|")
    for mode, tag in (("top", "A 最高x top-4"), ("bottom", "B 最低x bottom-4")):
        for theta in (1.0, 1.11371, 1.3):
            rows = run(allsrc, theta, 4, mode)
            print(summarize(rows, f"{tag} @ x>{theta}"))
    print()
    # 关数扫描（top-x，全 4 关 → 3/5/6 关）
    print("【关数扫描】top-x 组，最低波，Pinnacle")
    print("| 关数 | 门槛 | 天数 | 平均ROI | 95%CI | t |")
    print("|---|---|---|---|---|---|")
    for M in (2, 3, 4, 5, 6):
        for theta in (1.0, 1.11371, 1.3):
            rows = run(first, theta, M, "top")
            if not rows:
                continue
            r = [x["roi"] for x in rows]
            m = st.mean(r); se = st.pstdev(r)/math.sqrt(len(r))
            t = (m / se) if se else 0.0
            print(f"| {M}关 | x>{theta} | {len(r)} | {m:+.1%} | "
                  f"[{m-1.96*se:+.1%},{m+1.96*se:+.1%}] | {t:+.2f} |")


if __name__ == "__main__":
    main()
