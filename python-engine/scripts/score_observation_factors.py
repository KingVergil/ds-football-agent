#!/usr/bin/env python3
"""给「观察型因子」补虚拟结算（B 类修法，零 LLM 调用）。

问题（2026-09-12 定位）
─────────────────────
`_reflect_skipped()` 在「本狗没下单」的日子仍会造因子（这是**仿真人复盘**，合理），
但它把样本标成 `hit=None / profit=0`（只留触发记录，不给假收益）。后果：

* 这些因子**永远无法被统计打分** → 永远不会因为"表现差"被退役 → 只增不减
  （实测 45 天里观察型占 47~48%，且不随时间下降）；
* 它们却和真实因子**混在同一个 factor_perf、同一份 prompt 因子清单**里被使用。

修法：保留观察路径，但**给它补一次虚拟结算**——"如果当时按引擎的过门侧买了，
开奖后是命中还是未中"。于是观察因子也能进入可评估闭环（能被打分、能被退役）。

口径（诚实性关键）
────────────────
* **买单侧**由 `x ≥ θ` 决定（引擎的真实规则），**不是**由开奖结果反推——否则等于保送全中；
* 方向型观察样本：命中 ⇔ 实际方向 ∈ 过门侧集合；`profit = 0.65×SP − 1`（中）/ `−1`（不中）；
* 波动型（全包腿）观察样本：命中 ⇔ `SP ≥ 3/0.65 ≈ 4.615`（覆盖三注的成本线），
  `profit = (0.65×SP − 3) / 3`（按 3 注成本摊到每元）。

输入
───
* `--factor-memory <沙箱>/memory/factor_memory.json`：含观察样本（lota_id / sp / unit_cost）
* `--leg-pool data/leg_pool_full`：`rebuild_leg_pool.py` 的产物（每侧 x、实际结果、开奖 SP）
* `--theta 1.1`：过门门限（与回放一致）

用法
───
    python3 -m scripts.score_observation_factors \
        --factor-memory /tmp/beidan_run0908/memory/factor_memory.json \
        --leg-pool data/leg_pool_full --md docs/observation_factor_scoring.md
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path

BEIDAN_RATE = 0.65
HIGH_VOL_SP = 3.0 / BEIDAN_RATE      # 4.615…：全包腿"值不值"的成本线


def load_pool(pool_dir: str) -> dict[str, dict]:
    """lota_id → {side: x} + 实际结果/SP（同日多波取 x 最大的一份）。"""
    idx: dict[str, dict] = {}
    for f in sorted(glob.glob(os.path.join(pool_dir, "*.json"))):
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            lid = r.get("lota_id")
            if not lid:
                continue
            cur = idx.setdefault(lid, {"x": {}, "actual": "", "sp": 0.0, "settled": False})
            s = r.get("side")
            if s and float(r.get("x") or 0) > float(cur["x"].get(s) or 0):
                cur["x"][s] = float(r["x"])
            if r.get("settled"):
                cur.update({"actual": r.get("actual") or "", "sp": float(r.get("sp") or 0),
                            "settled": True})
    return idx


def score(fm_path: str, pool_dir: str, theta: float) -> dict:
    fm = json.loads(Path(fm_path).read_text(encoding="utf-8")).get("factor_perf") or {}
    idx = load_pool(pool_dir)
    factors = []
    for name, e in fm.items():
        obs, real = [], []
        for h in (e.get("history") or []):
            (obs if (h.get("hit") is None and not h.get("profit")) else real).append(h)
        ftype = str(e.get("type") or "directional")
        scored = []
        for h in obs:
            lid = h.get("lota_id")
            rec = idx.get(lid)
            if not rec or not rec.get("settled") or not rec["x"]:
                continue                      # 池子里没有 → 无法虚拟结算（诚实标注）
            gated = [s for s, x in rec["x"].items() if x >= theta]
            if not gated:
                continue                      # 当时没有任何侧过门 → 本就不该买
            sp = float(rec.get("sp") or 0)
            actual = rec.get("actual") or ""
            if ftype == "volatility":
                # 全包腿：命中 ⇔ 开奖 SP 覆盖三注成本；按 3 注成本摊每元
                hit = sp >= HIGH_VOL_SP
                profit = (BEIDAN_RATE * sp - 3.0) / 3.0 if hit else -1.0
                cost = 3.0
            else:
                hit = actual in gated
                profit = (BEIDAN_RATE * sp - 1.0) if hit else -1.0
                cost = 1.0
            scored.append({"lota_id": lid, "gated": gated, "actual": actual, "sp": sp,
                           "hit": hit, "profit": profit, "unit_cost": cost})
        real_pnl = sum(float(h.get("profit") or 0) for h in real)
        real_hit = sum(1 for h in real if h.get("hit") is True)
        real_miss = sum(1 for h in real if h.get("hit") is False)
        factors.append({
            "name": name, "type": ftype, "status": e.get("status"),
            "obs_total": len(obs), "obs_scored": len(scored),
            "obs_hit": sum(1 for s in scored if s["hit"]),
            "obs_pnl": round(sum(s["profit"] for s in scored), 3),
            "obs_roi": (sum(s["profit"] for s in scored) / len(scored)) if scored else None,
            "real_n": len(real), "real_hit": real_hit, "real_miss": real_miss,
            "real_pnl": round(real_pnl, 2),
            "scored": scored,
        })
    return {"factors": factors, "theta": theta, "pool_size": len(idx)}


def render(res: dict, fm_path: str) -> str:
    fs = res["factors"]
    scored = [f for f in fs if f["obs_scored"]]
    unscore = [f for f in fs if f["obs_total"] and not f["obs_scored"]]
    o = ["# 观察型因子虚拟结算（B 类修法验证）", ""]
    o.append(f"- 因子来源：`{fm_path}`（门限 θ={res['theta']}，腿池索引 {res['pool_size']} 场）")
    o.append(f"- 有观察样本的因子 {len([f for f in fs if f['obs_total']])} 个"
             f"｜**可虚拟结算 {len(scored)}**｜池中无记录无法结算 {len(unscore)}")
    n_obs = sum(f["obs_scored"] for f in scored)
    n_hit = sum(f["obs_hit"] for f in scored)
    tot_pnl = sum(f["obs_pnl"] for f in scored)
    o.append(f"- 已结算观察样本 **{n_obs}** 条｜命中 **{n_hit}**（{n_hit/n_obs:.1%}）"
             f"｜合计盈亏 **{tot_pnl:+.1f}**｜每元 **{tot_pnl/n_obs:+.1%}**" if n_obs else "- 无可结算样本")
    o.append("")
    o.append("## 逐因子（有虚拟结算的）")
    o.append("")
    o.append("| 因子 | 类型 | 观察样本 | 虚拟命中 | 虚拟盈亏 | 真实样本 | 真实命中 | 真实盈亏 |")
    o.append("|---|---|---|---|---|---|---|---|")
    for f in sorted(scored, key=lambda x: x["obs_pnl"]):
        o.append(f"| {f['name']} | {f['type']} | {f['obs_scored']}/{f['obs_total']} | "
                 f"{f['obs_hit']} | {f['obs_pnl']:+.2f} | {f['real_n']} | "
                 f"{f['real_hit']}/{f['real_hit']+f['real_miss']} | {f['real_pnl']:+.2f} |")
    if unscore:
        o.append("")
        o.append("## 无法虚拟结算（腿池无记录，需回放时落盘更多字段）")
        o.append("")
        o.append("| 因子 | 观察样本 | 原因 |")
        o.append("|---|---|---|")
        for f in unscore:
            o.append(f"| {f['name']} | {f['obs_total']} | 该场不在腿池（多为让球盘/无 x 侧） |")
    return "\n".join(o)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor-memory", required=True)
    ap.add_argument("--leg-pool", default="data/leg_pool_full")
    ap.add_argument("--theta", type=float, default=1.1)
    ap.add_argument("--md", default="")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)
    pool = args.leg_pool if os.path.isabs(args.leg_pool) else str(Path(__file__).resolve().parents[1] / args.leg_pool)
    res = score(args.factor_memory, pool, args.theta)
    text = render(res, args.factor_memory)
    print(text)
    if args.md:
        Path(args.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已落盘: {args.md}")
    if args.json_out:
        slim = [{k: v for k, v in f.items() if k != "scored"} for f in res["factors"]]
        Path(args.json_out).write_text(json.dumps({"meta": {"theta": args.theta},
                                                   "factors": slim}, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"已落盘: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
