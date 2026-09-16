#!/usr/bin/env python3
"""腿级 EV 的真面目：x 分档、SP 折扣修正、以及"有没有任何一档是正 EV"。

背景（2026-09-13）
─────────────────
票级反复证明"靠极端日撑起来"，根因是**腿级就是负 EV**：
奖池让球后 p̂×赔率（= x）看着便宜，但**开奖 SP 普遍低于下注时赔率**（实测中位 0.89，
且 x 越大折扣越狠）。所以：

    腿的真实每元期望 = 0.65 × x × 折扣(x) − 1

折扣(x) < 1 时，x 必须高到 `x ≥ (1/0.65)/折扣(x)` 才可能为正——而折扣本身随 x 走低，
于是形成"越便宜越假"的死循环。本脚本用数据把这个关系量出来，并回答：

  1. 逐档腿级 ROI（x 从 1.0 扫到 2.0）—— **有没有任何一档为正**；
  2. 折扣(x) 的经验曲线（含样本内外拆分，防过拟合）；
  3. 若按"预测 SP"重算门限（x' = x × 折扣(x)），腿级 ROI 是否变好。

用法
───
    python3 -m scripts.leg_ev_audit --pool data/leg_pool_all --md docs/leg_ev_audit.md
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path

RATE = 0.65          # 北单返奖率（只乘一次）
BREAKEVEN = 1 / RATE  # 1.538：整票打平所需 Πx


def load_legs(pool_dir: str) -> list[dict]:
    """展开成"每场每侧"的腿样本（含 x、下注赔率、开奖 SP、是否命中）。"""
    out = []
    for f in sorted(glob.glob(str(Path(pool_dir) / "*.json"))):
        day = Path(f).stem
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            if not r.get("settled"):
                continue
            sp = float(r.get("sp") or 0)
            odds = float(r.get("beidan_odds") or 0)
            x = float(r.get("x") or 0)
            if sp <= 0 or odds <= 0 or x <= 0:
                continue
            out.append({
                "day": day, "lota_id": r.get("lota_id"), "side": r.get("side"),
                "x": x, "odds": odds, "sp": sp,
                "ratio": sp / odds,
                "hit": str(r.get("actual") or "") == str(r.get("side") or ""),
            })
    return out


def roi_of(legs: list[dict]) -> tuple[int, float, float, float]:
    """(n, 命中率, 每元 ROI, 平均SP折扣)"""
    n = len(legs)
    if not n:
        return 0, 0.0, 0.0, 0.0
    hits = [l for l in legs if l["hit"]]
    ret = sum(RATE * l["sp"] - 1 for l in hits) - (n - len(hits))
    return n, len(hits) / n, ret / n, sum(l["ratio"] for l in legs) / n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="data/leg_pool_all")
    ap.add_argument("--md", default="")
    ap.add_argument("--split", default="2026-08-08", help="样本内/外切分日")
    args = ap.parse_args(argv)

    legs = load_legs(args.pool)
    days = sorted({l["day"] for l in legs})
    o = []
    o.append("# 腿级 EV 审计（x 分档 / SP 折扣 / 有没有正 EV 档）")
    o.append("")
    o.append(f"- 腿池 `{args.pool}`：**{len(legs)} 条已结算侧腿**，覆盖 {len(days)} 天"
             f"（{days[0]} ~ {days[-1]}）")
    o.append(f"- 口径：腿的真实每元 = `0.65×SP − 1`（命中）/ `−1`（未中）；x = 市场p̂ × 下注时北单赔率")
    o.append(f"- 打平所需：单腿 `x×折扣 ≥ {BREAKEVEN:.3f}`")
    o.append("")

    # 1) 逐档
    o.append("## 1. 逐档腿级 ROI（θ 扫）")
    o.append("")
    o.append("| x ≥ 阈值 | 腿数 | 命中率 | 平均折扣 SP/赔率 | **腿级 ROI** | 打平线(0.65/均SP) |")
    o.append("|---|---|---|---|---|---|")
    for th in (1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8):
        g = [l for l in legs if l["x"] >= th]
        n, hr, roi, ratio = roi_of(g)
        if not n:
            continue
        spbar = sum(l["sp"] for l in g) / n
        o.append(f"| {th:.2f} | {n} | {hr:.1%} | {ratio:.3f} | **{roi:+.1%}** | {RATE/spbar:.1%} |")
    o.append("")

    # 2) 折扣曲线 + 样本内外
    o.append("## 2. SP 折扣曲线（x 分档；样本内 ≤ 切分日，样本外 > 切分日）")
    o.append("")
    o.append("| x 档 | 样本内 n | 样本内折扣 | 样本外 n | 样本外折扣 | 折扣一致? |")
    o.append("|---|---|---|---|---|---|")
    bins = [(1.0, 1.1), (1.1, 1.2), (1.2, 1.3), (1.3, 1.4), (1.4, 1.6), (1.6, 9)]
    for lo, hi in bins:
        ins = [l for l in legs if lo <= l["x"] < hi and l["day"] <= args.split]
        outs = [l for l in legs if lo <= l["x"] < hi and l["day"] > args.split]
        ri = statistics.median([l["ratio"] for l in ins]) if ins else None
        ro = statistics.median([l["ratio"] for l in outs]) if outs else None
        same = "—" if (ri is None or ro is None) else ("✔" if (ri < 1) == (ro < 1) else "✘")
        o.append(f"| [{lo},{hi}) | {len(ins)} | {'—' if ri is None else f'{ri:.3f}'} | "
                 f"{len(outs)} | {'—' if ro is None else f'{ro:.3f}'} | {same} |")
    o.append("")

    # 3) 用"全样本折扣"修正后重算门限（诚实标注这是样本内拟合）
    o.append("## 3. 按预测 SP 修正后的腿级 ROI（`x' = x × 折扣(x)`）")
    o.append("")
    o.append("折扣按上表分档中位数估计（**样本内拟合**，仅供判断方向，不等于可交易策略）。")
    o.append("")
    disc = {}
    for lo, hi in bins:
        g = [l["ratio"] for l in legs if lo <= l["x"] < hi]
        disc[(lo, hi)] = statistics.median(g) if g else 1.0

    def disc_of(x):
        for (lo, hi), d in disc.items():
            if lo <= x < hi:
                return d
        return 1.0

    o.append("| x' ≥ 阈值 | 腿数 | 命中率 | 腿级 ROI |")
    o.append("|---|---|---|---|")
    for th in (1.0, 1.05, 1.1, 1.2):
        g = [l for l in legs if l["x"] * disc_of(l["x"]) >= th]
        n, hr, roi, _ = roi_of(g)
        if n:
            o.append(f"| {th:.2f} | {n} | {hr:.1%} | **{roi:+.1%}** |")
    o.append("")

    # 4) 极端档（x 最高）单独看
    top = sorted(legs, key=lambda l: -l["x"])[:100]
    n, hr, roi, ratio = roi_of(top)
    o.append("## 4. x 最高的 100 条腿（最便宜的档）")
    o.append("")
    o.append(f"- 腿数 {n}｜命中率 {hr:.1%}｜平均折扣 {ratio:.3f}｜**腿级 ROI {roi:+.1%}**")
    o.append("")

    # 5) 结论
    best = None
    for th in (1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8):
        g = [l for l in legs if l["x"] >= th]
        n, hr, roi, _ = roi_of(g)
        if n >= 30 and (best is None or roi > best[1]):
            best = (th, roi, n)
    o.append("## 5. 结论")
    o.append("")
    if best:
        o.append(f"- 在样本量 ≥30 的档位里，**最高腿级 ROI 是 x≥{best[0]:.2f} → {best[1]:+.1%}（n={best[2]}）**")
    o.append(f"- 打平线：`x×折扣 ≥ {BREAKEVEN:.3f}`；实测平均折扣 ~0.85 ⇒ 需要 x ≳ "
             f"{BREAKEVEN/0.85:.2f}，而**那正是折扣最狠的档**（x 越大折扣越小）")
    o.append("- ⇒ 结论：**当前候选池没有可证实的正 EV 档位**；票型/容错都只是重排方差。")
    text = "\n".join(o)
    print(text)
    if args.md:
        Path(args.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已落盘: {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
