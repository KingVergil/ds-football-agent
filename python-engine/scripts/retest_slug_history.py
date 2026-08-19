#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案A: reflect 因子层对照 — 验证「历史同信号比赛回顾」是否提升因子生成质量。

对照组(ctl): 现 reflect 逻辑（只思考当天结算订单）
实验组(exp): reflect + 历史同信号比赛回顾（use_slug_history=True）

两组从同一 07-12 备份 factor_memory 出发，在窗口 [start,end] 内按天独立演化：
  每天用真实结算订单跑一次 reflect（归因/新建各自 memory）。
最后复用 analyze_evolution 的指标（新因子日前后 7 天平均 Δ 对齐 + 因子质量统计）
对比两组。

用法:
  python3 scripts/retest_slug_history.py --dry-run      # 只生成 prompt，不调 LLM
  python3 scripts/retest_slug_history.py                # 全窗口跑（需 DEEPSEEK_API_KEY）
  python3 scripts/retest_slug_history.py --days 2026-07-13,2026-07-14
  python3 scripts/retest_slug_history.py --resume       # 跳过已完成 (day, regime)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import run_reflect                        # noqa: E402
from src.memory import FactorMemory, ReflectionMemory    # noqa: E402
from src.prompt_builder import count_tokens              # noqa: E402
from src.providers.deepseek import DeepSeekProvider      # noqa: E402


ROLES_DIR = PROJECT_ROOT / "data" / "roles"
BACKUP_DIR = PROJECT_ROOT / "data" / "roles.backup.20260712_010652"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
TMP_ROOT = Path("/private/tmp/slughist_retest")

CTL, EXP = "ctl", "exp"


class _DryProvider:
    """dry-run 用：不调 LLM，返回空串（run_reflect 会安全走无结果路径）。"""

    def call(self, *a, **k):
        return ""


class _RoleShim:
    """run_reflect 所需的最小 role 接口。"""

    def __init__(self, name: str, base_dir: Path, persona_text: str):
        self.name = name
        self.alpha_mode = False
        self.cross_factor_exclude: list[str] = []
        self.system_prompt_name = "baseline-v1"
        self._persona = persona_text
        self.memory = type(
            "Mem",
            (),
            {
                "factors": FactorMemory(base_dir),
                "reflections": ReflectionMemory(base_dir),
            },
        )()

    def persona_text(self) -> str:
        return self._persona


def load_backup_factor_memory(agent: str) -> dict:
    p = BACKUP_DIR / agent / "memory" / "factor_memory.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"factor_perf": {}}


def ensure_memory_dir(agent: str, regime: str, run_id: str = "1") -> Path:
    """首次从备份种子 memory；已有演化结果则保留（供 --resume）。"""
    base = TMP_ROOT / agent / f"run{run_id}" / regime / "memory"
    fm = base / "factor_memory.json"
    if not fm.exists():
        base.mkdir(parents=True, exist_ok=True)
        fm.write_text(
            json.dumps(load_backup_factor_memory(agent), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return base


def daily_settled_orders(agent: str, start: str, end: str) -> dict[str, list[dict]]:
    """真实角色订单按 settled_at 分组，只留 [start,end] 窗口内。"""
    role = json.loads(
        (ROLES_DIR / agent / f"{agent}.json").read_text(encoding="utf-8")
    )
    by_day: dict[str, list[dict]] = {}
    for o in role.get("orders") or []:
        d = (o.get("settled_at") or "")[:10]
        if not d or not (start <= d <= end):
            continue
        if not o.get("lota_id"):
            continue
        by_day.setdefault(d, []).append(o)
    return dict(sorted(by_day.items()))


def load_progress(out_dir: Path) -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    pf = out_dir / "progress.jsonl"
    if pf.exists():
        for line in pf.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                done.add((d["day"], d["regime"]))
            except Exception:
                continue
    return done


def append_progress(out_dir: Path, day: str, regime: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"day": day, "regime": regime,
                            "ts": datetime.now().isoformat()}, ensure_ascii=False) + "\n")


def build_role(agent: str, base_dir: Path) -> _RoleShim:
    persona = ""
    pp = ROLES_DIR / agent / "persona.md"
    if pp.exists():
        persona = pp.read_text(encoding="utf-8").strip()
    return _RoleShim(f"{agent}_{base_dir.name}", base_dir, persona)


