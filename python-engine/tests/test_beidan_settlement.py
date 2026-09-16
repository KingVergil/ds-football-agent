import os
import sys
import tempfile
from pathlib import Path

# 在导入 src 之前覆盖角色/session 根目录，避免污染线上 data/roles、data/sessions。
os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="beidan_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="beidan_sessions_"))
# 本文件验证线上取数路径（本地缓存）；显式关闭回测切片源，
# 让结果不依赖当前机器上是否已经同步了 fet_txt 切片（见 tests/test_backtest_fet.py）。
os.environ["DS_BACKTEST_FET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beidan_settlement import (
    BEIDAN_RETURN_RATE,
    VOID_SP,
    expand_leg_picks,
    handicap_result,
    result_code_to_pick,
    settle_leg,
    settle_multi_pick_parlay,
    settle_parlay_combo,
)
from src.beidan_parlay_dog import BeidanParlayDog
from src.beidan_parlay_dog import compact_slip_orders


# ═══════════════════════════════════════════
# 纯函数：结果 code / goal-line
# ═══════════════════════════════════════════

def test_result_code_mapping():
    assert result_code_to_pick("3") == "H"
    assert result_code_to_pick("1") == "D"
    assert result_code_to_pick("0") == "A"
    assert result_code_to_pick("*") is None
    assert result_code_to_pick("") is None


def test_handicap_result_goal_line():
    assert handicap_result("1:0", "0") == "H"
    assert handicap_result("1:0", "-1") == "D"
    assert handicap_result("1:0", "-2") == "A"
    assert handicap_result("0:3", "0") == "A"
    assert handicap_result("1:1", "1") == "H"
    assert handicap_result("2:2", "-1") == "A"


# ═══════════════════════════════════════════
# 纯函数：单腿结算
# ═══════════════════════════════════════════

def test_settle_leg_hit_and_miss():
    info = {"result": "0", "spvalue": 2.155, "score": "0:3", "goal_line": "0"}
    hit = settle_leg("A", info)
    assert hit["ready"] and hit["hit"] and not hit["push"]
    assert hit["actual"] == "A"
    assert abs(hit["sp"] - 2.155) < 1e-9
    assert hit["expected"] == "A" and not hit["mismatch"]

    miss = settle_leg("H", info)
    assert miss["ready"] and not miss["hit"]


def test_settle_leg_void():
    info = {"result": "*", "spvalue": 1.0, "score": "-:-", "goal_line": "0"}
    r = settle_leg("H", info)
    assert r["ready"] and r["hit"] and r["push"]
    assert r["sp"] == VOID_SP
    assert r["actual"] is None


def test_settle_leg_not_ready():
    assert settle_leg("H", {"home_odds": 2.0})["ready"] is False


# ═══════════════════════════════════════════
# 纯函数：过关结算（含 65% 返奖）
# ═══════════════════════════════════════════

def test_parlay_hit_uses_sp_and_65pct():
    combo = [{"lota_id": "L1", "pick": "H"}, {"lota_id": "L2", "pick": "A"}]
    bmap = {
        "L1": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"},
        "L2": {"result": "0", "spvalue": 3.0, "score": "0:1", "goal_line": "0"},
    }
    r = settle_parlay_combo(combo, bmap, 100.0)
    expected = round(100.0 * 2.0 * 3.0 * BEIDAN_RETURN_RATE, 2)
    assert r["ready"] and r["hit"]
    assert r["return_amount"] == expected
    assert r["profit"] == round(expected - 100.0, 2)
    assert r["sp_product"] == 6.0


def test_parlay_miss_returns_zero():
    combo = [{"lota_id": "L1", "pick": "D"}, {"lota_id": "L2", "pick": "A"}]
    bmap = {
        "L1": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"},
        "L2": {"result": "0", "spvalue": 3.0, "score": "0:1", "goal_line": "0"},
    }
    r = settle_parlay_combo(combo, bmap, 100.0)
    assert r["ready"] and not r["hit"]
    assert r["return_amount"] == 0.0
    assert r["profit"] == -100.0


