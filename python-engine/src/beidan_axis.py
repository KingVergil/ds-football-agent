"""北单两轴选腿：先判「方向」（directional），再按「波动」（volatility）排序。

为什么要有这个模块（2026-09-15 用户口径）
----------------------------------------
北单 `M过4` 串关的打平**每腿线** = `(1/0.65)^(1/4) = 1.11371`（0.65 只在整票收一次）。
一条腿对整票的贡献能拆成三个独立乘数：

```
SP·p̂  =   x        ×        ā         ×     (p̂ / mp)
          │                 │                └── 方向轴：真实命中率比市场 p̂ 高多少
          │                 └─────────────────── 波动轴：开奖 SP 相对赛前赔率的保留率
          └───────────────────────────────────── 价格轴：池子给价 / 锐市场公平价（引擎的 x 门）
```

三轴必须分工。历史教训（`docs/handoff_95dog_structural_loop.md` 及本轮补测）：
把三轴混成一条 `y = SP·1{中}` 去挑因子，既分不清是方向还是价格，腿级方差又大到
CI 覆盖一切（81 腿的 CI ±0.42，0/10 中票因此什么都证明不了）。实测两轴各自有信号、
而且**互相抵消**：高赔（冷门）侧方向边际最强但 SP 缩水最狠，热门侧 SP 不缩水但方向无边际。
所以正确顺序是：**先用方向门定"打哪边"，再用波动轴排序定"进哪几条腿"**。

实测（55 天腿池，`x>1.11371` 且 Pinnacle 源，n=1102；详见 docs/beidan_axis_two_stage.md）：

| 方向门 | 波动档（三侧赔率结构） | n | 命中−mp | ā | y=E[SP·1{中}] |
|---|---|---|---|---|---|
| 弱侧 mp<0.35 | 集中 T1 | 91 | **+10.1pp** | 0.91 | **1.533** |
| 弱侧 mp<0.35 | 中 T2 | 300 | +6.9pp | 0.79 | 1.429 |
| 弱侧 mp<0.35 | 分散 T3 | 503 | +2.4pp | 0.61 | 1.312 |

→ 只在方向门内、**按波动升序**取腿，才能拿到 `y` 的置信下沿过 1.11371 的组合。

用法
----
```python
from src.beidan_axis import two_stage_select, axis_report

legs = [{"side": "D", "market_p": 0.28, "x": 1.21, "beidan_odds": 3.9,
         "feats": feats_of(tags_sections)}]
out = two_stage_select(legs)      # {'passed': [...], 'rejected': [...], 'ranked': [...]}
```
"""

from __future__ import annotations

import math
import re
import statistics as st
from typing import Callable, Iterable, Optional, Sequence

RATE = 0.65
LINE1 = 1.0 / RATE                  # 1.5385 单关（1 串 1）腿级线
LEG_LINE4 = (1.0 / RATE) ** 0.25    # 1.11371 4 关票的每腿线

SIDES = ("H", "D", "A")
_SIDE_IDX = {"H": 0, "D": 1, "A": 2}

# 三侧赔率结构分散度 `span` 的三分位（55 天腿池实测，见 docs/beidan_axis_two_stage.md）
SPAN_T1 = 0.487
SPAN_T2 = 0.695

# ── 盘口文本 → 让球值 ────────────────────────────────────────────
_HANDICAP = {
    "平手": 0.0, "平半": 0.25, "半球": 0.5, "半一": 0.75,
    "一球": 1.0, "一球半": 1.5, "球半": 1.5, "两球": 2.0,
    "两球半": 2.5, "三球": 3.0,
}
_TOK_TIME = re.compile(r"(?:OPt[-\d]+m=|Δt[+-]\d+m)[↑↓→]*")


def handicap_val(tok: str) -> Optional[float]:
    """`受半/一` → -0.75，`一球` → 1.0，无法识别 → None。"""
    t = (tok or "").strip()
    if not t:
        return None
    sign = 1.0
    if t.startswith("受"):
        sign, t = -1.0, t[1:]
    t = t.replace("/", "")
    # 从长到短匹配，先命中更长（更具体）的盘口名
    for k in sorted(_HANDICAP, key=len, reverse=True):
        if k in t:
            return sign * _HANDICAP[k]
    return None


