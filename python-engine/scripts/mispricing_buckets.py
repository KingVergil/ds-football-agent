#!/usr/bin/env python3
"""北单奖池错价分桶实验（离线 · 只读本地缓存 · 0 次 LLM）。

要回答的问题
────────────
北单是**奖池型**玩法（parimutuel）：
    赛前赔率 o = 该结果"总投注 ÷ 押它"的快照；
    开奖 SP = 结算时的比值；派彩 = 2元 × 65% × Π SP（0.65 只乘一次）。
所以「这只狗能不能正期望」= 「奖池价里有没有系统性错价的桶，且错价够不够大」。

两个互补的检验量（每条 outcome = 一条"腿"）
────────────────────────────────────────
1) 锐市场口径（赛前可算）：
       x = p_锐市场(去水) × o_北单赛前赔率
   x 是"扣 0.65 之前的每元期望倍数"：单腿每元期望 = 0.65·x；M 关串 ≈ 0.65·x^M。
2) 实测口径（不依赖"锐市场=真相"）：
       z = 开奖SP × 1{这条腿中了}
   E[z] = y。单腿每元期望 = 0.65·y−1；M 关串 = 0.65·y^M − 1（0.65 只乘一次）。
   （输的腿派彩 0，所以不需要它自己的 SP —— 只用中奖方向的 SP 就能无偏估计。）

打平线：x* = (1/0.65)^(1/M) → M=2:1.240、3:1.154、5:1.090、8:1.055、9:1.049。

方法要点
────────
* 样本 = 缓存里**全部**北单场次（不看有没有下注、不按结果筛选）→ 无选择偏差。
* 同一场 H/D/A 三条腿高度相关 → 标准误按场次**聚类**（cluster-robust）。
* 粗桶：分辨率由样本量决定（σ≈0.2，4 桶 ≈5pp、8 桶 ≈7pp），细桶只会看到噪声。
* 只用与北单**同一让球盘口**的锐市场：gl=0 → Pinnacle 1X2；gl≠0 → 公平盘
  「平均欧盘胜/平/负(<line>)」且 line == goal_line。

用法
────
    python3 -m scripts.mispricing_buckets [--md docs/xxx.md] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_manager import MATCHES_DIR, TAGS_DIR  # noqa: E402
from src.beidan_settlement import result_code_to_pick  # noqa: E402

TAKEOUT = 0.65
SIDES = ("H", "D", "A")
SIDE_CN = {"H": "主胜/让球后主胜", "D": "平", "A": "客胜/让球后客胜"}
FAIR_LINE_RE = re.compile(
    r"平均欧盘胜/平/负\s*\((-?[\d.]+)\)\s*[:：]\s*"
    r"([\d.]+)\s*[/／]\s*([\d.]+)\s*[/／]\s*([\d.]+)")
PINNACLE_TRIPLE_RE = re.compile(r"([\d.]+)\s*/\s*([\d.]+)\s*/\s*([\d.]+)")
ODDS_BUCKETS = ((0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0),
                (3.0, 4.0), (4.0, 6.0), (6.0, 99.0))
BREAKEVEN = {m: (1.0 / TAKEOUT) ** (1.0 / m) for m in (1, 2, 3, 5, 8, 9)}


def bucket_of(o: float) -> str:
    for lo, hi in ODDS_BUCKETS:
        if lo <= o < hi:
            return f"{lo:g}–{hi:g}"
    return "?"


def devig(odds: list[float]) -> list[float]:
    """赔率 → 去水概率（比例法）：p_i = (1/o_i) / Σ_j (1/o_j)。"""
    inv = [1.0 / o for o in odds if o and o > 0]
    tot = sum(inv)
    return [v / tot for v in inv] if tot > 0 else []


def sharp_ref(tags: dict, goal_line: float) -> tuple[str, list[float]]:
    secs = tags.get("sections") or {}
    if goal_line != 0.0:
        txt = secs.get("fair-odds") or ""
        if not isinstance(txt, str):
            txt = json.dumps(txt, ensure_ascii=False)
        for m in FAIR_LINE_RE.finditer(txt):
            if abs(float(m.group(1)) - goal_line) < 1e-9:
                o = [float(m.group(i)) for i in (2, 3, 4)]
                if min(o) > 0:
                    return "让球欧盘(line 对齐)", o
        return "", []
    txt = secs.get("eu-odds-pinnacle") or ""
    if not isinstance(txt, str):
        txt = json.dumps(txt, ensure_ascii=False)
    trips = [t for t in PINNACLE_TRIPLE_RE.findall(txt) if all(float(x) > 1.0 for x in t)]
    return ("Pinnacle 1X2", [float(x) for x in trips[-1]]) if trips else ("", [])


def cluster_se(pairs: list[tuple[str, float]], mean: float) -> tuple[float, int]:
    """按场次聚类的均值标准误：SE = sqrt(Σ_场(Σ_腿(x−mean))²) / N_腿。"""
    if not pairs:
        return 0.0, 0
    per: dict[str, float] = {}
    for lid, x in pairs:
        per[lid] = per.get(lid, 0.0) + (x - mean)
    return math.sqrt(sum(v * v for v in per.values())) / len(pairs), len(per)


def describe(pairs: list[tuple[str, float]], label: str) -> dict:
    if not pairs:
        return {"label": label, "n": 0}
    xs = [x for _, x in pairs]
    mean = statistics.mean(xs)
    se, n_match = cluster_se(pairs, mean)
    return {"label": label, "n": len(xs), "n_match": n_match, "mean": mean,
            "median": statistics.median(xs), "sd": statistics.pstdev(xs), "se": se,
            "lo": mean - 1.96 * se, "hi": mean + 1.96 * se}


def verdict(d: dict, m: int = 9) -> str:
    be = BREAKEVEN[m]
    if d.get("lo", 0) > be:
        return "✅ 可用"
    if d.get("mean", 0) > be:
        return "⚠️ 临界"
    if d.get("mean", 0) > 1.0:
        return "➖ 不够"
    return "❌ 反向"


def fmt(d: dict, m: int = 9) -> str:
    if not d.get("n"):
        return f"{d['label']:<24} 无样本"
    return (f"{d['label']:<24} n={d['n']:>5d}(场{d['n_match']:>4d}) "
            f"x={d['mean']:.4f} [{d['lo']:.3f},{d['hi']:.3f}] σ={d['sd']:.3f} "
            f"{verdict(d, m)}")


# ─────────────────────────── 样本 ───────────────────────────

def build_rows(verbose: bool = True) -> list[dict]:
    tag_path = {p.stem: p for p in TAGS_DIR.glob("*.json")}
    rows: list[dict] = []
    seen: set[str] = set()
    stat = {"odds": 0, "no_tags": 0, "no_ref": 0}
    for p in sorted(MATCHES_DIR.glob("*.json")):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in (payload if isinstance(payload, list) else (payload.get("matches") or [])):
            lid = m.get("lota_id") or m.get("id")
            bi = m.get("beidan_info") or {}
            if not lid or not bi or lid in seen:
                continue
            seen.add(lid)
            o = [bi.get("home_odds"), bi.get("draw_odds"), bi.get("away_odds")]
            if not all(o):
                continue
            stat["odds"] += 1
            tp = tag_path.get(lid)
            if tp is None:
                stat["no_tags"] += 1
                continue
            try:
                tags = json.loads(tp.read_text(encoding="utf-8"))
            except Exception:
                stat["no_tags"] += 1
                continue
            try:
                gl = float(bi.get("goal_line"))
            except (TypeError, ValueError):
                gl = 0.0
            src, s_odds = sharp_ref(tags, gl)
            if not src:
                stat["no_ref"] += 1
                continue
            p_sharp, p_pool = devig(s_odds), devig([float(x) for x in o])
            if len(p_sharp) != 3 or len(p_pool) != 3:
                continue
            sp = bi.get("spvalue")
            stat["matches"] = stat.get("matches", 0) + 1
            rows.append({
                "lid": lid, "date": str(m.get("match_time") or "")[:10], "gl": gl,
                "src": src, "league": m.get("league_name") or "",
                "sp": float(sp) if sp else None, "result": bi.get("result"),
                "x": {s: p_sharp[i] * float(o[i]) for i, s in enumerate(SIDES)},
                "p_sharp": dict(zip(SIDES, p_sharp)), "p_pool": dict(zip(SIDES, p_pool)),
                "o_pool": {s: float(o[i]) for i, s in enumerate(SIDES)},
                "inv_sum": sum(1.0 / float(v) for v in o),
            })
    if verbose:
        print(f"  有北单三路赛前赔率 {stat['odds']} 场 → 拿到同盘口锐市场参考 "
              f"{stat.get('matches', 0)} 场（无 tags {stat['no_tags']}，"
              f"无同盘口参考 {stat['no_ref']}）")
    return rows


# ─────────────────────────── 主流程 ───────────────────────────

def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)

    print("=" * 82)
    print("北单奖池错价分桶实验（离线 · 0 次 LLM）")
    print("=" * 82)
    rows = build_rows()
    if not rows:
        print("没有可用样本")
        return
    legs = [(r["lid"], r["x"][s]) for r in rows for s in SIDES]
    out: dict = {"n_matches": len(rows), "n_legs": len(legs),
                 "breakeven": {str(k): v for k, v in BREAKEVEN.items()}}

    # ── 0) 有效性 ────────────────────────────────
    print("\n【0】有效性检查")
    d0 = describe(legs, "全样本")
    out["all"] = d0
    print("  " + fmt(d0))
    print(f"  打平线：单关 {BREAKEVEN[1]:.4f} | 5关 {BREAKEVEN[5]:.4f} | 9关 {BREAKEVEN[9]:.4f}")
    invs = sorted(r["inv_sum"] for r in rows)
    out["inv_sum_median"] = statistics.median(invs)
    print(f"  奖池赔率 Σ1/o 中位 {out['inv_sum_median']:.4f}"
          f"（≈1.0 → 赔率是未扣水的公平价，0.65 只在结算乘一次）")
    corr = {}
    for s_ in SIDES:
        a = [r["p_pool"][s_] for r in rows]
        b = [r["p_sharp"][s_] for r in rows]
        ma, mb = statistics.mean(a), statistics.mean(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        corr[s_] = cov / (math.sqrt(sum((x - ma) ** 2 for x in a))
                          * math.sqrt(sum((y - mb) ** 2 for y in b)))
        print(f"    corr(奖池隐含概率[{s_}], 锐市场概率[{s_}]) = {corr[s_]:+.3f}")
    out["corr"] = corr
    agree = sum(1 for r in rows
                if max(SIDES, key=lambda z: r["p_sharp"][z])
                == max(SIDES, key=lambda z: r["p_pool"][z]))
    out["fav_agree"] = agree / len(rows)
    print(f"  热门方向一致率 {agree}/{len(rows)} = {out['fav_agree']:.1%}"
          f"（越高 ⇒ 让球线对齐越可信）")
    srcs: dict[str, int] = {}
    for r in rows:
        srcs[r["src"]] = srcs.get(r["src"], 0) + 1
    out["sources"] = srcs
    print(f"  锐市场来源 {srcs}")

    # ── 1) 按方向 ────────────────────────────────
    print("\n【1】按方向")
    out["by_side"] = {}
    for s_ in SIDES:
        d = describe([(r["lid"], r["x"][s_]) for r in rows], f"{s_} {SIDE_CN[s_]}")
        out["by_side"][s_] = d
        print("  " + fmt(d))

    # ── 2) 按赔率桶 ──────────────────────────────
    print("\n【2】按北单奖池赔率分桶（x 口径：赛前可算）")
    out["by_odds"] = {}
    for lo, hi in ODDS_BUCKETS:
        b = f"{lo:g}–{hi:g}"
        pairs = [(r["lid"], r["x"][s_]) for r in rows for s_ in SIDES
                 if bucket_of(r["o_pool"][s_]) == b]
        d = describe(pairs, f"赔率 {b}")
        out["by_odds"][b] = d
        if d.get("n", 0) >= 20:
            print("  " + fmt(d))

    # ── 2b) 规则选腿（每场最多一条，避免事后挑选）──
    print("\n【2b】按规则选腿（每场在该桶里选腿；多腿取平均）")
    out["rule_by_odds"] = {}
    for lo, hi in ODDS_BUCKETS:
        b = f"{lo:g}–{hi:g}"
        pairs = []
        for r in rows:
            xs = [r["x"][s_] for s_ in SIDES if bucket_of(r["o_pool"][s_]) == b]
            if xs:
                pairs.append((r["lid"], sum(xs) / len(xs)))
        d = describe(pairs, f"规则取 赔率{b}")
        out["rule_by_odds"][b] = d
        if d.get("n", 0) >= 20:
            print("  " + fmt(d))

    # ── 3) 稳健性 ────────────────────────────────
    print("\n【3】稳健性")
    out["robust"] = {}
    checks = [
        ("平手盘 gl=0", lambda r: r["gl"] == 0),
        ("主让盘 gl<0", lambda r: r["gl"] < 0),
        ("Pinnacle 1X2 源", lambda r: r["src"].startswith("Pinnacle")),
        ("让球欧盘 源", lambda r: r["src"].startswith("让球欧盘")),
        ("热门一致", lambda r: max(SIDES, key=lambda z: r["p_sharp"][z])
         == max(SIDES, key=lambda z: r["p_pool"][z])),
        ("热门不一致", lambda r: max(SIDES, key=lambda z: r["p_sharp"][z])
         != max(SIDES, key=lambda z: r["p_pool"][z])),
    ]
    dates = sorted({r["date"] for r in rows if r["date"]})
    mid = dates[len(dates) // 2] if len(dates) > 3 else ""
    if mid:
        out["split_date"] = mid
        checks += [(f"前半 ≤{mid}", lambda r, m=mid: r["date"] <= m),
                   (f"后半 >{mid}", lambda r, m=mid: r["date"] > m)]
    for label, sel in checks:
        d = describe([(r["lid"], r["x"][s_]) for r in rows if sel(r) for s_ in SIDES], label)
        out["robust"][label] = d
        if d.get("n", 0) >= 20:
            print("  " + fmt(d))

    # ── 4) 实测口径（不假设锐市场=真相）──────────
    print("\n【4】实测口径：z = 开奖SP × 1{中了}（每 1 元派彩倍数，含 0.65）")
    print("     单腿期望 = 0.65·E[z] − 1；M 关串 = 0.65·E[z]^M − 1（0.65 只乘一次）")
    ok = [r for r in rows if r["sp"] and result_code_to_pick(r["result"]) in SIDES]
    print(f"  可用样本：{len(ok)} 场（有 result + SP）")
    out["realized"] = {"n_matches": len(ok), "by_odds": {}, "by_side": {}}

    def z_vals(sel_rows, bucket=None, side=None) -> list[float]:
        vals = []
        for r in sel_rows:
            win = result_code_to_pick(r["result"])
            cand = [side] if side else [s_ for s_ in SIDES
                                       if bucket is None or bucket_of(r["o_pool"][s_]) == bucket]
            if not cand:
                continue
            vals.append(sum(r["sp"] if z == win else 0.0 for z in cand) / len(cand))
        return vals

    def boot_ci(vals: list[float], m: int, n_boot: int = 4000) -> tuple[float, float, float]:
        """对场次重采样，给出 EV = 0.65·y^m − 1 的点估计与 95% CI（y = E[SP·1中]）。"""
        if not vals:
            return float("nan"), float("nan"), float("nan")
        rnd, n = random.Random(11), len(vals)
        y0 = statistics.mean(vals)
        evs = []
        for _ in range(n_boot):
            draw = [vals[rnd.randrange(n)] for _ in range(n)]
            y = statistics.mean(draw)
            evs.append(TAKEOUT * (y ** m) - 1.0)
        evs.sort()
        return (TAKEOUT * (y0 ** m) - 1.0, evs[int(0.025 * n_boot)], evs[int(0.975 * n_boot)])

    print(f"  {'桶':<9}{'n(场)':>6}{'y':>9}{'单腿EV':>9}   5关 EV[95%CI]{'':<6}9关 EV[95%CI]")
    for lo, hi in ODDS_BUCKETS:
        b = f"{lo:g}–{hi:g}"
        vals = z_vals(ok, bucket=b)
        if len(vals) < 30:
            continue
        y_raw = statistics.mean(vals)
        row: dict = {"n": len(vals), "y": y_raw, "ev": {}}
        for m in (1, 5, 9):
            row["ev"][m] = boot_ci(vals, m)
        out["realized"]["by_odds"][b] = row
        e = row["ev"]
        print(f"  {b:<9}{len(vals):>6}{y_raw:>9.3f}{e[1][0]:>8.0%}   "
              f"{e[5][0]:>+8.0%} [{e[5][1]:+.0%},{e[5][2]:+.0%}]  "
              f"{e[9][0]:>+8.0%} [{e[9][1]:+.0%},{e[9][2]:+.0%}]")
    for s_ in SIDES:
        vals = z_vals(ok, side=s_)
        if len(vals) < 30:
            continue
        y_raw = statistics.mean(vals)
        pt, l, h = boot_ci(vals, 1)
        out["realized"]["by_side"][s_] = {"n": len(vals), "y": y_raw, "ev": pt,
                                          "ev_lo": l, "ev_hi": h}
        print(f"  {'方向 ' + s_:<9}{len(vals):>6}{y_raw:>9.3f}{pt:>8.0%}")

    if mid and out["realized"]["by_odds"]:
        print("\n  好桶的时间稳健性（4–6 档）：")
        out["realized_time"] = {}
        for label, sel in ((f"前半 ≤{mid}", lambda r, m=mid: r["date"] <= m),
                           (f"后半 >{mid}", lambda r, m=mid: r["date"] > m)):
            vals = z_vals([r for r in ok if sel(r)], bucket="4–6")
            if len(vals) < 20:
                continue
            pt, l, h = boot_ci(vals, 5)
            out["realized_time"][label] = {"n": len(vals), "y": statistics.mean(vals),
                                           "ev5": pt, "ev5_lo": l, "ev5_hi": h}
            print(f"    {label:<16} n={len(vals):>4} y={statistics.mean(vals):.4f} "
                  f"5关EV {pt:+.0%} [{l:+.0%},{h:+.0%}]")

    # ── 4b) 赢率对照：奖池说的 vs 锐市场说的 vs 实际发生的 ──
    print("\n【4b】赢率对照（最直观）：奖池隐含 / 锐市场 / 实测")
    print(f"  {'桶':<8}{'n':>6}{'奖池隐含':>10}{'锐市场':>9}{'实测':>8}"
          f"{'中奖均SP':>10}{'y':>8}{'y±SE':>14}{'前5大贡献占比':>13}")
    out["winrate_table"] = {}
    for lo, hi in ODDS_BUCKETS:
        b = f"{lo:g}–{hi:g}"
        pp, ps, zs, sps = [], [], [], []
        for r in ok:
            w = result_code_to_pick(r["result"])
            for s_ in SIDES:
                if bucket_of(r["o_pool"][s_]) != b:
                    continue
                pp.append(r["p_pool"][s_])
                ps.append(r["p_sharp"][s_])
                if s_ == w:
                    sps.append(r["sp"])
                    zs.append(r["sp"])
                else:
                    zs.append(0.0)
        if len(zs) < 30:
            continue
        y = statistics.mean(zs)
        se = statistics.pstdev(zs) / math.sqrt(len(zs))
        tot = sum(zs) or 1.0
        top5 = sum(sorted(zs, reverse=True)[:5]) / tot
        out["winrate_table"][b] = {
            "n": len(zs), "p_pool": statistics.mean(pp), "p_sharp": statistics.mean(ps),
            "p_real": len(sps) / len(zs), "sp_win": statistics.mean(sps) if sps else 0.0,
            "y": y, "y_se": se, "y_lo": y - 1.96 * se, "y_hi": y + 1.96 * se,
            "top5_share": top5}
        print(f"  {b:<8}{len(zs):>6}{statistics.mean(pp):>10.3f}{statistics.mean(ps):>9.3f}"
              f"{len(sps)/len(zs):>8.3f}{(statistics.mean(sps) if sps else 0):>10.2f}"
              f"{y:>8.3f}   [{y-1.96*se:.3f},{y+1.96*se:.3f}]{top5:>13.1%}")

    # ── 4c) x 规则（赛前可算、与让球线无关）+ 让球线拆分 ──
    print("\n【4c】x 规则：「x ≥ 阈值就买该侧」（x 与让球线无关，纯赛前可算）")
    out["x_rule"] = {}

    def rule_vals(sel_rows, thr: float) -> tuple[list[float], set[str]]:
        zs, days = [], set()
        for r in sel_rows:
            picks = [s_ for s_ in SIDES if r["x"][s_] >= thr]
            if not picks:
                continue
            win = result_code_to_pick(r["result"])
            zs.append(sum(r["sp"] if s_ == win else 0.0 for s_ in picks) / len(picks))
            days.add(r["date"])
        return zs, days

    def rule_report(label: str, zs: list[float], days: set[str]) -> None:
        if len(zs) < 20:
            print(f"  {label:<26} n={len(zs)} 样本太少")
            return
        y = statistics.mean(zs)
        se = statistics.pstdev(zs) / math.sqrt(len(zs))
        m_star = (math.log(1 / TAKEOUT) / math.log(y)) if y > 1 else float("inf")
        out["x_rule"][label] = {"n": len(zs), "days": len(days), "y": y, "se": se,
                                "y_lo": y - 1.96 * se, "y_hi": y + 1.96 * se,
                                "ev5": TAKEOUT * y ** 5 - 1, "ev9": TAKEOUT * y ** 9 - 1,
                                "m_star": m_star}
        print(f"  {label:<26} n={len(zs):>4}场/{len(days):>2}天({len(zs)/len(days):>4.1f}腿/天) "
              f"y={y:.3f} [{y-1.96*se:.3f},{y+1.96*se:.3f}] 单腿{TAKEOUT*y-1:+.0%} "
              f"5关{TAKEOUT*y**5-1:+.0%} 9关{TAKEOUT*y**9-1:+.0%} 打平≥{m_star:.1f}关")

    rule_report("x≥1.10 全部场次", *rule_vals(ok, 1.10))
    rule_report("x≥1.10 且 gl=0", *rule_vals([r for r in ok if r["gl"] == 0], 1.10))
    rule_report("x≥1.10 且 gl≠0", *rule_vals([r for r in ok if r["gl"] != 0], 1.10))
    for thr in (1.05, 1.08, 1.12, 1.15, 1.20):
        rule_report(f"x≥{thr:.2f} 且 gl=0", *rule_vals([r for r in ok if r["gl"] == 0], thr))
    if len(dates) > 3:
        for label, sel in ((f"x≥1.10 gl=0 前半 ≤{mid}",
                            lambda r, m=mid: r["gl"] == 0 and r["date"] <= m),
                           (f"x≥1.10 gl=0 后半 >{mid}",
                            lambda r, m=mid: r["gl"] == 0 and r["date"] > m)):
            rule_report(label, *rule_vals([r for r in ok if sel(r)], 1.10))
    print("\n  x 规则限定在 4–6 赔率档内的对照（看两者是否同一批腿）：")
    for label, sel in (("x≥1.10 & 赔率4–6", lambda r: True),):
        zs, days = [], set()
        for r in ok:
            picks = [s_ for s_ in SIDES
                     if r["x"][s_] >= 1.10 and bucket_of(r["o_pool"][s_]) == "4–6"]
            if not picks:
                continue
            win = result_code_to_pick(r["result"])
            zs.append(sum(r["sp"] if s_ == win else 0.0 for s_ in picks) / len(picks))
            days.add(r["date"])
        rule_report(label, zs, days)

    # ── 5) 打平所需关数 ──────────────────────────
    print("\n【5】每个桶要几关才打平（实测 y）")
    out["ev_table"] = {}
    for b, row in out["realized"]["by_odds"].items():
        y = row["y"]
        m_star = (math.log(1 / TAKEOUT) / math.log(y)) if y > 1 else float("inf")
        out["ev_table"][b] = {"y": y, "m_star": m_star}
        print(f"  赔率 {b:<8} y={y:.4f} → 打平需 ≥{m_star:.1f} 关")

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n已写出 {args.json}")
    if args.md:
        write_md(Path(args.md), out)
        print(f"已写出 {args.md}")


def write_md(path: Path, out: dict) -> None:
    L: list[str] = ["# 北单奖池错价分桶实验（离线 · 0 次 LLM）\n"]
    L.append(f"- 样本：**{out['n_matches']} 场 / {out['n_legs']} 条腿**"
             f"（缓存里全部北单场次，无结果条件化选择）")
    L.append(f"- 奖池赔率 Σ1/o 中位 **{out['inv_sum_median']:.4f}**"
             f"（≈1.0 ⇒ 赔率未扣水，0.65 只乘一次）")
    L.append(f"- 热门方向一致率 **{out['fav_agree']:.1%}**；锐市场来源 {out['sources']}")
    L.append("- 检验量：`x = p_锐市场(去水) × o_赛前赔率`（赛前可算）；"
             "`y = E[开奖SP × 1{中了}]`（实测，不假设锐市场=真相）")
    L.append(f"- 打平线 `x*=(1/0.65)^(1/M)`：5关 {BREAKEVEN[5]:.4f}、9关 {BREAKEVEN[9]:.4f}\n")
    L.append("## 1. x 口径（赛前可算）按赔率桶\n")
    L.append("| 桶 | n(腿) | n(场) | x | 95% CI | σ | 判定(9关线) |")
    L.append("|---|---|---|---|---|---|---|")
    for b, d in out.get("by_odds", {}).items():
        if d.get("n", 0) < 20:
            continue
        L.append(f"| {b} | {d['n']} | {d['n_match']} | {d['mean']:.4f} | "
                 f"[{d['lo']:.3f}, {d['hi']:.3f}] | {d['sd']:.3f} | {verdict(d)} |")
    L.append("\n## 2. 实测口径 y（用开奖结果 + 开奖 SP）\n")
    L.append("| 桶 | n(场) | y | 单腿 EV | 5关 EV [95%CI] | 9关 EV [95%CI] |")
    L.append("|---|---|---|---|---|---|")
    for b, r in out.get("realized", {}).get("by_odds", {}).items():
        e = r["ev"]
        L.append(f"| {b} | {r['n']} | {r['y']:.4f} | {e[1][0]:+.0%} | "
                 f"{e[5][0]:+.0%} [{e[5][1]:+.0%},{e[5][2]:+.0%}] | "
                 f"{e[9][0]:+.0%} [{e[9][1]:+.0%},{e[9][2]:+.0%}] |")
    if out.get("x_rule"):
        L.append("\n## 2c. x 规则（赛前可算、与让球线无关）\n")
        L.append("| 规则 | n(场) | 天数 | 腿/天 | y | 95%CI | 5关 EV | 9关 EV | 打平≥关数 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for label, r in out["x_rule"].items():
            L.append(f"| {label} | {r['n']} | {r['days']} | {r['n']/r['days']:.1f} | {r['y']:.3f} | "
                     f"[{r['y_lo']:.3f},{r['y_hi']:.3f}] | {r['ev5']:+.0%} | {r['ev9']:+.0%} | "
                     f"{r['m_star']:.1f} |")
    if out.get("winrate_table"):
        L.append("\n## 2b. 赢率对照（奖池隐含 / 锐市场 / 实测）\n")
        L.append("| 桶 | n | 奖池隐含赢率 | 锐市场赢率 | 实测赢率 | 中奖均SP | y | y 95%CI |")
        L.append("|---|---|---|---|---|---|---|---|")
        for b, r in out["winrate_table"].items():
            L.append(f"| {b} | {r['n']} | {r['p_pool']:.3f} | {r['p_sharp']:.3f} | "
                     f"{r['p_real']:.3f} | {r['sp_win']:.2f} | {r['y']:.3f} | "
                     f"[{r['y_lo']:.3f},{r['y_hi']:.3f}] |")
    L.append("\n## 3. 打平所需关数（实测 y）\n")
    L.append("| 桶 | y | 打平关数 |")
    L.append("|---|---|---|")
    for b, r in out.get("ev_table", {}).items():
        L.append(f"| {b} | {r['y']:.4f} | ≥{r['m_star']:.1f} |")
    if out.get("realized_time"):
        L.append("\n## 4. 好桶的时间稳健性（4–6 档）\n")
        L.append("| 区间 | n | y | 5关 EV |")
        L.append("|---|---|---|---|")
        for label, r in out["realized_time"].items():
            L.append(f"| {label} | {r['n']} | {r['y']:.4f} | {r['ev5']:+.0%} |")
    L.append("\n## 5. 稳健性（x 口径）\n")
    L.append("| 组 | n(腿) | x | 95% CI | 判定 |")
    L.append("|---|---|---|---|---|")
    for label, d in out.get("robust", {}).items():
        if d.get("n", 0) < 20:
            continue
        L.append(f"| {label} | {d['n']} | {d['mean']:.4f} | "
                 f"[{d['lo']:.3f}, {d['hi']:.3f}] | {verdict(d)} |")
    path.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
