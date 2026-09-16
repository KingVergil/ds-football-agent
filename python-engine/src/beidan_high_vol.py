"""高波动 v2：SP 归一化 + 「错价比例」筛选（2026-09-14 用户口径）。

## 为什么需要它

`M过(N−4)` 票型上线后，**「全包腿」不再出现**（不再用 H/D/A 全选去覆盖冷门），
所以"高波动"不能再按全包腿定义。新口径：一场比赛波动大不大，看**开奖结果相对于
锐市场公平价贵了多少** —— 也就是"这场开出了市场没料到的结果"。

## 口径

```
Pinnacle 1X2（goal_line=0，去水）
      │  line_convert（Poisson 拟合 λh/λa）
      ▼
该场 goal_line 下的三路公平概率 p̂
      │
比例 = 开奖SP / Pinnacle价格 = SP × p̂(实际开出的一侧)
```

- **候选池 = 单日**（当日完场未下单北单场次）。**不滚动** —— 实测滚动 7 天时
  每天 top10 与前一日重复 8~10 条，等于天天对同一批反复反思、白烧 token。
- **入选条件**：`比例 > (1/0.65)^(1/4) = 1.11371`。
  `(1/返奖率)^(1/N)` 是「每关注」的盈亏平衡比例；每注实际 5 关（`9过5`），
  严格平衡线是 `^(1/5)=1.08998`，这里按 **N=4** 取值 ⇒ **故意留 gap**：
  每关多要求 ~2.4 个百分点，整串多出 ~17.5% 余量。
- **截断**：入选多于 `HIGH_VOL_TOP`(=10) 条时按比例降序保留前 10（省 token）。
  实测 7 月上半月单日入选 0~12 条、均值 2.9，只有 50 场级别的巨日才会撞到上限。
- 样本的 **pick/hit/profit 一律按引擎 gated 侧（x≥θ）** 计算，不由"实际开出的一侧"
  反推 —— 否则每个样本都命中，因子统计失去区分度（用户口径，见 2026-09-14）。

## 实测参考（2026-07-01~15，164 场）

| 口径 | 结果 |
|---|---|
| ~~单关线 `1/0.65=1.5385`~~ | 5 条（3.4%），8 天里 5 天为 0 —— **已废弃，别再拿它当腿级线** |
| 5关严格线 `^(1/5)=1.08998` | 31.1% 过线，单日 0~12 |
| **本口径 4关留gap `^(1/4)=1.11371`** | **44 条/15天，单日 0~12、均值 2.9，3 天为 0** |

> ⚠️ **腿级线 vs 整票线（2026-09-15 用户口径）**：`0.65` 只在整票结算时收一次，
> 所以 4 关票的打平条件是 `Π(SP·p̂) > 1/0.65 = 1.5385`，**摊到每腿**就是
> `SP·p̂ > (1/0.65)^(1/4) = 1.11371`。`1.5385` 只是「1 串 1（单关）」的腿级线，
> 拿它当 4 关票的腿级门会把标准抬高 `1.5385/1.11371 − 1 ≈ 38%` ——
> 这正是 2026-09-14 之前反思样本几乎为空、因子学不到错价的原因。

（同事件比较才成立：北单赛前赔率与 SP 都是**让球后**口径，必须把 Pinnacle 1X2 先
归一到该场 goal_line 才可以比 —— 直接拿 SP 除 goal-0 概率会把比值系统性抬到 >1。）

## 用法

```python
from src.beidan_high_vol import overlay_of_match, select_high_vol

rows = select_high_vol(matches, tags_of=lambda lid: dm.get_tags(lid), top=10)
# rows: [{lota_id, side, sp, goal_line, phat, ratio, effective}, ...] 按比例降序
```
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from .beidan_settlement import BEIDAN_RETURN_RATE, result_code_to_pick
from .line_convert import convert_1x2_odds

#: 阈值：每关平衡线 `(1/返奖率)^(1/N)`。
#: ⚠️ N 取 **4**（而每注实际是 5 关）—— **故意留 gap**：5 关的严格平衡线是
#: `(1/0.65)^(1/5)`=1.08998（每关高 9.0%），留 gap 后要求每关高 11.4%
#: （1.11371），整串多出 ~17.5% 余量（2026-09-14 用户口径）。
HIGH_VOL_GAP_LEGS = 4
HIGH_VOL_TOP = 10


def high_vol_threshold(legs: int = HIGH_VOL_GAP_LEGS,
                       return_rate: float = BEIDAN_RETURN_RATE) -> float:
    """每关的最低错价比例：`(1/返奖率)^(1/legs)`。

    `legs` 传入每注实际关数 ⇒ 得到严格平衡线；传更小的数 ⇒ 要求更严 ⇒ 留 gap。
    """
    return (1.0 / float(return_rate)) ** (1.0 / max(int(legs), 1))


#: 默认入选阈值（比例 > 该值）
HIGH_VOL_RATIO_THRESHOLD = high_vol_threshold()


def pinnacle_probs_at_line(pin_hda: Optional[Sequence[float]],
                           goal_line: float) -> Optional[tuple[float, float, float]]:
    """Pinnacle 1X2（goal_line=0）→ **归一化到 `goal_line`** 的三路公平概率。

    这一步就是"把锐市场价格归一到北单让球线"：去水 + Poisson 让球换算
    （复用 `line_convert`，与本狗 `x = 市场p̂ × 北单赔率` 用的是同一个换算）。
    拿不到 Pinnacle 1X2（或非法值）→ None。
    """
    if not pin_hda or len(pin_hda) != 3:
        return None
    try:
        h, d, a = (float(v) for v in pin_hda)
    except (TypeError, ValueError):
        return None
    if min(h, d, a) <= 1.0:
        return None
    try:
        probs = convert_1x2_odds(h, d, a, float(goal_line))
    except Exception:
        return None
    if not probs or len(probs) != 3:
        return None
    return tuple(float(p) for p in probs)  # type: ignore[return-value]


def actual_side(match: dict) -> Optional[str]:
    """该场**开奖结果**在让球后的落点（H/D/A）；无赛果 → None。"""
    bi = match.get("beidan_info") or {}
    raw = bi.get("result")
    if raw in (None, ""):
        return None
    try:
        return result_code_to_pick(str(raw).strip())
    except Exception:
        return None


def overlay_of_match(match: dict, tags: Optional[dict] = None, *,
                     return_rate: float = BEIDAN_RETURN_RATE,
                     threshold: float = HIGH_VOL_RATIO_THRESHOLD) -> Optional[dict]:
    """算一场比赛的「错价比例」。缺 Pinnacle / 缺赛果 / 缺 SP → None。

    返回 `{lota_id, side, sp, goal_line, phat, pinnacle_odds, ratio, effective, high_vol}`。
    `high_vol` ⇔ `ratio > threshold`（默认 `(1/0.65)^(1/4)`，见 `high_vol_threshold`）。
    """
    from .beidan_parlay_dog import BeidanParlayDog  # 局部 import：避免模块级循环

    lid = match.get("lota_id") or ""
    if not lid:
        return None
    bi = match.get("beidan_info") or {}
    try:
        sp = float(bi.get("spvalue") or 0.0)
    except (TypeError, ValueError):
        return None
    side = actual_side(match)
    if not side or sp <= 0:
        return None
    try:
        goal_line = float(bi.get("goal_line") or 0.0)
    except (TypeError, ValueError):
        goal_line = 0.0
    pin = BeidanParlayDog._pinnacle_hda(tags if isinstance(tags, dict) else {})
    probs = pinnacle_probs_at_line(pin, goal_line)
    if not probs:
        return None
    phat = {"H": probs[0], "D": probs[1], "A": probs[2]}[side]
    if phat <= 0:
        return None
    ratio = sp * phat                      # = 开奖SP / Pinnacle价格
    effective = return_rate * ratio        # = 0.65 × SP × p̂
    return {
        "lota_id": lid,
        "side": side,
        "sp": sp,
        "goal_line": goal_line,
        "phat": phat,
        "pinnacle_odds": (1.0 / phat) if phat else None,
        "ratio": ratio,
        "effective": effective,
        "high_vol": ratio > threshold,
    }


def select_high_vol(matches: Sequence[dict],
                    tags_of: Optional[Callable[[str], dict]] = None, *,
                    top: int = HIGH_VOL_TOP,
                    return_rate: float = BEIDAN_RETURN_RATE,
                    threshold: float = HIGH_VOL_RATIO_THRESHOLD) -> list[dict]:
    """筛出「高波动」场次：比例 > `threshold`，按比例降序，最多 `top` 条。

    `tags_of(lota_id)` 取该场 tags（含 `eu-odds-pinnacle`）；不传则跳过（全都拿不到
    Pinnacle → 返回空）。**只做筛选与排序，不构造样本** —— 样本由调用方按引擎
    gated 侧构造。
    """
    rows: list[dict] = []
    for m in matches or []:
        lid = m.get("lota_id") or ""
        tags = None
        if tags_of is not None and lid:
            try:
                tags = tags_of(lid)
            except Exception:
                tags = None
        row = overlay_of_match(m, tags, return_rate=return_rate,
                               threshold=threshold)
        if row and row["high_vol"]:
            rows.append(row)
    rows.sort(key=lambda r: -r["ratio"])
    return rows[: max(int(top), 0)] if top else rows
