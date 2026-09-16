"""两轴因子挖掘器（方向 / 波动）—— **首轮一次性** 与 **周期性** 共用同一套 prompt。

设计要点（见 docs/prompts/factor_produce_two_axis.md）：
  - 一个入口 `mine(axis, days, mode)`；首轮与周期只差 `mode` / 窗口 / 是否带现有因子库。
  - 判据口径固定：方向轴看 `d = 1{中} − mp`，波动轴看 `ā = 开奖SP/赛前赔率` 与 `span`；
    `y = E[SP·1{中}]` 只做最后验收，不参与挑因子。
  - 输出统一 JSON（factors[] / rejected / reflection），因子带机器可读 `cond`，
    归因与统计由引擎按 `cond` 求值 —— 这样"有票的周期"与"没票的首轮"才能共用一套。

本模块是**新增的独立链路**：单狗（`analyze`/`settle`/reflect）不 import 它，行为不变。

用法
    python3 -m src.axis_miner --axis both --days 2026-06-28 --tag day1
    python3 -m src.axis_miner --axis directional --days 2026-06-28,2026-07-02 --tag g1
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
from pathlib import Path

from .base_llm import BaseLLMProvider
from .store import _get_valid_section_slugs

ROOT = Path(__file__).resolve().parents[1]
SLUG_WHITELIST = sorted(_get_valid_section_slugs())

AXES = ("directional", "volatility")
MODES = ("initial", "periodic")


# ── 样本行 / 基线段（与 scripts/factor_mine_input.py 同源）───────────
def leg_line(l: dict) -> str:
    """一行一腿：`|` 前全是赛前字段，`|` 后是赛后标签（只能当标签用）。"""
    f = l.get("feats") or {}
    ah = f.get("ah_crown") or {}

    def _n(v, p=2):
        try:
            return f"{float(v):.{p}f}"
        except (TypeError, ValueError):
            return "na"

    try:
        gl = f"{float(l.get('goal_line') or 0):+.0f}"
    except (TypeError, ValueError):
        gl = "na"
    src = "P" if str(l.get("mkt_src") or "").startswith("Pinnacle") else "H"
    eu = f.get("eu1")
    disp, d0 = f.get("disp_last"), f.get("disp_first")
    return (
        f"{l['day'][5:]} {str(l['lota_id']).replace('Lota', '')} {l['side']} "
        f"mp={l['market_p']:.2f} od={l['beidan_odds']:.2f} span={l['span']:.2f} "
        f"x={l['x']:.2f} gl={gl} src={src} "
        f"| ah={_n(ah.get('h0'))}/{_n(ah.get('line0'))}/{_n(ah.get('a0'))}"
        f"->{_n(ah.get('h1'))}/{_n(ah.get('line1'))}/{_n(ah.get('a1'))} "
        f"| eu={'/'.join(f'{v:.2f}' for v in eu) if eu else '-'}"
        f"->{'-' if not f.get('eu_move') else '/'.join(f'{v:+.2f}' for v in f['eu_move'].values())} "
        f"| disp={'/'.join(f'{v:.1f}' for v in disp) if disp else '-'}"
        f" ds={'/'.join(f'{v:.1f}' for v in d0) if d0 else '-'} "
        f"| gap={f.get('strength_gap', '-')} gs={f.get('goals_sum', '-')} "
        f"| sp={l['sp']:.2f} hit={int(bool(l['hit']))} z={l['z']:.2f} "
        f"d={(1.0 if l['hit'] else 0.0) - l['market_p']:+.2f}"
    )


def baseline_block(legs: list, t1: float = 0.487, t2: float = 0.695) -> str:
    n = len(legs)
    hit = sum(1 for l in legs if l["hit"]) / n
    mp = st.mean(l["market_p"] for l in legs)
    ratios = [l["sp"] / l["beidan_odds"] for l in legs if l["beidan_odds"] > 0]
    y = st.mean(l["z"] for l in legs)
    tiers = []
    for lo, hi in ((0, t1), (t1, t2), (t2, 9)):
        g = [l for l in legs if lo < l["span"] <= hi]
        tiers.append(st.median([x["sp"] / x["beidan_odds"] for x in g]) if g else float("nan"))
    buckets = []
    for lo, hi in ((0, 0.25), (0.25, 0.40), (0.40, 1)):
        g = [l for l in legs if lo <= l["market_p"] < hi]
        buckets.append(((sum(1 for x in g if x["hit"]) / len(g)
                         - st.mean([x["market_p"] for x in g])) * 100) if g else float("nan"))
    return (
        f"全样本 n={n}｜命中率={hit:.1%}｜市场p̂均值={mp:.1%}｜方向边际 d 均值="
        f"{(hit - mp) * 100:+.2f}pp\n"
        f"ā 中位={st.median(ratios):.2f}｜y=E[SP·1{{中}}]={y:.3f}｜腿级线 1.11371\n"
        f"按 span 三档：T1(≤{t1}) ā={tiers[0]:.2f} / T2({t1}~{t2}) ā={tiers[1]:.2f} / "
        f"T3(>{t2}) ā={tiers[2]:.2f}\n"
        f"按 mp 三档：强侧(<0.25) d={buckets[0]:+.2f}pp / 中(0.25~0.40) d={buckets[1]:+.2f}pp / "
        f"热门(>0.40) d={buckets[2]:+.2f}pp"
    )


# ── prompt 模板（唯一来源；docs/prompts/factor_produce_two_axis.md 是它的说明书）──
SYSTEM = """你是北单串关的因子挖掘器。你看的是**已结算的历史腿**，任务是产出一批
「赛前可观测、能解释某一侧为什么被奖池错价」的候选因子。你不下单、不选票、不负责任何资金决策。

