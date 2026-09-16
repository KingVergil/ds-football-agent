#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选因子库：归一化去重 + 全窗口重估（0 LLM）。

为什么需要它
============
挖掘是**按天**做的，LLM 报的 evidence 只覆盖它那一天（7~53 腿），噪声极大。
但 `cond` 是机器可读的 —— 所以：

  1. **去重**：按 `(轴, 归一化 cond)` 合并同名/异名同条件（比"名字相似度"硬得多）；
  2. **重估**：把每条唯一条件放到**整个窗口**上用引擎重算 n / 命中 / d_pp / ā / y；
  3. **分档**：n 够的进候选库，n 不够的降级观察。

这样"归纳/去重"里确定性的一半就自动做完了，LLM 只剩语义判重（同义不同写法）。

用法
    python3 -m scripts.axis_library --days 2026-06-28,... --tag w15 --md docs/factor_mine_library.md
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_cond_audit import _stat, build_eval_env, eval_cond   # noqa: E402
from scripts.axis_two_stage_backtest import load_legs                 # noqa: E402
from src.beidan_axis import LEG_LINE4                                 # noqa: E402

PEER_FIELDS = ("rank_mp", "rank_od", "rank_disp", "disp.h", "disp.d", "disp.a",
               "disp.ds.h", "disp.ds.d", "disp.ds.a", "ah.h0", "ah.a0", "eu.h", "eu.d", "eu.a")


def norm_cond(cond: str) -> str:
    """归一化：统一比较符与分隔符、去空格、and 内部排序（用于确定性去重）。"""
    c = (cond or "").strip()
    c = c.replace("≤", "<=").replace("≥", ">=").replace("＜", "<").replace("＞", ">")
    c = re.sub(r"\s+", "", c)
    # 数值写法归一：1.10 -> 1.1、3.50 -> 3.5（字段名里的数字不受影响：需"数字.数字"模式）
    c = re.sub(r"(\d+\.\d*?)0+(?=\D|$)", r"\1", c)
    groups = []
    for or_part in re.split(r"或", c):
        terms = sorted(t for t in re.split(r"且", or_part) if t)
        groups.append("且".join(terms))
    return "或".join(sorted(groups))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", required=True)
    ap.add_argument("--in", dest="src", default="data/factor_mine_out")
    ap.add_argument("--tag", default="lib")
    ap.add_argument("--out", default="data/factor_mine_out")
    ap.add_argument("--md", default="")
    ap.add_argument("--min-n", type=int, default=15)
    args = ap.parse_args(argv)

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    legs = [l for l in load_legs("latest") if l["day"] in set(days)]
    env = build_eval_env(legs)

    raw = []
    for p in sorted(glob.glob(f"{args.src}/*.json")):
        stem = Path(p).stem
        if stem.startswith("merge_") or stem.startswith("library_") or "archive" in p:
            continue
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            continue
        day = (d.get("days") or [None])[0]
        if day not in days:
            continue
        for f in d.get("factors") or []:
            raw.append((day, d.get("axis"), f))

    lib: dict = {}
    for day, axis, f in raw:
        cond = f.get("cond") or ""
        if not cond:
            continue
        key = (axis, norm_cond(cond))
        e = lib.setdefault(key, {"axis": axis, "cond": cond, "key": key[1],
                                 "names": set(), "days": set(), "slugs": set(),
                                 "desc": f.get("desc", ""), "expect": f.get("expect", "")})
        e["names"].add(f.get("name") or "")
        e["days"].add(day)
        for s in f.get("slugs") or []:
            e["slugs"].add(s)

    rows = []
    for e in lib.values():
        try:
            r = _stat([l for l in legs if eval_cond(e["cond"], l, env)])
        except Exception:                                    # noqa: BLE001
            r = {"n": -1}
        e.update({k: r.get(k) for k in ("n", "hit_n", "miss_n", "hit", "mp",
                                        "d_pp", "a_med", "y")})
        e["peer_dep"] = any(f in e["cond"] for f in PEER_FIELDS)
        rows.append(e)
    rows.sort(key=lambda x: (-(x["n"] or 0), -(abs(x["d_pp"] or 0))))

    keep = [r for r in rows if (r["n"] or 0) >= args.min_n]
    drop = [r for r in rows if (r["n"] or 0) < args.min_n]
    (Path(args.out) / f"library_{args.tag}.json").write_text(
        json.dumps([{**r, "names": sorted(r["names"]), "days": sorted(r["days"]),
                     "slugs": sorted(r["slugs"])} for r in rows],
                   ensure_ascii=False, indent=1), encoding="utf-8")

    L, A = [], lambda s: L.append(s)
    A(f"# 两轴候选因子库（{len(days)} 天 / {len(legs)} 腿 · 归一化去重 + 全窗口重估）")
    A("")
    A(f"- 原始产出 **{len(raw)} 条** → 归一化去重后 **{len(rows)} 条唯一条件**"
      f"（n≥{args.min_n} 保留 {len(keep)} 条，降到观察区 {len(drop)} 条）")
    A(f"- 重估口径：方向轴看 `d_pp = 命中率 − 市场p̂`；波动轴看 `ā = 开奖SP/赛前赔率` 中位；"
      f"腿级线 {LEG_LINE4:.5f}")
    A("- ⚠️ 标 ⚠️ 的条件依赖**同场三侧名次/比较**，而腿池只收了过 x 门的腿 ⇒ 重估会偏"
      "（引擎正式归因时必须用该场三侧全量）")
    A("")
    A("| 轴 | 条件 | 名字 | n | 命中−p̂ | ā | y | 4关线 | ⚠️ |")
    A("|---|---|---|---|---|---|---|---|---|")
    for r in keep:
        mark = "✅" if (r["y"] or 0) > LEG_LINE4 else "❌"
        A(f"| {r['axis'][:4]} | `{r['cond'][:42]}` | {'、'.join(sorted(r['names'])[:2])[:22]} | "
          f"{r['n']} | {(r['d_pp'] or 0):+.1f} | {(r['a_med'] or 0):.2f} | "
          f"{(r['y'] or 0):.3f} | {mark} | {'⚠️' if r['peer_dep'] else ''} |")
    A("")
    A(f"**观察区（n<{args.min_n}，不进库）**：{len(drop)} 条 —— 多为单天小样本条件。")
    A("")
    out = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text(out, encoding="utf-8")
        print(f"已写 {args.md}")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
