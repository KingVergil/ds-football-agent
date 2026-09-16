#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""北单数据缺口体检（0 LLM）：**逐日看"能不能分析"**，而不只是"有没有数据"。

分析一天需要三样同时齐：
  ① `data/matches/<day>.json` 里该场**北单三路赔率齐全**（home/draw/away > 0）
     —— 缺了会被 `_beidan_odds()` 直接排除（不再用 Pinnacle 兜底：兜底会让 x≡1.00 静默失效）
  ② 该场有**赛前切片**（fet_txt 任一档：pass_1_day/12/6/3/2_hours、live）
  ③ 该场有**赛果 + 开奖 SP**（matches 的 beidan_info / data/beidan / data/beidan_sp 三源并集）

用法
    python3 -m scripts.beidan_data_gaps --start 2026-08-01 --end 2026-09-10 \
        --md docs/beidan_data_gaps.md
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 回放切片（deepseek_lota 侧的 fet_txt dump）：用 DS_FET_TXT_ROOT 指到你自己的目录
FET_ROOT = Path(os.environ.get("DS_FET_TXT_ROOT", "/path/to/fet_txt"))


def _days(start: str, end: str) -> list[str]:
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _slice_ids() -> set:
    ids: set = set()
    if not FET_ROOT.exists():
        return ids
    for w in FET_ROOT.iterdir():
        if w.is_dir():
            for p in w.glob("*.txt"):
                ids.add(p.stem)
    return ids


def _settled(day: str) -> set:
    out: set = set()
    for d in ("data/matches", "data/beidan"):
        p = ROOT / d / f"{day}.json"
        if not p.exists():
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        for m in (raw if isinstance(raw, list) else raw.get("matches", [])):
            bi = m.get("beidan_info") or {}
            if bi.get("result") not in (None, "") and float(bi.get("spvalue") or 0) > 0:
                out.add(m.get("lota_id"))
    p = ROOT / "data" / "beidan_sp" / f"{day}.json"
    if p.exists():
        for lid, v in (json.loads(p.read_text(encoding="utf-8")) or {}).items():
            v = v or {}
            if v.get("result") not in (None, "") and float(v.get("spvalue") or 0) > 0:
                out.add(lid)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--md", default="")
    args = ap.parse_args(argv)

    slices = _slice_ids()
    rows, L = [], []
    A = L.append
    A("| 天 | 北单场次 | 三路赔率齐 | 有切片 | 有赛果SP | **可分析** | 判定 |")
    A("|---|---|---|---|---|---|---|")
    for day in _days(args.start, args.end):
        p = ROOT / "data" / "matches" / f"{day}.json"
        if not p.exists():
            A(f"| {day} | — | — | — | — | — | ⛔ 无 matches 文件 |")
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        ms = raw if isinstance(raw, list) else raw.get("matches", [])
        bd = [m for m in ms if m.get("beidan_number")]
        if not bd:
            A(f"| {day} | 0 | — | — | — | — | ⛔ 无北单场次 |")
            continue
        odds_ok = [m for m in bd
                   if all(float((m.get("beidan_info") or {}).get(k) or 0) > 0
                          for k in ("home_odds", "draw_odds", "away_odds"))]
        with_slice = [m for m in odds_ok if m["lota_id"] in slices]
        st = _settled(day)
        ready = [m for m in with_slice if m["lota_id"] in st]
        verdict = ("✅ 可分析" if ready else
                   "⚠️ 缺赛果/SP" if with_slice else
                   "⚠️ 缺切片" if odds_ok else "⛔ 三路赔率全缺")
        A(f"| {day} | {len(bd)} | {len(odds_ok)} | {len(with_slice)} | "
          f"{len([m for m in bd if m['lota_id'] in st])} | **{len(ready)}** | {verdict} |")
        rows.append((day, len(bd), len(odds_ok), len(with_slice), len(ready)))
    ok = [r for r in rows if r[4] > 0]
    A("")
    A(f"**汇总**：{len(rows)} 天有北单场次｜可分析 **{len(ok)}** 天｜"
      f"不可分析 {len(rows) - len(ok)} 天")
    bad = [r for r in rows if r[4] == 0]
    if bad:
        A("")
        A("**不可分析的天（原因）**")
        A("")
        for d, n, o, s, _r in bad:
            why = ("三路赔率全缺" if o == 0 else
                   "缺赛前切片" if s == 0 else "缺赛果/SP")
            A(f"- `{d}`：北单 {n} 场，三路赔率齐 {o}，有切片 {s} → **{why}**")
    out = "\n".join(L) + "\n"
    if args.md:
        Path(args.md).write_text(out, encoding="utf-8")
        print(f"已写 {args.md}")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
