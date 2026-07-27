#!/usr/bin/env python3
"""
批量结算所有 agent 订单.

用法:
  python settle_all.py 2026-07-07              # 单日结算
  python settle_all.py 2026-07-01 2026-07-07   # 日期范围结算
"""
import sys
import time
from pathlib import Path
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))

from src.agent import Agent

ALL_DOGS = ["梭哈2狗", "梭哈3狗", "alpha狗", "alpha2狗", "平局狗", "跟风狗"]


def settle_one(agent: Agent, day: str) -> dict:
    """结算单个 agent 某一天, 返回结果摘要."""
    try:
        s = agent.settle(day)
        return {
            "agent": agent.user,
            "day": day,
            "settled": s.get("settled", 0),
            "hit": s.get("hit", 0),
            "miss": s.get("miss", 0),
            "push": s.get("push", 0),
            "pnl": s.get("pnl", 0),
            "capital": agent._get_capital(),
            "ok": True,
        }
    except Exception as e:
        return {"agent": agent.user, "day": day, "ok": False, "error": str(e)}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else start

    dates = []
    d = start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=1)

    print(f"🔄 批量结算 | {len(ALL_DOGS)} agents × {len(dates)} 天")
    print(f"   Agents: {', '.join(ALL_DOGS)}")
    print(f"   日期: {start} ~ {end}")
    print("=" * 60)

    t0 = time.time()
    grand_total = {"settled": 0, "hit": 0, "miss": 0, "push": 0, "pnl": 0.0}
    errors = []

    for day in dates:
        print(f"\n📅 {day}")

        # 并行结算当天全部 agent
        results = []
        with ThreadPoolExecutor(max_workers=len(ALL_DOGS)) as pool:
            futures = {}
            for name in ALL_DOGS:
                agent = Agent(user=name)
                futures[pool.submit(settle_one, agent, day)] = name

            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                if r["ok"]:
                    print(f"  {'✅' if r['pnl'] >= 0 else '🔻'} {r['agent']:8s} | "
                          f"{r['settled']}单 ✅{r['hit']} ❌{r['miss']} ➖{r['push']} | "
                          f"PnL {r['pnl']:+.0f} | 余额 {r['capital']:,.0f}")
                    for k in ("settled", "hit", "miss", "push"):
                        grand_total[k] += r[k]
                    grand_total["pnl"] += r["pnl"]
                else:
                    print(f"  ❌ {r['agent']:8s} | 错误: {r['error']}")
                    errors.append(r)

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"📊 汇总 | {grand_total['settled']}单 "
          f"✅{grand_total['hit']} ❌{grand_total['miss']} ➖{grand_total['push']} | "
          f"PnL {grand_total['pnl']:+.0f}")
    print(f"⏱ 耗时 {elapsed:.1f}s")
    if errors:
        print(f"⚠️ {len(errors)} 个错误:")
        for e in errors:
            print(f"  - {e['agent']} {e['day']}: {e['error']}")


if __name__ == "__main__":
    main()
