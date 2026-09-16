#!/usr/bin/env python3
"""逐日回放驱动 + 自动 review（薄壳直调 python 桥，2026-09-14）。

顺序（用户口径）
──────────────
    reflect(D) → analyze(D+1) → settle(D+1) → reflect(D+1) → analyze(D+2) → …

每个操作都做 review，**发现违规立即停下并记录**（不静默继续）：

| 编号 | 检查 | 判据 |
|---|---|---|
| R1 | 票型/成本 | 必须 `9过4`、252 元/单、每腿**单选**（`max_per_leg=1`） |
| R2 | **场次重复下单** | 同一天多张单之间，腿的 `lota_id` **重叠必须为 0** |
| R3 | 泄漏 | analyze 的 system prompt 不得含**当日赛后**信息（比分/开奖/赛果） |
| R4 | 因子迭代 | reflect 的 `factors_delta` / 累计因子数，观察是否改善 |

用法
────
    WS=<沙箱 workspace> python3 -m scripts.replay_day_by_day 2026-07-04 5
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import subprocess
import sys
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY_BIN = sys.executable
ENGINE = str(ROOT)

# analyze 的 system prompt 里**不该出现**的赛后标记
# ⚠️ 不能用裸「比分」——让球口径说明里就有「同一比分在不同让球线下…」（误报）
# 只查**赛后数据块**才会出现的标记
# ⚠️ 「赛果」「比分」在**让球口径说明**与**因子描述**里都会出现（如"赛果易出主胜"）→ 误报。
# 只保留「只可能来自赛后数据块」的标记。
LEAK_MARKERS = ["比分=", "开奖SP", "spvalue", "result_des", "实际:"]


def bridge(ws: str, func: str, day: str, opts: dict | None = None) -> tuple[dict, str]:
    req = {"func": func, "dog": "95狗", "day": day,
           "opts": {**(opts or {}), "role_root": ws}}
    env = dict(os.environ)
    r = subprocess.run([PY_BIN, "-m", "src.bridge"], input=json.dumps(req), text=True,
                       capture_output=True, cwd=ENGINE, env=env)
    data, err = {}, r.stdout
    for ln in r.stdout.splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") == "result":
            data = o.get("data") or {}
        elif o.get("type") == "error":
            data = {"_error": o.get("message")}
    return data, r.stderr


def read_orders(ws: str) -> tuple[float, list[dict]]:
    d = json.loads((pathlib.Path(ws) / "95狗.json").read_text(encoding="utf-8"))
    return float(d.get("capital") or 0), list(d.get("orders") or [])


def factors_of(ws: str) -> dict:
    p = pathlib.Path(ws) / "memory" / "factor_memory.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("factor_perf") or {}


def newest_md(ws: str, action: str, day: str) -> pathlib.Path | None:
    cands = sorted((pathlib.Path(ws) / "sessions" / "95狗").glob(f"*_{action}_{day}.md"))
    return cands[-1] if cands else None


def review_orders(orders: list[dict], new_from: int) -> list[str]:
    """R1 + R2：只审查本次新增的订单。"""
    bad: list[str] = []
    new = orders[new_from:]
    for i, o in enumerate(new, 1):
        tt = str(o.get("ticket_type") or (o.get("flex") or {}).get("ticket") or "")
        cost = float(o.get("total_stake") or 0)
        legs = o.get("legs") or []
        # 自适应档位（ticket_m=4）：n 腿 → `n过4`，n ≥ 5；成本 = C(n,4)×2
        n = len(legs)
        if not re.fullmatch(rf"{n}过4", tt):
            bad.append(f"R1 票型异常：{tt!r}（应为 {n}过4）")
        exp_cost = 2.0 * math.comb(n, 4) if n >= 4 else 0.0
        if abs(cost - exp_cost) > 0.01:
            bad.append(f"R1 成本异常：{cost}（{n}过4 应为 {exp_cost:.0f}）")
        multi = [l.get("lota_id") for l in legs if len(l.get("picks") or []) > 1]
        if multi:
            bad.append(f"R1 出现多选腿（max_per_leg=1 应全单选）：{multi}")
        if n < 5:
            bad.append(f"R1 腿数 {n} < 5（应空仓，不该出票）")
    # R2：同一天的多张单之间不得重复用同一场
    seen: dict[str, int] = {}
    for o in new:
        for l in o.get("legs") or []:
            lid = l.get("lota_id") or ""
            seen[lid] = seen.get(lid, 0) + 1
    dup = {k: v for k, v in seen.items() if v > 1}
    if dup:
        bad.append(f"R2 **场次重复下单**（同一 lota_id 出现在多张单里）：{dup}")
    return bad


def review_leak(ws: str, day: str) -> list[str]:
    """R3：analyze 的 system prompt 不得含当日赛后信息。"""
    md = newest_md(ws, "analyze", day)
    if not md:
        return [f"R3 找不到 analyze session（{day}）"]
    t = md.read_text(encoding="utf-8")
    m = re.search(r"<summary>System Prompt.*?</summary>([\s\S]*?)</details>", t)
    block = m.group(1) if m else t
    hits = [k for k in LEAK_MARKERS if k in block]
    out = []
    if hits:
        out.append(f"R3 **疑似泄漏**：system prompt 含赛后标记 {hits}（{md.name}）")
    return out


def main(argv: list[str]) -> int:
    ws = os.environ.get("WS") or ""
    if not ws or not pathlib.Path(ws).is_dir():
        print("❌ 需要 WS=<沙箱 workspace>")
        return 1
    start = date.fromisoformat(argv[1] if len(argv) > 1 else "2026-07-04")
    ndays = int(argv[2] if len(argv) > 2 else 5)
    # ⚠️ 唯一日志名（2026-09-14 教训）：曾有两个循环并发写同一个 day_by_day_review.md，
    # 后完成的那个把前者的 review 整份覆盖掉。默认带 start+时间戳；可用 D2D_LOG 指定。
    import time as _t
    log = pathlib.Path(os.environ.get("D2D_LOG")
                       or (pathlib.Path(ws) / f"day_by_day_review_{start}_{_t.strftime('%H%M%S')}.md"))
    # 单实例锁：同一沙箱同时只允许一个循环（否则状态与日志互相污染）
    lock = pathlib.Path(ws) / ".d2d.lock"
    if lock.exists():
        try:
            _pid = int(lock.read_text().strip() or 0)
        except Exception:
            _pid = 0
        _alive = False
        try:
            os.kill(_pid, 0)
            _alive = True
        except Exception:
            _alive = False
        if _alive:
            print(f"❌ 已有循环在跑（pid={_pid}）→ 拒绝启动。删 {lock} 可强制。")
            return 1
    lock.write_text(str(os.getpid()), encoding="utf-8")
    lines = [f"# 逐日回放 review（start={start} days={ndays}）", ""]
    lines.append("| 日 | 操作 | 结果 | review |")
    lines.append("|---|---|---|---|")

    def rec(day: str, op: str, res: str, bad: list[str]) -> None:
        flag = "✅" if not bad else "❌ " + "；".join(bad)
        lines.append(f"| {day} | {op} | {res} | {flag} |")
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  [{day}] {op}: {res} → {flag}")

    violation = False
    for k in range(ndays):
        d = (start + timedelta(days=k)).isoformat()
        d1 = (start + timedelta(days=k + 1)).isoformat()

        # ① 因子（D）
        f0 = len(factors_of(ws))
        data, _ = bridge(ws, "reflect", d, {})
        f1 = len(factors_of(ws))
        rec(d, "reflect", f"mode={data.get('mode')} 因子 {f0}→{f1}", [])

        # ② 分析（D+1）
        cap0, orders0 = read_orders(ws)
        data, err = bridge(ws, "analyze", d1, {"prefetched": False, "live": False,
                                               "beidan_only": True})
        cap1, orders1 = read_orders(ws)
        # 每波实际腿数（引擎口径，权威）—— 空仓是预期行为，但要能看出"有几条腿"
        waves = re.findall(r"规则组装（ticket_mode=rule）：(\d+) 条腿", err)
        wx = "/".join(waves) if waves else "无组票"
        bad = review_orders(orders1, len(orders0)) + review_leak(ws, d1)
        rec(d1, "analyze", f"placed={data.get('placed')} 各波腿数[{wx}] "
                           f"资金 {cap0:.0f}→{cap1:.0f}", bad)
        if bad:
            violation = True   # 记录但**不停止**（用户要尽量往下跑，违规单列汇报）

        # ③ 结算（D+1）
        data, _ = bridge(ws, "settle", d1, {"stage": "settle"})
        s = data.get("settlement") or {}
        rec(d1, "settle", f"结算 {s.get('settled')} PnL {s.get('pnl')} "
                          f"资金 {data.get('capital')}", [])
        _ = violation

    # 收尾：全局汇总
    cap, orders = read_orders(ws)
    settled = [o for o in orders if o.get("settled_at")]
    lines += ["", "## 汇总", "",
              f"- 资金：{cap:.2f}",
              f"- 订单：{len(orders)} 张（已结算 {len(settled)}）",
              f"- 因子：{len(factors_of(ws))} 个"]
    for o in orders:
        lines.append(f"  - {o.get('ticket_type')} 成本{o.get('total_stake')} "
                     f"中奖{o.get('hit')} 盈亏{o.get('profit')}")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        lock.unlink()
    except Exception:
        pass
    print(f"\nreview 落盘：{log}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