def _rows(text: str, n: int = 3) -> list[list[float]]:
    """把一个 tags 段落切成 `[[f1, f2, ...], ...]`（跳过取不到 n 个数的行）。"""
    out: list[list[float]] = []
    for m in _TOK_TIME.finditer(text or ""):
        rest = (text or "")[m.end():].split("\n")[0].strip()
        rest = re.sub(r"\(r?[\d.]+%?\)\s*$", "", rest)
        parts = [p for p in rest.split("/") if p != ""]
        if len(parts) >= n + 1:
            # 末尾可能是返还率（>5），先丢掉
            try:
                if len(parts) > n and float(parts[-1]) > 5:
                    parts = parts[:-1]
            except ValueError:
                pass
        if len(parts) != n:
            continue
        try:
            out.append([float(p) for p in parts])
        except ValueError:
            continue
    return out


def ah_series(text: str) -> list[tuple[float, float, float]]:
    """亚盘段落 → [(h_water, line, a_water), ...]（首行=最早，末行=最新）。"""
    out: list[tuple[float, float, float]] = []
    for m in _TOK_TIME.finditer(text or ""):
        rest = (text or "")[m.end():].split("\n")[0].strip()
        rest = re.sub(r"\(r?[\d.]+%?\)\s*$", "", rest)
        parts = [p for p in rest.split("/") if p != ""]
        if len(parts) >= 3:
            try:
                if float(parts[-1]) > 5:
                    parts = parts[:-1]
            except ValueError:
                pass
        if len(parts) < 3:
            continue
        line = handicap_val("/".join(parts[1:-1]))
        if line is None:
            continue
        try:
            out.append((float(parts[0]), line, float(parts[-1])))
        except ValueError:
            continue
    return out


def odds_span(odds_h, odds_d, odds_a) -> Optional[float]:
    """三侧北单赔率的相对分散度 `(max-min)/mean` —— 波动轴的核心观测量。

    越小 = 三侧赔率越接近（结构集中、低波动、SP 保留率 ā 越高）。
    """
    try:
        o = [float(odds_h), float(odds_d), float(odds_a)]
    except (TypeError, ValueError):
        return None
    if min(o) <= 0:
        return None
    m = sum(o) / 3.0
    return (max(o) - min(o)) / m if m > 0 else None


def features_of(sections: dict) -> dict:
    """tags 段落 → 赛前结构特征（只读赛前快照，不看赛果/SP）。"""
    secs = sections or {}
    f: dict = {}

    dis = _rows(secs.get("discrete-odds") or "", 3)
    if len(dis) >= 2:
        d0, d1 = dis[0], dis[-1]
        f["disp_first"], f["disp_last"] = d0, d1
        f["disp_min_side"] = SIDES[min(range(3), key=lambda i: d1[i])]
        f["disp_max_side"] = SIDES[max(range(3), key=lambda i: d1[i])]
        f["disp_trend"] = {s: d1[i] - d0[i] for i, s in enumerate(SIDES)}

    ah = ah_series(secs.get("asian-handicap-crown") or "")
    if len(ah) >= 2:
        f["ah_crown"] = {"h0": ah[0][0], "line0": ah[0][1], "a0": ah[0][2],
                         "h1": ah[-1][0], "line1": ah[-1][1], "a1": ah[-1][2],
                         "dline": ah[-1][1] - ah[0][1]}

    eu = _rows(secs.get("eu-odds-pinnacle") or "", 3)
    if len(eu) >= 2:
        e0, e1 = eu[0], eu[-1]
        f["eu1"] = e1
        f["eu_move"] = {s: ((e1[i] - e0[i]) / e0[i] if e0[i] else 0.0)
                        for i, s in enumerate(SIDES)}

    fair = secs.get("fair-odds") or ""
    m = re.search(r"主客进球和:\s*(-?[\d.]+)", fair)
    if m:
        f["goals_sum"] = float(m.group(1))
    m = re.search(r"主客实力差:\s*(-?[\d.]+)", fair)
    if m:
        f["strength_gap"] = float(m.group(1))
    return f


