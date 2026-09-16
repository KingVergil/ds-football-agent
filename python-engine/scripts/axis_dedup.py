#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归纳第 2 步：语义判重 + 定轴 + 命名（LLM 一次调用），随后引擎复算（0 LLM）。

输入：`survivors_*.json`（已过拟合/留出两窗的候选条件）。
输出：
  - `dedup_<tag>.json`  LLM 的合并结果（最终因子库草案）
  - `dedup_<tag>.md`    引擎对每条合并条件的**复算表**（拟合窗口 / 留出窗口）

LLM 只做"哪些是同一个机制、叫什么名字、是选还是否决"，**数字全部由引擎给**。

用法
    python3 -m scripts.axis_dedup --tag w30 --days <拟合窗口> --out data/factor_mine_out
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_cond_audit import _stat, build_eval_env, eval_cond   # noqa: E402
from scripts.axis_two_stage_backtest import load_legs                 # noqa: E402
from src.axis_miner import SLUG_WHITELIST                             # noqa: E402
from src.beidan_axis import LEG_LINE4                                 # noqa: E402
from src.base_llm import BaseLLMProvider                              # noqa: E402

SYSTEM = """你是北单两轴因子的**归纳器**。输入是一批已经过「拟合窗口 / 留出窗口」两窗检验的候选条件。
你的任务：按**机制**合并同义条件，产出最终因子库草案。数字不用你算 —— 引擎会复算，你只负责
"哪些是同一个机制、叫什么、是选还是否决"。

## 硬约束

1. ⛔ 不许发明新条件：`cond` 只能是输入里某条的**原样**、或输入的**子集**（去掉若干 term）、
   或同字段同运算的阈值取输入里出现过的某个值。不许自造阈值/新字段/新运算。
2. ✅ 合并必须说清机制：同一 `merged_from` 里的条目要能用一句话说清"为什么是同一件事"。
3. ✅ 定轴只允许 directional / volatility 二者之一：
   - directional = 影响**命中率相对市场 p̂**（看 d_pp）
   - volatility = 影响**开奖SP/赛前赔率**（看 ā），不许声称命中率
4. ✅ 方向轴必须给 `role`：
   - `select` = 这类腿命中率**高于**市场（可以买）
   - `veto`   = 这类腿命中率**显著低于**市场（别买；当否决条件用）
   波动轴一律 `role: rank`（当排序键：`expect=+` 排前、`-` 排后）。
5. ✅ 名字 4~10 汉字，不含数字/空格/emoji；同一机制只允许一种写法。
6. ✅ 优先级：**留出窗口的证据 > 拟合窗口**；两个窗口符号相反的条目要进 `rejected`。
7. ✅ 同一条输入只能进一个因子（或进 rejected），不许重复使用。

## 输出（只输出 JSON）

{
  "factors": [
    {"name": "主队中低概率否决",
     "axis": "directional", "role": "veto", "expect": "-",
     "cond": "side_is == H 且 mp <= 0.40",
     "merged_from": [1, 3, 7],
     "why_merge": "都是『买主队但市场概率不高』这一类腿，留出窗口命中率都显著低于市场",
     "desc": "≤60 字：条件 + 作用，写给以后的分析 agent 看",
     "slugs": ["discrete-odds"],
     "falsified_if": "在 n≥50 的样本里 d_pp ≥ 0 时作废"}
  ],
  "rejected": [{"idx": 5, "why": "两窗符号相反 / 已被更强的条件覆盖"}],
  "notes": "≤200 字：这批因子里你看到的机制结构"
}

## cond 语法（只允许这些字段与运算）

cond := or_expr；or_expr := and_expr (" 或 " and_expr)*；and_expr := term (" 且 " term)*
term := field op value；op := == | != | < | <= | > | >=
field ∈ self|side_is|rank_mp|rank_od|rank_disp|mp|od|ah.water|ah.line|ah.dline|
        eu.move|disp|disp.ds|span|gl|x|gap|gs|ah.h0|ah.h1|ah.a0|ah.a1|ah.line0|ah.line1|
        eu.h|eu.d|eu.a|disp.h|disp.d|disp.a|disp.ds.h|disp.ds.d|disp.ds.a
value := 数字 或 H|D|A 或另一个字段名

## slugs 白名单

{slugs}
"""