def test_parlay_all_void_refunds_stake():
    combo = [{"lota_id": "V", "pick": "H"}]
    bmap = {"V": {"result": "*", "spvalue": 1.0, "score": "-:-", "goal_line": "0"}}
    r = settle_parlay_combo(combo, bmap, 100.0)
    assert r["ready"] and r["hit"] and r["all_void"]
    assert r["return_amount"] == 100.0 and r["profit"] == 0.0


def test_parlay_not_ready_when_result_missing():
    r = settle_parlay_combo([{"lota_id": "L1", "pick": "H"}],
                            {"L1": {"home_odds": 2.0}}, 100.0)
    assert r["ready"] is False


# ═══════════════════════════════════════════
# 纯函数：多选展开 + 多选过关
# ═══════════════════════════════════════════

def test_expand_leg_picks():
    legs = [
        {"lota_id": "L1", "picks": ["H", "D"], "odds": {"H": 2.0, "D": 3.0}},
        {"lota_id": "L2", "picks": ["A"], "odds": {"A": 2.5}},
    ]
    combos = expand_leg_picks(legs)
    assert len(combos) == 2
    assert {c[0]["pick"] for c in combos} == {"H", "D"}
    assert all(c[1]["pick"] == "A" for c in combos)


def test_three_picks_expand():
    legs = [{"lota_id": "L1", "picks": ["H", "D", "A"],
             "odds": {"H": 2.0, "D": 3.0, "A": 4.0}}]
    combos = expand_leg_picks(legs)
    assert len(combos) == 3
    assert {c[0]["pick"] for c in combos} == {"H", "D", "A"}


def test_multi_pick_parlay():
    legs = [
        {"lota_id": "L1", "picks": ["H", "D"], "odds": {"H": 2.0, "D": 3.0}},
        {"lota_id": "L2", "picks": ["A"], "odds": {"A": 2.5}},
    ]
    bmap = {
        "L1": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"},
        "L2": {"result": "0", "spvalue": 2.5, "score": "0:1", "goal_line": "0"},
    }
    r = settle_multi_pick_parlay(legs, bmap, 100.0)
    assert r["combos_count"] == 2 and r["hit_count"] == 1
    # 命中一注: 50 * 2.0 * 2.5 * 0.65 = 162.5
    assert r["total_return"] == 162.5


# ═══════════════════════════════════════════
# 集成：真实北单缓存 + 订单落盘 + 结算
# ═══════════════════════════════════════════

def test_end_to_end_analyze_settle_uses_sp_and_65pct():
    dog = BeidanParlayDog(user="pytest_beidan_e2e")
    dog.reset()
    a = dog.analyze("2026-08-20", tickets=["2串1"], max_picks=2)
    assert a["placed"] > 0
    s = dog.settle("2026-08-20")
    assert s["settled"] == a["placed"]

    role = dog._ensure_role()
    for o in role.get_orders():
        if not o.get("settled_at"):
            continue
        assert o["settlement_rate"] == BEIDAN_RETURN_RATE
        sp_product = 1.0
        for leg in o["legs"]:
            sp_product *= float(leg["sp"])
        assert abs(float(o["sp_product"]) - sp_product) < 1e-6
        if o["hit"] and not o["all_void"]:
            expected = round(float(o["bet_size"]) * sp_product * BEIDAN_RETURN_RATE, 2)
            assert float(o["return_amount"]) == expected
        else:
            assert float(o["return_amount"]) == (float(o["bet_size"]) if o["all_void"] else 0.0)


def test_goal_line_consistent_with_official_result():
    """抽样证明官方 result 与 score+goal_line 推导一致（结算依据可靠）。"""
    dog = BeidanParlayDog(user="pytest_beidan_goal")
    matches, _ = dog._beidan_matches("2026-08-20")
    checked = 0
    for m in matches:
        bi = m.get("beidan_info") or {}
        r = str(bi.get("result") or "").strip()
        if r in ("", "*"):
            continue
        actual = result_code_to_pick(r)
        expected = handicap_result(bi.get("score"), bi.get("goal_line"))
        assert actual == expected
        checked += 1
    assert checked > 0


