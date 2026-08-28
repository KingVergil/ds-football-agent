"""
北单串关票型结构化验证：按票型（N串1 / N过M）校验注数计算。

用一组「单选/双选/全选」混合腿，逐票型核对：
  combos_count == expanded_count == reference_count。

运行:
  python3 scripts/verify_beidan_ticket_types.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_settlement import parse_ticket_spec, validate_parlay_type  # noqa: E402


TICKET_TYPES = [
    "2串1", "3串1", "4串1", "5串1", "6串1", "7串1", "8串1",
    "3过2", "4过3", "5过4", "6过5", "7过6", "8过7", "8过5", "8过6",
]


def build_mixed_legs() -> list[dict]:
    """8 腿混合：前 3 单选、中 2 双选、后 3 全选。"""
    pick_sets = [
        ["H"],
        ["A"],
        ["D"],
        ["H", "D"],
        ["H", "A"],
        ["H", "D", "A"],
        ["H", "D", "A"],
        ["H", "D", "A"],
    ]
    legs = []
    for i, picks in enumerate(pick_sets):
        legs.append({
            "lota_id": f"L{i + 1}",
            "picks": picks,
            "odds": {p: 2.0 for p in picks},
        })
    return legs


def main() -> int:
    legs = build_mixed_legs()
    fails = 0
    print(f"{'票型':<6} {'kind':<4} {'N':>2} {'M':>2} {'注数':>6} {'展开':>6} {'参考':>6}  结果")
    for tk in TICKET_TYPES:
        spec = parse_ticket_spec(tk)
        if not spec:
            print(f"{tk:<6} 解析失败")
            fails += 1
            continue
        r = validate_parlay_type(legs, spec)
        flag = "✅" if r["ok"] else "❌"
        if not r["ok"]:
            fails += 1
        print(f"{r['spec']:<6} {r['kind']:<4} {r['n']:>2} {r['m']:>2} "
              f"{r['combos_count']:>6} {r['expanded_count']:>6} "
              f"{r['reference_count']:>6}  {flag}")
    print()
    print("✅ 全部票型注数校验通过" if fails == 0 else f"❌ {fails} 个票型校验失败")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
