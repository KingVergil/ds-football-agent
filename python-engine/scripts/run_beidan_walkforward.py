"""
bc狗 walk-forward 串行回放（和 agent 狗一样的天循环）。

每天: analyze(LLM) → settle + factor reflect
每 N 天(默认7): 因子去重(dedup)
资金跨天滚动，模拟真实破产/回撤。

用法:
  python scripts/run_beidan_walkforward.py --dog bc狗 --start 2026-06-28 --end 2026-08-26
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
    ap.add_argument("--dog", default="bc狗")
    ap.add_argument("--start", default="2026-06-28")
    ap.add_argument("--end", default="2026-08-26")
    ap.add_argument("--capital", type=float, default=5000.0)
    ap.add_argument("--dedup-interval", type=int, default=7)
    ap.add_argument("--run-root", default=None)
    ap.add_argument("--no-alpha", action="store_true",
                    help="关闭跨狗 alpha（不注入其他单狗因子/订单倾向）")
    ap.add_argument("--stop-on-ruin", action="store_true",
                    help="结算后若剩余资金不足下一张票成本，立即停止")
    ap.add_argument("--offline", action="store_true",
                    help="纯离线：只读本地缓存（matches/beidan/beidan_sp），禁用一切联网拉取")
    ap.add_argument("--resume", action="store_true",
                    help="续跑：不重置资金/订单/因子，直接从现有 run-root 状态继续 start~end")
    args = ap.parse_args()

    run_root = Path(args.run_root or (TEST_ROOT / args.dog))
    os.environ["DS_ROLES_ROOT"] = str(run_root / "roles")
    os.environ["DS_SESSIONS_ROOT"] = str(run_root / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(run_root / "factors")
    from src.beidan_parlay_dog import BeidanParlayDog
    if args.offline:
        from src.data_manager import set_offline
        set_offline(True)
        print("  🔌 已开启离线模式：只读本地缓存，禁止联网刷新", flush=True)
    _copy_role_assets(args.dog, Path(os.environ["DS_ROLES_ROOT"]))

    dog = BeidanParlayDog(user=args.dog, capital=args.capital)
    if not args.resume:
        dog.reset(args.capital)
    else:
        print(f"  ▶️ 续跑：加载现有状态 start_capital={dog._ensure_role().capital:.0f}", flush=True)
    if args.no_alpha:
        role = dog._ensure_role()
        role.alpha_mode = False
        role.scope = "beidan"
        role.save()
        print(f"  🔒 已关闭 alpha (alpha_mode=False) | scope={role.scope}", flush=True)
    days = _date_range(args.start, args.end)

    trajectory_path = run_root / "trajectory.json"
    if args.resume and trajectory_path.exists():
        try:
            trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
            print(f"  ▶️ 续跑：已加载历史轨迹 {len(trajectory)} 天", flush=True)
        except Exception:
            trajectory = []
    else:
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
        if args.stop_on_ruin:
            # 破产停：剩余资金不足以再下最小一张票（486=8串1 243注×2）即停
            cfg = dog._load_parlay_config()
            combos = int(cfg.get("cover_picks", 3)) ** int(cfg.get("cover_legs", 5))
            min_cost = dog.UNIT_STAKE * combos
            if role.capital < min_cost:
                print(f"  🛑 资金 {role.capital:.0f} < 最小票成本 {min_cost:.0f}，破产停止", flush=True)
                break
        if i % args.dedup_interval == 0:
            before = nf
            after = _dedup_factors(dog)
            print(f"  🧹 去重: {before} -> {after} 因子", flush=True)

    trajectory_path.write_text(
        json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8")
    role = dog._ensure_role()
    print(f"完成 {args.dog}: 期末资金 {role.capital:.0f} | "
          f"PnL {role.capital - args.capital:+.0f} | "
          f"共跑 {len(trajectory)} 天/停在第 {i if 'i' in dir() else len(trajectory)} 天", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
