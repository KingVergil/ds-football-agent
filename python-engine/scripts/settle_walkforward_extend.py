"""结算 walk-forward 缺的 08-25/08-26 天，并补跑 08-27（足球日）作为回测。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WF_ROOT = ROOT / "data" / "beidan_walkforward"
DOGS = ["北单串关狗", "北单双选串关狗"]
EXTEND_DAY = "2026-08-27"


def run_dog(dog_name: str, extend_day: str) -> None:
    run_root = WF_ROOT / dog_name
    os.environ["DS_ROLES_ROOT"] = str(run_root / "roles")
    os.environ["DS_SESSIONS_ROOT"] = str(run_root / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(run_root / "factors")

    from src.beidan_parlay_dog import BeidanParlayDog
    dog = BeidanParlayDog(user=dog_name, capital=5000.0)
    role = dog._ensure_role()
    before_pending = sum(1 for o in role.get_orders() if not o.get("settled_at"))
    print(f"=== {dog_name} ===", flush=True)
    print(f"  结算前: 资金 {role.capital:.0f} | pending {before_pending}", flush=True)

    s = dog.settle()  # 结算全部 pending（08-25/08-26 现在有 SP）
    role = dog._ensure_role()
    after_pending = sum(1 for o in role.get_orders() if not o.get("settled_at"))
    print(f"  补结算: settled {s['settled']} | hit {s['hit']} | miss {s['miss']} | "
          f"pnl {s['pnl']:+.0f} | 资金 {role.capital:.0f} | pending {after_pending}",
          flush=True)

    a = dog.analyze(extend_day, use_llm=True)
    s2 = dog.settle(extend_day)
    role = dog._ensure_role()
    print(f"  {extend_day}: placed {a['placed']} | settled {s2['settled']} | "
          f"hit {s2['hit']} | miss {s2['miss']} | pnl {s2['pnl']:+.0f} | "
          f"资金 {role.capital:.0f}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dog", default=None, help="只处理指定狗；不传则两只")
    ap.add_argument("--extend", default=EXTEND_DAY, help="补跑足球日，默认 2026-08-27")
    args = ap.parse_args()
    dogs = [args.dog] if args.dog else DOGS
    for d in dogs:
        run_dog(d, args.extend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
