#!/usr/bin/env python3
"""回放沙箱「因子学习成果」分析：这轮到底学到了什么有用的因子？

判读原则（对齐 docs/ab_factor_library.md 的根因诊断）：
* **真实样本**（来自真下单 → 结算反思归因）= 有效信号来源；
* **观察样本**（hit=None/profit=0，来自无下单日的 `_reflect_skipped`）= 只留触发记录，
  不进盈亏/命中统计；纯观察型因子**没有任何决策验证过**，属噪声嫌疑。
所以本报告把「有真实样本的因子」与「纯观察型因子」分开列，并给每类算小计。

用法：
    python3 -m scripts.factor_learn_report /tmp/beidan_learn15
    python3 -m scripts.factor_learn_report /tmp/beidan_learn15 --md docs/factor_learn_15d.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _split_hist(e: dict) -> tuple[list, list]:
    """history 拆成（观察样本, 真实样本）。观察样本标签：hit is None 且 profit==0。"""
    obs, real = [], []
    for x in (e.get("history") or []):
        if x.get("hit") is None and not x.get("profit"):
            obs.append(x)
        else:
            real.append(x)
    return obs, real


def analyze(ws: Path) -> dict:
    fm = json.loads((ws / "memory" / "factor_memory.json").read_text(encoding="utf-8"))
    perf = fm.get("factor_perf") or {}
    refl_p = ws / "memory" / "reflection_memory.json"
    refl = json.loads(refl_p.read_text(encoding="utf-8")).get("reflections") or [] if refl_p.exists() else []
    role = json.loads((ws / "bcl狗.json").read_text(encoding="utf-8"))
    orders = role.get("orders") or []

    rows = []
    for n, e in perf.items():
        obs, real = _split_hist(e)
        rh = sum(1 for x in real if x.get("hit") is True)
        rf = sum(1 for x in real if x.get("hit") is False)
        profit = sum(float(x.get("profit") or 0) for x in real)
        days = sorted({str(x.get("date")) for x in real if x.get("date")})
        rows.append({
            "name": n, "status": e.get("status"), "type": e.get("type"),
            "desc": (e.get("desc") or "")[:60],
            "obs": len(obs), "real": len(real), "hit": rh, "miss": rf,
            "profit": round(profit, 2), "days": days,
            "verified": len(real) > 0,
        })
    rows.sort(key=lambda r: (-r["real"], -r["profit"]))

    legs = [l for o in orders for l in (o.get("ticket_legs") or [])]
    stake = sum(float(o.get("total_stake") or 0) for o in orders if (o.get("ticket_legs") or []))
    pnl = sum(float(o.get("profit") or 0) for o in orders if (o.get("ticket_legs") or []))
    n_leg = len(legs)
    k_leg = sum(1 for l in legs if l.get("hit"))
    leg_roi = (sum((0.65 * float(l.get("sp") or 0) - 1) if l.get("hit") else -1 for l in legs) / n_leg) if n_leg else 0.0

    return {
        "rows": rows, "reflections": refl, "orders": orders, "role": role,
        "tickets": len([o for o in orders if (o.get("ticket_legs") or [])]), "stake": stake, "pnl": pnl,
        "leg_n": n_leg, "leg_hit": k_leg, "leg_roi": leg_roi,
        "capital": float(role.get("capital") or 0),
    }


def render(a: dict, label: str) -> str:
    verified = [r for r in a["rows"] if r["verified"]]
    obs_only = [r for r in a["rows"] if not r["verified"]]
    o = []
    o.append(f"# 因子学习报告 · {label}")
    o.append("")
    o.append(f"- 订单 {a['tickets']} 票｜投注 {a['stake']:.0f} 元｜盈亏 {a['pnl']:+.0f} 元"
             f"｜资金 {a['capital']:.0f}")
    o.append(f"- 腿 {a['leg_n']}｜命中 {a['leg_hit']}（{a['leg_hit']/a['leg_n']:.1%}）"
             f"｜腿级 ROI {a['leg_roi']:+.1%}" if a["leg_n"] else "- 无腿")
    o.append(f"- 因子 {len(a['rows'])} 个：**有真实样本 {len(verified)}**｜纯观察型 {len(obs_only)}")
    o.append(f"- 反思 {len(a['reflections'])} 条")
    o.append("")
    o.append("## 有真实样本的因子（真下单 → 结算归因，唯一可信来源）")
    o.append("")
    o.append("| 因子 | 类型 | 真实样本 | 命中 | 未中 | 盈亏 | 出现日 |")
    o.append("|---|---|---|---|---|---|---|")
    for r in verified:
        o.append(f"| {r['name']} | {r['type']} | {r['real']} | {r['hit']} | {r['miss']} | "
                 f"{r['profit']:+.2f} | {','.join(d[5:] for d in r['days'])} |")
    if not verified:
        o.append("| （无） | | | | | | |")
    o.append("")
    o.append("## 纯观察型因子（无任何真实样本；噪声嫌疑）")
    o.append("")
    o.append("| 因子 | 类型 | 观察样本 | 状态 |")
    o.append("|---|---|---|---|")
    for r in obs_only:
        o.append(f"| {r['name']} | {r['type']} | {r['obs']} | {r['status']} |")
    if not obs_only:
        o.append("| （无） | | | |")
    return "\n".join(o)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sandbox")
    ap.add_argument("--label", default="")
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)
    ws = Path(args.sandbox)
    a = analyze(ws)
    text = render(a, args.label or ws.name)
    print(text)
    if args.md:
        Path(args.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已落盘: {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
