"""
北单串关狗 端到端验证（用临时沙箱角色，不碰线上角色）。

验证 analyze(选腿/组票/落单) → settle(按官方 result + spvalue × 65%) 整条链路：
  - 下单数 == 结算数
  - 资金守恒: 期末资金 == 期初 - Σbet + Σreturn
  - 每张结算单 settlement_rate == 0.65，return = bet × sp_product × 0.65
  - 每腿官方 result 与 score+goal_line 推导一致（mismatch == False）

运行:
  /Users/cjy/miniconda3/bin/python scripts/verify_beidan_dog_e2e.py [日期] [票型] [picks]
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["DS_ROLES_ROOT"] = tempfile.mkdtemp(prefix="beidan_e2e_roles_")
os.environ["DS_SESSIONS_ROOT"] = tempfile.mkdtemp(prefix="beidan_e2e_sessions_")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_settlement import BEIDAN_RETURN_RATE  # noqa: E402
from src.beidan_parlay_dog import BeidanParlayDog  # noqa: E402


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-08-20"
    tickets = [sys.argv[2]] if len(sys.argv) > 2 else ["2串1"]
    max_picks = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 2

    dog = BeidanParlayDog(user="e2e_sandbox")
    dog.reset()
    role = dog._ensure_role()
    capital0 = role.capital

    a = dog.analyze(day, tickets=tickets, max_picks=max_picks)
    print(f"analyze {day}: 场次{a['matches_count']} 候选腿{a['legs_selected']} "
          f"票型{a['tickets']} 每腿≤{a['max_picks']}选 下单{a['placed']}")

    role = dog._ensure_role()
    placed = [o for o in role.get_orders() if not o.get("settled_at")]
    total_bet = round(sum(float(o.get("bet_size", 0)) for o in placed), 2)

    s = dog.settle(day)
    print(f"settle {day}: 结算{s['settled']} 命中{s['hit']} 未中{s['miss']} "
          f"退款{s['push']} PnL {s['pnl']:+.2f}")

    role = dog._ensure_role()
    settled = [o for o in role.get_orders() if o.get("settled_at")]
    total_return = round(sum(float(o.get("return_amount", 0)) for o in settled), 2)

    errors = []
    if s["settled"] != a["placed"]:
        errors.append(f"结算数 {s['settled']} != 下单数 {a['placed']}")

    # 资金守恒
    expected_capital = round(capital0 - total_bet + total_return, 2)
    if round(role.capital, 2) != expected_capital:
        errors.append(f"资金不守恒: 实际 {role.capital:.2f} 期望 {expected_capital:.2f}")

    for o in settled:
        if o.get("settlement_rate") != BEIDAN_RETURN_RATE:
            errors.append(f"{o['id']} 返奖率 {o.get('settlement_rate')} != 0.65")
        sp = 1.0
        for leg in o["legs"]:
            if leg.get("mismatch"):
                errors.append(f"{o['id']} 腿 {leg['lota_id']} result 与 goal-line 不一致")
            sp *= float(leg.get("sp", 0.0))
        if abs(float(o.get("sp_product", 0.0)) - sp) > 1e-6:
            errors.append(f"{o['id']} sp_product {o.get('sp_product')} != 连乘 {sp}")
        if o.get("hit") and not o.get("all_void"):
            want = round(float(o["bet_size"]) * sp * BEIDAN_RETURN_RATE, 2)
            if float(o["return_amount"]) != want:
                errors.append(f"{o['id']} return {o['return_amount']} != 期望 {want}")
        elif o.get("all_void"):
            if float(o["return_amount"]) != float(o["bet_size"]):
                errors.append(f"{o['id']} 全延期应退本金 {o['bet_size']}")
        else:
            if float(o["return_amount"]) != 0.0:
                errors.append(f"{o['id']} 未中应 return 0")

    print(f"资金: 期初 {capital0:.2f} → 期末 {role.capital:.2f} | "
          f"总投注 {total_bet:.2f} 总派奖 {total_return:.2f}")
    if errors:
        print("❌ 失败:")
        for e in errors[:20]:
            print("   ", e)
        return 1
    print("✅ 端到端全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
