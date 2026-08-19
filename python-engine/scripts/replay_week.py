#!/usr/bin/env python3
"""上周 7 狗虚拟回放：用修复后的因子统计重放历史分析，逐日对比真实下单。

对每个 (狗, 日)：
  1. 重建仿真角色 {狗}_simwk —— 只保留当日前的历史上下文：
     - 订单：created_at < 当日窗口起点（真实已结算订单）
     - 反思：reflection_memory.json 中 date < 当日
     - 资金：initial + 当日之前已结算订单利润
     - persona / alpha_mode / cross_factor_exclude 与真实角色一致
     - 不写 factor_memory.json（analyze 不读角色因子库，且避免污染跨狗注册表）
  2. 用当前（修复后）代码 analyze(当日, live=True, prefetched=True, jingcai_only=True)
  3. 对比真实当日订单：方向一致数 / 反向数 / 未匹配数，并按真实赛果折算仿真 PnL

用法:
  python scripts/replay_week.py --all                  # 2026-07-27 ~ 2026-08-04
  python scripts/replay_week.py --days 2026-08-02      # 指定日
  python scripts/replay_week.py --days 2026-08-02 --dogs 梭哈2狗,均注狗
  python scripts/replay_week.py --workers 4

进度写入 scripts/replay_week_progress.jsonl，中断后可重跑续跑（已完成的跳过）。
"""

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROLES_DIR = ROOT / "data" / "roles"
MATCHES_DIR = ROOT / "data" / "matches"
PROGRESS = ROOT / "scripts" / "replay_week_progress.jsonl"

DOGS = ["alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗"]
DAYS = ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30",
        "2026-07-31", "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]
WINDOW_START = " 12:00:00"


# ─────────────────────────────────────────────
# 真实基线：当日比赛 lota_id + 当日真实订单
# ─────────────────────────────────────────────

def day_lids(day: str) -> dict:
    """足球日 [D 12:00, D+1 12:00) 内所有比赛的 lota_id → 对阵"""
    d = datetime.fromisoformat(day)
    start = day + WINDOW_START
    end = (d + timedelta(days=1)).strftime("%Y-%m-%d") + WINDOW_START
    lids = {}
    for cd in [day, (d + timedelta(days=1)).strftime("%Y-%m-%d")]:
        f = MATCHES_DIR / f"{cd}.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        ms = data if isinstance(data, list) else data.get("matches", data.get("data", []))
        for m in ms:
            mt = m.get("match_time", "")
            if start <= mt < end and m.get("lota_id"):
                lids[m["lota_id"]] = f"{m.get('home_name','?')} vs {m.get('away_name','?')}"
    return lids


def real_role_orders(dog: str) -> list[dict]:
    f = ROLES_DIR / dog / f"{dog}.json"
    return json.loads(f.read_text(encoding="utf-8")).get("orders", [])


def real_day_orders(dog: str, day: str) -> list[dict]:
    lids = day_lids(day)
    return [o for o in real_role_orders(dog) if o.get("lota_id") in lids and not o.get("skip")]


def capital_before(role_data: dict, day: str) -> float:
    """当日开始时的资金 = initial + 当日之前已结算订单的利润"""
    init = role_data.get("initial_capital", 1000)
    start = day + WINDOW_START
    total = init
    for o in role_data.get("orders", []):
        if o.get("profit") is None:
            continue
        ts = o.get("settled_at") or o.get("created_at") or ""
        if ts and ts < start:
            total += o.get("profit", 0)
    return round(total, 2)


# ─────────────────────────────────────────────
# 仿真角色准备
# ─────────────────────────────────────────────

def prepare_sim_role(dog: str, day: str) -> None:
    sim = f"{dog}_simwk"
    sim_dir = ROLES_DIR / sim
    if sim_dir.exists():
        shutil.rmtree(sim_dir)
    (sim_dir / "memory").mkdir(parents=True)
    (sim_dir / "predicts").mkdir(parents=True)

    real_dir = ROLES_DIR / dog
    persona = real_dir / "persona.md"
    if persona.exists():
        shutil.copy2(persona, sim_dir / "persona.md")

    role_data = json.loads((real_dir / f"{dog}.json").read_text(encoding="utf-8"))
    start = day + WINDOW_START
    pre_orders = [
        o for o in role_data.get("orders", [])
        if (o.get("created_at") or "") < start
    ]
    sim_json = {
        "name": sim,
        "capital": capital_before(role_data, day),
        "initial_capital": role_data.get("initial_capital", 1000),
        "system_prompt_name": role_data.get("system_prompt_name", "baseline-v1"),
        "alpha_mode": role_data.get("alpha_mode", False),
        "cross_factor_exclude": role_data.get("cross_factor_exclude", []),
        "updated_at": datetime.now().isoformat(),
        "orders": pre_orders,
    }
    (sim_dir / f"{sim}.json").write_text(
        json.dumps(sim_json, ensure_ascii=False, indent=2), encoding="utf-8")

    # 反思：只保留当日之前的
    ref_path = real_dir / "memory" / "reflection_memory.json"
    if ref_path.exists():
        ref_data = json.loads(ref_path.read_text(encoding="utf-8"))
        refs = ref_data.get("reflections", []) if isinstance(ref_data, dict) else ref_data
        refs = [r for r in refs if (r.get("date") or "") < day]
        (sim_dir / "memory" / "reflection_memory.json").write_text(
            json.dumps({"updated_at": datetime.now().isoformat(), "reflections": refs[-20:]},
                       ensure_ascii=False), encoding="utf-8")

    # slug 记忆：保持原样（分析 prompt 的 slug 段为累计信息，修复未涉及）
    slug_path = real_dir / "memory" / "slug_memory.json"
    if slug_path.exists():
        shutil.copy2(slug_path, sim_dir / "memory" / "slug_memory.json")

    # 不复制 factor_memory.json：analyze 不加载角色因子库；写进去反而会污染跨狗注册表


