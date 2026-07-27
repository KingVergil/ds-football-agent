#!/usr/bin/env python3
"""
实时监控 Agent 运行状态：资金曲线 + 因子状态

用法:
  python monitor.py 跟风狗            # 一次性查看
  python monitor.py 跟风狗 --loop     # 每 10s 刷新
  python monitor.py 跟风狗 --loop 5   # 每 5s 刷新
  python monitor.py                   # 默认监控 跟风狗
"""

import json
import sys
import os
import time
from pathlib import Path
from datetime import datetime

ROLES_DIR = Path(__file__).parent / "lota_data" / "roles"

# ── 颜色 ──
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def load_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def plot_capital_curve(history: list[dict], width: int = 60, height: int = 10):
    if not history:
        print(f"{DIM}  (无数据){RESET}")
        return

    values = [h["capital"] for h in history]
    dates = [h["date"] for h in history]
    hi, lo = max(values), min(values)
    if hi == lo:
        hi += 100
        lo -= 100

    bar_width = max(1, width // len(values))

    for row in range(height, -1, -1):
        threshold = lo + (hi - lo) * row / height
        label = lo + (hi - lo) * row / height
        line = f"{DIM}{label:>7.0f} │{RESET}"
        for v in values:
            line += "█" * bar_width if v >= threshold else " " * bar_width
        print(line)

    x = " " * 8 + "└" + "─" * (len(values) * bar_width)
    print(f"{DIM}{x}{RESET}")
    date_line = " " * 9
    for i, d in enumerate(dates):
        date_line += d[-5:] + " " * max(0, bar_width - 5)
    print(f"{DIM}{date_line}{RESET}")
    print()


def show_factors(factor_perf: dict):
    active = []
    retired = []
    for fid, s in factor_perf.items():
        (retired if s.get("status") == "retired" else active).append((fid, s))

    if active:
        print(f"{BOLD}📐 活跃因子 ({len(active)}):{RESET}")
        for fid, s in sorted(active, key=lambda x: -x[1]["total"]):
            total, hit, miss, push = s["total"], s["hit"], s["miss"], s["push"]
            denom = total - push
            rate = f"{hit/denom*100:.0f}%" if denom > 0 else "-"
            ret = s.get("total_return", s.get("profit", 0))
            desc = s.get("desc", "")
            color = GREEN if ret > 0 else (RED if ret < 0 else RESET)
            print(f"  {color}{fid}{RESET}: {total}次 命中{rate} 回报率{ret:+.2f}")
            if desc:
                print(f"    {DIM}{desc[:100]}{RESET}")
        print()

    if retired:
        print(f"{DIM}🪦 退役 ({len(retired)}): {', '.join(k for k,_ in retired)}{RESET}\n")


def show_pending(role_dir: Path):
    orders_dir = role_dir / "orders"
    pending = []
    if orders_dir.exists():
        for f in sorted(orders_dir.glob("*.json")):
            try:
                orders = json.loads(f.read_text(encoding="utf-8"))
                for o in (orders if isinstance(orders, list) else [orders]):
                    if not o.get("settled_at"):
                        pending.append(o)
            except Exception:
                pass
    if pending:
        print(f"{BOLD}📋 未结算 ({len(pending)}):{RESET}")
        for o in pending:
            print(f"  {o.get('lota_id','?')} {o.get('bet_type','')} {o.get('pick','')} "
                  f"@{o.get('odds',0):.2f} bet{o.get('bet_size',0):.0f}")
        print()


def run(role: str):
    role_dir = ROLES_DIR / role
    if not role_dir.exists():
        print(f"❌ 角色 '{role}' 不存在")
        return

    hist = load_json(role_dir / "capital_history.json") or []
    role_json = load_json(role_dir / f"{role}.json")
    capital = role_json.get("capital", 0) if role_json else 0
    initial = role_json.get("initial_capital", 10000) if role_json else 10000
    pnl = capital - initial

    print(f"\n{BOLD}{'='*50}{RESET}")
    print(f"{BOLD}  {role} 实时监控 {RESET}  {DIM}{datetime.now().strftime('%H:%M:%S')}{RESET}")
    print(f"{BOLD}{'='*50}{RESET}\n")
    print(f"  💰 资金: {initial:.0f} → {CYAN}{BOLD}{capital:.0f}{RESET}  "
          f"(PnL {RED if pnl<0 else GREEN}{pnl:+.0f}{RESET}, "
          f"{RED if pnl<0 else GREEN}{pnl/initial*100:+.1f}%{RESET})\n")

    print(f"{BOLD}📈 资金曲线:{RESET}")
    plot_capital_curve(hist)

    fm = load_json(role_dir / "memory" / "factor_memory.json")
    if fm:
        show_factors(fm.get("factor_perf", {}))
    else:
        print(f"{DIM}  (尚无因子){RESET}\n")

    show_pending(role_dir)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    role = args[0] if args else "跟风狗"
    loop = "--loop" in sys.argv
    interval = 10
    for i, a in enumerate(sys.argv):
        if a == "--loop" and i + 1 < len(sys.argv):
            try:
                interval = int(sys.argv[i + 1])
            except ValueError:
                pass

    try:
        while True:
            os.system("clear")
            run(role)
            if not loop:
                break
            print(f"{DIM}  ── {interval}s 后刷新 (Ctrl+C 退出) ──{RESET}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n{GREEN}👋 退出{RESET}")


if __name__ == "__main__":
    main()
