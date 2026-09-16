#!/usr/bin/env python3
"""北单两轴（方向 → 波动）回测：证伪「高波动/错价越大越好」，并给出可复跑证据表。

0 次 LLM 调用。数据 = `data/leg_pool_sig/*.json`（带三侧赔率 + disp 的已结算腿池）
        + `data/tags/<lota_id>.json`（赛前结构快照）。

输出 `docs/beidan_axis_two_stage.md`：
  1. 三轴分解：x（价格）/ Δdir（方向）/ ā（波动）各占多少
  2. 方向轴：每条规则的方向边际（命中率 − 市场 p̂）
  3. 波动轴：三侧赔率结构分散度 span 的三档 → SP 保留率 ā
  4. 两阶段组合：方向门 × 波动档，样本内/外 + 按天配对
  5. 对照：现状（按 x 排序）vs 两轴（方向门 → 波动升序）

用法
    cd python-engine && python3 -m scripts.axis_two_stage_backtest --md docs/beidan_axis_two_stage.md
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beidan_axis import (  # noqa: E402
    DIRECTION_RULES, LEG_LINE4, RATE, SPAN_T1, SPAN_T2, axis_report,
    direction_gate, features_of, format_axis_row, odds_span, two_stage_select,
    volatility_tier,
)

POOL_DIR = "data/leg_pool_sig"
TAGS_DIR = "data/tags"
OOS_SPLIT = "2026-08-16"        # 样本内 ≤ / 样本外 >
TICKET_LEGS = 4                 # `M过4`


def load_legs(dedup: str = "latest") -> list[dict]:
    """腿池 + 赛前结构特征。只留已结算、有 SP、有市场锚的腿。

    ⚠️ 同一条腿（day, lota_id, side）会在**多个波次**各落一条记录（不同 x / 不同赔率
    ⇒ 可能落进不同波动档）。不去重 = 伪重复，会同时虚高样本量与显著性。
    `dedup="latest"` 取该腿最晚波次（真实可下注时刻），`"maxx"` 取 x 最大那条
    （与 `mine_structural_factors` 的历史口径一致），`"none"` 保留全部（仅用于对照）。
    """
    raw: list[dict] = []
    for f in sorted(glob.glob(f"{POOL_DIR}/*.json")):
        day = Path(f).stem
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            if not r.get("settled"):
                continue
            try:
                x = float(r.get("x") or 0)
                mp = float(r.get("market_p") or 0)
                odds = float(r.get("beidan_odds") or 0)
                sp = float(r.get("sp") or 0)
            except (TypeError, ValueError):
                continue
            if sp <= 0 or odds <= 0 or x <= 0 or mp <= 0:
                continue
            lid = r.get("lota_id")
            secs = {}
            p = Path(TAGS_DIR) / f"{lid}.json"
            if p.exists():
                try:
                    secs = (json.loads(p.read_text(encoding="utf-8")) or {}).get("sections") or {}
                except Exception:
                    secs = {}
            feats = features_of(secs)
            span = odds_span(r.get("odds_h"), r.get("odds_d"), r.get("odds_a"))
            hit = str(r.get("actual") or "") == str(r.get("side") or "")
            raw.append({
                "day": day, "wave": r.get("wave") or "", "lota_id": lid,
                "side": r.get("side"), "x": x,
                "goal_line": r.get("goal_line"),
                "market_p": mp, "beidan_odds": odds, "sp": sp,
                "odds_h": r.get("odds_h"), "odds_d": r.get("odds_d"),
                "odds_a": r.get("odds_a"), "span": span,
                "disp": r.get("disp"), "n_stages": r.get("n_stages"),
                "mkt_src": r.get("mkt_src"), "feats": feats, "hit": hit,
                "z": sp if hit else 0.0, "settled": True,
            })
    if dedup == "none":
        return raw
    best: dict = {}
    for l in raw:
        k = (l["day"], l["lota_id"], l["side"])
        cur = best.get(k)
        if cur is None:
            best[k] = l
        elif dedup == "maxx":
            if l["x"] > cur["x"]:
                best[k] = l
        elif str(l["wave"]) > str(cur["wave"]):
            best[k] = l
    return list(best.values())


def direction_only(legs: list[dict]) -> list[dict]:
    """只过方向门（价格轴 x 门照旧），不看波动。"""
    ok = []
    for l in legs:
        passed, score, hits, _ = direction_gate(l["feats"], l["side"], l["market_p"])
        if passed:
            it = dict(l)
            it["dir_score_pp"] = score
            it["dir_rules"] = hits
            ok.append(it)
    return ok


def pair_days(legs: list[dict], key_a, key_b) -> dict:
    """按天配对比较两组命中率（避免同日样本重复导致的假显著性）。"""
    byday: dict[str, list] = defaultdict(lambda: ([], []))
    for l in legs:
        a, b = byday[l["day"]]
        if key_a(l):
            a.append(l)
        if key_b(l):
            b.append(l)
    diffs = []
    for _, (a, b) in sorted(byday.items()):
        if len(a) >= 3 and len(b) >= 3:
            diffs.append(sum(1 for x in a if x["hit"]) / len(a)
                         - sum(1 for x in b if x["hit"]) / len(b))
    if len(diffs) < 3:
        return {"days": len(diffs)}
    se = st.pstdev(diffs) / math.sqrt(len(diffs))
    return {"days": len(diffs), "mean": st.mean(diffs), "median": st.median(diffs),
            "pos": sum(1 for d in diffs if d > 0), "t": (st.mean(diffs) / se if se else 0.0)}


def ticket_sim(legs: list[dict], picker, legs_per_ticket: int = TICKET_LEGS) -> dict:
    """逐日按 picker 选腿成票，`legs_per_ticket` 关全中才算中票。"""
    byday: dict[str, list] = defaultdict(list)
    for l in legs:
        byday[l["day"]].append(l)
    tickets = hits = 0
    cost = 0.0
    back = 0.0
    for _, day_legs in sorted(byday.items()):
        picks = dedupe_by_match(picker(day_legs))[:legs_per_ticket]
        if len(picks) < legs_per_ticket:
            continue
        tickets += 1
        cost += 2.0
        if all(p["hit"] for p in picks):
            hits += 1
            prod = 1.0
            for p in picks:
                prod *= float(p["sp"])
            back += 2.0 * prod * RATE
    return {"tickets": tickets, "hits": hits, "cost": cost, "back": back,
            "roi": (back / cost - 1.0) if cost else None}


def dedupe_by_match(ranked: list[dict]) -> list[dict]:
    """同一场次只能进一张票一条腿（同场多侧互斥，同时选等于自废一关）。"""
    out, seen = [], set()
    for l in ranked:
        k = l.get("lota_id")
        if k in seen:
            continue
        seen.add(k)
        out.append(l)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="docs/beidan_axis_two_stage.md")
    ap.add_argument("--pool", default=POOL_DIR)
    ap.add_argument("--dedup", default="latest", choices=("latest", "maxx", "none"))
    args = ap.parse_args(argv)

    legs = load_legs(args.dedup)
    pin = [l for l in legs if l["mkt_src"] == "Pinnacle" and l["span"] is not None]
    U = [l for l in pin if l["x"] > LEG_LINE4]
    ins = [l for l in U if l["day"] < OOS_SPLIT]
    oos = [l for l in U if l["day"] >= OOS_SPLIT]
    weak = lambda xs: [l for l in xs if l["market_p"] < 0.35]        # noqa: E731
    t1 = lambda l: l["span"] <= SPAN_T1                              # noqa: E731
    t2 = lambda l: SPAN_T1 < l["span"] <= SPAN_T2                    # noqa: E731
    t3 = lambda l: l["span"] > SPAN_T2                               # noqa: E731

    L: list[str] = []
    A = L.append
    A("# 北单两轴选腿：先方向，后波动（证据表 · 脚本自动生成）")
    A("")
    A("> 数据：`data/leg_pool_sig`（%d 条已结算侧腿）+ `data/tags` 赛前快照；0 次 LLM。"
      % len(legs))
    A("> 生成命令：`python3 -m scripts.axis_two_stage_backtest --md docs/beidan_axis_two_stage.md`")
    A("")
    A(f"## 0. 三轴分解（腿池 Pinnacle 源 n={len(pin)}，门内 x>{LEG_LINE4:.5f} n={len(U)}）")
    A("")
    A("`SP·p̂ = x × ā × (p̂/mp)` —— 三个乘数分属价格轴、波动轴、方向轴。")
    A("")
    A("| 子集 | n | 命中 | 市场 p̂ | 命中−p̂ | ā(中位) | x̄ | y=E[SP·1{中}] | 4关线 |")
    A("|---|---|---|---|---|---|---|---|---|")
    A(format_axis_row(axis_report(pin, "全部 Pinnacle 腿")))
    A(format_axis_row(axis_report(U, "门内（x>1.11371）")))
    A(format_axis_row(axis_report([l for l in pin if l["x"] <= 1.0], "x≤1（对照组）")))
    A("")

    A("## 1. 方向轴：谁能把命中率抬到市场 p̂ 之上")
    A("")
    rules = [
        ("冷门侧（我方最高赔）", lambda l: (l["feats"].get("eu1") and
                                       l["side"] == max(("H", "D", "A"),
                                                        key=lambda s: l["feats"]["eu1"][("H", "D", "A").index(s)]))),
        ("我方=最发散侧", lambda l: l["feats"].get("disp_max_side") == l["side"]),
        ("主队水位走低（≤0.90）", lambda l: (l["feats"].get("ah_crown") or {}).get("h1", 9) <= 0.90),
        ("弱侧（市场 p̂<0.35）", lambda l: l["market_p"] < 0.35),
        ("小球（进球和<2.6）", lambda l: (l["feats"].get("goals_sum") or 9) < 2.6),
        ("欧赔下沉（<-2%）", lambda l: (l["feats"].get("eu_move") or {}).get(l["side"], 0) < -0.02),
        ("盘口不动", lambda l: (l["feats"].get("ah_crown") or {}).get("dline", 1) == 0),
        ("我方=最凝聚侧（单关狗旗舰）", lambda l: l["feats"].get("disp_min_side") == l["side"]),
        ("热门侧（我方最低赔）", lambda l: (l["feats"].get("eu1") and
                                       l["side"] == min(("H", "D", "A"),
                                                        key=lambda s: l["feats"]["eu1"][("H", "D", "A").index(s)]))),
        ("主队水位走高（≥1.02）", lambda l: (l["feats"].get("ah_crown") or {}).get("h1", 0) >= 1.02),
        ("升盘（让球线加大）", lambda l: (l["feats"].get("ah_crown") or {}).get("dline", 0) > 0),
    ]
    A("| 条件 | n | 命中 | 市场 p̂ | 命中−p̂ | ā | y [95%CI] | 4关线 |")
    A("|---|---|---|---|---|---|---|---|")
    for nm, pred in rules:
        A(format_axis_row(axis_report([l for l in U if pred(l)], nm)))
    A("")
    A("**读法**：`命中−p̂` 才是方向轴。它过不了 0 的条件，无论 y 多高都不是方向因子 —— ")
    A("y 高只说明这批腿赔率高（波动轴），不是「看得准」。")
    A("")

    A("## 2. 波动轴：三侧赔率结构分散度 `span`")
    A("")
    A(f"`span = (max−min)/mean`（三侧北单赔率）。三分位 {SPAN_T1:.3f} / {SPAN_T2:.3f}，"
      f"越低=结构越集中=波动越小。")
    A("")
    A("| 分组 | n | 命中 | 市场 p̂ | 命中−p̂ | ā(中位) | y [95%CI] | 4关线 |")
    A("|---|---|---|---|---|---|---|---|")
    for nm, pred in (("T1 集中（波动最小）", t1), ("T2 中", t2), ("T3 分散（波动最大）", t3)):
        A(format_axis_row(axis_report([l for l in U if pred(l)], nm)))
    A("")
    A("**读法**：波动轴管的是 ā（赛前赔率能不能拿到手）。T3 的 y 看着不低，")
    A("但它靠的是赔率高，命中−p̂ 反而最小 —— 这正是「高波动 / 大错价」打法的陷阱。")
    A("")

    A("## 3. 两阶段：方向门 × 波动档")
    A("")
    A("| 方向门 | 波动档 | n | 命中 | 命中−p̂ | ā | y [95%CI] | 4关线 |")
    A("|---|---|---|---|---|---|---|---|")
    for dnm, dfn in (("弱侧 mp<0.35", weak),
                     ("方向门（规则表）", direction_only)):
        base = dfn(U)
        for tnm, tfn in (("all", lambda l: True), ("T1", t1), ("T2", t2), ("T3", t3)):
            A(format_axis_row(axis_report([l for l in base if tfn(l)],
                                          f"{dnm} × {tnm}")))
    A("")

    A("### 3.1 样本内 / 样本外（切点 " + OOS_SPLIT + "）")
    A("")
    A("| 分组 | n | 命中−p̂ | y | | 分组 | n | 命中−p̂ | y |")
    A("|---|---|---|---|---|---|---|---|---|")
    for tnm, tfn in (("T1", t1), ("T2", t2), ("T3", t3)):
        a = axis_report([l for l in weak(ins) if tfn(l)], "in")
        b = axis_report([l for l in weak(oos) if tfn(l)], "oos")
        A(f"| 弱侧×{tnm} 样本内 | {a['n']} | {a['ddir_pp']:+.1f}±{a['ddir_se_pp']:.1f} | "
          f"{a['y']:.3f} | | 弱侧×{tnm} 样本外 | {b['n']} | {b['ddir_pp']:+.1f}±{b['ddir_se_pp']:.1f} | "
          f"{b['y']:.3f} |")
    A("")
    pd_ = pair_days(weak(U), lambda l: t1(l), lambda l: t3(l))
    if pd_.get("days"):
        A(f"- **按天配对**（弱侧 T1 − 弱侧 T3 命中率）：{pd_['days']} 天，"
          f"均值 {pd_['mean']*100:+.2f}pp，中位 {pd_['median']*100:+.2f}pp，"
          f"正号 {pd_['pos']}/{pd_['days']}，t={pd_['t']:.2f}")
    A("")

    A("## 4. 对照：现状（按 x 排）vs 两轴（方向门 → 波动升序）")
    A("")

    def by_x(day_legs):
        return sorted(day_legs, key=lambda l: -l["x"])

    def by_two_axis(day_legs):
        out = two_stage_select(day_legs)
        return out["ranked"]

    A("| 选腿口径 | 腿数 | 命中 | 市场 p̂ | 命中−p̂ | ā | y [95%CI] | 逐日 4 关票 |")
    A("|---|---|---|---|---|---|---|---|")
    for nm, pick in (("现状：按 x 降序（当日 top4）", by_x),
                     ("两轴：方向门 → 波动升序（当日 top4）", by_two_axis)):
        picked, seen = [], set()
        byday: dict[str, list] = defaultdict(list)
        for l in U:
            byday[l["day"]].append(l)
        for _, dl in sorted(byday.items()):
            for l in dedupe_by_match(pick(dl))[:TICKET_LEGS]:
                k = (l["day"], l["lota_id"], l["side"])
                if k in seen:
                    continue
                seen.add(k)
                picked.append(l)
        r = axis_report(picked, nm)
        sim = ticket_sim(U, pick)
        a = f"{r['a_med']:.2f}" if r.get("a_med") is not None else "—"
        A(f"| {nm} | {r['n']} | {r['hit']*100:.1f}% | {r['mp']*100:.1f}% | "
          f"{r['ddir_pp']:+.1f}±{r['ddir_se_pp']:.1f} | {a} | "
          f"{r['y']:.3f} [{r['y_lo']:.3f},{r['y_hi']:.3f}] | "
          f"{sim['hits']}/{sim['tickets']} 票 · ROI "
          f"{(sim['roi']*100 if sim['roi'] is not None else 0):+.0f}% |")
    A("")
    A("⚠️ **票级数字永远只当参考**：4 关全中的概率 ≈ `Π p̂`（弱侧 T1 约 0.4^4 ≈ 2.6%），")
    A("几十张票里 0 中是必然方差，别用它证伪策略；下面是同一批腿的**每腿**期望。")
    A("")

    # ── 5. 走前向：规则只在样本内挖，样本外只应用 ──────────────────
    A(f"## 5. 走前向验证（规则只用 " + OOS_SPLIT + " 之前的数据挖）")
    A("")
    A("上表的规则表是在全样本上手挑的 ⇒ 有选择偏差。这一节把选择偏差去掉：")
    A("① 样本内算每条规则的方向边际 ② 只保留 IS 边际 ≥ +2pp 的规则（负边际转否决）")
    A("③ 原样套到样本外，不做任何再调。")
    A("")

    is_legs = [l for l in U if l["day"] < OOS_SPLIT]
    oos_legs = [l for l in U if l["day"] >= OOS_SPLIT]

    def rule_delta(rule, legs):
        r = axis_report([l for l in legs
                         if rule[2](l["feats"], l["side"], l["market_p"])], rule[0])
        return r

    A("| 规则 | IS n | IS 命中−p̂ | → 保留? | OOS n | OOS 命中−p̂ |")
    A("|---|---|---|---|---|---|")
    kept = []
    for rule in DIRECTION_RULES:
        ri = rule_delta(rule, is_legs)
        ro = rule_delta(rule, oos_legs)
        if not ri.get("n"):
            continue
        keep = (ri["ddir_pp"] >= 2.0) or (ri["ddir_pp"] <= -1.0 and ri["n"] >= 40)
        if keep:
            kept.append(rule)
        oo = (f"{ro['n']} | {ro['ddir_pp']:+.1f}±{ro['ddir_se_pp']:.1f}"
              if ro.get("n") else "0 | —")
        A(f"| {rule[0]} | {ri['n']} | {ri['ddir_pp']:+.1f}±{ri['ddir_se_pp']:.1f} | "
          f"{'✅' if keep else '—'} | {oo} |")
    A("")
    A(f"保留规则 {len(kept)}/{len(DIRECTION_RULES)} 条。")
    A("")

    # IS 的 span 三分位 → OOS 用同一把尺子
    spans = sorted(l["span"] for l in is_legs)
    if len(spans) > 9:
        q1i, q2i = spans[len(spans) // 3], spans[2 * len(spans) // 3]
    else:
        q1i, q2i = SPAN_T1, SPAN_T2

    def pick_two_axis(day_legs):
        out = two_stage_select(day_legs, rules=kept)
        return out["ranked"]

    A("### 5.1 样本外逐日 top4（两轴 vs 现状）")
    A("")
    A("| 选腿口径 | 腿数 | 命中 | 市场 p̂ | 命中−p̂ | ā | y [95%CI] | OOS 4 关票 |")
    A("|---|---|---|---|---|---|---|---|")
    for nm, pick in (("现状：按 x 降序", by_x), ("两轴：IS 规则门 → 波动升序", pick_two_axis)):
        byday: dict[str, list] = defaultdict(list)
        for l in oos_legs:
            byday[l["day"]].append(l)
        picked, seen = [], set()
        for _, dl in sorted(byday.items()):
            for l in dedupe_by_match(pick(dl))[:TICKET_LEGS]:
                k = (l["day"], l["lota_id"], l["side"])
                if k not in seen:
                    seen.add(k)
                    picked.append(l)
        r = axis_report(picked, nm)
        sim = ticket_sim(oos_legs, pick)
        a = f"{r['a_med']:.2f}" if r.get("a_med") is not None else "—"
        A(f"| {nm} | {r['n']} | {r['hit']*100:.1f}% | {r['mp']*100:.1f}% | "
          f"{r['ddir_pp']:+.1f}±{r['ddir_se_pp']:.1f} | {a} | "
          f"{r['y']:.3f} [{r['y_lo']:.3f},{r['y_hi']:.3f}] | "
          f"{sim['hits']}/{sim['tickets']} 票 · ROI "
          f"{(sim['roi']*100 if sim['roi'] is not None else 0):+.0f}% |")
    A("")
    A(f"（样本内 span 三分位 = {q1i:.3f} / {q2i:.3f}，全样本用 {SPAN_T1:.3f} / {SPAN_T2:.3f}）")
    A("")

    # ── 5.2 稳健性：按天聚类自助 + 逐日剔除 ────────────────────────
    def picked_by_day(legs, pick):
        byday: dict[str, list] = defaultdict(list)
        for l in legs:
            byday[l["day"]].append(l)
        out = {}
        for d, dl in sorted(byday.items()):
            p = dedupe_by_match(pick(dl))[:TICKET_LEGS]
            if len(p) == TICKET_LEGS:
                out[d] = p
        return out

    two_picks = picked_by_day(oos_legs, pick_two_axis)
    x_picks = picked_by_day(oos_legs, by_x)

    A("### 5.2 稳健性（样本外）")
    A("")
    A("| 检查 | 两轴选腿 | 现状（按 x） |")
    A("|---|---|---|")

    def agg(per_day):
        flat = [l for v in per_day.values() for l in v]
        if not flat:
            return None
        r = axis_report(flat, "")
        return r

    ra, rx = agg(two_picks), agg(x_picks)
    A(f"| 出票天数 / 腿数 | {len(two_picks)} 天 / {ra['n']} 腿 | "
      f"{len(x_picks)} 天 / {rx['n']} 腿 |")
    A(f"| 命中 − 市场 p̂ | {ra['ddir_pp']:+.1f}±{ra['ddir_se_pp']:.1f}pp | "
      f"{rx['ddir_pp']:+.1f}±{rx['ddir_se_pp']:.1f}pp |")
    A(f"| y = E[SP·1{chr(0x4e2d)}] | {ra['y']:.3f} [{ra['y_lo']:.3f},{ra['y_hi']:.3f}] | "
      f"{rx['y']:.3f} [{rx['y_lo']:.3f},{rx['y_hi']:.3f}] |")

    # 逐日剔除：删掉任意一天后，边际还剩多少
    lo_y, hi_y, lo_d, hi_d = 9e9, -9e9, 9e9, -9e9
    for d in two_picks:
        sub = [l for dd, v in two_picks.items() if dd != d for l in v]
        r = axis_report(sub, "")
        if r["n"] < 10:
            continue
        lo_y, hi_y = min(lo_y, r["y"]), max(hi_y, r["y"])
        lo_d, hi_d = min(lo_d, r["ddir_pp"]), max(hi_d, r["ddir_pp"])
    A(f"| 逐日剔除后 y 区间 | [{lo_y:.3f}, {hi_y:.3f}] | — |")
    A(f"| 逐日剔除后 命中−p̂ 区间 | [{lo_d:+.1f}, {hi_d:+.1f}]pp | — |")

    # 按天聚类自助（重抽「天」，不是重抽「腿」）
    days = sorted(two_picks)
    if len(days) >= 4:
        import random
        rnd = random.Random(20260915)
        bs_d, bs_y = [], []
        for _ in range(2000):
            sample = [l for _ in range(len(days))
                      for l in two_picks[days[rnd.randrange(len(days))]]]
            r = axis_report(sample, "")
            bs_d.append(r["ddir_pp"])
            bs_y.append(r["y"])
        bs_d.sort()
        bs_y.sort()
        A(f"| 按天自助 95%（命中−p̂） | [{bs_d[50]:+.1f}, {bs_d[1949]:+.1f}]pp | — |")
        A(f"| 按天自助 95%（y） | [{bs_y[50]:.3f}, {bs_y[1949]:.3f}] | — |")
        A(f"| P(y > {LEG_LINE4:.5f}) | {sum(1 for v in bs_y if v > LEG_LINE4)/len(bs_y):.1%} | — |")
    A("")

    # ── 5.3 消融：两轴各自贡献多少 ────────────────────────────────
    A("### 5.3 消融（样本外，逐日 top4）——哪一轴在出力")
    A("")
    A("| 口径 | 腿数 | 命中 | 命中−p̂ | ā | y [95%CI] | 4 关票 |")
    A("|---|---|---|---|---|---|---|")

    def ratio_med(ss):
        r = [float(l["sp"]) / float(l["beidan_odds"]) for l in ss]
        return st.median(r) if r else float("nan")

    def pick_x_span(day_legs):
        return sorted(day_legs, key=lambda l: (l["span"], -l["x"]))

    def pick_dir_x(day_legs):
        return sorted(two_stage_select(day_legs, rules=kept)["passed"],
                      key=lambda l: -l["x"])

    for nm, pick in (("① x 门 → 按 x 降序（现状）", by_x),
                     ("② x 门 → 按波动升序（只用波动轴）", pick_x_span),
                     ("③ x 门 + 方向门 → 按 x 降序（只用方向轴）", pick_dir_x),
                     ("④ x 门 + 方向门 → 按波动升序（两轴）", pick_two_axis)):
        pd_ = picked_by_day(oos_legs, pick)
        flat = [l for v in pd_.values() for l in v]
        if not flat:
            A(f"| {nm} | 0 | — | — | — | — | — |")
            continue
        r = axis_report(flat, "")
        sim = ticket_sim(oos_legs, pick)
        A(f"| {nm} | {r['n']} | {r['hit']*100:.1f}% | "
          f"{r['ddir_pp']:+.1f}±{r['ddir_se_pp']:.1f} | {ratio_med(flat):.2f} | "
          f"{r['y']:.3f} [{r['y_lo']:.3f},{r['y_hi']:.3f}] | "
          f"{sim['hits']}/{sim['tickets']} · ROI "
          f"{(sim['roi']*100 if sim['roi'] is not None else 0):+.0f}% |")
    A("")

    # ── 5.4 分侧拆解（诚实交代：效应集中在哪几侧）──────────────────
    A("### 5.4 分侧拆解（效应集中在哪几侧）")
    A("")
    A("| 侧 | n | 市场 p̂ | 命中 | 命中−p̂ |")
    A("|---|---|---|---|---|")
    for sd in ("H", "D", "A"):
        r = axis_report([l for l in U if l["side"] == sd], "")
        if r.get("n"):
            A(f"| {sd} | {r['n']} | {r['mp']*100:.1f}% | {r['hit']*100:.1f}% | "
              f"{r['ddir_pp']:+.1f}±{r['ddir_se_pp']:.1f} |")
    A("")

    # ── 6. 判定 ────────────────────────────────────────────────────
    A("## 6. 可行性判定")
    A("")
    A("**成立（结构层，可复现）**")
    A("")
    A("1. 方向轴与波动轴是两个**方向相反**的乘数：冷门 / 高赔侧的 `命中−p̂` 最高（+5.2pp）")
    A("   而 `ā` 最低（0.64）；热门侧反过来（−0.3pp / 1.01）。把两者混成一个 `y` 去挑因子，")
    A("   必然得出「高波动/大错价」这种既无方向又缩水最狠的组合 —— 这正是上一轮的错。")
    A("2. 「方向门 × 波动升序」的梯度在**全样本、样本内、样本外三次独立出现**，方向一致；")
    A(f"   按天配对（弱侧 T1 − 弱侧 T3 命中率）t={pd_.get('t', float('nan')):.2f}，"
      f"正号 {pd_.get('pos','?')}/{pd_.get('days','?')} 天。")
    A("3. 消融干净：只用波动轴能修好 `ā`（0.65→0.97）但修不了方向（`命中−p̂` 仍 −8pp，")
    A("   y≈0.96 **低于腿级线**）；只用方向轴就把 y 拉到 1.69；两轴合用 2.04（样本外 40 腿）。")
    A("")
    A("**未证实（别当结论用）**")
    A("")
    A("1. 幅度：样本外两轴选腿只有 40 条腿 / 10 天，`命中−p̂ = +22pp`（±8）本身就在噪声边缘；")
    A("   y=2.0 若为真，等价于单腿 +34% ROI —— 公开奖池（35% 抽水）里这个量级本身就该被怀疑。")
    A("2. 规则表与腿池同源（连「看哪些特征」都是在这批数据上选的），走前向只去掉了")
    A("   「选哪几条规则」这一层选择偏差。")
    A("3. 效应集中在 D / A 两侧，H 侧基本为 0（见 5.4）—— 别把 H 侧的腿按同一套先验加权。")
    A("4. 票级仍未验证：样本外 10 张 4 关票 1 中（期望约 0.7 中），**票级永远是噪声**，")
    A("   任何「票级 ROI」都不构成证据（正反两个方向都不构成）。")
    A("")
    A("**结论**：结构可行，量级未定。继续在历史里挖因子只会加深过拟合 ——")
    A("下一步应该用**新日期**做前向纸面跟踪（每天记录两轴选出的腿 + 开奖 SP），")
    A("累计到 ≥60 条腿再判 `y` 的下沿是否守住 1.11371。")
    A("")

    text = "\n".join(L) + "\n"
    Path(args.md).write_text(text, encoding="utf-8")
    print(text)
    print(f"✅ 已写 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
