#!/usr/bin/env python3
"""两个沙箱回放的腿级/票级对比（A/B 对照用）。

用途：比较「注入因子库」与「空因子库」同窗口回放的表现，输出可判读的腿级统计
（票级在 9 关票上方差极大，不能作判据——见 docs/beidan_ledger_status.md §3.4）。

用法：
    python3 -m scripts.compare_arms /tmp/beidan_seq_bcl狗 /tmp/beidan_seq_control \
        --labels 注入因子 空因子

输出：每臂的 票数/投注/派彩/票级ROI + 腿数/命中率/腿级ROI + 分方向明细 + 两臂差检验。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def _won(o: dict) -> bool:
    return bool(o.get("hit")) or bool(o.get("slips_any_hit"))


def load_arm(ws: Path) -> dict:
    """读一个沙箱角色的订单，算票级与腿级统计。"""
    role = json.loads((ws / "bcl狗.json").read_text(encoding="utf-8"))
    orders = role.get("orders") or []
    # 只算真下出去的票（成本 > 0 且有腿）
    tickets = [o for o in orders if float(o.get("total_stake") or 0) > 0 and (o.get("ticket_legs") or [])]
    legs = [l for o in tickets for l in (o.get("ticket_legs") or [])]

    stake = sum(float(o.get("total_stake") or 0) for o in tickets)
    ret = sum(float(o.get("return_amount") or 0) for o in tickets)
    pnl = sum(float(o.get("profit") or 0) for o in tickets)
    ticket_hits = sum(1 for o in tickets if o.get("hit"))

    n = len(legs)
    k = sum(1 for l in legs if l.get("hit"))
    leg_roi = (sum((0.65 * float(l.get("sp") or 0) - 1) if l.get("hit") else -1 for l in legs) / n) if n else 0.0
    avg_sp = (sum(float(l.get("sp") or 0) for l in legs) / n) if n else 0.0
    avg_x = (sum(float(l.get("x_mkt") or 0) for l in legs) / n) if n else 0.0

    by_side = {}
    for s in "HDA":
        g = [l for l in legs if (l.get("picks") or [l.get("pick")])[0] == s]
        if not g:
            by_side[s] = {"n": 0, "hit": 0}
            continue
        h = sum(1 for l in g if l.get("hit"))
        r = sum((0.65 * float(l.get("sp") or 0) - 1) if l.get("hit") else -1 for l in g) / len(g)
        by_side[s] = {"n": len(g), "hit": h, "rate": h / len(g), "roi": r}

    return {
        "role": role, "tickets": tickets, "legs": legs,
        "ticket_n": len(tickets), "stake": stake, "return": ret, "pnl": pnl,
        "ticket_hits": ticket_hits, "ticket_roi": (pnl / stake if stake else 0.0),
        "leg_n": n, "leg_hit": k, "leg_rate": (k / n if n else 0.0), "leg_roi": leg_roi,
        "avg_sp": avg_sp, "avg_x": avg_x, "by_side": by_side,
        "capital": float(role.get("capital") or 0),
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    hw = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - hw), min(1.0, c + hw))


def compare(arms: list[tuple[str, dict]]) -> str:
    out = []
    out.append("=" * 74)
    out.append(f"{'指标':<26}" + "".join(f"{lab:>16}" for lab, _ in arms))
    out.append("=" * 74)

    def row(label, fn, fmt):
        out.append(f"{label:<26}" + "".join(f"{fmt(fn(a)):>16}" for _, a in arms))

    row("票数", lambda a: a["ticket_n"], lambda v: f"{v:d}")
    row("总投注(元)", lambda a: a["stake"], lambda v: f"{v:.0f}")
    row("总派彩(元)", lambda a: a["return"], lambda v: f"{v:.0f}")
    row("票级 ROI", lambda a: a["ticket_roi"], lambda v: f"{v:+.1%}")
    row("中票数", lambda a: a["ticket_hits"], lambda v: f"{v:d}")
    out.append("-" * 74)
    row("腿数", lambda a: a["leg_n"], lambda v: f"{v:d}")
    row("腿命中", lambda a: a["leg_hit"], lambda v: f"{v:d}")
    row("腿命中率", lambda a: a["leg_rate"], lambda v: f"{v:.1%}")
    row("命中率 95%CI 下沿", lambda a: wilson(a["leg_hit"], a["leg_n"])[0], lambda v: f"{v:.1%}")
    row("命中率 95%CI 上沿", lambda a: wilson(a["leg_hit"], a["leg_n"])[1], lambda v: f"{v:.1%}")
    row("平均 SP", lambda a: a["avg_sp"], lambda v: f"{v:.2f}")
    row("平均 x", lambda a: a["avg_x"], lambda v: f"{v:.3f}")
    row("腿级 ROI(实现)", lambda a: a["leg_roi"], lambda v: f"{v:+.1%}")
    row("打平线(0.65/SP̄)", lambda a: (0.65 / a["avg_sp"] if a["avg_sp"] else 0), lambda v: f"{v:.1%}")
    out.append("-" * 74)
    row("资金", lambda a: a["capital"], lambda v: f"{v:.0f}")
    row("资金 ROI", lambda a: (a["capital"] - 5000) / 5000, lambda v: f"{v:+.2%}")

    for s in "HDA":
        out.append("-" * 74)
        row(f"{s} 腿数", lambda a, s=s: a["by_side"][s]["n"], lambda v: f"{v:d}")
        row(f"{s} 命中率", lambda a, s=s: a["by_side"][s].get("rate", 0), lambda v: f"{v:.1%}")
        row(f"{s} 腿级 ROI", lambda a, s=s: a["by_side"][s].get("roi", 0), lambda v: f"{v:+.1%}")

    # 两臂腿命中率差检验（比例 z 检验）
    if len(arms) == 2:
        (l1, a1), (l2, a2) = arms
        n1, k1, n2, k2 = a1["leg_n"], a1["leg_hit"], a2["leg_n"], a2["leg_hit"]
        if n1 and n2:
            p1, p2 = k1 / n1, k2 / n2
            pp = (k1 + k2) / (n1 + n2)
            se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2))
            z = (p1 - p2) / se if se else 0.0
            out.append("=" * 74)
            out.append(f"腿命中率差：{l1} {p1:.1%} vs {l2} {p2:.1%} → 差 {p1-p2:+.1%}，z={z:.2f}"
                       f"（|z|>1.96 为 5% 显著）")
            r1, r2 = a1["leg_roi"], a2["leg_roi"]
            out.append(f"腿级 ROI 差：{l1} {r1:+.1%} vs {l2} {r2:+.1%} → 差 {r1-r2:+.1%}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sandboxes", nargs="+", help="两个沙箱目录")
    ap.add_argument("--labels", default="", help="逗号分隔的臂名（缺省用目录名）")
    ap.add_argument("--json-out", default="", help="可选：把统计落盘为 json")
    args = ap.parse_args(argv)
    labels = [x for x in (args.labels.split(",") if args.labels else []) if x]
    arms = []
    for i, p in enumerate(args.sandboxes):
        ws = Path(p)
        lab = labels[i] if i < len(labels) else ws.name
        arms.append((lab, load_arm(ws)))
    report = compare(arms)
    print(report)
    if args.json_out:
        slim = {lab: {k: a[k] for k in ("ticket_n", "stake", "return", "pnl", "ticket_roi",
                                        "leg_n", "leg_hit", "leg_rate", "leg_roi", "avg_sp",
                                        "avg_x", "by_side", "capital")} for lab, a in arms}
        Path(args.json_out).write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已落盘: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