# ── 方向轴：证据表（55 天腿池，x>1.11371 且 Pinnacle 源，n=847）────────
# 每条规则的 `delta_pp` = 该条件下「命中率 − 市场 p̂」的百分点边际。
# ⚠️ 规则之间相关（例如弱侧 ⊂ 冷门侧），score 只是**先验**，不是无偏估计；
#    真实增量由 scripts/axis_two_stage_backtest.py 的组合口径给。
def _f(feats: dict, key: str, default=None):
    return (feats or {}).get(key, default)


def _coldest_side(feats: dict) -> Optional[str]:
    eu = _f(feats, "eu1")
    if not eu:
        return None
    return SIDES[max(range(3), key=lambda i: eu[i])]


def _hottest_side(feats: dict) -> Optional[str]:
    eu = _f(feats, "eu1")
    if not eu:
        return None
    return SIDES[min(range(3), key=lambda i: eu[i])]


def _ah(feats: dict, key: str = "ah_crown") -> dict:
    return _f(feats, key, {}) or {}


def _my_water(feats: dict, side: str) -> Optional[float]:
    ah = _ah(feats)
    if side == "H":
        return ah.get("h1")
    if side == "A":
        return ah.get("a1")
    return None


# (名称, 方向轴边际 pp, 判定, 说明) —— 负数 = 否决项
DIRECTION_RULES: tuple[tuple[str, float, Callable[[dict, str, float], bool], str], ...] = (
    ("冷门侧（我方最高赔）", +4.8,
     lambda f, s, mp: _coldest_side(f) == s, "n=381，方向轴最强的一条"),
    ("我方=最发散侧", +4.4,
     lambda f, s, mp: _f(f, "disp_max_side") == s, "n=348，离散把这一侧当分歧方"),
    ("主队水位走低（≤0.90）", +4.4,
     lambda f, s, mp: (_ah(f).get("h1") or 9) <= 0.90, "n=246，主队被买"),
    ("弱侧（市场 p̂<0.35）", +3.4,
     lambda f, s, mp: mp < 0.35, "n=688，弱侧整体有方向边际"),
    ("小球（预期进球和<2.6）", +3.2,
     lambda f, s, mp: (_f(f, "goals_sum") or 9) < 2.6, "n=339"),
    ("欧赔下沉（我方 <-2%）", +2.9,
     lambda f, s, mp: (_f(f, "eu_move", {}) or {}).get(s, 0) < -0.02, "n=474"),
    ("盘口不动（让球线首=末）", +2.5,
     lambda f, s, mp: _ah(f).get("dline", 1) == 0, "n=573"),
    ("我方高水（≥1.00）", +2.3,
     lambda f, s, mp: (_my_water(f, s) or 0) >= 1.00, "n=157"),
    ("我方=最凝聚侧（本门内无效）", -0.2,
     lambda f, s, mp: _f(f, "disp_min_side") == s, "n=266，单关狗旗舰策略在北单门内没有增量"),
    ("热门侧（我方最低赔）", -0.5,
     lambda f, s, mp: _hottest_side(f) == s, "n=130，无方向边际"),
    ("欧赔上升（我方 >+2%）", -1.1,
     lambda f, s, mp: (_f(f, "eu_move", {}) or {}).get(s, 0) > 0.02, "n=239"),
    ("主队水位走高（≥1.02）", -2.5,
     lambda f, s, mp: (_ah(f).get("h1") or 0) >= 1.02, "n=202，方向轴明确为负"),
    ("升盘（让球线加大）", -2.5,
     lambda f, s, mp: _ah(f).get("dline", 0) > 0, "n 小但方向轴为负，作否决"),
)

DIRECTION_MIN_PP = 2.0      # 方向门：先验边际低于此值不放行


