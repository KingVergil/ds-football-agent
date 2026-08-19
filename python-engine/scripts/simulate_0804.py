#!/usr/bin/env python3
"""仿真重放 2026-08-04 分析（不下真实单，落到 _sim 临时角色）。

用法: python scripts/simulate_0804.py <sim_role_name>
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent import Agent
from src.providers.deepseek import DeepSeekProvider


def main():
    user = sys.argv[1]
    a = Agent(user=user)
    a.set_provider(DeepSeekProvider())
    # live=True 才会走 LLM 节点（should_call_llm 要求 live 且有 provider）；
    # prefetched=True 复用已预取的 8-04 数据，不强制联网刷新。
    r = a.analyze("2026-08-04", live=True, jingcai_only=True, prefetched=True)

    print(f"\n=== {user} 仿真结果 ===")
    print(f"比赛: {r['matches_count']} | 下单: {r['placed']}")
    for o in r["orders"]:
        lid = o.get("lota_id", "?")
        if o.get("skip"):
            print(f"  ⏭ {lid} skip: {o.get('reason', '')[:60]}")
        else:
            print(f"  ✅ {lid} {o.get('bet_type','')} {o.get('pick','')} "
                  f"@{o.get('odds', 0):.2f} bet {o.get('bet_size', 0):.0f} "
                  f"— {o.get('reason', '')[:70]}")


if __name__ == "__main__":
    main()
