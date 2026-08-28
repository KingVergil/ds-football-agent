"""
北单串关狗：一轮 LLM 分析 → 下单 → 结算（临时沙箱角色，不碰线上角色）。

运行（需要联网调 DeepSeek）:
  /Users/cjy/miniconda3/bin/python scripts/run_beidan_llm_round.py [足球日]
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["DS_ROLES_ROOT"] = tempfile.mkdtemp(prefix="beidan_llm_roles_")
os.environ["DS_SESSIONS_ROOT"] = tempfile.mkdtemp(prefix="beidan_llm_sessions_")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_parlay_dog import BeidanParlayDog  # noqa: E402
from src.beidan_settlement import BEIDAN_RETURN_RATE  # noqa: E402


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-08-24"
    dog = BeidanParlayDog(user="llm_round")
    dog.reset()

    a = dog.analyze(day, use_llm=True)
    src = "🧠LLM" if a.get("llm_used") else "📐规则(LLM失败回退)"
    print(f"LLM 分析: {src} | 票型 {a.get('tickets')} | 候选腿 {a.get('legs_selected')} "
          f"| 下单 {a.get('placed')} 注")

    role = dog._ensure_role()
    leg_seen: dict[str, dict] = {}
    for o in role.get_orders():
        for l in o["legs"]:
            if l["lota_id"] not in leg_seen:
                leg_seen[l["lota_id"]] = l

    singles = [l for l in leg_seen.values() if len(l.get("picks") or []) == 1]
    covers = [l for l in leg_seen.values() if len(l.get("picks") or []) >= 2]
    print("\n单选腿（8串1）:")
    for l in singles:
        print(f"  - {l['home_name']} vs {l['away_name']} 让{l['goal_line']} 选 {l['pick']}")
    print("全包腿（8串1，胜平负全选）:")
    for l in covers:
        print(f"  - {l['home_name']} vs {l['away_name']} 让{l['goal_line']} H/D/A")

    s = dog.settle(day)
    role = dog._ensure_role()
    print(f"\n结算: {s['settled']} 注 | 命中 {s['hit']} | 未中 {s['miss']} | "
          f"退款 {s['push']} | PnL {s['pnl']:+.2f} | 资金 {role.capital:.2f}")

    for o in role.get_orders():
        if o.get("settled_at") and o.get("hit"):
            sp = float(o["sp_product"])
            print(f"\n🎯 命中单 [{o['ticket_type']}] "
                  f"{'+'.join(l['pick'] for l in o['legs'])}")
            print(f"   SP连乘={sp} | 返奖 = 本金{o['bet_size']} × {sp} × "
                  f"{BEIDAN_RETURN_RATE} = {o['return_amount']:.2f} 元")
    return 0


if __name__ == "__main__":
    sys.exit(main())
