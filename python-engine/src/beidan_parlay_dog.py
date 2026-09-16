"""
bc狗 — 长串（6+ 串）+ 每腿 1~3 选（默认双选）+ 开奖SP结算。

在竞彩串关狗（chuan_guan_dog.ChuanGuanDog）骨架之上派生：
  - 数据：data/beidan/*.json 的 beidan_info（goal_line + 胜平负赔率 + result + spvalue）
  - 选腿：规则版，每腿可选 1~3 个选项（单选/双选/全选）
  - 默认票型：一张 8串1，5 个全包腿（胜/平/负全选）+ 3 个单选腿，共 3^5=243 注
  - 北单每注 2 元（UNIT_STAKE），返奖 = 每注 2 元 × 开奖SP连乘 × 65%
  - 组票：N串1 / N过M，多选腿做笛卡尔积展开为单注子单
  - 结算：以官方开奖 result（3=胜/1=平/0=负）判命中，用开奖 spvalue 连乘 × 65% 返奖

用法:
  python3 -m src.beidan_parlay_dog analyze [YYYY-MM-DD] [--picks 2] [--tickets 6串1] [--dry-run] [--stake-pct 5]
  python3 -m src.beidan_parlay_dog settle [YYYY-MM-DD]
  python3 -m src.beidan_parlay_dog pending | status | reset
  python3 -m src.beidan_parlay_dog backtest <start> <end> [--picks 2] [--tickets 6串1]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import backtest_fet
from .beidan_settlement import (
    BEIDAN_RETURN_RATE,
    parlay_combos_count,
    _round2,
    parlay_combinations,
    parse_ticket_spec,
    settle_leg,
    settle_parlay_combo,
)
from .beidan_high_vol import (
    HIGH_VOL_RATIO_THRESHOLD,
    HIGH_VOL_TOP,
    overlay_of_match,
    select_high_vol,
)
from .chuan_guan_dog import ChuanGuanDog
from .data_manager import DataManager, is_offline
from .environment import football_day_calendar_dates, get_football_day
from .models import _uid
from .p_calibration import calibration_for, mode_of_leg
from .role import Role

_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_bj(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now(_BEIJING_TZ).strftime(fmt)


_GL_TRANSLATION_RULE = """## ⚠️ 让球线换算（推荐 H/D/A 前必读，先算清楚再给方向）
北单 H/D/A 是**让球后**方向；goal_line 是主队让球线（负=主队让球，正=主队受让，0=平手）。
每场先算：让球后净胜 =（主队进球 − 客队进球）+ goal_line，>0→H，=0→D，<0→A；不许跳过换算直接写方向。
因子描述里的「主胜/平局/客胜」按**实际赛果**写，落点必须经上面换算，禁止直接当 H/D/A 套用。

常用让球线对照（“主净胜 X”= 主队赢 X 个净胜球；“主净负 X”= 客队赢 X 球）：
- goal_line=0（平手）：主胜→H；实际平局→D；客胜→A
- goal_line=-1（主让1球）：主净胜≥2→H；主恰好净胜1→D；实际平局或客胜→A
- goal_line=-2（主让2球）：主净胜≥3→H；主恰好净胜2→D；主净胜≤1、实际平局或客胜→A
- goal_line=+1（主受1球）：实际平局或主胜→H；主恰好净负1→D；客净胜≥2→A
- goal_line=+2（主受2球）：主净负≤1、实际平局或主胜→H；主恰好净负2→D；客净胜≥3→A
更深的让球线（±3 及以上）套上方公式，不要照 ±1 的例子硬猜。

### 信号落点速查（先按信号语义找实际落点，再套公式给 H/D/A）
- 「上盘/让球方难赢、退盘示弱、主队让不起」= 受让方赢盘：
  goal_line<0（主队让球）→ **A**（含实际平局或客胜，一次覆盖）；goal_line>0（主队受让）→ **H**；
  goal_line=0 只能说明主队难赢，D 还是 A 再按资金/离散/盘口定。
- 「实际平局概率升高 / 防平」：goal_line=0 → D；goal_line<0 → A；goal_line>0 → H。
- 北单 D 不是“实际平局”，而是“让球后恰好打平”：主让 1 球=主队恰好净胜 1 球；
  主让 2 球=恰好净胜 2 球；主受让 1 球=主队恰好净负 1 球。没有“恰好 X 球”的独立证据就不要写 D。
- 信号指向受让方/下盘且 goal_line<0 时，输出 A 就是明确的，不许再因“因子只写 D 或 A 不明确”
  而退回 skip/高波动；A 已经覆盖实际平局与客胜两个赛果。"""


# 北单过关的编程上限（引擎兜底口径，人设只能在其内选择）：
#   · 每**注**组合腿数 ≤ 9（北单最高 9 关）
#   · 容错额度 N − M ≤ 8（票型 "N过M"）
#   ⇒ 最多可选场数 = 9 + 8 = 17，例如「12 场单选、容错 4」= 12过8 合法（每注 8 关）。
BD_MAX_COMBO_LEGS = 9
BD_MAX_TOLERANCE = 8

#: 护栏保守默认（配置里 max_combos / max_stake_pct 为 0 或缺失时生效）
#: —— 0 绝不能表示"无限制"（2026-09-14：0704 实测 126 注被放大到 456 注 = 912 元/单）
#: ⚠️ 用户口径（2026-09-14）：**允许梭哈**，0/缺失 = 100%（不做保守默认）
DEFAULT_MAX_STAKE_PCT = 100.0


def cap_picks_by_x(picks: list, xs: dict, max_per_leg: int) -> list:
    """规则路径：一侧最多买 `max_per_leg` 个方向，超了留 **x 最高**的。

    为什么需要（2026-09-14）：规则路径（`gate_basis=x`）原本买「所有 x ≥ θ 的侧」，
    完全无视 `max_per_leg` —— 配了也白配。实测 3 条双选腿把 9过4 的 126 注放大到
    456 注 = 912 元/单。规则路径的"侧"由 x 决定 ⇒ 留 x 最高的那侧。
    未超限时原样返回；超限时按 H/D/A 稳定顺序返回（下游按顺序展示）。
    """
    try:
        n = int(max_per_leg)
    except (TypeError, ValueError):
        return list(picks)
    if n <= 0 or len(picks) <= n:
        return list(picks)
    keep = set(sorted(picks, key=lambda s2: -float((xs or {}).get(s2, 0.0)))[:n])
    return [s2 for s2 in ("H", "D", "A") if s2 in keep]


# 反思用：先讲清「样本里的 H/D/A 与 SP 都是**让球后**的」，再给换算表。
# 没有这段时 LLM 会把 `实际:D` 当"原始比分打平"读（用户报障：主队让 1 球却买 A，读不懂），
# 而北单 D 是"让球后恰好打平"（主让 1 球 = 主队恰好净胜 1 球）。
_REFLECT_GL_RULE = """## ⚠️ 读样本前必读：H/D/A 与开奖 SP 都是「让球后」口径
- `实际:H/D/A` 是**让球后**赛果，**不是**原始比分方向；`开奖SP` 是这个**让球后结果**的派彩赔率。
- `pick`（买的侧 / 过门侧）同样是**让球后**方向，与 `实际` 同一口径。
- 每场片段都标了让球线：`让球让1`=goal_line −1（主队让 1 球）、`让球受让1`=+1（主队受让 1 球）、
  `让球0(平手)`=0。**同一比分在不同让球线下会得出不同的 H/D/A**，不换算就等于读错样本。
- 因此：主队 1:2 输球，若 `让球受让1`，让球后为 2:2 → 实际 **D**（不是 A）；若 `让球让1`，让球后 1:2 → 实际 **A**。
- 因子描述里的「主胜/平局/客胜」按**实际赛果**写，但落点必须经换算给出 H/D/A。

""" + _GL_TRANSLATION_RULE


# 反思用（2026-09-15 用户口径）：把**单关狗验证过的「结构条件 → 方向」因子形态**移植到北单，
# 但标签换成「进 4 关票」的腿级口径。没有这段时 LLM 只会产出"盘口/离散/资金"的泛泛描述，
# 或干脆复述引擎早就硬筛过的 x —— 两者都无法在门内做增量。
_REFLECT_STRUCTURAL_RULE = """## 🧱 结构性因子规格（本狗专属 · 北单 N过4）

**因子要回答的问题**：在引擎已按 `x ≥ (1/0.65)^(1/4)=1.11371` 硬筛过的腿里，
**哪些赛前结构条件能让这条腿更值得进 4 关票**。

**标签口径（腿级，4 关票）**
- 北单只在**整票结算时收一次 0.65**（官方：`过关奖金 = 2元 × ΠSP × 65%`），
  所以 4 关票打平要 `Π(SP·p̂) > 1/0.65`，摊到每腿就是 `SP·p̂ > 1.11371`。
- 一条腿的观测标签是 `z = 开奖SP·1{中}`；因子要挑的是 `y = E[z]` 显著高于 1.11371 的腿。
- ❌ **禁止结果条件化**：不许写「这场爆冷了 / SP 高 / 大冷」这类只有赛后才知道的描述。

**因子形态（与单关狗同构，必须写成「结构条件 → 方向落点」）**
- 结构条件 = **赛前可算**的盘口 / 水位 / 离散 / 资金 / 实力差特征（≥2 个维度组合，单一维度不算因子）。
- 方向落点 = H/D/A（**让球后**口径，见上）。
- 描述里要能一眼看出：什么盘口形态 + 什么水位/资金/离散 → 买哪一侧。

**离线实测先验（55 天腿池 3577 条；门内 x>1.11371 且 Pinnacle 源 n=847，基线 y=1.312）**
- ✅ 门内有正增量的结构：
  `我方=最高赔（冷门侧）` y=1.451 [1.219,1.683] n=381｜`我方=最发散侧` 1.411 [1.188,1.634] n=348｜
  `预期进球和<2.6（小球）` 1.374｜`主队水位走低(≤0.90)` 1.374｜`盘口不动` 1.337｜
  `欧赔下沉(<-2%)` 1.326｜`凝聚×弱侧(p̂<0.35)且低水` 1.889 [0.80,2.98] n=26（样本小，待验证）
- ❌ 门内**负增量**（写成因子就是废因子，别写）：
  `升盘（让球线加大）` y=0.941 n=41｜`欧赔上升 且 我方最凝聚（赔升防冷）` 1.036 n=92｜
  `主队水位走高(≥1.02)` 1.085 n=202｜`我方=最凝聚侧` **1.222（低于基线 1.312）**
  —— **单关狗的旗舰「离散凝聚」搬过来在 x 门内没有增量**，不要照搬单关狗的因子名。
- ⚠️ 这些是**门内增量**，不是 x 的替代品：引擎已经筛过 x，因子必须在门内再分层。

