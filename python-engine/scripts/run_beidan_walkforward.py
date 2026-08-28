"""
北单串关狗 walk-forward 串行回放（和 agent 狗一样的天循环）。

每天: analyze(LLM) → settle + factor reflect
每 N 天(默认7): 因子去重(dedup)
资金跨天滚动，模拟真实破产/回撤。

用法:
  python scripts/run_beidan_walkforward.py --dog 北单串关狗 --start 2026-06-28 --end 2026-08-26
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_ROOT = ROOT / "data" / "beidan_walkforward"


def _copy_role_assets(dog_name: str, role_root: Path) -> None:
    role_root.mkdir(parents=True, exist_ok=True)
    src_dir = ROOT / "data" / "roles" / dog_name
    for fn in ("persona.md", "parlay.json"):
        src = src_dir / fn
        if src.exists():
            (role_root / fn).write_text(src.read_text(encoding="utf-8"),
                                        encoding="utf-8")


def _dedup_factors(dog) -> int:
    """按 clean_name 去重合并（确定性，不调 LLM）。"""
    from src.factor_induction import clean_name, merge_entries
    role = dog._ensure_role()
    role.memory.factors.load()
    fp = role.memory.factors.factor_perf
    if not fp:
        return 0
    groups: dict[str, list[tuple[str, dict]]] = {}
    for name, entry in fp.items():
        groups.setdefault(clean_name(name), []).append((name, entry))
    new_fp: dict[str, dict] = {}
    for items in groups.values():
        items.sort(key=lambda x: -(x[1].get("total", 0) or 0))
        base_name, base_entry = items[0]
        base_entry["_name"] = base_name
        for name, entry in items[1:]:
            merge_entries(base_entry, entry, name)
        new_fp[base_name] = base_entry
    role.memory.factors.factor_perf = new_fp
    role.memory.factors._save()
    return len(new_fp)


def _date_range(start: str, end: str) -> list[str]:
    d = date.fromisoformat(start)
    e = date.fromisoformat(end)
    out = []
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dog", default="北单串关狗")
    ap.add_argument("--start", default="2026-06-28")
    ap.add_argument("--end", default="2026-08-26")
    ap.add_argument("--capital", type=float, default=5000.0)
    ap.add_argument("--dedup-interval", type=int, default=7)
    ap.add_argument("--run-root", default=None)
    args = ap.parse_args()

    run_root = Path(args.run_root or (TEST_ROOT / args.dog))
    os.environ["DS_ROLES_ROOT"] = str(run_root / "roles")
    os.environ["DS_SESSIONS_ROOT"] = str(run_root / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(run_root / "factors")
    from src.beidan_parlay_dog import BeidanParlayDog
    _copy_role_assets(args.dog, Path(os.environ["DS_ROLES_ROOT"]))

    dog = BeidanParlayDog(user=args.dog, capital=args.capital)
    dog.reset(args.capital)
    days = _date_range(args.start, args.end)

    trajectory = []
    print(f"walkforward {args.dog} {args.start}~{args.end} capital={args.capital:.0f} "
          f"dedup每{args.dedup_interval}天", flush=True)
    for i, d in enumerate(days, 1):
        t0 = time.time()
        try:
            a = dog.analyze(d, use_llm=True)
            s = dog.settle(d, reflect=True)
        except Exception as e:
            print(f"  ❌ {d} 异常: {e}", flush=True)
            continue
        role = dog._ensure_role()
        try:
            role.memory.factors.load()
            nf = len(role.memory.factors.factor_perf or {})
        except Exception:
            nf = 0
        row = {"day": d, "placed": a.get("placed", 0), "settled": s.get("settled", 0),
               "hit": s.get("hit", 0), "miss": s.get("miss", 0),
               "push": s.get("push", 0), "pnl": s.get("pnl", 0.0),
               "capital": role.capital, "factor_count": nf,
               "seconds": round(time.time() - t0, 1)}
        trajectory.append(row)
        print(f"  {d} 下单{row['placed']} 结算{row['settled']} "
              f"中{row['hit']} 挂{row['miss']} PnL{row['pnl']:+.0f} "
              f"资金{row['capital']:.0f} 因子{nf}", flush=True)
        if i % args.dedup_interval == 0:
            before = nf
            after = _dedup_factors(dog)
            print(f"  🧹 去重: {before} -> {after} 因子", flush=True)

    (run_root / "trajectory.json").write_text(
        json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8")
    role = dog._ensure_role()
    print(f"完成 {args.dog}: 期末资金 {role.capital:.0f} | "
          f"PnL {role.capital - args.capital:+.0f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
