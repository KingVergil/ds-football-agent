#!/usr/bin/env python3
"""赛前信号 → SP 缩水 → 腿级 ROI：找"赔率靠得住"的腿。

假设（2026-09-13）
────────────────
腿级命中率 32.7% 远高于打平线 20.9%，却整体亏钱，唯一原因是**开奖 SP 比下注时赔率低**
（实测中位折扣 0.89，且 x 越大折扣越狠）。所以只要能**赛前**筛掉"会缩水"的腿，
策略就可能转正。

本脚本检验三个**纯赛前**信号是否能预测"折扣小"（赔率靠得住）与正 ROI：
  1. `disp`      = 离散指数首行→末行最大相对变动（引擎 prematch_dispersion）
                   —— 变动小 = 盘口稳 = 赔率可能靠得住
  2. `n_stages`  = 该场可用快照档位数（1~3）—— 档位多 = 数据厚 = 定价更充分
  3. `odds_span` = 三侧北单赔率的相对离散（(max−min)/mean）—— 结构分散 vs 集中

判据：按信号分档后看 **平均折扣** 与 **腿级 ROI**，并要求跨样本内外一致（防过拟合）。

用法
───
    python3 -m scripts.sp_stability_audit --pool data/leg_pool_sig --md docs/sp_stability_audit.md
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path

RATE = 0.65


def load(pool_dir: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(Path(pool_dir) / "*.json"))):
        day = Path(f).stem
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            if not r.get("settled"):
                continue
            sp = float(r.get("sp") or 0)
            odds = float(r.get("beidan_odds") or 0)
            x = float(r.get("x") or 0)
            if sp <= 0 or odds <= 0 or x <= 0:
                continue
            oh, od, oa = (float(r.get(k) or 0) for k in ("odds_h", "odds_d", "odds_a"))
            span = None
            if min(oh, od, oa) > 0:
                m = (oh + od + oa) / 3
                span = (max(oh, od, oa) - min(oh, od, oa)) / m if m else None
            out.append({
                "day": day, "lota_id": r.get("lota_id"), "side": r.get("side"),
                "x": x, "odds": odds, "sp": sp, "ratio": sp / odds,
                "disp": (float(r["disp"]) if r.get("disp") is not None else None),
                "n_stages": int(r.get("n_stages") or 0),
                "span": span,
                "hit": str(r.get("actual") or "") == str(r.get("side") or ""),
            })
    return out


def stat(g: list[dict]) -> dict:
    n = len(g)
    if not n:
        return {"n": 0, "hit": 0.0, "ratio": 0.0, "roi": 0.0}
    hits = [l for l in g if l["hit"]]
    ret = sum(RATE * l["sp"] - 1 for l in hits) - (n - len(hits))
    return {
        "n": n, "hit": len(hits) / n,
        "ratio": statistics.median([l["ratio"] for l in g]),
        "roi": ret / n,
    }


def table(o: list[str], title: str, groups: list[tuple[str, list[dict]]]) -> None:
    o.append(f"### {title}")
    o.append("")
    o.append("| 分组 | 腿数 | 命中率 | 折扣(中位) | **腿级 ROI** |")
    o.append("|---|---|---|---|---|")
    for name, g in groups:
        s = stat(g)
        if not s["n"]:
            continue
        o.append(f"| {name} | {s['n']} | {s['hit']:.1%} | {s['ratio']:.3f} | **{s['roi']:+.1%}** |")
    o.append("")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="data/leg_pool_sig")
    ap.add_argument("--split", default="2026-08-08")
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)

    legs = load(args.pool)
    known = [l for l in legs if l["disp"] is not None]
    o = ["# 赛前稳定性 → SP 缩水 → 腿级 ROI", ""]
    o.append(f"- 腿池 `{args.pool}`：{len(legs)} 条已结算侧腿"
             f"（其中带 `disp` 信号的 {len(known)} 条）")
    o.append(f"- 口径：折扣 = 开奖 SP / 下注时北单赔率（<1 = 缩水）；腿级 ROI = 0.65×SP−1 / −1")
    o.append(f"- 样本内 ≤ {args.split}，样本外 > {args.split}")
    o.append("")

    # 1) disp 分档（越小越稳）
    if known:
        ds = sorted(l["disp"] for l in known)
        q1, q2 = ds[len(ds) // 3], ds[2 * len(ds) // 3]
        buckets = [("稳（disp 最低 1/3）", lambda l: l["disp"] <= q1),
                   ("中", lambda l: q1 < l["disp"] <= q2),
                   ("不稳（disp 最高 1/3）", lambda l: l["disp"] > q2)]
        o.append(f"（disp 三分位：{q1:.4f} / {q2:.4f}）")
        o.append("")
        table(o, "1. 按赛前离散变动 `disp` 分档", [(n, [l for l in known if f(l)]) for n, f in buckets])
        o.append("样本内外一致性检查：")
        o.append("")
        o.append("| 分组 | 样本内 n / 折扣 / ROI | 样本外 n / 折扣 / ROI |")
        o.append("|---|---|---|")
        for n, f in buckets:
            gi = [l for l in known if f(l) and l["day"] <= args.split]
            go = [l for l in known if f(l) and l["day"] > args.split]
            si, so = stat(gi), stat(go)
            o.append(f"| {n} | {si['n']} / {si['ratio']:.3f} / {si['roi']:+.1%} | "
                     f"{so['n']} / {so['ratio']:.3f} / {so['roi']:+.1%} |")
        o.append("")

    # 2) n_stages
    table(o, "2. 按可用快照档位数 `n_stages` 分档",
          [(f"{k} 档", [l for l in legs if l["n_stages"] == k]) for k in (1, 2, 3)])

    # 3) odds_span
    sp_legs = [l for l in legs if l["span"] is not None]
    if sp_legs:
        ss = sorted(l["span"] for l in sp_legs)
        m = ss[len(ss) // 2]
        table(o, "3. 按三侧赔率结构分散度 `odds_span` 分档",
              [("集中（≤中位）", [l for l in sp_legs if l["span"] <= m]),
               ("分散（>中位）", [l for l in sp_legs if l["span"] > m])])

    # 4) 组合信号：稳 + n_stages 高
    if known:
        combo = [l for l in known if l["disp"] <= q1 and l["n_stages"] >= 3]
        table(o, "4. 组合信号（disp 最低 1/3 且 3 档齐全）", [("稳+档位齐", combo)])

    # 5) 结论
    o.append("## 结论")
    o.append("")
    best = None
    for name, g in ([(n, [l for l in known if f(l)]) for n, f in buckets] if known else []):
        s = stat(g)
        if s["n"] >= 30 and (best is None or s["roi"] > best[1]):
            best = (name, s["roi"], s["n"])
    if best:
        o.append(f"- 三个 disp 分组里 ROI 最高：**{best[0]} → {best[1]:+.1%}（n={best[2]}）**")
    o.append("- 若所有分组的腿级 ROI 仍为负，则**赛前稳定性不足以救回被 SP 缩水吃掉的边际**，"
             "需要换数据面（或改用固定赔率市场）。")
    text = "\n".join(o)
    print(text)
    if args.md:
        Path(args.md).write_text(text + "\n", encoding="utf-8")
        print(f"\n已落盘: {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
