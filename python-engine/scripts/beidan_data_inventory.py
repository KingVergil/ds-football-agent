#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""北单测试数据盘点（只读，0 LLM）。

回答两个问题：
  1. 现在到底有多少可用的北单测试数据（场次 / 天数 / 腿数 / 已建池 / 已被消耗）。
  2. 按 batch = 20 场跑一阶 LLM 分析，要多少批、多少 token、多少钱。

口径
----
- 可回放场次 = 同时满足 (1) 有赛前切片（fet_txt/pass_* 或 live）
  (2) 有赛果 + 开奖 SP（data/matches 的 beidan_info、data/beidan、data/beidan_sp 三源并集）。
  只有两条都齐，才能重建「赛前 x/赔率」并结算「开奖 SP」。
- 腿数 = 场次 x 3 侧（H/D/A）；引擎建池时还会按 x 门限裁掉一部分。
- token 系数用实测值（见 docs/prompts/README.md 的真实 dump）。

用法
    python3 -m scripts.beidan_data_inventory --md docs/beidan_data_inventory.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 回放切片（deepseek_lota 侧的 fet_txt dump）：用 DS_FET_TXT_ROOT 指到你自己的目录
FET_ROOT = Path(os.environ.get("DS_FET_TXT_ROOT", "/path/to/fet_txt"))

# 实测：docs/prompts/llm_run_2026-08-15/01_stage1.md = 48711 字符 / 20 场
#       （23% 非 ASCII）约 23.4k tokens，即约 1.17k tokens/场（全量原文，不截断）
TOK_PER_MATCH = 1170.0
OUT_TOK_PER_BATCH = 2000.0
PRICE_IN = (0.5, 4.0)
PRICE_OUT = (2.0, 16.0)


def _match_days() -> dict:
    out: dict = {}
    for d in ("data/matches", "data/beidan"):
        for p in sorted((ROOT / d).glob("*.json")):
            raw = json.loads(p.read_text(encoding="utf-8"))
            ms = raw if isinstance(raw, list) else raw.get("matches", [])
            bucket = out.setdefault(p.stem, {})
            for m in ms:
                if m.get("beidan_number") and m.get("lota_id"):
                    bucket.setdefault(m["lota_id"], m)
    return {k: list(v.values()) for k, v in out.items()}


def _slice_ids() -> set:
    ids: set = set()
    if not FET_ROOT.exists():
        return ids
    for w in FET_ROOT.iterdir():
        if w.is_dir():
            for p in w.glob("*.txt"):
                ids.add(p.stem)
    return ids


def _settled_ids(day: str) -> dict:
    out: dict = {}
    for d in ("data/matches", "data/beidan"):
        p = ROOT / d / f"{day}.json"
        if not p.exists():
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        ms = raw if isinstance(raw, list) else raw.get("matches", [])
        for m in ms:
            bi = m.get("beidan_info") or {}
            if bi.get("result") not in (None, "") and float(bi.get("spvalue") or 0) > 0:
                out[m.get("lota_id")] = bi
    p = ROOT / "data" / "beidan_sp" / f"{day}.json"
    if p.exists():
        for lid, v in (json.loads(p.read_text(encoding="utf-8")) or {}).items():
            v = v or {}
            if v.get("result") not in (None, "") and float(v.get("spvalue") or 0) > 0:
                out.setdefault(lid, v)
    return out


def _pool_stats() -> dict:
    out = {}
    for name in ("leg_pool_all", "leg_pool_full", "leg_pool_sig"):
        d = ROOT / "data" / name
        days, recs, uniq, settled = set(), 0, set(), 0
        for p in (sorted(d.glob("*.json")) if d.exists() else []):
            days.add(p.stem)
            for r in json.loads(p.read_text(encoding="utf-8")):
                recs += 1
                uniq.add((p.stem, r.get("lota_id"), r.get("side")))
                settled += 1 if r.get("settled") else 0
        out[name] = {"days": len(days), "records": recs,
                     "unique": len(uniq), "settled": settled}
    return out


PROMPT_DUMP = "docs/prompts/llm_run_2026-08-15/01_stage1.md"


