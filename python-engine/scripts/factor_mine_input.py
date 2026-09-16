#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「两轴因子挖掘」的 prompt 输入（只读 + 落盘纯文本，0 LLM）。

产出 `data/factor_mine_input/<axis>_grp<i>.md`：每份 = 基线段 + 一行一腿的样本段，
可以直接贴进 `docs/prompts/factor_produce_two_axis.md` 的 USER 模板。

口径与 `scripts/axis_two_stage_backtest.py` 一致：腿池按 (day, lota_id, side) 去重，
特征来自 `data/tags/<lota_id>.json`（只看赛前快照；sp/hit/z 只作为标签出现）。

用法
    python3 -m scripts.factor_mine_input                     # 默认前 15 个可回放天
    python3 -m scripts.factor_mine_input --days 2026-06-28,2026-07-02 --group-size 5
"""
from __future__ import annotations

import argparse
import statistics as st
from pathlib import Path

from scripts.axis_two_stage_backtest import load_legs
from src.axis_miner import baseline_block, leg_line  # 单一来源（src/axis_miner.py）


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="", help="逗号分隔；空 = 取腿池最早的 15 天")
    ap.add_argument("--group-size", type=int, default=5)
    ap.add_argument("--out", default="data/factor_mine_input")
    args = ap.parse_args(argv)

    legs = load_legs("latest")
    all_days = sorted({l["day"] for l in legs})
    days = ([d.strip() for d in args.days.split(",") if d.strip()] if args.days
            else all_days[:15])
    sub = [l for l in legs if l["day"] in set(days)]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"窗口 {len(days)} 天：{days[0]} → {days[-1]}｜腿数 {len(sub)}")
    base = baseline_block(sub)
    print("基线：\n" + base + "\n")

    for i in range(0, len(days), args.group_size):
        grp = days[i:i + args.group_size]
        glegs = [l for l in sub if l["day"] in set(grp)]
        lines = [leg_line(l) for l in glegs]
        body = (f"【本轮任务】\n轴={{AXIS}}｜worker={{WORKER_ID}}｜窗口={grp[0]}~{grp[-1]}"
                f"（共 {len(grp)} 天）\n\n【基线】\n{baseline_block(glegs)}\n\n"
                f"【样本：一行一腿（{len(glegs)} 条）】\n" + "\n".join(lines) + "\n")
        for axis in ("directional", "volatility"):
            p = out / f"{axis}_grp{i // args.group_size + 1}.md"
            p.write_text(body.replace("{AXIS}", axis).replace("{WORKER_ID}",
                                                             f"{axis[:4]}_g{i // args.group_size + 1}"),
                          encoding="utf-8")
        chars = len(body)
        print(f"grp{i // args.group_size + 1} {grp[0]}~{grp[-1]}：{len(glegs)} 腿｜"
              f"{chars:,} 字符 ≈ {chars * 0.48 / 1000:.1f}k tokens"
              f"｜单腿 {chars / max(1, len(glegs)):.0f} 字符")
    print(f"\n已写 {out}（每组合两轴各一份，共 {2 * ((len(days) + args.group_size - 1) // args.group_size)} 份）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