# ═══════════════════════════════════════════
# bc狗：一张票 = 一条 slip 级订单（不再按 combo 膨胀落盘）
# ═══════════════════════════════════════════

def _mk_match(lid: str, home: str, away: str) -> dict:
    return {
        "lota_id": lid,
        "home_name": home,
        "away_name": away,
        "league_name": "测试联赛",
        "match_time": "2026-08-20 20:00:00",
        "beidan_number": lid[-1],
        "beidan_info": {"goal_line": 0, "home_odds": 2.0, "draw_odds": 3.0,
                        "away_odds": 4.0},
    }


def test_analyze_writes_slip_level_order():
    """analyze 落盘应为「一张票一条 slip 级 order」，组合不再逐注展开。"""
    dog = BeidanParlayDog(user="pytest_beidan_slip")
    dog.reset()
    _legacy(dog)
    matches = [_mk_match("L1", "A", "B"), _mk_match("L2", "C", "D")]
    dog._beidan_matches = lambda day, live=False, **kw: (matches, [])

    a = dog.analyze("2026-08-20", tickets=["2串1"], max_picks=2, live=False)

    # 规则回退/手工模式的语义（工作区 2026-09 起）：没有 LLM 给方向时，
    # `_beidan_score_leg` 一律退化为「覆盖全部可投注边」→ 2 腿 × 3 选 = 9 注，
    # 但组合只应产生 1 条 slip 级订单。
    assert a["placed"] == 1
    assert len(a["orders"]) == 1
    o = a["orders"][0]
    assert o["slip_type"] == "2串1"
    assert o["combos_count"] == 9
    assert o["total_stake"] == 18.0
    assert o["bet_size"] == 2.0
    assert len(o["legs"]) == 2
    # 腿保留 picks 列表（不落盘展开后的单选 combo）
    assert all(l.get("picks") == ["H", "D", "A"] for l in o["legs"])

    # 角色 JSON 里也只有这一条（而非 9 条）
    role = dog._ensure_role()
    assert len(role.get_orders()) == 1


def test_settle_slip_level_uses_sp_and_65pct():
    """settle 在 slip 级订单上按现算组合结算：命中注返回 unit×SP连乘×65%。"""
    dog = BeidanParlayDog(user="pytest_beidan_settle_slip")
    dog.reset()
    role = dog._ensure_role()
    legs = [
        {"lota_id": "L1", "picks": ["H", "D"], "odds": {"H": 2.0, "D": 3.0},
         "goal_line": 0.0, "home_name": "A", "away_name": "B"},
        {"lota_id": "L2", "picks": ["A"], "odds": {"A": 2.5},
         "goal_line": 0.0, "home_name": "C", "away_name": "D"},
    ]
    order = {
        "id": "o1", "slip_id": "s1", "slip_type": "2串1", "slip_index": 1,
        "combos_count": 2, "unit_stake": 2.0, "total_stake": 4.0,
        "ticket_legs": list(legs), "predict_id": "", "lota_id": "L1",
        "bet_type": dog.BET_TYPE, "ticket_type": "2串1", "pick": "x",
        "odds": 6.0, "bet_size": 2.0, "legs": list(legs),
        "created_at": "2026-08-20 12:00:00", "settled_at": None,
    }
    role.orders.append(order)
    role.save()

    beidan_map = {
        "L1": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"},
        "L2": {"result": "0", "spvalue": 3.0, "score": "0:1", "goal_line": "0"},
    }
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: beidan_map

    s = dog.settle("2026-08-20", reflect=False)

    assert s["settled"] == 1
    assert s["hit"] == 1
    assert s["pnl"] == 3.8
    o = role.get_orders()[0]
    assert o["return_amount"] == 7.8
    assert o["profit"] == 3.8
    assert o["hit"] is True
    assert o["sp_product"] == 6.0
    assert o["settlement_rate"] == BEIDAN_RETURN_RATE
    assert [l["sp"] for l in o["legs"]] == [2.0, 3.0]


