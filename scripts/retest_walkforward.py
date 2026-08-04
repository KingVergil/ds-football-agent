#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""样本外（walk-forward）验证：因子禁用规则是否真有价值，还是纯后验拟合。

协议（尽量消除后验偏差）：
  1. 家族分类器用固定关键词（下单前定义，不随结果调整）。
  2. 规则阈值在 07-20 前冻结（常见风控口径，非按测试结果挑选）。
  3. 测试窗口 07-21~08-03 只做"盲测"：
     - frozen：禁用集合由训练期一次定死；
     - walkforward：每个订单时点只用该时点之前已实现的结果重算家族状态。
  4. 对照组：随机禁用同等数量的家族（5000 次），给出改善的零分布。
"""

import json
import random
from collections import defaultdict
from datetime import date
from pathlib import Path

RETEST = Path("/private/tmp/ds_retest/lota_data")
TRAIN_END = date(2026, 7, 20)      # 训练截止（不含）
TEST_LO = date(2026, 7, 21)
TEST_HI = date(2026, 8, 3)


def family_of(reason: str) -> str:
    r = reason or ""
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


def load_orders():
    role = json.loads((RETEST / "roles/梭哈2狗/梭哈2狗.json").read_text(encoding="utf-8"))
    out = []
    for o in role.get("orders", []):
        sa = o.get("settled_at")
        if not sa:
            continue
        try:
            d = date.fromisoformat(sa[:10])
        except ValueError:
            continue
        out.append({
            "d": d,
            "settled": sa[:10],
            "match": (o.get("match") or o.get("lota_id") or "")[:26],
            "fam": family_of(o.get("reason", "")),
            "profit": float(o.get("profit") or 0),
            "hit": o.get("hit"),
        })
    out.sort(key=lambda x: x["settled"])
    return out


def fam_stats(orders):
    st = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "consec": 0})
    for o in orders:
        s = st[o["fam"]]
        s["n"] += 1
        s["pnl"] += o["profit"]
        if o["hit"] is True:
            s["w"] += 1
            s["consec"] = 0
        elif o["hit"] is False:
            s["l"] += 1
            s["consec"] += 1
    for s in st.values():
        s["roi"] = s["pnl"] / (s["n"] * 10000) if s["n"] else 0  # 近似：注额约万级，直接用 n*10000 粗算展示
        s["wr"] = s["w"] / (s["w"] + s["l"]) if (s["w"] + s["l"]) else 0
    return st


def rule_block(st, variant):
    """冻结规则：输入家族训练期统计，输出是否禁用。"""
    if variant == "R_A":  # 训练期 ROI≤-10% 且 n≥3
        return st["n"] >= 3 and st["roi"] <= -0.10
    if variant == "R_B":  # 训练期累计亏损≥5000 且 n≥3
        return st["n"] >= 3 and st["pnl"] <= -5000
    if variant == "R_C":  # 胜率<45%(n≥5) 或 累计亏损≥10000(n≥3)
        return (st["n"] >= 5 and st["wr"] < 0.45) or (st["n"] >= 3 and st["pnl"] <= -10000)
    raise ValueError(variant)


def evaluate(orders, train, rule, mode):
    """mode: frozen | walkforward | none"""
    train_stats = fam_stats(train)
    blocked = set() if mode != "none" else None
    live = {k: dict(v) for k, v in train_stats.items()}
    fixed = {k: rule_block(v, rule) for k, v in train_stats.items()}
    pnl_actual = 0.0
    pnl_fixed = 0.0
    n_skip = 0
    for o in orders:
        fam = o["fam"]
        if mode == "frozen":
            skip = fixed.get(fam, False)
        elif mode == "walkforward":
            st = live.setdefault(fam, {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "consec": 0})
            skip = rule_block(st, rule)
        else:
            skip = False
        pnl_actual += o["profit"]
        if skip:
            n_skip += 1
        else:
            pnl_fixed += o["profit"]
        if mode == "walkforward":
            st = live.setdefault(fam, {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "consec": 0})
            st["n"] += 1
            st["pnl"] += o["profit"]
            if o["hit"] is True:
                st["w"] += 1
                st["consec"] = 0
            elif o["hit"] is False:
                st["l"] += 1
                st["consec"] += 1
    return pnl_actual, pnl_fixed, n_skip, fixed if mode == "frozen" else (live if mode == "walkforward" else {})


def main():
    orders = load_orders()
    train = [o for o in orders if o["d"] < TRAIN_END]
    test = [o for o in orders if TEST_LO <= o["d"] <= TEST_HI]
    print(f"训练: {len(train)} 单 | 测试: {len(test)} 单\n")

    ts = fam_stats(train)
    print("===== 训练期家族统计（07-10~07-19，规则只用这些） =====")
    for fam in sorted(ts):
        s = ts[fam]
        print(f"  {fam:18s} n={s['n']:3d} 胜率={s['wr']*100:5.1f}% ROI≈{s['roi']*100:+6.1f}% 累计PnL={s['pnl']:+9.0f} 期末连亏={s['consec']}")

    test_actual = sum(o["profit"] for o in test)
    print(f"\n===== 测试窗口盲测（07-21~08-03, 实际 PnL {test_actual:+.0f}） =====")

    results = {}
    for rule in ("R_A", "R_B", "R_C"):
        pa, pf, nskip, fixed = evaluate(test, train, rule, "frozen")
        pa2, pf2, nskip2, _ = evaluate(test, train, rule, "walkforward")
        results[rule] = (pf, nskip, pf2, nskip2)
        blk = [k for k, v in fixed.items() if v]
        print(f"\n[{rule}] 冻结禁用家族: {blk if blk else '无'}")
        print(f"  frozen     : 修复后 {pf:+.0f} (改善 {pf-test_actual:+.0f}) 禁 {nskip}/{len(test)} 单")
        print(f"  walkforward: 修复后 {pf2:+.0f} (改善 {pf2-test_actual:+.0f}) 禁 {nskip2}/{len(test)} 单")

    print("\n===== 随机对照（按各规则禁用数 ×5000 次） =====")
    fams = list(ts.keys())
    rng = random.Random(42)
    for rule in ("R_A", "R_B", "R_C"):
        pf, nskip, pf2, nskip2 = results[rule]
        _, _, _, fixed = evaluate(test, train, rule, "frozen")
        n_block = sum(1 for v in fixed.values() if v)
        if n_block == 0:
            print(f"\n[{rule}] 无禁用，跳过对照")
            continue
        sims = []
        for _ in range(5000):
            blk = set(rng.sample(fams, n_block))
            sims.append(sum(o["profit"] for o in test if o["fam"] not in blk))
        sims.sort()
        mean = sum(sims) / len(sims)
        p5, p95 = sims[250], sims[4749]
        rank_frozen = sum(1 for x in sims if x <= pf) / len(sims) * 100
        print(f"\n[{rule}] 禁用 {n_block} 个家族")
        print(f"  随机分布: 均值 {mean:+.0f} | 5%-95% [{p5:+.0f}, {p95:+.0f}]")
        print(f"  frozen 修复后 {pf:+.0f} (改善 {pf-test_actual:+.0f}) → 超过 {rank_frozen:.1f}% 的随机结果")
        print(f"  walkforward 修复后 {pf2:+.0f} (改善 {pf2-test_actual:+.0f})")


if __name__ == "__main__":
    main()