**产出要求**
- 每条因子给出：结构条件（可复算）＋落点方向＋样本数/命中率/平均回报（与现有因子库格式一致）。
- 优先产出**组合条件**（如「冷门侧 × 盘口不动 × 赔率下沉」），并说明它相对基线（y=1.312）的增量。
"""


def _close_reflect_session(sess, rt_obj, role, dog) -> None:
    """收尾 reflect_only 自建的 session：落盘 md + 解绑 rt.session。

    ⚠️ 只解绑**我们自己**建的那个（sess 非 None 才动），
    否则会误伤 settle/analyze 流程正在用的 session。
    """
    if sess is None:
        return
    try:
        sess.finish(capital_after=role.capital, stats=dog.status())
    except Exception:
        pass
    if rt_obj is not None:
        try:
            rt_obj.session = None
        except Exception:
            pass


class BeidanParlayDog(ChuanGuanDog):
    """北单让球胜平负长串狗（独立角色/资金/订单，默认 user=bc狗）。"""

    MAX_COMBO_LEGS = BD_MAX_COMBO_LEGS       # 每注组合腿数上限（最高 9 关）
    MAX_TOLERANCE = BD_MAX_TOLERANCE         # 容错上限（N − M ≤ 8）
    PICK_N = BD_MAX_COMBO_LEGS + BD_MAX_TOLERANCE   # 最多可选场数 17
    MAX_PICKS = 3                # 统一模式每腿默认最多选项数；--picks 可改 1/2/3
    BEIDAN_ODDS_FACTOR = 1.0     # 已废弃：北单赔率本就是公平赔率（见 _beidan_odds 去水兜底）
    START_CAPITAL = 1000.0
    BET_TYPE = "北单串关"
    UNIT_STAKE = 2.0             # 北单每注 2 元
    DEFAULT_TICKET = "8串1"
    SINGLE_PICK_LEGS = 3         # 默认策略：3 个单选腿（8串1 内）
    FULL_COVER_LEGS = 5          # 默认策略：5 个全包腿（8串1 内）
    COVER_MODE = "all"           # 全包腿模式："all"=胜/平/负全选；"top"=取前 COVER_PICKS 个
    COVER_PICKS = 3              # COVER_MODE="top" 时每腿选项数（2=双选，3=三选）
    REFLECT_HIGH_SP_TOP = 5      # 反思时取多少场「高波动」候选（现按赛前离散分层，不再按开奖SP）

    # 粗筛阈值：覆盖一条腿要 3 注成本 → 开奖 SP ≥ 3/0.65 ≈ 4.615 才算"这腿出了高波动"。
    # 波动路径**只要这个粗标签**（复用样本里的 hit：全包腿 hit ⇔ profit>0 ⇔ sp>4.615），
    # 不做 SP 分布/分位数的精确计算。
    HIGH_VOL_SP = 3.0 / 0.65

    # 北单返奖数学（人设/引擎共用同一套口径，禁止各写一份）
    BEIDAN_TAKEOUT = 1.0 / 0.65  # 打平所需的最小「整票边际连乘」= 1.538

    # flex 模式护栏默认值（可由角色目录 parlay.json 覆盖；不写 mode 的角色走 legacy）
    FLEX_DEFAULTS = {
        "mode": "legacy",        # legacy=固定票型模板 | flex=LLM 给腿集、引擎只做护栏
        "min_legs": 2,           # 最小腿数（不足 → 空仓）
        "max_legs": BD_MAX_COMBO_LEGS + BD_MAX_TOLERANCE,  # 最大选场数 17（容错票 N 可 >9）
        "max_per_leg": 3,        # 每腿最多选几个方向（1~3）
        "max_combos": 128,       # 注数上限（成本 = 2 元 × 注数）
        "max_stake_pct": 3.0,    # 单票成本 ≤ 资金该百分比
        "min_leg_v": 1.0,        # 单腿边际 v̂ < 该值 → 该腿不进票（无边际=不买）
        "min_ticket_v": None,    # 整票 Πv̂ 下限；None → 1/0.65（即 0.65×Πv̂−1 > 0 才出票）
        # ── phase 2：引擎侧市场 p̂（Pinnacle / 让球欧盘 / 必发）──
        "market_allowance": 1.25,  # LLM 自报 p̂ 最多可高于市场 p̂ 的倍数（超出即回落到市场×该倍数）
        "require_market_p": False,  # True = 拿不到市场参考的场次不进票
        # ── 奖池型漂移安全垫（北单 SP 是赛后奖池定的，不是下注时的价）──
        "sp_drift_sigma": 0.13,  # 单腿「开奖SP/下注时赔率」对数标准差（1531 场实测 p10 0.849/p90 1.208）
        "safety_z": 0.674,       # 打平线取哪个分位（0.674 ≈ p25）
        # ── 奖池账本两道硬门（默认 off；bc狗在 parlay.json 里开启）──
        # 账本按「盘口类型 × x 区间」普查开奖结果（src/pool_ledger.py）：
        #   门 ① 腿池：只收 gl 在 gl_classes 内、且 x = 市场p̂×赛前赔率 ≥ θ 的腿；
        #   门 ② 关数：每注关数 M ≥ m_star = ceil(ln(1/0.65)/ln(y 的 CI 下沿))。
        "selector": "llm",       # llm = LLM 在 gate 候选里挑；x_top = 0-LLM 规则臂
        "section_budget": 0,     # >0 时截断每场数据段到该 token 数；0 = 全量进 prompt（靠 batch 控长）
        # 成票判据：x = 用引擎算的错价倍数（确定性，与 LLM 的 p̂ 无关）；p = 用 LLM 报的 p̂（旧口径）
        "gate_basis": "x",
        # 组装方式：llm = 保留 stage2' 让 LLM 组装；rule = **删掉 stage2**，引擎按规则组装
        "ticket_mode": "llm",
        "x_cap": 0.0,            # >0：把腿的平均 x 超过该值的极端尾部排除（噪声大）；0=不排除
        "rule_legs": 9,          # x_top 模式取多少条腿（每注关数 = 腿数）
        "rule_pick": "random",   # 池子超过 rule_legs 时怎么挑：random(按日种子随机) | x_desc
        "rule_x_cap": 0.0,       # >0 时丢掉 x 超过该值的腿（极端尾部噪声大）
        "pool_gate": {
            "mode": "off",           # off | shadow（只记录不拦）| enforce
            "gl_classes": ["gl0"],   # 放行集合（只做不让球；让球盘实测无边际）
            "observe_gl_classes": ["glN"],   # 观察集合：进候选/算 x，但不下注（只记录）
            "min_n": 30,             # 账本样本不足 → 回落 default_*
            "window_days": 60,       # 滚动窗口（None/0 = 全部历史）
            "default_theta": 1.1,    # 账本不足时用的 x 门限
            "default_min_legs": 3,   # 账本不足时用的最小关数
            "max_min_legs": 9,       # m_star 上限（北单最高 9 关）
            "lookback_days": 14,     # 每次结算回看几天（开奖 SP 常滞后 ~3 天，留足余量）
            "block_min_n": 200,      # 「判无边际 → 空仓」需要的最少腿数（证据不足不下结论）
            "block_min_days": 8,     # 同上，最少天数
        },
    }

    ALLOWED_TICKETS = (
        [f"{n}串1" for n in range(2, BD_MAX_COMBO_LEGS + 1)]
        + [f"{n}过{m}"
           for m in range(2, BD_MAX_COMBO_LEGS + 1)
           for n in range(m + 1, m + BD_MAX_TOLERANCE + 1)]
    )
    DECIDE_PRIORITY = ["9串1", "8串1", "7串1", "6串1", "5串1", "4串1", "3串1", "2串1"]

    def __init__(self, user: str = "bc狗", capital: float = START_CAPITAL):
        super().__init__(user=user, capital=capital)
        self._dm = DataManager()
        self._max_picks = self.MAX_PICKS
        self._parlay_cfg = None
        self._flex_plan_ticket: Optional[str] = None   # LLM 本轮指定的票型（可选）

    def _load_parlay_config(self) -> dict:
        """从角色目录 parlay.json 读取策略配置；缺失则回退类默认。

        两类键：
          - legacy 票型模板：ticket / single_legs / cover_legs / cover_mode / cover_picks
          - flex 护栏：mode / min_legs / max_legs / max_per_leg / max_combos /
            max_stake_pct / min_leg_v / min_ticket_v
        结构参数只在这里读（persona 只管策略，不重复叙述票型）。
        """
        if self._parlay_cfg is None:
            cfg = {
                "ticket": self.DEFAULT_TICKET,
                "single_legs": self.SINGLE_PICK_LEGS,
                "cover_legs": self.FULL_COVER_LEGS,
                "cover_mode": self.COVER_MODE,
                "cover_picks": self.COVER_PICKS,
                **{k: v for k, v in self.FLEX_DEFAULTS.items()},
                # 2026-09-13 新增（必须在 cfg 键集里，否则 parlay.json 写的值会被丢掉）：
                #   ticket_tolerance：规则模式票型 = N过(N−tol)，0/缺省 = N串1
                #   retire：周期性因子退役口径（只对串关狗生效，见 agent.node_factor_review）
                "ticket_tolerance": 0,
                "ticket_m": 0,          # 固定每注 M 关（自适应档位；0=关，用 ticket_tolerance）
                "retire": {},
            }
            try:
                role = self._ensure_role()
                p = role._role_dir / "parlay.json"
                if p.exists():
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        for k in cfg:
                            if k in data and data[k] is not None:
                                cfg[k] = data[k]
                        # pool_gate 子键做深合并：角色只写部分键时，其余键保留默认
                        if isinstance(data.get("pool_gate"), dict):
                            merged = dict(self.FLEX_DEFAULTS.get("pool_gate") or {})
                            merged.update(data["pool_gate"])
                            cfg["pool_gate"] = merged
            except Exception:
                pass
            self._parlay_cfg = cfg
        return self._parlay_cfg

    @staticmethod
    def _is_flex(cfg: dict) -> bool:
        return str((cfg or {}).get("mode") or "legacy").strip().lower() == "flex"

    # ═══════════════════════════════════════════
    # 数据
    # ═══════════════════════════════════════════

    def _filter_beidan_matches(self, matches: list[dict], start: str, end: str,
                               only_upcoming: bool = False,
                               as_of: Optional[str] = None) -> list[dict]:
        """去重并只保留窗口内、带 beidan_info 的北单场次。

        only_upcoming=True 时，已开赛（match_time <= 参照时刻）的场次直接排除。
        参照时刻：as_of（回测波次，如 "2026-08-22 16:30"）优先，缺省取真实当前时间。
        """
        seen: set[str] = set()
        result: list[dict] = []
        # ⚠️ 只比到「分钟」：match_time 是 19 字符（含秒），as_of 是 16 字符，
        # 直接做字符串比较时 "…20:30:00" > "…20:30" ⇒ **正好在波次整点开赛的场次会被误当成未开赛**。
        now = str(as_of or _now_bj())[:16] if only_upcoming else ""
        for m in matches or []:
            lid = m.get("lota_id") or ""
            if not m.get("beidan_info"):
                continue
            if lid in seen:
                continue
            mt = str(m.get("match_time", ""))
            if not (start <= mt <= end):
                continue
            # 已开赛（含"正好此刻开赛"）一律不进这一波：此刻已经无法再下注
            if only_upcoming and mt[:16] <= now:
                continue
            seen.add(lid)
            result.append(m)
        return result

    def _prepare_beidan_data(self, day_date: str) -> list[str]:
        """按「北单准备」流程重刷比赛缓存，并预取 compact-fet / tags。

        返回 warnings；用于 live 分析刷新拿不到比赛时兜底，避免直接 0 场。
        比赛缓存与 compact-fet 均走 DataManager 调度器（跨进程单飞），
        与竞彩狗的 prefetch 共享同一套日期锁 / 按场锁。
        """
        d = date.fromisoformat(day_date)
        start, end = get_football_day(d)
        warnings: list[str] = []
        all_matches: list[dict] = []
        for cd in football_day_calendar_dates(d):
            prep = self._dm.prepare_matches(
                cd, live=True, with_beidan_odds=True, owner=f"{self.user}:prepare"
            )
            all_matches.extend(prep.get("matches") or [])

        candidates = self._filter_beidan_matches(all_matches, start, end,
                                                 only_upcoming=True)
        if not candidates:
            warnings.append(f"{day_date} 北单数据获取失败（准备流程也未拿到比赛）")
            return warnings

        feats = self._dm.prepare_features(
            [m.get("lota_id", "") for m in candidates],
            with_tags=True, owner=f"{self.user}:prepare",
        )
        ok = feats.get("fetched", 0) + feats.get("cached", 0)
        fail = feats.get("failed", 0)
        by_lid = {m.get("lota_id", ""): m for m in candidates}
        for lid in feats.get("failed_lids", []):
            m = by_lid.get(lid) or {}
            warnings.append(
                f"{lid} {m.get('home_name', '?')} vs "
                f"{m.get('away_name', '?')} compact-fet 缺失"
            )

        print(
            f"[beidan] 准备北单流程完成：候选 {len(candidates)} 场，"
            f"compact-fet 预取 {ok}/{len(candidates)}"
        )
        if fail:
            warnings.append(f"北单 compact-fet 预取失败 {fail} 场")
        return warnings

    def _beidan_matches(self, day_date: str, live: bool = False,
                        as_of: Optional[str] = None) -> tuple[list[dict], list[str]]:
        """读取足球日窗口内、带 beidan_info 的北单场次。

        统一从 matches/<date>.json 后过滤（北单 = beidan_number 非空）；
        live 或缓存缺失 beidan_info 时按需刷新北单赔率，避免依赖过期的 beidan 历史缓存；
        live / 回测波次（as_of）额外排除该时刻已开赛的场次（已开球的不进新票）。
        """
        d = date.fromisoformat(day_date)
        start, end = get_football_day(d)
        out: list[dict] = []
        warnings: list[str] = []
        for cd in football_day_calendar_dates(d):
            if is_offline() or backtest_fet.active():
                # 离线/回测：只读本地缓存，禁联网。优先 matches/<date>.json
                # （refresh_beidan_cache 已写入最新 beidan_info 赛前赔率）；
                # 旧日 legacy beidan/<date>.json 兜底（含开奖 result/spvalue）。
                # 回测还多一层理由：线上 prepare_matches 在缓存缺 beidan_info 时会
                # 用 `/matches?date=D` 重刷 D.json，而该接口的 date 是「足球日结束日」
                # 口径（返回 D-1 的窗），会把这个足球日的比赛列表整窗刷错位。
                ms = self._dm.get_cached_beidan_matches(cd) or []
                if not ms:
                    ms = self._dm._read_legacy_beidan(cd) or []
                out.extend(ms or [])
                continue
            # 走调度器：同日期跨进程单飞 + 5 分钟新鲜窗口 + beidan_info 完整性判定；
            # 刷新返回全量比赛 → 再按北单编号过滤，避免非北单场次混入串关选项
            prep = self._dm.prepare_matches(
                cd, live=live, with_beidan_odds=True, owner=f"{self.user}:analyze"
            )
            ms = [m for m in prep.get("matches") or [] if m.get("beidan_number")]
            if not ms:
                # 迁移期 legacy beidan/<date>.json 兜底
                ms = self._dm.get_cached_beidan_matches(cd) or []
            out.extend(ms or [])

        result = self._filter_beidan_matches(out, start, end,
                                             only_upcoming=live or bool(as_of),
                                             as_of=as_of)
        if live and not result:
            warnings.append(f"{day_date} live 刷新未拿到北单比赛，转走准备北单流程获取数据")
            warnings.extend(self._prepare_beidan_data(day_date))
            cached: list[dict] = []
            for cd in football_day_calendar_dates(d):
                cached.extend(self._dm.get_cached_beidan_matches(cd) or [])
            result = self._filter_beidan_matches(cached, start, end,
                                                 only_upcoming=live or bool(as_of),
                                                 as_of=as_of)

        # 回测切片源：范围内但该波没有可用档位的场次（已开赛 / 缺档）不进分析；
        # 绝不回退到线上实时缓存（那是赛前终盘快照 = 前视）。
        if backtest_fet.active():
            src = backtest_fet.current()
            kept = [m for m in result
                    if not (src.in_scope(m.get("lota_id") or "")
                            and src.resolve(m.get("lota_id") or "") is None)]
            if len(kept) != len(result):
                print(f"  🧊 回测切片: {len(result)} 场 → {len(kept)} 场"
                      f"（{len(result) - len(kept)} 场该波无可用档位）")
            result = kept

        if not result:
            warnings.append(f"{day_date} 窗口内无北单比赛（可能缓存缺失或当天无场次）")
        return result, warnings

    def _beidan_odds(self, match: dict) -> dict:
        """北单让球胜平负赔率（goal_line + h/d/a）。

        红线：三路赔率不完整时，单边非 0 可能是赛后只保留的胜方 SP（前视泄漏），
        绝不能拿单边当唯一可选项。
        - 三路完整 → 直接使用；
        - 让 0 且三路不完整 → 用 Pinnacle 欧赔兜底三路；
        - 让球盘（goal_line != 0）三路不完整 → 返回 {}（排除，无法兜底）。
        """
        bi = match.get("beidan_info") or {}
        try:
            gl = float(bi.get("goal_line"))
        except (TypeError, ValueError):
            gl = 0.0

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        h, d, a = _f(bi.get("home_odds")), _f(bi.get("draw_odds")), _f(bi.get("away_odds"))
        if h > 0 and d > 0 and a > 0:
            return {"goal_line": gl, "h": h, "d": d, "a": a}

        if gl != 0:
            return {}  # 让球盘无完整三路赔率，无法用欧赔兜底 → 排除

        # ⚠️ 2026-09-14 **移除 Pinnacle 兜底**（这是一个静默失效的真 bug）：
        # 旧实现用 `get_odds().eu` 去水后**当成北单赔率**，而市场参考 `_market_p_hat`
        # 取的也是同一个 Pinnacle 源 ⇒ x = p̂ × (1/p̂) ≡ 1.00 **恒等**，
        # 错价检测整体退化：实测 07-05~07-13 连续 8 天 x 全为 1.00 → 无一侧 ≥ θ
        # → LLM 看不到方向 → 全部 skip → 空仓（资金 8 天不动）。
        # 三路不完整（常见于缓存里只留下胜方 SP 的场次）⇒ **排除该场**并计数告警，
        # 绝不静默用锐市场价顶上。数据补全后这些日子才能正常回放。
        try:
            self._odds_missing = int(getattr(self, "_odds_missing", 0)) + 1
        except Exception:
            pass
        if getattr(self, "_odds_missing", 0) == 1:
            print("  ⚠️ 北单三路赛前赔率缺失 → 该场排除"
                  "（不再用 Pinnacle 兜底：兜底会让 x≡1.00 静默失效）")
        return {}

    # ═══════════════════════════════════════════
    # 选腿（规则版，支持单选/双选/3选）
    # ═══════════════════════════════════════════

    def _beidan_score_leg(self, match: dict, max_picks: Optional[int] = None,
                          cover_all: bool = False,
                          fixed_picks: Optional[int] = None) -> Optional[dict]:
        bi = match.get("beidan_info") or {}
        odds = self._beidan_odds(match)
        side_odds = {
            "H": odds.get("h", 0.0),
            "D": odds.get("d", 0.0),
            "A": odds.get("a", 0.0),
        }
        side_odds = {k: v for k, v in side_odds.items() if v and v > 0}
        if not side_odds:
            return None

        # 不做任何"按赔率挑边"的排序（那是硬控，永远不用）。
        # 规则回退/手工模式没有 LLM 的方向判断可依据，因此单边/多选一律退化为"覆盖全部可投注边"，
        # 绝不因为某边赔率低就把它当"更稳"的答案；真正的单选方向只能由 LLM 给出。
        picks = list(side_odds.keys())
        if not picks:
            return None

        # 不再拿隐含概率当"置信度"；用 0/1 这类中性值表示"已选"，具体方向由 LLM 定。
        confidence = 1.0 if picks else 0.0
        return {
            "lota_id": match.get("lota_id", ""),
            "home_name": match.get("home_name", "?"),
            "away_name": match.get("away_name", "?"),
            "league_name": match.get("league_name", ""),
            "match_time": match.get("match_time", ""),
            "beidan_number": match.get("beidan_number", ""),
            "goal_line": odds.get("goal_line", 0.0),
            "picks": picks,
            "odds": {s: round(side_odds[s], 4) for s in picks},
            "confidence": confidence,
            "beidan_info": bi,
        }

    def _select_legs(self, matches: list[dict],
                     max_picks: Optional[int] = None) -> list[dict]:
        legs = []
        for m in matches:
            leg = self._beidan_score_leg(m, max_picks=max_picks)
            if leg:
                legs.append(leg)
        legs.sort(key=lambda x: -x["confidence"])
        return legs

    def _select_legs_mixed(self, matches: list[dict]) -> list[dict]:
        """默认策略（非 LLM 回退/手动）：按入场顺序取前 SINGLE_PICK_LEGS 腿做单选，
        其余 FULL_COVER_LEGS 腿按 COVER_MODE 覆盖（全包/双选），组成 DEFAULT_TICKET。
        方向/排序本应由 LLM 决定；这里仅作无 LLM 时的机械兜底，不做公平赔率排序。"""
        cfg = self._load_parlay_config()
        ranked: list[dict] = []
        for m in matches:
            top = self._beidan_score_leg(m, max_picks=1)
            if top:
                ranked.append(m)

        legs: list[dict] = []
        for m in ranked[: cfg["single_legs"]]:
            leg = self._beidan_score_leg(m, max_picks=1)
            if leg:
                legs.append(leg)
        for m in ranked[cfg["single_legs"]:
                        cfg["single_legs"] + cfg["cover_legs"]]:
            leg = self._score_cover_leg(m)
            if leg:
                legs.append(leg)
        return legs

    def _score_cover_leg(self, match: dict) -> Optional[dict]:
        """全包/双选腿：COVER_MODE="all" → 全选；"top" → 固定取前 COVER_PICKS 个。"""
        cfg = self._load_parlay_config()
        if cfg["cover_mode"] == "all":
            return self._beidan_score_leg(match, cover_all=True)
        return self._beidan_score_leg(match, fixed_picks=cfg["cover_picks"])

    def _llm_leg(self, match: dict, picks: list[str]) -> Optional[dict]:
        """按 LLM 给定的 picks 构建腿（单选/全包通用）。"""
        bi = match.get("beidan_info") or {}
        odds = self._beidan_odds(match)
        side_odds = {"H": odds.get("h", 0.0), "D": odds.get("d", 0.0),
                     "A": odds.get("a", 0.0)}
        valid = [p for p in picks if side_odds.get(p, 0.0) > 0]
        if not valid:
            return None
        # 方向由 LLM 给定，这里不再用隐含概率算"置信度"（那是邪路）。
        # 用中性值标记：这是"已选"而非"概率越高越稳"。
        confidence = 1.0
        return {
            "lota_id": match.get("lota_id", ""),
            "home_name": match.get("home_name", "?"),
            "away_name": match.get("away_name", "?"),
            "league_name": match.get("league_name", ""),
            "match_time": match.get("match_time", ""),
            "beidan_number": match.get("beidan_number", ""),
            "goal_line": odds.get("goal_line", 0.0),
            "picks": valid,
            "odds": {p: round(side_odds[p], 4) for p in valid},
            "confidence": confidence,
            "beidan_info": bi,
            "llm": True,
        }

    def _direction_factor_text(self, role: Role, as_of=None) -> str:
        """方向型因子文本：只取 selected_active 的 main（含顺向 pos / 反向 neg）。

        用途：单选腿。方向型因子给出明确可下注方向（主/平/客），
        才是决定「单选往哪边打」的依据；赔率只是参考。
        """
        try:
            role.memory.factors.load()
        except Exception:
            return ""
        main, _, _, _ = role.memory.factors.selected_active(as_of)
        pos = [x for x in main if x[2].get("sign") == "pos"]
        neg = [x for x in main if x[2].get("sign") == "neg"]
        lines = []
        if pos:
            lines.append("📈 方向型·顺向因子（给出明确方向，单选候选）:")
            for fid, s0, p0 in pos:
                desc = s0.get("desc", "")
                if p0.get("axis_kind") in ("direction", "directional"):
                    # 两轴因子：判据是「命中率 − 市场 p̂」，不是注单盈亏
                    lines.append(f"  ▲ {fid} [方向边际 {p0['axis_edge_pp']:+.1f}pp"
                                 f"（累计 {p0['n']:g} 腿，命中 {p0['hits']:g}）]")
                    if desc:
                        lines.append(f"     {desc[:70]}")
                    continue
                score = float(p0.get("rank_score") or p0.get("w_return") or 0)
                lines.append(
                    f"  ▲ {fid} [近{p0['n']}单 命中{p0['hits']:g}/{p0['n']} "
                    f"收缩命中{p0['shrunk_rate']:.0%} 归一化{score:+.2f} "
                    f"加权回报{p0['w_return']:+.2f}]"
                )
                if desc:
                    lines.append(f"     {desc[:70]}")
        if neg:
            lines.append("🔄 方向型·反向因子（负回报，反买/规避信号 → 一律规避，走 skip）:")
            for fid, s0, p0 in neg:
                desc = s0.get("desc", "")
                if p0.get("axis_kind") in ("direction", "directional"):
                    lines.append(f"  ▼ {fid} [方向边际 {p0['axis_edge_pp']:+.1f}pp"
                                 f"（累计 {p0['n']:g} 腿，命中 {p0['hits']:g}）]")
                    if desc:
                        lines.append(f"     {desc[:70]}")
                    continue
                score = float(p0.get("rank_score") or p0.get("w_return") or 0)
                lines.append(
                    f"  ▼ {fid} [近{p0['n']}单 命中{p0['hits']:g}/{p0['n']} "
                    f"归一化{score:+.2f} 加权回报{p0['w_return']:+.2f}]"
                )
                if desc:
                    lines.append(f"     {desc[:70]}")
        return "\n".join(lines)

    def _self_factor_slugs(self, role: Role, as_of=None) -> list[str]:
        """bc狗 长上下文：把所有活跃方向因子的 slugs 都纳入每场数据段。"""
        slugs: list[str] = []
        try:
            role.memory.factors.load()
            main, _, _, _ = role.memory.factors.selected_active(
                as_of)
            for fid, sdata, _ in main:
                fac_id = sdata.get("fac_id") or role.memory.factors.fac_id_for(fid)
                for s in (sdata.get("slugs") or role.memory.factors._load_slugs(fac_id)):
                    if s not in slugs:
                        slugs.append(s)
        except Exception:
            pass
        return slugs

    def _volatility_factor_text(self, role: Role, as_of=None) -> str:
        """波动型因子文本：只取 selected_active 的 volatility。

        **定位是粗筛，不是计算器**：它只回答"今天这些场次像不像会出高波动结果"，
        用来决定哪几条腿要多选/覆盖；不负责精确概率（那是方向路径与引擎门的事）。
        展示口径：命中场次的高波动率 vs 当日基线（差值 pp），比"均SP"更能说明筛选力。
        """
        try:
            role.memory.factors.load()
        except Exception:
            return ""
        _, _, volatility, _ = role.memory.factors.selected_active(
            as_of, include_volatility=True
        )
        if not volatility:
            return ""
        lines = ["🌊 波动型因子（**只用于排序**：判断这条腿的赛前赔率能不能兑现；"
                 "不用于定方向——买哪一侧只由 x 决定）:"]
        for fid, s0, p0 in volatility:
            edge = p0.get("vol_edge")
            prec = p0.get("vol_precision")
            base = p0.get("vol_base")
            if p0.get("axis_delta") is not None:
                stat = (f"兑现 {p0['axis_ratio']:.2f} vs 当日基线 {p0['axis_base']:.2f}"
                        f"（{p0['axis_delta']:+.2f}，{p0.get('axis_samples_n', 0)} 天）"
                        f" ⇒ {'排序靠前' if p0['axis_delta'] > 0 else '排序靠后'}")
            elif edge is not None and prec is not None and base is not None:
                stat = (f"命中场次高波动率 {float(prec):.0%}"
                        f"（当日基线 {float(base):.0%}，{float(edge) * 100:+.0f}pp）")
            else:
                avg_sp = p0.get("avg_sp")
                stat = (f"均SP {avg_sp:.2f}" if avg_sp
                        else f"加权回报{p0['w_return']:+.2f}")
            desc = s0.get("desc", "")
            lines.append(f"  ⚡ {fid} [近{p0['n']}单 {stat}]")
            if desc:
                lines.append(f"     {desc[:70]}")
        return "\n".join(lines)

    def _select_legs_llm(self, matches: list[dict], day_date: str
                         ) -> tuple[Optional[list[dict]], Optional[list[dict]]]:
        """LLM 分析（对齐 agent.py 的 build_prompt→call_llm→parse 写法，
        不修改 agent.py）：system 放目标/决策要求/人设/场次，user 放一句指令。

        返回 (singles, covers)。(None, None) 表示 LLM 失败（由调用方回退规则）。"""
        import json as _json
        from .providers.deepseek import DeepSeekProvider
        from .prompt_builder import count_tokens

        role = self._ensure_role()
        provider = self._runtime().provider
        if provider is None:
            try:
                self.set_provider(DeepSeekProvider())
                provider = self._runtime().provider
            except Exception as e:
                print(f"  ⚠️ LLM 不可用: {e}")
                return None, None
        if not matches:
            return [], []

        valid_matches = []
        for m in matches:
            o = self._beidan_odds(m)
            if o:
                m["_beidan_odds"] = o
                valid_matches.append(m)
        matches = valid_matches
        cand_map = {m.get("lota_id"): m for m in matches if m.get("lota_id")}

        def _gl(g) -> str:
            try:
                gl = float(g)
            except (TypeError, ValueError):
                gl = 0.0
            if gl > 0:
                return f"goal_line=+{gl:g}（主队受让{gl:g}球）"
            if gl < 0:
                return f"goal_line={gl:g}（主队让{abs(gl):g}球）"
            return "goal_line=0（平手）"

        as_of = None
        if day_date:
            try:
                from datetime import datetime as _dt
                as_of = _dt.strptime(day_date, "%Y-%m-%d")
            except ValueError:
                as_of = None

        dir_factor_text = self._direction_factor_text(role, as_of)
        vol_factor_text = self._volatility_factor_text(role, as_of)
        factor_slugs = self._self_factor_slugs(role, as_of)
        persona = role.persona_text() or "(未设人设)"
        cfg = self._load_parlay_config()
        # ⚠️ 这些是 **legacy** 票型模板的键；flex 狗没有它们也不能崩（历史上会 KeyError）
        n_legs = int(cfg.get("single_legs") or 0) + int(cfg.get("cover_legs") or 0)
        if str(cfg.get("cover_mode") or "all") == "all":
            cover_desc = "全包(H/D/A)"
            cover_note = "每场 H/D/A 全选，保证这 {n} 场必中".format(
                n=cfg.get("cover_legs") or 0)
        else:
            cover_desc = "双选" if int(cfg.get("cover_picks") or 3) == 2 else "三选"
            cover_note = "每场取前 {n} 个高概率方向".format(n=cfg.get("cover_picks") or 3)
        combo_count = int(cfg.get("cover_picks") or 1) ** int(cfg.get("cover_legs") or 0)
        dir_factor_block = ""
        if dir_factor_text:
            dir_factor_block = (
                "## 📌 方向型因子（仅供定「单选」方向；赔率低≠更稳，方向由这些因子+离散/资金/盘口一致决定）\n"
                + dir_factor_text + "\n"
            )
        vol_factor_block = ""
        if vol_factor_text:
            vol_factor_block = (
                "## 🛡️ 波动型因子（**只用于排序**：判断该腿赛前赔率能不能兑现；不用于定方向）\n"
                + vol_factor_text + "\n"
            )
        factor_cue_block = (
            "## 🧭 因子用法（重要，按用途分工，不要混用）\n"
            "- **方向型因子**：用来判断「引擎认的那一侧，你同意还是否决」——它给出明确方向"
            "（主/客/平某一侧），且该方向需离散/资金/盘口同向；赔率只是参考，不因低赔就默认它是答案。\n"
            "- ⚠️ **赔率低不等于更稳**：北单水位很低的场次往往只是市场一致看多、回报极薄，"
            "属于「便宜但无信息」——它不该成为你同意的理由。\n"
            "- **波动型因子 → 正向信号**：它标出「池子错价更大」的场次（= 更该买的腿），"
            "用来**支持你同意**，不是否决理由。\n"
            "  ⚠️ 本票型**每场只买一侧，没有全包/覆盖腿**；买哪一侧只由 x 决定。\n"
            "- 🔄 方向型里的**反向（负回报）**因子 → 一律规避（同样走 skip），不要硬选一个方向。\n"
            "- 判断依据是「它给你一个方向，还是只提醒你小心」。**读描述语义判断，不要按因子名套用**。\n\n"
            # ── 结构验证（2026-09-15 用户口径）：把单关狗「因子 → 三步验证 → 下单」的联动
            # 移植到北单。引擎已按 x 硬筛 ⇒ 复述 x 没有增量，必须在门内做结构分层。
            "## 🧱 结构验证（每条腿进票前必做 · 与因子联动）\n"
            "引擎已按 `x ≥ 1.11371`（= (1/0.65)^(1/4)，**4 关票腿级线**；整票打平要 Π(SP·p̂)>1/0.65）"
            "硬筛过候选。**你的任务不是复述 x**，而是在门内分层：哪些结构条件下的腿更值得进 4 关票。\n"
            "逐个维度看原始数据段（不是看因子名）：\n"
            "1. **盘口**：亚盘 Crown / Pinnacle 首末行对比 → 升盘（让球线加大）/ 退盘 / 盘口不动；深盘还是浅盘。\n"
            "2. **水位**：我方水位是低水（≤0.90，受保护）还是高水（≥1.02，疑似诱盘）。\n"
            "3. **赔率与资金**：Pinnacle 欧赔首末行 → 我方赔率下沉 / 上升；必发（盈亏指数、市场指数、凯利、成交量）方向。\n"
            "4. **离散**：我方离散是全场最凝聚还是最发散；凝聚度是否单边极致（≥5x）。\n"
            "5. **实力/进球**：主客实力差、预期进球和（<2.6 小球 / >3.2 大球）。\n"
            "判定（基于 55 天腿池实测，门内基线 y=1.312）：\n"
            "- 🔻 **降级/skip**：升盘；我方高水；欧赔上升且我方最凝聚（赔升防冷）；我方=最凝聚侧。"
            "（这几类实测 y ≤ 1.22，跌回腿级线附近或以下）\n"
            "- 🔺 **升级**：我方=最高赔（冷门侧，y=1.451）；我方=最发散侧（1.411）；预期进球和<2.6（1.374）；"
            "盘口不动（1.337）；欧赔下沉（1.326）；主队水位走低（1.374）。\n"
            "- **rank 规则**：结构升级 + 弱侧（市场 p̂ 低）排最前；结构降级排最后或直接 skip；"
            "同档内仍按 x 从高到低。\n"
            "- 每条腿的 `why` 必须写出**结构证据**（盘口/水位/离散/资金 各一句），只写「x 高」不算理由。\n\n"
        )

        # Stage1：分块初筛，batch=20，逐场输出 {摘要, 推荐(H/D/A/高波动/skip), factors}
        BATCH_SIZE = 20
        match_blocks: list[tuple[dict, str, str]] = []
        for m in matches:
            o = m["_beidan_odds"]
            sec = self._match_sections_text(m, budget=None,
                                            extra_slugs=factor_slugs,
                                            source_order=True)   # 忠实按原始 txt 的段落顺序
            x_txt = ""
            _src_n, _pm = self._market_p_hat(m)
            if _pm:
                _xs = {s2: float(_pm.get(s2, 0)) * float(o.get(k) or 0)
                       for s2, k in (("H", "h"), ("D", "d"), ("A", "a"))}
                x_txt = (" 错价倍数x(H/D/A)="
                         + "/".join(f"{_xs[s2]:.2f}" for s2 in ("H", "D", "A"))
                         + f"（x = 市场p̂×北单赔率，>1 表示奖池比锐市场便宜；来源 {_src_n}）")
            info = (
                f"- {m.get('lota_id')} | {m.get('home_name')} vs {m.get('away_name')} "
                f"[{m.get('league_name', '')}] {_gl(o.get('goal_line'))} "
                f"赔率H/D/A={o.get('h')}/{o.get('d')}/{o.get('a')}{x_txt}"
            )
            match_blocks.append((m, info, sec))
        self._assert_prompt_redacted("\n".join(x[1] for x in match_blocks))

        # 两轴因子：把当天【过门腿 + 因子命中标记】落盘（只含赛前信息，结算后再拼结果）
        try:
            n_ax = self._save_axis_legs(role, day_date, matches, as_of)
            if n_ax:
                print(f"  🧱 两轴因子：落盘 {n_ax} 条过门腿的命中标记")
        except Exception as e:
            print(f"  ⚠️ 两轴腿标记落盘失败（不影响分析）: {e}")

        stage1_items: list[dict] = []

        def _run_batch(batch_no: int, chunk: list[tuple[dict, str, str]]) -> list[dict]:
            chunk_text = "\n".join(
                info + "\n   因子数据段:\n" + sec for _, info, sec in chunk
            )
            prompt_stage1 = f"""你是北单串关分析 agent（{role.name}）的初筛器（stage1 · 逐场判断器）。
数据段是**全量原文**（不给你摘要），一次 {len(chunk)} 场。你只做逐场判断，不做组票。

## 人设（策略唯一来源，按它执行）
{persona}

## 因子清单（按用途分开，禁止混用）
{dir_factor_block}
{vol_factor_block}

## 本场的判据（重要）
- **方向已经由引擎定价给出**：每场行尾有 `错价倍数x(H/D/A)=…`，
  `x = 市场p̂ × 北单赛前赔率`，**x 最高且 >1 的那一侧就是引擎认定的"奖池定便宜了"的一侧**。
  你的任务不是猜比分，而是判断：**引擎认的这一侧，你是同意、还是否决**。
- 让球线语义（参考信息，不是纪律）：H/D/A 都是**让球后**的结果——goal_line=-1 时"主胜"= 主胜 2 球以上；
  goal_line=+1 时"主胜"= 主不败（平或胜）。赔率已是该盘口的赛前赔率，直接用。
  **盘口类型只是产品信息**：买不买由引擎的门决定，你不要因为盘口类型去 skip。
- **同意**：没有信息层面的反证 → `推荐` 填那一侧（或你认为更好的同盘口一侧，但要写理由）。
- **否决**：只允许**信息层面**的反证（伤停/阵容/轮换/盘口异动/奖池快照过期/波动风险）→ `推荐="skip"` + `veto`。
  波动型因子是**正向信号**：它标出「池子错价更大」的场次（= 更该买的那类），
  用来**加强你的同意**，**不能用来选方向、也不是否决理由**。
- 不要因为"方向不明确"或"盘口类型"就 skip：方向由 x 决定，你只负责有没有信息层面的反证。
- ⚠️ **唯一例外 —— 该场没有任何一侧 x > 1**（引擎没认定"池子便宜"的侧）：
  这时**没有可买的方向**，你可以直接 `推荐="skip"` 并在 `veto` 写明"无过门侧（x≤1）"。
  这是引擎层面的无方向，不是你方向不明确，**不算违规**；引擎本来也不会买该场。

## 输出
只输出 JSON，每场一个对象：
{{"items":[{{"lota_id":"Lota...","rank":1,"推荐":"H|D|A|skip","factors":["命中的因子名"],"veto":"否决理由，没有就空串","extra_picks":["H|D|A"]}}]}}
- **`rank`（必填）＝ 你给的优先级，1 最高**。数组也请按 rank 升序排。
- **排序规则（重要，引擎按 rank 取前 N 条腿）**：
  1. **波动型因子与方向型因子「同向触发」的场次排最前**（= 池子错价更大 + 方向明确，双重确认）；
  2. 其次：只方向型因子正向触发的场次；
  3. 再其次：无因子命中、但无信息层面反证的场次；
  4. 最后：被否决（`推荐="skip"`）的场次。
  同一档内按 x 从高到低排。`skip` 的场次也照常输出并给 `rank`（引擎会跳过它们）。
- `factors`：只能填上面因子清单里出现过的名字，没有命中就空数组。
- `extra_picks`（可选）：**只有当你认为这场除了过门侧之外还值得再多买一个方向时**才填。
  引擎会把它们并进这条腿，并按**被选侧的平均 x** 判门——摊薄到门限以下就自动丢弃（所以别乱填）。
不要编 lota_id，不要输出其它文字。"""
            stage1_user = (
                f"以下为待判断比赛（每场含全量数据段）：\n\n{chunk_text}\n\n"
                "逐场给出「同意/否决 + 归因」；只输出 JSON。"
            )
            try:
                resp = provider.call(
                    prompt_stage1,
                    [{"role": "user", "content": stage1_user}],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
            except Exception as e:
                print(f"  ⚠️ stage1 LLM 失败（batch {batch_no}）: {e}")
                raise
            if not resp:
                raise RuntimeError(f"stage1 batch {batch_no} 空响应")
            try:
                data = _json.loads(self._extract_json(str(resp)))
            except Exception as e:
                print(f"  ⚠️ stage1 返回非 JSON（batch {batch_no}）: {e}")
                raise
            out: list[dict] = []
            for it in (data.get("items") or []):
                if isinstance(it, dict) and it.get("lota_id") in cand_map:
                    it["推荐"] = str(it.get("推荐") or "skip").upper()
                    factors = it.get("factors") or []
                    it["factors"] = [str(f) for f in factors] if isinstance(factors, list) else []
                    it["veto"] = str(it.get("veto") or "").strip()[:120]
                    ex = it.get("extra_picks") or []
                    it["extra_picks"] = [str(s2).upper() for s2 in ex
                                         if str(s2).upper() in ("H", "D", "A")] \
                        if isinstance(ex, list) else []
                    it.pop("conf", None)      # conf 无信息量（真跑里 12/12 全 0）→ 已删
                    out.append(it)
            print(f"    [stage1] batch {batch_no} 完成，输出 {len(out)} 条", flush=True)
            # ⚠️ **每个 batch 的原始响应都要落盘**（2026-09-14 修复）：
            # 此前只把**最后一个 batch** 交给 session logger，batch 1/2 的响应
            # 全沙箱无落盘（实测 grep 'batch 1/3' = 0 命中）⇒ stage1 审计/rank 对照
            # 只能看到 1/3 的样本（10/50、5/45、14/54），无法复原腿序决策。
            try:
                _rd = pathlib.Path(self._ensure_role()._role_dir) / "memory"
                _rd.mkdir(parents=True, exist_ok=True)
                (_rd / f"stage1_resp_{day_date}_b{batch_no}.json").write_text(
                    json.dumps({"day": day_date, "batch": batch_no,
                                "n_items": len(out), "response": resp},
                               ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
            except Exception as e:
                print(f"  ⚠️ stage1 批次落盘失败（b{batch_no}）: "
                      f"{type(e).__name__}: {e}")
            display_prompt = (
                prompt_stage1
                + "\n\n## 本块场次\n"
                + "\n".join(info for _, info, _ in chunk)
            )
            features = {c[0].get("lota_id"): c[2] for c in chunk}
            return out, {
                "batch_no": batch_no,
                "prompt": display_prompt,
                "response": resp,
                "tokens_in": count_tokens(prompt_stage1 + stage1_user),
                "tokens_out": count_tokens(resp),
                "match_features": features,
            }

        batches = [
            match_blocks[i:i + BATCH_SIZE]
            for i in range(0, len(match_blocks), BATCH_SIZE)
        ]
        print(f"  [stage1] 共 {len(batches)} 块，batch_size={BATCH_SIZE}，4 worker 并行",
              flush=True)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = {
                    ex.submit(_run_batch, idx + 1, b): idx
                    for idx, b in enumerate(batches)
                }
                results: dict[int, tuple[list[dict], dict]] = {}
                for fut in as_completed(futures):
                    results[futures[fut]] = fut.result()
        except Exception as e:
            print(f"  ⚠️ stage1 并行分析失败: {e}")
            return None, None

        rt = self._runtime()
        for idx in sorted(results):
            items, log_info = results[idx]
            stage1_items.extend(items)
            # ⚠️ 2026-09-16 修复：这段日志原先写在循环**外面**，用的是循环变量泄漏出来的
            # `log_info`（永远只剩最后一块）⇒ 只有最后一块的 prompt/response/逐场特征
            # 落进 session，前面的块全丢档（实测 09-16 两块 20+13，sessions 下只有 13 个
            # Lota*.json = 第二块，analyze md 里也只有「batch 2/2」）。
            # 每块的调用必须各自落一条。
            try:
                if rt and rt.session:
                    rt.session.llm_call(
                        log_info["prompt"],
                        log_info["response"],
                        tokens_in=log_info["tokens_in"],
                        tokens_out=log_info["tokens_out"],
                        token_breakdown={
                            "data": log_info["tokens_in"],
                            "sys": 0,
                            "mem": 0,
                            "tools": 0,
                            "user": 0,
                        },
                        match_features=log_info["match_features"],
                        label=f"stage1 batch {log_info['batch_no']}/{len(batches)}",
                    )
            except Exception:
                pass
        # LLM 决策决定腿序（2026-09-14）：`rank` 由 stage1 给出，规则见 prompt 的
        # 「排序规则」——波动型+方向型同向触发排最前。引擎不自己打分，只尊重 rank。
        # 缺 rank 的保持原相对顺序（稳定排序），排在有 rank 的后面。
        # 排序前的**输入序**（stage1 呈现顺序）→ 供 A/B 的"输入序"臂复原
        _input_idx = {it.get("lota_id"): i for i, it in enumerate(stage1_items)}

        def _rank_of(it: dict):
            """rank 可能是数字也可能是字符串（LLM 两种都会给）—— 统一转 float。"""
            try:
                return float(str(it.get("rank")).strip())
            except (TypeError, ValueError):
                return None
        if any(_rank_of(it) is not None for it in stage1_items):
            stage1_items.sort(key=lambda it: (0, _rank_of(it))
                              if _rank_of(it) is not None else (1, 0.0))
        if rt.session:
            rt.session.tool_call(
                "stage1",
                {
                    "batches": len(batches),
                    "workers": 4,
                    "items": len(stage1_items),
                },
                f"{len(stage1_items)} 条初筛结果",
            )

        if not stage1_items:
            return [], []

        # flex 模式：stage1 之后改走「腿集」决策
        if self._is_flex(cfg):
            # ticket_mode="rule"：**不再调用 stage2 LLM**，引擎按规则组装腿集
            if str(cfg.get("ticket_mode") or "llm").strip().lower() == "rule":
                return self._assemble_legs_rule(stage1_items, cand_map, cfg, day_date), []
            flex_legs = self._stage2_flex(
                matches=matches, cand_map=cand_map, stage1_items=stage1_items,
                cfg=cfg, role=role, provider=provider, persona=persona,
                factor_slugs=factor_slugs, dir_factor_text=dir_factor_text,
                vol_factor_text=vol_factor_text, day_date=day_date,
            )
            # 约定：flex 下 singles = 腿集（每腿自带 picks，可多选），covers 恒为空
            return (flex_legs, []) if flex_legs is not None else (None, None)

        print(f"  [stage2] 拆分：单腿 LLM 主观排序 + 波动腿波动性排序", flush=True)
        single_items = [it for it in stage1_items if it.get("推荐") in ("H", "D", "A")]
        vol_items = [it for it in stage1_items if it.get("推荐") in ("高波动", "skip")]
        single_rec = {it.get("lota_id"): it.get("推荐") for it in single_items}

        singles: list[dict] = []
        covers: list[dict] = []
        seen: set[str] = set()

        # stage2a：单腿只用数据 + 非波动因子做 LLM 主观排序
        if single_items:
            single_text_parts = []
            for it in single_items:
                m = cand_map.get(it.get("lota_id"))
                if not m:
                    continue
                sec = self._match_sections_text(m, budget=None,
                                                extra_slugs=factor_slugs)
                o = m.get("_beidan_odds") or self._beidan_odds(m)
                info = (
                    f"- {m.get('lota_id')} | {m.get('home_name')} vs {m.get('away_name')} "
                    f"[{m.get('league_name', '')}] {_gl(o.get('goal_line'))} "
                    f"赔率H/D/A={o.get('h')}/{o.get('d')}/{o.get('a')}"
                )
                single_text_parts.append(info + "\n   因子数据段:\n" + sec)
            single_text = "\n".join(single_text_parts)
            prompt_singles = f"""你是北单串关分析 agent（{role.name}）。只做逐场同意/否决，不组票、不决定票型。