## 本轮只挖一个轴

{axis_block}

## 本轮是哪种挖掘

{mode_block}

## 判据口径（唯一标准，别自创）

腿的收益标签是 `z = 开奖SP × 1{{中}}`，`y = E[z]`；4 关票的**每腿打平线 = 1.11371**
（`(1/0.65)^(1/4)`，0.65 只在整票收一次）。但**两轴必须分开归因**：

- 方向轴判据：`d = 1{{中}} − mp`（命中率相对锐市场 p̂ 的边际，单位 pp）
- 波动轴判据：`ā = 开奖SP / 赛前赔率`（赔率保不保得住）与 `span`（三侧结构分散度）
- `y` 是两轴的乘积结果，**只能用来看一眼，不能拿来挑因子**。

样本里给你 `d`、`z`、`sp`，但它们只是**标签**：写因子条件时一个都不许用。

## 硬约束（违反即作废）

1. ⛔ **禁止用 x、赔率高低、SP 大小作为方向因子的条件**。那是波动轴的东西：
   实测「x 越大 ā 越差」（x≤1 → 0.95；门内 → 0.78；结构最分散档 → 0.61），
   按 x 排序选腿的方向边际是 −9 ~ −13pp。凡是形如「x 高」「高赔」「高 SP」的条件，
   一律归到波动轴，不许写成方向因子。
2. ⛔ **禁止把赛后信息写进条件**：`sp / hit / z / 比分 / 开奖码 / 「这场爆冷了」`
   只能出现在 evidence 里，不能出现在 cond 里。
3. ⛔ **禁止只看命中样本**。同一条件必须在「中」和「不中」两侧都有样本，
   并且要报出两边的计数。
4. ⛔ **禁止输出同义反复**（如「冷门侧容易爆冷」「强队容易赢」）。条件必须是可判定字段
   （mp / span / ah / eu / disp / gap / gs 及其首末差）。
5. ✅ **每条因子必须能回答三件事**：条件是什么（可判定）、作用在哪个轴、期望符号正还是负。
6. ✅ 样本数 <20 的条件必须标 `"low_sample": true`，别包装成发现。
7. ✅ 命名规范（为了后面自动去重）：**4~10 个汉字**，不含数字/空格/emoji/括号，
   结构 = 「可观测特征 + 作用方向」，例如 `冷门侧盘口不动`、`三侧赔率收敛`。
   同一含义只允许一种写法。
