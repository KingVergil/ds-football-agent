#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比两份回放报告（python 引擎 vs JS ds_replay）。

用法:
  python scripts/compare_replays.py <reportA.json> <reportB.json>

两份报告的 schema 都可接受：
  - JS ds_replay: trajectory = [{day, dogs: {狗名: {capital, pnl, settled, ...}}}]
  - Python runall: trajectory = [{day, capital, pnl, settled, placed, pending, active_factors}]
"""
import json
import sys
from pathlib import Path


def traj_rows(traj, dogs):
    rows = []
    for t in traj:
        if "dogs" in t:  # JS schema
            for dog, v in t["dogs"].items():
                rows.append((t["day"], dog, v))
        else:  # Python schema（单狗扁平）
            rows.append((t["day"], dogs[0], t))
    return rows


def load(p: str) -> dict:
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    return {
        "run_id": d.get("run_id"),
        "engine": d.get("engine", "js"),
        "range": d["range"],
        "dogs": d["dogs"],
        "start_capital": d["start_capital"],
        "end_capital": d["end_capital"],
        "user_notes": d.get("user_notes", ""),
        "rows": traj_rows(d["trajectory"], d["dogs"]),
        "reviews": d.get("factor_reviews", []),
    }


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    a, b = load(sys.argv[1]), load(sys.argv[2])
    print(f"## {a['engine']} vs {b['engine']} 回放对比")
    print(f"- {a['run_id']} vs {b['run_id']}")
    print(f"- 范围: {a['range']} vs {b['range']} | 狗: {a['dogs']} vs {b['dogs']}")
    print(f"- user_notes: {a['user_notes'][:40]!r} vs {b['user_notes'][:40]!r}")
    print(f"- 起点资金: {a['start_capital']} vs {b['start_capital']}")
    print(f"- 终点资金: {a['end_capital']} vs {b['end_capital']}")

    key = lambda r: (r[0], r[1])
    ma, mb = {key(r): r for r in a["rows"]}, {key(r): r for r in b["rows"]}
    print("\n| 日期 | 引擎 | 下单 | 结算 | PnL | 资金 | 待定 | 活跃因子 |")
    print("|---|---|---|---|---|---|---|---|")
    for day, dog in sorted(set(ma) | set(mb)):
        for eng, m in ((a["engine"], ma), (b["engine"], mb)):
            r = m.get((day, dog))
            if not r:
                continue
            v = r[2]
            print(f"| {day} | {eng} | {v.get('placed', '-')} | {v.get('settled', 0)} | "
                  f"{v.get('pnl', 0):+.1f} | {v.get('capital', '-')} | {v.get('pending', 0)} | {v.get('active_factors', 0)} |")

    print(f"\n因子退役: {a['engine']}={len(a['reviews'])} 次, {b['engine']}={len(b['reviews'])} 次")
    for eng, revs in ((a["engine"], a["reviews"]), (b["engine"], b["reviews"])):
        for r in revs:
            retired = r.get("retired") or []
            dorm = r.get("dormant") or []
            print(f"- {eng} [{r.get('day')}] 候选={r.get('candidates', '-')} "
                  f"退役={len(retired)} 休眠={len(dorm)}")


if __name__ == "__main__":
    main()