## 人设
{persona}

## 单选决策
下面给出 stage1 初筛出的单选候选（推荐 H/D/A，含完整原始数据）。请从数据 + 方向因子出发，
判断哪些方向真正站得住，并按信心排序，最多 {cfg['single_legs']} 条，宁缺毋滥。
反向因子只做排除/降级，不参与正向排序。
H/D/A 定义：H=让球后主胜，D=让球后平，A=让球后客胜。goal_line 是主队让球线（正=主队受让，负=主队让球）；候选里的 H/D/A 是赛前赔率（不是开奖SP），已按 goal_line 折算，直接按给出的方向判断。

{_GL_TRANSLATION_RULE}

{dir_factor_block}
{factor_cue_block}

## 单选候选数据
{single_text}

只输出 JSON：
{{"singles":[{{"lota_id":"Lota...","pick":"H"}}], "empty":false}}
数量不足就少列；完全无方向才 empty:true。不要输出 covers。"""
            singles_user = "根据上述单选候选输出单选排序 JSON。"
            try:
                resp = provider.call(
                    prompt_singles,
                    [{"role": "user", "content": singles_user}],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
            except Exception as e:
                print(f"  ⚠️ stage2 单选 LLM 失败: {e}")
                return None, None
            if not resp:
                return None, None
            try:
                rt = self._runtime()
                if rt.session:
                    rt.session.llm_call(
                        prompt_singles,
                        resp,
                        tokens_in=count_tokens(prompt_singles),
                        tokens_out=count_tokens(resp),
                        token_breakdown={
                            "data": count_tokens(single_text),
                            "sys": 0, "mem": 0, "tools": 0, "user": 0,
                        },
                        label="stage2 单选排序",
                    )
            except Exception:
                pass
            try:
                data = _json.loads(self._extract_json(str(resp)))
            except Exception as e:
                print(f"  ⚠️ stage2 单选非 JSON: {e}")
                return None, None
            if not data.get("empty"):
                for item in (data.get("singles") or []):
                    if not isinstance(item, dict):
                        continue
                    lid = item.get("lota_id")
                    pick = single_rec.get(lid)
                    if pick not in ("H", "D", "A"):
                        continue
                    m = cand_map.get(lid)
                    if not m or lid in seen:
                        continue
                    leg = self._llm_leg(m, [pick])
                    if leg:
                        seen.add(lid)
                        singles.append(leg)

        # stage2b：波动腿用波动因子 avg_sp/w_return 排序，覆盖腿保持原逻辑
        from .factor_select import factor_profile as _factor_profile
        try:
            role.memory.factors.load()
            factor_stats = getattr(role.memory.factors, "factor_perf", {}) or {}
        except Exception:
            factor_stats = {}
        as_of_dt = None
        if day_date:
            try:
                from datetime import datetime as _dt2
                as_of_dt = _dt2.strptime(day_date, "%Y-%m-%d")
            except ValueError:
                as_of_dt = None
        vol_prof: dict[str, dict] = {}
        for key, s in factor_stats.items():
            try:
                p = _factor_profile(s, now=as_of_dt)
            except Exception:
                p = None
            if not p or p.get("factor_type") != "volatility":
                continue
            fac = s.get("fac_id") or ("fac_" + key)
            vol_prof[fac] = p
            vol_prof[key] = p

        def _vol_score(item: dict) -> float:
            best = 0.0
            for f in (item.get("factors") or []):
                p = vol_prof.get(f)
                if p:
                    sp = p.get("avg_sp")
                    best = max(best, float(sp) if sp else float(p.get("w_return", 0.0) or 0.0))
            return best

        vol_candidates = [(it, _vol_score(it)) for it in vol_items if it.get("lota_id") not in seen]
        vol_candidates.sort(key=lambda x: -x[1])
        for it, _score in vol_candidates[: int(cfg["cover_legs"])]:
            m = cand_map.get(it.get("lota_id"))
            if not m or it.get("lota_id") in seen:
                continue
            leg = self._score_cover_leg(m)
            if leg:
                seen.add(it.get("lota_id"))
                covers.append(leg)

        rt = self._runtime()
        if rt.session:
            rt.session.tool_call(
                "stage2",
                {
                    "singles": len(singles),
                    "covers": len(covers),
                },
                f"单选 {len(singles)} 条 / 覆盖 {len(covers)} 条",
            )
        return singles, covers

    # ═══════════════════════════════════════════
    # flex 模式：腿集决策 + 边际/护栏（北单 65% 一次性返奖）
    # ═══════════════════════════════════════════
    #
    # 数学（人设与引擎共用同一口径）：
    #   单腿边际 v̂ = mean(Σ_{s∈S} p̂_s × 赛前赔率_s)     ← S = 该腿买的 directions
    #   整票 ROI   = 0.65 × Π v̂ − 1                    ← 0.65 只乘一次，与串长无关
    #   打平需要 Π v̂ > 1/0.65 = 1.538
    # 多选腿拿的是「所选方向的平均边际」：全包 = 买市场本身（≈1.00），只换命中率。

    @staticmethod
    def _gl_text(g) -> str:
        try:
            gl = float(g)
        except (TypeError, ValueError):
            gl = 0.0
        if gl > 0:
            return f"goal_line=+{gl:g}（主队受让{gl:g}球）"
        if gl < 0:
            return f"goal_line={gl:g}（主队让{abs(gl):g}球）"
        return "goal_line=0（平手）"

    @staticmethod
    def _market_p(side_odds: dict) -> dict:
        """北单赛前赔率 → 去水市场隐含概率（缺 p̂ 时的兜底：无观点 = 市场价）。"""
        inv = {s: 1.0 / float(v) for s, v in (side_odds or {}).items()
               if v and float(v) > 0}
        tot = sum(inv.values())
        return {s: v / tot for s, v in inv.items()} if tot > 0 else {}

    @staticmethod
    def _leg_marginal(leg: dict) -> Optional[float]:
        """单腿边际 v̂ = mean(被选方向的 p̂_s × 赔率_s)。

        口径（2026-09-11 用 1764 场已开奖样本核过）：
          北单赛前赔率/开奖SP 是**公平赔率**（Σ1/赔率 中位 1.002，SP≈赛前赔率），
          65% 抽水只在结算时乘一次 →「p̂ = 市场概率」时 v̂ ≈ 1.000。
        未给 p̂ 的方向按**中性值**计（= 市场本身，v̂≈1.0），不给白送边际；
        整腿一个 p̂ 都没给 → 返回 None（无观点 = 不进票）。
        """
        odds = leg.get("odds") or {}
        p_hat = leg.get("p_hat") or {}
        sides = [s for s in (leg.get("picks") or []) if odds.get(s)]
        if not sides:
            return None
        full = leg.get("odds_all") or odds
        inv_sum = sum(1.0 / float(v) for v in full.values() if v and float(v) > 0)
        neutral = (1.0 / inv_sum) if inv_sum > 0 else 1.0
        vals: list[float] = []
        stated = 0
        for s in sides:
            ps = p_hat.get(s)
            try:
                ps = float(ps)
            except (TypeError, ValueError):
                ps = 0.0
            if 0.0 < ps < 1.0:
                stated += 1
                vals.append(ps * float(odds[s]))
            else:
                vals.append(neutral)
        if stated == 0:
            return None
        return sum(vals) / len(vals)

    def _flex_leg(self, match: dict, picks: list[str],
                  p_hat: dict, p_mkt: Optional[dict] = None,
                  allowance: float = 0.0, k_cal: float = 1.0) -> Optional[dict]:
        leg = self._llm_leg(match, picks)
        if not leg:
            return None
        o = match.get("_beidan_odds") or self._beidan_odds(match) or {}
        leg["odds_all"] = {s: float(o.get(k) or 0.0)
                           for s, k in (("H", "h"), ("D", "d"), ("A", "a"))}
        raw = {}
        for s in leg["picks"]:
            try:
                raw[s] = float((p_hat or {}).get(s))
            except (TypeError, ValueError):
                raw[s] = None
        claim = {s: v for s, v in raw.items() if v is not None}
        # ① 方向路径校准：按因子历史（声称概率 vs 实际命中）压低/放大自报概率
        k_cal = float(k_cal or 1.0)
        if k_cal != 1.0:
            for s, ps in list(raw.items()):
                if ps is not None:
                    raw[s] = min(0.999, max(0.001, ps * k_cal))
        # ② 市场封顶：LLM 可以比市场更看好某侧，但最多 market_allowance 倍
        clamped: list[str] = []
        if p_mkt and allowance and allowance > 0:
            for s, ps in list(raw.items()):
                pm = p_mkt.get(s)
                if ps is None or not pm or pm <= 0:
                    continue
                cap = float(pm) * float(allowance)
                if ps > cap:
                    raw[s] = cap
                    clamped.append(s)
        leg["p_hat"] = {s: v for s, v in raw.items() if v is not None}
        leg["p_claim"] = claim
        leg["k_cal"] = round(k_cal, 4)
        if p_mkt:
            leg["p_mkt"] = {s: float(p_mkt[s]) for s in leg["picks"] if p_mkt.get(s)}
        # 奖池错价比率 x = 市场 p̂ × 北单赛前赔率（被选方向的均值；拿不到 → None）
        xs = []
        for s in leg["picks"]:
            pm = (p_mkt or {}).get(s)
            o_s = float((leg.get("odds_all") or {}).get(s) or 0.0)
            if pm and o_s > 0:
                xs.append(float(pm) * o_s)
        leg["x_mkt"] = round(sum(xs) / len(xs), 4) if xs else None
        leg["market_ref"] = bool(p_mkt)
        leg["clamped"] = clamped
        leg["leg_v"] = self._leg_marginal(leg)
        return leg

    # ── phase 2：市场 p̂（引擎侧独立于 LLM 的概率锚）──────────────

    _FAIR_LINE_RE = re.compile(
        r"(?:平均欧盘胜/平/负|竞彩胜平负)\(([-+]?\d+(?:\.\d+)?)\)\s*[:：]\s*"
        r"([\d.]+)\s*/\s*([\d.]+)\s*/\s*([\d.]+)")
    _BETFAIR_PRICE_RE = re.compile(
        r"价位\(主/和/客\)\s*[:：]\s*([\d.]+)\s*/\s*([\d.]+)\s*/\s*([\d.]+)")
    _PINNACLE_TRIPLE_RE = re.compile(r"([\d.]+)\s*/\s*([\d.]+)\s*/\s*([\d.]+)")

    @classmethod
    def _pinnacle_hda(cls, secs: dict) -> Optional[list[float]]:
        """锐市场 1X2（Pinnacle）三路赔率：取 `eu-odds-pinnacle` 段最后一行（最接近开赛）。"""
        txt = (secs or {}).get("eu-odds-pinnacle") or ""
        if not isinstance(txt, str):
            return None
        trips = [t for t in cls._PINNACLE_TRIPLE_RE.findall(txt)
                 if all(float(v) > 1.0 for v in t)]
        return [float(v) for v in trips[-1]] if trips else None

    @staticmethod
    def _devig(odds: dict) -> dict:
        inv = {s: 1.0 / float(v) for s, v in (odds or {}).items()
               if v and float(v) > 0}
        tot = sum(inv.values())
        return {s: v / tot for s, v in inv.items()} if tot > 0 else {}

    def _market_ref_odds(self, match: dict,
                         tags: Optional[dict] = None) -> tuple[str, dict]:
        """取「与北单 goal_line 同一盘口」的锐市场赔率（外部独立于 LLM）。

        优先级：
          1. goal_line != 0 → 公平盘段「平均欧盘胜/平/负(<line>)」/「竞彩胜平负(<line>)」
             且 <line> == goal_line（同一让球盘口才可比）；
          2. goal_line == 0 → Pinnacle 欧赔（`extract_odds`），退化用必发「价位(主/和/客)」。
        拿不到 → ("", {})（该腿没有市场锚，由 require_market_p 决定去留）。
        """
        lid = match.get("lota_id") or ""
        if not lid:
            return "", {}
        try:
            goal_line = float((match.get("beidan_info") or {}).get("goal_line") or 0.0)
        except (TypeError, ValueError):
            goal_line = 0.0
        secs = tags if isinstance(tags, dict) else (self._dm.get_tags(lid) or {})
        fair_text = secs.get("fair-odds") or ""
        if goal_line != 0 and fair_text:
            best = None
            for m in self._FAIR_LINE_RE.finditer(fair_text):
                try:
                    line = float(m.group(1))
                except (TypeError, ValueError):
                    continue
                if abs(line - goal_line) > 1e-9:
                    continue
                odds = [float(x) for x in m.groups()[1:]]
                if min(odds) > 0:
                    best = {"H": odds[0], "D": odds[1], "A": odds[2]}
                    break
            if best:
                return "让球欧盘", best
        # ② gl≠0 且没有书商同盘口报价（主受让 gl>0 几乎都没有）→ Poisson 换算到该让球线
        if goal_line != 0:
            pin = self._pinnacle_hda(secs)
            if pin:
                from .line_convert import convert_1x2_odds
                conv = convert_1x2_odds(pin[0], pin[1], pin[2], goal_line)
                # 本函数统一返回**赔率**（调用方还会 _devig 一次）→ 把概率换成等价公平赔率 1/p
                if conv and min(conv) > 0:
                    return "Pinnacle换算(line)", {"H": 1.0 / conv[0], "D": 1.0 / conv[1],
                                                  "A": 1.0 / conv[2]}
        if goal_line == 0:
            try:
                eu = (self._dm.get_odds(lid) or {}).get("eu") or {}
            except Exception:
                eu = {}
            if all(eu.get(k) for k in ("h", "d", "a")):
                return "Pinnacle", {"H": float(eu["h"]), "D": float(eu["d"]),
                                    "A": float(eu["a"])}
            # 线上欧赔缓存拿不到 → 用当前数据段里的 Pinnacle 文本（回放里 = 该波次切片）
            pin = self._pinnacle_hda(secs)
            if pin:
                return "Pinnacle", {"H": pin[0], "D": pin[1], "A": pin[2]}
            m = self._BETFAIR_PRICE_RE.search(secs.get("betfair-eu") or "")
            if m:
                odds = [float(x) for x in m.groups()]
                if min(odds) > 0:
                    return "必发", {"H": odds[0], "D": odds[1], "A": odds[2]}
        return "", {}

    def _market_p_hat(self, match: dict,
                      tags: Optional[dict] = None) -> tuple[str, dict]:
        """市场 p̂（去水）+ 来源名；拿不到返回 ("", {})。"""
        src, odds = self._market_ref_odds(match, tags)
        if not src:
            return "", {}
        p = self._devig(odds)
        return (src, p) if len(p) == 3 else ("", {})

    @classmethod
    def _breakeven_table(cls, legs=(2, 3, 4, 5, 6, 7, 8, 10)) -> str:
        n_row = " | ".join(str(n) for n in legs)
        v_row = " | ".join(f"{cls.BEIDAN_TAKEOUT ** (1.0 / n):.3f}" for n in legs)
        sep = "|" + "---|" * (len(legs) + 1)
        return f"| 腿数 | {n_row} |\n{sep}\n| 每腿最低 v̂ | {v_row} |"

    @classmethod
    def _min_ticket_v(cls, cfg: dict, n_legs: int) -> float:
        """整票 Πv̂ 下限 = 打平线 × SP 漂移安全垫。

        北单是奖池型：下注时看到的是**当时的奖池快照（1/f 归一化）**，最终奖金由
        **赛后开奖 SP**决定。1531 场实测「开奖SP/下注时赔率」中位 1.008、p10 0.849、
        p90 1.208 → 单腿对数漂移 σ≈0.13，n 腿乘积 σ≈σ·√n。取 p25 分位作安全垫：

            min_ticket_v(n) = (1/0.65) × exp(z · σ · √n)
            2腿 1.741 / 3腿 1.790 / 5腿 1.871

        显式配置了 `min_ticket_v` 时以配置为准（跳过安全垫，给回测做对照用）。
        """
        explicit = cfg.get("min_ticket_v")
        if explicit is not None:
            return float(explicit)
        n = max(1, int(n_legs or 1))
        sigma = float(cfg.get("sp_drift_sigma") or 0.0)
        z = float(cfg.get("safety_z") or 0.0)
        return cls.BEIDAN_TAKEOUT * math.exp(z * sigma * math.sqrt(n))

    @staticmethod
    def _pool_leg_ok(leg: dict, theta: float,
                     allowed_gl: set[str]) -> tuple[bool, str]:
        """腿池门单腿判定：盘口类型必须在允许集合内，且 x ≥ θ。"""
        gl = float(leg.get("goal_line") or 0.0)
        cls = "gl0" if abs(gl) < 1e-9 else "glN"
        if cls not in allowed_gl:
            return False, f"盘口 {cls}（让球线 {gl:g}）不在允许集合 {sorted(allowed_gl)}"
        x = leg.get("x_mkt")
        if x is None:
            return False, "拿不到锐市场参考 → 算不出 x（无法证明奖池错价）"
        if float(x) < float(theta or 0.0):
            return False, (f"x={float(x):.3f} < 门限 {float(theta):.3f}"
                           "（奖池没比锐市场便宜）")
        return True, ""

    _pg_theta_hint: float = 1.1          # 仅用于 prompt 里给候选打「x≥θ」标记

    def _select_legs_x_top(self, matches: list[dict], cfg: dict,
                           as_of: Optional[str] = None) -> list[dict]:
        """0-LLM 规则臂：账本门筛腿（盘口 + x ≥ θ）→ 按 x 降序取前 N 条，p̂ = 市场 p̂。

        * 每腿的 v̂ = p̂(市场) × 赔率 = x ⇒ 整票 Πv̂ = Πx，ROI 门变成纯确定性判断；
        * 不做多选（每腿只买 x 最高的那一侧）、不调 LLM、不做因子校准（k=1）；
        * 票型不指定 → 引擎按腿数兜底 `N串1`；腿数不足 m_star 时由关数门拦掉（空仓）。
        """
        pc = cfg.get("pool_gate") if isinstance(cfg, dict) else None
        pol = self._pool_gate_policy(cfg, as_of=as_of) if isinstance(pc, dict) else None
        theta = float((pol or {}).get("theta")
                      or (pc or {}).get("default_theta") or 1.1)
        allowed = {str(c) for c in ((pol or {}).get("gl_classes")
                                    or (pc or {}).get("gl_classes") or ["gl0"])}
        n_take = int(cfg.get("rule_legs") or 9)
        cands: list[tuple[float, dict, str, dict]] = []
        for m in matches:
            gl = float((m.get("beidan_info") or {}).get("goal_line") or 0.0)
            cls = "gl0" if abs(gl) < 1e-9 else "glN"
            if cls not in allowed:
                continue          # 规则版路径不逐场记原因（保持原行为）
            _src, p_mkt = self._market_p_hat(m)
            if not p_mkt:
                continue
            o = self._beidan_odds(m) or {}
            for side, key in (("H", "h"), ("D", "d"), ("A", "a")):
                try:
                    odds = float(o.get(key) or 0.0)
                    pm = float(p_mkt.get(side) or 0.0)
                except (TypeError, ValueError):
                    continue
                if odds > 0 and pm > 0 and pm * odds >= theta:
                    cands.append((pm * odds, m, side, p_mkt))
        # 一场最多一条腿（同场多方向互斥，串起来必不中）
        best: dict[str, tuple[float, dict, str, dict]] = {}
        for x, m, side, p_mkt in cands:
            lid = m.get("lota_id") or ""
            if lid and (lid not in best or x > best[lid][0]):
                best[lid] = (x, m, side, p_mkt)
        pool = sorted(best.values(), key=lambda t: -t[0])
        cap = float(cfg.get("rule_x_cap") or 0.0)
        if cap > 0:
            pool = [t for t in pool if t[0] <= cap]
        # 池子超过 n_take 时**不做 x 排序择优**（那是在噪声上做选择 → 赢家诅咒）：
        # 默认按「足球日 + 策略」定种子的随机抽样，无偏地取池子的一份子集。
        if len(pool) > n_take:
            pick_mode = str(cfg.get("rule_pick") or "random").strip().lower()
            if pick_mode == "x_desc":
                pool = pool[:n_take]
            else:
                import random as _rnd
                pool = _rnd.Random(f"{as_of}|{n_take}").sample(pool, n_take)
        legs: list[dict] = []
        for x, m, side, p_mkt in pool:
            leg = self._flex_leg(m, [side], {side: float(p_mkt[side])},
                                 p_mkt=p_mkt, allowance=0.0, k_cal=1.0)
            if not leg:
                continue
            leg["rule_x"] = round(float(x), 4)
            leg["factors"] = []
            legs.append(leg)
        print(f"  🎯 规则臂 x_top：候选 {len(cands)} 腿 / {len(best)} 场 → 取 {len(legs)} 条"
              f"（盘口 {sorted(allowed)}，θ={theta:g}，"
              f"每注最少 {((pol or {}).get('m_star'))} 关，"
              f"来源 {((pol or {}).get('source'))}）")
        return legs

    def _assemble_legs_rule(self, stage1_items: list[dict], cand_map: dict,
                            cfg: dict, day_date: str) -> list[dict]:
        """**引擎规则组装**（ticket_mode="rule"，不再调用 stage2 LLM）。

        规则（全部确定性）：
          1. 腿池 = `gl0` 且某侧 `x ≥ θ`；
          2. **veto 是 LLM 唯一的硬杠杆**：stage1 标了 veto 的场次**整场剔除**；
          3. 一条腿 = 该场**所有过门侧**（同场两侧过门 → 自动双选/三选，成本 ×k）；
             stage1 的 `extra_picks`（可选）会并进来，但引擎按**被选侧平均 x** 判门：
             摊薄到 < θ 就自动丢弃（不需要靠纪律）；
          4. 可选的 `x_cap`：平均 x 超过上限的腿排除（极端尾部是锐市场参考噪声）；
          5. 票型由引擎兜底 `N串1`；关数门（M ≥ m_star）与 ROI 门照旧。
        """
        pol = self._pool_gate_policy(cfg, as_of=day_date)
        theta = float((pol or {}).get("theta")
                      or ((cfg.get("pool_gate") or {}).get("default_theta")) or 1.1)
        allowed = {str(c) for c in ((pol or {}).get("gl_classes") or ["gl0"])}
        x_cap = float(cfg.get("x_cap") or 0.0)
        # R2 修复（2026-09-14）：**本日已下过单的场次不得再买**。
        # 旧行为：两个波次各自独立选腿 → 实测 07-04 两张单重叠 7/9 条腿（同一天加倍下注）。
        _already_used: set[str] = set()
        try:
            for _o in self._ensure_role().get_orders():
                for _l in (_o.get("legs") or []):
                    if _l.get("lota_id"):
                        _already_used.add(_l["lota_id"])
        except Exception:
            pass
        # ── 两轴因子：给腿打分（方向边际 / 兑现差）并按它排序；没有 cond 因子的狗自动跳过 ──
        import os as _os
        _role_ax = self._ensure_role()
        _ax_fx = self._axis_factors(_role_ax)
        _ax_w = self._axis_factor_weights(_role_ax, day_date) if _ax_fx else {}
        _ax_on = bool(_ax_w) and _os.environ.get("DS_AXIS_RANK", "1") not in ("0", "off", "false")
        _ax_env = None
        if _ax_on:
            from .axis_cond import build_eval_env, eval_cond as _ec
            _ax_env = build_eval_env(self._axis_env_rows(cand_map, day_date))

        _honor_veto = bool(cfg.get("honor_stage1_veto", True))
        _ax_rows: dict = {}
        if _ax_on:
            _all = self._axis_env_rows(cand_map, day_date)
            from .axis_cond import build_eval_env as _be, eval_cond as _ec
            _ax_env = _be(_all)
            _ax_rows = {(r["lota_id"], r["side"]): r for r in _all}

        def _axis_score(lid: str, picks: list) -> tuple:
            """(方向分, 兑现分)——**只排序，不否决**。

            方向分 = Σ 命中的方向因子边际（带符号：顺向因子 +、规避类因子 −）。
            一条腿被多个负边际因子命中就会沉到后面，但**不会出局**——
            本臂要对比的是"排序效果"，不是"分类能力"。
            """
            if not _ax_on:
                return 0.0, 0.0
            d_sum, v_sum = 0.0, 0.0
            for side in picks:
                row = _ax_rows.get((lid, side))
                if not row:
                    continue
                for f in _ax_fx:
                    w = _ax_w.get(f["name"])
                    if not w:
                        continue
                    try:
                        if not _ec(f["cond"], row, _ax_env):
                            continue
                    except Exception:
                        continue
                    if w.get("axis") == "volatility":
                        v_sum += float(w["w"])
                    else:
                        d_sum += float(w["w"])          # 负边际 = 负分（沉底）
            return d_sum, v_sum

        legs: list[dict] = []
        dropped: list[str] = []
        for it in stage1_items:
            lid = it.get("lota_id")
            m = cand_map.get(lid)
            if not m:
                continue
            # stage1 veto 是否当硬过滤：默认是（改造前行为）；`honor_stage1_veto=false`
            # 时忽略它，让腿只受引擎门（x/盘口/已下单）与票型约束 —— 用于"门挡单"的对照实验。
            if str(it.get("veto") or "").strip() and _honor_veto:
                dropped.append(f"{lid}: stage1 veto")
                continue
            o = m.get("_beidan_odds") or self._beidan_odds(m) or {}
            try:
                gl = float(o.get("goal_line") or 0.0)
            except (TypeError, ValueError):
                gl = 0.0
            cls = "gl0" if abs(gl) < 1e-9 else "glN"
            if cls not in allowed:
                # ⚠️ 此前这里**静默 continue**：让球盘被剔除但 dropped 里没有记录，
                # 日志只显示"剔除 N 场"，看不出还有多少场是盘口类被挡掉的（2026-09-15 修正）。
                dropped.append(f"{lid}: 盘口 {cls} 不在允许集合 {sorted(allowed)}")
                continue
            _src, p_mkt = self._market_p_hat(m)
            if not p_mkt:
                continue
            xs = {s2: float(p_mkt[s2]) * float(o.get(k) or 0.0)
                  for s2, k in (("H", "h"), ("D", "d"), ("A", "a"))}
            # R2 修复（2026-09-14）：**同一足球日已下过单的场次不得再买**。
            # 旧行为：两个波次各自独立选腿 → 实测 07-04 两张单重叠 7/9 条腿
            # （等于对同一天加倍下注）。已用场次由调用方传入。
            if lid in _already_used:
                dropped.append(f"{lid}: 本日已下单，跳过（跨波去重）")
                continue
            picks = [s2 for s2 in ("H", "D", "A") if xs[s2] >= theta]
            extra = [s2 for s2 in (it.get("extra_picks") or [])
                     if s2 in ("H", "D", "A") and s2 not in picks]
            picks = picks + extra
            if not picks:
                continue
            # ⚠️ `max_per_leg` 必须在这里也生效（2026-09-14 修正）——
            # 旧实现只在 LLM 路径 `picks[:max_per_leg]` 裁，规则路径（gate_basis=x）
            # 完全无视它：`max_per_leg` 配了也白配，实测 3 条双选腿把 9过4 的
            # 126 注放大到 456 注 = 912 元/单。规则路径的"侧"由 x 决定 ⇒ 留 x 最高的。
            picks = cap_picks_by_x(picks, xs, int(cfg.get("max_per_leg") or 3))
            mean_x = sum(xs[s2] for s2 in picks) / len(picks)
            if mean_x < theta:
                dropped.append(f"{lid}: 平均 x={mean_x:.3f} < θ={theta:g}（多选摊薄）")
                continue
            if x_cap > 0 and mean_x > x_cap:
                dropped.append(f"{lid}: 平均 x={mean_x:.3f} > x_cap={x_cap:g}（极端尾部）")
                continue
            leg = self._flex_leg(m, picks, {s2: float(p_mkt[s2]) for s2 in picks},
                                 p_mkt=p_mkt, allowance=0.0, k_cal=1.0)
            if not leg:
                continue
            leg["factors"] = list(it.get("factors") or [])
            # 把 LLM 的 rank 落进腿记录（2026-09-14）：否则复盘只能挖 session md
            # （字段顺序不定/含示例 JSON/可能截断），实测挖了 3 次都没成功。
            # 落进订单后，rank vs 实际命中的对照就是一次直接的数据读取。
            try:
                leg["llm_rank"] = float(str(it.get("rank")).strip())
            except (TypeError, ValueError):
                leg["llm_rank"] = None
            leg["rule_picks"] = picks
            _d, _v = _axis_score(lid, picks)
            leg["_axis_dir"], leg["_axis_vol"], leg["_axis_x"] = _d, _v, mean_x
            legs.append(leg)
        if dropped:
            print(f"  🧮 规则组装剔除 {len(dropped)} 场：" + "; ".join(dropped[:4])
                  + ("…" if len(dropped) > 4 else ""))
        # 票型由引擎定：每注最多 9 关 ⇒ `N串1`（N = 腿数，≤9）。
        # ⚠️ 腿数超过 9 时**不按 x 择优**（在噪声上选择=赢家诅咒，实测更差），
        #    按 stage1 的呈现顺序保留前 9 条（稳定、与 x 无关）。
        max_n = min(int(cfg.get("max_legs") or 9), 9)
        if _ax_on and legs:
            # 两轴排序：方向边际（命中率−市场p̂）优先，其次兑现差，最后才看 x。
            # 与"纯 stage1 rank"的区别：rank 是 LLM 逐场感觉、跨 batch 不可比；
            # 这两个量有账本支撑、跨 batch 可比。DS_AXIS_RANK=0 可关掉做 A/B。
            legs.sort(key=lambda l: (-float(l.get("_axis_dir") or 0),
                                     -float(l.get("_axis_vol") or 0),
                                     -float(l.get("_axis_x") or 0)))
            print(f"  🧱 两轴排序：{len(legs)} 条腿按【方向分→兑现分→x】重排"
                  f"（只排序，不过滤；过滤仍由 LLM veto + 引擎门负责）")
        if len(legs) > max_n:
            _why = "两轴排序" if _ax_on else "stage1 顺序"
            print(f"  ⚖️ 腿数 {len(legs)} > {max_n}：按{_why}保留前 {max_n} 条"
                  "（不做 x 择优）")
            legs = legs[:max_n]
        if legs:
            # 票型由引擎定。`ticket_tolerance` > 0 时出容错票 `N过(N−tol)`，
            # 0/缺省 = 单注 `N串1`（改造前行为）。
            #
            # 为什么默认给容错：离线仿真（data/leg_pool_full，40 天 3927 侧腿，真实结算）实测
            #   9串1  −100%｜9过7 +464%(抽最好日 −98.5%)｜9过6 +429%(−31%)｜9过5 +240%(−8%)
            #   **9过4 +109%（抽掉最好日仍 +0.3%）**｜9过3 +41%（+0.2%）
            # → 容错把"中奖"从 9 关全中（≈5e-5）挪到"允错 4 腿"（≈42%），方差大幅下降；
            #   档位只能靠真实结算稳健性选，**不能靠独立模型推**（独立假设不成立，会算出 +3000% 的假值）。
            n_legs = len(legs)
            # ── 自适应档位（2026-09-14 用户口径）──
            # `ticket_m` > 0 时**固定每注 M 关**、腿数随当天可用量浮动：n 腿 → `n过M`。
            # 每腿平衡线 = (1/0.65)^(1/M)，M 固定 ⇒ 判门口径不随腿数漂移（这是我们想要的）。
            # 门槛：**n ≥ 5 才出票**（少于 5 场直接空仓，符合"场次不够就只产因子"）。
            tm = int(cfg.get("ticket_m") or 0)
            tol = int(cfg.get("ticket_tolerance") or 0)
            if tm > 0:
                if n_legs >= 5 and n_legs > tm:
                    self._flex_plan_ticket = f"{n_legs}过{tm}"
                elif n_legs >= tm:
                    self._flex_plan_ticket = ""      # 腿数不足 5 → 空仓
                else:
                    self._flex_plan_ticket = ""
            elif tol > 0 and n_legs > 2:
                tol = min(tol, n_legs - 2, BD_MAX_TOLERANCE)
                self._flex_plan_ticket = f"{n_legs}过{n_legs - tol}"
            else:
                self._flex_plan_ticket = f"{n_legs}串1"
        # ── 决策落盘（2026-09-14）：rank 有效性对照的数据基础 ──
        # 复盘三次都卡在"从 session md 抽候选/rank"（字段顺序不定、含示例 JSON、截断）
        # ⇒ 引擎直接把**有序候选**（含 rank / x / 是否入选 / 剔除原因）落成 JSON，
        # 之后 A/B（rank 序 vs 输入序取前 N）就是一次直接读取，零解析风险。
        try:
            # ⚠️ `_input_idx` 只存在于 `_select_legs_llm` 的局部作用域（918 行），
            # 这里必须按 `stage1_items` 自己重建 —— 此前直接引用 ⇒ NameError，
            # 又被下面的 `except: pass` 吞掉 ⇒ leg_decision 文件静默缺失（08-02/08-03 实测）。
            _input_idx = {it.get("lota_id"): i for i, it in enumerate(stage1_items)}
            _dec = {
                "day": day_date,
                "ticket": self._flex_plan_ticket,
                "n_kept": len(legs),
                "kept_ids": [l.get("lota_id") for l in legs],
                # ⚠️ `stage1_items` 此刻已按 rank 排过序 ⇒ **必须同时留输入序**，
                # 否则 A/B 的"输入序取前 N"这一臂无法复原（实测踩到）。
                "candidates": [
                    {"lota_id": it.get("lota_id"),
                     "rank": it.get("rank"),
                     "推荐": it.get("推荐"),
                     "input_idx": _input_idx.get(it.get("lota_id"))}
                    for it in stage1_items
                ],
                "dropped": dropped,
            }
            # ⚠️ 文件名必须带**时刻**（2026-09-14）：同一天有 2 个波次，
            # 只按日期命名会让第 2 波**覆盖**第 1 波的决策记录
            # （实测 08-09 只剩"入选 0"的第 2 波，第 1 波的 7过4 决策丢失）。
            import time as _t
            _p = (pathlib.Path(self._ensure_role()._role_dir) / "memory"
                  / f"leg_decision_{day_date}_{_t.strftime('%H%M%S')}.json")
            _p.write_text(json.dumps(_dec, ensure_ascii=False, indent=2,
                                    default=str), encoding="utf-8")
        except Exception as e:
            # 落盘失败必须**可见**（此前 pass ⇒ 缺文件且零提示，实测坑了两天）
            print(f"  ⚠️ 决策落盘失败（leg_decision_{day_date}.json）: "
                  f"{type(e).__name__}: {e}")
        print(f"  🧮 规则组装（ticket_mode=rule）：{len(legs)} 条腿"
              f"（θ={theta:g}，x_cap={x_cap or '关'}，盘口 {sorted(allowed)}，"
              f"票型 {self._flex_plan_ticket or '空仓'}）")
        # ⚠️ 空票型必须真的**空仓**（2026-09-14 修复）：`ticket_m` 下腿数不足 5 时
        # `_flex_plan_ticket` 置空表示"空仓"，但下游会把空串回落成 `N串1`
        # —— 实测 07-21 因此出了 1 张 `4串1`（2 元）的票。这里直接返回空腿集。
        if legs and not self._flex_plan_ticket:
            print(f"  ⛔ 票型为空（腿数 {len(legs)} 不足）→ 空仓，不组装腿集")
            return []
        return legs

    def _pool_gate_policy(self, cfg: dict,
                          as_of: Optional[str] = None) -> Optional[dict]:
        """读账本给出门策略；未开启/读不到 → None（等于没有门）。"""
        pc = (cfg or {}).get("pool_gate")
        if not isinstance(pc, dict):
            return None
        mode = str(pc.get("mode") or "off").strip().lower()
        if mode in ("", "off", "0", "false", "none"):
            return None
        try:
            role = self._ensure_role()
            from .pool_ledger import PoolLedger
            led = PoolLedger(Path(role._role_dir) / "memory" / "pool_ledger.json").load()
        except Exception:
            return None
        pol = led.policy(cfg, as_of=as_of)
        pol["mode"] = mode
        pol["observe_gl_classes"] = [str(c) for c in (pc.get("observe_gl_classes") or [])]
        return pol

    def _apply_flex_guardrails(self, cfg: dict, legs: list[dict],
                               capital: float,
                               ticket: Optional[str] = None,
                               pool_policy: Optional[dict] = None
                               ) -> tuple[list[dict], dict]:
        """按护栏修剪腿集（从尾部删 = 丢 LLM 排序里最弱的腿），并算注数/成本/Πv̂/ROI。

        票型由 LLM 指定（`N串1` 或 `N过M`，N=腿数 ≤9）；注数与期望按票型**精确**计算：
        `等效 Πv̂ = Σ_S Π(k_i·v̂_i) / Σ_S Π k_i`（m=N 时即 Πv̂）。
        顺序：先剔「无边际」腿（v̂ < min_leg_v）→ 再按腿数 / 注数 / 成本上限从尾部删。
        """
        max_legs = min(int(cfg.get("max_legs") or 9), self.PICK_N)
        # ── 护栏默认值必须**保守**（2026-09-14 修正）──
        # 旧实现 `or 0) or 10**9` / `or 0) or 100.0`：把「未配置 / 0」解释成
        # **不限注数 + 可押全部本金** —— 安全默认方向是反的。0704 实测：配置里
        # 两个 0 让 126 注的 9过4 被 3 条双选腿放大到 456 注 → 912 元/单（本金 18%）。
        # 现在：0/缺失 → 单口径注数（= 全单选票型的注数）+ 单票 ≤ 本金 10%。
        _cfg_combos = int(cfg.get("max_combos") or 0)
        _cfg_pct = float(cfg.get("max_stake_pct") or 0)
        _guard_warns: list[str] = []
        if _cfg_combos > 0:
            max_combos = _cfg_combos
        else:
            # 单口径注数：按当前腿数 + 容错额度算「每腿只买 1 侧」的注数
            # ⚠️ 必须同时考虑 `ticket_m`（固定每注 M 关）—— 只看 ticket_tolerance
            # 会在我们改用 ticket_m=4 时算出 m=n ⇒ C(9,9)=1，把上限压成 1 注（自伤）。
            _tm = int(cfg.get("ticket_m") or 0)
            _tol = int(cfg.get("ticket_tolerance") or 0)
            _m = _tm if _tm > 0 else max(len(legs) - _tol, 2)
            # ⚠️ 必须是「每腿只买 1 侧」的注数 = C(N, M)，**不是**多选展开数
            # （用展开数当上限等于没设上限 —— 第一版就踩了这个坑）。
            from math import comb as _comb
            max_combos = max(1, _comb(len(legs), _m)) if legs else 1
            _guard_warns.append(
                f"max_combos 未配置 → 保守默认 {max_combos} 注"
                f"（C({len(legs)},{_m}) 全单选口径；想放多选请显式配更大值）")
        if _cfg_pct > 0:
            max_stake_pct = _cfg_pct
        else:
            # ⚠️ 用户口径（2026-09-14）：**允许梭哈，不要保守** ⇒ 0/缺失 = 100%。
            max_stake_pct = 100.0
        min_leg_v = float(cfg.get("min_leg_v") or 0.0)
        budget = max(0.0, float(capital or 0.0) * max_stake_pct / 100.0)
        for w in _guard_warns:
            print(f"  ⚠️ 护栏：{w}")

        gate_basis = str(cfg.get("gate_basis") or "x").strip().lower()
        kept: list[dict] = []
        dropped: list[dict] = []
        for leg in legs:
            v = leg.get("leg_v")
            # 成票判据锁死 x：用引擎算的错价倍数（= 市场 p̂ × 赔率）判"这条腿值不值得买"，
            # LLM 报的 p̂ 只用于排序/多选决策，不参与门。
            if gate_basis == "x" and leg.get("x_mkt") is not None:
                v = float(leg["x_mkt"])
            if v is None:
                dropped.append({"lota_id": leg.get("lota_id"), "v": None,
                                "why": "无法评估边际（缺赔率/概率/市场参考）"})
                continue
            if float(v) < min_leg_v:
                dropped.append({"lota_id": leg.get("lota_id"), "v": float(v),
                                "why": f"边际 {gate_basis}̂={float(v):.3f} < {min_leg_v:g}（无边际=不买）"})
                continue
            kept.append(leg)

        # ── 门 ① 腿池：盘口类型 + x ≥ θ（shadow 只记录不拦）──
        gate: Optional[dict] = None
        if pool_policy:
            mode = str(pool_policy.get("mode") or "off").lower()
            allowed_gl = {str(c) for c in (pool_policy.get("gl_classes") or ["gl0"])}
            # 观察组（如让球盘）：照常出现在候选里、照常算 x，但**不进票**，只记录
            observe_gl = {str(c) for c in (pool_policy.get("observe_gl_classes") or [])}
            gate = {"mode": mode, "theta": pool_policy.get("theta"),
                    "gl_classes": sorted(allowed_gl),
                    "observe_gl_classes": sorted(observe_gl),
                    "m_star": pool_policy.get("m_star"),
                    "source": pool_policy.get("source"),
                    "y": pool_policy.get("y"), "y_lo": pool_policy.get("y_lo"),
                    "n": pool_policy.get("n"), "refused": [], "observed": []}
            if mode in ("enforce", "shadow"):
                kept2: list[dict] = []
                for leg in kept:
                    gl = float(leg.get("goal_line") or 0.0)
                    cls = "gl0" if abs(gl) < 1e-9 else "glN"
                    if cls in observe_gl and cls not in allowed_gl:
                        if len(gate["observed"]) < 60:
                            gate["observed"].append({"lota_id": leg.get("lota_id"),
                                                     "gl": gl, "x": leg.get("x_mkt")})
                        dropped.append({
                            "lota_id": leg.get("lota_id"), "v": leg.get("leg_v"),
                            "why": (f"观察盘口（{cls}，x={leg.get('x_mkt')}）"
                                    "—— 记录不下注，等 CI 下沿 >1 再放行")})
                        continue
                    ok, why = self._pool_leg_ok(leg, gate["theta"], allowed_gl)
                    if ok:
                        kept2.append(leg)
                    elif mode == "shadow":
                        gate["refused"].append({"lota_id": leg.get("lota_id"), "why": why})
                        kept2.append(leg)
                    else:
                        dropped.append({"lota_id": leg.get("lota_id"), "v": leg.get("leg_v"),
                                        "why": f"腿池门：{why}"})
                kept = kept2

        # 票型基数：腿池门**改变过腿集**时，LLM 报的 N 已经对不上，
        # 回落到「按最终腿数」的兜底票型（如 12过8 → 只剩 6 腿 ⇒ 6串1），
        # 绝不按对不上的 N 去算容错额度。
        basis = len(kept) if (gate and str(gate.get("mode")) == "enforce") else len(legs)
        if gate:
            gate["ticket_basis"] = basis
        spec = parse_ticket_spec(self._valid_flex_ticket(ticket, basis)) \
            if len(legs) >= 2 else None
        if spec is not None and spec.n < len(legs):
            # 兜底票型（腿数超上限）会给出 N < 腿数：只保留前 N 腿
            dropped_extra = legs[spec.n:]
            legs = legs[: spec.n]
        else:
            dropped_extra = []
        # 容错额度 t = N − M：修剪腿时**保持 t 不变**（9过8 裁到 8 腿 → 8过7），
        # 这样"允许容错"的意图不会因为成本护栏裁腿而悄悄消失。
        tol = (max(0, int(spec.n) - int(spec.m))
               if (spec and spec.kind == "过") else 0)

        def _m_for(ls: list[dict]) -> int:
            n = len(ls)
            if n <= 0:
                return 0
            m = max(2, n - tol) if tol > 0 else n
            return min(m, BD_MAX_COMBO_LEGS)   # 每注最高 9 关

        def _combos(ls: list[dict]) -> int:
            """按票型算注数（腿数会随修剪变化，m 随容错额度同步调整）。"""
            if not ls:
                return 0
            if spec is None:
                n = 1
                for l in ls:
                    n *= max(1, len(l.get("picks") or []))
                return n
            return int(self._ticket_ev(ls, _m_for(ls))[0])

        slimmed_log: list[dict] = []

        def _slim_multi(ls: list[dict]) -> bool:
            """把多选腿收成单选（保留 x 最大的一侧）→ 保住腿数、只降注数。

            为什么先削 pick 再删腿：删腿会改变票型（9过4 → 8过3…），而超预算的
            根因往往只是某几条腿多买了 1 侧。削 pick 能同时满足「保住 N 条腿」与
            「成本受限」（2026-09-14 用户口径：9 腿就该是 252 元）。
            """
            for l in ls:
                picks = l.get("picks") or []
                if len(picks) > 1:
                    best = max(picks, key=lambda k: float((l.get("odds") or {}).get(k) or 0)
                               * float((l.get("p_hat") or {}).get(k) or 0))
                    l["picks"] = [best]
                    # ⚠️ 所有「按侧」的字典都要一起削，否则注数仍按旧侧数算
                    # （只削 picks/odds 会漏掉 p_hat/p_claim/rule_picks）
                    for _k in ("odds", "p_hat", "p_claim"):
                        _d = l.get(_k)
                        if isinstance(_d, dict) and _d:
                            l[_k] = {best: _d[best]} if best in _d else {}
                    if isinstance(l.get("rule_picks"), list):
                        l["rule_picks"] = [best]
                    l["slimmed_from"] = picks
                    slimmed_log.append({"lota_id": l.get("lota_id"),
                                        "from": picks, "to": [best]})
                    return True
            return False

        while kept and (len(kept) > max_legs
                        or _combos(kept) > max_combos
                        or self.UNIT_STAKE * _combos(kept) > budget):
            if _slim_multi(kept):
                continue
            l = kept.pop()
            dropped.append({"lota_id": l.get("lota_id"), "v": l.get("leg_v"),
                            "why": f"超出护栏（腿数≤{max_legs} / 注数≤{max_combos} / "
                                   f"成本≤{budget:.2f}元）"})

        for l in dropped_extra:
            dropped.append({"lota_id": l.get("lota_id"), "v": l.get("leg_v"),
                            "why": f"超出选场上限（最多 {self.PICK_N} 场）"})

        combos = _combos(kept) if kept else 0
        cost = _round2(self.UNIT_STAKE * combos)
        m_eff = _m_for(kept) if (spec and kept) else len(kept)
        basis_legs = kept
        if gate_basis == "x":
            # 整票判据同样锁死 x：把每腿的 leg_v 换成 x 再算 Πv̂（成本/注数不受影响）
            basis_legs = [{**l, "leg_v": float(l["x_mkt"])}
                          for l in kept if l.get("x_mkt") is not None]
            if len(basis_legs) != len(kept):
                basis_legs = kept
        if basis_legs:
            _c, _ev = self._ticket_ev(basis_legs, m_eff)
            ticket_v = (_ev / _c) if _c else 0.0
        else:
            ticket_v = 0.0
        # 仅用于展示：LLM 自报 p̂ 口径下的 Πv̂（不参与成票）
        ticket_v_llm = 0.0
        if kept:
            _c2, _ev2 = self._ticket_ev(kept, m_eff)
            ticket_v_llm = (_ev2 / _c2) if _c2 else 0.0
        min_ticket_v = self._min_ticket_v(cfg, m_eff or len(kept))
        if kept:
            n_now = len(kept)
            tk_final = (f"{n_now}串1" if (not spec or m_eff >= n_now)
                        else f"{n_now}过{m_eff}")
        else:
            tk_final = ""
        meta = {
            "combos": combos,
            "cost": cost,
            "ticket": tk_final,
            "ticket_requested": (spec.label if spec else ""),
            "ticket_changed": bool(spec and tk_final != spec.label),
            "m": m_eff,
            "ticket_v": ticket_v,
            "ticket_v_llm": ticket_v_llm,
            "gate_basis": gate_basis,
            "roi": 0.65 * ticket_v - 1.0,
            "min_ticket_v": min_ticket_v,
            "breakeven": self.BEIDAN_TAKEOUT,
            "drift_safety": (min_ticket_v / self.BEIDAN_TAKEOUT) if self.BEIDAN_TAKEOUT else 1.0,
            "legs": len(kept),
            "budget": round(budget, 2),
            "max_stake_pct": max_stake_pct,
            "slimmed": slimmed_log,
            "dropped": dropped,
        }
        srcs: dict[str, int] = {}
        for l in legs:
            k = l.get("market_src") or "无参考"
            srcs[k] = srcs.get(k, 0) + 1
        meta["market"] = {
            "candidates": len(legs),
            "with_ref": sum(1 for l in legs if l.get("market_ref")),
            "without_ref": sum(1 for l in legs if not l.get("market_ref")),
            "clamped_sides": sum(len(l.get("clamped") or []) for l in legs),
            "kept_with_ref": sum(1 for l in kept if l.get("market_ref")),
            "sources": srcs,
        }
        if gate:
            gate["kept_x"] = [l.get("x_mkt") for l in kept if l.get("x_mkt") is not None]
            gate["x_min"] = min(gate["kept_x"]) if gate["kept_x"] else None
            gate["m"] = m_eff
            m_star = gate.get("m_star")
            if str(gate.get("mode")) == "enforce":
                if m_star is None and str(gate.get("source")) == "ledger":
                    # 账本证据充分但没有任何桶的 CI 下沿 >1 → 自动降级：空仓
                    gate["block"] = True
                    gate["why"] = ("账本里没有可证实的正边际（y_lo="
                                   f"{float(gate.get('y_lo') or 0):.3f}）→ 空仓")
                elif m_star and m_eff and m_eff < int(m_star):
                    gate["block"] = True
                    gate["why"] = (f"每注 {m_eff} 关 < 打平所需 {int(m_star)} 关"
                                   f"（{gate.get('source')} y_lo="
                                   f"{float(gate.get('y_lo') or 0):.3f}）")
        meta["pool_gate"] = gate

        cal_legs = [l for l in legs if abs(float(l.get("k_cal") or 1.0) - 1.0) > 1e-9]
        meta["calibration"] = {
            "legs_with_k": len(cal_legs),
            "min_k": min((float(l.get("k_cal") or 1.0) for l in cal_legs), default=1.0),
            "detail": {l.get("lota_id"): {"k": l.get("k_cal"), "factors": l.get("factors")}
                       for l in cal_legs},
        }
        return kept, meta

    @classmethod
    def _default_ticket(cls, n: int) -> str:
        """没给票型/票型不合法时的引擎兜底：n ≤ 9 → `n串1`；n > 9 → `n过9`。

        n > 9 时 `n串1` 本身就是非法票（一注 17 关），兜底必须给**合法**票型：
        取最深合法组合 `n过9`（每注 9 关、容错 n−9 ≤ 8），成本由护栏再裁。
        """
        n = min(int(n or 0), BD_MAX_COMBO_LEGS + BD_MAX_TOLERANCE)
        if n <= 0:
            return ""
        if n <= BD_MAX_COMBO_LEGS:
            return f"{n}串1"
        return f"{n}过{BD_MAX_COMBO_LEGS}"

    @classmethod
    def _valid_flex_ticket(cls, ticket: Optional[str], n: int) -> str:
        """校验 LLM 指定的票型：N == 腿数，且 2 ≤ M ≤ 9（最高 9 关）、N − M ≤ 8（容错上限）。

        合法例：`9串1`、`9过8`、`12过8`（12 场单选容错 4）、`17过9`。
        不合法（幻觉票型/腿数对不上/超上限）→ 回落 `_default_ticket`，绝不按错票型落单。
        """
        default = cls._default_ticket(n)
        if not default:
            return ""
        if not ticket:
            return default
        spec = parse_ticket_spec(str(ticket).strip())
        if (not spec or spec.n != n or spec.m < 2
                or spec.m > BD_MAX_COMBO_LEGS or spec.m > spec.n
                or (spec.n - spec.m) > BD_MAX_TOLERANCE):
            return default
        return spec.label

    @staticmethod
    def _ticket_ev(legs: list[dict], m: int) -> tuple[float, float]:
        """票型 (N=腿数, m) 的 (注数, 期望份额)。

        每个"注" = 从 N 腿里取 m 腿、每腿取一个方向：
            注数 = Σ_S Π k_i                （k_i = 该腿被选方向数）
            期望 = Σ_S Π (k_i · v̂_i)         （Σ_p p̂×赔率 = k_i × v̂_i）
        ROI = 0.65 × 期望/注数 − 1，故"等效 Πv̂" = 期望/注数；m=N 时它就等于 Πv̂（向后兼容）。
        """
        n = len(legs)
        if n <= 0:
            return 0.0, 0.0
        m = max(2, min(int(m or n), n))
        if m >= n:
            c = e = 1.0
            for l in legs:
                k = max(1, len(l.get("picks") or []))
                c *= k
                e *= k * float(l.get("leg_v") or 0.0)
            return c, e
        from itertools import combinations as _comb
        combos = ev = 0.0
        for idx in _comb(range(n), m):
            c = e = 1.0
            for i in idx:
                k = max(1, len(legs[i].get("picks") or []))
                c *= k
                e *= k * float(legs[i].get("leg_v") or 0.0)
            combos += c
            ev += e
        return combos, ev

    def _flex_slip(self, legs: list[dict], ticket: Optional[str] = None) -> Optional[dict]:
        """腿集 → 一张票（票型由 LLM 指定，缺省 N串1；N=腿数，注数=票型注数）。"""
        n = len(legs)
        if n < 2 or n > self.PICK_N:   # 最多 17 场（护栏本该拦在前一步）
            return None
        tk = self._valid_flex_ticket(ticket, n)
        spec = parse_ticket_spec(tk)
        if spec and spec.n < n:
            legs = legs[: spec.n]
        built = self._build_slips(legs, [tk])
        return built[0] if built else None

    @staticmethod
    def _flex_plan_text(legs: list[dict], meta: dict, capital: float) -> str:
        pct = (meta.get("cost", 0.0) / capital * 100.0) if capital else 0.0
        heads = " + ".join(
            f"{l.get('lota_id')}{'/'.join(l.get('picks') or [])}"
            f"@v̂{float(l.get('leg_v') or 0):.2f}" for l in legs)
        mkt = meta.get("market") or {}
        mkt_txt = ""
        if mkt:
            mkt_txt = (f" | 市场锚 {mkt.get('with_ref', 0)}/{mkt.get('candidates', 0)} 腿"
                       f"（封顶 {mkt.get('clamped_sides', 0)} 侧）")
        cal = meta.get("calibration") or {}
        cal_txt = ""
        if cal.get("legs_with_k"):
            cal_txt = (f" | 校准压低 {cal['legs_with_k']} 腿"
                       f"（最小 k={float(cal.get('min_k') or 1.0):.2f}）")
        pg = meta.get("pool_gate") or {}
        pg_txt = ""
        if pg:
            pg_txt = (f" | 池门 {pg.get('mode')} θ={pg.get('theta')}"
                      f" 剩余腿 {len(pg.get('kept_x') or [])}"
                      f"(x_min={pg.get('x_min')})"
                      f" 每注{pg.get('m')}关/需≥{pg.get('m_star')}"
                      f" [{pg.get('source')}"
                      + (f" 拦掉{len(pg.get('refused') or [])}"
                         if pg.get("refused") else "")
                      + "]"
                      + (" ⛔不出票" if pg.get("block") else ""))
        tk = meta.get("ticket") or f"{len(legs)}串1"
        tk_note = ""
        if meta.get("ticket_changed"):
            tk_note = f"（请求 {meta.get('ticket_requested')} → 成本护栏下变为 {tk}）"
        return (f"🧾 flex 票 {tk}{tk_note} | 注数 {meta.get('combos')} | "
                f"成本 {meta.get('cost'):.0f}（资金 {pct:.1f}%） | "
                f"Πv̂ {float(meta.get('ticket_v') or 0):.3f} | "
                f"估 ROI {float(meta.get('roi') or 0):+.1%}{mkt_txt}{cal_txt}{pg_txt}"
                f"{' | ' + heads if heads else ''}")

    def _stage2_flex(self, *, matches: list[dict], cand_map: dict,
                     stage1_items: list[dict], cfg: dict, role: Role,
                     provider, persona: str, factor_slugs: list[str],
                     dir_factor_text: str, vol_factor_text: str,
                     day_date: str) -> Optional[list[dict]]:
        """flex：一次调用让 LLM 直接给「腿集」（每腿 1~3 方向 + 自估概率）。

        引擎不替它定串长/腿数，只算边际与护栏（见 _apply_flex_guardrails）。
        返回 None = LLM 失败（调用方回退规则版）。
        """
        import json as _json
        from .prompt_builder import count_tokens

        # 奖池门策略（读账本）——候选行的 x 标记与门说明都要用，必须在候选循环之前算
        _pol = self._pool_gate_policy(cfg, as_of=day_date)
        self._pg_theta_hint = float((_pol or {}).get("theta") or 1.1)
        allowed_hint = (set((_pol or {}).get("gl_classes") or ["gl0"]) if _pol else {"gl0"})

        capital = float(self._get_capital() or 0.0)
        max_legs = min(int(cfg.get("max_legs") or 9), self.PICK_N)
        max_per_leg = int(cfg.get("max_per_leg") or 3)
        max_combos = int(cfg.get("max_combos") or 0)
        max_stake_pct = float(cfg.get("max_stake_pct") or 0)
        budget = capital * max_stake_pct / 100.0
        max_by_cost = int(budget // self.UNIT_STAKE)

        # 方向路径校准：只有清单里真实存在的因子名才接受归因（防幻觉）
        calib = calibration_for(role)
        try:
            fp = role.memory.factors.factor_perf or {}
        except Exception:
            fp = {}
        allowed_factors: set[str] = set(fp.keys())
        for e in fp.values():
            for a in (e.get("aliases") or []):
                allowed_factors.add(str(a))

        cand_parts: list[str] = []
        info_lines: list[str] = []
        mkt_by_lid: dict[str, tuple[str, dict]] = {}
        slugs = list(self.FACTOR_SECTIONS)
        for s in (factor_slugs or []):
            if s not in slugs:
                slugs.append(s)
        for it in stage1_items:
            lid = it.get("lota_id")
            m = cand_map.get(lid)
            if not m:
                continue
            o = m.get("_beidan_odds") or self._beidan_odds(m)
            if not o:
                continue
            # 原始数据段只在 stage1 出现（那里全量 + batch）；stage2 只吃紧凑腿表
            src_name, p_mkt = self._market_p_hat(m)
            if src_name:
                mkt_by_lid[lid] = (src_name, p_mkt)
                p_txt = "/".join(f"{s}{p_mkt[s]:.3f}" for s in ("H", "D", "A"))
                gl_leg = float(o.get("goal_line") or 0.0)
                cls = "gl0" if abs(gl_leg) < 1e-9 else "glN"
                xs = {s2: float(p_mkt[s2]) * float(o.get(k) or 0)
                      for s2, k in (("H", "h"), ("D", "d"), ("A", "a"))}
                theta = float((self._pg_theta_hint or 1.1))
                x_txt = "/".join(
                    f"{s2}{xs[s2]:.2f}{'✅' if xs[s2] >= theta else ''}"
                    for s2 in ("H", "D", "A"))
                tag = ("盘口=gl0｜可下注盘口" if cls in (allowed_hint or {"gl0"})
                       else "盘口=glN｜只观察不下注（引擎规则，无需你处理）")
                mkt_txt = (f" | 市场p̂({src_name}去水) H/D/A={p_txt}"
                           f" | x(H/D/A)={x_txt} {tag}")
            else:
                mkt_txt = " | 市场p̂ 无参考（x 算不出 → 这条腿会被门剔除）"
            vet = str(it.get("veto") or "")
            facs = "/".join(str(f) for f in (it.get("factors") or [])[:4])
            s1 = (f"stage1 {it.get('推荐')}"
                  + (f"｜veto: {vet}" if vet else "")
                  + (f"｜factors: {facs}" if facs else "")
                  + ")")
            info = (
                f"- {lid} | {m.get('home_name')} vs {m.get('away_name')} "
                f"[{m.get('league_name', '')}] {self._gl_text(o.get('goal_line'))} "
                f"赔率 H/D/A={float(o.get('h')):.2f}/{float(o.get('d')):.2f}/{float(o.get('a')):.2f}"
                f"{mkt_txt} | {s1}"
            )
            info_lines.append(info)
            cand_parts.append(info)
        self._assert_prompt_redacted("\n".join(info_lines))
        cand_text = "\n".join(cand_parts)

        # 奖池门（读账本；未开启 → 空串，prompt 完全不变）
        gate_block = ""
        if _pol:
            _obs = "/".join(_pol.get("observe_gl_classes") or []) or "无"
            gate_block = (
                "## ⛔ 奖池门（引擎硬规则，先看这个）\n"
                "- **候选里所有场次都给你**（含让球盘）；但**只有不让球（goal_line=0）的腿会进票**。\n"
                f"- 让球盘（{_obs}）**只观察、不下注**：x 照算、你可以评论，但它不进票"
                "（等该盘口的 CI 下沿 > 打平线再放行）。\n"
                f"- 放行腿还要 **x = 市场 p̂ × 北单赛前赔率 ≥ {float(_pol.get('theta') or 0):.2f}**；"
                "候选行里已标好每侧的 x（✅ = 过门）。\n"
                f"- 每注关数必须 **≥ {_pol.get('m_star')} 关**（打平所需），否则整票不出票（空仓）。\n"
                f"- 门限来源：{_pol.get('source')} —— {_pol.get('reason')}\n\n")

        dir_block = ""
        if dir_factor_text:
            dir_block = "## 📌 方向型因子（用来找「哪个方向被高估/低估」）\n" + dir_factor_text + "\n"
        # 波动型因子块已删除（新口径：波动因子不产生 x，是正向「更该买」信号，见 stage1）

        # 当日粗筛留档：stage1 对**所有候选场次**的因子归因（不管最后有没有下注），
        # 结算时用它统计"这个因子标记的场次里出高波动的比例"——避免只看下注腿的选择偏差。
        try:
            self._save_vol_screen(role, day_date, stage1_items)
        except Exception as e:
            print(f"  ⚠️ 粗筛留档失败（不影响分析）: {e}")

        prompt = f"""你是北单串关分析 agent（{role.name}）。你只决定一件事：**这一票买哪几条腿、每条腿买哪些方向**。