8. ⛔ `src=H`（让球欧盘换算）的腿只能当**对照组**：它们的市场 p̂ 是换算产物，
   已被证实不是真错价。因子条件**不许只在 `src=H` 的样本上成立**。
9. ⛔ **禁止把「宇宙定义」写成条件**：`gl == 0`（可下注场次的定义）、`src == P`、
   `x >= 1.11`（引擎的价格门）都不是因子 —— 它们对语料里的腿恒成立，没有筛选力。
10. ⛔ **方向轴禁止裸单侧条件**：只写 `side_is == H` 等于「买主队」，没有筛选力。
    方向因子的条件里必须至少有一个**结构字段**（mp/ah/eu/disp/gap/gs）与侧绑定。
11. ⛔ **样本地板**：`evidence.n < 8`，或 `hit_n < 2`，或 `miss_n < 2`，
    或对照组 `counter` 的 n < 5 —— 一律写进 `rejected`，不许放进 `factors`
    （n=4 全中这种不是发现，是噪声）。
12. ⛔ **`cond` 必须能被引擎求值**：只能用下方语法表里的字段与运算符
    （`且` / `或` / 比较式）。写不出合法 `cond` 的因子**放进 `rejected`**，
    不许在 `factors` 里写「无法求值」「需另计算」这类话。
13. ⛔ **方向轴的 `side` 必须可解析**：只能是 `H` / `D` / `A`，或可判定的规则
    （`最低mp侧` / `最高赔侧` / `gap正号侧` / `该侧`）。方向轴不许写「不指定」；
    「不指定」只属于波动轴。
14. ⛔ **方向因子必须能推出「每场唯一一侧」**。腿是"场次×侧"的样本行，
    但下单时一场只能买一侧，所以条件必须隐含一个选侧规则：
    用 `rank_mp == 1` / `rank_od == 3`（最低 mp / 最高赔侧）这类**名次条件**，
    或 `gap` 的正负号侧。**全局字段（span / gs / gl / x）配 `该侧` 不算选侧规则**
    —— 这类条件在三侧上同时成立，样本会被三侧稀释，也不能落到票上。
15. ✅ `ah.water` / `eu.move` / `disp` 是「我方」字段：`D` 侧没有亚盘水位，
    用 `ah.water` 的条件自动只覆盖 H/A 两侧；这不是错误，正常写即可。
16. ⛔ **单位统一**：`d_pp` 一律写**百分点**（+6.8 表示 +6.8pp，不要写 0.068）。
17. ✅ 名次字段（`rank_mp` / `rank_od` / `rank_disp`）由引擎按**该场三侧全量**计算（1 = 最低），
    你直接写 `rank_mp == 1` 即可，不要自己用「≥/≤ 三侧比较」绕。
18. ✅ 首末要显式选一个：`ah.line` = 末行让球线、`ah.line0` = 首行；
    水位同理 `ah.h1/a1`（末）与 `ah.h0/a0`（首）。别写「盘口浅」这种没有首末的说法。

## 已证伪清单（别再挖，挖了也会被剔除）

- 「离散凝聚」（单关狗旗舰策略）：在北单门内 `d` 只有 +0.1pp，无效。
- 「升盘」「主队水位走高」「欧赔上升且最凝聚」：`d` 为负（−0.3 ~ −2.5pp）。
- 「热门侧」：`d = −0.3pp`，但 `ā = 1.01`（赔率保得住）——属波动轴，别写成方向因子。
- 「x > 1.5385 的腿」：已证实是让球欧盘换算产物，不是真错价。

## 输出

只输出一个 JSON 对象，不要任何解释文字：