def _prompt_breakdown() -> dict:
    """拆一份真实 stage1 dump（batch=20），量每场每段多少字符、压缩上限在哪。"""
    p = ROOT / PROMPT_DUMP
    if not p.exists():
        return {}
    t = p.read_text(encoding="utf-8")
    i = t.index("## USER")
    j = t.index("## RESPONSE")
    sys_t, usr = t[:i], t[i:j]
    blocks = [b for b in re.split(r"(?=^- Lota)", usr, flags=re.M)[1:] if b.strip()]
    if not blocks:
        return {}
    sections: dict = {}
    for b in blocks:
        sections.setdefault("[头部行 lota/赔率/x]", []).append(len(b.split("[section:", 1)[0]))
        for m in re.finditer(r"\[section:([a-z\-]+)\]\n(.*?)(?=\n\[section:|\Z)", b, re.S):
            sections.setdefault(m.group(1), []).append(len(m.group(2)))

    def _rows(body: str) -> list:
        return [L for L in body.splitlines() if L.strip()]

    series = {}
    for name in ("discrete-odds", "eu-odds-pinnacle", "asian-handicap-pinnacle",
                 "asian-handicap-crown"):
        full, samp, fl, nrow, n = [], [], [], [], 0
        for b in blocks:
            for m in re.finditer(r"\[section:([a-z\-]+)\]\n(.*?)(?=\n\[section:|\Z)", b, re.S):
                if m.group(1) != name:
                    continue
                ls = _rows(m.group(2))
                if not ls:
                    continue
                n += 1
                full.append(len(m.group(2)))
                nrow.append(len(ls))
                fl.append(len(ls[0]) + len(ls[-1]) + 2)
                peak = max(range(len(ls)),
                           key=lambda k: sum(float(x) for x in re.findall(r"\d+\.\d+", ls[k])) or 0)
                idx = sorted({0, len(ls) - 1, len(ls) - 2, len(ls) - 3, peak})
                samp.append(sum(len(ls[k]) + 1 for k in idx if k < len(ls)))
        if n:
            series[name] = {"n": n, "full": st.mean(full), "rows": st.mean(nrow),
                            "sample": st.mean(samp), "fl": st.mean(fl)}

    def _total(mode: str) -> float:
        tot = 0.0
        for b in blocks:
            tot += len(b.split("[section:", 1)[0])
            for m in re.finditer(r"\[section:([a-z\-]+)\]\n(.*?)(?=\n\[section:|\Z)", b, re.S):
                name, body = m.group(1), m.group(2)
                ls = _rows(body)
                if not ls:
                    continue
                if name == "betfair-buysell" and all("None" in L for L in ls):
                    continue
                if name not in series or mode == "full":
                    tot += sum(len(L) + 1 for L in ls)
                    continue
                if mode == "fl":
                    idx = sorted({0, len(ls) - 1})
                else:
                    peak = max(range(len(ls)),
                               key=lambda k: sum(float(x) for x in re.findall(r"\d+\.\d+", ls[k])) or 0)
                    idx = sorted({0, len(ls) - 1, len(ls) - 2, len(ls) - 3, peak})
                tot += sum(len(ls[k]) + 1 for k in idx if k < len(ls))
        return tot / len(blocks)

    head_waste = 0.0
    for b in blocks:
        m = re.search(r"赔率H/D/A=([^ ]+)", b.split("\n", 1)[0])
        if m:
            head_waste += max(0, len(m.group(1)) - 15)
    return {"n": len(blocks), "system": len(sys_t), "user": len(usr),
            "user_per": len(usr) / len(blocks), "sections": sections,
            "series": series,
            "chars": {"full": _total("full"), "sample": _total("sample"), "fl": _total("fl")},
            "head_waste": head_waste / len(blocks)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="docs/beidan_data_inventory.md")
    ap.add_argument("--batch", type=int, default=20)
    args = ap.parse_args(argv)

    mdays = _match_days()
    slices = _slice_ids()
    rows = []
    for day in sorted(mdays):
        ms = mdays[day]
        st_ids = _settled_ids(day)
        lids = {m["lota_id"] for m in ms}
        st_ids = {k: v for k, v in st_ids.items() if k in lids}
        with_slice = [m for m in ms if m["lota_id"] in slices]
        playable = [m for m in ms if m["lota_id"] in slices and m["lota_id"] in st_ids]
        rows.append({"day": day, "matches": len(ms), "slice": len(with_slice),
                     "settled": len(st_ids), "playable": len(playable)})

    use = [r for r in rows if r["playable"] > 0]
    by_month = defaultdict(Counter)
    for r in rows:
        m = r["day"][:7]
        by_month[m]["days"] += 1
        by_month[m]["matches"] += r["matches"]
        by_month[m]["slice"] += r["slice"]
        by_month[m]["playable"] += r["playable"]

    per_day = [r["playable"] for r in use]
    batches = sum(math.ceil(n / args.batch) for n in per_day)
    tok_in = batches * args.batch * TOK_PER_MATCH
    tok_out = batches * OUT_TOK_PER_BATCH
    pool = _pool_stats()
    lo_cost = tok_in / 1e6 * PRICE_IN[0] + tok_out / 1e6 * PRICE_OUT[0]
    hi_cost = tok_in / 1e6 * PRICE_IN[1] + tok_out / 1e6 * PRICE_OUT[1]

    L = []
    A = L.append
    A("# 北单测试数据盘点 + 一阶 LLM（batch=20）成本核算")
    A("")
    A("> 只读脚本 `scripts/beidan_data_inventory.py` 生成，0 LLM。"
      "口径 = 有赛前切片 **且** 有赛果+SP。")
    A("")
    A("## 1. 原始数据面")
    A("")
    A("| 项 | 数量 |")
    A("|---|---|")
    A(f"| 覆盖天（`data/matches` + `data/beidan`） | {len(rows)}（{rows[0]['day']} → {rows[-1]['day']}） |")
    A(f"| 北单场次（去重 lota_id） | {sum(r['matches'] for r in rows)} |")
    A(f"| 有赛前切片的场次 | {sum(r['slice'] for r in rows)} |")
    A(f"| 有赛果 + SP 的场次 | {sum(r['settled'] for r in rows)} |")
    A(f"| **可回放场次（两者齐）** | **{sum(per_day)}** |")
    A(f"| **可回放天数** | **{len(use)}** |")
    A(f"| 可回放腿数上限（x3 侧） | {sum(per_day) * 3} |")
    A("")
    A("| 月份 | 天 | 北单场次 | 有切片 | **可回放场次** | 上限腿数(x3) |")
    A("|---|---|---|---|---|---|")
    for m in sorted(by_month):
        c = by_month[m]
        A(f"| {m} | {c['days']} | {c['matches']} | {c['slice']} | **{c['playable']}** | "
          f"{c['playable'] * 3} |")
    A("")
    A(f"- 每日可回放场次：最少 {min(per_day)}｜中位 {st.median(per_day):.0f}｜"
      f"均值 {st.mean(per_day):.1f}｜最多 {max(per_day)}")
    A(f"- 场次规模分布（桶宽 20）："
      f"{dict(sorted(Counter(min(n // 20 * 20, 180) for n in per_day).items()))}")
    A("")

    A("## 2. 已建腿池 & 已消耗")
    A("")
    A("| 池 | 天 | 记录数 | 唯一腿(day,lid,side) | 已结算 |")
    A("|---|---|---|---|---|")
    for k, v in pool.items():
        A(f"| `data/{k}` | {v['days']} | {v['records']} | {v['unique']} | {v['settled']} |")
    A("")
    A("- 唯一腿 = 同一条腿在多个波次各落一条记录后的去重结果（统计时必须先去重）。")
    A("- 已跑过的回放：`data/replays/sandboxes/95狗_0801` = 08-01 → 08-22，"
      "10 票 / 81 腿，占可用腿不到 3%。")
    A("")

    A("## 3. 一阶 LLM（batch = 20 场）成本核算")
    A("")
    A("实测系数：`docs/prompts/llm_run_2026-08-15/01_stage1.md` = 48,711 字符 / 20 场，"
      f"约 23.4k tokens（全量原文不截断）≈ {TOK_PER_MATCH:.0f} tokens/场。")
    A("")
    A("| 项 | 数值 |")
    A("|---|---|")
    A(f"| 可回放天数 | {len(use)} |")
    A(f"| 一阶调用次数（batch={args.batch}） | **{batches}**（均值 {batches/len(use):.1f} 次/天） |")
    A(f"| 输入 tokens | {tok_in/1e6:.2f} M |")
    A(f"| 输出 tokens（每批约 {OUT_TOK_PER_BATCH:.0f} 估） | {tok_out/1e6:.2f} M |")
    A(f"| 成本（{PRICE_IN[0]}~{PRICE_IN[1]} 元/M 入） | {tok_in/1e6*PRICE_IN[0]:.1f} ~ "
      f"{tok_in/1e6*PRICE_IN[1]:.1f} 元 |")
    A(f"| 成本（{PRICE_OUT[0]}~{PRICE_OUT[1]} 元/M 出） | {tok_out/1e6*PRICE_OUT[0]:.1f} ~ "
      f"{tok_out/1e6*PRICE_OUT[1]:.1f} 元 |")
    A(f"| **一阶总成本（全量 {len(use)} 天）** | **{lo_cost:.1f} ~ {hi_cost:.1f} 元** |")
    A("")
    A(f"参考：120 元预算下按最贵档也只占 {hi_cost/120:.1%}，钱不是瓶颈；"
      f"瓶颈是墙钟时间（每批 30~90s，串行 {batches*0.5/60:.1f}~{batches*1.5/60:.1f} 小时，可并发）。")
    A("")

    A("## 4. 按天的批次数（最后 20 个可回放天）")
    A("")
    A(f"| 天 | 北单场次 | 可回放 | batch={args.batch} 批数 |")
    A("|---|---|---|---|")
    for r in use[-20:]:
        A(f"| {r['day']} | {r['matches']} | {r['playable']} | "
          f"{math.ceil(r['playable']/args.batch)} |")
    A("")

    A("## 5. 一阶批次里该放什么（两轴用得到的输入）")
    A("")
    A("| 两轴需要的量 | 来源 | 是否在 `tags` 段落里 | 谁算 |")
    A("|---|---|---|---|")
    A("| 三侧北单赔率 → `span`（波动轴排序键） | 北单赔率 | 引擎已有（候选行） | 引擎（确定性） |")
    A("| x = 市场 p̂ × 北单赔率（价格轴门） | Pinnacle 1X2 | 引擎已有 | 引擎（确定性） |")
    A("| 命中率 − 市场 p̂（方向轴） | 开奖结果 | — （只用历史腿池标定） | 引擎（确定性） |")
    A("| 亚盘首末行 / 水位 / 升退盘 | `asian-handicap-crown` | 是 | LLM 可读，引擎也能解析 |")
    A("| 离散指数首末行 | `discrete-odds` | 是 | 同上 |")
    A("| 欧赔首末行 | `eu-odds-pinnacle` | 是 | 同上 |")
    A("| 实力差 / 预期进球 | `fair-odds` | 是 | 同上 |")
    A("| 阵容 / 伤停 / 联赛语境 | `lineup` / `match-history`（覆盖不齐） | 部分 | **只有 LLM 能读** |")
    A("")
    A("⇒ 一阶 LLM 的增量只可能来自**规则表覆盖不到的那几行**（阵容、语境、组合形态），"
      "其余部分引擎能 0 成本算完。但注意第 6 节的实测：这些行**不便宜**"
      "（`lineup` 558 字符、`match-history` 704 字符，且覆盖率只有一半），")
    A("   想省 token 只能从时间序列的中间行里省 —— 而中间行正是形态信息的载体。")
    A("")

    pb = _prompt_breakdown()
    if pb:
        A("## 6. 现有 stage1 prompt 的真实构成与压缩上限（实测）")
        A("")
        A(f"样本：`{PROMPT_DUMP}`（batch={pb['n']} 场，SYSTEM {pb['system']} 字符 / "
          f"USER {pb['user']} 字符）。")
        A("")
        A("| 段 | 字符/场 | 占比 | 平均行数/场 |")
        A("|---|---|---|---|")
        tot = sum(st.mean(v) for v in pb["sections"].values())
        for k, v in sorted(pb["sections"].items(), key=lambda x: -st.mean(x[1])):
            if st.mean(v) < 20:
                continue
            rows = pb["series"].get(k, {}).get("rows")
            A(f"| `{k}` | {st.mean(v):.0f} | {st.mean(v)/tot:.0%} | "
              f"{('%.1f' % rows) if rows else '—'} |")
        A("")
        A("| 时间序列 | 全序字符/场 | 首+末3+极值 | 只留首末 |")
        A("|---|---|---|---|")
        for k, v in pb["series"].items():
            A(f"| `{k}` | {v['full']:.0f}（{v['rows']:.1f} 行） | "
              f"{v['sample']:.0f}（省 {1-v['sample']/v['full']:.0%}） | "
              f"{v['fl']:.0f}（省 {1-v['fl']/v['full']:.0%}） |")
        A("")
        c = pb["chars"]
        A("**每场字符数（同一份 dump，三档口径）**")
        A("")
        A("| 口径 | 字符/场 | ≈tokens/场 | 省 | 丢掉的信息 |")
        A("|---|---|---|---|---|")
        A(f"| ① 现状（三序列全序） | {c['full']:.0f} | {c['full']*0.48:.0f} | — | — |")
        A(f"| ② 首+末3+极值行 | {c['sample']:.0f} | {c['sample']*0.48:.0f} | "
          f"{1-c['sample']/c['full']:.0%} | 中段低频形态（折返、跳变速度） |")
        A(f"| ③ 只留首末行 | {c['fl']:.0f} | {c['fl']*0.48:.0f} | "
          f"{1-c['fl']/c['full']:.0%} | 全部中段路径，只剩首末差 |")
        A("")
        A(f"另有两笔**不损失信息**的纯浪费：头部行赔率全精度（{pb['head_waste']:.0f} 字符/场）、"
          "`betfair-buysell` 全 `None` 段（约 85 字符/场）。")
        A("")
        A("⇒ 结论：**能压到 600~800 tokens/场 的唯一来源就是删三段时间序列的中间行**"
          "（离散 27 行、欧赔 8 行、亚盘 7 行合计占 61% 字符）。判据层面（首末差 / 当前水位 / "
          "盘口是否不动）不丢；**形态层面（单调性、折返、极值时刻）会全丢**。"
          "要保住形态，就得走 ②（约 510 tokens/场），或让 LLM 按需回读某一场的全序。")
        A("")

    text = "\n".join(L) + "\n"
    Path(args.md).write_text(text, encoding="utf-8")
    print(text)
    print(f"已写 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
