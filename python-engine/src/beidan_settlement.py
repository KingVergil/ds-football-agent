"""
北单（北京单场）串关结算引擎 — 纯函数实现，不依赖 LLM / 数据层，便于单测。

口径（与官方一致，2026-08 需求）：
  - 让球胜平负开奖结果 code: "3"=主胜(H) / "1"=平(D) / "0"=负(A) / "*"=延期取消
  - 中奖奖金 = 单注本金 × 所选场次开奖SP值连乘 × 65%
    即北单整体返奖率为 65%，spvalue 是「开奖SP」（未再乘 65%）。
  - 过关（串关）：每注全腿命中才中奖；某腿延期取消按 SP=1.0 处理，
    剩余腿继续连乘；全部腿取消则整注按本金退还（不乘 65%）。

这里的「本金」对应 order.bet_size，与官方 2 元/注只是比例不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Optional


# ═══════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════

BEIDAN_RETURN_RATE = 0.65          # 北单整体返奖率（官方口径）
VOID_RESULT = "*"                  # 延期/取消的开奖结果
VOID_SP = 1.0                      # 延期/取消腿在过关里的 SP 值
BEIDAN_TAX_THRESHOLD = 10000.0     # 单注中奖金额超过该值需缴个人所得税
BEIDAN_TAX_RATE = 0.20             # 个税税率（就全额按 20%）

# 开奖结果 code → 让球胜平负方向
RESULT_CODE_TO_PICK = {"3": "H", "1": "D", "0": "A"}
PICK_TO_RESULT_CODE = {"H": "3", "D": "1", "A": "0"}


# ═══════════════════════════════════════════
# 基础换算
# ═══════════════════════════════════════════

def result_code_to_pick(result: Optional[str]) -> Optional[str]:
    """开奖结果 code → H/D/A；延期/取消或未知返回 None。"""
    if result is None:
        return None
    s = str(result).strip()
    return RESULT_CODE_TO_PICK.get(s)


def handicap_result(score: Optional[str], goal_line) -> Optional[str]:
    """按让球线从最终比分推导让球胜平负方向（用于校验官方 result）。

    goal_line: 主队让球数（负=主让，正=主受，0=不让，与数据一致）。
    """
    if not score or ":" not in score:
        return None
    try:
        h_s, a_s = score.split(":", 1)
        h = int(h_s)
        a = int(a_s)
    except (ValueError, TypeError):
        return None
    try:
        gl = float(goal_line)
    except (TypeError, ValueError):
        gl = 0.0
    adj = (h - a) + gl
    if adj > 0:
        return "H"
    if adj < 0:
        return "A"
    return "D"


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round2(value: float) -> float:
    return round(value, 2)


# ═══════════════════════════════════════════
# 单腿结算
# ═══════════════════════════════════════════

def settle_leg(pick: str, beidan_info: Optional[dict]) -> dict:
    """结算单腿单个选项（单选）。

    返回:
      ready: False 表示该场尚未开奖（result 缺失），不能结算
      hit:   是否命中（延期/取消按命中处理，push=True）
      push:  是否延期/取消
      sp:    该腿开奖 SP（延期/取消为 1.0）
      actual: 官方 result 归一后的 H/D/A（延期/取消为 None）
      expected: 由 score+goal_line 推导的方向（校验用）
      mismatch: actual 与 expected 不一致（仅告警，不影响以官方为准）
    """
    info = beidan_info or {}
    raw_result = info.get("result")
    if raw_result is None or str(raw_result).strip() == "":
        return {"ready": False, "reason": "no_result", "hit": False, "push": False,
                "sp": 0.0, "actual": None, "expected": None, "mismatch": False,
                "result_code": None}

    result_s = str(raw_result).strip()
    if result_s == VOID_RESULT:
        return {"ready": True, "hit": True, "push": True, "sp": VOID_SP,
                "actual": None, "expected": None, "mismatch": False,
                "result_code": VOID_RESULT}

    actual = result_code_to_pick(result_s)
    sp = _to_float(info.get("spvalue"))
    expected = handicap_result(info.get("score"), info.get("goal_line"))
    mismatch = bool(actual and expected and actual != expected)
    hit = (pick == actual)
    return {"ready": True, "hit": hit, "push": False, "sp": sp,
            "actual": actual, "expected": expected, "mismatch": mismatch,
            "result_code": result_s}


# ═══════════════════════════════════════════
# 多选展开（每腿 1~3 选 → 笛卡尔积子注）
# ═══════════════════════════════════════════

def expand_leg_picks(legs: list[dict]) -> list[list[dict]]:
    """把「每腿多选」展开为「每腿单选」的笛卡尔积组合。

    每个 leg 需含:
      picks: list[str]     可选方向（如 ["H","D"]）
      odds:  dict[str,float] 各方向的赛前赔率占位（可选）
    返回 list[list[leg dict]]，每个子组合中 leg["pick"] 为单值、leg["odds"] 为对应赔率。
    """
    if not legs:
        return [[]]
    head, *tail = legs
    rest = expand_leg_picks(tail)
    out: list[list[dict]] = []
    for pick in (head.get("picks") or []):
        leg_one = dict(head)
        leg_one["pick"] = pick
        odds_map = head.get("odds") if isinstance(head.get("odds"), dict) else {}
        leg_one["odds"] = odds_map.get(pick, 0.0)
        leg_one["picks"] = list(head.get("picks") or [])
        for r in rest:
            out.append([leg_one] + r)
    return out


# ═══════════════════════════════════════════
# 票型（结构化类型）与注数计算
# ═══════════════════════════════════════════

@dataclass(frozen=True)
class TicketSpec:
    """串关票型结构：N串1（kind='串'，m=n）或 N过M（kind='过'，m<n）。"""
    n: int
    m: int
    kind: str

    @property
    def label(self) -> str:
        return f"{self.n}串1" if self.kind == "串" else f"{self.n}过{self.m}"


def parse_ticket_spec(spec: Optional[str]) -> Optional[TicketSpec]:
    """解析票型字符串：'8串1' / '8过5'。"""
    if not spec:
        return None
    for sep in ("串", "过"):
        if sep in spec:
            try:
                n = int(spec.split(sep)[0])
                m = int(spec.split(sep)[1]) if sep == "过" else n
            except (ValueError, IndexError):
                return None
            return TicketSpec(n=n, m=m, kind=sep)
    return None


def parlay_leg_combinations(legs: list[dict], spec: TicketSpec) -> list[list[dict]]:
    """按票型取腿组合（不含多选展开）：
    N串1 = [前 N 腿]；N过M = 前 N 腿中所有 C(N,M) 个 M 腿组合。"""
    if spec.m < 2 or spec.n < spec.m or spec.n > len(legs):
        return []
    chosen = legs[: spec.n]
    if spec.m == spec.n:
        return [list(chosen)]
    return [list(c) for c in combinations(chosen, spec.m)]


def parlay_combos_count(legs: list[dict], spec: TicketSpec) -> int:
    """结构化注数计算：
    N串1 = ∏(每腿 options)；N过M = Σ_{C(N,M)} ∏(该 M 腿组合 options)。"""
    total = 0
    for leg_combo in parlay_leg_combinations(legs, spec):
        p = 1
        for leg in leg_combo:
            p *= len(leg.get("picks") or [])
        total += p
    return total


def parlay_combinations(legs: list[dict], spec: TicketSpec) -> list[list[dict]]:
    """结构化组合展开（含多选笛卡尔积），每个元素为「每腿单选」的子注。"""
    out: list[list[dict]] = []
    for leg_combo in parlay_leg_combinations(legs, spec):
        out.extend(expand_leg_picks(leg_combo))
    return out


def validate_parlay_type(legs: list[dict], spec: TicketSpec) -> dict:
    """按票型验证：展开数量 == 注数 == 独立参考实现。"""
    combos = parlay_combinations(legs, spec)
    count = parlay_combos_count(legs, spec)

    # 独立参考实现：暴力枚举 C(N,M) 腿组合，再对每腿 options 相乘
    ref = 0
    for leg_combo in parlay_leg_combinations(legs, spec):
        p = 1
        for leg in leg_combo:
            p *= len(leg.get("picks") or [])
        ref += p
    return {
        "spec": spec.label,
        "n": spec.n,
        "m": spec.m,
        "kind": spec.kind,
        "combos_count": count,
        "expanded_count": len(combos),
        "reference_count": ref,
        "ok": (count == len(combos) == ref),
    }


# ═══════════════════════════════════════════
# 过关（串关）结算
# ═══════════════════════════════════════════

def settle_parlay_combo(combo_legs: list[dict], beidan_map: dict[str, dict],
                        bet_size: float = 0.0) -> dict:
    """结算一张「每腿单选」的过关子单（N串1 的一注）。

    combo_legs: 本注各腿，每腿含 lota_id + pick（单选），可含其他展示字段。
    beidan_map: {lota_id: beidan_info}。
    bet_size:   本注本金。

    返回:
      ready: False 表示有腿缺开奖结果（未到结算时点），不能结算。
      hit / push / return_amount / profit / sp_product / leg_results。
    """
    leg_results: list[dict] = []
    sp_product = 1.0
    non_void_count = 0

    for leg in combo_legs:
        lid = leg.get("lota_id") or ""
        info = beidan_map.get(lid)
        if info is None:
            return {"ready": False, "reason": f"missing_beidan_info:{lid}"}
        sl = settle_leg(leg.get("pick"), info)
        if not sl.get("ready"):
            return {"ready": False, "reason": f"no_result:{lid}"}

        merged = dict(leg)
        merged.update(sl)
        merged["lota_id"] = lid
        leg_results.append(merged)

        if not sl.get("push"):
            non_void_count += 1
            sp_product *= sl.get("sp", 0.0)
        else:
            sp_product *= VOID_SP

    all_hit = bool(leg_results) and all(r.get("hit") for r in leg_results)
    all_void = non_void_count == 0

    if not all_hit:
        return_amount = 0.0
        profit = _round2(-float(bet_size))
        hit = False
    elif all_void:
        # 整注全部延期/取消：按本金退还（官方口径，不乘 65%）
        return_amount = _round2(float(bet_size))
        profit = 0.0
        hit = True
    else:
        return_amount = _round2(float(bet_size) * sp_product * BEIDAN_RETURN_RATE)
        if return_amount > BEIDAN_TAX_THRESHOLD:
            return_amount = _round2(return_amount * (1 - BEIDAN_TAX_RATE))
        profit = _round2(return_amount - float(bet_size))
        hit = True

    return {
        "ready": True,
        "hit": hit,
        "push": all_hit and any(r.get("push") for r in leg_results),
        "all_void": all_void,
        "sp_product": round(sp_product, 6),
        "return_amount": return_amount,
        "profit": profit,
        "leg_results": leg_results,
    }


def settle_multi_pick_parlay(legs: list[dict], beidan_map: dict[str, dict],
                             bet_size: float = 0.0) -> dict:
    """结算一张「每腿可多选」的过关票：先展开笛卡尔积，再逐注结算。

    主要用于独立验证/单测；生产下单时已在 analyze 阶段展开成单注 order。
    返回汇总: 注数、命中注数、总派奖、总盈利、每注明细。
    """
    combos = expand_leg_picks(legs)
    sub_bet = _round2(float(bet_size) / len(combos)) if combos else 0.0
    settled = []
    total_return = 0.0
    hit_count = 0
    for combo in combos:
        r = settle_parlay_combo(combo, beidan_map, sub_bet)
        settled.append(r)
        if r.get("ready") and r.get("hit"):
            hit_count += 1
            total_return += r.get("return_amount", 0.0)
    return {
        "combos_count": len(combos),
        "hit_count": hit_count,
        "total_return": _round2(total_return),
        "total_profit": _round2(total_return - float(bet_size)),
        "settled": settled,
    }