def fmt_metric(axis: str, r: dict) -> str:
    if axis == "directional":
        return f"d={r.get('d_pp') or 0:+.1f}pp"
    return f"ā={(r.get('a_med') or 0):.2f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="w30")
    ap.add_argument("--days", required=True, help="拟合窗口的天（留出 = 其余全部）")
    ap.add_argument("--src", default="data/factor_mine_out")
    ap.add_argument("--temperature", type=float, default=0.2)
    args = ap.parse_args(argv)

    src = Path(args.src)
    surv = json.loads((src / f"survivors_{args.tag}.json").read_text(encoding="utf-8"))
    rowtxt, idx2 = [], {}
    for i, r in enumerate(surv, 1):
        idx2[i] = r
        names = "、".join((r.get("names") or [])[:2])
        rowtxt.append(f"{i} | {r['axis'][:4]} | {r['cond']} | "
                      f"拟合 n={r['fit']['n']} {fmt_metric(r['axis'], r['fit'])} | "
                      f"留出 n={r['hold']['n']} {fmt_metric(r['axis'], r['hold'])} | {names}")

    user = ("【候选条件（已过两窗检验）】\n"
            "序号 | 轴 | cond | 拟合窗口 | 留出窗口 | LLM 原来的名字\n"
            + "\n".join(rowtxt)
            + "\n\n【要求】\n"
              "1. 合并成最终因子库；每条给出 name/axis/role/cond/merged_from/why_merge/desc/slugs/falsified_if；\n"
              "2. 两窗符号相反、或样本太小的，进 rejected；\n"
              "3. 目标：方向轴 ≤ 6 条、波动轴 ≤ 5 条。宁少勿滥。\n")

    from src.providers.deepseek import DeepSeekProvider
    p = DeepSeekProvider(temperature=args.temperature)
    raw = p.call(SYSTEM.replace("{slugs}", ", ".join(SLUG_WHITELIST)),
                 [{"role": "user", "content": user}],
                 temperature=args.temperature,
                 response_format={"type": "json_object"})
    (src / f"dedup_{args.tag}.raw.md").write_text(raw or "", encoding="utf-8")
    clean = BaseLLMProvider.strip_thinking(raw or "")
    i, j = clean.find("{"), clean.rfind("}")
    data = json.loads(clean[i:j + 1]) if 0 <= i < j else {}
    (src / f"dedup_{args.tag}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # 引擎复算：LLM 给的 cond 在拟合/留出两窗重算
    fit_days = {d.strip() for d in args.days.split(",") if d.strip()}
    legs = load_legs("latest")
    fit = [l for l in legs if l["day"] in fit_days]
    hold = [l for l in legs if l["day"] not in fit_days]
    envf, envh = build_eval_env(fit), build_eval_env(hold)

    L, A = [], lambda s: L.append(s)
    A(f"# 因子库草案（{args.tag}）· LLM 归纳 + 引擎复算")
    A("")
    A(f"- 输入存活候选 {len(surv)} 条 → 合并为 **{len(data.get('factors') or [])} 条**，"
      f"rejected {len(data.get('rejected') or [])} 条")
    A(f"- 拟合窗口 {len(fit)} 腿｜留出窗口 {len(hold)} 腿｜腿级线 {LEG_LINE4:.5f}")
    A("")
    A("| # | 因子 | 轴 | 角色 | cond | 并入 | 拟合 n / 指标 | 留出 n / 指标 | 两窗同号 |")
    A("|---|---|---|---|---|---|---|---|---|")
    final = []
    for k, f in enumerate(data.get("factors") or [], 1):
        cond = f.get("cond") or ""
        try:
            rf = _stat([l for l in fit if eval_cond(cond, l, envf)])
            rh = _stat([l for l in hold if eval_cond(cond, l, envh)])
        except Exception as e:                               # noqa: BLE001
            A(f"| {k} | {f.get('name')} | {f.get('axis')} | {f.get('role')} | "
              f"`{cond}` | {len(f.get('merged_from') or [])} | ⛔ {str(e)[:30]} | — | — |")
            continue
        ax = f.get("axis")
        if ax == "directional":
            same = (rf.get("d_pp") or 0) * (rh.get("d_pp") or 0) > 0
        else:
            same = ((rf.get("a_med") or 0) - (rh.get("a_med") or 0)) ** 2 < 0.09
        A(f"| {k} | {f.get('name')} | {ax[:4]} | {f.get('role')} | `{cond}` | "
          f"{len(f.get('merged_from') or [])} | {rf.get('n',0)} / {fmt_metric(ax, rf)} | "
          f"{rh.get('n',0)} / {fmt_metric(ax, rh)} | {'✅' if same else '⚠️'} |")
        final.append({**f, "fit": rf, "hold": rh, "two_window_same_sign": same})
    A("")
    if data.get("notes"):
        A(f"**LLM 备注**：{data['notes']}")
        A("")
    if data.get("rejected"):
        A("**被拒（原序号 → 原因）**")
        A("")
        for r in data["rejected"]:
            A(f"- #{r.get('idx')}：{r.get('why')}")
        A("")
    (src / f"dedup_{args.tag}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (src / f"final_{args.tag}.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(L))
    print(f"已写 {src}/dedup_{args.tag}.{{json,md}} 与 final_{args.tag}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