串长（腿数）由你自己定，不固定。

## 人设（策略唯一来源，按它执行）
{persona}

## 北单返奖数学（必须按这个算，不许跳过）
- 中奖奖金 = 每注 2 元 × Π(开奖SP) × 0.65；**0.65 只乘一次，与串长无关**。
- 单注毛额 > 10000 元要缴 20% 个税（**全额计税**）：毛额 10000~12500 是净亏区，单注 SP 连乘要么 ≤7692，要么 ≥9615。
- 单腿边际 v̂ = p̂（你估的概率）× 该方向北单赛前赔率；与市场持平 = 1.000。
- 整票期望 ROI = 0.65 × Π v̂ − 1。**打平需要 Π v̂ > {self.BEIDAN_TAKEOUT:.3f}**：

{self._breakeven_table()}

- ⚠️ 北单是**奖池型**：你看到的赔率只是「下注当时的奖池快照（1/f）」，最终奖金由**赛后开奖 SP**决定
  （1531 场实测：开奖SP/下注时赔率 中位 1.008，p10 0.849、p90 1.208 → 单腿漂移 ±13%）。
  所以引擎按**每注关数 M**（容错票按 M，不按选场数 N）加了漂移安全垫，**实际出票线**是：
  2关 {self._min_ticket_v(cfg, 2):.3f}、3关 {self._min_ticket_v(cfg, 3):.3f}、5关 {self._min_ticket_v(cfg, 5):.3f}、
  8关 {self._min_ticket_v(cfg, 8):.3f}、9关 {self._min_ticket_v(cfg, 9):.3f}——别卡着 {self.BEIDAN_TAKEOUT:.3f} 出票。
