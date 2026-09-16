#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把两轴因子**应用**到某一天的候选腿（分析用，0 LLM）。

用法（分析流程的第一步：引擎先算，LLM 再判）
    python3 -m scripts.axis_apply --tag w30 --day 2026-08-09 --prompt

输出：
  1. 逐腿：命中的**方向 veto** / **方向 select** / 波动排序档（预测 ā）
  2. 两类筛选后的候选（veto 后 & 波动升序），供 LLM 组票
  3. `--prompt` 时额外落一份可直接拼进分析 prompt 的文本块

设计口径（见 docs/prompts/factor_produce_two_axis.md）：
  - 方向轴 = veto/select（命中率相对市场），波动轴 = 排序键（SP 保留率 ā）
  - x 门（引擎既有）在阶段 0；三轴分工，互不替代
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_cond_audit import build_eval_env, eval_cond          # noqa: E402
from scripts.axis_two_stage_backtest import load_legs, axis_report    # noqa: E402
from src.beidan_axis import LEG_LINE4                                  # noqa: E402


def load_factors(src: str, tag: str) -> list[dict]:
    p = Path(src) / f"final_{tag}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def apply_day(factors: list[dict], legs: list[dict], day: str,
              x_min: float = LEG_LINE4) -> dict:
    """阶段 0 = x 门（价格轴）；阶段 1 = 方向 veto/select；阶段 2 = 波动排序。"""
    env = build_eval_env(legs)
    rows, blocked = [], []
    for l in legs:
        if float(l.get("x") or 0) < float(x_min):
            blocked.append(l)          # 价格轴未过门：不进候选（引擎既有口径）
            continue
        hits = {"veto": [], "select": [], "rank": []}
        for f in factors:
            cond = f.get("cond") or ""
            try:
                ok = eval_cond(cond, l, env)
            except Exception:                                # noqa: BLE001
                ok = False
            if not ok:
                continue
            if f.get("axis") == "volatility":
                hits["rank"].append(f)
            elif f.get("role") == "veto":
                hits["veto"].append(f)
            else:
                hits["select"].append(f)
        rows.append({**l, "_hits": hits})
    vetoed = [r for r in rows if r["_hits"]["veto"]]
    kept = [r for r in rows if not r["_hits"]["veto"]]
    # 波动排序键：命中的 rank 因子越多、方向越一致 → 越靠前（这里用命中数与缺省顺序做稳定排序）
    def key(r):
        rk = r["_hits"]["rank"]
        score = sum(1 if f.get("expect") == "+" else -1 for f in rk)
        return (-score, -r["x"])
    kept_sorted = sorted(kept, key=key)
    return {"day": day, "rows": rows, "blocked": blocked,
            "vetoed": vetoed, "kept": kept_sorted, "x_min": x_min,
            "base": axis_report(legs, "当日全部候选"),
            "kept_report": axis_report(kept_sorted, "veto 后"),
            "veto_report": axis_report(vetoed, "被 veto")}


def prompt_block(res: dict, top: int = 12) -> str:
    L, A = [], lambda s: L.append(s)
    A(f"## 🧱 结构因子（引擎已算好，{res['day']}）")
    A("")
    A(f"- 当日腿池 {len(res['rows']) + len(res.get('blocked') or [])} 条 → "
      f"过 x 门（x ≥ {res.get('x_min', LEG_LINE4):.5f}）**{len(res['rows'])} 条**"
      f"｜其中 {len(res['vetoed'])} 条命中**方向否决**")
    A("- 否决 = 该结构下命中率显著低于市场报价（历史两窗验证过的），**别进票**")
    A("- 波动档 = 该腿的赛前赔率能不能兑现（ā = 开奖SP/赛前赔率），**只用来排序**，不改方向")
    A("")
    A("### 被否决的腿")
    A("")
    A("| 场次 | 侧 | x | 否决因子 |")
    A("|---|---|---|---|")
    for r in res["vetoed"]:
        A(f"| {r['lota_id']} | {r['side']} | {r['x']:.2f} | "
          f"{'、'.join(f['name'] for f in r['_hits']['veto'])} |")
    A("")
    A(f"### 通过筛选的候选（按波动档排序，前 {top}）")
    A("")
    A("| 场次 | 侧 | 赔率 | x | 波动档因子 | 方向加成 |")
    A("|---|---|---|---|---|---|")
    for r in res["kept"][:top]:
        rk = r["_hits"]["rank"]
        pos = "、".join(f["name"] for f in rk if f.get("expect") == "+")
        neg = "、".join(f["name"] for f in rk if f.get("expect") != "+")
        vol = " / ".join(x for x in (
            f"🟢保真（排序靠前）：{pos}" if pos else "",
            f"🔴缩水（排序靠后）：{neg}" if neg else "") if x)
        A(f"| {r['lota_id']} | {r['side']} | {r['beidan_odds']:.2f} | {r['x']:.2f} | "
          f"{vol or '—'} | "
          f"{'、'.join(f['name'] for f in r['_hits']['select']) or '—'} |")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="w30")
    ap.add_argument("--src", default="data/factor_mine_out")
    ap.add_argument("--day", required=True)
    ap.add_argument("--out", default="data/factor_mine_out")
    ap.add_argument("--prompt", action="store_true", help="落一份 prompt 文本块")
    args = ap.parse_args(argv)

    factors = load_factors(args.src, args.tag)
    legs = [l for l in load_legs("latest") if l["day"] == args.day]
    if not legs:
        print(f"{args.day} 无候选腿")
        return 1
    res = apply_day(factors, legs, args.day)

    print(f"=== {args.day}｜候选 {len(res['rows'])} 腿｜"
          f"否决 {len(res['vetoed'])}｜通过 {len(res['kept'])}")
    for nm in ("base", "kept_report", "veto_report"):
        r = res[nm]
        print(f"  {nm:12s} n={r['n']:3d} 命中={r['hit']*100:5.1f}% mp={r['mp']*100:5.1f}% "
              f"d={r['ddir_pp']:+5.1f}pp ā={r['a_med']:.2f} y={r['y']:.3f}")
    blk = prompt_block(res)
    (Path(args.out) / f"apply_{args.day}.md").write_text(blk, encoding="utf-8")
    if args.prompt:
        print()
        print(blk)
    print(f"\n已写 {Path(args.out) / f'apply_{args.day}.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
