#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快照全链路回放（最省方向验证）：只跑实验组（新方案 = 历史同信号回顾）。

对照组直接用真实历史订单参考（口径不纯，真实盘含人工干预，仅作方向参考）。

流程：
  1. 从 07-12 备份（干净快照，含 07-11/12 结果）创建 <agent>_snapshot 角色；
  2. 逐日 [结算(真实赛果, 触发 reflect=新方案) → 分析(LLM 推荐/下单)]，
     资金/订单/因子库持久化在快照角色，下一天分析能看到演化后的因子；
  3. 输出逐日/周/全窗口对比（快照 vs 真实历史）：单数、注额、盈亏、ROI、资金曲线。

支持 --resume 断点续跑（每完成一天记一次进度，LLM 失败当天不记、下次重跑该天）。

用法:
  /Users/cjy/miniconda3/bin/python scripts/snapshot_replay.py            # 全窗口
  /Users/cjy/miniconda3/bin/python scripts/snapshot_replay.py --resume   # 续跑
  /Users/cjy/miniconda3/bin/python scripts/snapshot_replay.py --days 2026-07-13,2026-07-14
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ROLES_DIR = PROJECT_ROOT / "data" / "roles"
BACKUP_DIR = PROJECT_ROOT / "data" / "roles.backup.20260712_010652"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
MATCHES_DIR = PROJECT_ROOT / "data" / "matches"
WINDOW_START = " 12:00:00"


def snapshot_name(agent: str) -> str:
    return f"{agent}_snapshot"


def setup_snapshot_role(agent: str, force: bool = False) -> None:
    """从 07-12 备份创建快照角色（干净起点，含 07-11/12 结果）。"""
    sim = snapshot_name(agent)
    sim_dir = ROLES_DIR / sim
    if sim_dir.exists():
        if not force:
            return
        shutil.rmtree(sim_dir)
    sim_dir.mkdir(parents=True)
    (sim_dir / "memory").mkdir()
    (sim_dir / "predicts").mkdir()

    bk = BACKUP_DIR / agent
    if (bk / "persona.md").exists():
        shutil.copy2(bk / "persona.md", sim_dir / "persona.md")
    data = json.loads((bk / f"{agent}.json").read_text(encoding="utf-8"))
    data["name"] = sim
    (sim_dir / f"{sim}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for fp in (bk / "memory").glob("*.json"):
        shutil.copy2(fp, sim_dir / "memory" / fp.name)
    print(f"  🧱 快照角色已创建: {sim}（资金 {data.get('capital', '?')}，"
          f"订单 {len(data.get('orders') or [])}，因子库随备份）")


def day_lids(day: str) -> dict[str, str]:
    """足球日 [day 12:00, day+1 12:00) 内比赛的 lota_id → 对阵。

    扫全部 matches 缓存（文件命名约定不统一，按 match_time 窗口归集最稳）。
    """
    start = day + WINDOW_START
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat() + WINDOW_START
    lids = {}
    for fp in sorted(MATCHES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        ms = data if isinstance(data, list) else (data.get("matches") or data.get("data") or [])
        for m in ms:
            mt = m.get("match_time", "")
            if start <= mt < end and m.get("lota_id"):
                lids[m["lota_id"]] = f"{m.get('home_name','?')} vs {m.get('away_name','?')}"
    return lids


def real_orders(agent: str) -> list[dict]:
    return json.loads(
        (ROLES_DIR / agent / f"{agent}.json").read_text(encoding="utf-8")
    ).get("orders", [])


def orders_in_lids(orders: list[dict], lids: dict[str, str]) -> list[dict]:
    out = []
    for o in orders:
        lid = o.get("lota_id", "")
        if lid in lids and not o.get("skip"):
            out.append(o)
    return out


def order_summary(o: dict) -> dict:
    return {
        "lota_id": o.get("lota_id", ""),
        "match": o.get("match", ""),
        "bet_type": o.get("bet_type", ""),
        "pick": o.get("pick", ""),
        "odds": o.get("odds", 0),
        "handicap": o.get("handicap", 0),
        "bet_size": o.get("bet_size", 0),
        "hit": o.get("hit"),
        "profit": o.get("profit", 0),
        "settled_at": o.get("settled_at", ""),
        "reason": (o.get("reason") or "")[:120],
    }


def bucket_stats(orders: list[dict]) -> dict:
    n = len(orders)
    stake = sum(o.get("bet_size", 0) or 0 for o in orders)
    pnl = sum(o.get("profit", 0) or 0 for o in orders if o.get("settled_at"))
    pending = sum(1 for o in orders if not o.get("settled_at"))
    return {"n": n, "stake": round(stake, 2), "pnl": round(pnl, 2),
            "roi": round(pnl / stake, 4) if stake else 0.0,
            "pending": pending}


def week_of(dstr: str) -> str:
    d = date.fromisoformat(dstr)
    return (d - timedelta(days=d.weekday())).isoformat()


def load_progress(out_dir: Path) -> set[str]:
    done: set[str] = set()
    pf = out_dir / "progress.jsonl"
    if pf.exists():
        for line in pf.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["day"])
            except Exception:
                continue
    return done


def append_progress(out_dir: Path, day: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"day": day, "ts": datetime.now().isoformat()},
                           ensure_ascii=False) + "\n")


