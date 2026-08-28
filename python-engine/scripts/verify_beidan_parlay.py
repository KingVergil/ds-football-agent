"""
北单串关结算正确性验证（随机生成串关，不介入角色）。

做法：
  1. 全量扫描 data/beidan/*.json，验证官方 result 与 score+goal_line 推导一致；
  2. 随机抽取已开奖场次，随机组 2~8 腿串关、每腿随机 1~3 选；
  3. 用独立参考实现（官方公式：命中腿 spvalue 连乘 × 65%，延期按 SP=1、全延期退本金）
     与 src.beidan_settlement 的输出逐注比对；
  4. 同时校验多选笛卡尔积展开数量与派奖金额。

运行:
  python3 scripts/verify_beidan_parlay.py [trials] [seed]
"""

from __future__ import annotations

import glob
import itertools
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_settlement import (  # noqa: E402
    BEIDAN_RETURN_RATE,
    expand_leg_picks,
    settle_multi_pick_parlay,
    settle_parlay_combo,
)


REF_RETURN_RATE = 0.65
REF_RESULT_TO_PICK = {"3": "H", "1": "D", "0": "A"}
REF_PICK_TO_RESULT = {"H": "3", "D": "1", "A": "0"}


def load_drawn_matches() -> list[dict]:
    """读取全部已开奖北单比赛（beidan_info 有 result 与 spvalue）。"""
    out: list[dict] = []
    for p in sorted(glob.glob(str(ROOT / "data" / "beidan" / "*.json"))):
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        matches = data if isinstance(data, list) else data.get("matches", [])
        for m in matches:
            bi = m.get("beidan_info")
            if not bi:
                continue
            r = str(bi.get("result") or "").strip()
            if r and bi.get("spvalue") not in (None, "", 0):
                out.append(m)
    return out


def ref_handicap_result(score, goal_line):
    if not score or ":" not in score:
        return None
    try:
        h, a = score.split(":", 1)
        h, a = int(h), int(a)
    except (ValueError, TypeError):
        return None
    try:
        gl = float(goal_line)
    except (TypeError, ValueError):
        gl = 0.0
    adj = (h - a) + gl
    return "H" if adj > 0 else ("A" if adj < 0 else "D")


def ref_leg_hit(pick, info):
    r = str(info.get("result") or "").strip()
    if r == "*":
        return "push", 1.0
    actual = REF_RESULT_TO_PICK[r]
    return ("hit" if pick == actual else "miss"), float(info.get("spvalue") or 0.0)


def ref_settle_combo(combo, beidan_map, bet_size):
    sp = 1.0
    non_void = 0
    for leg in combo:
        info = beidan_map[leg["lota_id"]]
        status, s = ref_leg_hit(leg["pick"], info)
        if status == "miss":
            return {"hit": False, "sp_product": round(sp * s, 6),
                    "return_amount": 0.0}
        if status == "hit":
            non_void += 1
            sp *= s
        else:  # push: SP=1
            sp *= 1.0
    if non_void == 0:
        return {"hit": True, "sp_product": 1.0,
                "return_amount": round(bet_size, 2)}
    return {"hit": True, "sp_product": round(sp, 6),
            "return_amount": round(bet_size * sp * REF_RETURN_RATE, 2)}


def ref_expand(legs):
    """独立实现的多选笛卡尔积展开。"""
    if not legs:
        return [[]]
    head, *tail = legs
    rest = ref_expand(tail)
    out = []
    for pick in head["picks"]:
        for r in rest:
            out.append([{"lota_id": head["lota_id"], "pick": pick}] + r)
    return out


def verify_goal_line_consistency(matches) -> tuple[int, int]:
    checked = 0
    mismatches = []
    for m in matches:
        bi = m["beidan_info"]
        r = str(bi.get("result") or "").strip()
        if r in ("", "*"):
            continue
        actual = REF_RESULT_TO_PICK[r]
        expected = ref_handicap_result(bi.get("score"), bi.get("goal_line"))
        checked += 1
        if actual != expected:
            mismatches.append((m.get("lota_id"), bi.get("score"),
                               bi.get("goal_line"), r, actual, expected))
    return checked, mismatches


def random_parlay(matches, rng, max_legs=8):
    n_legs = rng.randint(2, max_legs)
    chosen = rng.sample(matches, k=n_legs)
    legs = []
    for m in chosen:
        bi = m["beidan_info"]
        # 让球胜平负有效选项来自赛前赔率 >0 的边；缺失时退回 H/D/A 全集
        sides = []
        for side, key in (("H", "home_odds"), ("D", "draw_odds"), ("A", "away_odds")):
            try:
                if float(bi.get(key)) > 0:
                    sides.append(side)
            except (TypeError, ValueError):
                pass
        if not sides:
            sides = ["H", "D", "A"]
        k = rng.randint(1, min(3, len(sides)))
        picks = rng.sample(sides, k=k)
        legs.append({"lota_id": m["lota_id"], "picks": picks,
                     "odds": {s: 2.0 for s in picks}})
    return legs


def main() -> int:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20260827
    rng = random.Random(seed)

    matches = load_drawn_matches()
    if not matches:
        print("❌ 没有已开奖北单数据")
        return 1

    checked, mismatches = verify_goal_line_consistency(matches)
    print(f"① goal-line 一致性: 已开奖 {checked} 场，官方 result 与 score+goal_line 推导"
          f"{'一致' if not mismatches else '存在不一致'}")
    if mismatches:
        for x in mismatches[:10]:
            print("   不一致:", x)
        return 1

    beidan_map_all = {m["lota_id"]: m["beidan_info"] for m in matches}
    fails = 0
    total_combos = 0
    for _ in range(trials):
        legs = random_parlay(matches, rng)

        combos_module = expand_leg_picks(legs)
        combos_ref = ref_expand(legs)
        if len(combos_module) != len(combos_ref):
            fails += 1
            print(f"❌ 展开数量不一致: module={len(combos_module)} ref={len(combos_ref)}")
            continue

        total_bet = round(rng.uniform(10, 500), 2)
        sub_bet = round(total_bet / len(combos_module), 2) if combos_module else 0.0

        # 逐注比对
        for cm, cr in zip(combos_module, combos_ref):
            got = settle_parlay_combo(cm, beidan_map_all, sub_bet)
            want = ref_settle_combo(cr, beidan_map_all, sub_bet)
            total_combos += 1
            if not got.get("ready"):
                fails += 1
                print("❌ module 未就绪:", got)
                continue
            if got["hit"] != want["hit"] or got["return_amount"] != want["return_amount"]:
                fails += 1
                print("❌ 结算不一致:")
                print("   module:", got)
                print("   ref   :", want)

        # 汇总口径比对
        agg = settle_multi_pick_parlay(legs, beidan_map_all, total_bet)
        ref_return = round(sum(
            ref_settle_combo(c, beidan_map_all, sub_bet)["return_amount"]
            for c in combos_ref
        ), 2)
        if agg["total_return"] != ref_return:
            fails += 1
            print(f"❌ 汇总派奖不一致: module={agg['total_return']} ref={ref_return}")

    print(f"② 随机串关: {trials} 组 / {total_combos} 注 | "
          f"BEIDAN_RETURN_RATE={BEIDAN_RETURN_RATE} | "
          f"{'✅ 全部通过' if fails == 0 else f'❌ {fails} 处失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
