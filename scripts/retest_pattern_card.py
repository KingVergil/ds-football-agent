#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟"自身模式统计卡"：梭哈2狗下单时能看到该模式的池化滚动统计，决策如何变化。

协议：
  - 决策时点 = settled_at（近似下单时点，数据只有结算时间）。
  - 卡片内容 = 全 7 狗在 [D-14, D-1] 已结算的同家族订单：n / 胜率 / ROI(注额加权)。
  - 只用该时点之前已实现的结果（walk-forward，无偷看）。
  - 模拟规则（LLM 看卡后的简化行为）：
      skip     : 卡片 ROI<0 且 n≥阈 → 整单跳过
      halve    : 同上 → 仓位减半（利润×0.5）
      weight   : ROI<0 → 0.5 倍；ROI>0 且 n≥阈 → 1.2 倍；否则 1 倍
  输出：实际 vs 各规则的测试期 PnL，及逐单决策表（梭哈2狗窗口内）。
"""

import json
import glob
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

RETEST = Path("/private/tmp/ds_retest/lota_data")
WIN_LO = date(2026, 7, 21)
WIN_HI = date(2026, 8, 3)
ROLL = 14


def fam(r):
    r = r or ""
    if "平局" in r and any(k in r for k in ["下盘", "不败", "受让", "即赢盘"]):
        return "P1_平局博下盘"
    if "主胜" in r and any(k in r for k in ["极端低位", "极度凝聚", "极端凝聚", "持续低位"]) and any(
        k in r for k in ["高水", "浅盘", "半球", "平半", "半一", "让球"]
    ):
        return "P2_浅盘凝聚上盘"
    if "深盘" in r:
        return "P3_深盘凝聚"
    if "必发" in r:
        return "P4_必发资金"
    return "P5_其他"


def load_all():
    out = []
    for f in sorted(glob.glob(str(RETEST / "roles/*/*.json"))):
        a = os.path.basename(os.path.dirname(f))
        if os.path.basename(f) != f"{a}.json":
            continue
        try:
            role = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for o in role.get("orders", []):
            sa = o.get("settled_at")
            if not sa:
                continue
            try:
                d = date.fromisoformat(sa[:10])
            except ValueError:
                continue
            out.append({
                "dog": a,
                "d": d,
                "settled": sa[:10],
                "fam": fam(o.get("reason", "")),
                "profit": float(o.get("profit") or 0),
                "stake": float(o.get("bet_size") or 0),
                "hit": o.get("hit"),
                "match": (o.get("match") or o.get("lota_id") or "")[:24],
            })
    out.sort(key=lambda x: (x["settled"], x["dog"]))
    return out


def card(all_orders, fam_key, d):
    """[d-14, d-1] 全狗同家族统计。"""
    lo = d - timedelta(days=ROLL)
    rows = [o for o in all_orders if o["fam"] == fam_key and lo <= o["d"] < d]
    n = len(rows)
    stake = sum(o["stake"] for o in rows)
    pnl = sum(o["profit"] for o in rows)
    w = sum(1 for o in rows if o["hit"] is True)
    l = sum(1 for o in rows if o["hit"] is False)
    return {
        "n": n,
        "pnl": pnl,
        "roi": pnl / stake if stake else 0.0,
        "wr": w / (w + l) if (w + l) else 0.0,
    }


def simulate(all_orders, rule, n_thr):
    rows = []
    for o in all_orders:
        if not (WIN_LO <= o["d"] <= WIN_HI) or o["dog"] != "梭哈2狗":
            continue
        c = card(all_orders, o["fam"], o["d"])
        scale = 1.0
        signal = c["n"] >= n_thr and c["roi"] < 0
        if rule == "skip":
            scale = 0.0 if signal else 1.0
        elif rule == "halve":
            scale = 0.5 if signal else 1.0
        elif rule == "weight":
            if signal:
                scale = 0.5
            elif c["n"] >= n_thr and c["roi"] > 0:
                scale = 1.2
        rows.append({**o, "scale": scale, "card": c})
    return rows


def main():
    all_orders = load_all()
    print(f"全狗订单: {len(all_orders)} | 窗口: {WIN_LO}~{WIN_HI}")
    actual = sum(o["profit"] for o in all_orders if o["dog"] == "梭哈2狗" and WIN_LO <= o["d"] <= WIN_HI)
    print(f"梭哈2狗窗口内实际 PnL: {actual:+.0f}\n")

    for n_thr in (10, 20):
        print(f"########## n 阈值 = {n_thr} ##########")
        for rule in ("skip", "halve", "weight"):
            rows = simulate(all_orders, rule, n_thr)
            fixed = sum(o["profit"] * o["scale"] for o in rows)
            print(f"  [{rule:6s}] 修复后 {fixed:+.0f} (改善 {fixed-actual:+.0f})")
        # 打印 skip 规则的逐单明细（信息量最大）
        rows = simulate(all_orders, "skip", n_thr)
        print(f"\n  -- skip 逐单（窗口内梭哈2狗 {len(rows)} 单）--")
        for o in rows:
            c = o["card"]
            tag = "⛔跳" if o["scale"] == 0 else "  "
            print(f"  {o['settled']} {o['match']:24s} {o['fam']:18s} n={c['n']:3d} ROI={c['roi']*100:+6.1f}% PnL={o['profit']:+7.0f} {tag}")
        print()


if __name__ == "__main__":
    main()