def factor_stats(mem: dict) -> dict:
    fp = mem.get("factor_perf", {})
    total = len(fp)
    samples = sum(len(v.get("history", []) or []) for v in fp.values())
    hit = sum(v.get("hit", 0) for v in fp.values())
    push = sum(v.get("push", 0) for v in fp.values())
    denom = max(1, samples - push)
    profit = sum(v.get("profit", 0) for v in fp.values())
    tret = sum(v.get("total_return", 0) for v in fp.values())
    positive = sum(1 for v in fp.values()
                   if v.get("total", 0) > 0 and v.get("profit", 0) > 0)
    status = Counter(v.get("status", "active") for v in fp.values())
    return {
        "total": total, "samples": samples,
        "hit_rate": round(hit / denom, 3),
        "profit": round(profit, 2), "total_return": round(tret, 3),
        "positive": positive,
        "status": dict(status),
    }


def new_factor_events(mem: dict, start: str, end: str) -> list[dict]:
    fp = mem.get("factor_perf", {})
    by_date: Counter = Counter()
    for v in fp.values():
        fs = (v.get("first_seen") or "")[:10]
        if fs and start <= fs <= end:
            by_date[fs] += 1
    return [{"date": d, "new_count": c} for d, c in sorted(by_date.items())]


def build_metrics(agent: str, start: str, end: str,
                  live_start: str, w_mode: str, memories: dict[str, dict]) -> dict:
    from eval_log_score import evaluate
    from analyze_evolution import aggregate, align_events, daily_series

    res = evaluate(agent, 7, "exact-kelly", "direction", w_mode,
                   live_start, live_only=True)
    daily = daily_series(res["orders"])
    role_orders = json.loads(
        (ROLES_DIR / agent / f"{agent}.json").read_text(encoding="utf-8")
    ).get("orders", [])
    out = {"daily_days": len(daily), "daily_total": round(
        sum(x["sum_delta"] for x in daily), 3), "regimes": {}}
    for regime, mem in memories.items():
        events = new_factor_events(mem, start, end)
        aligned = align_events(daily, events, 7)
        agg = aggregate(aligned)
        roi = attributed_window_roi(mem, role_orders, start, end)
        out["regimes"][regime] = {
            "new_factor_days": len(events),
            "new_factor_count": sum(e["new_count"] for e in events),
            "aligned": len(aligned),
            "events": aligned,
            "agg": agg,
            "stats": factor_stats(mem),
            "window_roi": roi,
        }
    return out


def attributed_window_roi(mem: dict, role_orders: list[dict],
                           start: str, end: str) -> dict:
    """归因到因子的唯一订单（接真实注额/盈亏）的窗口 ROI。"""
    order_by_lid = {}
    for o in role_orders:
        lid = o.get("lota_id")
        if lid:
            order_by_lid[lid] = ((o.get("settled_at") or "")[:10],
                                 o.get("bet_size", 0) or 0,
                                 o.get("profit", 0) or 0)
    lids = set()
    for v in mem.get("factor_perf", {}).values():
        for h in v.get("history", []) or []:
            lid = h.get("lota_id")
            d = (h.get("date") or "")[:10]
            if not lid or not d:
                continue
            rec = order_by_lid.get(lid)
            if rec and rec[0] == d and start <= d <= end:
                lids.add(lid)
    stake = sum(order_by_lid[l][1] for l in lids)
    pnl = sum(order_by_lid[l][2] for l in lids)
    return {"n": len(lids), "stake": round(stake, 2), "pnl": round(pnl, 2),
            "roi": round(pnl / stake, 4) if stake else 0.0}