{{
  "axis": "{axis}", "mode": "{mode}", "worker": "{worker}",
  "days": {days_json}, "n_legs": <你实际看到的腿数>,
  "baseline": {{"n": <..>, "hit": <..>, "mp": <..>, "d_pp": <..>, "a_med": <..>, "y": <..>}},
  "factors": [
    {{"name": "冷门侧盘口不动",
      "desc": "≤60 字：条件 + 作用方向，写给未来分析 agent 看的一句话",
      "cond": "rank_mp == 1 且 ah.dline == 0",
      "side": "最低赔侧 / 指定侧 H|D|A / 不指定",
      "expect": "+",
      "hypothesis": "为什么可能成立的一句话机制",
      "revise_of": "",
      "evidence": {{"n": 46, "hit_n": 18, "miss_n": 28, "d_pp": 6.8, "a_med": 0.83, "y": 1.52,
                   "note": "相对基线的增量"}},
      "counter": "条件不成立时的 d 是多少（对照组）",
      "low_sample": false, "unverifiable": false,
      "slugs": ["asian-handicap-pinnacle", "discrete-odds"],
      "falsified_if": "在 n≥50 的样本里 d ≤ 0"}}
  ],
  "rejected": [{{"idea": "...", "why": "样本不足 / 与已证伪清单重合 / 条件是赛后信息",
                "suggest_retire": ""}}],
  "reflection": "≤200 字：这批样本里本轴最像什么"
}}

## cond 小语法（能求值才算数）

```
cond   := or_expr
or_expr:= and_expr (" 或 " and_expr)*
and_expr:= term (" 且 " term)*
term   := field op value
op     := == | != | < | <= | > | >=
```

字段（`self` = 本因子选中的那一侧，即 `side`）：

```
侧相关: self | side_is | rank_mp | rank_od      # rank_* 该侧在三侧里的名次，1 = 最低
       | mp | od | ah.water | ah.line | ah.dline # 我方 mp / 我方赔率 / 我方水位 / 我方让球线 / 盘口变化
       | eu.move                                  # 我方欧赔首→末相对变动（负数=下沉）
       | disp | disp.ds                           # 我方离散 末值 / 首值
全局  : span | gl | x | gap | gs
       | ah.h0|ah.h1|ah.a0|ah.a1|ah.line0|ah.line1
       | eu.h|eu.d|eu.a | disp.h|disp.d|disp.a | disp.ds.h|disp.ds.d|disp.ds.a
```

例：`rank_mp == 1 且 ah.dline == 0`、`mp <= 0.40 且 eu.move <= -0.05`、
`ah.water <= 0.91`（我方水位低）、`span <= 0.49`、`gs < 2.6`、
`side_is == H 或 side_is == A`（不含平局）。

## slugs 白名单（只能从里面选，只报该因子真正依赖的段）

{slugs}
"""

AXIS_BLOCK = {
    "directional": """### 轴 = 方向轴（directional）

你要回答的是：**什么赛前条件能让某一侧的命中率高于锐市场给的 p̂（d > 0）**。

可用信号（都是赛前可观测）：
- 市场结构：该侧 mp 的高低、三侧 mp 的排序、mp 是否被池子反向定价；
- 亚盘：水位（h1/a1）高低、让球线首→末变化、盘口是否不动；
- 离散：末行三侧分布、我方是否最发散 / 最凝聚、离散首→末变化；
- 欧赔：我方赔率首→末相对变化；
- 公平盘：实力差、预期进球和。

已知的粗先验（可作起点，但必须自己在样本里核数）：
冷门侧 +5.2pp｜最发散侧 +4.9｜主队水位走低 +4.9｜弱侧(mp<0.35) +3.7｜
小球(<2.6) +3.8｜欧赔下沉 +3.1｜盘口不动 +0.8｜最凝聚侧 +0.1｜热门侧 −0.3

⚠️ **`span` 是波动轴的排序键，不是方向字段**。方向轴上它的全样本经验是**低 span 更强**
（弱侧 mp<0.35：低 span 档 d≈+9pp、高 span 档 d≈+1.7pp），而不是反过来。
方向轴若确实要写结构条件，必须**与侧绑定**（如 `rank_mp == 1 且 span <= 0.49`），
并且给出「高 span 同条件」的反向对照；单独一条 `span >= x` 不许写成方向因子。

