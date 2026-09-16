#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐日 A/B 体检（0 LLM）：引擎实际出的腿 vs 各种口径，含票级结算。

用法
    python3 -m scripts.axis_day_report --sandbox /private/tmp/axis_sb2/95狗 \
        --days 2026-08-07,2026-08-08,2026-08-09

对照臂（全部在同一批"当天过门腿"上算，腿级命中率）：
  过门池      — 当天所有 x ≥ 腿级线的腿
  veto 组     — 命中任一 role=veto 的两轴因子
  veto 后剩余 — 上面两类之差
  A 按 x 降序前 9
  B 引擎实际  — 从该狗订单里取出、且属于该足球日的腿
票级：每张订单用开奖 SP 复算 4 关/容错票的回收。
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_two_stage_backtest import load_legs          # noqa: E402
from src.axis_cond import build_eval_env, eval_cond            # noqa: E402
from src.beidan_axis import LEG_LINE4                           # noqa: E402


def _ticket_payout(legs: list[dict], need: int = 4) -> tuple[float, int]:
    """容错票回收：Σ_{所有 need 关组合全中} 2 × 0.65 × Π SP。"""
    pay, wins = 0.0, 0
    for combo in itertools.combinations(legs, need):
        if all(l.get("hit") for l in combo):
            prod = 1.0
            for l in combo:
                prod *= float(l.get("sp") or 0)
            pay += 2 * 0.65 * prod
            wins += 1
    return pay, wins


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", required=True, help="沙箱狗目录（含 <狗>.json）")
    ap.add_argument("--factors", default="data/factor_mine_out/final_w30.json")
    ap.add_argument("--days", required=True)
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)

    sb = Path(args.sandbox)
    # 狗配置 = 与目录同名的那个 json（parlay.json 只是票型配置，没有 orders）
    cand = sb / f"{sb.name}.json"
    if not cand.exists():
        cand = next((p for p in sorted(sb.glob("*.json")) if "parlay" not in p.name), None)
    dog = json.loads(cand.read_text(encoding="utf-8")) if cand else {}
    factors = json.loads(Path(args.factors).read_text(encoding="utf-8"))
    veto = {f["name"] for f in factors if f.get("role") == "veto"}

    pool = {}
    for l in load_legs("latest"):
        if l["x"] >= LEG_LINE4:
            pool[(l["day"], l["lota_id"], l["side"])] = l

    bought = set()
    for o in dog.get("orders") or []:
        for l in (o.get("legs") or []):
            for s in (l.get("picks") or []):
                bought.add((l.get("lota_id"), s))

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    L, A = [], lambda s: L.append(s)
    tot = {k: [0, 0] for k in ("pool", "veto", "keep", "A", "B")}
    A("| 天 | 过门池 | veto组 | veto后剩余 | A x降序前9 | B 引擎实际 |")
    A("|---|---|---|---|---|---|")
    for day in days:
        rows = [{**l, "hit": l["hit"]} for (d, _lid, _s), l in pool.items() if d == day]
        if not rows:
            continue
        env = build_eval_env([l for (d, _l, _s), l in pool.items() if d >= day]
                             if False else
                             [l for l in load_legs("latest") if l["day"] == day])
        for r in rows:
            r["f"] = [f["name"] for f in factors if (f.get("cond") or "")
                      and _safe(eval_cond, f["cond"], r, env)]
        vet = [r for r in rows if any(n in veto for n in r["f"])]
        keep = [r for r in rows if r not in vet]
        armA = sorted(rows, key=lambda r: -r["x"])[:9]
        armB = [r for r in rows if (r["lota_id"], r["side"]) in bought]

        def g(ss):
            if not ss:
                return "—"
            h = sum(1 for r in ss if r["hit"])
            return f"{h}/{len(ss)}={h / len(ss) * 100:.0f}%"
        A(f"| {day} | {g(rows)} | {g(vet)} | {g(keep)} | {g(armA)} | {g(armB)} |")
        for k, ss in (("pool", rows), ("veto", vet), ("keep", keep), ("A", armA), ("B", armB)):
            tot[k][0] += len(ss)
            tot[k][1] += sum(1 for r in ss if r["hit"])

    A("")
    A("**合计（腿级）**")
    A("")
    A("| 臂 | 命中/腿数 | 命中率 |")
    A("|---|---|---|")
    for k, label in (("pool", "过门池"), ("veto", "veto组"), ("keep", "veto后剩余"),
                     ("A", "A x降序前9"), ("B", "B 引擎实际")):
        n, h = tot[k]
        A(f"| {label} | {h}/{n} | {h / n * 100 if n else 0:.1f}% |")
    A("")
    A("**票级结算**")
    A("")
    A("| 订单 | 腿数 | 命中腿 | 回收 | 成本 | 净 |")
    A("|---|---|---|---|---|---|")
    for o in dog.get("orders") or []:
        legs = []
        for l in (o.get("legs") or []):
            for s in (l.get("picks") or []):
                p = pool.get((None, l.get("lota_id"), s)) or next(
                    (v for (d, lid, sd), v in pool.items()
                     if lid == l.get("lota_id") and sd == s), None)
                if p:
                    legs.append(p)
        if not legs:
            continue
        pay, wins = _ticket_payout(legs)
        cost = float(o.get("total_stake") or 0)
        A(f"| {o.get('slip_id', '')[:18]} | {len(legs)} | "
          f"{sum(1 for x in legs if x['hit'])} | {pay:.0f} | {cost:.0f} | {pay - cost:+.0f} |")
    A("")
    out = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text(out, encoding="utf-8")
        print(f"已写 {args.md}")
    print(out)
    return 0


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:                                            # noqa: BLE001
        return False


if __name__ == "__main__":
    raise SystemExit(main())