def render_report(agent: str, start: str, end: str, max_matches: int,
                  history_tokens: int, days_run: list[str], metrics: dict,
                  memories: dict[str, dict]) -> str:
    L: list[str] = []
    L.append(f"# reflect 因子层对照：历史同信号回顾（方案A）")
    L.append("")
    L.append(f"- 角色: {agent}")
    L.append(f"- 窗口: {start} ~ {end}（结算日 {len(days_run)} 天）")
    L.append(f"- 对照组: 现 reflect 逻辑（只思考当天订单）")
    L.append(f"- 实验组: +历史同信号回顾（最近 {max_matches} 场/次，预算 {history_tokens} token）")
    L.append(f"- Δ 口径: 复用 analyze_evolution（exact-kelly / day-stake / live-only）")
    L.append("")

    L.append("## 新因子日对齐（前后 7 天平均 Δ，越正 = 因子出现后行情越好）")
    L.append("")
    L.append("| regime | 可对齐事件 | 前7d | 后7d | 改善 | 正向 | t | p(改善) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for regime, m in metrics["regimes"].items():
        a = m["agg"]
        label = "对照组" if regime == CTL else "实验组"
        p = a["p_improve"]
        p_s = f"{p:.3f}" if p == p else "-"
        L.append(f"| {label} | {a['n']} | {a['avg_before']:+.3f} | "
                 f"{a['avg_after']:+.3f} | {a['avg_diff']:+.3f} | "
                 f"{a['pos']}/{a['n']} | {a['t']:.2f} | {p_s} |")
    L.append("")

    L.append("## 因子质量（窗口结束时各自 memory）")
    L.append("")
    L.append("| regime | 因子总数 | 窗口新增 | 归因样本 | 命中率 | 总回报 | 总盈亏 | 正收益因子 | 归因订单窗口ROI |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for regime, m in metrics["regimes"].items():
        s = m["stats"]
        w = m["window_roi"]
        label = "对照组" if regime == CTL else "实验组"
        L.append(f"| {label} | {s['total']} | {m['new_factor_count']} | {s['samples']} | "
                 f"{s['hit_rate']:.1%} | {s['total_return']:+.3f} | "
                 f"{s['profit']:+.0f} | {s['positive']} | "
                 f"{w['n']}单 {w['roi']*100:+.1f}% |")
    L.append("")

    ctl_names = set((memories[CTL].get("factor_perf") or {}).keys())
    exp_names = set((memories[EXP].get("factor_perf") or {}).keys())
    L.append("## 因子集合差异")
    L.append("")
    L.append(f"- 共用: {len(ctl_names & exp_names)}；仅对照组: {len(ctl_names - exp_names)}；仅实验组: {len(exp_names - ctl_names)}")
    if ctl_names - exp_names:
        L.append("- 仅对照组: " + ", ".join(sorted(ctl_names - exp_names)))
    if exp_names - ctl_names:
        L.append("- 仅实验组: " + ", ".join(sorted(exp_names - ctl_names)))
    L.append("")
    return "\n".join(L)


def run_agg(base_out: Path) -> int:
    """汇总 base_out/run*/report.json 的多轮对照结果（均值）。"""
    runs = sorted(base_out.glob("run*/report.json"))
    if not runs:
        print(f"未找到 {base_out / 'run*' / 'report.json'}")
        return 1
    print(f"共 {len(runs)} 轮: {', '.join(r.parent.name for r in runs)}\n")
    for label, key in (("对照组", CTL), ("实验组", EXP)):
        rows = []
        for rp in runs:
            try:
                d = json.loads(rp.read_text(encoding="utf-8"))
                m = d["regimes"][key]
                s = m["stats"]
                w = m["window_roi"]
                a = m["agg"]
                rows.append((rp.parent.name, s["hit_rate"], s["total_return"],
                             w["roi"], m["new_factor_count"], a["avg_diff"]))
            except Exception as e:
                print(f"  ⚠️ {rp}: {e}")
        if not rows:
            continue
        n = len(rows)
        means = tuple(sum(r[i] for r in rows) / n for i in range(1, 6))
        print(f"══ {label} ══")
        for r in rows:
            print(f"  {r[0]}: 命中率 {r[1]:.1%} | 总回报 {r[2]:+.3f} | "
                  f"归因ROI {r[3]*100:+.1f}% | 新增 {r[4]:>2} | Δ改善 {r[5]:+.3f}")
        print(f"  均值: 命中率 {means[0]:.1%} | 总回报 {means[1]:+.3f} | "
              f"归因ROI {means[2]*100:+.1f}% | 新增 {means[3]:.1f} | Δ改善 {means[4]:+.3f}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="reflect 因子层对照（方案A）")
    ap.add_argument("--agent", default="梭哈2狗")
    ap.add_argument("--start", default="2026-07-13")
    ap.add_argument("--end", default="2026-08-12")
    ap.add_argument("--days", default="", help="逗号分隔的结算日，覆盖 start/end")
    ap.add_argument("--max-matches", type=int, default=8)
    ap.add_argument("--history-tokens", type=int, default=3200)
    ap.add_argument("--history-days", type=int, default=90,
                    help="历史时间遗忘窗口（天），默认近 3 个月")
    ap.add_argument("--run-id", default="1",
                    help="第几轮对照（run1/run2/run3，隔离输出与演化记忆）")
    ap.add_argument("--live-start", default="2026-07-11")
    ap.add_argument("--w-mode", choices=["bankroll", "day-stake"], default="day-stake")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--agg", action="store_true",
                    help="汇总 base_out/run*/report.json 的多轮结果")
    args = ap.parse_args(argv)

    base_out = Path(args.out) if args.out else REPORTS_DIR / f"slughist_{args.agent}"
    if args.agg:
        return run_agg(base_out)

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    if not days:
        days = list(daily_settled_orders(args.agent, args.start, args.end).keys())
    if not days:
        print("窗口内没有已结算订单")
        return 1

    out_dir = base_out / f"run{args.run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = load_progress(out_dir) if args.resume else set()

    base_ctl = ensure_memory_dir(args.agent, CTL, args.run_id)
    base_exp = ensure_memory_dir(args.agent, EXP, args.run_id)
    role_ctl = build_role(args.agent, base_ctl)
    role_exp = build_role(args.agent, base_exp)

    provider = _DryProvider() if args.dry_run else DeepSeekProvider()
    if not args.dry_run and not provider.is_available:
        print("⚠️ DEEPSEEK_API_KEY 未设置")
        return 1

    from src.data_manager import DataManager
    from src.slug_history import get_index

    dm = DataManager()
    get_index()  # 预构建一次索引，多天复用

    all_orders = daily_settled_orders(args.agent, args.start, args.end)
    history_used: dict[str, int] = {}
    run_days = [d for d in days if d in all_orders]
    skipped = len(days) - len(run_days)
    if skipped:
        print(f"⚠️ {skipped} 天无结算订单，跳过")

    for day in run_days:
        settled = all_orders[day]
        for regime, role, use_hist in ((CTL, role_ctl, False),
                                       (EXP, role_exp, True)):
            if (day, regime) in done:
                print(f"  ↪ {day} {regime} 已完成，跳过")
                continue
            res = run_reflect(
                settled, day, role,
                provider=provider, dm=dm,
                use_slug_history=use_hist,
                slug_history_max=args.max_matches,
                slug_history_tokens=args.history_tokens,
                slug_history_days=args.history_days,
                extra_matches=False,   # 两组一致、不联网、可复现
                save_fac=False,
            )
            if use_hist:
                history_used[day] = len(res["history_block"].splitlines()) if res["history_block"] else 0
            print(f"  {day} {regime}: settled={len(settled)} "
                  f"alpha={res['alpha'][:40]!r} new={len(res['new_factors'])} "
                  f"attr={len(res['attr_map'])} hist_lines={history_used.get(day, '-')} "
                  f"prompt_tokens={count_tokens(res['prompt'])}")

            if args.dry_run:
                pdir = out_dir / "prompts"
                pdir.mkdir(parents=True, exist_ok=True)
                (pdir / f"{day}_{regime}.txt").write_text(
                    res["prompt"], encoding="utf-8")
            elif not res["reflection"] or res.get("error"):
                print(f"  ⚠️ {day} {regime} LLM 无结果，不记进度，可 --resume 重试")
            else:
                append_progress(out_dir, day, regime)

    if args.dry_run:
        print(f"\n✅ dry-run 完成：prompts 已写入 {out_dir / 'prompts'}")
        return 0

    # ── 指标对比 ──
    memories = {
        CTL: json.loads((base_ctl / "factor_memory.json").read_text(encoding="utf-8")),
        EXP: json.loads((base_exp / "factor_memory.json").read_text(encoding="utf-8")),
    }
    metrics = build_metrics(args.agent, args.start, args.end,
                            args.live_start, args.w_mode, memories)
    report = render_report(args.agent, args.start, args.end, args.max_matches,
                           args.history_tokens, run_days, metrics, memories)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"factor_memory_{CTL}.json").write_text(
        json.dumps(memories[CTL], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"factor_memory_{EXP}.json").write_text(
        json.dumps(memories[EXP], ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + report)
    print(f"\n报告: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
