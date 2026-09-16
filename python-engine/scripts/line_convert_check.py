#!/usr/bin/env python3
"""让球线换算校验 + 用换算后的市场 p̂ 重跑分桶（回答：让球盘到底有没有边际）。

背景：北单是让球胜平负，但锐市场段里只有部分盘口有「平均欧盘胜/平/负(<line>)」
（主受让 gl>0 经常没有）→ 之前的实验只覆盖 ~74% 的场次，而且结论「让球盘无边际」
是建立在**较弱的锐市场参考**上的。本脚本用 `src/line_convert.py`（Poisson 换算）
把 Pinnacle 1X2 换算到北单的 goal_line，于是**每一场**都能算 x。

三部分：
  ① 精度：换算概率 vs 同盘口「平均欧盘」的真实值（只在两者都有的场次上比）
  ② 覆盖：能算 x 的场次从多少提升到多少（按让球线符号分）
  ③ 分桶：用换算后的 p̂ 重算 x 桶与 gl0/glN 的 y 与 CI → 门该不该只做 gl0
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_manager import MATCHES_DIR  # noqa: E402
from src.beidan_settlement import result_code_to_pick  # noqa: E402
from src.line_convert import convert_1x2_odds, convert_1x2_to_line  # noqa: E402
from scripts.mispricing_buckets import (  # noqa: E402
    FAIR_LINE_RE, PINNACLE_TRIPLE_RE, bucket_of, describe, fmt, verdict,
)
from src.pool_ledger import TAKEOUT  # noqa: E402

SIDES = ("H", "D", "A")


def devig(odds):
    inv = [1.0 / o for o in odds if o and o > 0]
    tot = sum(inv)
    return [v / tot for v in inv] if tot > 0 else []


def _fair_line(txt: str, line: float):
    for m in FAIR_LINE_RE.finditer(txt or ""):
        if abs(float(m.group(1)) - line) < 1e-9:
            o = [float(m.group(i)) for i in (2, 3, 4)]
            if min(o) > 0:
                return o
    return None


def _pinnacle(txt: str):
    trips = [t for t in PINNACLE_TRIPLE_RE.findall(txt or "")
             if all(float(x) > 1.0 for x in t)]
    return [float(x) for x in trips[-1]] if trips else None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="bc狗")
    ap.add_argument("--theta", type=float, default=1.10)
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)

    # 回放切片目录：用 DS_FET_TXT_ROOT 指到自己的 fet_txt dump
    fet_root = Path(os.environ.get("DS_FET_TXT_ROOT", "/path/to/fet_txt"))
    idx = {p.stem: p for p in fet_root.glob("*.txt")}
    tag_dir = None
    from src.data_manager import TAGS_DIR
    tag_dir = TAGS_DIR
    rows, cmp_rows, no_ref = [], [], 0
    covered_old = cov_new = 0
    gl_stat = {"0": [0, 0], "neg": [0, 0], "pos": [0, 0]}
    for p in sorted(MATCHES_DIR.glob("*.json")):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in (payload if isinstance(payload, list) else (payload.get("matches") or [])):
            lid = m.get("lota_id") or m.get("id")
            bi = m.get("beidan_info") or {}
            if not lid or not bi:
                continue
            o = [bi.get("home_odds"), bi.get("draw_odds"), bi.get("away_odds")]
            if not all(o):
                continue
            try:
                gl = float(bi.get("goal_line"))
            except (TypeError, ValueError):
                continue
            tp = tag_dir / f"{lid}.json"
            if not tp.exists():
                no_ref += 1
                continue
            secs = (json.loads(tp.read_text(encoding="utf-8")) or {}).get("sections") or {}
            fair = _fair_line(secs.get("fair-odds") or "", gl)
            pin = _pinnacle(secs.get("eu-odds-pinnacle") or "")
            p_line = devig(fair) if fair else None
            p_conv = convert_1x2_odds(pin[0], pin[1], pin[2], gl) if pin else None
            cls = "0" if abs(gl) < 1e-9 else ("neg" if gl < 0 else "pos")
            gl_stat[cls][0] += 1
            if p_line or p_conv:
                gl_stat[cls][1] += 1
            if p_line:
                covered_old += 1
            if p_line or p_conv:
                cov_new += 1
            # ① 精度对照（同盘口两者都有）
            if p_line and p_conv:
                cmp_rows.append({"gl": gl, "line": p_line, "conv": p_conv,
                                 "lid": lid, "src": "fair"})
            p_sharp = p_line or p_conv
            if not p_sharp:
                no_ref += 1
                continue
            sp = bi.get("spvalue")
            rows.append({"lid": lid, "date": str(m.get("match_time") or "")[:10],
                         "gl": gl, "x": {s: p_sharp[i] * float(o[i])
                                         for i, s in enumerate(SIDES)},
                         "p_sharp": dict(zip(SIDES, p_sharp)),
                         "sp": float(sp) if sp else None, "result": bi.get("result")})

    print("=" * 84)
    print("① 让球线换算精度（换算 vs 同盘口「平均欧盘」，两者都有的场次）")
    print("=" * 84)
    if cmp_rows:
        errs = [abs(a - b) for r in cmp_rows for a, b in zip(r["line"], r["conv"])]
        print(f"  对照场次 {len(cmp_rows)}；三路概率绝对误差：均值 {statistics.mean(errs):.4f} "
              f"中位 {statistics.median(errs):.4f} p90 {sorted(errs)[int(0.9*len(errs))]:.4f} "
              f"最大 {max(errs):.4f}")
        by = {}
        for r in cmp_rows:
            by.setdefault(int(r["gl"]), []).extend(
                [abs(a - b) for a, b in zip(r["line"], r["conv"])])
        for gl, e in sorted(by.items()):
            print(f"    line={gl:+d}: n={len(e)//3:>4} 场  平均误差 {statistics.mean(e):.4f}")
    else:
        print("  无对照样本")

    print("\n" + "=" * 84)
    print("② 覆盖率（能算 x 的场次）")
    print("=" * 84)
    tot = sum(v[0] for v in gl_stat.values())
    print(f"  总场次 {tot}（有北单赔率）")
    print(f"  只靠 line-matched「平均欧盘」：{covered_old} 场（{covered_old/max(tot,1):.0%}）")
    print(f"  加 Poisson 换算后：        {cov_new} 场（{cov_new/max(tot,1):.0%}）")
    for k, lab in (("0", "平手 gl=0"), ("neg", "主让 gl<0"), ("pos", "主受让 gl>0")):
        n, c = gl_stat[k]
        print(f"    {lab}: {c}/{n} = {c/max(n,1):.0%}")

    print("\n" + "=" * 84)
    print("③ 分桶（x 用「换算后」的市场 p̂；y = E[SP×1{中}] 实测）")
    print("=" * 84)
    ok = [r for r in rows if r["sp"] and result_code_to_pick(r["result"]) in SIDES]
    print(f"  可结算样本 {len(ok)} 场（有 result + SP）")

    def zs(sel, x_lo):
        out = []
        for r in sel:
            win = result_code_to_pick(r["result"])
            for s in SIDES:
                if r["x"][s] >= x_lo:
                    out.append((r["lid"], float(r["sp"]) if s == win else 0.0))
        return out

    for cls, sel in (("全部", lambda r: True),
                     ("gl=0", lambda r: abs(r["gl"]) < 1e-9),
                     ("gl≠0", lambda r: abs(r["gl"]) > 1e-9),
                     ("gl<0 主让", lambda r: r["gl"] < 0),
                     ("gl>0 主受让", lambda r: r["gl"] > 0)):
        rows_c = [r for r in ok if sel(r)]
        for theta in (1.0, 1.10, 1.20):
            pairs = zs(rows_c, theta)
            d = describe(pairs, f"{cls} x≥{theta:.2f}")
            if d.get("n", 0) >= 20:
                print("  " + fmt(d))

    if args.md:
        Path(args.md).write_text("# 让球线换算校验\n（见 stdout 表格）\n", encoding="utf-8")


if __name__ == "__main__":
    main()
