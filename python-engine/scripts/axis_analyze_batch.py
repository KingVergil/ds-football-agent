#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""留出窗口批量分析 + 三组对照（引擎两轴 vs 现状 vs LLM 组票）。

用法
    python3 -m scripts.axis_analyze_batch --tag w30 --days 2026-08-09,... --parallel 7 \
        --md docs/analysis_holdout_w30.md

三组：
  A 现状       — 过 x 门后按 x 降序取 4（老口径）
  B 引擎两轴   — veto 后按波动档降序取 4
  C LLM 组票   — 引擎筛完的腿 + 标注 → LLM 自己组 4 关票（取它的全部腿）
判定：**腿级命中率** + 票级（4 关全中）。票级方差极大，只看腿级。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_apply import load_factors                             # noqa: E402
from scripts.axis_analyze import run_day                                # noqa: E402
from scripts.axis_two_stage_backtest import load_legs                   # noqa: E402

PERSONA = "data/replays/sandboxes/95狗_0801/workspace/persona.md"


def dedupe(rows: list) -> list:
    out, seen = [], set()
    for r in rows:
        if r["lota_id"] in seen:
            continue
        seen.add(r["lota_id"])
        out.append(r)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="w30")
    ap.add_argument("--src", default="data/factor_mine_out")
    ap.add_argument("--days", required=True)
    ap.add_argument("--parallel", type=int, default=7)
    ap.add_argument("--stagger", type=float, default=3.0)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--md", default="")
    ap.add_argument("--skip-llm", action="store_true")
    args = ap.parse_args(argv)

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    legs_all = load_legs("latest")
    have = {l["day"] for l in legs_all}
    days = [d for d in days if d in have]
    print(f"留出窗口 {len(days)} 天｜并发 {args.parallel}")

    results: dict = {}
    if args.skip_llm:
        for d in days:
            results[d] = {"ok": True}
    else:
        import threading
        from src.providers.deepseek import DeepSeekProvider
        _local = threading.local()

        def _provider():
            if not getattr(_local, "p", None):
                _local.p = DeepSeekProvider(temperature=args.temperature)
            return _local.p

        def _job(day):
            last = None
            for attempt in range(3):
                try:
                    return day, run_day(args.tag, day, args.src, PERSONA,
                                        args.temperature, args.src,
                                        legs_cache=legs_all, provider=_provider())
                except Exception as e:                        # noqa: BLE001
                    last = e
                    time.sleep(15 * (attempt + 1))
            return day, last

        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            futs = {}
            for d in days:
                futs[ex.submit(_job, d)] = d
                time.sleep(args.stagger)
            for fu in as_completed(futs):
                day, r = fu.result()
                results[day] = r if isinstance(r, dict) else {"err": str(r)[:120]}
                tag = "OK" if isinstance(r, dict) and r.get("data") else f"FAIL {str(r)[:60]}"
                n_t = len((r.get("data") or {}).get("tickets") or []) if isinstance(r, dict) else 0
                print(f"  {day} {tag}｜票 {n_t}", flush=True)

    # ── 三组对照（腿级） ──
    rows, agg = [], {"A": [0, 0], "B": [0, 0], "C": [0, 0], "POOL": [0, 0], "KEEP": [0, 0]}
    tickets = {"A": [0, 0], "B": [0, 0], "C": [0, 0]}
    for d in days:
        lg = [l for l in legs_all if l["day"] == d]
        factors = load_factors(args.src, args.tag)
        from scripts.axis_apply import apply_day
        res = apply_day(factors, lg, d)
        gated = res["rows"]
        grp = {
            "POOL": gated,
            "KEEP": res["kept"],
            "A": sorted(gated, key=lambda r: -r["x"]),
            "B": res["kept"],
        }
        got = results.get(d)
        picks_c = []
        if isinstance(got, dict) and got.get("data"):
            idx = {(r["lota_id"], r["side"]): r for r in gated}
            for t in got["data"].get("tickets") or []:
                for l in t.get("legs") or []:
                    r = idx.get((l.get("lota_id"), l.get("side")))
                    if r:
                        picks_c.append(r)
        grp["C"] = picks_c
        line = [f"| {d} | {len(gated)} |"]
        for k in ("A", "B", "C"):
            sel = dedupe(grp[k])[:4] if k != "C" else dedupe(grp[k])
            if sel:
                hit = sum(1 for r in sel if r["hit"])
                agg[k][0] += len(sel); agg[k][1] += hit
                if k != "C":
                    tickets[k][0] += 1
                    tickets[k][1] += 1 if all(r["hit"] for r in sel) else 0
            else:
                hit = 0
            line.append(f"{len(sel)}腿/{hit}中")
        for k in ("POOL", "KEEP"):
            agg[k][0] += len(grp[k]); agg[k][1] += sum(1 for r in grp[k] if r["hit"])
        line.append(f"{len(grp['KEEP'])}腿/{sum(1 for r in grp['KEEP'] if r['hit'])}中")
        rows.append("| " + " | ".join(line[1:]) + " |")

    L, A = [], lambda s: L.append(s)
    A(f"# 留出窗口批量分析（{len(days)} 天）· 三组对照")
    A("")
    A(f"- 因子库 `final_{args.tag}.json`（挖掘窗口：前 30 天；本表全部为**留出天**）")
    A("- A 现状=按 x 降序取4｜B 引擎两轴=veto 后按波动档取4｜C=LLM 组票的全部腿")
    A("- 票级(4 关全中)方差极大，只作参考；结论看**腿级命中率**")
    A("")
    A("| 天 | 过门腿 | A 现状 | B 引擎两轴 | C LLM | 引擎筛后池 |")
    A("|---|---|---|---|---|---|")
    L.extend(rows)
    A("")
    A("## 合计（腿级）")
    A("")
    A("| 组 | 腿数 | 命中 | 命中率 | 票数 | 中票 |")
    A("|---|---|---|---|---|---|")
    for k, nm in (("POOL", "过 x 门全部"), ("KEEP", "veto 后全池"), ("A", "A 现状 top4"),
                  ("B", "B 引擎两轴 top4"), ("C", "C LLM 组票")):
        n, h = agg[k]
        t_n, t_h = tickets.get(k, ("", ""))
        rate = f"{h / n * 100:.1f}%" if n else "—"
        A(f"| {nm} | {n} | {h} | {rate} | {t_n if t_n != '' else '—'} | "
          f"{t_h if t_h != '' else '—'} |")
    A("")
    out = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text(out, encoding="utf-8")
        print(f"已写 {args.md}")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
