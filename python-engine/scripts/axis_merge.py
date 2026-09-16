#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归纳第 1 步：阈值合并 + 切点走前向（0 LLM）。

输入 `library_<tag>.json`（`scripts/axis_library.py` 的产物：归一化去重 + 全窗口重估）。

做两件事：
  1. **同族归并**：把「结构相同、只差一个数值阈值」的条件归成一族
     （如 `span >= 0.3 / 0.487 / 0.5 / 0.55 / 0.69` 是一族）。
  2. **切点走前向**：在**拟合窗口**（前 15 天）上扫候选阈值选最优，
     再在**留出窗口**（其余天）上原样复算 —— 防"在同一批数据上挑切点"。

判据：方向轴按 |d_pp|（命中率 − 市场 p̂）最大化；波动轴按 |ā − 基线ā| 最大化；
两者都要求 n ≥ --min-n。留出窗口的数字只报不选。

用法
    python3 -m scripts.axis_merge --tag w15 --fit 2026-06-28,... --md docs/factor_mine_merged.md
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_cond_audit import _stat, build_eval_env, eval_cond   # noqa: E402
from scripts.axis_library import norm_cond                            # noqa: E402
from scripts.axis_two_stage_backtest import load_legs                 # noqa: E402
from src.beidan_axis import LEG_LINE4                                 # noqa: E402

_TERM = re.compile(r"^([a-z_][\w.]*)(==|!=|<=|>=|<|>)(-?\d+(?:\.\d+)?)$")


def atoms(cond: str):
    """条件 → [(field, op, value_str_or_field)]，同时给出骨架。"""
    norm = norm_cond(cond)
    groups = []
    for g in norm.split("或"):
        terms = []
        for t in g.split("且"):
            if not t:
                continue
            m = _TERM.match(t)
            terms.append((m.group(1), m.group(2), m.group(3)) if m
                         else (t, "", ""))
        groups.append(sorted(terms))
    skel = "或".join(sorted(
        "且".join(f"{f}{o}#" if v and _TERM.match(f"{f}{o}{v}") else f"{f}{o}{v}"
                  for f, o, v in g) for g in groups))
    flat = [t for g in groups for t in g]
    return norm, flat, skel


def _vals_of(field: str, legs: list) -> list:
    """取该字段在样本上的取值（用于给扫描补候选切点）。"""
    top = {"span": "span", "mp": "market_p", "od": "beidan_odds", "x": "x"}
    out = []
    for l in legs:
        if field in top:
            v = l.get(top[field])
        else:
            v = (l.get("feats") or {}).get(field)
        if isinstance(v, (int, float)):
            out.append(round(float(v), 3))
    return out


