#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两轴「分析」：引擎标注 + LLM 组票（一次调用），跑某一天。

与 `axis_apply.py` 的分工：
  - `axis_apply` 只做引擎侧（逐腿 veto/select/波动档）
  - 本脚本把引擎标注拼进分析 prompt，让 LLM 在这批**已经算好结构**的腿上组票，
    并要求它：① 只用清单里的因子名 ② 明确写出每张票的理由 ③ 遵守 veto。

用法
    python3 -m scripts.axis_analyze --tag w30 --day 2026-08-22 --persona <path> --out data/factor_mine_out
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.axis_apply import apply_day, load_factors, prompt_block   # noqa: E402
from scripts.axis_two_stage_backtest import load_legs                  # noqa: E402
from src.base_llm import BaseLLMProvider                               # noqa: E402

SYSTEM = """你是北单串关的分析 agent（沙箱实验版）。你的任务：在引擎已经算好的候选里**组票**。

## 引擎已经算好的东西（不要重算、不要推翻）

1. **x 门**：`x = 市场p̂ × 北单赔率`，只有 x ≥ 1.11371 的腿进候选。x 是**价格轴**，不代表方向。
2. **方向轴**：引擎用历史两窗验证过的结构条件，标出
   - 🛑 **否决腿**：这类结构下命中率显著低于市场报价 ⇒ **不许进票**；
   - ✅ **加成腿**：这类结构下命中率高于市场报价 ⇒ 优先。
3. **波动轴**：标出该腿赛前赔率能不能兑现（ā = 开奖SP/赛前赔率）。
   波动档**只用来排序**（ā 高 = 该腿赔率靠得住），**不许用它判断方向**。

## 你要做的三件事

1. **组票**：从通过筛选的候选里挑腿，组成 4 关票（`M过4`）。一天可以有 1~3 张票。
2. **排序理由**：每张票写一句话，说明为什么这几条腿连乘合适（引用你用的因子名）。
3. **信息面否决**（可选）：只允许基于**赛前信息**的否决（例如数据段缺失、盘口异常）。
   不许用"我觉得这场会赢"这类主观理由；不许否决只是因为"x 不够高"。

## 硬约束

- 每张票 4 条腿、**同一场比赛只能出现一次**（同场多侧互斥，同时选等于自废一关）。
- 🛑 否决腿一律不许出现在票里。
- 只输出 JSON，不要解释文字。

## 输出

{
  "tickets": [
    {"legs": [{"lota_id": "Lota...", "side": "H", "reason": "…"}],
     "why": "≤60 字：为什么这四条腿连乘合适（引用因子名）"}
  ],
  "skipped": [{"lota_id": "Lota...", "why": "信息面否决的理由，没有就留空数组"}],
  "notes": "≤150 字：这批腿的整体结构你看到什么（方向 vs 波动的分布）"
}
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="w30")
    ap.add_argument("--src", default="data/factor_mine_out")
    ap.add_argument("--day", required=True)
    ap.add_argument("--persona", default="data/replays/sandboxes/95狗_0801/workspace/persona.md")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--out", default="data/factor_mine_out")
    args = ap.parse_args(argv)

    out = run_day(args.tag, args.day, args.src, args.persona, args.temperature, args.out)
    data, res = out["data"], out["res"]
    print(f"=== {args.day}｜候选 {len(res['rows'])}｜否决 {len(res['vetoed'])}｜"
          f"通过 {len(res['kept'])}｜LLM 出票 {len(data.get('tickets') or [])} 张")
    for k, t in enumerate(data.get("tickets") or [], 1):
        print(f"  票{k}: " + " / ".join(f"{l.get('side')}@{l.get('lota_id')}"
                                        for l in (t.get('legs') or [])))
        print(f"        {t.get('why')}")
        for l in t.get("legs") or []:
            print(f"        - {l.get('side')} {l.get('lota_id')}: {l.get('reason')}")
    print(f"  notes: {data.get('notes')}")
    return 0


def build_user(res: dict, persona: str) -> str:
    blk = prompt_block(res)
    cand = "\n".join(
        f"{r['lota_id']} {r['side']} 赔率={r['beidan_odds']:.2f} x={r['x']:.2f} "
        f"市场p̂={r['market_p']:.2f} span={r['span']:.2f}"
        for r in res["kept"][:40])
    return (f"【人设（策略来源）】\n{persona}\n\n{blk}\n\n"
            f"【通过筛选的可选腿（字段：lota_id 侧 赔率 x 市场p̂ span）】\n{cand}\n\n"
            f"【要求】组 1~3 张 4 关票；同场只用一次；不许用否决腿；按 JSON 输出。")


def run_day(tag: str, day: str, src: str = "data/factor_mine_out",
            persona_path: str = "", temperature: float = 0.2,
            out_dir: str = "data/factor_mine_out", legs_cache: list = None,
            provider=None) -> dict:
    """跑一天：引擎标注 → LLM 组票 → 落盘。返回 {res, data, raw}。线程安全（各写各的文件）。"""
    factors = load_factors(src, tag)
    legs = [l for l in (legs_cache if legs_cache is not None else load_legs("latest"))
            if l["day"] == day]
    res = apply_day(factors, legs, day)
    persona = ""
    p = Path(persona_path)
    if persona_path and p.exists():
        persona = p.read_text(encoding="utf-8")[:2500]
    user = build_user(res, persona)
    if provider is None:
        from src.providers.deepseek import DeepSeekProvider
        provider = DeepSeekProvider(temperature=temperature)
    raw = provider.call(SYSTEM, [{"role": "user", "content": user}],
                        temperature=temperature,
                        response_format={"type": "json_object"})
    out = Path(out_dir)
    (out / f"analyze_{day}.prompt.md").write_text(
        f"## SYSTEM\n{SYSTEM}\n\n## USER\n{user}\n", encoding="utf-8")
    (out / f"analyze_{day}.raw.md").write_text(raw or "", encoding="utf-8")
    clean = BaseLLMProvider.strip_thinking(raw or "")
    i, j = clean.find("{"), clean.rfind("}")
    data = json.loads(clean[i:j + 1]) if 0 <= i < j else {}
    (out / f"analyze_{day}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"res": res, "data": data, "raw": raw}


if __name__ == "__main__":
    raise SystemExit(main())
