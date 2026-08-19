#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""""负因子护栏"测试：退役/休眠因子保留在 prompt 作为"已知陷阱"（归因+警示），
而不是彻底不用。测试 7.25 之后的梭哈2狗订单，若护栏生效会怎样。

护栏集（模拟 7.25 时点的系统认知）：
  - 主版本：07-24 复盘实际退役(9)+休眠(14) 的因子（当时系统已知的"负因子"）
  - 对照版：当前全部退役因子（29，含 08-02 复盘新增）
匹配规则：订单理由与护栏因子 desc 的关键词重叠分 ≥2 → 命中（该单可被归因到已知陷阱）
输出：命中单会被"警示/跳过"两种口径下的 PnL 对比 + 逐单归因。
"""

import json
import re
from datetime import date
from pathlib import Path

RETEST = Path("/private/tmp/ds_retest/data")
KEYWORDS = ["平局", "下盘", "不败", "受让", "深盘", "主胜", "极端", "凝聚",
            "必发", "高水", "升盘", "退盘", "穿盘", "低水", "重注", "水位",
            "诱", "浅盘", "大球", "小球", "冷门", "过热"]


def overlap(r, desc):
    return sum(1 for kw in KEYWORDS if kw in r and kw in (desc or ""))


def load():
    txt24 = open(RETEST / "sessions/梭哈2狗/2026-07-24T102000_factor_review_2026-07-24.md",
                 encoding="utf-8").read()
    j24 = json.loads(txt24.split("```json")[1].split("```")[0])
    retired24 = set(j24["retired"])
    dormant24 = set(j24["dormant"])

    mem = json.loads((RETEST / "roles/梭哈2狗/memory/factor_memory.json").read_text(encoding="utf-8"))["factor_perf"]
    current_retired = {fid for fid, s in mem.items() if s.get("status") == "retired"}

    role = json.loads((RETEST / "roles/梭哈2狗/梭哈2狗.json").read_text(encoding="utf-8"))
    orders = []
    for o in role.get("orders", []):
        sa = o.get("settled_at")
        if sa and date.fromisoformat(sa[:10]) >= date(2026, 7, 25):
            orders.append(o)
    return retired24, dormant24, current_retired, mem, orders


def main():
    retired24, dormant24, current_retired, mem, orders = load()
    guard_main = {f: mem[f] for f in retired24 | dormant24 if f in mem}
    guard_all = {f: mem[f] for f in current_retired if f in mem}

    print(f"护栏主版本: 07-24 退役({len(retired24)})+休眠({len(dormant24)}) = {len(guard_main)} 个因子")
    print(f"对照版本: 当前全部退役 {len(guard_all)} 个\n")

    for label, guard in [("07-24 护栏", guard_main), ("当前全部退役", guard_all)]:
        hits = []
        for o in orders:
            r = o.get("reason") or ""
            best = []
            for fid, s in guard.items():
                sc = overlap(r, s.get("desc") or "")
                if sc >= 2:
                    best.append((sc, fid, s.get("status")))
            best.sort(reverse=True)
            hits.append((o, best))
        n_hit = sum(1 for _, b in hits if b)
        pnl_actual = sum(float(o.get("profit") or 0) for o, _ in hits)
        pnl_skip = sum(float(o.get("profit") or 0) for o, b in hits if not b)
        hit_pnl = sum(float(o.get("profit") or 0) for o, b in hits if b)
        print(f"===== {label} =====")
        print(f"命中护栏的订单: {n_hit}/{len(orders)} 单（这部分 PnL {hit_pnl:+.0f}）")
        print(f"  若警示仍下注（归因式护栏）: PnL = {pnl_actual:+.0f}（命中单只是被标注，不改变行为）")
        print(f"  若命中即跳过（硬护栏）   : PnL = {pnl_skip:+.0f} (改善 {pnl_skip-pnl_actual:+.0f})")
        print()

    # 逐单归因明细（07-24 护栏命中单）
    print("===== 07-24 护栏命中逐单（归因展示） =====")
    for o in sorted(orders, key=lambda x: float(x.get("profit") or 0)):
        r = o.get("reason") or ""
        best = sorted(((overlap(r, s.get("desc") or ""), fid, s.get("status"))
                       for fid, s in guard_main.items()), reverse=True)
        if best and best[0][0] >= 2:
            sc, fid, st = best[0]
            print(f"  {o.get('settled_at','')[:10]} PnL={o.get('profit'):+7.0f} | 命中[{st}] {fid} (分{sc}) | {str(o.get('reason',''))[:45]}")


if __name__ == "__main__":
    main()