输出要求：`side` 必须写清「买哪一侧」（或写明判定规则），`expect` 用 `+` / `-`。
目标不是罗列条件，而是找到**能把 d 抬起来的组合**（单条 +2pp 以上才有意义）。""",
    "volatility": """### 轴 = 波动轴（volatility）

你要回答的是：**什么赛前条件能让赛前赔率更可能被兑现（ā 高），或者让收益尾部更厚**。
这一轴**不许**声称命中率提升 —— 命中率是方向轴的事。

可用信号：
- 结构集中度：`span`（三侧北单赔率分散度，越小越集中）、三侧赔率是否有明显冷门；
- 该侧赔率绝对高度（高赔 = 池子会砍得更狠）：实测冷门侧 ā=0.64、热门侧 ā=1.01；
- 亚盘水位与让球线的稳定性（走水快 = 临场变盘 = SP 缩水风险）；
- 离散首→末变化（分歧收敛 vs 扩大）；
- 欧赔首→末变化（价格已经动过的腿，开奖时更可能再动）。

已知的粗先验：span 三档 ā = 0.98 / 0.80 / 0.61（T1/T2/T3）；
按 x 降序取腿 ā=0.65（最差），x≤1 的腿 ā=0.95。

输出要求：`expect` 说明它对排序的影响（`+` = 应当排前，`-` = 应当排后）。
`side` 通常写「不指定」。因子要能直接回答：**这条腿的赔率值不值得连乘**。
`d_pp` 只作为附注（说明这条腿方向上也还行），**不能**当成波动轴的证据；
波动轴的证据是 `a_med`（对照基线）与 `n`。""",
}

MODE_BLOCK = {
    "initial": """### mode = initial（首轮）

你面对的是一个**空因子库**。目标是尽可能把候选条件铺开：
- 8~15 条，允许写到只是"看起来像"的条件（样本 n≥20 即可），每条必须带
  `hypothesis` 一句话说明它为什么可能成立（机制），供后续归纳时判断是否同源；
- 不要为了凑数把同一个条件写成多条（如「冷门侧低水」和「低水冷门侧」）。""",
    "periodic": """### mode = periodic（周期）

你面对的是**已有因子库**（见【现有因子库】段）。目标是**增量与修订**，不是重铺一遍：
- 只输出两类因子：① 库里没有的新条件；② 对库内已有因子的修订（阈值改、方向反、条件收窄）。
  修订用同一个 `name`，并在 `revise_of` 里写明要改哪条、改成什么；
