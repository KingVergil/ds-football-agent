#!/usr/bin/env python3
"""组合层离线仿真（方案 C 的原型）：同一腿池上扫不同「票型规划」，看哪种组合构造更好。

为什么能离线
───────────
组合层（选票型 / 容错 / 是否多选）在方案 C 里不进 LLM，是腿池的**纯函数**：
    plan(腿池 with x、北单赔率、实际结果、开奖SP、资金) → 票构造
所以历史回放已经把腿池和结果都落盘了，可以直接**重放**，不需要再花真调用。

腿池来源（二选一）
──────────────
1. `--from-run <沙箱>`：从回放沙箱的订单里取**已下注腿**（sanity 基线用）。
2. `--from-prompts <dir>`：从 `dump_prompts` 落盘的 stage1 prompt 里解析**每波全部候选场**
   （含 x、赔率），再按 θ 过门重建腿池——这才是仿真真正要的输入。

计划（可扫）
──────────
* `top_n`        : 按 stage1 顺序取前 N 条腿（现基线）
* `x_cap`        : 丢掉平均 x > cap 的腿（极端尾部）
* `min_x`        : 只保留平均 x ≥ 该值的腿
* `pick_mode`    : single(每腿只买 x 最大侧) / gate(所有过门侧=允许多选)
* `ticket`       : N串1 / N过(N−1) / N过(N−2)
* `bankroll`     : 初始资金，按每注 2 元扣成本、命中按 0.65×ΠSP 派彩

用法
───
    python3 -m scripts.sim_ticket_plans --from-prompts /tmp/seq_dump --days 2026-07-16 2026-07-17
    python3 -m scripts.sim_ticket_plans --from-run /tmp/beidan_run0815
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from itertools import combinations
from math import comb
from pathlib import Path

SIDES = ("H", "D", "A")
UNIT = 2.0            # 北单每注 2 元
TAKEOUT = 1 / 0.65    # 打平所需 Πx（单次乘）


# ── 腿池解析 ──────────────────────────────────────────
_LINE = re.compile(
    r"Lota(\d+) \| [^\n]*?goal_line=(-?[\d.]+)[^\n]*?赔率H/D/A=([\d./]+)[^\n]*?错价倍数x\(H/D/A\)=([\d./]+)"
)
_RESULT = re.compile(r"Lota(\d+)")


def pool_from_prompts(dump_dir: str, theta: float = 1.1) -> dict[str, list[dict]]:
    """从 dump_prompts 的 stage1 prompt 里解析每场三侧 (赔率, x)，按 θ 过门成腿池。

    返回 {day: [leg,...]}；leg = {lota_id, x, mean_x, picks, odds(被选侧北单赔率)}
    ⚠️ 这里拿不到"实际结果/开奖SP"（prompt 里被剥离，防后视）——需另配 settle 数据源。
    """
    pools: dict[str, list[dict]] = {}
    for f in sorted(glob.glob(os.path.join(dump_dir, "*.md"))):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
        day = m.group(1) if m else "?"
        txt = Path(f).read_text(encoding="utf-8", errors="ignore")
        legs = []
        for hit in _LINE.finditer(txt):
            lid = "Lota" + hit.group(1)
            gl = float(hit.group(2))
            odds = [float(v) for v in hit.group(3).split("/") if v]
            xs = [float(v) for v in hit.group(4).split("/") if v]
            if len(odds) != 3 or len(xs) != 3:
                continue
            picks = [s for s, x in zip(SIDES, xs) if x >= theta]
            if not picks:
                continue
            legs.append({
                "lota_id": lid, "goal_line": gl,
                "x": {s: xs[i] for i, s in enumerate(SIDES)},
                "odds": {s: odds[i] for i, s in enumerate(SIDES)},
                "mean_x": sum(xs[SIDES.index(s)] for s in picks) / len(picks),
                "picks": picks,
            })
        if legs:
            pools.setdefault(day, []).extend(legs)
    return pools


def pool_from_run(ws: str) -> dict[str, list[dict]]:
    """从回放沙箱订单取已下注腿（含实际结果/SP），作为可结算的基线腿池。"""
    role = json.loads((Path(ws) / "bcl狗.json").read_text(encoding="utf-8"))
    pools: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()   # (day, lota_id) 去重：同日多票会重复出现同一场
    for o in role.get("orders") or []:
        tls = o.get("ticket_legs") or []
        if not tls:
            continue
        day = min([str(l.get("match_time"))[:10] for l in tls if l.get("match_time")] or ["?"])
        for l in tls:
            picks = l.get("picks") or ([l["pick"]] if l.get("pick") else [])
            key = (day, str(l.get("lota_id")))
            if key in seen:
                continue
            seen.add(key)
            pools.setdefault(day, []).append({
                "lota_id": l.get("lota_id"),
                "mean_x": float(l.get("x_mkt") or 1.0),
                "x": {s: float(l.get("x_mkt") or 1.0) for s in picks},
                "picks": picks,
                "actual": l.get("actual"),
                "sp": float(l.get("sp") or 0.0),
                "hit": bool(l.get("hit")),
                "settled": True,
            })
    return pools


# ── 计划（组合层纯函数）────────────────────────────
def plan_ticket(pool: list[dict], *, top_n=9, min_x=1.0, x_cap=0.0,
                pick_mode="gate", ticket="串1", tolerance=3) -> list[dict]:
    """腿池 → 一张票的构造（不改腿池顺序，只在数量/票型上做文章）。"""
    legs = [l for l in pool if float(l.get("mean_x") or 0) >= min_x]
    if x_cap > 0:
        legs = [l for l in legs if float(l.get("mean_x") or 0) <= x_cap]
    legs = legs[:top_n]
    if len(legs) < 2:
        return []
    n = len(legs)
    if ticket == "串1":
        m = n
    else:  # N过M：m = n - tolerance
        m = max(2, n - tolerance)
    ways = comb(n, n - m)          # 注数
    return [{"legs": legs, "n": n, "m": m, "ways": ways,
             "cost": ways * UNIT, "pick_mode": pick_mode}]


def settle_plan(slip: dict) -> tuple[float, int]:
    """按腿的真实结果结算（仅当腿带 settled 字段）；返回 (派彩, 中奖注数)。"""
    legs = slip["legs"]
    if any(not l.get("settled") for l in legs):
        return 0.0, 0
    # 每条腿买 picks 里实际命中的那个方向才算这条腿中
    hit_flags = []
    for l in legs:
        hit_flags.append(bool(l.get("hit")))
    payout = 0.0
    win_bets = 0
    n, m = slip["n"], slip["m"]
    for combo in combinations(range(n), m):
        if all(hit_flags[i] for i in combo):
            o = 1.0
            for i in combo:
                o *= float(legs[i].get("sp") or 0.0)
            payout += 0.65 * o * UNIT
            win_bets += 1
    return payout, win_bets


def pool_from_leg_pool(pool_dir: str, theta: float = 1.1, pick_mode: str = "gate") -> dict[str, list[dict]]:
    """从 rebuild_leg_pool 的产物加载，按 (日, 波次, 场) 聚合成腿。

    pick_mode: "gate" = 所有 x≥θ 的侧都买（允许多选）；"single" = 只买 x 最大的侧。
    """
    pools: dict[str, list[dict]] = {}
    for f in sorted(glob.glob(os.path.join(pool_dir, "*.json"))):
        day = os.path.basename(f)[:-5]
        rows = json.loads(Path(f).read_text(encoding="utf-8"))
        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            if float(r.get("x") or 0) < theta:
                continue
            # ⚠️ (波次, 场) 可能重复出现（同一场在同一波被取到多次）→ 同一侧只保留一份，
            #    否则"同场买两次"会造出不可能的串关组合（2026-09-12 实测：96% 派彩来自这种假组合）
            key = (r.get("wave"), r.get("lota_id"))
            bucket = groups.setdefault(key, [])
            if any(b.get("side") == r.get("side") for b in bucket):
                continue
            bucket.append(r)
        # 跨波去重：同一场一天只允许出现一次（否则跨波会组成"同场两腿"的假关）
        best_by_lid: dict[str, list[dict]] = {}
        for (wave, lid), rs in sorted(groups.items()):
            cur = best_by_lid.get(lid)
            if cur is None or max(float(r["x"]) for r in rs) > max(float(r["x"]) for r in cur):
                best_by_lid[lid] = rs
        day_legs = []
        for lid, rs in best_by_lid.items():
            rs.sort(key=lambda z: -float(z["x"]))
            picks = [r["side"] for r in rs] if pick_mode == "gate" else [rs[0]["side"]]
            chosen = [r for r in rs if r["side"] in picks]
            day_legs.append({
                "lota_id": lid,
                "mean_x": sum(float(r["x"]) for r in chosen) / len(chosen),
                "x": {r["side"]: float(r["x"]) for r in rs},
                "picks": picks,
                "odds": {r["side"]: float(r.get("beidan_odds") or 0) for r in rs},
                "actual": rs[0].get("actual") or "",
                "sp": float(rs[0].get("sp") or 0.0),
                "settled": bool(rs[0].get("settled")),
                "hit": any(r["side"] == rs[0].get("actual") for r in chosen) if rs[0].get("actual") else False,
            })
        # 稳定顺序：按本波内原出现序（与 stage1 顺序近似）——按第一次出现排
        seen = {}
        for i, r in enumerate(rows):
            seen.setdefault(r["lota_id"], i)
        day_legs.sort(key=lambda l: seen.get(l["lota_id"], 1 << 30))
        if day_legs:
            pools.setdefault(day, []).extend(day_legs)
    return pools


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-run", default="", help="回放沙箱（含结果与SP，可结算）")
    ap.add_argument("--from-prompts", default="", help="dump_prompts 目录（仅候选，不可结算）")
    ap.add_argument("--from-leg-pool", default="", help="rebuild_leg_pool 产物目录（可结算）")
    ap.add_argument("--pick-mode", default="gate", choices=("gate", "single"))
    ap.add_argument("--days", nargs="*", default=[], help="只看这些日（默认全部）")
    ap.add_argument("--theta", type=float, default=1.1)
    ap.add_argument("--out", default="", help="结果 json 落盘")
    args = ap.parse_args(argv)

    if args.from_leg_pool:
        pools = pool_from_leg_pool(args.from_leg_pool, args.theta, args.pick_mode)
        settle_ok = True
    elif args.from_run:
        pools = pool_from_run(args.from_run)
        settle_ok = True
    elif args.from_prompts:
        pools = pool_from_prompts(args.from_prompts, args.theta)
        settle_ok = False
    else:
        print("需要 --from-run 或 --from-prompts")
        return 1
    if args.days:
        pools = {d: v for d, v in pools.items() if d in set(args.days)}

    print(f"腿池：{len(pools)} 天｜共 {sum(len(v) for v in pools.values())} 腿"
          f"｜可结算={settle_ok}")

    plans = [
        ("基线 top9 串1 (现行)",      dict(top_n=9,  ticket="串1")),
        ("top7 串1",                 dict(top_n=7,  ticket="串1")),
        ("top5 串1",                 dict(top_n=5,  ticket="串1")),
        ("top9 过1 (N过N-1)",        dict(top_n=9,  ticket="过", tolerance=1)),
        ("top9 过2",                 dict(top_n=9,  ticket="过", tolerance=2)),
        ("top9 过4",                 dict(top_n=9,  ticket="过", tolerance=4)),
        ("top9 串1 + x≤1.20",        dict(top_n=9,  ticket="串1", x_cap=1.20)),
        ("top9 串1 + x≤1.15",        dict(top_n=9,  ticket="串1", x_cap=1.15)),
        ("top9 串1 + min_x≥1.20",    dict(top_n=9,  ticket="串1", min_x=1.20)),
        ("top9 过3",                 dict(top_n=9,  ticket="过", tolerance=3)),
        ("top9 串1 + x≤1.18",        dict(top_n=9,  ticket="串1", x_cap=1.18)),
        ("top9 串1 + x≤1.25",        dict(top_n=9,  ticket="串1", x_cap=1.25)),
        ("top9 串1 + x≤1.30",        dict(top_n=9,  ticket="串1", x_cap=1.30)),
        ("top12 串1 + x≤1.20",       dict(top_n=12, ticket="串1", x_cap=1.20)),
        ("top9 过4 + x≤1.20",        dict(top_n=9,  ticket="过", tolerance=4, x_cap=1.20)),
        ("top9 串1 只买最高x侧",      dict(top_n=9,  ticket="串1", pick_mode="single")),
    ]

    rows = []
    for label, kw in plans:
        stake = payout = 0.0
        legs_used = wins = 0
        days_played = 0
        for day, pool in sorted(pools.items()):
            slips = plan_ticket(pool, **kw)
            if not slips:
                continue
            days_played += 1
            for sl in slips:
                stake += sl["cost"]
                legs_used += len(sl["legs"])
                if settle_ok:
                    pay, wb = settle_plan(sl)
                    payout += pay
                    wins += wb
        rows.append({"plan": label, "days": days_played, "legs": legs_used,
                     "stake": stake, "payout": payout, "pnl": payout - stake,
                     "roi": (payout - stake) / stake if stake else 0.0, "win_bets": wins})

    print(f"\n{'计划':<24}{'天':>4}{'腿':>5}{'投注':>9}{'派彩':>9}{'盈亏':>9}{'ROI':>9}{'中奖注':>7}")
    for r in rows:
        print(f"{r['plan']:<24}{r['days']:>4}{r['legs']:>5}{r['stake']:>9.0f}"
              f"{r['payout']:>9.0f}{r['pnl']:>+9.0f}{r['roi']:>+9.1%}{r['win_bets']:>7}")
    if not settle_ok:
        print("\n⚠️ 仅候选池（prompt 来源）不带结果/SP → 只能看成本与腿数，不能算盈亏。")
    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已落盘: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
