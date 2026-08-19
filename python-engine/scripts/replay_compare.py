#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比 LLM 回放决策 vs 历史真实决策（07-13~07-25）。

读 retest 环境回放后的角色订单（match_time 落在窗口内）与生产环境同期订单，
按 agent 汇总：下单数、类型、注额、盈亏（历史）/待定（回放）。
"""

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

PROD = Path("/Users/cjy/Desktop/code/ds_agents/data/roles")
PROD_DATA = Path("/Users/cjy/Desktop/code/ds_agents/data")
OLD_RETEST = Path("/private/tmp/ds_retest_old/data/roles")
OLD_RETEST_DATA = Path("/private/tmp/ds_retest_old/data")
DEDUP_RETEST = Path("/private/tmp/ds_retest/data/roles")
DEDUP_RETEST_DATA = Path("/private/tmp/ds_retest/data")
LO = date(2026, 7, 15)
HI = date(2026, 7, 25)
DOGS = ["alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗"]


def lid_date_map(data_root):
    """从 matches 缓存构建 lota_id -> match 日期 映射。"""
    m = {}
    for f in (data_root / "matches").glob("*.json"):
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for x in rows if isinstance(rows, list) else rows.get("matches", []):
            mt = x.get("match_time") or ""
            if x.get("lota_id") and mt:
                m[x["lota_id"]] = mt[:10]
    return m


def orders_of(root, data_root, dog, lo, hi):
    f = root / dog / f"{dog}.json"
    try:
        role = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    ldmap = lid_date_map(data_root)
    out = []
    for o in role.get("orders", []):
        dstr = ldmap.get(o.get("lota_id", ""))
        if not dstr:
            continue
        try:
            d = date.fromisoformat(dstr)
        except ValueError:
            continue
        if lo <= d <= hi:
            out.append(o)
    return out


def rep(orders, label):
    n = len(orders)
    if not n:
        print(f"  {label}: 0 单")
        return
    stake = sum(float(o.get("bet_size") or 0) for o in orders)
    pnl = sum(float(o.get("profit") or 0) for o in orders)
    settled = sum(1 for o in orders if o.get("settled_at"))
    types = defaultdict(int)
    for o in orders:
        types[o.get("bet_type", "?")] += 1
    roi = pnl / stake * 100 if stake else 0
    print(f"  {label}: {n} 单 | 注额 {stake:>9.0f} | PnL {pnl:+9.0f} | ROI {roi:+6.1f}% | 已结算 {settled}")


def main():
    print("===== 07-15 ~ 07-25 三路对比（历史 vs 旧代码回放 vs 去重+当前代码回放） =====")
    for dog in DOGS:
        hist = orders_of(PROD, PROD_DATA, dog, LO, HI)
        old = orders_of(OLD_RETEST, OLD_RETEST_DATA, dog, LO, HI)
        dedup = orders_of(DEDUP_RETEST, DEDUP_RETEST_DATA, dog, LO, HI)
        print(f"\n--- {dog} ---")
        rep(hist, "历史")
        rep(old, "旧代码回放")
        rep(dedup, "去重+当前代码回放")


if __name__ == "__main__":
    main()
