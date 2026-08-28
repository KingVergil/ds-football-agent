#!/usr/bin/env python
"""
按历史订单刷新所有狗的因子有效期（last_seen）—— 修复「结算反思缺失 → 因子有效期不更新 → 误休眠」。

背景（2026-08-25）：
  桥接 settle（看板 / 回放）此前漏设 LLM provider，node_reflect（结算后反思 / 归因）被跳过，
  因子 last_seen 从不随使用更新；因子有效性判定（factor_profile.dormant）按 last_seen 与
  当前时间的间隔计算，于是大量在用因子被误休眠 / 误退役。

本脚本用确定性方法修复时间维度（不调 LLM）：
  遍历每只狗的「已结算历史订单」，若订单 reason 中出现某因子名，就把该因子 last_seen
  刷新为该订单对应的足球日（matches 缓存 match_time − 12h；缺省用 created_at − 12h）。
  只更新 last_seen / first_seen（为空时）与 updated_at，不动 total / hit / profit / history。

用法（在 python-engine 目录下执行）:
  python scripts/refresh_factor_time.py                # 全部 live 狗 + 串关2狗
  python scripts/refresh_factor_time.py --dry-run      # 只打印将刷新内容，不写盘
  python scripts/refresh_factor_time.py --dog 梭哈2狗   # 指定狗
  python scripts/refresh_factor_time.py --revive-dormant [--min-recent 2026-07-01]
        # 顺带把「历史订单里有使用记录、且状态为 dormant」的因子恢复 active
        # （仅当最近使用日期 >= --min-recent，默认 2026-07-01）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ROLES_DIR = Path(os.environ.get("DS_ROLES_ROOT") or (DATA / "roles"))
MATCHES_DIR = DATA / "matches"


def football_day_of(time_str: str) -> str:
    """北京时间比赛时间 → 足球日（窗口 [D 12:01, D+1 12:00]）。"""
    if not time_str:
        return ""
    try:
        return (datetime.strptime(str(time_str)[:19], "%Y-%m-%d %H:%M:%S")
                - timedelta(hours=12)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def build_lota_day() -> dict[str, str]:
    """扫描 matches 缓存：lota_id → 足球日。"""
    lota_day: dict[str, str] = {}
    if not MATCHES_DIR.exists():
        return lota_day
    for f in MATCHES_DIR.glob("*.json"):
        try:
            arr = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for m in arr:
            lid = m.get("lota_id")
            if lid:
                day = football_day_of(m.get("match_time", ""))
                if day:
                    lota_day.setdefault(lid, day)
    return lota_day


def order_football_day(order: dict, lota_day: dict[str, str]) -> str:
    lid = str(order.get("lota_id", ""))
    if lid and lid in lota_day:
        return lota_day[lid]
    return football_day_of(order.get("created_at", ""))


def list_dogs(args) -> list[str]:
    sys.path.insert(0, str(ROOT))
    from src.role_registry import live_agents, ROLES_DIR as RR_ROLES_DIR

    dogs = live_agents()
    for n in ("串关2狗",):
        if (RR_ROLES_DIR / n / f"{n}.json").exists() and n not in dogs:
            dogs.append(n)
    if args.dog:
        dogs = [d for d in dogs if d == args.dog] or [args.dog]
    return dogs


def role_paths(dog: str) -> tuple[Path, Path]:
    if (ROLES_DIR / dog / f"{dog}.json").exists():
        return ROLES_DIR / dog / f"{dog}.json", ROLES_DIR / dog / "memory" / "factor_memory.json"
    # 平铺沙箱布局（DS_ROLES_ROOT）
    return ROLES_DIR / f"{dog}.json", ROLES_DIR / "memory" / "factor_memory.json"


def refresh_dog(dog: str, lota_day: dict[str, str], args) -> dict:
    role_p, fm_p = role_paths(dog)
    if not role_p.exists():
        return {"dog": dog, "error": f"角色文件缺失 {role_p}"}
    if not fm_p.exists():
        return {"dog": dog, "error": f"因子记忆缺失 {fm_p}"}

    try:
        role = json.loads(role_p.read_text(encoding="utf-8"))
        fm = json.loads(fm_p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"dog": dog, "error": f"读取失败: {e}"}

    fp = fm.get("factor_perf") or {}
    names = [n for n in fp.keys() if len(n.strip()) >= 2]
    if not names:
        return {"dog": dog, "orders": 0, "matched": 0, "refreshed": 0, "revived": 0, "changed": False}

    orders = role.get("orders") or []
    settled = [o for o in orders if o.get("settled_at")]
    # factor 名 → 最近使用足球日
    last_use: dict[str, str] = {}
    matched_orders = 0
    for o in settled:
        reason = str(o.get("reason") or "")
        if not reason:
            continue
        day = order_football_day(o, lota_day)
        if not day:
            continue
        hit = False
        for n in names:
            if n in reason:
                hit = True
                if day > last_use.get(n, ""):
                    last_use[n] = day
        if hit:
            matched_orders += 1

    refreshed = 0
    revived = 0
    for n, day in last_use.items():
        s = fp.get(n)
        if not isinstance(s, dict):
            continue
        old = str(s.get("last_seen") or "")
        if day > old:
            s["last_seen"] = day
            refreshed += 1
        if not s.get("first_seen") or day < str(s.get("first_seen") or "9999-99-99"):
            s["first_seen"] = day
        if args.revive_dormant and s.get("status") == "dormant" and day >= args.min_recent:
            s["status"] = "active"
            revived += 1

    changed = refreshed > 0 or revived > 0
    if changed and not args.dry_run:
        fm["updated_at"] = datetime.now().isoformat()
        tmp = fm_p.with_name(fm_p.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(fm, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(fm_p)

    return {
        "dog": dog,
        "orders": len(settled),
        "matched": matched_orders,
        "refreshed": refreshed,
        "revived": revived,
        "changed": changed,
        "top": sorted(last_use.items(), key=lambda kv: kv[1])[-8:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="按历史订单刷新所有狗因子有效期（last_seen）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    ap.add_argument("--dog", default="", help="只处理指定狗")
    ap.add_argument("--revive-dormant", action="store_true",
                    help="历史订单有使用记录且状态为 dormant 的因子恢复 active")
    ap.add_argument("--min-recent", default="2026-07-01",
                    help="--revive-dormant 的最小最近使用日期（默认 2026-07-01）")
    args = ap.parse_args()

    lota_day = build_lota_day()
    dogs = list_dogs(args)
    print(f"狗列表: {', '.join(dogs)}")
    print(f"matches 缓存 lota 数: {len(lota_day)} | dry-run={args.dry_run} revive={args.revive_dormant}")
    print()

    total_refreshed = total_revived = 0
    for dog in dogs:
        r = refresh_dog(dog, lota_day, args)
        if r.get("error"):
            print(f"❌ {dog}: {r['error']}")
            continue
        total_refreshed += r["refreshed"]
        total_revived += r["revived"]
        tag = "（dry-run）" if args.dry_run and r["changed"] else ""
        print(f"▸ {dog}: 已结算 {r['orders']} 单，命中因子订单 {r['matched']} 单，"
              f"刷新 last_seen {r['refreshed']} 个，恢复 active {r['revived']} 个{tag}")
        for n, d in r["top"]:
            print(f"    {d}  {n}")
        print()

    print(f"合计: 刷新 {total_refreshed} 个因子，恢复 active {total_revived} 个"
          f"{'（dry-run，未写盘）' if args.dry_run else '（已写盘）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