def direction_score(feats: dict, side: str, market_p: float,
                    rules: Optional[Sequence] = None) -> tuple[float, list[str]]:
    """方向轴先验边际（百分点）。返回 (score, 命中的规则名)。"""
    score, hits = 0.0, []
    for name, delta, pred, _ in (rules if rules is not None else DIRECTION_RULES):
        try:
            ok = bool(pred(feats or {}, side, float(market_p or 0.0)))
        except Exception:
            ok = False
        if ok:
            score += delta
            hits.append(name)
    return score, hits


def direction_gate(feats: dict, side: str, market_p: float,
                   min_pp: float = DIRECTION_MIN_PP,
                   rules: Optional[Sequence] = None) -> tuple[bool, float, list[str], str]:
    """方向门：边际 ≥ min_pp 且不命中否决项。返回 (放行?, score, 规则, 原因)。"""
    tbl = rules if rules is not None else DIRECTION_RULES
    score, hits = direction_score(feats, side, market_p, rules=tbl)
    neg = [n for n in hits
           if any(n == r[0] and r[1] < 0 for r in tbl)]
    if neg:
        return False, score, hits, f"方向否决：{'、'.join(neg)}"
    if score < min_pp:
        return False, score, hits, f"方向边际 {score:+.1f}pp < {min_pp:.1f}pp"
    return True, score, hits, "、".join(hits)


# ── 波动轴：SP 保留率 ā 的观测量 ────────────────────────────────
def volatility_key(leg: dict) -> tuple[float, float]:
    """波动轴排序键，**越小越好**（= 波动越小 = SP 越靠得住）。

    主序是 `span`（三侧赔率结构分散度，实测 T1 的 ā=0.96 → T3 的 ā=0.61），
    次序用我方赛前赔率（同样预测 SP 缩水：赔率越高、池子砍得越狠）。
    """
    span = leg.get("span")
    if span is None:
        try:
            span = odds_span(leg.get("odds_h"), leg.get("odds_d"), leg.get("odds_a"))
        except Exception:
            span = None
    if span is None:
        span = 9.99                      # 拿不到结构 → 当最差波动处理（保守）
    try:
        odds = float(leg.get("beidan_odds") or 0) or 99.0
    except (TypeError, ValueError):
        odds = 99.0
    return (float(span), odds)


def volatility_tier(span: Optional[float]) -> str:
    """结构分散度 → 波动档（T1 集中 / T2 中 / T3 分散）。"""
    if span is None:
        return "T?"
    if span <= SPAN_T1:
        return "T1"
    if span <= SPAN_T2:
        return "T2"
    return "T3"


# ── 两阶段选择 ──────────────────────────────────────────────────
def two_stage_select(legs: Sequence[dict], x_theta: float = LEG_LINE4,
                     min_dir_pp: float = DIRECTION_MIN_PP,
                     max_span: Optional[float] = None,
                     top: Optional[int] = None,
                     rules: Optional[Sequence] = None,
                     span_cuts: Optional[tuple] = None) -> dict:
    """先方向、后波动。

    输入 `legs`：每条腿是 dict，至少含 `side` / `market_p` / `x` / `beidan_odds`，
    以及 `feats`（`features_of(tags_sections)` 的产物；缺失时方向门只剩弱侧规则）
    或顶层的 `odds_h/odds_d/odds_a`。

    返回 `{"passed": [...], "ranked": [...], "rejected": [(leg, why), ...],
            "stage1_dropped": n, "stage2_dropped": n}`

    - 阶段 0（价格轴）：`x < x_theta` 直接出局 —— x 是引擎门，不参与排序。
    - 阶段 1（方向轴）：`direction_gate` 不过 → 出局。
    - 阶段 2（波动轴）：按 `volatility_key` 升序排；`max_span` 可再砍掉高波动档。
    """
    passed, rejected = [], []
    for leg in legs or []:
        try:
            x = float(leg.get("x") or 0.0)
        except (TypeError, ValueError):
            x = 0.0
        if x < float(x_theta):
            rejected.append((leg, f"价格轴：x={x:.3f} < {float(x_theta):.5f}"))
            continue
        feats = leg.get("feats") or {}
        side = str(leg.get("side") or "")
        try:
            mp = float(leg.get("market_p") or 0.0)
        except (TypeError, ValueError):
            mp = 0.0
        ok, score, hits, why = direction_gate(feats, side, mp, min_pp=min_dir_pp,
                                              rules=rules)
        if not ok:
            rejected.append((leg, f"方向轴：{why}"))
            continue
        item = dict(leg)
        item["dir_score_pp"] = score
        item["dir_rules"] = hits
        item["vol_key"] = volatility_key(leg)
        item["vol_tier"] = volatility_tier(item["vol_key"][0])
        passed.append(item)

    stage1_dropped = len(rejected)
    if max_span is not None:
        keep = []
        for it in passed:
            if it["vol_key"][0] > float(max_span):
                rejected.append((it, f"波动轴：span={it['vol_key'][0]:.3f} > {float(max_span):.3f}"))
            else:
                keep.append(it)
        passed = keep
    stage2_dropped = len(rejected) - stage1_dropped

    ranked = sorted(passed, key=lambda it: (it["vol_key"], -it["dir_score_pp"]))
    if top is not None:
        ranked = ranked[: int(top)]
    return {"passed": passed, "ranked": ranked, "rejected": rejected,
            "stage1_dropped": stage1_dropped, "stage2_dropped": stage2_dropped}