- 多选腿（一条腿买多个方向）的边际 = 这些方向 v̂ 的**平均值**：只有当你对被选的**每一个**方向都认为 v̂>1 时多选才不亏；三个方向全买 = 买市场本身（≈1.00），只是用成本换命中率。
- ⚠️ 所以「求稳买低串」在这个玩法里是负期望的数学陷阱：2串1 要求每腿 +24% 边际，做不到就是稳定 −35%。正期望只能来自**多条真有边际的腿连乘**。**没有边际就不出票**，空仓是合法结果。

## 引擎护栏（超出会被修剪或拒单，别试探）
- 腿数 2~{max_legs}；每腿 1~{max_per_leg} 个方向；整票注数 ≤ {max_combos}；单票成本 ≤ 资金 {max_stake_pct:g}%（≈ {budget:.2f} 元 → 最多 {max_by_cost} 注）
- **成票判据锁死 x**：引擎按 `Πx > 出票线` 决定出不出票（x = 市场p̂ × 赛前赔率）。
  你报的 p **不参与成票**，只用于排序与多选决策——所以"把 p 报高一点换出票"是无效的。
- **市场 p̂ 封顶**：每条候选后面都给了「市场 p̂（锐市场同一盘口去水；北单赔率本身是公平赔率，Σ1/赔率≈1.00）」。你报的 p̂ 最多只能是市场 p̂ 的 {float(cfg.get('market_allowance') or 0):g} 倍，**超出的部分引擎直接砍掉**——所以"我特别看好"没有意义，必须说清你为什么比锐市场还准（例如阵容/资金流/盘口背离这类市场没定价的信息）。
{('- 拿不到市场参考的场次：**引擎会直接剔除**。' if cfg.get('require_market_p') else '- 拿不到市场参考的场次：可以用，但你没有外部依据，默认别给高 p̂。')}

{gate_block}
{dir_block}
## 候选腿表（引擎定价 + stage1 判断；原始数据段在 stage1，这里不重复）
{cand_text}

## 你的工作（组装，不是重新定价）
1. **方向以 x 为准**：`x(H/D/A)` 是引擎算好的错价倍数，**带 ✅ 的那一侧就是过门的一侧**；
   一行里可以有多个 ✅。你**不能靠自报概率创造边际**（p 被封顶在市场的 {float(cfg.get('market_allowance') or 0):g} 倍 + 按因子历史压低），`p` 只用于排序。
2. **多选是你的正当决策**（一条腿买 2~3 个方向）：当**每个被选方向各自都 ✅ 过门**时可以多选
   （引擎按被选方向的**平均 x** 判这条腿过不过门；成本 ×2/×3）。
   例：某行 `H1.19✅ D1.13✅` → 可以双选 H+D（不确定哪个赢时用成本换方向保险）；只有一侧 ✅ → 必须单选。
3. **只能否决**：stage1 标了 `veto` 的腿默认不选；不同意它的理由可以选，但要在 `why` 写清。
4. **定串长与票型**：从过门腿里挑 N 条组成 `N串1`（N ≥ 每注最少关数）。
   候选多于成本/注数上限时**不要在 x 上择优**（那是在噪声上做选择），按 stage1 的信息剔除即可。

## 票型（结算模式，由你选；不给就用引擎兜底）
- `ticket` 缺省 = `N串1`（N=腿数，**每腿都必须中**，N ≤ 9）。N > 9 时引擎兜底为 `N过9`。
- 容错票 `N过M`：**容忍 t = N − M 条腿错**，注数 = `Σ C(N,M) × Π每腿选项数`，成本按注数放大。
- 硬上限（引擎兜底口径，超了会被改）：**每注最多 9 关**（M ≤ 9）、**容错最多 8 场**（N − M ≤ 8）
  ⇒ 选场数 N 最多 {max_legs}。所以 N 可以 > 9，只要 M ≤ 9，例如：
  · `12过8` = 12 场单选、容错 4（每注 8 关）——这正是"允许容错"的用法；
  · `9过8` = 9 场单容忍错 1；`9串1` = 9 场全中。
- 怎么选：**先算清楚成本**（注数 × 2 元 ≤ 资金 {max_stake_pct:g}% ≈ {budget:.2f} 元，最多 {max_by_cost} 注）。
  容错买的是"少一条腿报废"的保险，但每多一个容错档，注数按组合数增长：
  容错增加的边际收益抵不过注数爆炸时就别买，宁可减腿或空仓。腿都很有把握 → `N串1`；
  只有一两条不太有把握、且成本放得下 → `N过(N−1)`（例如 9过8）。

## 输出（只输出 JSON）
{{"legs":[{{"lota_id":"Lota...","picks":["H"],"p":{{"H":0.55}},"factors":["因子名"],"why":"≤20字"}}],"ticket":"N串1 或 N过M","empty":false,"reason":""}}
- legs 按信心从强到弱；picks ⊆ H/D/A（1~{max_per_leg} 个）；**必须给每个被选方向的概率 p（0~1）**。
  ⚠️ p 只用于排序与信心表达：引擎会把它封顶在市场的 {float(cfg.get('market_allowance') or 0):g} 倍并按因子历史校准，**边际来自 x，不来自你的 p**。
- **单选 / 多选的条件**：只有一侧 ✅ → 单选；**多个方向各自都 ✅** → 可以多选（成本 ×k、边际按平均，
  是用成本换方向保险）。不要选没有 ✅ 的方向。
- `factors`：这条腿靠上面因子清单里的哪些因子成立（**只能填清单里出现过的名字**，没有就空数组）。
  引擎会按因子历史**校准你报的概率**：某个因子过去声称 60% 实际只中 40%，下一轮它支持的概率就会被压低。