def render_report(agent: str, days: list[str], records: list[dict],
                  role_orders: list[dict] | None = None,
                  start_capital: float | None = None,
                  actual_capital: float | None = None) -> str:
    L: list[str] = []
    L.append(f"# 快照全链路回放：实验组（新方案） vs 真实历史（对照参考）")
    L.append("")
    L.append(f"- 角色: {agent}（快照角色 {snapshot_name(agent)}，07-12 备份起点）")
    L.append(f"- 窗口: {days[0]} ~ {days[-1]}（{len(days)} 天）")
    L.append("- 实验组: 每天 LLM 推荐/下单 + 真实赛果结算 + reflect 历史同信号回顾（近90天/8场/3200token）")
    L.append("- 对照组: 真实历史订单（含人工干预，仅方向参考）")
    L.append("")
    L.append("## 逐日对比")
    L.append("")
    L.append("| 日 | 真实单/ROI | 快照单/ROI | 快照可用余额(下单后) |")
    L.append("|---|---|---|---|")
    for r in records:
        rl, sl = r["real"], r["sim"]
        L.append(f"| {r['day']} | {rl['n']}单 {rl['roi']*100:+.1f}% | "
                 f"{sl['n']}单 {sl['roi']*100:+.1f}% | {r['capital']:.0f} |")
    L.append("")
    L.append("## 周对比（周一起）")
    L.append("")
    L.append("| 周 | 真实 | 快照 |")
    L.append("|---|---|---|")
    wk: dict[str, dict] = defaultdict(lambda: {"real": [], "sim": []})
    for r in records:
        wk[week_of(r["day"])]["real"].append(r["real"])
        wk[week_of(r["day"])]["sim"].append(r["sim"])
    for w in sorted(wk):
        def agg(items):
            n = sum(x["n"] for x in items)
            stake = sum(x["stake"] for x in items)
            pnl = sum(x["pnl"] for x in items)
            return f"{n}单 {pnl/stake*100:+.1f}%" if stake else f"{n}单 -"
        L.append(f"| {w} | {agg(wk[w]['real'])} | {agg(wk[w]['sim'])} |")
    L.append("")
    L.append("## 全窗口合计")
    L.append("")
    real_all = bucket_stats([o for r in records for o in r["real_orders"]])
    sim_all = bucket_stats([o for r in records for o in r["sim_orders"]])
    # 资金曲线/回撤：按真实赛果结算时序重构（起始资金 + 窗口订单逐笔盈亏）
    curve: list[tuple[str, float, float]] = []
    if role_orders is not None and start_capital is not None:
        win_lids = set()
        for d in days:
            win_lids |= set(day_lids(d).keys())
        # 备份里未结算订单在回放早期被结算的盈亏（不在窗口足球日内，结算时间=回放日）
        replay_day = datetime.now().strftime("%Y-%m-%d")
        backfill = sum(
            (o.get("profit", 0) or 0) for o in role_orders
            if o.get("lota_id") not in win_lids
            and (o.get("settled_at") or "")[:10] >= replay_day
            and not o.get("skip")
        )
        cap = start_capital + backfill
        for d in days:
            lids = day_lids(d)
            day_pnl = sum(
                (o.get("profit", 0) or 0) for o in role_orders
                if o.get("lota_id") in lids and not o.get("skip")
            )
            cap += day_pnl
            curve.append((d, day_pnl, cap))
    L.append(f"- 真实: {real_all['n']} 单 | 注额 {real_all['stake']:.0f} | "
             f"盈亏 {real_all['pnl']:+.0f} | ROI {real_all['roi']*100:+.1f}%"
             f"（{real_all['pending']} 单待结算）")
    L.append(f"- 快照: {sim_all['n']} 单 | 注额 {sim_all['stake']:.0f} | "
             f"盈亏 {sim_all['pnl']:+.0f} | ROI {sim_all['roi']*100:+.1f}%"
             f"（{sim_all['pending']} 单待结算）")
    if curve:
        peak = curve[0][2]
        peak_day = curve[0][0]
        max_dd, dd_from, dd_to = 0.0, "", ""
        for d, pnl, c in curve:
            if c > peak:
                peak = c
                peak_day = d
            dd = (c - peak) / peak if peak else 0
            if dd < max_dd:
                max_dd = dd
                dd_from = peak_day
                dd_to = d
        L.append(f"- 快照结算后资金: 起点 {start_capital + backfill:.0f}（备份 {start_capital:.0f} "
                 f"+ 回填期结算 {backfill:+.0f}）→ {curve[-1][2]:.0f} "
                 f"(窗口累计 {curve[-1][2] - start_capital - backfill:+.0f})")
        if actual_capital is not None:
            gap = actual_capital - curve[-1][2]
            L.append(f"- 快照实际余额（角色账本）: {actual_capital:.0f}"
                     f"（与窗口曲线差额 {gap:+.0f} = 回放早期退单/回填等资金变动，"
                     f"不影响窗口订单 ROI）")
        L.append(f"- 快照最大回撤: {max_dd*100:.1f}%（峰值 {peak:.0f} 高点 {dd_from} → 低点 {dd_to}）")
        L.append("")
        L.append("## 快照资金曲线（按足球日）")
        L.append("")
        L.append("| 足球日 | 当日盈亏 | 结算后资金 |")
        L.append("|---|---|---|")
        for d, pnl, c in curve:
            L.append(f"| {d} | {pnl:+10.0f} | {c:10.0f} |")
    L.append("")
    L.append("## 快照逐单明细")
    L.append("")
    L.append("| 日 | 比赛 | 类型 | pick | 赔率 | 注额 | 结果 | 盈亏 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in records:
        for o in r["sim_orders"]:
            icon = "✅" if o.get("hit") is True else ("❌" if o.get("hit") is False else "➖")
            L.append(f"| {r['day']} | {o.get('match','?')} | {o.get('bet_type','')} | "
                     f"{o.get('pick','')} | {o.get('odds',0):.2f} | {o.get('bet_size',0):.0f} | "
                     f"{icon} | {o.get('profit',0):+.0f} |")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="快照全链路回放（实验组 vs 真实历史）")
    ap.add_argument("--agent", default="梭哈2狗")
    ap.add_argument("--start", default="2026-07-13")
    ap.add_argument("--end", default="2026-08-12")
    ap.add_argument("--days", default="", help="逗号分隔，覆盖 start/end")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--init", action="store_true", help="强制重建快照角色")
    ap.add_argument("--out", default="")
    ap.add_argument("--max-matches", type=int, default=8)
    ap.add_argument("--history-tokens", type=int, default=3200)
    ap.add_argument("--history-days", type=int, default=90)
    args = ap.parse_args(argv)

    # 新方案 reflect 配置（settle 图内 node_reflect 读环境变量）
    os.environ["REFLECT_SLUG_HISTORY"] = "1"
    os.environ["REFLECT_SLUG_HISTORY_MAX"] = str(args.max_matches)
    os.environ["REFLECT_SLUG_HISTORY_TOKENS"] = str(args.history_tokens)
    os.environ["REFLECT_SLUG_HISTORY_DAYS"] = str(args.history_days)
    os.environ["REFLECT_EXTRA_MATCHES"] = "0"   # 与方案A一致，不联网补样本

    setup_snapshot_role(args.agent, force=args.init)
    sim = snapshot_name(args.agent)

    from src.agent import Agent, _rt
    from src.providers.deepseek import DeepSeekProvider

    agent = Agent(user=sim)
    agent.init_role()
    agent.set_provider(DeepSeekProvider())
    rt = _rt({"user": sim})
    if not rt.provider or not rt.provider.is_available:
        print("⚠️ DEEPSEEK_API_KEY 未设置")
        return 1

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    if not days:
        d0 = date.fromisoformat(args.start)
        d1 = date.fromisoformat(args.end)
        days = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]

    out_dir = Path(args.out) if args.out else REPORTS_DIR / f"snapshot_{args.agent}"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = load_progress(out_dir) if args.resume else set()

    real = real_orders(args.agent)
    # 已有记录（续跑时保留此前天的对比数据）
    day_records: dict[str, dict] = {}
    dfp = out_dir / "days.jsonl"
    if dfp.exists():
        for line in dfp.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                day_records[rec["day"]] = rec
            except Exception:
                continue

    for day in days:
        if day in done:
            print(f"  ↪ {day} 已完成，跳过")
            continue
        lids = day_lids(day)
        if not lids:
            print(f"  ⏭ {day} 无比赛数据（matches 缓存缺失）")
            continue
        # 重跑安全：清掉当天未结算的快照订单（防重复下单）
        if rt.role:
            before = [o for o in rt.role.orders
                      if not (o.get("lota_id", "") in lids and not o.get("settled_at"))]
            if len(before) != len(rt.role.orders):
                rt.role.orders = before
                rt.role.save()
        try:
            settlement = agent.settle(day, jingcai_only=True)
            analysis = agent.analyze(day, live=True, prefetched=True, jingcai_only=True)
        except Exception as e:
            print(f"  ❌ {day} 失败: {e}（未记进度，可 --resume 重试）")
            break

        if not analysis.get("llm_response"):
            print(f"  ⚠️ {day} LLM 无响应（下单 0 单），未记进度，可 --resume 重试")
            break

        sim_orders = [order_summary(o) for o in orders_in_lids(rt.role.orders, lids)]
        real_orders_day = [order_summary(o) for o in orders_in_lids(real, lids)]
        rec = {
            "day": day,
            "capital": round(rt.role.capital, 2) if rt.role else 0,
            "settlement": settlement,
            "matches_count": analysis.get("matches_count", 0),
            "prompt_tokens": analysis.get("prompt_tokens", 0),
            "sim": bucket_stats(sim_orders),
            "real": bucket_stats(real_orders_day),
            "sim_orders": sim_orders,
            "real_orders": real_orders_day,
        }
        day_records[day] = rec
        with open(dfp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        append_progress(out_dir, day)
        print(f"  {day} | 场次{rec['matches_count']} prompt{rec['prompt_tokens']}t | "
              f"快照 {rec['sim']['n']}单 ROI{rec['sim']['roi']*100:+.1f}% | "
              f"真实 {rec['real']['n']}单 ROI{rec['real']['roi']*100:+.1f}% | "
              f"资金 {rec['capital']:.0f}")

    # 收尾：把窗口最后一天的订单也结算掉（如 08-12 的比赛 08-13 出赛果）
    if days:
        flush_day = (date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat()
        try:
            agent.settle(flush_day, jingcai_only=True)
            print(f"  🧹 收尾结算 {flush_day}")
        except Exception as e:
            print(f"  ⚠️ 收尾结算失败: {e}")
        # 重算所有天的快照订单（收尾结算后补全 settled/profit）
        if rt.role:
            for d in days:
                if d not in day_records:
                    continue
                lids = day_lids(d)
                sim_orders = [order_summary(o) for o in orders_in_lids(rt.role.orders, lids)]
                day_records[d]["sim"] = bucket_stats(sim_orders)
                day_records[d]["sim_orders"] = sim_orders
                # capital 保留每天记录时的值（资金曲线用），不要覆盖成最终余额
            with open(dfp, "w", encoding="utf-8") as f:
                for d in days:
                    if d in day_records:
                        f.write(json.dumps(day_records[d], ensure_ascii=False) + "\n")

    records = [day_records[d] for d in days if d in day_records]
    if records:
        start_cap = 0.0
        bk_json = BACKUP_DIR / args.agent / f"{args.agent}.json"
        if bk_json.exists():
            try:
                start_cap = float(json.loads(bk_json.read_text(encoding="utf-8")).get("capital", 0))
            except Exception:
                start_cap = 0.0
        report = render_report(
            args.agent, days, records,
            role_orders=rt.role.orders if rt.role else [],
            start_capital=start_cap,
            actual_capital=rt.role.capital if rt.role else None,
        )
        (out_dir / "report.md").write_text(report, encoding="utf-8")
        (out_dir / "report.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n" + report)
        print(f"\n报告: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