# ── 因子审计：两轴分开报（因子产出的评测口径）────────────────────
def axis_report(legs: Iterable[dict], name: str = "") -> dict:
    """对一个腿子集出两轴指标 —— **不要**再用单条 `y` 挑因子。

    方向轴 `ddir_pp`  = 命中率 − 市场 p̂（百分点，带标准误 `ddir_se_pp`）
    波动轴 `a_med`    = 开奖 SP / 赛前赔率 的中位数（<1 = 缩水）
    结算轴 `y`        = E[SP·1{中}]，只用来最后验收（对照 1.11371）
    """
    ss = [l for l in (legs or []) if l.get("settled", True)]
    n = len(ss)
    if not n:
        return {"name": name, "n": 0}
    hit = sum(1 for l in ss if l.get("hit")) / n
    mp = st.mean(float(l.get("market_p") or 0.0) for l in ss)
    se_h = math.sqrt(max(hit * (1 - hit), 0.0) / n)
    se_mp = (st.pstdev([float(l.get("market_p") or 0.0) for l in ss]) / math.sqrt(n)
             if n > 1 else 0.0)
    ratios = [float(l["sp"]) / float(l["beidan_odds"]) for l in ss
              if float(l.get("sp") or 0) > 0 and float(l.get("beidan_odds") or 0) > 0]
    zs = [float(l["sp"]) if l.get("hit") else 0.0 for l in ss]
    y = st.mean(zs)
    se_z = (st.pstdev(zs) / math.sqrt(n)) if n > 1 else 0.0
    return {
        "name": name, "n": n, "hit": hit, "mp": mp,
        "ddir_pp": (hit - mp) * 100.0,
        "ddir_se_pp": math.sqrt(se_h ** 2 + se_mp ** 2) * 100.0,
        "a_med": (st.median(ratios) if ratios else None),
        "x": st.mean(float(l.get("x") or 0.0) for l in ss),
        "y": y, "y_lo": y - 1.96 * se_z, "y_hi": y + 1.96 * se_z,
        "roi_single": RATE * y - 1.0,
        "passes_leg_line": (y - 1.96 * se_z) > LEG_LINE4,
    }


def format_axis_row(r: dict) -> str:
    if not r.get("n"):
        return f"| {r.get('name','')} | 0 | — | — | — | — | — | — |"
    a = f"{r['a_med']:.2f}" if r.get("a_med") is not None else "—"
    mark = "✅" if r.get("passes_leg_line") else ("≈" if r["y"] > LEG_LINE4 else "❌")
    return (f"| {r['name']} | {r['n']} | {r['hit']*100:.1f}% | {r['mp']*100:.1f}% | "
            f"{r['ddir_pp']:+.1f}±{r['ddir_se_pp']:.1f} | {a} | "
            f"{r['y']:.3f} [{r['y_lo']:.3f},{r['y_hi']:.3f}] | {mark} |")