- 已被库内因子覆盖的不要再写一遍；样本不足或已被证伪的写进 `rejected`；
- 3~8 条即可。库里某条因子在本窗口明显失效（n≥25 且 d 反号），写进 `rejected` 并标
  `suggest_retire`，由退役流处理，你不要自己删。""",
}


def build_system(axis: str, mode: str, worker: str, days: list, ) -> str:
    assert axis in AXES and mode in MODES
    return SYSTEM.format(
        axis_block=AXIS_BLOCK[axis], mode_block=MODE_BLOCK[mode],
        axis=axis, mode=mode, worker=worker,
        days_json=json.dumps(days, ensure_ascii=False),
        slugs=", ".join(SLUG_WHITELIST),
    )


def build_user(legs: list, days: list, axis: str, existing: list = None) -> str:
    head = (f"【本轮任务】\n轴={axis}｜worker={axis[:4]}_g1｜窗口={days[0]}~{days[-1]}"
            f"（共 {len(days)} 天）\n\n【基线】\n{baseline_block(legs)}\n")
    lib = ""
    if existing:
        lib = ("\n【现有因子库】（名称｜轴｜一句话条件）\n"
               + "\n".join(f"{n}｜{t}｜{d}" for n, t, d in existing) + "\n")
    body = "\n".join(leg_line(l) for l in legs)
    return (
        head + lib +
        f"\n【样本：一行一腿（{len(legs)} 条），`|` 前是赛前字段，`|` 后是赛后标签（只能当标签用）】\n"
        + body +
        "\n\n【要求】\n"
        "1. 先把样本按本轴判据排序，找出**分布两端**的条件，再写因子；\n"
        "2. 每条因子必须给出 n / hit_n / miss_n / d_pp / a_med / y 六个数字（自己从样本里数）；\n"
        "3. 至少给一条 rejected：你考虑过但放弃的条件 + 放弃原因；\n"
        "4. 因子 8~15 条（periodic 3~8 条），宁少勿滥；同义写法合并成一条。\n"
    )


def parse_json(text: str) -> dict | None:
    clean = BaseLLMProvider.strip_thinking(text or "")
    clean = re.sub(r"^```(?:json)?|```$", "", clean.strip(), flags=re.M).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        i, j = clean.find("{"), clean.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(clean[i:j + 1])
            except json.JSONDecodeError:
                return None
    return None


def mine(axis: str, legs: list, days: list, mode: str = "initial",
         existing: list = None, temperature: float = 0.6,
         provider=None) -> dict:
    """跑一次挖掘。返回 {system, user, raw, data}。"""
    from .providers.deepseek import DeepSeekProvider
    provider = provider or DeepSeekProvider(temperature=temperature)
    worker = f"{axis[:4]}_g1"
    system = build_system(axis, mode, worker, days)
    user = build_user(legs, days, axis, existing=existing)
    raw = provider.call(system, [{"role": "user", "content": user}],
                        temperature=temperature,
                        response_format={"type": "json_object"})
    return {"system": system, "user": user, "raw": raw, "data": parse_json(raw),
            "axis": axis, "mode": mode, "days": days, "n_legs": len(legs)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="both", choices=("directional", "volatility", "both"))
    ap.add_argument("--days", required=True, help="逗号分隔；空串=腿池前 5 天")
    ap.add_argument("--mode", default="initial", choices=MODES)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--out", default="data/factor_mine_out")
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--by-day", action="store_true",
                    help="每个 (天 × 轴) 一个独立 worker（并行挖掘用）")
    ap.add_argument("--chunk-legs", type=int, default=0,
                    help="按腿数切批（推荐 200）：整天累加到该腿数就开新批，每批独立 worker")
    ap.add_argument("--parallel", type=int, default=1, help="并发 worker 数")
    ap.add_argument("--stagger", type=float, default=2.0,
                    help="并发时每次提交的间隔秒数（避开同时 TLS 握手）")
    args = ap.parse_args(argv)

    from scripts.axis_two_stage_backtest import load_legs
    legs_all = load_legs("latest")
    days = [d.strip() for d in args.days.split(",") if d.strip()] or \
        sorted({l["day"] for l in legs_all})[:5]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    axes = AXES if args.axis == "both" else (args.axis,)

    # 任务清单：(窗口天元组, 轴)
    if args.chunk_legs > 0:
        cnt = {d: sum(1 for l in legs_all if l["day"] == d) for d in days}
        batches, cur, n = [], [], 0
        for d in days:
            if cur and n + cnt[d] > args.chunk_legs:
                batches.append(tuple(cur))
                cur, n = [], 0
            cur.append(d)
            n += cnt[d]
        if cur:
            batches.append(tuple(cur))
        print(f"按 {args.chunk_legs} 腿/批切出 {len(batches)} 批 × {len(axes)} 轴 = "
              f"{len(batches) * len(axes)} 次调用")
        for b in batches:
            print(f"  {b[0][5:]}~{b[-1][5:]}  {len(b)} 天  {sum(cnt[d] for d in b)} 腿")
        jobs = [(b, a) for b in batches for a in axes]
    elif args.by_day:
        jobs = [((d,), a) for d in days for a in axes]
    else:
        jobs = [(tuple(days), a) for a in axes]

    def _one(job):
        win, axis = list(job[0]), job[1]
        sub = [l for l in legs_all if l["day"] in set(win)]
        res = mine(axis, sub, win, mode=args.mode, temperature=args.temperature)
        span = f"{win[0].replace('-', '')}_{win[-1].replace('-', '')}" if len(win) > 1 \
            else win[0].replace("-", "")
        stem = f"{args.tag}_{span}_{axis}"
        (out / f"{stem}.prompt.md").write_text(
            f"## SYSTEM\n{res['system']}\n\n## USER\n{res['user']}\n", encoding="utf-8")
        (out / f"{stem}.raw.md").write_text(res["raw"] or "", encoding="utf-8")
        if res["data"]:
            (out / f"{stem}.json").write_text(
                json.dumps(res["data"], ensure_ascii=False, indent=1), encoding="utf-8")
        n_f = len((res["data"] or {}).get("factors") or [])
        return (f"[{win[0][5:]}~{win[-1][5:]} {axis[:4]}] 腿 {len(sub):3d}｜"
                f"解析 {'OK' if res['data'] else '失败'}｜因子 {n_f:2d} 条｜{len(res['user']):,} 字符")

    if args.parallel > 1 and len(jobs) > 1:
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _one_retry(job, tries: int = 3):
            for i in range(tries):
                try:
                    return _one(job)
                except Exception as e:                       # noqa: BLE001
                    if i == tries - 1:
                        raise
                    print(f"[retry {i + 1}] {(job[0] or 'win')[-5:]} {job[1][:4]}："
                          f"{str(e)[:70]}", flush=True)
                    time.sleep(15 * (i + 1))

        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            futs = {}
            for j in jobs:                       # 错开发起，避免同时 TLS 握手
                futs[ex.submit(_one_retry, j)] = j
                time.sleep(args.stagger)
            for fu in as_completed(futs):
                try:
                    print(fu.result(), flush=True)
                except Exception as e:                      # noqa: BLE001
                    print(f"[FAIL] {futs[fu]}: {str(e)[:160]}", flush=True)
    else:
        for j in jobs:
            try:
                print(_one(j), flush=True)
            except Exception as e:                          # noqa: BLE001
                print(f"[FAIL] {j}: {str(e)[:160]}", flush=True)

    _merge(out, args.tag)
    return 0


def _merge(out: Path, tag: str) -> None:
    """把本轮所有 worker 的因子按名字归并，出候选库（供归纳 + 去重）。"""
    by: dict = {}
    for p in sorted(out.glob(f"{tag}_*.json")):
        if p.name.startswith("merge_"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                   # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        day = ",".join(d.get("days") or []) or p.stem
        for f in d.get("factors") or []:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            e = by.setdefault(name, {"name": name, "axis": d.get("axis"),
                                     "conds": [], "days": [], "n": 0,
                                     "d_pp": [], "a_med": [], "desc": f.get("desc", "")})
            ev = f.get("evidence") or {}
            e["days"].append(day)
            e["n"] += int(ev.get("n") or 0)
            if ev.get("d_pp") is not None:
                e["d_pp"].append(float(ev["d_pp"]))
            if ev.get("a_med") is not None:
                e["a_med"].append(float(ev["a_med"]))
            cd = f.get("cond") or ""
            if cd and cd not in e["conds"]:
                e["conds"].append(cd)
    rows = sorted(by.values(), key=lambda x: -x["n"])
    p = out / f"merge_{tag}.json"
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合并候选 {len(rows)} 条（同名跨天已归并）→ {p}")
    for r in rows[:25]:
        d = st.mean(r["d_pp"]) if r["d_pp"] else float("nan")
        a = st.mean(r["a_med"]) if r["a_med"] else float("nan")
        print(f"  {r['name'][:16]:18s} {r['axis'][:4]} 出现{len(r['days'])}天 "
              f"Σn={r['n']:3d} d̄={d:+6.1f} ā̄={a:.2f}  {r['conds'][0][:52]}")


if __name__ == "__main__":
    raise SystemExit(main())