def test_compact_slip_orders_reduces_to_one_slip():
    """旧式逐注 orders → 每 slip 压缩成一条 slip 级 order，并汇总结算。"""
    legs = [
        {"lota_id": "L1", "picks": ["H", "D"], "odds": {"H": 2.0, "D": 3.0},
         "goal_line": 0.0, "home_name": "A", "away_name": "B"},
        {"lota_id": "L2", "picks": ["A"], "odds": {"A": 2.5},
         "goal_line": 0.0, "home_name": "C", "away_name": "D"},
    ]
    old = []
    for i, (p1, p2) in enumerate([("H", "A"), ("D", "A")]):
        combo = [dict(legs[0], pick=p1, sp=2.0, actual="H"),
                 dict(legs[1], pick=p2, sp=3.0, actual="A")]
        win = (i == 0)
        old.append({
            "id": f"ord_{i}", "slip_id": "s1", "slip_type": "2串1",
            "slip_index": i + 1, "combos_count": 2,
            "ticket_legs": list(legs), "bet_type": "北单串关",
            "ticket_type": "2串1", "pick": f"{p1}+{p2}", "odds": 6.0,
            "bet_size": 2.0, "legs": combo, "lota_id": "L1",
            "created_at": "2026-08-20 12:00:00",
            "settled_at": "2026-08-20 23:00:00",
            "hit": win, "all_void": False,
            "return_amount": (2.0 * 2.0 * 3.0 * 0.65) if win else 0.0,
            "profit": (2.0 * 2.0 * 3.0 * 0.65 - 2.0) if win else -2.0,
            "sp_product": 6.0, "settlement_rate": 0.65,
        })

    compacted = compact_slip_orders(old)
    assert len(compacted) == 1
    o = compacted[0]
    assert o["slip_id"] == "s1"
    assert o["combos_count"] == 2
    assert o["total_stake"] == 4.0
    assert len(o["legs"]) == 2
    assert o["hit"] is True
    # 腿级结果回填到 ticket_legs
    assert {l["lota_id"]: l["sp"] for l in o["legs"]} == {"L1": 2.0, "L2": 3.0}


