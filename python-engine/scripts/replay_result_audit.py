#!/usr/bin/env python3
"""回放结果审计（4 关票口径）——吃任意沙箱 workspace，输出可复算的腿级/票级结论。

口径（用户 2026-09-15）：
    本狗玩 `M过4`（M>4）：整票只在结算时收一次 0.65（官方：过关奖金 = 2×ΠSP×65%）
    ⇒ 腿级线 = (1/0.65)^(1/4) = 1.11371；票级打平要 Π(SP·p̂) > 1/0.65 = 1.5385
    腿标签 z = SP·1{中}，y = E[z]；y 的 95% CI 下沿过 1.11371 才算「有增量」。

用法
    python3 -m scripts.replay_result_audit --ws <workspace> [--md docs/xxx.md] [--label 名字]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
from pathlib import Path

RATE = 0.65
LINE4 = (1.0 / RATE) ** 0.25
LINE1 = 1.0 / RATE
CODE = {"3": "H", "1": "D", "0": "A"}
BANDS = ((1.5385, 99.0, "x>1.5385"), (1.3, 1.5385, "1.3~1.5385"),
         (1.11371, 1.3, "1.1137~1.3"), (1.0, 1.11371, "1.0~1.1137"), (-9, 1.0, "x<1.0"))


def load(ws: Path, dog: str = "95狗"):
    p = ws / f"{dog}.json"
    if not p.exists():
        cands = [q for q in ws.glob("*.json") if not q.name.startswith(("fact", "report", "session"))]
        p = cands[0]
    d = json.loads(p.read_text(encoding="utf-8"))
    legs, tickets = [], []
    for o in d.get("orders") or []:
        tickets.append(o)
        for l in o.get("ticket_legs") or []:
            bi = l.get("beidan_info") or {}
            side = (l.get("picks") or [None])[0]
            act = CODE.get(str(bi.get("result")).strip(), "")
            sp = float(bi.get("spvalue") or 0)
            legs.append({"lid": l.get("lota_id"), "side": side, "x": l.get("x_mkt"),
                         "odds": (l.get("odds") or {}).get(side), "sp": sp, "act": act,
                         "hit": bool(act) and act == side, "slip": o.get("slip_type"),
                         "day": (l.get("match_time") or "")[:10],
                         "factors": l.get("factors") or []})
    return d, legs, tickets


def band_row(name: str, ss: list[dict]) -> str:
    ss = [l for l in ss if l["sp"] > 0 and l["act"]]
    if not ss:
        return f"| {name} | 0 | — | — | — | — |"
    zs = [l["sp"] if l["hit"] else 0.0 for l in ss]
    n = len(zs); y = st.mean(zs); se = st.pstdev(zs) / math.sqrt(n)
    hit = sum(1 for l in ss if l["hit"]) / n
    xs = [l["x"] for l in ss if l.get("x")]
    ok = "✅" if y - 1.96 * se > LINE4 else ("≈" if y > LINE4 else "❌")
    return (f"| {name} | {n} | {hit*100:.1f}% | {st.mean(xs) if xs else 0:.3f} | "
            f"{y:.3f} [{y-1.96*se:.3f},{y+1.96*se:.3f}] | {RATE*y-1:+.1%} {ok} |")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", required=True)
    ap.add_argument("--dog", default="95狗")
    ap.add_argument("--md", default="")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    ws = Path(args.ws)
    d, legs, tickets = load(ws, args.dog)

    L: list[str] = []
    A = L.append
    A(f"# 回放结果审计：{args.label or ws.name}")
    A("")
    A(f"- ws `{ws}`｜资金 {d.get('initial_capital')} → **{d.get('capital')}**｜"
      f"下单 {len(tickets)} 票 / {len(legs)} 腿")
    staked = sum(t.get("total_stake") or 0 for t in tickets)
    returned = sum(t.get("return_amount") or 0 for t in tickets)
    win = [t for t in tickets if t.get("hit")]
    A(f"- 合计成本 **{staked:.0f}**｜回收 **{returned:.2f}**｜"
      f"净 **{(returned-staked):+.2f}**（{(returned-staked)/staked*100 if staked else 0:+.1f}%）"
      f"｜中票 **{len(win)}/{len(tickets)}**")
    A("")
    if win:
        big = max(win, key=lambda t: t.get("profit") or 0)
        A(f"- 单票最大盈利 {big.get('profit'):+.2f}"
          f"（占净利 {big.get('profit')/(returned-staked)*100:.0f}% 若净利>0）")
        A("")

    A("## 腿级（z = SP·1{中}，y=E[z]，腿级线 1.11371）")
    A("")
    A("| 分档 | n | 命中 | x̄ | y [95%CI] | 单腿每元 | 4关线 |")
    A("|---|---|---|---|---|---|---|")
    A(band_row("全部下单腿", legs))
    for lo, hi, nm in BANDS:
        A(band_row(nm, [l for l in legs if l.get("x") and lo < l["x"] <= hi]))
    A("")

    A("## 票级")
    A("")
    A("| 票型 | 腿数 | 成本 | 中? | 回收 | 盈亏 | 预测ROI(4关组合均值) | ΠSP（实际） |")
    A("|---|---|---|---|---|---|---|---|")
    for t in tickets:
        ls = [l for l in (t.get("ticket_legs") or []) if l.get("x_mkt")]
        n = len(ls)
        pred = None
        if n >= 4:
            from itertools import combinations
            if n <= 14:
                subs = list(combinations([float(l["x_mkt"]) for l in ls], 4))
            else:  # 抽样，省算力
                import random
                rnd = random.Random(7)
                xs = [float(l["x_mkt"]) for l in ls]
                subs = [tuple(rnd.sample(xs, 4)) for _ in range(20000)]
            prod = [math.prod(s) for s in subs]
            pred = RATE * (sum(prod) / len(prod)) - 1.0
        A(f"| {t.get('slip_type')} | {len(t.get('ticket_legs') or [])} | "
          f"{t.get('total_stake'):.0f} | "
          f"{'✅' if t.get('hit') else '❌'} | {t.get('return_amount'):.2f} | "
          f"{t.get('profit'):+.2f} | "
          f"{(f'{pred:+.1%}' if pred is not None else '—')} | {t.get('sp_product'):.2f} |")
    A("")
    A("## 因子归因（下单腿命中的因子）")
    A("")
    cnt: dict[str, list[int]] = {}
    for l in legs:
        for f in l.get("factors") or []:
            cnt.setdefault(f, []).append(1 if l["hit"] else 0)
    if cnt:
        A("| 因子 | 腿数 | 命中 | z 均值 |")
        A("|---|---|---|---|")
        for f, hs in sorted(cnt.items(), key=lambda x: -len(x[1]))[:20]:
            zs = [l["sp"] if l["hit"] else 0.0 for l in legs if f in (l.get("factors") or [])]
            A(f"| {f} | {len(hs)} | {sum(hs)}/{len(hs)} | {st.mean(zs):.3f} |")
    else:
        A("（本批下单腿没有因子归因）")
    A("")
    text = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
