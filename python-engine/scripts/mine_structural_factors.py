#!/usr/bin/env python3
"""结构性因子离线挖掘（0 次 LLM 调用）。

目标口径（用户 2026-09-15）：本狗玩 `M过4`（M>4）票型 ⇒ **腿级线 = (1/0.65)^(1/4) = 1.11371**
（整票只收一次 0.65）。所以每条腿的标签是 `z = SP·1{中}`、`y = E[z]`：
只有 y 的置信下沿 > 1.11371 的结构性条件，才值得写成因子。

先验来自单关狗已验证的结构因子族（盘口移动 / 离散凝聚 / 水位 / 资金结构），
逐个落到北单腿级上量一遍：
    离散凝聚（min 侧 / 我方是否凝聚 / 凝聚度）
    亚盘升退（让球线首→末变化） × 水位（低水/高水）× 水位移动
    欧赔下沉/上升（我方赔率首→末变化）
    必发资金（盈亏指数 / 成交量 / 凯利）
    实力差 / 深盘 / 预期进球

用法
    python3 -m scripts.mine_structural_factors --md docs/structural_factor_mine.md
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import re
import statistics as st
from collections import defaultdict
from pathlib import Path

RATE = 0.65
LINE4 = (1.0 / RATE) ** 0.25        # 1.11371
LINE1 = 1.0 / RATE                  # 1.5385

SIDES = ("H", "D", "A")
_SIDE_IDX = {"H": 0, "D": 1, "A": 2}

# 亚盘行：Δt+123m↑→↓↓0.94/半球/0.94/97.00   （首行带 r96.98%）
_AH_ROW = re.compile(
    r"(?:OPt[-\d]+m=|Δt[+-]\d+m)(?:[↑↓→]+)?"
    r"([\d.]+)/([^/]+)/([\d.]+)(?:/\(?r?([\d.]+)%?\)?)?")
# 欧赔行：OPt-10067m=1.92/3.55/3.84(r94.08%)
_EU_ROW = re.compile(r"(?:OPt[-\d]+m=|Δt[+-]\d+m)(?:[↑↓→]+)?([\d.]+)/([\d.]+)/([\d.]+)")
# 离散行：OPt-1434m=3.99/15.18/0.24
_DIS_ROW = re.compile(r"(?:OPt[-\d]+m=|Δt[+-]\d+m)(?:[↑↓→]+)?([\d.]+)/([\d.]+)/([\d.]+)")

_HANDICAP = {"平手": 0.0, "平半": 0.25, "半球": 0.5, "半一": 0.75, "一球": 1.0,
             "一球半": 1.5, "球半": 1.5, "两球": 2.0, "两球半": 2.5, "三球": 3.0}
_TAGS_DIR = Path("data/tags")


def handicap_val(tok: str) -> float | None:
    t = (tok or "").strip()
    if not t:
        return None
    sign = 1.0
    if t.startswith("受"):
        sign, t = -1.0, t[1:]
    for k, v in _HANDICAP.items():
        if k in t:
            return sign * v
    return None


def rows_of(text: str, rx) -> list[list[float]]:
    out = []
    for m in rx.finditer(text or ""):
        try:
            out.append([float(g) if g is not None else float("nan") for g in m.groups()])
        except (TypeError, ValueError):
            continue
    return out


def ah_series(text: str) -> list[tuple[float, float, float, float]]:
    """[(h_water, line, a_water, rrr)]，按文本顺序（首=最早）。"""
    out = []
    for m in re.finditer(r"(?:OPt[-\d]+m=|Δt[+-]\d+m)(?:[↑↓→]+)?"
                         r"([\d.]+)/([^/]+)/([\d.]+)", text or ""):
        line = handicap_val(m.group(2))
        if line is None:
            continue
        try:
            out.append((float(m.group(1)), line, float(m.group(3)), 0.0))
        except ValueError:
            continue
    return out


def load_legs(dedup: bool = True) -> list[dict]:
    seen: dict = {}
    for f in sorted(glob.glob("data/leg_pool_all/*.json")):
        day = Path(f).stem
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            if not r.get("settled"):
                continue
            try:
                sp = float(r.get("sp") or 0); od = float(r.get("beidan_odds") or 0)
                x = float(r.get("x") or 0); mp = float(r.get("market_p") or 0)
            except (TypeError, ValueError):
                continue
            if sp <= 0 or od <= 0 or x <= 0 or mp <= 0:
                continue
            hit = str(r.get("actual") or "") == str(r.get("side") or "")
            rec = {"day": day, "lid": r.get("lota_id"), "side": r.get("side"),
                   "x": x, "odds": od, "mp": mp, "sp": sp, "hit": hit,
                   "z": sp if hit else 0.0, "gl": r.get("goal_line"),
                   "src": r.get("mkt_src")}
            if dedup:
                k = (day, rec["lid"], rec["side"])
                if k not in seen or rec["x"] > seen[k]["x"]:
                    seen[k] = rec
            else:
                seen[(day, rec["lid"], rec["side"], len(seen))] = rec
    return list(seen.values())


def features(lid: str) -> dict:
    p = _TAGS_DIR / f"{lid}.json"
    if not p.exists():
        return {}
    try:
        secs = (json.loads(p.read_text(encoding="utf-8")).get("sections") or {})
    except Exception:
        return {}
    f: dict = {}

    # ── 离散指数：首行 / 末行 ──
    dis = rows_of(secs.get("discrete-odds") or "", _DIS_ROW)
    if len(dis) >= 2:
        d0, d1 = dis[0], dis[-1]
        f["disp_first"], f["disp_last"] = d0, d1
        f["disp_min_side"] = SIDES[min(range(3), key=lambda i: d1[i])]
        f["disp_max_side"] = SIDES[max(range(3), key=lambda i: d1[i])]
        mn = min(d1)
        f["disp_ratio"] = {s: (d1[i] / mn if mn > 0 else float("inf"))
                           for i, s in enumerate(SIDES)}
        f["disp_trend"] = {s: (d1[i] - d0[i]) for i, s in enumerate(SIDES)}
        f["disp_conc"] = bool(mn > 0 and max(d1) / mn >= 5.0)   # 单边极致凝聚

    # ── 亚盘：Crown / Pinnacle 首末行 ──
    for name, key in (("crown", "asian-handicap-crown"),
                      ("pinnacle", "asian-handicap-pinnacle")):
        ah = ah_series(secs.get(key) or "")
        if len(ah) >= 2:
            h0, l0, a0, _ = ah[0]
            h1, l1, a1, _ = ah[-1]
            f[f"ah_{name}"] = {"line0": l0, "line1": l1, "dline": l1 - l0,
                               "h0": h0, "h1": h1, "a0": a0, "a1": a1,
                               "dh": h1 - h0, "da": a1 - a0}

    # ── 欧赔 Pinnacle：首末行 ──
    eu = rows_of(secs.get("eu-odds-pinnacle") or "", _EU_ROW)
    if len(eu) >= 2:
        e0, e1 = eu[0], eu[-1]
        f["eu0"], f["eu1"] = e0, e1
        f["eu_move"] = {s: ((e1[i] - e0[i]) / e0[i] if e0[i] > 0 else 0.0)
                        for i, s in enumerate(SIDES)}

    # ── 必发欧盘积累：成交量 / 盈亏指数 / 市场指数 / 凯利 ──
    bf = secs.get("betfair-eu") or ""
    for label, key in (("vol", "成交量"), ("price", "价位"),
                       ("pnl", "盈亏指数"), ("mkt", "市场指数"), ("kelly", "凯利")):
        m = re.search(rf"{label}\(主/和/客\):([-\d.]+)/([-\d.]+)/([-\d.]+)", bf)
        if m:
            f[f"bf_{key}"] = [float(m.group(1)), float(m.group(2)), float(m.group(3))]

    # ── 公平盘：实力差 / 预期进球 / ELO / 让球欧盘 ──
    fair = secs.get("fair-odds") or ""
    for key, rx in (("strength_gap", r"主客实力差:\s*(-?[\d.]+)"),
                    ("goals_sum", r"主客进球和:\s*(-?[\d.]+)"),
                    ("exp_home", r"预期主队进球:\s*(-?[\d.]+)"),
                    ("exp_away", r"预期客队进球:\s*(-?[\d.]+)"),
                    ("elo_home", r"主队ELO:\s*(-?[\d.]+)"),
                    ("elo_away", r"客队ELO:\s*(-?[\d.]+)")):
        m = re.search(rx, fair)
        if m:
            f[key] = float(m.group(1))
    m = re.search(r"平均欧盘胜/平/负\(([-+]?\d+(?:\.\d+)?)\):\s*([\d.]+)/([\d.]+)/([\d.]+)", fair)
    if m:
        f["ref_gl"] = float(m.group(1))
        f["ref_odds"] = [float(m.group(2)), float(m.group(3)), float(m.group(4))]
    return f


def cond_table(legs: list[dict], feats: dict, name: str, pred, base_label: str) -> dict:
    ss = [l for l in legs if pred(l, feats.get(l["lid"]) or {})]
    if not ss:
        return {"name": name, "n": 0}
    zs = [l["z"] for l in ss]
    n = len(zs)
    y = st.mean(zs)
    se = st.pstdev(zs) / math.sqrt(n)
    return {"name": name, "n": n, "hit": sum(1 for l in ss if l["hit"]) / n,
            "x": st.mean(l["x"] for l in ss), "y": y, "lo": y - 1.96 * se,
            "hi": y + 1.96 * se, "roi": RATE * y - 1}


def row(c: dict) -> str:
    if not c.get("n"):
        return f"| {c['name']} | 0 | — | — | — | — | — |"
    ok = "✅" if c["lo"] > LINE4 else ("≈" if c["y"] > LINE4 else "❌")
    return (f"| {c['name']} | {c['n']} | {c['hit']*100:.1f}% | {c['x']:.3f} | "
            f"{c['y']:.3f} [{c['lo']:.3f},{c['hi']:.3f}] | {c['roi']:+.1%} | {ok} |")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="docs/structural_factor_mine.md")
    ap.add_argument("--min-n", type=int, default=25)
    args = ap.parse_args(argv)

    legs = load_legs()
    feats = {l["lid"]: features(l["lid"]) for l in legs}
    have = sum(1 for l in legs if feats.get(l["lid"]))
    L: list[str] = []
    A = L.append
    A("# 结构性因子离线挖掘（北单腿级 · 4 关票口径）")
    A("")
    A(f"- 腿池 {len(legs)} 条（每场每侧一条）｜有标签 {have} 条｜"
      f"腿级线 `(1/0.65)^(1/4) = {LINE4:.5f}`（**4 关票**；0.65 只在整票收一次）")
    A("- 标签 `z = 开奖SP·1{中}`、`y = E[z]`；`✅` = y 的 95% 置信**下沿**都过腿级线")
    A("")

    gate = [l for l in legs if l["x"] > LINE4]
    A("## 0. 基线与门槛")
    A("")
    A("| 子集 | n | 命中 | x̄ | y [95%CI] | 单腿每元 | 4关线 |")
    A("|---|---|---|---|---|---|---|")
    A(row(cond_table(legs, feats, "全部", lambda l, f: True, "")))
    A(row(cond_table(gate, feats, "x > 1.11371（引擎门内）", lambda l, f: True, "")))
    A(row(cond_table([l for l in gate if l["src"] == "Pinnacle"], feats,
                     "x > 1.11371 且 Pinnacle 源", lambda l, f: True, "")))
    A("")

    # 挖掘只在「引擎门内 + Pinnacle 源」做：让球欧盘那批已证实是换算产物
    P = [l for l in gate if l["src"] == "Pinnacle"]
    A(f"## 1. 结构条件（在 x>1.11371 且 Pinnacle 源的 **{len(P)}** 条里切）")
    A("")
    A("| 条件 | n | 命中 | x̄ | y [95%CI] | 单腿每元 | 4关线 |")
    A("|---|---|---|---|---|---|---|")

    def g(f, k, d=None):
        v = f.get(k, d)
        return v

    conds: list[tuple[str, callable]] = []
    # ── 离散凝聚（单关狗最核心的一族）──
    conds += [
        ("离散：我方=最凝聚侧", lambda l, f: (g(f, "disp_min_side") == l["side"])),
        ("离散：我方=最凝聚侧 且 单边极致(≥5x)", lambda l, f: (
            g(f, "disp_min_side") == l["side"] and g(f, "disp_conc"))),
        ("离散：我方=最凝聚侧 且 凝聚比≥10x", lambda l, f: (
            g(f, "disp_min_side") == l["side"]
            and (g(f, "disp_ratio", {}) or {}).get(l["side"], 0) >= 10)),
        ("离散：我方=最发散侧", lambda l, f: (g(f, "disp_max_side") == l["side"])),
        ("离散：凝聚比<2x（无凝聚）", lambda l, f: (
            (g(f, "disp_ratio", {}) or {}).get(l["side"], 99) < 2)),
        ("离散：我方凝聚值赛前上升", lambda l, f: (
            (g(f, "disp_trend", {}) or {}).get(l["side"], 0) > 0)),
        ("离散：我方凝聚值赛前下降", lambda l, f: (
            (g(f, "disp_trend", {}) or {}).get(l["side"], 0) < 0)),
    ]
    # ── 亚盘：升退 + 水位（Crown / Pinnacle 各一套）──
    for src in ("crown", "pinnacle"):
        def _ah(f, s=src):
            return g(f, f"ah_{s}") or {}
        conds += [
            (f"亚盘[{src}]：升盘（让球线加大）", lambda l, f, s=src: _ah(f, s).get("dline", 0) > 0),
            (f"亚盘[{src}]：退盘（让球线减小）", lambda l, f, s=src: _ah(f, s).get("dline", 0) < 0),
            (f"亚盘[{src}]：盘口不动", lambda l, f, s=src: _ah(f, s).get("dline", 1) == 0),
            (f"亚盘[{src}]：主队水位走低（≤0.90）", lambda l, f, s=src: _ah(f, s).get("h1", 9) <= 0.90),
            (f"亚盘[{src}]：主队水位走高（≥1.02）", lambda l, f, s=src: _ah(f, s).get("h1", 0) >= 1.02),
            (f"亚盘[{src}]：客队水位走低（≤0.90）", lambda l, f, s=src: _ah(f, s).get("a1", 9) <= 0.90),
            (f"亚盘[{src}]：我方低水受保护", lambda l, f, s=src: (
                _ah(f, s).get("h1", 9) <= 0.93 if l["side"] == "H" else
                (_ah(f, s).get("a1", 9) <= 0.93 if l["side"] == "A" else False))),
            (f"亚盘[{src}]：我方高水（诱盘）", lambda l, f, s=src: (
                _ah(f, s).get("h1", 0) >= 1.00 if l["side"] == "H" else
                (_ah(f, s).get("a1", 0) >= 1.00 if l["side"] == "A" else False))),
            (f"亚盘[{src}]：退盘 且 我方低水", lambda l, f, s=src: (
                _ah(f, s).get("dline", 0) < 0 and (
                    _ah(f, s).get("h1", 9) <= 0.93 if l["side"] == "H" else
                    (_ah(f, s).get("a1", 9) <= 0.93 if l["side"] == "A" else False)))),
            (f"亚盘[{src}]：退盘 且 我方=最凝聚", lambda l, f, s=src: (
                _ah(f, s).get("dline", 0) < 0 and g(f, "disp_min_side") == l["side"])),
            (f"亚盘[{src}]：深盘（|让球|≥1）", lambda l, f, s=src: abs(_ah(f, s).get("line1", 0)) >= 1),
            (f"亚盘[{src}]：浅盘（|让球|≤0.5）", lambda l, f, s=src: abs(_ah(f, s).get("line1", 0)) <= 0.5),
        ]
    # ── 欧赔下沉 / 上升 ──
    conds += [
        ("欧赔：我方赔率下沉（<-2%）", lambda l, f: (
            (g(f, "eu_move", {}) or {}).get(l["side"], 0) < -0.02)),
        ("欧赔：我方赔率上升（>+2%）", lambda l, f: (
            (g(f, "eu_move", {}) or {}).get(l["side"], 0) > 0.02)),
        ("欧赔：我方赔率基本不动（±2%）", lambda l, f: (
            abs((g(f, "eu_move", {}) or {}).get(l["side"], 0)) <= 0.02)),
        ("欧赔：我方=最低赔（热门侧）", lambda l, f: (
            bool(g(f, "eu1")) and _SIDE_IDX[l["side"]] == min(range(3), key=lambda i: g(f, "eu1")[i]))),
        ("欧赔：我方=最高赔（冷门侧）", lambda l, f: (
            bool(g(f, "eu1")) and _SIDE_IDX[l["side"]] == max(range(3), key=lambda i: g(f, "eu1")[i]))),
        ("欧赔：下沉 且 我方=最凝聚", lambda l, f: (
            (g(f, "eu_move", {}) or {}).get(l["side"], 0) < -0.02
            and g(f, "disp_min_side") == l["side"])),
        ("欧赔：上升 且 我方=最凝聚（赔升防冷）", lambda l, f: (
            (g(f, "eu_move", {}) or {}).get(l["side"], 0) > 0.02
            and g(f, "disp_min_side") == l["side"])),
    ]
    # ── 必发资金结构 ──
    conds += [
        ("必发：我方盈亏指数为正", lambda l, f: (
            (g(f, "bf_pnl", [0, 0, 0]) or [0, 0, 0])[_SIDE_IDX[l["side"]]] > 0)),
        ("必发：我方盈亏指数为负", lambda l, f: (
            (g(f, "bf_pnl", [0, 0, 0]) or [0, 0, 0])[_SIDE_IDX[l["side"]]] < 0)),
        ("必发：我方市场指数最高", lambda l, f: (
            bool(g(f, "bf_mkt")) and _SIDE_IDX[l["side"]] == max(range(3), key=lambda i: g(f, "bf_mkt")[i]))),
        ("必发：我方市场指数最低", lambda l, f: (
            bool(g(f, "bf_mkt")) and _SIDE_IDX[l["side"]] == min(range(3), key=lambda i: g(f, "bf_mkt")[i]))),
        ("必发：我方成交量最大", lambda l, f: (
            bool(g(f, "bf_vol")) and _SIDE_IDX[l["side"]] == max(range(3), key=lambda i: g(f, "bf_vol")[i]))),
        ("必发：我方凯利最低", lambda l, f: (
            bool(g(f, "bf_kelly")) and _SIDE_IDX[l["side"]] == min(range(3), key=lambda i: g(f, "bf_kelly")[i]))),
    ]
    # ── 实力差 / 盘口 ──
    conds += [
        ("实力差>1（碾压）", lambda l, f: abs(g(f, "strength_gap") or 0) > 1),
        ("实力差<0.5（均衡）", lambda l, f: abs(g(f, "strength_gap") or 0) < 0.5),
        ("预期进球和<2.6（小球）", lambda l, f: (g(f, "goals_sum") or 9) < 2.6),
        ("预期进球和>3.2（大球）", lambda l, f: (g(f, "goals_sum") or 0) > 3.2),
        ("我方为实力强侧", lambda l, f: (
            (g(f, "strength_gap") or 0) > 0.5 if l["side"] == "H"
            else ((g(f, "strength_gap") or 0) < -0.5 if l["side"] == "A" else False))),
        ("我方为实力弱侧", lambda l, f: (
            (g(f, "strength_gap") or 0) < -0.5 if l["side"] == "H"
            else ((g(f, "strength_gap") or 0) > 0.5 if l["side"] == "A" else False))),
    ]

    out = []
    for nm, pred in conds:
        c = cond_table(P, feats, nm, pred, "")
        if c.get("n", 0) >= args.min_n:
            out.append(c)
    out.sort(key=lambda c: -c["y"])
    for c in out:
        A(row(c))
    A("")
    A(f"（只列 n≥{args.min_n}；共试了 {len(conds)} 个条件 —— "
      f"多重比较警告：n 小的高 y 是噪声，要看 CI 下沿是否过线）")
    A("")

    # ── 组合：最凝聚 + 低水（单关狗最赚的那一族）──
    A("## 2. 组合条件（单关狗最赚的一族：凝聚 × 水位）")
    A("")
    A("| 条件 | n | 命中 | x̄ | y [95%CI] | 单腿每元 | 4关线 |")
    A("|---|---|---|---|---|---|---|")
    combos = [
        ("凝聚×低水（Crown）", lambda l, f: g(f, "disp_min_side") == l["side"] and (
            ((g(f, "ah_crown") or {}).get("h1", 9) <= 0.93 if l["side"] == "H" else
             ((g(f, "ah_crown") or {}).get("a1", 9) <= 0.93 if l["side"] == "A" else False)))),
        ("凝聚×低水（Pinnacle）", lambda l, f: g(f, "disp_min_side") == l["side"] and (
            ((g(f, "ah_pinnacle") or {}).get("h1", 9) <= 0.93 if l["side"] == "H" else
             ((g(f, "ah_pinnacle") or {}).get("a1", 9) <= 0.93 if l["side"] == "A" else False)))),
        ("凝聚×高水（诱盘）", lambda l, f: g(f, "disp_min_side") == l["side"] and (
            ((g(f, "ah_crown") or {}).get("h1", 0) >= 1.00 if l["side"] == "H" else
             ((g(f, "ah_crown") or {}).get("a1", 0) >= 1.00 if l["side"] == "A" else False)))),
        ("凝聚×深盘", lambda l, f: g(f, "disp_min_side") == l["side"]
            and abs((g(f, "ah_crown") or {}).get("line1", 0)) >= 1),
        ("凝聚×浅盘", lambda l, f: g(f, "disp_min_side") == l["side"]
            and abs((g(f, "ah_crown") or {}).get("line1", 9)) <= 0.5),
        ("凝聚×退盘", lambda l, f: g(f, "disp_min_side") == l["side"]
            and (g(f, "ah_crown") or {}).get("dline", 0) < 0),
        ("凝聚×升盘", lambda l, f: g(f, "disp_min_side") == l["side"]
            and (g(f, "ah_crown") or {}).get("dline", 0) > 0),
        ("凝聚×我方赔率下沉", lambda l, f: g(f, "disp_min_side") == l["side"]
            and (g(f, "eu_move", {}) or {}).get(l["side"], 0) < -0.02),
        ("凝聚×弱侧（p̂<0.35）", lambda l, f: g(f, "disp_min_side") == l["side"] and l["mp"] < 0.35),
        ("凝聚×弱侧 且 低水", lambda l, f: g(f, "disp_min_side") == l["side"] and l["mp"] < 0.35 and (
            ((g(f, "ah_crown") or {}).get("h1", 9) <= 0.93 if l["side"] == "H" else
             ((g(f, "ah_crown") or {}).get("a1", 9) <= 0.93 if l["side"] == "A" else False)))),
        ("弱侧（p̂<0.35）× 深盘", lambda l, f: l["mp"] < 0.35
            and abs((g(f, "ah_crown") or {}).get("line1", 0)) >= 1),
        ("弱侧（p̂<0.35）× 必发盈亏为正", lambda l, f: l["mp"] < 0.35 and (
            (g(f, "bf_pnl", [0, 0, 0]) or [0, 0, 0])[_SIDE_IDX[l["side"]]] > 0)),
    ]
    for nm, pred in combos:
        A(row(cond_table(P, feats, nm, pred, "")))
    A("")

    # ── 只按 x 分档（对照组：x 本身还有没有信息）──
    A("## 3. 对照：只按 x 分档（Pinnacle 源）")
    A("")
    A("| 档 | n | 命中 | x̄ | y [95%CI] | 单腿每元 | 4关线 |")
    A("|---|---|---|---|---|---|---|")
    for lo, hi in ((1.0, 1.11371), (1.11371, 1.25), (1.25, 1.4), (1.4, 1.6), (1.6, 9)):
        A(row(cond_table(P, feats, f"x∈({lo},{hi}]",
                         lambda l, f, lo=lo, hi=hi: lo < l["x"] <= hi, "")))
    A("")

    text = "\n".join(L) + "\n"
    Path(args.md).write_text(text, encoding="utf-8")
    print(text)
    print(f"✅ 已写 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
