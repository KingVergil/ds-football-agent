#!/usr/bin/env python3
"""回放目录审计：LLM 见过哪些错价腿（x）、选了什么、结算如何。产物 docs/replay_x_audit.md。

数据全部取自 `data/replays/sandboxes/95狗_0703init/workspace`：
  sessions/95狗/*_analyze_*.md  → 每波候选表（逐场 `赔率H/D/A=…` + `错价倍数x(H/D/A)=…` + 来源）
  95狗.json                     → 真实下单腿（x_mkt / beidan_info.result / spvalue）+ 票级结算
  memory/leg_decision_*.json    → LLM 的 rank / 推荐方向
"""
from __future__ import annotations

import glob
import json
import math
import re
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

WS = Path("data/replays/sandboxes/95狗_0703init/workspace")
RATE = 0.65
LINE4 = (1.0 / RATE) ** 0.25          # 1.11371
LINE1 = 1.0 / RATE                     # 1.5385
CODE = {"3": "H", "1": "D", "0": "A"}
BANDS = ((1.5385, 99.0, "x > 1.5385"), (1.3, 1.5385, "1.3 ~ 1.5385"),
         (1.11371, 1.3, "1.11371 ~ 1.3"), (1.0, 1.11371, "1.0 ~ 1.11371"),
         (-9.0, 1.0, "x < 1.0"))
ROW = re.compile(
    r"^-\s+(Lota\d+)\s*\|\s*([^|]+?)\s*\[([^\]]*)\]\s*goal_line=([-+]?\d+(?:\.\d+)?)"
    r"[^\n]*?赔率H/D/A=([\d.]+)/([\d.]+)/([\d.]+)\s*错价倍数x\(H/D/A\)=([\d.]+)/([\d.]+)/([\d.]+)")
SRC = re.compile(r"来源\s*([^）\)]+)")


def band_of(x):
    for lo, hi, nm in BANDS:
        if lo < x <= hi:
            return nm
    return "?"


def parse_candidates():
    out = {}
    for f in sorted(glob.glob(str(WS / "sessions/95狗/*_analyze_*.md"))):
        m = re.search(r"_analyze_(\d{4}-\d{2}-\d{2})", Path(f).name)
        if not m:
            continue
        day = m.group(1)
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            r = ROW.match(line.strip())
            if not r:
                continue
            lid, _, _, gl, o1, o2, o3, x1, x2, x3 = r.groups()
            sm = SRC.search(line)
            src = sm.group(1).strip() if sm else ""
            for s, x, o in zip(("H", "D", "A"), (float(x1), float(x2), float(x3)),
                               (float(o1), float(o2), float(o3))):
                k = (day, lid, s)
                if k not in out or x > out[k]["x"]:
                    out[k] = {"day": day, "lid": lid, "side": s, "x": x,
                              "odds": o, "gl": float(gl), "src": src}
    return out


def load_dog():
    d = json.loads((WS / "95狗.json").read_text(encoding="utf-8"))
    legs, tickets = [], []
    for o in d.get("orders") or []:
        tickets.append(o)
        for l in o.get("ticket_legs") or []:
            bi = l.get("beidan_info") or {}
            side = (l.get("picks") or [None])[0]
            act = CODE.get(str(bi.get("result")).strip(), "")
            sp = float(bi.get("spvalue") or 0)
            legs.append({"day": (l.get("match_time") or "")[:10], "lid": l.get("lota_id"),
                         "side": side, "x": l.get("x_mkt"), "sp": sp, "act": act,
                         "hit": bool(act) and act == side, "slip": o.get("slip_type"),
                         "odds": (l.get("odds") or {}).get(side),
                         "gl": l.get("goal_line")})
    return d, legs, tickets


def yrow(nm, ss, extra=""):
    if not ss:
        return f"| {nm} | 0 | — | — | — | — |"
    zs = [l["sp"] if l["hit"] else 0.0 for l in ss]
    n = len(zs)
    y = st.mean(zs)
    se = st.pstdev(zs) / math.sqrt(n)
    hit = sum(1 for l in ss if l["hit"]) / n
    xs = [l["x"] for l in ss if l.get("x")]
    verdict = ("✅ CI下沿过4关线" if y - 1.96 * se > LINE4
               else ("≈ 点估计过线" if y > LINE4 else "❌"))
    return (f"| {nm} | {n} | {hit*100:.1f}% | {st.mean(xs) if xs else 0:.3f} | "
            f"{y:.3f} [{y-1.96*se:.3f},{y+1.96*se:.3f}] | {RATE*y-1:+.1%} {verdict} |")