def sweep(cond: str, field: str, op: str, cands: list, fit: list, envf: dict,
          axis: str, base_a: float, min_n: int):
    """把 cond 里的 (field,op,*) 换成候选阈值，选拟合窗口最优的那一个。"""
    best = None
    for c in cands:
        new = re.sub(rf"{re.escape(field)}{re.escape(op)}-?\d+(?:\.\d+)?",
                     f"{field}{op}{c}", norm_cond(cond))
        try:
            ss = [l for l in fit if eval_cond(new, l, envf)]
        except Exception:                                     # noqa: BLE001
            continue
        if len(ss) < min_n:
            continue
        r = _stat(ss)
        score = abs(r["d_pp"]) if axis == "directional" else abs((r["a_med"] or 0) - base_a)
        if best is None or score > best[0]:
            best = (score, new, r, c)
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="w15")
    ap.add_argument("--fit", required=True, help="拟合窗口（逗号分隔的天）")
    ap.add_argument("--src", default="data/factor_mine_out")
    ap.add_argument("--md", default="")
    ap.add_argument("--min-n", type=int, default=15)
    args = ap.parse_args(argv)

    fit_days = [d.strip() for d in args.fit.split(",") if d.strip()]
    all_legs = load_legs("latest")
    fit = [l for l in all_legs if l["day"] in set(fit_days)]
    hold = [l for l in all_legs if l["day"] not in set(fit_days)]
    envf, envh = build_eval_env(fit), build_eval_env(hold)
    lib = json.loads((Path(args.src) / f"library_{args.tag}.json").read_text(encoding="utf-8"))

    # 同族归并
    fam = defaultdict(list)
    for r in lib:
        if not r.get("cond"):
            continue
        _, flat, skel = atoms(r["cond"])
        fam[(r["axis"], skel)].append(r)

    base_a = st.median([l["sp"] / l["beidan_odds"] for l in fit if l["beidan_odds"] > 0])
    out, merged_n = [], 0
    for (axis, skel), members in fam.items():
        numeric_keys = defaultdict(set)
        for m in members:
            for f, o, v in atoms(m["cond"])[1]:
                if _TERM.match(f"{f}{o}{v}"):
                    numeric_keys[(f, o)].add(float(v))
        varying = [k for k, vs in numeric_keys.items() if len(vs) > 1]
        if len(varying) == 1 and len(members) > 1:
            f, o = varying[0]
            cands = sorted(numeric_keys[(f, o)])
            pool_vals = sorted(set(_vals_of(f, fit)))
            if len(pool_vals) > 60:          # 太散就取分位点，别把扫描拖爆
                step = max(1, len(pool_vals) // 40)
                pool_vals = pool_vals[::step]
            cands = sorted(set(cands) | set(pool_vals))
            best = sweep(members[0]["cond"], f, o, cands, fit, envf, axis, base_a, args.min_n)
            if best:
                _, new_cond, r_fit, cut = best
                r_hold = _stat([l for l in hold if eval_cond(new_cond, l, envh)])
                names = sorted({n for m in members for n in m.get("names", [])})
                out.append({"axis": axis, "cond": new_cond, "cut": cut,
                            "merged_from": len(members), "names": names,
                            "fit": r_fit, "hold": r_hold,
                            "slugs": sorted({s for m in members for s in m.get("slugs", [])})})
                merged_n += len(members)
            continue
        for m in members:                    # 无法归并的单条，直接透传（重估）
            r_hold = _stat([l for l in hold if eval_cond(m["cond"], l, envh)])
            out.append({"axis": axis, "cond": m["cond"], "cut": None, "merged_from": 1,
                        "names": m.get("names", []), "fit": {k: m.get(k) for k in
                                                              ("n", "d_pp", "a_med", "y")},
                        "hold": r_hold, "slugs": m.get("slugs", [])})

    kept = [r for r in out if (r["fit"].get("n") or 0) >= args.min_n]
    if True:  # 方向轴按 |d|、波动轴按 |ā-基线| 排序
        kept.sort(key=lambda r: -(abs(r["fit"]["d_pp"]) if r["axis"] == "directional"
                                  else abs((r["fit"].get("a_med") or 0) - base_a)))
    (Path(args.src) / f"merged_{args.tag}.json").write_text(
        json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")

    L, A = [], lambda s: L.append(s)
    A(f"# 因子归纳 · 阈值合并 + 切点走前向（{len(lib)} 条 → {len(out)} 条）")
    A("")
    A(f"- 拟合窗口：{len(fit_days)} 天 / {len(fit)} 腿｜留出窗口：{len(hold)} 腿")
    A(f"- 归并：{merged_n} 条并进 {sum(1 for r in out if r['cut'] is not None)} 个族；"
      f"其余单条透传。保留 n≥{args.min_n} 共 **{len(kept)}** 条")
    A(f"- 基线 ā(拟合窗口中位) = {base_a:.2f}｜腿级线 {LEG_LINE4:.5f}")
    A("- ⚠️ 切点是在**拟合窗口**上选的；留出窗口列只用于检验，未参与选择。")
    A("")
    A("| 轴 | 条件 | 由几条合并 | 拟合 n | 拟合 d_pp | 拟合 ā | 留出 n | 留出 d_pp | 留出 ā |")
    A("|---|---|---|---|---|---|---|---|---|")
    for r in kept:
        fj, hd = r["fit"], r["hold"]
        A(f"| {r['axis'][:4]} | `{r['cond'][:40]}` | {r['merged_from']} | {fj.get('n',0)} | "
          f"{(fj.get('d_pp') or 0):+.1f} | {(fj.get('a_med') or 0):.2f} | {hd.get('n',0)} | "
          f"{(hd.get('d_pp') or 0):+.1f} | {(hd.get('a_med') or 0):.2f} |")
    A("")
    out_s = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text(out_s, encoding="utf-8")
        print(f"已写 {args.md}")
    print(out_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