- 空仓就输出 {{"legs":[],"empty":true,"reason":"为什么今天没有正边际的腿"}}。"""
        user_msg = ("按人设与上面的门/腿表组装这一票的腿集 JSON："
                    "方向以 x 为准，只做过门（✅+🟢）的腿，尊重 stage1 的 veto；"
                    "没有可下注的腿就空仓。")

        try:
            resp = provider.call(
                prompt,
                [{"role": "user", "content": user_msg}],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            print(f"  ⚠️ stage2 flex LLM 失败: {e}")
            return None
        if not resp:
            return None
        try:
            data = _json.loads(self._extract_json(str(resp)))
        except Exception as e:
            print(f"  ⚠️ stage2 flex 非 JSON: {e}")
            return None

        try:
            rt = self._runtime()
            if rt.session:
                rt.session.llm_call(
                    prompt, resp,
                    tokens_in=count_tokens(prompt),
                    tokens_out=count_tokens(str(resp)),
                    token_breakdown={"data": count_tokens(cand_text),
                                     "sys": 0, "mem": 0, "tools": 0, "user": 0},
                    label="stage2 腿集（flex）",
                )
        except Exception:
            pass

        # ⚠️ **票型归谁定**（2026-09-14 修复）：`ticket_mode="rule"`（本狗配置）时
        # 票型由引擎按配置算（`ticket_m` → `n过4`），**LLM 的 `ticket` 字段不得覆盖**。
        # 此前 LLM 的 stage2 `ticket` 会盖掉引擎算好的 `9过4`，实测 07-25 把 9 腿票
        # 降成「每注 2 关」，被池门（≥3 关）拒掉 → 明明是富波却空仓。
        try:
            _tmode = str((self._load_parlay_config() or {}).get("ticket_mode")
                         or "rule").strip().lower()
        except Exception:
            _tmode = "rule"
        if _tmode != "rule":
            self._flex_plan_ticket = str(data.get("ticket") or "").strip() or None
        elif data.get("ticket"):
            print(f"  🎫 忽略 LLM 票型 {str(data.get('ticket'))!r}"
                  f"（ticket_mode=rule，引擎票型 {self._flex_plan_ticket or '待定'}）")
        if data.get("empty") and not (data.get("legs") or []):
            self._flex_plan_ticket = None
            print(f"  🈳 LLM 选择空仓: {str(data.get('reason') or '')[:60]}")
            return []

        legs: list[dict] = []
        seen: set[str] = set()
        for item in (data.get("legs") or []):
            if not isinstance(item, dict):
                continue
            lid = item.get("lota_id")
            if not lid or lid in seen:
                continue
            m = cand_map.get(lid)
            if not m:
                print(f"  ⚠️ flex: 忽略未知场次 {lid}")
                continue
            picks = [str(p).upper() for p in (item.get("picks") or [])]
            picks = [p for p in picks if p in ("H", "D", "A")]
            picks = list(dict.fromkeys(picks))[:max_per_leg]
            if not picks:
                continue
            src_name, p_mkt = mkt_by_lid.get(lid, ("", {}))
            # 因子归因（只认清单里出现过的名字）→ 用于方向路径的概率校准
            factors = [str(f).strip() for f in (item.get("factors") or [])
                       if str(f).strip() in allowed_factors]
            factors = list(dict.fromkeys(factors))[:5]
            k_cal, k_detail = calib.k_for_leg(factors, mode_of_leg(picks))
            leg = self._flex_leg(m, picks, item.get("p") or {},
                                 p_mkt=p_mkt or None,
                                 allowance=float(cfg.get("market_allowance") or 0.0),
                                 k_cal=k_cal)
            if not leg:
                continue
            if cfg.get("require_market_p") and not leg.get("market_ref"):
                print(f"  ⚠️ flex: {lid} 无市场参考（require_market_p），剔除")
                continue
            leg["market_src"] = src_name
            leg["factors"] = factors
            leg["k_cal"] = round(float(k_cal), 4)
            if k_detail:
                leg["k_detail"] = k_detail
            leg["llm_why"] = str(item.get("why") or "")[:60]
            seen.add(lid)
            legs.append(leg)

        rt = self._runtime()
        if rt.session:
            rt.session.tool_call(
                "stage2_flex",
                {"legs": len(legs)},
                "flex 腿集 " + ", ".join(
                    f"{l.get('lota_id')}{'/'.join(l.get('picks') or [])}"
                    f"@v̂{float(l.get('leg_v') or 0):.2f}" for l in legs) or "（空）",
            )
        return legs

    @staticmethod
    def _assert_prompt_redacted(text: str) -> None:
        """红线护栏：北单 analyze prompt 不得出现开奖结果/SP/比分等未来字段。"""
        for token in ("result", "spvalue", "score", "draw_datetime", "result_des"):
            if token in text:
                raise RuntimeError(f"北单 analyze prompt 泄漏未来字段: {token}")

    # ═══════════════════════════════════════════
    # 组票（多选笛卡尔积展开）
    # ═══════════════════════════════════════════

    def _build_slips(self, legs: list[dict], tickets: list[str]) -> list[dict]:
        built: list[dict] = []
        for tk in tickets:
            spec = parse_ticket_spec(tk)
            if not spec:
                continue
            if (spec.m < 2 or spec.n < spec.m or spec.n > len(legs)
                    or spec.m > BD_MAX_COMBO_LEGS
                    or (spec.n - spec.m) > BD_MAX_TOLERANCE):
                continue
            chosen = legs[: spec.n]
            combos = parlay_combinations(chosen, spec)
            if not combos:
                continue
            sub_odds = []
            for combo in combos:
                o = 1.0
                for leg in combo:
                    o *= float(leg.get("odds") or 0.0)
                sub_odds.append(round(o, 4))
            built.append({
                "ticket_type": tk,
                "legs": chosen,
                # 容错票：sub_ticket 直接用完整票型（12过8），避免展示成"8串1"把 N 藏掉
                "sub_ticket": (f"{spec.m}串1" if spec.m == spec.n else tk),
                "combos": combos,
                "sub_odds": sub_odds,
                "combos_count": len(combos),
            })
        return built

    def _build_default_slips(self, singles: list[dict],
                             covers: list[dict]) -> list[dict]:
        """默认策略组票：覆盖腿 + 单选腿组成一张 DEFAULT_TICKET。"""
        legs = list(singles) + list(covers)
        cfg = self._load_parlay_config()
        return self._build_slips(legs, [cfg["ticket"]])

    def _decide_tickets(self, legs: list[dict]) -> list[str]:
        for tk in self.DECIDE_PRIORITY:
            spec = parse_ticket_spec(tk)
            if spec and spec.n <= len(legs):
                return [tk]
        return []

    @staticmethod
    def _truncate_to_target(cfg: dict, singles: list[dict],
                            covers: list[dict]) -> tuple[list[dict], list[dict]]:
        """落单阶段：只按 LLM 给出的顺序，把候选截断到「目标单选数 + 目标覆盖数」。

        关键：**不再用任何分数二次排序**。LLM 在输出里排在前面的就是它更看重的，
        引擎只做数量上的截取（取前 need_s / need_c 个），绝不因某腿赔率低/confidence高
        就擅自把它提前。截断后若仍不足目标数，交给 _meets_min_quality 判定为"拒绝落盘"。
        """
        need_s = int(cfg.get("single_legs") or 0)
        need_c = int(cfg.get("cover_legs") or 0)
        singles = (singles or [])[:max(need_s, 0)]
        covers = (covers or [])[:max(need_c, 0)]
        return singles, covers

    @staticmethod
    def _meets_min_quality(cfg: dict, singles: list[dict],
                           covers: list[dict]) -> tuple[bool, str]:
        """检查是否达到「目标单选数 + 目标覆盖数」。

        返回 (ok, reason)。ok=False 时不落盘，但会把这张票作为「被拒绝的决策记录」
        留在 orders 里供展示/排查；reason 为拒绝理由。
        """
        need_s = int(cfg.get("single_legs") or 0)
        need_c = int(cfg.get("cover_legs") or 0)
        n_s, n_c = len(singles), len(covers)
        if n_s < need_s:
            return False, f"单选腿不足 {need_s} 场（仅 {n_s} 场），拒绝落盘"
        if n_c < need_c:
            return False, f"覆盖/防冷腿不足 {need_c} 场（仅 {n_c} 场），拒绝落盘"
        return True, ""
    # ═══════════════════════════════════════════
    # analyze — 规则选腿 + 串关下单
    # ═══════════════════════════════════════════

    def _analyze_waves(self, day_date: str, waves: list, **kw) -> dict:
        """回测多波次：一个足球日按固定习惯波次逐波分析（周末两波 = 两张票）。

        波次表见 `src/backtest_fet.py`（周末 16:30/20:30，工作日 22:30）。
        每波只分析「该波时刻尚未开赛」的场次；各波取数档位由切片源按
        gap = 开赛 - 波次时刻 解析，逐波独立出票（等价实盘两个启动点各一张票）。
        """
        merged = {
            "date": day_date,
            "waves": [],
            "matches_count": 0,
            "legs_selected": 0,
            "tickets": [],
            "max_picks": 0,
            "llm_used": False,
            "orders": [],
            "placed": 0,
            "reject_reason": "",
            "rejected": False,
            "dry_run": bool(kw.get("dry_run")),
            "skipped": [],
            "warnings": [],
            "flex": None,
            "session_path": "",
        }
        try:
            for w in waves:
                backtest_fet.set_access_time(w)
                at = w.strftime("%Y-%m-%d %H:%M")
                print(f"\n  🕰️ 回测波次 {at}（足球日 {day_date}）")
                r = self.analyze(day_date, as_of=w, **kw)
                merged["waves"].append({
                    "at": at,
                    "matches": r.get("matches_count", 0),
                    "legs": r.get("legs_selected", 0),
                    "placed": r.get("placed", 0),
                    "flex": r.get("flex"),
                })
                merged["matches_count"] += int(r.get("matches_count") or 0)
                merged["legs_selected"] += int(r.get("legs_selected") or 0)
                merged["tickets"] += list(r.get("tickets") or [])
                merged["max_picks"] = r.get("max_picks") or merged["max_picks"]
                merged["llm_used"] = merged["llm_used"] or bool(r.get("llm_used"))
                merged["orders"] += list(r.get("orders") or [])
                merged["placed"] += int(r.get("placed") or 0)
                if r.get("rejected"):
                    merged["rejected"] = True
                if r.get("reject_reason"):
                    merged["reject_reason"] = (
                        f"{merged['reject_reason']}；{r['reject_reason']}"
                        if merged["reject_reason"] else r["reject_reason"])
                merged["skipped"] += list(r.get("skipped") or [])
                merged["warnings"] += list(r.get("warnings") or [])
                if r.get("flex"):
                    merged["flex"] = r["flex"]
                if r.get("session_path"):
                    merged["session_path"] = r["session_path"]
        finally:
            backtest_fet.set_access_time(None)
            src = backtest_fet.current()
            if src is not None:
                try:
                    src.print_report()
                except Exception:
                    pass
        return merged

    def analyze(self, day_date: str = None, live: bool = False,
                dry_run: bool = False, tickets: Optional[list[str]] = None,
                stake_pct: Optional[float] = None, max_picks: Optional[int] = None,
                use_llm: bool = False, as_of=None) -> dict:
        """分析一个足球日。

        as_of: 回测波次时刻（naive datetime，北京时间）。回测切片源启用时由
               `_analyze_waves` 逐波传入；None 且切片源启用 → 自动按该日固定波次
               逐波跑（周末 16:30/20:30 两波，工作日 22:30 一波）。线上不传。
        """
        day_date = day_date or self._default_day()
        if as_of is None and backtest_fet.active():
            waves = backtest_fet.current().waves(day_date)
            if waves:
                return self._analyze_waves(
                    day_date, waves, live=live, dry_run=dry_run, tickets=tickets,
                    stake_pct=stake_pct, max_picks=max_picks, use_llm=use_llm,
                )
        as_of_str = as_of.strftime("%Y-%m-%d %H:%M") if as_of else None
        self._max_picks = 3
        cfg = self._load_parlay_config()

        # live 多波次：与单关狗/竞彩串关一致——先退「全未开赛」的旧票
        # （含已开赛腿的票保留），再重新选腿组合；避免每点一次「⚡ 分析」
        # 就叠一张同窗口新票（2026-09-08 曾连续两跑留下两张 8串1）。
        # 回测波次（as_of）不退款重选：每波独立出票，等价于实盘两个启动点各下一张。
        if live and not dry_run and not as_of_str:
            self.refresh_orders(day_date)

        session = self._begin_session("analyze", day_date)
        try:
            role = self._ensure_role()
            matches, data_warnings = self._beidan_matches(
                day_date, live=live, as_of=as_of_str)
            if live or as_of_str:
                matches = self._live_clean_matches(matches)
            llm_used = False
            flex_meta: Optional[dict] = None
            self._flex_plan_ticket = None
            flex_mode = self._is_flex(cfg)
            # 0-LLM 规则臂（selector="x_top"）：完全不调 LLM，腿由账本门 + x 排序产生
            rule_mode = (flex_mode
                         and str(cfg.get("selector") or "llm").strip().lower() == "x_top")
            if rule_mode:
                singles = self._select_legs_x_top(matches, cfg, as_of=day_date)
                covers = []
                llm_used = False
            elif use_llm:
                singles, covers = self._select_legs_llm(matches, day_date)
                if singles is None:
                    if flex_mode:
                        # flex 狗不套历史模板（8串1/5包3单）：没有 LLM 输出就是空仓
                        print("  ⛔ LLM 失败：flex 狗不套模板 → 空仓")
                        singles, covers = [], []
                    else:
                        print("  → LLM 失败，回退规则组票")
                        legs = self._select_legs_mixed(matches)
                        singles = legs[: self.SINGLE_PICK_LEGS]
                        covers = legs[self.SINGLE_PICK_LEGS:
                                      self.SINGLE_PICK_LEGS + self.FULL_COVER_LEGS]
                else:
                    llm_used = True
            if rule_mode or use_llm:
                if flex_mode:
                    # flex：LLM 给腿集（每腿自带 picks），引擎算边际/护栏/ROI
                    # 奖池门策略按**足球日**取账本（as_of=当天 → 只吃之前的开奖结果）
                    pool_policy = self._pool_gate_policy(cfg, as_of=day_date)
                    legs, flex_meta = self._apply_flex_guardrails(
                        cfg, singles or [], role.capital,
                        ticket=self._flex_plan_ticket,
                        pool_policy=pool_policy)
                    print("  " + self._flex_plan_text(legs, flex_meta, role.capital))
                    for d in flex_meta.get("dropped") or []:
                        print(f"    ↩️ 剔腿 {d.get('lota_id')}: {d.get('why')}")
                    tickets = [f"{len(legs)}串1"] if legs else []
                    singles, covers = list(legs), []
                    legs_selected = len(legs)
                    slip_legs = list(legs)
                    min_legs = int(cfg.get("min_legs") or 2)
                    if len(legs) < min_legs:
                        quality_ok = False
                        reject_reason = (f"flex: 可用腿 {len(legs)} < 最小 {min_legs}"
                                         "（没有正边际的腿），空仓")
                    elif (flex_meta.get("pool_gate") or {}).get("block"):
                        quality_ok = False
                        reject_reason = ("池门: " +
                                         str((flex_meta.get("pool_gate") or {}).get("why") or ""))
                    elif flex_meta["ticket_v"] <= flex_meta["min_ticket_v"]:
                        quality_ok = False
                        reject_reason = (
                            f"flex: 整票 Πv̂={flex_meta['ticket_v']:.3f} ≤ 出票线 "
                            f"{flex_meta['min_ticket_v']:.3f}"
                            f"（打平 1.538 + {len(legs)} 腿 SP 漂移安全垫；估 ROI "
                            f"{flex_meta['roi']:+.1%}），不出票")
                    else:
                        quality_ok = True
                        reject_reason = ""
                    # 用**护栏后的最终票型**组票（腿数可能被成本护栏裁过，票型随之变化）
                    slip = (self._flex_slip(legs, ticket=(flex_meta or {}).get("ticket"))
                            if quality_ok else None)
                    slips = [slip] if slip else []
                    if not quality_ok:
                        print(f"  ⛔ {reject_reason}")
                else:
                    tickets = [self.DEFAULT_TICKET]
                    singles, covers = self._truncate_to_target(cfg, singles, covers)
                    legs_selected = len(singles) + len(covers)
                    slip_legs = list(singles) + list(covers)
                    quality_ok, reject_reason = self._meets_min_quality(
                        cfg, singles, covers)
                    slips = (self._build_default_slips(singles, covers)
                             if quality_ok else [])
                    if not quality_ok:
                        print(f"  ⛔ {reject_reason}")
            elif flex_mode:
                # flex 狗：没有 LLM 输出（use_llm=False，如 skip_llm 演示）时**不套历史模板**
                print("  ⛔ flex 狗未启用 LLM（skip_llm?）→ 空仓；要跑规则臂请设 "
                      'selector="x_top"')
                tickets, singles, covers, slip_legs, slips = [], [], [], [], []
                legs_selected = 0
                quality_ok, reject_reason = False, "flex: 未走 LLM，空仓"
            elif max_picks is None and tickets is None:
                legs = self._select_legs_mixed(matches)
                singles = legs[: self.SINGLE_PICK_LEGS]
                covers = legs[self.SINGLE_PICK_LEGS:
                              self.SINGLE_PICK_LEGS + self.FULL_COVER_LEGS]
                tickets = [self.DEFAULT_TICKET]
                singles, covers = self._truncate_to_target(cfg, singles, covers)
                legs_selected = len(singles) + len(covers)
                slip_legs = list(singles) + list(covers)
                quality_ok, reject_reason = self._meets_min_quality(
                    cfg, singles, covers)
                slips = (self._build_default_slips(singles, covers)
                         if quality_ok else [])
                if not quality_ok:
                    print(f"  ⛔ {reject_reason}")
            elif flex_mode:
                print("  ⛔ flex 狗不接受 legacy 票型参数（--tickets/--picks）→ 空仓")
                tickets, singles, covers, slip_legs, slips = [], [], [], [], []
                legs_selected = 0
                quality_ok, reject_reason = False, "flex: 未走 LLM，空仓"
            else:
                self._max_picks = int(max_picks) if max_picks else self.MAX_PICKS
                self._max_picks = max(1, min(3, self._max_picks))
                legs = self._select_legs(matches, max_picks=self._max_picks)
                tickets = list(tickets) if tickets else self._decide_tickets(legs)
                legs = legs[: self.PICK_N]
                slips = self._build_slips(legs, tickets)
                legs_selected = len(legs)
                quality_ok, reject_reason = True, ""
                slip_legs = list(legs)

            placed, orders, skipped = 0, [], []
            for slip in slips:
                if slip["combos_count"] <= 0:
                    skipped.append(f"{slip['ticket_type']} 无有效组合")
                    continue
                # 北单固定每注 2 元
                slip_cost = self.UNIT_STAKE * slip["combos_count"]
                if slip_cost > role.capital:
                    skipped.append(f"{slip['ticket_type']} 资金不足(需{slip_cost:.0f})")
                    continue
                slip_id = _uid("slip_")
                bet_type = self.BET_TYPE
                # 一张票 = 一条 slip 级订单：k 条腿（含 picks 列表）存一次；
                # 笛卡尔积组合不再落盘，结算时现算（见 settle()）。
                combo_odds = slip["sub_odds"] or []
                order = {
                    "id": _uid("ord_"),
                    "slip_id": slip_id,
                    "slip_type": slip["ticket_type"],
                    "slip_index": 1,
                    "combos_count": slip["combos_count"],
                    "unit_stake": self.UNIT_STAKE,
                    "total_stake": _round2(slip_cost),
                    "ticket_legs": list(slip["legs"]),
                    "predict_id": "",
                    "lota_id": slip["legs"][0]["lota_id"] if slip["legs"] else "",
                    "bet_type": bet_type,
                    "ticket_type": slip["sub_ticket"],
                    "pick": f"{slip['ticket_type']} x{slip['combos_count']}注",
                    "odds": max(combo_odds) if combo_odds else 0.0,
                    "bet_size": self.UNIT_STAKE,
                    "legs": list(slip["legs"]),
                    "created_at": _now_bj(),
                    "settled_at": None,
                }
                if flex_meta is not None:
                    # flex 计划元数据（腿内已带 p_hat/leg_v，结算后可做边际校准）
                    order["flex"] = dict(flex_meta)
                orders.append(order)
                if not dry_run and quality_ok:
                    role.withdraw(slip_cost)
                    role.save_order(order)
                placed += 1

            # 若未达到目标单选/覆盖数：即使 combos 无效，也把「被拒绝的决策」作为
            # 记录输出（含选择 + 理由），但绝不落盘。backtest 的 place_order(o) 只处理
            # 真正落盘的订单（这里不进 role.orders，也不会被回放/结算误读）。
            if not quality_ok and slip_legs:
                rejected = {
                    "id": _uid("ord_"),
                    "slip_id": _uid("slip_"),
                    "slip_type": self.DEFAULT_TICKET,
                    "slip_index": 1,
                    "combos_count": 0,
                    "unit_stake": self.UNIT_STAKE,
                    "total_stake": 0.0,
                    "ticket_legs": list(slip_legs),
                    "predict_id": "",
                    "lota_id": slip_legs[0]["lota_id"] if slip_legs else "",
                    "bet_type": self.BET_TYPE,
                    "ticket_type": self.DEFAULT_TICKET,
                    "pick": f"{self.DEFAULT_TICKET} 拒绝落盘",
                    "odds": 0.0,
                    "bet_size": 0.0,
                    "legs": list(slip_legs),
                    "created_at": _now_bj(),
                    "settled_at": None,
                    "reject_reason": reject_reason,
                    "rejected": True,
                }
                orders = [rejected]
                skipped.append(reject_reason)
                placed = 0
                try:
                    session.rejection(reject_reason, singles, covers)
                except Exception:
                    pass

            return {
                "date": day_date,
                "matches_count": len(matches),
                "legs_selected": legs_selected,
                "tickets": tickets,
                "max_picks": self._max_picks,
                "llm_used": llm_used,
                "orders": orders,
                "placed": (len([o for o in orders if not o.get("rejected")])
                           if dry_run else placed),
                "reject_reason": reject_reason,
                "rejected": (not quality_ok) and bool(slip_legs),
                "dry_run": dry_run,
                "skipped": skipped,
                "warnings": data_warnings,
                "flex": flex_meta,
                "session_path": str(session._path),
            }
        finally:
            self._end_session(session)

    # ═══════════════════════════════════════════
    # settle — 北单开奖 result + spvalue 结算（65% 返奖）
    # ═══════════════════════════════════════════

    @staticmethod
    def _football_day_start_from_match_time(match_time: Optional[str]) -> Optional[str]:
        """从比赛开赛时间反推北单足球日起始日（窗口 [D 12:01, D+1 12:00]）。

        12:00 及以前的比赛属于前一个足球日；12:01 以后属于当天足球日。
        """
        if not match_time:
            return None
        s = str(match_time).strip().replace("T", " ")
        s = s[:16]
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        if dt.hour < 12 or (dt.hour == 12 and dt.minute == 0):
            base = dt.date() - timedelta(days=1)
        else:
            base = dt.date()
        return base.isoformat()

    def _infer_sp_dates_from_orders(self, orders: list[dict]) -> list[str]:
        """按未结算订单里的比赛时间推断应拉取的北单 SP 日期。

        settle 的 day_date 业务口径是「窗口结束日」，但用户/测试偶尔会传
        「窗口起始日」；直接 `day_date - 1` 会取错 SP，导致静默漏结。
        这里以订单腿的 match_time 为准，比入参日期更可靠。
        """
        out: set[str] = set()
        for o in orders or []:
            for leg in list(o.get("legs") or []) + list(o.get("ticket_legs") or []):
                if not isinstance(leg, dict):
                    continue
                sp = self._football_day_start_from_match_time(leg.get("match_time"))
                if sp:
                    out.add(sp)
        return sorted(out)

    def _fetch_beidan_results(self, day_date: Optional[str],
                              lids: set[str],
                              sp_dates: Optional[list[str]] = None,
                              orders: Optional[list[dict]] = None) -> dict[str, dict]:
        """结算前先通过 beidan/sp 接口把开奖 result/spvalue 合并进本地缓存，再读回。

        走 DataManager.prepare_beidan_sp：同一 sp_date 跨进程单飞，
        并发结算（多只北单狗）只拉一次线上，窗口内重复结算直接读本地。

        若调用方未显式传 sp_dates，则回退为 day_date - 1（兼容旧的窗口结束日口径）；
        建议 settle() 传入从订单 match_time 推断出的 sp_dates，避免日期口径错位。
        """
        if sp_dates is None:
            sp_dates = []
            if day_date:
                try:
                    sp_dates = [(date.fromisoformat(day_date) - timedelta(days=1)).isoformat()]
                except Exception:
                    sp_dates = []
        sp_dates = list(dict.fromkeys(sp_dates or []))

        for sp_date in sp_dates:
            if is_offline():
                # 离线回放：不读 beidan_sp 缓存（可能被线上半开奖污染），
                # 也不联网。直接走 get_cached_beidan_results（读 beidan/<date>.json，
                # 含全量 result/spvalue），避免 result_suspect 跳过结算。
                break
            try:
                prep = self._dm.prepare_beidan_sp(sp_date, owner=f"{self.user}:settle")
                if prep.get("warning"):
                    print(f"  ⚠️ {prep['warning']}")
            except Exception as e:
                print(f"  ⚠️ 开奖SP拉取失败（继续用本地缓存）: {e}")

        if is_offline():
            # 离线回放：三源合并开奖 result/spvalue，优先取「带 result」的源。
            #   ① legacy beidan/<date>.json（最完整开奖）
            #   ② matches/<date>.json 的 beidan_info（08-29/30 等开奖在报文里）
            #   ③ beidan_sp/<date>.json（独立开奖 SP）
            # 用「已有 result 才覆盖」的策略，避免无 result 的不完整 beidan_info 顶掉真实开奖。
            result: dict[str, dict] = {}
            for sp_date in sp_dates:
                for m in (self._dm._read_legacy_beidan(sp_date) or []):
                    lid = m.get("lota_id")
                    bi = m.get("beidan_info")
                    if lid and bi and lid in lids:
                        result.setdefault(lid, bi)
                # ② matches 缓存的 beidan_info（仅当该 lid 尚无 result 时补缺）
                for lid, bi in self._dm.get_cached_beidan_results({lid for lid in lids}).items():
                    if lid not in lids:
                        continue
                    if lid not in result and isinstance(bi, dict) and bi.get("result") not in (None, ""):
                        result[lid] = bi
                # ③ beidan_sp 独立缓存（仅当该 lid 尚无 result 时补缺）
                for lid, info in (self._dm.get_beidan_sp_cache(sp_date) or {}).items():
                    if lid not in lids or lid in result:
                        continue
                    if isinstance(info, dict) and info.get("result") not in (None, ""):
                        result[lid] = info
            return result

        result = self._dm.get_cached_beidan_results(lids)
        if not sp_dates:
            return result

        # 线上开奖优先于本地缓存：比赛缓存若被其他 agent 覆盖（lota_id 轮换/缺失），
        # merge_beidan_sp 会 updated=0，但这里仍用独立落盘的开奖 SP 补缺。
        # 关键是补缺前做与 merge_beidan_sp 相同的脏值校验，不能覆盖 result_suspect 防线。
        goal_line_by_lid: dict[str, float] = {}
        for o in orders or []:
            for leg in list(o.get("legs") or []) + list(o.get("ticket_legs") or []):
                if not isinstance(leg, dict):
                    continue
                lid = leg.get("lota_id")
                if lid and "goal_line" in leg and lid not in goal_line_by_lid:
                    goal_line_by_lid[lid] = leg.get("goal_line")

        for sp_date in sp_dates:
            if is_offline():
                break
            sp_cache = self._dm.get_beidan_sp_cache(sp_date)
            for lid, info in sp_cache.items():
                if lid not in lids:
                    continue
                base = dict(result.get(lid) or {})
                candidate = dict(base)
                candidate.pop("result_suspect", None)
                candidate.update(info)
                if lid in goal_line_by_lid and candidate.get("goal_line") is None:
                    candidate["goal_line"] = goal_line_by_lid[lid]

                probe = settle_leg("H", candidate)
                if probe.get("ready") and probe.get("mismatch"):
                    base["result_suspect"] = True
                else:
                    base = candidate
                result[lid] = base
        return result

    def _settle_one(self, role: Role, order: dict,
                    beidan_map: dict[str, dict]) -> Optional[dict]:
        legs = [dict(l) for l in order.get("legs", [])]
        bet_size = float(order.get("bet_size") or 0)
        res = settle_parlay_combo(legs, beidan_map, bet_size)
        if not res.get("ready"):
            return None
        order["hit"] = res.get("hit")
        order["push"] = res.get("push", False)
        order["all_void"] = res.get("all_void", False)
        order["sp_product"] = res.get("sp_product", 1.0)
        order["return_amount"] = res.get("return_amount", 0.0)
        order["profit"] = res.get("profit", 0.0)
        order["settlement_rate"] = BEIDAN_RETURN_RATE
        order["legs"] = res.get("leg_results", legs)
        order["settled_at"] = _now_bj()
        role.deposit(res.get("return_amount", 0.0))
        role.save_order(order)
        role.save()
        return {"hit": res.get("hit"), "profit": res.get("profit", 0.0)}

    def _persist_lid_opening(self, beidan_map: dict[str, dict],
                             unsettled: list[dict],
                             sp_dates: list[str]) -> None:
        """把 settle 拿到的逐腿开奖（result/spvalue/score/goal_line）落盘，保证跨环境不丢。

        做两件事：
        1. 回写订单 legs 的 beidan_info —— orders 随后 role.save() 持久化，订单即完整开奖载体。
        2. 按体育日 upsert 进 beidan_sp/<date>.json —— 该文件即该日全量开奖，可独立同步。
        不联网；失败只警告不影响结算。
        """
        if not beidan_map:
            return
        from .data_manager import save_beidan_sp_cache_merge
        # 1) 回写订单 legs 的 beidan_info
        for o in unsettled:
            for l in list(o.get("legs") or []) + list(o.get("ticket_legs") or []):
                if not isinstance(l, dict):
                    continue
                lid = l.get("lota_id")
                info = beidan_map.get(lid)
                if not info:
                    continue
                cur = l.get("beidan_info") or {}
                if not isinstance(cur, dict):
                    cur = {}
                # 用开奖 field(带 result/spvalue) 补全/刷新 beidan_info
                merged = {**cur, **info}
                l["beidan_info"] = merged
        # 2) upsert 到 beidan_sp/<date>.json（按 lota_id 补全开奖字段）
        try:
            save_beidan_sp_cache_merge(sp_dates, beidan_map)
        except Exception as e:
            print(f"  ⚠️ 开奖落盘失败（不影响结算）: {e}")

    def _judge_ticket_legs(self, ticket_legs: list[dict],
                           beidan_map: dict[str, dict],
                           diag: Optional[dict] = None,
                           tolerance: int = 0) -> Optional[dict]:
        """腿级判定，带**一票否决**（确定的错腿数 > 容错额度 → 整票必不中）。

        tolerance = N − M（`N串1` 为 0）：`N过M` 允许 N−M 条腿错，所以只有
        **已确定的零命中腿数 > tolerance** 时才能提前判死。

        返回三种：
          · None                     → 还判不了（有腿缺开奖/未到点）
          · {"dead": True, ...}      → 错腿已超容错，整票必不中：不必等其它腿、也不必算组合
          · {"leg_map", "single_hit", "sp_product", "leg_hits"} → 全部腿就绪且仍有希望

        为什么能提前判死：每注要从 N 腿里取 M 腿全中；某腿 picks 一个都没中时，
        任何包含它的 M 腿组合都断掉 —— 能凑出的完整注数最多 C(N−d, M)，
        `d > N − M` 时连一注都凑不出 → 整票必不中（与其它腿无关）。
        """
        if diag is None:
            diag = {"missing_lids": [], "not_ready": [], "runs": 0}
        diag["runs"] += 1
        from .beidan_settlement import VOID_SP, settle_leg

        leg_map: dict[str, dict] = {}
        leg_hits: dict[str, list[str]] = {}
        dead_legs: list[dict] = []
        pending = 0
        for leg in ticket_legs:
            lid = leg.get("lota_id") or ""
            info = beidan_map.get(lid)
            if info is None:
                if lid not in diag["missing_lids"]:
                    diag["missing_lids"].append(lid)
                pending += 1
                continue
            picks = leg.get("picks") or ([leg.get("pick")] if leg.get("pick") else [])
            probe = picks[0] if picks else "H"
            sl = settle_leg(probe, info)
            if not sl.get("ready"):
                if lid not in diag["not_ready"]:
                    diag["not_ready"].append(lid)
                pending += 1
                continue
            leg_map[lid] = sl
            hits = list(picks) if sl.get("push") else [
                p for p in picks if p == sl.get("actual")]
            leg_hits[lid] = hits
            if not hits:
                dead_legs.append({"lota_id": lid, "picks": list(picks),
                                  "actual": sl.get("actual")})

        # 容错：N过M 允许 N−M 条腿错；错腿数超过额度才必死
        # （额度内的错腿仍在 leg_map 里，不会阻塞结算：活注只取"不含错腿"的那些组合）
        if len(dead_legs) > int(tolerance or 0):
            return {"dead": True, "dead_leg": dead_legs[0], "dead_legs": dead_legs,
                    "tolerance": int(tolerance or 0), "leg_map": leg_map}
        if pending or len(leg_map) < len(ticket_legs):
            return None

        single_hit = True
        for leg in ticket_legs:
            picks = leg.get("picks") or ([leg.get("pick")] if leg.get("pick") else [])
            if len(picks) == 1:
                sl = leg_map[leg.get("lota_id")]
                if not sl.get("hit"):
                    single_hit = False
                    break

        sp_product = 1.0
        for leg in ticket_legs:
            sl = leg_map[leg.get("lota_id")]
            sp_product *= VOID_SP if sl.get("push") else sl.get("sp", 0.0)

        return {"leg_map": leg_map, "single_hit": single_hit,
                "sp_product": sp_product, "leg_hits": leg_hits,
                "dead_legs": dead_legs}

    def settle(self, day_date: str = None, reflect: bool = True) -> dict:
        session = self._begin_session("settle", day_date or "all")
        try:
            role = self._ensure_role()
            # 奖池账本：与有没有订单无关（普查），且**回看窗口**补记（开奖 SP 常滞后 ~3 天）
            try:
                n_led = self._update_pool_ledger(role, day_date)
                if n_led:
                    print(f"  📒 奖池账本：补记 {n_led} 场开奖结果")
            except Exception as e:
                print(f"  ⚠️ 奖池账本更新失败（不影响结算）: {e}")
            unsettled = [o for o in role.get_orders()
                         if not o.get("settled_at")
                         and o.get("bet_type") == self.BET_TYPE]
            if not unsettled:
                summary = {"settled": 0, "hit": 0, "miss": 0, "push": 0,
                           "pnl": 0.0, "slips_any_hit": 0, "slips_total": 0}
                # ⚠️ 两轴账本必须**与有没有订单无关**（2026-09-15 校对修正）：
                # 此前空仓日直接 return ⇒ 那些"分析过、也有结果、只是没下注"的腿
                # 不进账本 ⇒ 账本只统计下过注的日子（选择偏差）。
                try:
                    n_ax0 = self._accumulate_two_axis(role, day_date)
                    if n_ax0:
                        print(f"  🧱 两轴因子：{n_ax0} 个因子记入当日样本（空仓日）")
                except Exception as e:
                    print(f"  ⚠️ 两轴统计失败（空仓日，不影响结算）: {e}")
                try:
                    n_scr0 = self._accumulate_vol_screen(role, day_date)
                    if n_scr0:
                        print(f"  🌊 波动粗筛：{n_scr0} 个因子记入当日筛查结果（空仓日）")
                except Exception as e:
                    print(f"  ⚠️ 波动粗筛失败（空仓日，不影响结算）: {e}")
                if reflect:
                    try:
                        self._reflect_skipped(role, day_date)
                    except Exception as e:
                        print(f"  ⚠️ 跳单日因子生成失败（不影响）: {e}")
                session.settlement(summary)
                return summary

            lids = {lid for o in unsettled for lid in self._leg_ids(o)}
            inferred_sp_dates = self._infer_sp_dates_from_orders(unsettled)
            fetch_sp_dates = inferred_sp_dates
            if not fetch_sp_dates and day_date:
                try:
                    fetch_sp_dates = [
                        (date.fromisoformat(day_date) - timedelta(days=1)).isoformat()
                    ]
                except Exception:
                    fetch_sp_dates = []
            beidan_map = self._fetch_beidan_results(
                day_date, lids, sp_dates=fetch_sp_dates, orders=unsettled
            )
            # 治本：把逐腿开奖落盘（订单 legs beidan_info + beidan_sp/<date>.json），
            # 保证跨环境同步不丢 result/spvalue。
            self._persist_lid_opening(beidan_map, unsettled, fetch_sp_dates)

            summary = {"settled": 0, "hit": 0, "miss": 0, "push": 0, "pnl": 0.0,
                       "slips_any_hit": 0, "slips_total": 0}
            settled_orders: list[dict] = []
            total_return = 0.0
            # 结算诊断：收集「为什么某张 8串1 未能结算」，供打印/返回，避免无提示挂单。
            diag = {"missing_lids": [], "not_ready": [], "runs": 0}
            diag_notes: list[str] = []

            # 每张票 = 一条 slip 级 order。组合不落盘：settle 时用 parlay_combinations
            # 现算每注（每腿单选），再逐注走 settle_parlay_combo 判定与派彩。
            for o in unsettled:
                legs = o.get("legs") or []
                ticket_legs = o.get("ticket_legs") or legs
                spec = parse_ticket_spec(o.get("slip_type") or o.get("ticket_type"))
                combos = parlay_combinations(legs, spec) if spec else []
                if not combos:
                    continue
                unit_stake = float(o.get("bet_size") or self.UNIT_STAKE)
                combos_count = int(o.get("combos_count") or len(combos))
                total_stake = _round2(float(o.get("total_stake")
                                           or unit_stake * len(combos)))

                # 票型容错额度：N过M 允许 N−M 条腿错（N串1 = 0）
                tolerance = max(0, int(spec.n) - int(spec.m)) if spec else 0
                # 先判整票腿级结果（ready 才可结算；同时回填每腿 actual/sp/push）
                judged = self._judge_ticket_legs(ticket_legs, beidan_map, diag,
                                                 tolerance=tolerance)
                if judged is None:
                    # 诊断：明确告诉用户这张票为何未结算
                    missing = [lid for lid in diag["missing_lids"]]
                    not_ready = [lid for lid in diag["not_ready"]]
                    reason = []
                    if missing:
                        reason.append(f"缺开奖数据场次({len(missing)}): {', '.join(missing[:5])}")
                    if not_ready:
                        reason.append(f"未到开奖时点场次({len(not_ready)}): {', '.join(not_ready[:5])}")
                    if not reason:
                        reason.append("全部腿均为全包但开奖未齐")
                    note = (f"⚠️ 未结算: slip {o.get('slip_id') or o.get('id')} "
                            f"—— {('; '.join(reason))}")
                    diag_notes.append(note)
                    print(f"  {note}", flush=True)
                    continue  # 未到结算时点，整票跳过
                leg_map = judged["leg_map"]
                for l in legs:
                    sl = leg_map.get(l.get("lota_id"))
                    if not sl:
                        continue
                    l["actual"] = sl.get("actual")
                    l["sp"] = sl.get("sp")
                    l["push"] = sl.get("push", False)
                    l["hit"] = bool(sl.get("hit"))

                # ⚡ 一票否决：有腿已确定零命中 → 整票必不中。
                # 不必等其它腿开奖，也不必遍历组合（8串1 三选是 6561 注，纯浪费）。
                if judged.get("dead"):
                    dl = judged.get("dead_leg") or {}
                    o["settled_at"] = _now_bj()
                    o["sp_product"] = 0.0
                    o["settlement_rate"] = BEIDAN_RETURN_RATE
                    o["hit"] = False
                    o["push"] = False
                    o["all_void"] = False
                    o["return_amount"] = 0.0
                    o["profit"] = _round2(-total_stake)
                    o["early_kill"] = dl
                    o["early_kill_legs"] = list(judged.get("dead_legs") or [])
                    settled_orders.append(o)
                    summary["settled"] += 1
                    summary["slips_total"] += 1
                    summary["miss"] += 1
                    summary["pnl"] = _round2(summary["pnl"] - total_stake)
                    n_dead = len(judged.get("dead_legs") or [])
                    tol = int(judged.get("tolerance") or 0)
                    print(f"  ⚡ 一票否决 slip {o.get('slip_id') or o.get('id')}: "
                          f"{n_dead} 条腿确定未中 > 容错 {tol} → 整票不中，"
                          f"跳过 {combos_count} 注组合计算"
                          f"（首条 {dl.get('lota_id')} 买 {'/'.join(dl.get('picks') or [])} "
                          f"实际 {dl.get('actual')}）",
                          flush=True)
                    continue

                # 票级 SP 连乘（用于快照/展示；串1 下即中奖注的 SP 连乘）
                sp_product = 1.0
                for l in legs:
                    sp_product *= float(l.get("sp") or 1.0)

                # 只算**活下来的组合**：每条腿只保留命中的方向（走水腿保留全部），
                # 未命中的组合必然派彩 0，没必要展开（数学上完全等价）。
                surv_legs: list[dict] = []
                for l in legs:
                    sl = leg_map.get(l.get("lota_id")) or {}
                    picks = l.get("picks") or ([l.get("pick")] if l.get("pick") else [])
                    keep = list(picks) if sl.get("push") else [
                        p for p in picks if p == sl.get("actual")]
                    surv_legs.append({**l, "picks": keep})
                combos_surv = parlay_combinations(surv_legs, spec) if spec else []

                slip_return = 0.0
                hit_combos = 0
                all_void = True
                slip_ready = True
                for combo in (combos_surv or combos):
                    r = settle_parlay_combo(combo, beidan_map, unit_stake)
                    if not r.get("ready"):
                        slip_ready = False
                        break
                    slip_return += r.get("return_amount", 0.0)
                    if r.get("hit"):
                        hit_combos += 1
                    if not r.get("all_void"):
                        all_void = False
                if not slip_ready:
                    continue  # 组合内某腿未开奖 → 整票跳过

                slip_hit = hit_combos > 0
                slip_return = _round2(slip_return)
                slip_pnl = _round2(slip_return - total_stake)
                o["settled_at"] = _now_bj()
                o["sp_product"] = round(sp_product, 6)
                o["settlement_rate"] = BEIDAN_RETURN_RATE
                o["hit"] = slip_hit
                o["push"] = all_void
                o["all_void"] = all_void
                o["return_amount"] = slip_return
                o["profit"] = slip_pnl
                settled_orders.append(o)
                total_return += slip_return
                summary["settled"] += 1
                summary["slips_total"] += 1
                if slip_hit:
                    summary["hit"] += 1
                    summary["slips_any_hit"] += 1
                else:
                    summary["miss"] += 1
                summary["pnl"] = _round2(summary["pnl"] + slip_pnl)

            if total_return:
                role.deposit(total_return)
            # 方向路径飞轮：把「声称概率 vs 实际开奖」落进校准库（下一轮用它压低高估的 p̂）
            try:
                n_cal = self._accumulate_p_calibration(role, settled_orders)
                if n_cal:
                    print(f"  🎯 概率校准：记入 {n_cal} 个 (p̂, 结果) 对")
            except Exception as e:
                print(f"  ⚠️ 概率校准写盘失败（不影响结算）: {e}")
            try:
                n_scr = self._accumulate_vol_screen(role, day_date)
                if n_scr:
                    print(f"  🌊 波动粗筛：{n_scr} 个因子记入当日筛查结果")
            except Exception as e:
                print(f"  ⚠️ 波动粗筛统计失败（不影响结算）: {e}")
            # 两轴因子：方向=命中率−市场p̂，波动=兑现 vs 当日基线（按日样本，带日期）
            try:
                n_ax = self._accumulate_two_axis(role, day_date)
                if n_ax:
                    print(f"  🧱 两轴因子：{n_ax} 个因子记入当日样本")
            except Exception as e:
                print(f"  ⚠️ 两轴统计失败（不影响结算）: {e}")
            role.save()

            summary["pnl"] = _round2(summary["pnl"])
            # 把「未结算原因」带进返回，前端/日志可感知，避免无提示挂单。
            if diag_notes:
                summary["_diagnostics"] = diag_notes
            if reflect:
                try:
                    if settled_orders:
                        self._reflect_settled(role, settled_orders, day_date)
                    else:
                        self._reflect_skipped(role, day_date)
                except Exception as e:
                    print(f"  ⚠️ 因子反思失败（不影响结算）: {e}")
            session.settlement(summary)
            return summary
        finally:
            self._end_session(session)

    # ── 波动路径：日级粗筛（"今天哪些场次可能出高波动"）──────────────

    def dump_prompts(self, day_date: str, live: bool = False,
                     out_dir: Optional[str] = "docs/prompts",
                     max_chars: int = 0) -> dict:
        """**只构建 prompt、绝不调 LLM**：把 stage1 / stage2 的 prompt 原样落盘供人工 review。

        做法：装一个「捕获型 provider」，把每次 `provider.call()` 的 system/user 抄下来，
        再返回一个**空腿集**的合法 JSON（于是引擎判定空仓，不会落任何单）。
        """
        import json as _json
        import re as _re
        from .prompt_builder import count_tokens

        calls: list[dict] = []

        class _Capture:
            """只记录不请求的 provider 桩。"""

            def call(self, system, messages, **kw):
                user = "\n".join(str(m.get("content") or "") for m in (messages or []))
                calls.append({"system": system or "", "user": user, "kw": kw})
                if "初筛器" in (system or ""):
                    lids: list[str] = []
                    for m2 in _re.finditer(r"Lota\d+", user):
                        if m2.group(0) not in lids:
                            lids.append(m2.group(0))
                    return _json.dumps(
                        {"items": [{"lota_id": i, "推荐": "H", "factors": []} for i in lids]},
                        ensure_ascii=False)
                return _json.dumps({"legs": [], "empty": True,
                                    "reason": "prompt-dump（只构建不落单）"},
                                   ensure_ascii=False)

        rt = self._runtime()
        prev = getattr(rt, "provider", None)
        self.set_provider(_Capture())
        try:
            res = self.analyze(day_date, live=live, use_llm=True, dry_run=True)
        finally:
            self._runtime().provider = prev

        files: list[str] = []
        summary: list[dict] = []
        if out_dir:
            base = Path(out_dir)
            base.mkdir(parents=True, exist_ok=True)
            for i, c in enumerate(calls, 1):
                stage = "stage1" if "初筛器" in c["system"] else "stage2"
                sys_tok = count_tokens(c["system"])
                usr_tok = count_tokens(c["user"])
                summary.append({"i": i, "stage": stage, "system_chars": len(c["system"]),
                                "user_chars": len(c["user"]), "tokens": sys_tok + usr_tok})
                f = base / f"{self.user}_{day_date}_{stage}_{i:02d}.md"
                body = (f"# {self.user} · {day_date} · {stage} #{i}\n\n"
                        f"- system {len(c['system'])} 字符 / ~{sys_tok} tokens\n"
                        f"- user {len(c['user'])} 字符 / ~{usr_tok} tokens\n"
                        f"- call kwargs: {c['kw']}\n\n"
                        f"## SYSTEM\n\n{c['system']}\n\n## USER\n\n{c['user']}\n")
                f.write_text(body[:max_chars] if max_chars else body, encoding="utf-8")
                files.append(str(f))
        return {"day": day_date, "calls": summary, "files": files,
                "orders": len(res.get("orders") or []),
                "tickets": res.get("tickets") or [],
                "matches_count": res.get("matches_count")}

    def _update_pool_ledger(self, role: Role, day_date: Optional[str]) -> int:
        """把「开奖结果」补记进奖池账本（与有没有下注无关）。

        北单整期结束才出 SP（常常滞后 ~3 天），所以这里是**回看窗口**（默认 7 天）
        反复扫，SP 到了就补记；`as_of=day_date` 保证只吃当天之前的结果（回放不偷看未来）。
        """
        cfg = self._load_parlay_config()
        pc = (cfg or {}).get("pool_gate")
        if not isinstance(pc, dict):
            return 0
        if str(pc.get("mode") or "off").lower() in ("", "off", "0", "false", "none"):
            return 0
        from .pool_ledger import update as _ledger_update
        as_of = str(day_date or "")[:10] or None
        res = _ledger_update(self.user, as_of=as_of,
                             lookback_days=int(pc.get("lookback_days") or 14))
        return int(res.get("added") or 0)

    def _screen_path(self, role: Role, day_date: Optional[str]) -> Optional[Path]:
        if not day_date:
            return None
        try:
            base = Path(role._role_dir) / "memory"
        except Exception:
            return None
        return base / f"screen_{day_date}.json"

    # ══════════════════════════════════════════════════════════════════
    # 两轴因子通道（方向：命中率−市场p̂；波动：兑现 vs 当日基线）
    # 见 docs/beidan_two_axis_analysis_plan.md。全部走"赛前落盘 → 结算后统计"，
    # 分析 prompt 里永远看不到开奖信息。
    # ══════════════════════════════════════════════════════════════════

    def _axis_legs_path(self, role: Role, day_date: Optional[str],
                        wave: Optional[str] = None) -> Optional[Path]:
        """当天过门腿的落盘路径。**按波次分文件**：一天两波时若共用一个文件，
        第二波会覆盖第一波的记录（A/B 复核时拿不到第一波买过的腿）。"""
        if not day_date:
            return None
        tag = day_date
        if wave:
            try:
                hhmm = str(wave).split(" ")[-1].replace(":", "")
                tag = f"{day_date}_{hhmm}"
            except Exception:
                tag = day_date
        try:
            return Path(role._role_dir) / "memory" / f"axis_legs_{tag}.json"
        except Exception:
            return None

    def _axis_factors(self, role: Role) -> list[dict]:
        """带机器条件 `cond` 的两轴因子（其它因子不参与该通道）。"""
        out = []
        try:
            role.memory.factors.load()
            for name, st in (role.memory.factors.factor_perf or {}).items():
                cond = (st or {}).get("cond")
                if cond:
                    out.append({"name": name, "type": st.get("type"),
                                "role": st.get("role"), "cond": cond})
        except Exception:
            return []
        return out

    def _axis_factor_weights(self, role: Role, day_date: Optional[str]) -> dict:
        """每个两轴因子的权重（只吃 `date < day_date` 的账本样本）。

        方向因子 → 方向边际（命中率 − 市场 p̂）；波动因子 → 兑现 − 当日基线。
        """
        out: dict = {}
        try:
            role.memory.factors.load()
            stats = role.memory.factors.factor_perf or {}
        except Exception:
            return out
        for name, st in stats.items():
            if not (st or {}).get("cond"):
                continue
            samples = [s for s in (st.get("axis_samples") or [])
                       if str(s.get("date") or "")[:10] < str(day_date or "9999-12-31")[:10]]
            if not samples:
                continue
            axis = st.get("type") or "directional"
            if axis == "volatility":
                deltas = [(float(s.get("ratio_med") or 0) - float(s.get("base_med") or 0))
                          for s in samples]
                w = sum(deltas) / len(deltas)
            else:
                n = sum(int(s.get("n") or 0) for s in samples)
                if n <= 0:
                    continue
                edge = (sum(float(s.get("sum_hit") or 0) for s in samples)
                        - sum(float(s.get("sum_p") or 0) for s in samples)) / n
                w = edge
            out[name] = {"axis": axis, "role": st.get("role"), "w": w}
        return out

    def _axis_env_rows(self, cand_map: dict, day_date: Optional[str]) -> list[dict]:
        """当天候选场的**三侧全量**行（名次类条件必须按全量求值）。"""
        from .beidan_axis import features_of, odds_span
        rows = []
        for m in (cand_map or {}).values():
            o = m.get("_beidan_odds") or self._beidan_odds(m)
            if not o:
                continue
            _src, pm = self._market_p_hat(m)
            if not pm:
                continue
            lid = m.get("lota_id")
            try:
                feats = features_of(self._dm.get_tags(lid) or {})
            except Exception:
                feats = {}
            gl = float((m.get("beidan_info") or {}).get("goal_line") or 0)
            span = odds_span(o.get("h"), o.get("d"), o.get("a"))
            for side, key in (("H", "h"), ("D", "d"), ("A", "a")):
                try:
                    mp = float(pm.get(side) or 0)
                    od = float(o.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if mp <= 0 or od <= 0:
                    continue
                rows.append({"day": day_date, "lota_id": lid, "side": side,
                             "market_p": mp, "beidan_odds": od, "x": mp * od,
                             "span": span, "goal_line": gl, "feats": feats})
        return rows

    def _save_axis_legs(self, role: Role, day_date: Optional[str],
                        matches: list[dict], as_of=None) -> int:
        """把当天【过门腿】+ 两轴因子命中标记落盘 —— 只含赛前信息，不含任何开奖结果。

        结算后由 `_accumulate_two_axis()` 读回、拼上结果做统计。
        """
        from .axis_cond import build_eval_env, eval_cond
        from .beidan_axis import LEG_LINE4, features_of, odds_span

        fx = self._axis_factors(role)
        p = self._axis_legs_path(role, day_date, as_of)
        if not fx or p is None:
            return 0
        theta = float(LEG_LINE4)
        try:
            cfg = self._load_parlay_config()
            pol = self._pool_gate_policy(cfg, as_of=day_date) or {}
            theta = float(pol.get("theta") or theta)
        except Exception:
            pass

        # ⚠️ 名次类条件（rank_mp / rank_od / rank_disp）必须按**该场三侧全量**求值，
        #    否则同场只有 1~2 侧过门时名次会退化（rank_disp 永远到不了 3）。
        all_sides: list[dict] = []          # 三侧全量：只用于建名次环境
        legs: list[dict] = []               # 过门腿：真正输出的样本
        for m in matches or []:
            o = m.get("_beidan_odds") or self._beidan_odds(m)
            if not o:
                continue
            _src, pm = self._market_p_hat(m)
            if not pm:
                continue
            lid = m.get("lota_id")
            secs = {}
            try:
                secs = self._dm.get_tags(lid) or {}
            except Exception:
                secs = {}
            feats = features_of(secs)
            gl = float((m.get("beidan_info") or {}).get("goal_line") or 0)
            span = odds_span(o.get("h"), o.get("d"), o.get("a"))
            for side, key in (("H", "h"), ("D", "d"), ("A", "a")):
                try:
                    mp = float(pm.get(side) or 0)
                    od = float(o.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if mp <= 0 or od <= 0:
                    continue
                row = {"day": day_date, "lota_id": lid, "side": side,
                       "market_p": mp, "beidan_odds": od, "x": mp * od,
                       "span": span, "goal_line": gl, "feats": feats}
                all_sides.append(row)
                if mp * od >= theta:
                    legs.append(dict(row))
        if not legs:
            return 0
        env = build_eval_env(all_sides or legs)
        for l in legs:
            hit = []
            for f in fx:
                try:
                    if eval_cond(f["cond"], l, env):
                        hit.append(f["name"])
                except Exception:
                    continue
            l["f"] = hit
            l.pop("feats", None)              # 落盘只留标量，别把原始段写进去
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"day": day_date, "theta": theta, "legs": legs},
                                ensure_ascii=False), encoding="utf-8")
        return len(legs)

    def _accumulate_two_axis(self, role: Role, day_date: Optional[str]) -> int:
        """结算后：把"预测命中且有开奖结果的腿"变成按日样本（带 date）。

        - 方向因子：样本 {date, n, sum_p, sum_hit}    ← 命中率 − 市场 p̂
        - 波动因子：样本 {date, n, ratio_med, base_med} ← 兑现 vs 当日基线

        分析时只累计 `date < 分析日` 的样本（见 `factor_select._axis_profile`）。
        """
        import statistics as _st
        from .beidan_settlement import result_code_to_pick

        # 一天可能有多个波次 → 把当天所有波次的落盘合并（按 lota+side 去重，避免两波重复计样本）
        p0 = self._axis_legs_path(role, day_date)
        if p0 is None:
            return 0
        legs, _seen = [], set()
        for pf in sorted(p0.parent.glob(f"axis_legs_{day_date}*.json")):
            try:
                for l in (json.loads(pf.read_text(encoding="utf-8")) or {}).get("legs") or []:
                    k = (l.get("lota_id"), l.get("side"))
                    if k in _seen:
                        continue
                    _seen.add(k)
                    legs.append(l)
            except Exception:
                continue
        if not legs:
            return 0

        actual: dict[str, str] = {}
        sp_map: dict[str, float] = {}
        try:
            for m in self._unplaced_completed(day_date, False, 10 ** 6):
                bi = m.get("beidan_info") or {}
                act = result_code_to_pick(str(bi.get("result")))
                sp = float(bi.get("spvalue") or 0)
                if act:
                    actual[m.get("lota_id")] = act
                if sp > 0:
                    sp_map[m.get("lota_id")] = sp
        except Exception:
            pass
        if not actual:
            return 0

        ratios_all = [sp_map[l["lota_id"]] / l["beidan_odds"] for l in legs
                      if l["lota_id"] in sp_map and l.get("beidan_odds")]
        base_med = _st.median(ratios_all) if ratios_all else None

        per_dir: dict[str, list] = {}
        per_vol: dict[str, list] = {}
        for l in legs:
            act = actual.get(l["lota_id"])
            for name in (l.get("f") or []):
                if act:
                    per_dir.setdefault(name, []).append((l["market_p"], 1.0 if act == l["side"] else 0.0))
                sp = sp_map.get(l["lota_id"])
                if sp and l.get("beidan_odds"):
                    per_vol.setdefault(name, []).append(sp / l["beidan_odds"])

        ftype = {f["name"]: f.get("type") for f in self._axis_factors(role)}
        n_upd = 0
        for name, pairs in per_dir.items():
            if ftype.get(name) == "volatility":
                continue
            st = role.memory.factors.factor_perf.setdefault(name, {})
            st.setdefault("type", "directional")
            # 按日期幂等：同一天重复结算 / 与离线 seed 撞日，都只保留一条
            st["axis_samples"] = [x for x in (st.get("axis_samples") or [])
                                  if str(x.get("date"))[:10] != str(day_date)[:10]]
            st.setdefault("axis_samples", []).append({
                "date": day_date, "n": len(pairs),
                "sum_p": round(sum(p for p, _ in pairs), 4),
                "sum_hit": round(sum(h for _, h in pairs), 4)})
            n_upd += 1
        for name, rs in per_vol.items():
            if ftype.get(name) != "volatility" or base_med is None:
                continue
            st = role.memory.factors.factor_perf.setdefault(name, {})
            st.setdefault("type", "volatility")
            st["axis_samples"] = [x for x in (st.get("axis_samples") or [])
                                  if str(x.get("date"))[:10] != str(day_date)[:10]]
            st.setdefault("axis_samples", []).append({
                "date": day_date, "n": len(rs),
                "ratio_med": round(_st.median(rs), 4),
                "base_med": round(base_med, 4)})
            n_upd += 1
        if n_upd:
            try:
                role.memory.factors._save()
            except Exception:
                pass
        return n_upd

    def _save_vol_screen(self, role: Role, day_date: Optional[str],
                         stage1_items: list[dict]) -> None:
        """把当天 stage1 的逐场因子归因落盘（粗筛留档，供结算统计）。"""
        p = self._screen_path(role, day_date)
        if p is None or not stage1_items:
            return
        payload = {"day": day_date, "saved_at": _now_bj(), "items": {}}
        for it in stage1_items:
            lid = it.get("lota_id")
            if not lid:
                continue
            payload["items"][lid] = {
                "f": [str(x) for x in (it.get("factors") or [])][:5],
                "r": str(it.get("推荐") or ""),
            }
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _day_sp_map(self, day_date: Optional[str]) -> dict[str, float]:
        """当日北单比赛的 `lota_id → 开奖SP`（含未下注场次），取自本地缓存。"""
        out: dict[str, float] = {}
        try:
            for m in self._unplaced_completed(day_date, False, 10 ** 6):
                lid = m.get("lota_id")
                v = float((m.get("beidan_info") or {}).get("spvalue") or 0)
                if lid and v > 0:
                    out[lid] = v
        except Exception:
            pass
        return out

    def _accumulate_vol_screen(self, role: Role,
                               day_date: Optional[str]) -> int:
        """结算时把"当日粗筛"变成因子级统计：标记场次里出高波动的比例 vs 当日基线。

        粗标签：开奖 SP ≥ `HIGH_VOL_SP`（覆盖成本线）即算"出了高波动"。
        返回更新的因子数。
        """
        p = self._screen_path(role, day_date)
        if p is None or not p.exists():
            return 0
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            items = payload.get("items") or {}
        except Exception:
            return 0
        sp_map = self._day_sp_map(day_date)
        if not sp_map or not items:
            return 0
        day_sps = list(sp_map.values())
        base = (sum(1 for v in day_sps if v >= self.HIGH_VOL_SP) / len(day_sps)
                if len(day_sps) >= 5 else None)
        if base is None:
            return 0
        per_factor: dict[str, list[int]] = {}
        for lid, meta in items.items():
            sp = sp_map.get(lid)
            if not sp:
                continue
            high = 1 if sp >= self.HIGH_VOL_SP else 0
            for f in (meta.get("f") or []):
                per_factor.setdefault(str(f), []).append(high)
        n_factors = 0
        for f, highs in per_factor.items():
            try:
                role.memory.factors.record_screen(f, n=len(highs),
                                                  high=sum(highs),
                                                  base_sum=base * len(highs))
                n_factors += 1
            except Exception:
                continue
        if n_factors:
            try:
                role.memory.factors._save()
            except Exception:
                pass
        return n_factors

    def _accumulate_p_calibration(self, role: Role,
                                  settled_orders: list[dict]) -> int:
        """把 flex 腿的「声称概率 p̂ + 因子归因」与开奖结果对齐，写进概率校准库。

        只有带 `p_hat`（=flex 腿）的订单参与；走水/取消腿不携带信息，跳过。
        按路径分开统计（单选 direction / 多选 cover）。
        """
        calib = calibration_for(role)
        n = 0
        for o in settled_orders or []:
            for leg in (o.get("ticket_legs") or o.get("legs") or []):
                claims = leg.get("p_hat") or {}
                if not claims:
                    continue
                picks = leg.get("picks") or []
                n += calib.record_leg(
                    [str(f) for f in (leg.get("factors") or [])],
                    mode_of_leg(picks),
                    claims,
                    leg.get("actual"),
                )
        if n:
            calib.save()
        return n

    # ═══════════════════════════════════════════
    # 结算 / 反思 解耦入口（2026-09-13 用户工单）
    # ═══════════════════════════════════════════
    # 背景：原来只有 `settle(reflect=True/False)` 一个入口，结算对账与「产因子」
    # 耦合在一条流程里，无法"只结算不产因子"或"只产因子不重算结算"。
    # 这里拆成两个**显式、可独立调用**的方法；`settle(reflect=...)` 原语义保持不变
    # （既有调用方/单狗都不受影响）。

    def settle_only(self, day_date: str = None) -> dict:
        """**只结算订单**（对账开奖 → 每腿 actual/hit/profit → 资金变动），不产因子。

        等价于 `settle(day_date, reflect=False)`，但语义显式。
        """
        return self.settle(day_date, reflect=False)

    def reflect_only(self, day_date: str = None, include_skipped: bool = True) -> dict:
        """**只产因子/反思**（不改动订单与资金）：对已结算订单做归因与发现。

        * 有已结算订单 → `_reflect_settled`（真实样本：hit/profit 来自真实派彩）
        * 无已结算订单且 `include_skipped=True` → `_reflect_skipped`（观察样本，带虚拟结算）

        返回统计，便于编排方判断本次是否真的产出了东西。
        """
        role = self._ensure_role()
        settled_orders = [o for o in role.get_orders() if o.get("settled_at")]
        before = len((role.memory.factors.factor_perf or {}) if role.memory.factors._loaded
                     else (role.memory.factors.load() or role.memory.factors.factor_perf) or {})
        # 留痕（2026-09-14）：独立 reflect 路径此前**从不写 session md**，导致
        # 「初始化阶段 0 因子 0 落单 → 随机抽北单产因子」这一段**无法 review**
        # （analyze / settle 都会 start() 一个 session，唯独 reflect_only 不会）。
        # 这里补上：两个子反思（方向 / 波动）追加进同一个 md。
        # ⚠️ 已有 session（settle 流内调用）时不抢，避免打断既有留痕。
        sess = None
        _rt_obj = None
        try:
            from .agent import _rt as _rt_reflect
            from .session_logger import SessionLogger as _SessionLogger
            _rt_obj = _rt_reflect({"user": self.user})
            if _rt_obj.session is None:
                sess = _SessionLogger(user=self.user)
                sess.start(action="reflect", day_date=day_date or "", capital=role.capital)
                _rt_obj.session = sess
        except Exception:
            sess = None
        try:
            if settled_orders:
                self._reflect_settled(role, settled_orders, day_date)
                mode = "settled"
            elif include_skipped:
                self._reflect_skipped(role, day_date)
                mode = "skipped"
            else:
                mode = "none"
        except Exception as e:
            _close_reflect_session(sess, _rt_obj, role, self)
            return {"day": day_date, "mode": "error", "error": str(e)[:300]}
        _close_reflect_session(sess, _rt_obj, role, self)
        role.memory.factors.load()
        after = len(role.memory.factors.factor_perf or {})
        refl = len(role.memory.reflections.reflections or [])
        return {
            "day": day_date, "mode": mode,
            "settled_orders": len(settled_orders),
            "factors_before": before, "factors_after": after,
            "factors_delta": after - before, "reflections": refl,
            "session_md": str(getattr(sess, "_path", "")) or None,
        }

    def _reflect_settled(self, role: Role, settled_orders: list[dict],
                         day_date: Optional[str]) -> None:
        """北单串关反思：把 8 串 1 子单按「每场比赛」去重成一个样本，
        显式标出「单选/全包 类型 + 实际中奖方向 actual + 开奖SP」，
        再走 agent.py node_reflect/run_reflect（不修改 agent.py）。

        修复点：
        1. 去重：leg_samples 由"每注每腿"改为"每场一条"，杜绝同一场比赛被
           combo 重复刷屏，使「高波动全包候选」能真正覆盖多场高 SP，而非全被
           单场最高 SP 的分身占满。
        2. 露真实结果：每条样本带 actual(实际方向) / sp_value(开奖SP) /
           role(单选/全包)，供反思与「全包低SP拖累」判断使用。
        """
        if not settled_orders:
            return

        # 跨 slip 判断每场是单选还是全包：某场只要在任意一张票中被单选，
        # 就以「单选」视角归因（单选看 pick 是否命中）；否则视为全包。
        # （同一次 settle 可能同时结算多张票，不同票里同一场可能是单选/全包，
        #   必须按 slip_id 分开建 role 映射，不能只看第一张票。）
        slip_role: dict[str, dict[str, str]] = {}
        for o in settled_orders:
            sid = o.get("slip_id") or o.get("id")
            if sid in slip_role:
                continue
            m: dict[str, str] = {}
            for tl in (o.get("ticket_legs") or []):
                lid = tl.get("lota_id")
                if not lid:
                    continue
                picks = tl.get("picks") or ([tl["pick"]] if tl.get("pick") else [])
                m[lid] = "单选" if len(picks) == 1 else "全包"
            slip_role[sid] = m

        all_lids: set[str] = set()
        single_lids: set[str] = set()
        leg_single_pick: dict[str, str] = {}
        for o in settled_orders:
            sid = o.get("slip_id") or o.get("id")
            for tl in (o.get("ticket_legs") or []):
                lid = tl.get("lota_id")
                if not lid:
                    continue
                all_lids.add(lid)
                if slip_role.get(sid, {}).get(lid) == "单选":
                    single_lids.add(lid)
                    picks = tl.get("picks") or ([tl["pick"]] if tl.get("pick") else [])
                    leg_single_pick.setdefault(lid, picks[0] if picks else "")
        leg_role: dict[str, str] = {
            lid: ("单选" if lid in single_lids else "全包") for lid in all_lids
        }

        # 当日「高波动场次占比」基线（粗筛对照用；只要一个比例）
        day_sps = [float(l.get("sp") or 0)
                   for o in settled_orders for l in (o.get("legs") or [])]
        vol_base = self._day_high_vol_baseline(day_date, day_sps)

        seen: dict[str, dict] = {}
        for o in settled_orders:
            for leg in o.get("legs", []):
                lid = leg.get("lota_id")
                if not lid or lid in seen:
                    continue
                sp = float(leg.get("sp", 0.0) or 0.0)
                actual = leg.get("actual") or ""
                pick = leg.get("pick", "")
                role_label = leg_role.get(lid, "全包")
                if role_label == "单选":
                    # 单选取该场在票中的单选方向（可能与 leg 的 pick 一致）
                    pick = leg_single_pick.get(lid, pick)
                if leg.get("push"):
                    profit = 0.0
                    hit = None
                elif role_label == "单选":
                    hit = (pick == actual)
                    profit = round(2.0 * sp * BEIDAN_RETURN_RATE - 2.0, 2) if hit else -2.0
                else:
                    # 全包腿三路都买：成本 = 3 注 × UNIT_STAKE，只有开奖 SP 高到覆盖三注
                    # 成本（SP > 3/返奖率 ≈ 4.62）才算这条全包腿有价值。
                    # 不能默认 hit=True —— 否则任何 SP≥1.54 的全包腿都会被记成"赚"，
                    # 因子库会被低 SP 正路刷成 hit==total 的假命中。
                    cost = self.UNIT_STAKE * 3
                    profit = round(self.UNIT_STAKE * sp * BEIDAN_RETURN_RATE - cost, 2)
                    hit = profit > 0
                seen[lid] = {
                    "id": f"leg_{lid}",
                    "lota_id": lid,
                    "bet_type": "北单腿",
                    "pick": pick,
                    "actual": actual,
                    "role": role_label,
                    "sp_value": sp,
                    "odds": sp,
                    "bet_size": 2.0,
                    # 样本的注数基数：单选腿 1 注，全包腿 3 注。写进样本后
                    # factor_select 的反推/统计不必再猜口径（老样本靠负回报识别）。
                    "unit_cost": 1.0 if role_label == "单选" else 3.0,
                    # 波动路径的粗筛对照：当日高波动场次占比（标签仍是 hit，不算分布）
                    "vol_base": vol_base if role_label == "全包" else None,
                    "profit": profit,
                    "hit": hit,
                    "reason": (f"{leg.get('home_name', '')} vs "
                               f"{leg.get('away_name', '')} 让{leg.get('goal_line')} "
                               f"| 类型:{role_label} "
                               f"| 【结果标签（只用于判断覆盖值不值，禁止写进因子描述）】"
                               f"实际:{actual or '?'} 开奖SP:{sp:.2f}"),
                }
        leg_samples = list(seen.values())
        if not leg_samples:
            return

        # 波动路径候选：**不再按开奖 SP 排序取 TOP-N**（那是结果条件化选择，
        # 归纳出的因子只会描述事后赢家、学不到区分度）。改为按**赛前离散**分层排序：
        # 低/中/高 + 未知都在样本里，开奖 SP 只作为标签出现。
        from .tools import prematch_dispersion, stratified_pick
        for s in leg_samples:
            s["_disp"] = prematch_dispersion(s.get("lota_id"))
        cover_all = [s for s in leg_samples if s.get("role") == "全包"]
        cover_sorted = stratified_pick(cover_all, len(cover_all),
                                       key=lambda s: s.get("_disp"))
        n_cov = len(cover_sorted) or 1
        for i, s in enumerate(cover_sorted):
            d = s.get("_disp")
            if d is None:
                tag = "赛前离散未知"
            elif i < n_cov / 3:
                tag = "赛前离散低"
            elif i < 2 * n_cov / 3:
                tag = "赛前离散中"
            else:
                tag = "赛前离散高"
            d_txt = "" if d is None else f"离散{d:.2f}｜"
            s["reason"] = f"【{tag}｜{d_txt}】" + s.get("reason", "")

        # 4.3 反思拆分：单选腿走 directional，全包腿走 volatility，互不污染。
        single_samples = [s for s in leg_samples if s.get("role") == "单选"]
        cover_samples = cover_sorted

        from .agent import _rt, node_reflect
        rt = _rt({"user": self.user})
        if rt.provider is None:
            from .providers.deepseek import DeepSeekProvider
            self.set_provider(DeepSeekProvider())
        rt.role = role

        if single_samples:
            rt.last_settled_orders = single_samples
            node_reflect({
                "user": self.user,
                "day_date": day_date or "",
                "reflect_extra": {
                    "factor_scope": "directional",
                    "rich_match_info": True,

                    "extra_only_beidan": True,


                    "system_rules": _REFLECT_GL_RULE + _REFLECT_STRUCTURAL_RULE,
                    "extra_matches": True,
                    "extra_by_sp": False,
                    "extra_max": 3,
                    "parlay_emphasis": False,
                },
            })

        if cover_samples:
            rt.last_settled_orders = cover_samples
            node_reflect({
                "user": self.user,
                "day_date": day_date or "",
                "reflect_extra": {
                    "factor_scope": "volatility",
                    "rich_match_info": True,

                    "extra_only_beidan": True,


                    "system_rules": _REFLECT_GL_RULE + _REFLECT_STRUCTURAL_RULE,
                    "extra_matches": True,
                    # 补充样本也按赛前离散分层（曾经按开奖 SP 取 TOP-N = 结果条件化）
                    "extra_by_sp": False,
                    "extra_by_strata": True,
                    "extra_max": self.REFLECT_HIGH_SP_TOP,
                    "parlay_emphasis": True,
                },
            })


    def _day_high_vol_rate(self, day_date: Optional[str]) -> Optional[float]:
        """当日「错价场次占比」——**波动 v2 口径**的对照基线。

        口径：当日已完场北单比赛里，`开奖SP × p̂ > (1/0.65)^(1/4) = 1.11371`
        （**每腿线**，4 关票口径；不是单关线 `1/0.65`=1.5385 —— 0.65 只在整票收一次）
        的比例。用作波动因子的筛选力对照：某因子命中场次的高波动率
        若与当日基线差不多，它就**没有筛选力**。
        有 Pinnacle 的样本 <5 场返回 None（不拿两场当基线）。
        """
        if not day_date:
            return None
        try:
            pool = self._unplaced_completed(day_date, False, 999)
            rows = [r for r in (
                overlay_of_match(m, self._dm.get_tags(m.get("lota_id") or "") or {})
                for m in pool) if r]
        except Exception:
            return None
        if len(rows) < 5:
            return None
        return sum(1 for r in rows if r["high_vol"]) / len(rows)

    def _day_high_vol_baseline(self, day_date: Optional[str],
                               extra_sp: Optional[list] = None) -> Optional[float]:
        """当日「高波动场次占比」——粗筛基线（一个比例，不做分布计算）。

        口径：当日所有已完场北单比赛里，开奖 SP ≥ `HIGH_VOL_SP`（覆盖成本线）的比例。
        用作波动因子的对照：某因子命中场次的高SP率如果跟当日基线差不多，它就**没有筛选力**。
        样本不足（<5 场）返回 None（不拿两场当基线）。
        """
        sps = []
        for x in (extra_sp or []):
            try:
                v = float(x)
            except (TypeError, ValueError):
                continue
            if v > 0:
                sps.append(v)
        try:
            for m in self._unplaced_completed(day_date, False, 10 ** 6):
                v = float((m.get("beidan_info") or {}).get("spvalue") or 0)
                if v > 0:
                    sps.append(v)
        except Exception:
            pass
        if len(sps) < 5:
            return None
        return sum(1 for v in sps if v >= self.HIGH_VOL_SP) / len(sps)

    def _unplaced_completed(self, day_date: Optional[str], by_sp: bool,
                            limit: int, by_strata: bool = False) -> list[dict]:
        """完场未下单比赛（用于跳单日因子生成）。

        by_strata=True：按**赛前离散**分层抽样（低/中/高），不用开奖 SP 选样本
        —— 波动路径用这个，避免"候选由事后结果决定"。
        """
        import random
        if not day_date:
            return []
        try:
            start_d = date.fromisoformat(day_date)
        except ValueError:
            return []
        start, end = get_football_day(start_d)
        cands: list[dict] = []
        for cd in football_day_calendar_dates(start_d):
            for m in self._dm.get_cached_matches(cd, lottery_type="all"):
                lid = m.get("lota_id")
                if not lid:
                    continue
                mt = m.get("match_time", "")
                if not (start <= mt <= end):
                    continue
                if m.get("state") != 6:
                    continue
                cands.append(m)
        if not cands:
            return []
        # 用每场 match_time 反推北单 SP 日期，直接合并 legacy/beidan_sp 开奖结果。
        for m in cands:
            lid = m.get("lota_id")
            info = dict(m.get("beidan_info") or {})
            sp_date = self._football_day_start_from_match_time(m.get("match_time"))
            if sp_date:
                for lm in self._dm._read_legacy_beidan(sp_date):
                    if lm.get("lota_id") == lid:
                        info.update(lm.get("beidan_info") or {})
                        break
                sp_info = self._dm.get_beidan_sp_cache(sp_date).get(lid)
                if sp_info:
                    info.update(sp_info)
            m["beidan_info"] = info
        cands = [m for m in cands
                 if (m.get("beidan_info") or {}).get("result") not in (None, "")]
        if by_strata:
            from .tools import prematch_dispersion, stratified_pick
            return stratified_pick(cands, limit,
                                   key=lambda m: prematch_dispersion(m.get("lota_id", "")))
        if by_sp:
            cands.sort(key=lambda m: -float((m.get("beidan_info") or {}).get("spvalue") or 0))
        else:
            random.shuffle(cands)
        return cands[:limit]

    def _reflect_skipped(self, role: Role, day_date: Optional[str]) -> None:
        """前一日未下单（skip）时仍生成因子：
        - 方向：随机 6 场完场未下单比赛
        - 波动（**v2，2026-09-14**）：当日**错价场次** —— `开奖SP × p̂ > 1.11371`
          （= `(1/0.65)^(1/4)`，**每腿线**／4 关票口径；p̂ 用 Pinnacle 1X2 经 Poisson
          归一到该场 goal_line），比例降序取前
          `HIGH_VOL_TOP`(=10) 条；样本 pick/hit/profit 按**引擎 gated 侧**算。
          旧口径是「按赛前离散分层抽 5 场当全包腿」—— `M过(N−4)` 票型下不再有
          全包腿，故废弃。

        ⚠️ 这是**观察型因子**的唯一来源（仿真人复盘，合理）。2026-09-13 起**带虚拟结算**：
        买单侧由引擎真实规则 `x ≥ θ` 决定（不由赛果反推 ⇒ 不是保送全中），再按开奖判命中：
            方向型：命中 ⇔ 实际方向 ∈ 过门侧；profit = 0.65×SP − 1（中）/ −1（不中）
            波动 v2（2026-09-14 起）：同样按过门侧算 —— 只有**入选条件**不同
               （当日错价场次 `SP×p̂ > (1/0.65)^(1/4)`，比例降序前 10），
               不再用旧全包口径（命中 ⇔ SP ≥ 3/0.65）。
        实测虚拟每元 −31.9%，与真实落单腿级 −29.7% 一致 ⇒ 口径可信。
        改前是 hit=None/profit=0，导致观察因子**无法被打分、无法退役、只增不减**
        （45 天里观察型占 47~48%，从不下降）。`DS_NO_SKIP_REFLECT=1` 仍可整条关闭该路径。
        """
        if os.environ.get("DS_NO_SKIP_REFLECT", "").strip().lower() in ("1", "true", "on", "yes"):
            print("  ⏭ 跳过观察型因子生成（DS_NO_SKIP_REFLECT=1）：无下单日不造因子")
            return
        from .agent import _rt, node_reflect
        from .beidan_settlement import result_code_to_pick

        # 门限 θ：取该日账本门策略，缺省回落配置默认
        try:
            _cfg = self._load_parlay_config()
            _pol = self._pool_gate_policy(_cfg, as_of=day_date) or {}
            theta = float(_pol.get("theta")
                          or (_cfg.get("pool_gate") or {}).get("default_theta") or 1.1)
        except Exception:
            theta = 1.1

        def _gated_sides(m: dict) -> list[str]:
            """按引擎规则算该场过门侧（x = 市场p̂ × 北单赔率 ≥ θ）；拿不到数据返回 []。"""
            try:
                _src, p_mkt = self._market_p_hat(m)
                if not p_mkt:
                    return []
                o = self._beidan_odds(m) or {}
                out = []
                for side, key in (("H", "h"), ("D", "d"), ("A", "a")):
                    odds = float(o.get(key) or 0.0)
                    pm = float(p_mkt.get(side) or 0.0)
                    if odds > 0 and pm > 0 and pm * odds >= theta:
                        out.append(side)
                return out
            except Exception:
                return []

        # 当日高波动基线（粗筛对照）
        vol_base = self._day_high_vol_baseline(day_date)

        def _samples(matches: list[dict], is_cover: bool,
                     vol_base_override: float = None) -> list[dict]:
            out = []
            for m in matches:
                lid = m.get("lota_id")
                bi = m.get("beidan_info") or {}
                raw = bi.get("result")
                actual = (result_code_to_pick(str(raw).strip())
                          if raw not in (None, "") else None)
                sp = float(bi.get("spvalue") or 0.0)
                if not lid or actual is None or sp <= 0:
                    continue
                goal_line = bi.get("goal_line")
                if goal_line is None:
                    goal_line = m.get("goal_line", 0.0)
                # 2026-09-13 起改为**带虚拟结算**（见方法 docstring）：
                # 买单侧由引擎真实规则 x≥θ 决定，不由赛果反推 ⇒ 不是保送全中。
                gated = _gated_sides(m)
                if is_cover:
                    # 全包腿：命中 ⇔ 开奖 SP 覆盖三注成本；按 3 注成本摊每元
                    hit = sp >= self.HIGH_VOL_SP
                    profit = round((BEIDAN_RETURN_RATE * sp - 3.0) / 3.0, 4) if hit else -1.0
                    pick = "全包"
                    role_label = "全包"
                    unit_cost = 3.0
                    value_note = (f" | 观察样本·虚拟结算(全包, 成本线 SP≥{self.HIGH_VOL_SP:.2f})")
                else:
                    # 方向腿：无过门侧 ⇒ 该场本就不该买，不计分（保持诚实）
                    if not gated:
                        continue
                    hit = actual in gated
                    profit = round(BEIDAN_RETURN_RATE * sp - 1.0, 4) if hit else -1.0
                    pick = "/".join(gated)
                    role_label = "单选" if len(gated) == 1 else "多选"
                    unit_cost = 1.0
                    value_note = f" | 观察样本·虚拟结算(按 x≥{theta:g} 买的 {pick})"
                out.append({
                    "id": f"leg_{lid}",
                    "lota_id": lid,
                    "bet_type": "北单腿",
                    "pick": pick,
                    "actual": actual,
                    "role": role_label,
                    "sp_value": sp,
                    "odds": sp,
                    "bet_size": 2.0,
                    "unit_cost": unit_cost,
                    "vol_base": (vol_base_override if vol_base_override is not None
                                 else (vol_base if is_cover else None)),
                    "profit": profit,
                    "hit": hit,
                    "observation": True,
                    "reason": (f"{m.get('home_name', '')} vs {m.get('away_name', '')} "
                               f"让{goal_line} | 类型:{role_label} | "
                               f"实际:{actual} | 开奖SP:{sp:.2f}" + value_note),
                })
            return out

        direction_samples = _samples(self._unplaced_completed(day_date, False, 6), False)

        # ── 波动 v2（2026-09-14 用户口径）──
        # 旧口径按「全包腿」定义高波动；`M过(N−4)` 票型下不再有全包腿，改为按
        # **错价比例**筛：比例 = 开奖SP / Pinnacle价格（Pinnacle 1X2 经 Poisson
        # 归一到该场 goal_line），只有 `开奖SP × p̂ > (1/0.65)^(1/4) = 1.11371`
        # （**每腿线**／4 关票口径，0.65 只在整票收一次）入选，
        # 多于 HIGH_VOL_TOP 条按比例降序截断（省 token）。
        # 样本的 pick/hit/profit 一律走**引擎 gated 侧（x≥θ）**，不由开奖反推。
        # 单日池（2026-09-14 用户口径）：不滚动 —— 滚动窗口下每天 top10 与前一日
        # 重复 8~10 条，等于天天对同一批反复反思，白烧 token。
        vol_pool = self._unplaced_completed(day_date, False, 999)
        vol_rows = select_high_vol(
            vol_pool,
            tags_of=lambda lid: self._dm.get_tags(lid) or {},
            top=HIGH_VOL_TOP,
        )
        _by_lid = {m.get("lota_id"): m for m in vol_pool}
        vol_matches = [_by_lid[r["lota_id"]] for r in vol_rows if r["lota_id"] in _by_lid]
        volatility_samples = _samples(
            vol_matches, False, vol_base_override=self._day_high_vol_rate(day_date))
        if vol_rows:
            # ⚠️ 报**真正喂进去的样本数**（vol_matches 里没 gated 侧的会被 _samples 丢掉），
            # 不要拿 vol_matches 冒充"有 gated 侧"（2026-09-14 自查修正）。
            print(f"  🌊 波动 v2：当日池 {len(vol_pool)} 场 → 错价比例 >"
                  f"{HIGH_VOL_RATIO_THRESHOLD:.4f} 的 {len(vol_rows)} 场 → "
                  f"gated 侧样本 {len(volatility_samples)} 条"
                  + ("（已截断 top10）" if len(vol_rows) >= HIGH_VOL_TOP else ""))
        else:
            print(f"  🌊 波动 v2：当日池 {len(vol_pool)} 场 → 无错价场次，跳过")

        if not direction_samples and not volatility_samples:
            return

        rt = _rt({"user": self.user})
        if rt.provider is None:
            from .providers.deepseek import DeepSeekProvider
            self.set_provider(DeepSeekProvider())
        rt.role = role

        if direction_samples:
            rt.last_settled_orders = direction_samples
            node_reflect({
                "user": self.user,
                "day_date": day_date or "",
                "reflect_extra": {
                    "factor_scope": "directional",
                    "rich_match_info": True,

                    "extra_only_beidan": True,


                    "system_rules": _REFLECT_GL_RULE + _REFLECT_STRUCTURAL_RULE,
                    "extra_matches": False,
                    "parlay_emphasis": False,
                },
            })

        if volatility_samples:
            rt.last_settled_orders = volatility_samples
            node_reflect({
                "user": self.user,
                "day_date": day_date or "",
                "reflect_extra": {
                    "factor_scope": "volatility",
                    "rich_match_info": True,

                    "extra_only_beidan": True,


                    "system_rules": _REFLECT_GL_RULE + _REFLECT_STRUCTURAL_RULE,
                    "extra_matches": False,
                    "extra_by_sp": False,
                    "parlay_emphasis": True,
                },
            })

    # ═══════════════════════════════════════════
    # 状态 / 回测
    # ═══════════════════════════════════════════

    def pending(self) -> list[dict]:
        role = self._ensure_role()
        return [o for o in role.get_orders() if not o.get("settled_at")]

    def status(self) -> dict:
        role = self._ensure_role()
        orders = role.get_orders()
        settled = [o for o in orders if o.get("settled_at")]
        return {
            "user": self.user,
            "capital": role.capital,
            "total_orders": len(orders),
            "settled": len(settled),
            "pending": len(orders) - len(settled),
            "pnl": round(sum(float(o.get("profit", 0)) for o in settled), 2),
        }

    def reset(self, capital: float = None) -> dict:
        role = self._ensure_role()
        role.capital = float(capital) if capital else float(self.START_CAPITAL)
        role.initial_capital = role.capital
        role.orders = []
        role.save()
        return {"capital": role.capital, "orders": len(role.orders)}

    def compact(self) -> dict:
        """把 role.orders 压缩为 slip 级（一张票一条，8 条腿存一次），并落盘。"""
        role = self._ensure_role()
        before = len(role.orders)
        role.orders = compact_slip_orders(role.orders)
        role.save()
        return {"before": before, "after": len(role.orders)}

    def backtest(self, start_date: str, end_date: str,
                 max_picks: int = None, tickets: list[str] = None) -> dict:
        d = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        rows = []
        while d <= end:
            ds = d.isoformat()
            a = self.analyze(ds, max_picks=max_picks, tickets=tickets)
            s = self.settle(ds)
            rows.append({
                "date": ds,
                "matches": a.get("matches_count", 0),
                "legs": a.get("legs_selected", 0),
                "tickets": a.get("tickets", []),
                "placed": a.get("placed", 0),
                "settled": s.get("settled", 0),
                "hit": s.get("hit", 0),
                "miss": s.get("miss", 0),
                "push": s.get("push", 0),
                "pnl": s.get("pnl", 0.0),
                "capital": self._ensure_role().capital,
            })
            d += timedelta(days=1)
        totals = {
            "placed": sum(r["placed"] for r in rows),
            "settled": sum(r["settled"] for r in rows),
            "hit": sum(r["hit"] for r in rows),
            "miss": sum(r["miss"] for r in rows),
            "push": sum(r["push"] for r in rows),
            "pnl": round(sum(r["pnl"] for r in rows), 2),
            "capital": rows[-1]["capital"] if rows else 0,
            "empty_days": sum(1 for r in rows if r["placed"] == 0),
        }
        return {"days": rows, "totals": totals}


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

def compact_slip_orders(orders: list[dict]) -> list[dict]:
    """把旧版「每注一条 order」压缩成「每票一条 slip order」。

    旧版（展开组合）会把一张 8串1 拆成 3^5=243 条 order，每条都重复存储
    ticket_legs/legs 的完整腿信息，导致 role.json 无限膨胀到几十 MB。
    此函数按 slip_id 归并为一张票：k 条腿（含 picks 列表）只存一次，
    组合不再落盘（settle 现算），并从各注回填腿级结算结果/票级汇总。
    """
    from collections import OrderedDict

    groups: dict[str, list[dict]] = OrderedDict()
    for o in orders or []:
        sid = o.get("slip_id") or o.get("id") or ""
        if sid:
            groups.setdefault(sid, []).append(o)
        else:
            groups.setdefault(o.get("id") or f"_k{len(groups)}", []).append(o)

    out: list[dict] = []
    for sid, grp in groups.items():
        first = grp[0]
        ticket_legs = list(first.get("ticket_legs") or first.get("legs") or [])
        unit = float(first.get("bet_size") or first.get("unit_stake") or 2.0)
        combos_count = int(first.get("combos_count") or len(grp))

        # 回填腿级结果：从任意已结算注的 legs 取 actual/sp/push/hit 按 lota_id 归并
        by_lid: dict[str, dict] = {}
        for o in grp:
            for l in (o.get("legs") or []):
                lid = l.get("lota_id") or ""
                if lid and lid not in by_lid:
                    by_lid[lid] = dict(l)
        for l in ticket_legs:
            lid = l.get("lota_id") or ""
            src = by_lid.get(lid)
            if not src:
                continue
            for k in ("actual", "sp", "push", "hit"):
                if k in src and src[k] is not None:
                    l[k] = src[k]

        settled_at = ""
        for o in grp:
            sa = o.get("settled_at") or ""
            if sa and sa > settled_at:
                settled_at = sa
        hit = any(bool(o.get("hit")) for o in grp)
        all_void = bool(grp) and all(bool(o.get("all_void")) for o in grp)
        return_amount = _round2(sum(float(o.get("return_amount") or 0.0) for o in grp))
        profit = _round2(sum(float(o.get("profit") or 0.0) for o in grp))
        sp_product = 1.0
        for l in ticket_legs:
            sp_product *= float(l.get("sp") or 1.0)

        out.append({
            "id": first.get("id") or _uid("ord_"),
            "slip_id": sid,
            "slip_type": first.get("slip_type") or first.get("ticket_type") or "",
            "slip_index": 1,
            "combos_count": combos_count,
            "unit_stake": unit,
            "total_stake": _round2(float(first.get("total_stake") or unit * combos_count)),
            "ticket_legs": ticket_legs,
            "predict_id": first.get("predict_id", ""),
            "lota_id": (ticket_legs[0]["lota_id"] if ticket_legs
                        else (first.get("lota_id") or "")),
            "bet_type": first.get("bet_type", ""),
            "ticket_type": first.get("ticket_type") or first.get("slip_type") or "",
            "pick": first.get("pick", ""),
            "odds": first.get("odds", 0.0),
            "bet_size": first.get("bet_size", unit),
            "legs": ticket_legs,
            "created_at": first.get("created_at", ""),
            "settled_at": settled_at or None,
            "hit": hit,
            "all_void": all_void,
            "return_amount": return_amount,
            "profit": profit,
            "settlement_rate": first.get("settlement_rate"),
            "sp_product": round(sp_product, 6),
        })
    return out


def _fmt_order(o: dict) -> str:
    legs = o.get("legs", [])
    def _leg_pick(l: dict) -> str:
        picks = l.get("picks") or ([l.get("pick")] if l.get("pick") else [])
        return "/".join(picks)

    def _leg_hc(l: dict) -> str:
        v = l.get("goal_line")
        return str(v) if isinstance(v, (int, float)) else ""

    leg_txt = " + ".join(
        f"{l.get('home_name','?')}vs{l.get('away_name','?')} {_leg_pick(l)}"
        f"({_leg_hc(l)})"
        for l in legs
    )
    slip = o.get("slip_type", "")
    tag = f"[{slip} {o.get('combos_count','')}注]" if slip else f"[{o.get('ticket_type','串关')}]"
    return f"{tag} {o.get('ticket_type','北单串关')} 买 {leg_txt} | 总赔率 {o.get('odds',0):.2f} 投注 {o.get('total_stake', o.get('bet_size',0)):.2f}"


def main(argv: list[str] = None) -> int:
    p = argparse.ArgumentParser(prog="beidan_parlay_dog", description="bc狗")
    p.add_argument("action", choices=["analyze", "settle", "pending", "status", "reset",
                                      "compact", "backtest", "prompt"])
    p.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD（足球日起始日，默认当天）")
    p.add_argument("end", nargs="?", default=None, help="backtest 结束日 YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="只预览不落单")
    p.add_argument("--llm", action="store_true", help="用 LLM 分析选腿（默认规则版）")
    p.add_argument("--tickets", default=None, help="逗号分隔，如 6串1,7串1")
    p.add_argument("--picks", type=int, default=None,
                   help="统一模式每腿最多选项数 1/2/3（不传则用默认 8串1:5全包+3单选）")
    p.add_argument("--stake-pct", type=float, default=None,
                   help="北单每注固定 2 元，此参数仅兼容保留，不参与计算")
    p.add_argument("--user", default="bc狗", help="角色名（独立资金/订单）")
    p.add_argument("--out", default="docs/prompts", help="prompt 动作的输出目录")
    args = p.parse_args(argv)

    dog = BeidanParlayDog(user=args.user)
    if args.action == "analyze":
        tickets = [t.strip() for t in args.tickets.split(",")] if args.tickets else None
        r = dog.analyze(args.day, dry_run=args.dry_run, tickets=tickets,
                        stake_pct=args.stake_pct, max_picks=args.picks,
                        use_llm=args.llm)
        tks = "+".join(r.get("tickets") or []) or "空仓"
        src_tag = "🧠LLM" if r.get("llm_used") else "📐规则"
        print(f"📅 {r['date']} 北单场次 {r['matches_count']} | 候选腿 {r['legs_selected']} "
              f"| {src_tag} | 每腿≤{r['max_picks']}选 | 票型 {tks}")
        for o in r["orders"]:
            print("  " + _fmt_order(o))
        if r["skipped"]:
            print("  跳过:", "; ".join(r["skipped"]))
        total_bet = sum(float(o["bet_size"]) for o in r["orders"])
        print(f"{'🔍 dry-run 预览' if r['dry_run'] else '✅ 已下单'}: "
              f"{r['placed']} 注 | 总投注 {total_bet:.0f} 元")
    elif args.action == "prompt":
        # 只构建 prompt、不调 LLM（step-by-step review 用）
        r = dog.dump_prompts(args.day or dog._default_day(), live=False,
                             out_dir=args.out)
        print(f"📝 prompt dump（未调用 LLM）：{r['day']} | 场次 {r['matches_count']} "
              f"| LLM 调用点 {len(r['calls'])} 个")
        for c in r["calls"]:
            print(f"   #{c['i']} {c['stage']}: system {c['system_chars']} 字符 + "
                  f"user {c['user_chars']} 字符 ≈ {c['tokens']} tokens")
        print(f"   落单 {r['orders']} 张（应为 0），票型 {r['tickets']}")
        for f in r["files"]:
            print("   📄 " + f)
    elif args.action == "settle":
        r = dog.settle(args.day)
        print(f"📊 结算: {r['settled']} 张子单 命中{r['hit']} 未中{r['miss']} "
              f"退款{r['push']} PnL {r['pnl']:+.2f}")
    elif args.action == "pending":
        for o in dog.pending():
            print("  ⏳ " + _fmt_order(o))
    elif args.action == "status":
        s = dog.status()
        print(f"💰 资金 {s['capital']:.2f} | 订单 {s['total_orders']} "
              f"(已结{s['settled']}/待{s['pending']}) | PnL {s['pnl']:+.2f}")
    elif args.action == "reset":
        r = dog.reset()
        print(f"♻️ 已重置: 资金 {r['capital']:.2f} | 订单 {r['orders']}")
    elif args.action == "compact":
        r = dog.compact()
        print(f"♻️ 已压缩: {r['before']} -> {r['after']} 条 (slip 级)")
    elif args.action == "backtest":
        if not args.day or not args.end:
            print("用法: python -m src.beidan_parlay_dog backtest <start> <end> [--picks 2] [--tickets 6串1]")
            return 1
        tickets = [t.strip() for t in args.tickets.split(",")] if args.tickets else None
        r = dog.backtest(args.day, args.end, max_picks=args.picks, tickets=tickets)
        print("日期 | 北单 | 腿 | 票型 | 子单 | 结算(中/挂/退) | 当日PnL | 资金")
        for row in r["days"]:
            tks = "+".join(row["tickets"]) or "空仓"
            print(f"{row['date']} | {row['matches']:>2} | {row['legs']} | {tks:<6} | "
                  f"{row['placed']:>3} | {row['settled']}({row['hit']}/{row['miss']}/{row['push']}) | "
                  f"{row['pnl']:+.2f} | {row['capital']:.2f}")
        t = r["totals"]
        print(f"汇总: 子单{t['placed']} 结算{t['settled']} 中{t['hit']} 挂{t['miss']} "
              f"退{t['push']} 空仓日{t['empty_days']} | PnL {t['pnl']:+.2f} | 资金 {t['capital']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
