"""
北单串关因子测试：并行挖掘（7 月训练侧）→ 归并 → 8 月串行回放。

兼容旧入口（串行全量）：
  python scripts/run_beidan_factor_test.py 2026-07-01 2026-08-24

Phase A 并行挖掘：
  python scripts/run_beidan_factor_test.py mine --start 2026-07-01 --end 2026-07-31 --workers 6 --resume

Phase A' 归并：
  python scripts/run_beidan_factor_test.py reduce --mined-root data/beidan_factor_test/mined --out-root data/beidan_factor_test/library

Phase B 串行回放：
  python scripts/run_beidan_factor_test.py backtest --start 2026-08-01 --end 2026-08-24 \
      --factor-memory data/beidan_factor_test/library/factor_memory.json \
      --run-root data/beidan_factor_test/runs/factor --reflect false
  python scripts/run_beidan_factor_test.py backtest --start 2026-08-01 --end 2026-08-24 \
      --run-root data/beidan_factor_test/runs/baseline --baseline
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_ROOT = ROOT / "data" / "beidan_factor_test"
FORBIDDEN_PROMPT_TOKENS = ("result", "spvalue", "score", "draw_datetime", "result_des")


# ═══════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════

def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _copy_role_assets(dog_name: str, role_root: Path) -> None:
    """把狗的人设 + 策略配置(parlay.json) 复制到临时角色根。"""
    role_root.mkdir(parents=True, exist_ok=True)
    src_dir = ROOT / "data" / "roles" / dog_name
    for fn in ("persona.md", "parlay.json"):
        src = src_dir / fn
        if src.exists():
            (role_root / fn).write_text(src.read_text(encoding="utf-8"),
                                        encoding="utf-8")


def _date_range(start: str, end: str) -> list[str]:
    d = date.fromisoformat(start)
    e = date.fromisoformat(end)
    out = []
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ═══════════════════════════════════════════
# Phase A worker（子进程，独立根目录）
# ═══════════════════════════════════════════

def _mine_day(payload: tuple) -> dict:
    day, dog_name = payload
    wroot = TEST_ROOT / "workers" / dog_name / day
    mroot = TEST_ROOT / "mined" / dog_name / day
    if (mroot / "done.flag").exists():
        return {"day": day, "dog": dog_name, "status": "skipped"}

    # 每个 worker 独立根：必须写在 import dog 之前（role.py 在 import 时读 env）
    os.environ["DS_ROLES_ROOT"] = str(wroot / "roles")
    os.environ["DS_SESSIONS_ROOT"] = str(wroot / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(wroot / "factors")
    os.environ["REFLECT_SLUG_HISTORY"] = "0"
    os.environ["REFLECT_EXTRA_MATCHES"] = "0"

    from src.beidan_parlay_dog import BeidanParlayDog
    _copy_role_assets(dog_name, Path(os.environ["DS_ROLES_ROOT"]))

    t0 = time.time()
    try:
        dog = BeidanParlayDog(user=dog_name, capital=5000.0)
        dog.reset(5000.0)
        a = dog.analyze(day, use_llm=True)
        s = dog.settle(day, reflect=True)
        role = dog._ensure_role()
        role.memory.factors.load()
        role.memory.reflections.load()

        summary = {
            "day": day,
            "dog": dog_name,
            "placed": a.get("placed", 0),
            "settled": s.get("settled", 0),
            "hit": s.get("hit", 0),
            "miss": s.get("miss", 0),
            "push": s.get("push", 0),
            "pnl": s.get("pnl", 0.0),
            "capital": role.capital,
            "factor_count": len(role.memory.factors.factor_perf or {}),
            "llm_used": a.get("llm_used", False),
            "seconds": round(time.time() - t0, 1),
        }
        _atomic_write_json(mroot / "summary.json", summary)
        _atomic_write_json(mroot / "factor_memory.json",
                           {"factor_perf": role.memory.factors.factor_perf})
        _atomic_write_json(mroot / "reflection_memory.json",
                           {"reflections": role.memory.reflections.reflections})
        _atomic_write_json(mroot / "orders.json", role.orders)
        (mroot / "done.flag").write_text("ok", encoding="utf-8")
        return {"day": day, "dog": dog_name, "status": "ok", **summary}
    except Exception as e:
        _atomic_write_json(mroot / "error.json",
                           {"error": str(e), "trace": traceback.format_exc()})
        return {"day": day, "dog": dog_name, "status": "error", "error": str(e)}


def _fmt_result(r: dict) -> str:
    if r.get("status") == "skipped":
        return f"  ⏭ {r.get('dog','')}/{r['day']} skipped"
    if r.get("status") == "error":
        return f"  ❌ {r.get('dog','')}/{r['day']} error: {r.get('error')}"
    return (f"  ✅ {r.get('dog','')}/{r['day']} 下单{r.get('placed',0)} "
            f"结算{r.get('settled',0)} "
            f"PnL{r.get('pnl',0):+.0f} 因子{r.get('factor_count',0)} "
            f"{r.get('seconds',0)}s")


def run_mine(args) -> int:
    days = _date_range(args.start, args.end)
    dogs = [d.strip() for d in args.dogs.split(",") if d.strip()] or ["bc狗"]
    tasks = [(d, dog) for d in days for dog in dogs]
    if args.resume:
        tasks = [(d, dog) for d, dog in tasks
                 if not (TEST_ROOT / "mined" / dog / d / "done.flag").exists()]
    print(f"Phase A mine: {len(days)} days × {len(dogs)} dogs | workers={args.workers}")
    results: list[dict] = []
    if args.workers <= 1:
        for t in tasks:
            r = _mine_day(t)
            results.append(r)
            print(_fmt_result(r))
    else:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=args.workers,
                                 max_tasks_per_child=1) as ex:
            for r in ex.map(_mine_day, tasks):
                results.append(r)
                print(_fmt_result(r), flush=True)
    ok = sum(1 for r in results if r.get("status") == "ok")
    err = sum(1 for r in results if r.get("status") == "error")
    print(f"\nPhase A 完成: ok={ok} error={err} total={len(results)}")
    return 0 if err == 0 else 1


# ═══════════════════════════════════════════
# Phase A' reduce（朴素归并；完整 factor_induction 后续接）
# ═══════════════════════════════════════════

def run_reduce(args) -> int:
    mined_root = Path(args.mined_root)
    out_root = Path(args.out_root)
    merged: dict[str, dict] = {}
    seen_days = []
    for p in sorted(mined_root.glob("*/factor_memory.json")):
        day = p.parent.name
        data = json.loads(p.read_text(encoding="utf-8"))
        fp = data.get("factor_perf", {})
        for name, s in fp.items():
            if name not in merged:
                merged[name] = dict(s)
                merged[name]["history"] = list(s.get("history", []))
            else:
                merged[name]["history"].extend(s.get("history", []))
                merged[name]["history"] = sorted(
                    merged[name]["history"], key=lambda h: h.get("date", "")
                )
                # 统计重算（保守：样本量相加，命中/盈亏相加）
                for k in ("total", "hit", "miss", "push"):
                    merged[name][k] = merged[name].get(k, 0) + s.get(k, 0)
                merged[name]["profit"] = merged[name].get("profit", 0.0) + s.get("profit", 0.0)
                merged[name]["last_seen"] = max(
                    merged[name].get("last_seen", ""), s.get("last_seen", "")
                )
                merged[name]["first_seen"] = min(
                    merged[name].get("first_seen", "9999"), s.get("first_seen", "9999")
                )
        seen_days.append(day)
    out_root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(out_root / "factor_memory.json",
                       {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "factor_perf": merged})
    (out_root / "summary.json").write_text(json.dumps(
        {"days": len(seen_days), "factor_count": len(merged),
         "note": "朴素归并，未走 factor_induction 判重"}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"reduce: {len(seen_days)} 天 → {len(merged)} 个候选因子 → {out_root}")
    return 0


# ═══════════════════════════════════════════
# Phase B 串行回放（因子组 / 对照组）
# ═══════════════════════════════════════════

def run_backtest(args) -> int:
    run_root = Path(args.run_root or (TEST_ROOT / "runs" / (
        "baseline" if args.baseline else "factor")))
    days = _date_range(args.start, args.end)
    dog_name = getattr(args, "dog", None) or "bc狗"
    reflect = str(args.reflect).strip().lower() in ("1", "true", "yes")

    os.environ["DS_ROLES_ROOT"] = str(run_root / "roles")
    os.environ["DS_SESSIONS_ROOT"] = str(run_root / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(run_root / "factors")
    from src.beidan_parlay_dog import BeidanParlayDog
    _copy_role_assets(dog_name, Path(os.environ["DS_ROLES_ROOT"]))
    dog = BeidanParlayDog(user=dog_name, capital=5000.0)
    dog.reset(5000.0)
    role = dog._ensure_role()
    role.alpha_mode = True
    role.cross_factor_exclude = []
    role.save()
    if not args.baseline and args.factor_memory and Path(args.factor_memory).exists():
        role = dog._ensure_role()
        role.memory.factors.path.parent.mkdir(parents=True, exist_ok=True)
        role.memory.factors.path.write_text(
            Path(args.factor_memory).read_text(encoding="utf-8"), encoding="utf-8"
        )

    rows = []
    print(f"Phase B backtest: {args.start}~{args.end} baseline={args.baseline} "
          f"factor={args.factor_memory} reflect={reflect}")
    for d in days:
        a = dog.analyze(d, use_llm=True)
        s = dog.settle(d, reflect=reflect)
        role = dog._ensure_role()
        r = {"day": d, "placed": a.get("placed", 0), "settled": s.get("settled", 0),
             "hit": s.get("hit", 0), "miss": s.get("miss", 0), "push": s.get("push", 0),
             "pnl": s.get("pnl", 0.0), "capital": role.capital}
        rows.append(r)
        print(f"  {r['day']} 下单{r['placed']} 结算{r['settled']} "
              f"中{r['hit']} 挂{r['miss']} PnL{r['pnl']:+.0f} 资金{r['capital']:.0f}")
    _atomic_write_json(run_root / "trajectory.json", rows)
    total_pnl = round(sum(r["pnl"] for r in rows), 2)
    print(f"Phase B 汇总: 期末资金 {rows[-1]['capital']:.0f} | PnL {total_pnl:+.0f}")
    return 0


# ═══════════════════════════════════════════
# 兼容旧入口：串行全量（analyze→settle→reflect）
# ═══════════════════════════════════════════

def run_serial(start: str, end: str) -> int:
    from src.beidan_parlay_dog import BeidanParlayDog
    dog = BeidanParlayDog(user="bc狗_因子测试", capital=5000.0)
    dog.reset(5000.0)
    print(f"{'日期':<12} {'下单':>4} {'结算':>4} {'中':>3} {'挂':>3} {'PnL':>9} {'资金':>9} {'因子':>4}")
    for d in _date_range(start, end):
        a = dog.analyze(d, use_llm=True)
        s = dog.settle(d, reflect=True)
        role = dog._ensure_role()
        try:
            role.memory.factors.load()
            nf = len(role.memory.factors.factor_perf or {})
        except Exception:
            nf = 0
        print(f"{d:<12} {a.get('placed',0):>4} {s.get('settled',0):>4} "
              f"{s.get('hit',0):>3} {s.get('miss',0):>3} {s.get('pnl',0):>+9.0f} "
              f"{role.capital:>9.0f} {nf:>4}")
    return 0


# ═══════════════════════════════════════════
# P1b 单狗 K 日 batch（frozen-state 近似）
# ═══════════════════════════════════════════

def _merge_factor_perf(acc: dict[str, dict], new: dict[str, dict]) -> None:
    for name, s in (new or {}).items():
        if name not in acc:
            acc[name] = dict(s)
            acc[name]["history"] = list(s.get("history", []))
            continue
        t = acc[name]
        t["history"] = sorted(
            t.get("history", []) + s.get("history", []),
            key=lambda h: h.get("date", ""),
        )
        for k in ("total", "hit", "miss", "push"):
            t[k] = t.get(k, 0) + s.get(k, 0)
        t["profit"] = t.get("profit", 0.0) + s.get("profit", 0.0)
        t["last_seen"] = max(t.get("last_seen", ""), s.get("last_seen", ""))
        t["first_seen"] = min(t.get("first_seen", "9999"), s.get("first_seen", "9999"))


def _batch_worker(payload: tuple) -> dict:
    day, factor_memory, phase, orders, capital, dog_name = payload
    wroot = TEST_ROOT / "batch_workers" / dog_name / phase / day
    os.environ["DS_ROLES_ROOT"] = str(wroot / "roles")
    os.environ["DS_SESSIONS_ROOT"] = str(wroot / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(wroot / "factors")
    os.environ["REFLECT_SLUG_HISTORY"] = "0"
    os.environ["REFLECT_EXTRA_MATCHES"] = "0"
    from src.beidan_parlay_dog import BeidanParlayDog
    _copy_role_assets(dog_name, Path(os.environ["DS_ROLES_ROOT"]))

    dog = BeidanParlayDog(user=dog_name, capital=float(capital))
    dog.reset(float(capital))
    role = dog._ensure_role()
    if factor_memory:
        role.memory.factors.path.parent.mkdir(parents=True, exist_ok=True)
        role.memory.factors.path.write_text(
            json.dumps({"factor_perf": factor_memory}), encoding="utf-8"
        )
        role.memory.factors.load()

    if phase == "analyze":
        a = dog.analyze(day, use_llm=True, dry_run=True)
        return {"day": day, "orders": a.get("orders", []),
                "llm_used": a.get("llm_used", False)}

    # settle + reflect
    for o in orders:
        dog._ensure_role().place_order(o)
    s = dog.settle(day, reflect=True)
    role = dog._ensure_role()
    role.memory.factors.load()
    return {"day": day, "pnl": s.get("pnl", 0.0), "settled": s.get("settled", 0),
            "hit": s.get("hit", 0), "miss": s.get("miss", 0),
            "factor_perf": role.memory.factors.factor_perf}


def run_batch(args) -> int:
    from concurrent.futures import ProcessPoolExecutor
    days = _date_range(args.start, args.end)
    K = int(args.batch)
    dog_name = getattr(args, "dog", None) or "bc狗"
    capital = 5000.0
    factor_memory: dict[str, dict] = {}
    print(f"P1b batch: {len(days)} days | batch={K} | workers={args.workers}")
    print(f"{'chunk':<7} {'day':<12} {'下单':>4} {'结算':>4} {'中':>3} {'挂':>3} "
          f"{'PnL':>9} {'资金':>9} {'因子':>4}")

    for i in range(0, len(days), K):
        chunk = days[i:i + K]
        chunk_capital = capital
        # ① analyze（并行，全部冻结在 chunk 起点 factor_memory）
        with ProcessPoolExecutor(max_workers=args.workers,
                                 max_tasks_per_child=1) as ex:
            analyze_res = list(ex.map(
                _batch_worker,
                [(d, factor_memory, "analyze", None, chunk_capital, dog_name)
                 for d in chunk],
            ))
        by_day = {r["day"]: r for r in analyze_res}

        # ② apply（串行，按天占用资金额度判断，不动 running capital）
        placed: dict[str, list] = {}
        available = chunk_capital
        for d in chunk:
            orders = by_day[d]["orders"]
            total = round(sum(float(o.get("bet_size", 0)) for o in orders), 2)
            if total <= available:
                placed[d] = orders
                available -= total
            else:
                placed[d] = []

        # ③ settle+reflect（并行）
        active = [d for d in chunk if placed.get(d)]
        with ProcessPoolExecutor(max_workers=args.workers,
                                 max_tasks_per_child=1) as ex:
            settle_res = list(ex.map(
                _batch_worker,
                [(d, factor_memory, "settle", placed[d], chunk_capital, dog_name)
                 for d in active],
            ))

        # ④ barrier reduce（串行，按日期 apply 因子 + 资金）
        for r in settle_res:
            capital += r["pnl"]
            _merge_factor_perf(factor_memory, r.get("factor_perf") or {})
            print(f"{i // K + 1:<7} {r['day']:<12} {r.get('settled',0):>4} "
                  f"{r.get('hit',0):>3} {r.get('miss',0):>3} {r['pnl']:>+9.0f} "
                  f"{capital:>9.0f} {len(factor_memory):>4}")

    _atomic_write_json(TEST_ROOT / "batch_result.json",
                       {"days": days, "capital": capital,
                        "factor_memory": factor_memory})
    print(f"\nP1b 完成: 期末资金 {capital:.0f} | 因子 {len(factor_memory)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="run_beidan_factor_test")
    p.add_argument("start", nargs="?", default=None)
    p.add_argument("end", nargs="?", default=None)
    sub = p.add_subparsers(dest="cmd")

    m = sub.add_parser("mine")
    m.add_argument("--start", default="2026-07-01")
    m.add_argument("--end", default="2026-07-31")
    m.add_argument("--workers", type=int, default=6)
    m.add_argument("--resume", action="store_true")
    m.add_argument("--dogs", default="bc狗",
                   help="逗号分隔的狗名（并行跑多只狗）")

    r = sub.add_parser("reduce")
    r.add_argument("--mined-root", default=str(TEST_ROOT / "mined"))
    r.add_argument("--out-root", default=str(TEST_ROOT / "library"))

    b = sub.add_parser("backtest")
    b.add_argument("--start", default="2026-08-01")
    b.add_argument("--end", default="2026-08-24")
    b.add_argument("--factor-memory", default=None)
    b.add_argument("--factors-root", default=None)
    b.add_argument("--run-root", default=None)
    b.add_argument("--baseline", action="store_true")
    b.add_argument("--reflect", default="false")
    b.add_argument("--dog", default="bc狗")

    k = sub.add_parser("batch")
    k.add_argument("--start", default="2026-07-01")
    k.add_argument("--end", default="2026-07-12")
    k.add_argument("--batch", type=int, default=4)
    k.add_argument("--workers", type=int, default=4)
    k.add_argument("--dog", default="bc狗")

    args = p.parse_args()
    if args.cmd == "mine":
        return run_mine(args)
    if args.cmd == "reduce":
        return run_reduce(args)
    if args.cmd == "backtest":
        return run_backtest(args)
    if args.cmd == "batch":
        return run_batch(args)
    start = args.start or "2026-07-01"
    end = args.end or "2026-08-24"
    return run_serial(start, end)


if __name__ == "__main__":
    sys.exit(main())