# ─────────────────────────────────────────────
# 单个 (狗, 日) 回放任务（子进程）
# ─────────────────────────────────────────────

def replay_one(args: tuple) -> dict:
    dog, day = args
    t0 = time.time()
    try:
        prepare_sim_role(dog, day)
        from src.agent import Agent
        from src.providers.deepseek import DeepSeekProvider
        a = Agent(user=f"{dog}_simwk")
        a.set_provider(DeepSeekProvider())
        r = a.analyze(day, live=True, jingcai_only=True, prefetched=True)
        sim_orders = [o for o in r.get("orders", []) if not o.get("skip")]
        return {
            "dog": dog, "day": day, "ok": True,
            "matches_count": r.get("matches_count", 0),
            "prompt_tokens": r.get("prompt_tokens", 0),
            "orders": sim_orders,
            "session_path": r.get("session_path", ""),
            "seconds": round(time.time() - t0, 1),
        }
    except Exception as e:
        return {
            "dog": dog, "day": day, "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "seconds": round(time.time() - t0, 1),
        }


# ─────────────────────────────────────────────
# 对比与 PnL 折算
# ─────────────────────────────────────────────

OPPOSITE = {"H": "A", "A": "H", "over": "under", "under": "over"}


def evaluate(sim_orders: list[dict], real_orders: list[dict]) -> dict:
    """按真实赛果折算仿真 PnL；方向对比真实当日订单。"""
    real_by_key = {}
    real_by_lid = {}
    for o in real_orders:
        key = (o.get("lota_id"), o.get("bet_type"), o.get("pick"))
        stake = o.get("bet_size", 0) or 0
        if stake > 0:
            real_by_key[key] = (o.get("profit", 0)) / stake
        real_by_lid.setdefault(o.get("lota_id"), []).append(o)

    same, opposite, unmatched = 0, 0, []
    sim_pnl = 0.0
    for so in sim_orders:
        lid = so.get("lota_id")
        key = (lid, so.get("bet_type"), so.get("pick"))
        stake = so.get("bet_size", 0) or 0
        if key in real_by_key:
            same += 1
            sim_pnl += stake * real_by_key[key]
            continue
        # 同场 2 元盘反向（H↔A / over↔under）→ 用真实反方盈亏取反（半盘近似）
        opp = OPPOSITE.get(so.get("pick"))
        opp_key = (lid, so.get("bet_type"), opp)
        if opp_key in real_by_key and so.get("bet_type") in ("亚盘", "大小球"):
            opposite += 1
            sim_pnl += stake * (-real_by_key[opp_key])
            continue
        unmatched.append(f"{lid} {so.get('bet_type')} {so.get('pick')}")

    real_pnl = sum(o.get("profit", 0) for o in real_orders)
    return {
        "real_count": len(real_orders),
        "sim_count": len(sim_orders),
        "same": same, "opposite": opposite, "unmatched": unmatched,
        "real_pnl": round(real_pnl, 2),
        "sim_pnl": round(sim_pnl, 2),
    }


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", help="逗号分隔日期，或 --all")
    ap.add_argument("--all", action="store_true", help="跑全部 9 天")
    ap.add_argument("--dogs", default=",".join(DOGS))
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    days = DAYS if args.all else [d.strip() for d in args.days.split(",") if d.strip()]
    dogs = [d.strip() for d in args.dogs.split(",") if d.strip()]

    done = set()
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                done.add((rec.get("dog"), rec.get("day")))
            except Exception:
                pass

    tasks = [(dog, day) for day in days for dog in dogs if (dog, day) not in done]
    print(f"任务: {len(tasks)} 个 (狗×日) | 已完成跳过: {len(done)} | workers={args.workers}")
    if not tasks:
        print("全部已完成，无新任务。")
        return

    t_all = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(replay_one, t): t for t in tasks}
        for fut in as_completed(futs):
            rec = fut.result()
            tag = f"[{rec['dog']} {rec['day']}]"
            if rec.get("ok"):
                real = real_day_orders(rec["dog"], rec["day"])
                ev = evaluate(rec.get("orders", []), real)
                rec["eval"] = ev
                print(f"{tag} 场次{rec['matches_count']} 真实{ev['real_count']}单 仿真{ev['sim_count']}单 "
                      f"同向{ev['same']}/反向{ev['opposite']}/未匹配{len(ev['unmatched'])} "
                      f"PnL 真实{ev['real_pnl']:+.0f} 仿真{ev['sim_pnl']:+.0f} "
                      f"prompt {rec['prompt_tokens']}tok {rec['seconds']}s", flush=True)
            else:
                print(f"{tag} ❌ {rec.get('error')}", flush=True)
            with PROGRESS.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\n完成，总耗时 {time.time()-t_all:.0f}s。进度: {PROGRESS}")


if __name__ == "__main__":
    main()
