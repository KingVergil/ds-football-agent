#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策级 what-if 复盘：验证"因子修复"对上周梭哈2狗亏损单的影响。

说明：
- 不重跑 LLM（不可复现），而是用订单理由 + 因子记忆历史做确定性模拟。
- 输入：/private/tmp/ds_retest/lota_data（角色订单、factor_memory、reflection_memory）。
- 修复规则（模拟）：
    R1 快速退役：某模式家族出现连续 ≥2 亏损 或 累计 PnL ≤ -10000 → 该家族禁用（含此前因子历史）。
    R2 反思联动：家族被禁用后，注入的同模式背书反思视为失效（提示模型勿用）。
- 两种变体：
    variant=seeded   ：用因子记忆历史（07-29 之前的亏损）给家族初始状态（更接近"修复一直生效"）
    variant=online   ：只从窗口内订单实时累计（更保守，只测"连续亏损后禁"）
"""

import json
import re
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path

RETEST = Path("/private/tmp/ds_retest/lota_data")


def family_of(reason: str) -> str:
    """按下单理由给订单归类（关键词规则，透明可调）。"""
    r = reason or ""
    if "平局" in r and any(k in r for k in ["下盘", "不败", "受让", "即赢盘", "平局凝聚", "平局离散"]):
        return "P1_平局离散博下盘"
    if "主胜" in r and any(k in r for k in ["极端低位", "极度凝聚", "极端凝聚"]) and any(
        k in r for k in ["高水", "浅盘", "半球", "平半", "半一", "-0.5", "-0.25"]
    ):
        return "P2_浅盘极端凝聚上盘"
    if "深盘" in r and any(k in r for k in ["凝聚", "极端", "实力差"]):
        return "P3_深盘凝聚"
    if "必发" in r and any(k in r for k in ["买盘", "买压", "卖盘", "资金"]):
        return "P4_必发资金"
    return "P5_其他"


def seed_family_state(factor_perf: dict, before: date) -> dict:
    """从因子记忆历史构造 07-29 之前的家族累计 PnL / 连亏。"""
    fam = {}
    for fid, s in factor_perf.items():
        f = family_of((s.get("desc") or "") + " " + fid)
        if f.startswith("P5"):
            continue
        acc = fam.setdefault(f, {"pnl": 0.0, "consec": 0})
        for h in sorted(s.get("history") or [], key=lambda x: x.get("date", "")):
            try:
                d = date.fromisoformat((h.get("date") or "")[:10])
            except ValueError:
                continue
            if d >= before:
                continue
            acc["pnl"] += float(h.get("profit") or 0)
            hit = h.get("hit")
            if hit is False:
                acc["consec"] += 1
            elif hit is True:
                acc["consec"] = 0
    return fam


def run(orders, factor_perf, before: date, variant: str):
    """返回逐单决策。"""
    fam_state = seed_family_state(factor_perf, before) if variant == "seeded" else {}
    blocked = set()
    rows = []
    for o in sorted(orders, key=lambda x: x["settled_at"]):
        fam = family_of(o.get("reason", ""))
        pnl = float(o.get("profit") or 0)
        hit = o.get("hit")
        st = fam_state.setdefault(fam, {"pnl": 0.0, "consec": 0})
        is_blocked = fam in blocked
        if not is_blocked and (
            st["consec"] >= 2 or st["pnl"] <= -10000
        ):
            is_blocked = True
            blocked.add(fam)
        # 更新实时状态（无论是否被禁，都累计真实结果；被禁后不再解除）
        st["pnl"] += pnl
        if hit is False:
            st["consec"] += 1
        elif hit is True:
            st["consec"] = 0
        rows.append({
            "settled": o["settled_at"][:10],
            "match": (o.get("match") or o.get("lota_id") or "")[:26],
            "fam": fam,
            "profit": pnl,
            "hit": "W" if hit is True else ("L" if hit is False else "P"),
            "blocked": is_blocked,
        })
    return rows


def main() -> int:
    role = json.loads((RETEST / "roles/梭哈2狗/梭哈2狗.json").read_text(encoding="utf-8"))
    mem = json.loads((RETEST / "roles/梭哈2狗/memory/factor_memory.json").read_text(encoding="utf-8"))
    orders = []
    for o in role.get("orders", []):
        sa = o.get("settled_at")
        if not sa:
            continue
        try:
            d = date.fromisoformat(sa[:10])
        except ValueError:
            continue
        if date(2026, 7, 29) <= d <= date(2026, 8, 3):
            orders.append(o)

    before = date(2026, 7, 29)
    for variant in ("online", "seeded"):
        rows = run(orders, mem["factor_perf"], before, variant)
        actual = sum(r["profit"] for r in rows)
        fixed = sum(r["profit"] for r in rows if not r["blocked"])
        saved = fixed - actual
        print(f"\n########## variant={variant} ##########")
        print(f"{'日期':10s} {'比赛':26s} {'家族':20s} {'盈亏':>9s} {'结果':4s} {'修复后':6s}")
        for r in rows:
            print(f"{r['settled']:10s} {r['match']:26s} {r['fam']:20s} {r['profit']:>+9.0f} {r['hit']:4s} {'⛔禁' if r['blocked'] else '—'}")
        n_blk = sum(1 for r in rows if r["blocked"])
        print(f"\n实际 PnL: {actual:+.0f} | 修复后 PnL: {fixed:+.0f} | 改善: {saved:+.0f} | 被禁单数: {n_blk}/{len(rows)}")

    # 反思联动检查
    refs = json.loads((RETEST / "roles/梭哈2狗/memory/reflection_memory.json").read_text(encoding="utf-8"))
    endorsing = [
        (r.get("date", ""), r.get("reflection", "")[:60])
        for r in refs.get("reflections", [])
        if "平局" in (r.get("reflection") or "") and any(
            k in (r.get("reflection") or "") for k in ["下盘不败", "受让不败", "可重注", "证明该因子有效"]
        )
    ]
    print("\n########## 反思联动（R2）##########")
    print("窗口前存在'平局博下盘'背书反思（修复后会被标记失效）:")
    for d, t in sorted(endorsing):
        print(f"  [{d}] {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
