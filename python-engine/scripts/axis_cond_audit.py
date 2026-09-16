#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用引擎自己的求值器复核 LLM 产出的 `cond`（0 LLM）。

两件事：
  1. **可求值性**：`cond` 是否落在 `src/axis_miner.py` 定义的语法里（能求值 → 归因可以自动化）。
  2. **复算**：按 cond 在腿池上重算 `n / hit_n / miss_n / d_pp / a_med / y`，与 LLM 报的对照。
     —— 这条过了，"首轮挖掘与周期挖掘共用一套"才成立（引擎算归因，不靠 LLM 自报）。

用法
    python3 -m scripts.axis_cond_audit --merge data/factor_mine_out/merge_7day.json \
        --days 2026-06-28,... --md docs/factor_mine_audit_7day.md
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

from scripts.axis_two_stage_backtest import load_legs              # noqa: E402
from src.beidan_axis import LEG_LINE4                              # noqa: E402

# 字段名可以带数字（ah.h1 / eu.d / disp.ds.h），value 允许是数字、侧（H/D/A）或另一个字段
# 求值器已搬到 src/axis_cond.py（引擎与脚本共用一份实现）
from src.axis_cond import (  # noqa: E402
    _TERM, build_eval_env, eval_cond, eval_term, field_value,
)


def _stat(ss: list) -> dict:
    n = len(ss)
    if not n:
        return {"n": 0}
    hit = sum(1 for l in ss if l["hit"])
    mp = st.mean(l["market_p"] for l in ss)
    ratios = [l["sp"] / l["beidan_odds"] for l in ss if l["beidan_odds"] > 0]
    return {"n": n, "hit_n": hit, "miss_n": n - hit, "hit": hit / n, "mp": mp,
            "d_pp": (hit / n - mp) * 100,
            "a_med": st.median(ratios) if ratios else None,
            "y": st.mean(l["z"] for l in ss)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", default="data/factor_mine_out/merge_7day.json")
    ap.add_argument("--days", required=True)
    ap.add_argument("--md", default="")
    ap.add_argument("--tol", type=float, default=6.0, help="d_pp 容差（pp）")
    ap.add_argument("--per-file", action="store_true",
                    help="按每个 worker 的原始产出逐一复核（LLM 报数只覆盖它那一天）")
    args = ap.parse_args(argv)

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    legs = [l for l in load_legs("latest") if l["day"] in set(days)]
    env = build_eval_env(legs)
    lib = json.loads(Path(args.merge).read_text(encoding="utf-8"))
    print(f"腿池 {len(legs)} 条｜候选 {len(lib)} 条\n")

    if args.per_file:
        tot = exact = near = off = unp = 0
        rows = []
        for p in sorted(Path(args.merge).parent.glob("*.json")):
            if p.name.startswith("merge_"):
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or not d.get("factors"):
                continue
            day = (d.get("days") or [None])[0]
            if day not in days:
                continue
            sub = [l for l in legs if l["day"] == day]
            env2 = build_eval_env(sub)
            for f in d["factors"]:
                tot += 1
                ev = f.get("evidence") or {}
                try:
                    r = _stat([l for l in sub if eval_cond(f.get("cond") or "", l, env2)])
                except Exception:                            # noqa: BLE001
                    unp += 1
                    continue
                dn = abs(r["n"] - int(ev.get("n") or 0))
                dd = abs(r["d_pp"] - (ev.get("d_pp") or 0))
                tag = "exact" if dn == 0 and dd <= 2 else ("near" if dn <= 2 and dd <= 8 else "off")
                exact += tag == "exact"
                near += tag == "near"
                off += tag == "off"
                if tag != "exact":
                    rows.append(f"| {p.stem[:26]} | {f.get('name')} | `{(f.get('cond') or '')[:40]}` | "
                                f"{r['n']} | {ev.get('n')} | {r['d_pp']:+.1f} | "
                                f"{(ev.get('d_pp') or 0):+.1f} | {'接近' if tag == 'near' else '偏离'} |")
        md = [f"共 {tot} 条因子｜**完全复现 {exact}（{exact/max(tot,1):.0%}）**｜"
              f"接近 {near}｜偏离 {off}｜不可求值 {unp}", "",
              "| worker | 因子 | cond | 引擎 n | LLM n | 引擎 d_pp | LLM d_pp | 判定 |",
              "|---|---|---|---|---|---|---|---|"] + rows
        out = "\n".join(md) + "\n"
        if args.md:
            Path(args.md).write_text("# 两轴候选因子 · 逐 worker 引擎复核\n\n" + out, encoding="utf-8")
            print(f"已写 {args.md}")
        print(out)
        return 0

    L, A = [], lambda s: L.append(s)
    A("| 因子 | 轴 | cond | 引擎复算 n | LLM 报 n | 引擎 d_pp | LLM d_pp | 一致? |")
    A("|---|---|---|---|---|---|---|---|")
    ok = bad = unparsed = 0
    for f in lib:
        cond = (f.get("conds") or [""])[0]
        # LLM 只在它那一天/那批样本上计数 ⇒ 复核也必须限定在同一天
        own = set(f.get("days") or []) or set(days)
        sub = [l for l in legs if l["day"] in own]
        try:
            hit = [l for l in sub if eval_cond(cond, l, env)]
        except Exception as e:                             # noqa: BLE001
            unparsed += 1
            A(f"| {f['name']} | {f['axis'][:4]} | `{cond[:46]}` | — | {f['n']} | — | — | ⛔ {str(e)[:24]} |")
            continue
        r = _stat(hit)
        llm_d = st.mean(f["d_pp"]) if f.get("d_pp") else float("nan")
        same = (r.get("n", 0) == f["n"]) and abs((r.get("d_pp") or 0) - llm_d) <= args.tol
        ok += 1 if same else 0
        bad += 0 if same else 1
        A(f"| {f['name']} | {f['axis'][:4]} | `{cond[:46]}` | {r.get('n',0)} | {f['n']} | "
          f"{(r.get('d_pp') or 0):+.1f} | {llm_d:+.1f} | {'✅' if same else '⚠️'} |")
    A("")
    A(f"可求值 {ok + bad}/{len(lib)}｜复算一致（n 相等且 |Δd_pp|≤{args.tol:g}）**{ok}**｜"
      f"不一致 {bad}｜不可求值 {unparsed}")
    out = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text("# 两轴候选因子 · 引擎求值复核\n\n" + out, encoding="utf-8")
        print(f"已写 {args.md}")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