if __name__ == "__main__":
    # 不依赖 pytest 的简易 runner（当前环境 pytest 会段错误）。
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"✅ {fn.__name__}")
            passed += 1
        except Exception:
            failed += 1
            print(f"❌ {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


# ═══════════════════════════════════════════
# 串关结算：一票否决（一关确定不中 → 不算组合）+ 只算活组合
# ═══════════════════════════════════════════

def _legacy(dog) -> None:
    """显式锁定 legacy 模式：不依赖共享 DS_ROLES_ROOT 里有没有 parlay.json。"""
    dog._parlay_cfg = {**BeidanParlayDog.FLEX_DEFAULTS, "mode": "legacy",
                       "ticket": "8串1", "single_legs": 3, "cover_legs": 5,
                       "cover_mode": "all", "cover_picks": 3}


def _mk_order(legs: list[dict], combos: int, unit: float = 2.0,
              tk: str = None) -> dict:
    tk = tk or f"{len(legs)}串1"
    return {
        "id": "o1", "slip_id": "s1", "slip_type": tk, "slip_index": 1,
        "combos_count": combos, "unit_stake": unit,
        "total_stake": round(unit * combos, 2),
        "ticket_legs": list(legs), "predict_id": "", "lota_id": legs[0]["lota_id"],
        "bet_type": "北单串关", "ticket_type": tk, "pick": "x",
        "odds": 0.0, "bet_size": unit, "legs": list(legs),
        "created_at": "2026-08-20 12:00:00", "settled_at": None,
    }


def _leg(lid: str, picks: list[str]) -> dict:
    return {"lota_id": lid, "picks": picks, "odds": {p: 2.0 for p in picks},
            "goal_line": 0.0, "home_name": f"H{lid}", "away_name": f"A{lid}"}


def test_settle_one_leg_wrong_kills_ticket_without_double_pick_math():
    """一关确定不中 → 立刻按不中结算（不等其它腿、不算组合）。"""
    dog = BeidanParlayDog(user="pytest_early_kill")
    dog.reset()
    role = dog._ensure_role()
    legs = [_leg("L1", ["A"]), _leg("L2", ["H"]), _leg("L3", ["D"])]
    role.orders.append(_mk_order(legs, combos=1))
    role.save()
    # L1 已开奖且买 A 实际 H（必杀）；L2/L3 还没有结果（info 缺失）
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: {
        "L1": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"}}
    s = dog.settle("2026-08-20", reflect=False)
    assert s["settled"] == 1 and s["miss"] == 1 and s["hit"] == 0
    o = role.get_orders()[0]
    assert o["hit"] is False and o["return_amount"] == 0.0
    assert o["profit"] == -o["total_stake"]
    assert o["early_kill"]["lota_id"] == "L1" and o["early_kill"]["actual"] == "H"
    # 未开奖的腿不伪造结果
    assert [l.get("actual") for l in o["legs"]] == ["H", None, None]


def test_settle_all_picks_of_a_leg_missing_kills_ticket():
    """多选腿全不中同样是必杀（双选买 H/D 实际 A）。"""
    dog = BeidanParlayDog(user="pytest_early_kill_cover")
    dog.reset()
    role = dog._ensure_role()
    legs = [_leg("L1", ["H", "D"]), _leg("L2", ["H"])]
    role.orders.append(_mk_order(legs, combos=2))
    role.save()
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: {
        "L1": {"result": "0", "spvalue": 5.0, "score": "0:1", "goal_line": "0"},
        "L2": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"}}
    s = dog.settle("2026-08-20", reflect=False)
    assert s["settled"] == 1 and s["miss"] == 1
    assert role.get_orders()[0]["early_kill"]["lota_id"] == "L1"


def test_settle_only_surviving_combos_are_counted():
    """双选腿只算命中的那一注：L1 买 H/D 实际 H、L2 买 H/A 实际 H → 只派 1 注。"""
    dog = BeidanParlayDog(user="pytest_survivors")
    dog.reset()
    role = dog._ensure_role()
    legs = [_leg("L1", ["H", "D"]), _leg("L2", ["H", "A"])]
    role.orders.append(_mk_order(legs, combos=4, unit=2.0))   # 成本 4 注 = 8 元
    role.save()
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: {
        "L1": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"},
        "L2": {"result": "3", "spvalue": 3.0, "score": "2:0", "goal_line": "0"}}
    s = dog.settle("2026-08-20", reflect=False)
    assert s["settled"] == 1 and s["hit"] == 1
    o = role.get_orders()[0]
    # 只有 1 注活下来：2 元 × 2.0 × 3.0 × 0.65 = 7.8（另 3 注派彩 0）
    assert o["combos_count"] == 4
    assert o["return_amount"] == 7.8
    assert o["profit"] == round(7.8 - 8.0, 2)
    assert o["sp_product"] == 6.0


def test_settle_single_pick_multi_leg_still_works():
    """回归：全单选多腿票（无必杀、无多选）结算结果不变。"""
    dog = BeidanParlayDog(user="pytest_single_ok")
    dog.reset()
    role = dog._ensure_role()
    legs = [_leg("L1", ["H"]), _leg("L2", ["A"])]
    role.orders.append(_mk_order(legs, combos=1))
    role.save()
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: {
        "L1": {"result": "3", "spvalue": 2.0, "score": "1:0", "goal_line": "0"},
        "L2": {"result": "0", "spvalue": 3.0, "score": "0:2", "goal_line": "0"}}
    s = dog.settle("2026-08-20", reflect=False)
    assert s["settled"] == 1 and s["hit"] == 1
    assert role.get_orders()[0]["return_amount"] == 7.8


# ═══════════════════════════════════════════
# 容错票（N过M）结算：容错额度内的错腿不算一票否决
# ═══════════════════════════════════════════

def _res(lid: str, result: str, sp: float = 2.0) -> dict:
    return {"result": result, "spvalue": sp, "score": "1:0", "goal_line": "0"}


def test_settle_tolerance_ticket_survives_one_miss():
    """8过7（容错 1）：错 1 条腿**不**一票否决，不含该腿的活组合照常派彩。"""
    dog = BeidanParlayDog(user="pytest_tol_ok")
    dog.reset()
    role = dog._ensure_role()
    legs = [_leg(f"L{i}", ["H"]) for i in range(1, 9)]
    role.orders.append(_mk_order(legs, combos=8, tk="8过7"))   # C(8,7)=8 注 = 16 元
    role.save()
    res = {f"L{i}": _res(f"L{i}", "3") for i in range(1, 9)}
    res["L1"] = _res("L1", "0")          # L1 买 H 实际 A → 唯一错腿（容错内）
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: res
    s = dog.settle("2026-08-20", reflect=False)
    assert s["settled"] == 1 and s["hit"] == 1 and s["miss"] == 0
    o = role.get_orders()[0]
    assert "early_kill" not in o and o["hit"] is True
    # 7 个含 L1 的组合全废，只剩 1 注（L2..L8）活：2 元 × 2^7 × 0.65
    assert o["return_amount"] == 166.4
    assert o["profit"] == round(166.4 - 16.0, 2)
    assert o["legs"][0]["actual"] == "A" and o["legs"][0]["hit"] is False


def test_settle_tolerance_ticket_killed_when_exceeding():
    """8过7 错 2 条腿 > 容错 1 → 一票否决：不等其它腿开奖、不算 C(8,7) 注组合。"""
    dog = BeidanParlayDog(user="pytest_tol_dead")
    dog.reset()
    role = dog._ensure_role()
    legs = [_leg(f"L{i}", ["H"]) for i in range(1, 9)]
    role.orders.append(_mk_order(legs, combos=8, tk="8过7"))
    role.save()
    # 只给 2 条错腿的结果，其余 6 条"没数据" → 仍应立刻判死
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: {
        "L1": _res("L1", "0"), "L2": _res("L2", "0")}
    s = dog.settle("2026-08-20", reflect=False)
    assert s["settled"] == 1 and s["miss"] == 1 and s["hit"] == 0
    o = role.get_orders()[0]
    assert o["hit"] is False and o["return_amount"] == 0.0
    assert o["early_kill"]["lota_id"] == "L1"
    assert [d["lota_id"] for d in o.get("early_kill_legs") or []] == ["L1", "L2"]
    assert o["profit"] == -16.0
    # 未开奖的腿不伪造结果
    assert [l.get("actual") for l in o["legs"][2:]] == [None] * 6


def test_settle_12_over_8_tolerance_four():
    """12过8（12 场单选容错 4）：错 4 条腿仍在容错内 → 8 条活腿的 1 注照常派彩。"""
    dog = BeidanParlayDog(user="pytest_tol_12")
    dog.reset()
    role = dog._ensure_role()
    legs = [_leg(f"L{i:02d}", ["H"]) for i in range(1, 13)]
    role.orders.append(_mk_order(legs, combos=495, tk="12过8"))   # C(12,8)=495 注 = 990 元
    role.save()
    res = {f"L{i:02d}": _res(f"L{i:02d}", "3") for i in range(1, 13)}
    for i in range(1, 5):                # 恰好 4 条错腿（=容错额度）
        res[f"L{i:02d}"] = _res(f"L{i:02d}", "0")
    dog._fetch_beidan_results = lambda day, lids, sp_dates=None, orders=None: res
    s = dog.settle("2026-08-20", reflect=False)
    assert s["settled"] == 1 and s["hit"] == 1
    o = role.get_orders()[0]
    assert "early_kill" not in o
    # 只有 1 注活（L05..L12 全中）：2 元 × 2^8 × 0.65
    assert o["return_amount"] == 332.8
    assert o["profit"] == round(332.8 - 990.0, 2)