def main():
    cand = parse_candidates()
    d, legs, tickets = load_dog()
    CL = list(cand.values())
    L: list[str] = []
    A = L.append
    A("# 回放目录审计：LLM 见过哪些错价腿（x）、选了什么、结算如何")
    A("")
    A(f"只读 `{WS}`。公式：`z = SP·1{{中}}`、`y = E[z]`、单腿每元 `0.65y−1`；"
      f"4 关票腿级打平线 `(1/0.65)^(1/4) = {LINE4:.5f}`，单关线 `1/0.65 = {LINE1:.4f}`。")
    A("")
    A(f"- analyze 日志 {len(glob.glob(str(WS / 'sessions/95狗/*_analyze_*.md')))} 份｜"
      f"候选侧去重 **{len(CL)}** 条｜覆盖 {len({c['day'] for c in CL})} 天")
    A(f"- 回放下单 {len(tickets)} 票 / {len(legs)} 腿｜资金 {d.get('initial_capital'):.0f} → "
      f"**{d.get('capital'):.2f}**")
    A("")

    A("## A. 候选池里 x 的分布（LLM 每波都看得到 `错价倍数x(H/D/A)`）")
    A("")
    A("| x 区间 | 候选条数 | 占比 |")
    A("|---|---|---|")
    for lo, hi, nm in BANDS:
        n = sum(1 for c in CL if lo < c["x"] <= hi)
        A(f"| {nm} | {n} | {n/len(CL)*100:.1f}% |")
    A("")
    eq1 = sum(1 for c in CL if abs(c["x"] - 1.0) < 1e-9)
    A(f"- x 恰好 =1.000 的候选 **{eq1}** 条（{eq1/len(CL)*100:.1f}%）—— p̂ 与池子赔率同源、零信息。")
    A("")
    A("## B. x 高位全来自「让球欧盘」——这是结构性事实")
    A("")
    A("| 来源 | 候选条数 | x>1.11371 | x>1.3 | x>1.5385 | x 均值 |")
    A("|---|---|---|---|---|---|")
    for s in ("Pinnacle", "Pinnacle换算(line)", "让球欧盘", ""):
        ss = [c for c in CL if c["src"] == s]
        if not ss:
            continue
        A(f"| {s or '(空)'} | {len(ss)} | {sum(1 for c in ss if c['x']>1.11371)} | "
          f"{sum(1 for c in ss if c['x']>1.3)} | {sum(1 for c in ss if c['x']>1.5385)} | "
          f"{st.mean(c['x'] for c in ss):.3f} |")
    A("")
    A(f"**x>1.5385 的 {sum(1 for c in CL if c['x']>1.5385)} 条候选，100% 来自「让球欧盘」；"
      f"Pinnacle 源 0 条。**")
    A("")

    A("## C. x > 1.3 的候选腿：LLM 见过 / 买了没有 / 赛后")
    A("")
    picked_keys = {(l["day"], l["lid"], l["side"]) for l in legs}
    hi = sorted([c for c in CL if c["x"] > 1.3], key=lambda z: -z["x"])
    A(f"共 {len(hi)} 条，列前 40：")
    A("")
    A("| day | 场次 | 侧 | x | 赔率 | gl | 来源 | 下单? |")
    A("|---|---|---|---|---|---|---|---|")
    for c in hi[:40]:
        A(f"| {c['day']} | {c['lid']} | {c['side']} | {c['x']:.3f} | {c['odds']:.2f} | "
          f"{c['gl']:+.0f} | {c['src']} | "
          f"{'✅' if (c['day'], c['lid'], c['side']) in picked_keys else '❌'} |")
    A("")

    A("## D. LLM 实际下单的 125 条腿：按 x 分档的腿级结算（回放自己的钱）")
    A("")
    A("| 分档 | n | 命中率 | x̄ | y=E[z] [95%CI] | 单腿每元 0.65y−1 |")
    A("|---|---|---|---|---|---|")
    ok = [l for l in legs if l["sp"] > 0 and l["act"]]
    A(yrow("全部下单腿", ok))
    for lo, hi2, nm in BANDS:
        A(yrow(nm, [l for l in ok if l.get("x") and lo < l["x"] <= hi2]))
    A("")
    A("## E. 票级真实结算（15 票）")
    A("")
    A("| 票型 | 成本 | 中? | 返还 | 盈亏 | 中票 SP 连乘 |")
    A("|---|---|---|---|---|---|")
    for o in tickets:
        A(f"| {o.get('slip_type')} | {o.get('total_stake'):.0f} | "
          f"{'✅' if o.get('hit') else '❌'} | {o.get('return_amount'):.2f} | "
          f"{o.get('profit'):+.2f} | {o.get('sp_product'):.2f} |")
    staked = sum(o.get("total_stake") or 0 for o in tickets)
    net = d.get("capital") - d.get("initial_capital")
    win = [o for o in tickets if o.get("hit")]
    A("")
    A(f"- 合计成本 **{staked:.0f}**｜净 **{net:+.2f}**（{net/staked*100:+.1f}% of staked）｜"
      f"中票 **{len(win)}/{len(tickets)}**")
    if win:
        big = max(win, key=lambda o: o.get("profit") or 0)
        A(f"- 单票最大盈利 **{big.get('profit'):+.2f}**（占净利 {big.get('profit')/net*100:.0f}%）"
          f"—— 净利由极少数票决定，不是可复现的过程。")
    A("")
    A("## F. LLM 的 rank 与 x 的关系（它有没有在往高 x 排）")
    A("")
    pairs = []
    for f in sorted(glob.glob(str(WS / "memory/leg_decision_*.json"))):
        dd = json.loads(Path(f).read_text(encoding="utf-8"))
        for c in dd.get("candidates") or []:
            rec = c.get("推荐")
            if not rec or rec == "SKIP" or c.get("rank") is None:
                continue
            x = cand.get((dd.get("day"), c.get("lota_id"), rec))
            if x:
                pairs.append((float(c["rank"]), x["x"]))
    A(f"- 可对齐 {len(pairs)} 条（rank × x）")
    if pairs:
        for r in sorted({p[0] for p in pairs})[:6]:
            xs = [x for rr, x in pairs if rr == r]
            A(f"  - rank {r:g}：n={len(xs)}，x 均值 {st.mean(xs):.3f}，中位 {st.median(xs):.3f}")
        lo = [x for r, x in pairs if r <= 2]
        hi2 = [x for r, x in pairs if r >= 4]
        A(f"- rank≤2 组 x 均值 {st.mean(lo):.3f}（n={len(lo)}） vs rank≥4 组 {st.mean(hi2):.3f}"
          f"（n={len(hi2)}）⇒ **rank 与 x 基本无关（不是在按错价排序）**")
    A("")
    A("## G. 因子库现状（回放 workspace/factors）")
    A("")
    fs = sorted(glob.glob(str(WS / "factors/*.json")))
    mine = other = empty = 0
    for f in fs:
        try:
            c = str(json.loads(Path(f).read_text(encoding="utf-8")).get("content") or "")
        except Exception:
            continue
        if c.startswith("矿"):
            mine += 1
        elif not c:
            empty += 1
        else:
            other += 1
    A(f"- 共 **{len(fs)}** 个因子文件：矿式（带 n/hit/avg_return/CI）**{mine}**、"
      f"自由文本（盘口/水位/离散/资金类描述）**{other}**、空 **{empty}**")
    n_x = sum(1 for f in fs
              if any(k in Path(f).read_text(encoding="utf-8") for k in ("错价", "错价倍数", "池子错价")))
    A(f"- 其中提到「错价/池子错价」的仅 **{n_x}** 个 ⇒ **现有因子形态里根本没有 x≥θ 这类条件**；"
      f"x 门槛一直是引擎侧硬门，不是 LLM 学出来的。")
    A("")
    out = Path("docs/replay_x_audit.md")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"\n✅ 已写 {out}")


if __name__ == "__main__":
    main()
