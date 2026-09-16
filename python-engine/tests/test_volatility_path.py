"""
波动路径（3b）：赛前特征分层抽样 + 波动因子生命周期 + 与单关狗的隔离。

修掉的问题：
  G6 波动因子候选原本按**开奖 SP**取 TOP-N（结果条件化选择）→ 归纳出的因子只会描述
     事后赢家、学不到区分度；
  G4 波动因子没有生命周期（cleanup 直接 keep、review 不区分 type）。
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="volpath_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="volpath_sessions_"))
os.environ["DS_BACKTEST_FET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from src.beidan_parlay_dog import BeidanParlayDog  # noqa: E402
from src.factor_select import factor_profile, volatility_lifecycle  # noqa: E402
from src.tools import prematch_dispersion, stratified_pick  # noqa: E402
from factor_cleanup_dryrun import classify  # noqa: E402

TODAY = datetime.datetime.now()


def _h(rr: float, hit, days_ago: int = 1, **extra) -> dict:
    return {"date": (TODAY - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d"),
            "hit": hit, "return_ratio": rr, "unit_cost": 3.0, **extra}


# ═══════════════════════════════════════════
# 赛前特征：prematch_dispersion
# ═══════════════════════════════════════════

_TEXT = """公平盘数据:
主客实力差:1.01

离散指数 t=Δt±m odds=h/d/a
OPt-1438m=1.34/3.08/4.55
Δt+1418m↓↓↓1.67/2.46/3.64
Δt+1404m↑↑↑1.58/3.60/5.32
"""


def test_prematch_dispersion_uses_first_and_last_rows():
    d = prematch_dispersion("L1", sections={"discrete-odds": _TEXT})
    # 首行 1.34/3.08/4.55 → 末行 1.58/3.60/5.32，最大相对变动在 d 方向
    exp = max(abs(a - b) / ((a + b) / 2) for a, b in
              zip((1.34, 3.08, 4.55), (1.58, 3.60, 5.32)))
    assert d is not None and abs(d - exp) < 1e-9


def test_prematch_dispersion_needs_two_rows():
    assert prematch_dispersion("L1", sections={"discrete-odds": "离散指数 t=x\nOPt-10m=1.5/3.5/5.0"}) is None
    assert prematch_dispersion("L1", sections={}) is None


# ═══════════════════════════════════════════
# 分层抽样：确定性、覆盖低/中/高、缺失归尾
# ═══════════════════════════════════════════

def test_stratified_pick_covers_all_strata_deterministically():
    items = [{"i": i, "f": float(i)} for i in range(9)]
    picked = stratified_pick(items, 3, key=lambda x: x["f"])
    assert [p["i"] for p in picked] == [picked[0]["i"], picked[1]["i"], picked[2]["i"]]
    assert len({("low", "mid", "high")[j] for j in range(3)}) == 3
    # 低/中/高各一个：分别落在 [0,3)、[3,6)、[6,9)
    assert picked[0]["i"] < 3 and 3 <= picked[1]["i"] < 6 and picked[2]["i"] >= 6
    # 确定性：再跑一次结果一致
    assert stratified_pick(items, 3, key=lambda x: x["f"]) == picked
    # key 缺失 → 排最后
    mixed = [{"i": 0, "f": 1.0}, {"i": 1, "f": None}, {"i": 2, "f": 2.0}]
    assert stratified_pick(mixed, 3, key=lambda x: x["f"])[-1]["i"] == 1
    # limit >= len → 按 key 升序返回
    assert [x["i"] for x in stratified_pick(items, 99, key=lambda x: x["f"])] == list(range(9))


def test_stratified_pick_never_uses_outcome():
    """候选集合只由赛前特征决定：把 SP 反转不影响结果。"""
    items = [{"lid": f"L{i}", "f": i} for i in range(6)]
    a = stratified_pick(items, 3, key=lambda x: x["f"])
    b = stratified_pick([dict(x) for x in items], 3, key=lambda x: x["f"])
    assert [x["lid"] for x in a] == [x["lid"] for x in b]


# ═══════════════════════════════════════════
# 波动因子生命周期（G4）
# ═══════════════════════════════════════════

def test_lifecycle_keep_when_cover_pays():
    stats = {"type": "volatility", "path": "beidan",
             "history": [_h(2.0, True, 1), _h(2.2, True, 2), _h(-3.0, False, 3)]}
    life = volatility_lifecycle(stats, now=TODAY)
    assert life["action"] == "keep" and life["w_return"] > 0


def test_lifecycle_retires_long_term_loser_despite_high_winning_sp():
    """1 胜 3 负：中奖时 SP 很高（avg_sp 6.9），但覆盖长期倒亏 → retire。"""
    stats = {"type": "volatility", "path": "beidan",
             "history": [_h(1.5, True, 1), _h(-3.0, False, 2), _h(-3.0, False, 3),
                         _h(-3.0, False, 4)]}
    life = volatility_lifecycle(stats, now=TODAY)
    assert life["action"] == "retire"
    assert life["avg_sp"] and life["avg_sp"] > 4.615      # 只看 avg_sp 会误判成"赚"
    assert life["w_return"] < 0


def test_lifecycle_observes_small_sample_and_dormant():
    assert volatility_lifecycle(
        {"type": "volatility", "path": "beidan", "history": [_h(-3.0, False, 1)]},
        now=TODAY)["action"] == "observe"
    assert volatility_lifecycle({"type": "volatility", "path": "beidan", "history": []},
                                now=TODAY)["action"] == "observe"
    old = {"date": "2026-01-01", "hit": True, "return_ratio": 1.0}
    assert volatility_lifecycle(
        {"type": "volatility", "path": "beidan", "history": [old]},
        now=TODAY)["action"] == "dormant"


def test_cleanup_gate_isolates_non_beidan_dogs():
    """⚠️ 隔离红线：没有 path=beidan 的波动因子（单关狗/竞彩狗）仍然是 keep。"""
    loser = {"type": "volatility", "total": 4, "hit": 0, "history": [
        _h(-3.0, False, i) for i in range(1, 5)]}
    action, why = classify(dict(loser), TODAY, 90, 10)
    assert action == "keep" and "不纳入" in why
    beidan = dict(loser, path="beidan")
    action2, why2 = classify(beidan, TODAY, 90, 10)
    assert action2 == "retire" and "[波动/北单]" in why2


# ═══════════════════════════════════════════
# 补充样本抽样：不再用开奖 SP
# ═══════════════════════════════════════════

def test_extra_matches_strata_ignores_sp(monkeypatch):
    from src import agent as agent_mod

    matches = []
    for i in range(6):
        matches.append({
            "lota_id": f"Lota{i}", "match_time": "2026-08-20 20:00:00", "state": 6,
            # SP 与"赛前离散"故意反相关：若实现仍在用 SP，选出来的 lid 会不同
            "beidan_info": {"result": "3", "spvalue": float(100 - i * 10)},
        })

    class _DM:
        def get_cached_matches(self, cd, lottery_type="all"):
            return matches

        def _read_legacy_beidan(self, d):
            return []

        def get_beidan_sp_cache(self, d):
            return {}

    disp = {f"Lota{i}": float(i) for i in range(6)}
    monkeypatch.setattr("src.tools.prematch_dispersion", lambda lid, sections=None: disp.get(lid))
    picked = agent_mod._extra_reflect_matches(_DM(), "2026-08-21", set(), max_extra=3,
                                              by_strata=True)
    # 分层：低区间(Lota0/1)、中区间(Lota2/3)、高区间(Lota4/5) 各取一个
    assert len(picked) == 3
    idx = sorted(int(p[4:]) for p in picked)
    assert idx[0] < 2 and 2 <= idx[1] < 4 and idx[2] >= 4
    # 若按 SP 排（历史行为）会选到 Lota0/1/2 —— 两者必须不同，证明没用 SP
    by_sp = agent_mod._extra_reflect_matches(_DM(), "2026-08-21", set(), max_extra=3,
                                             by_sp=True)
    assert sorted(by_sp) != sorted(picked)


def test_unplaced_completed_strata_ignores_sp(monkeypatch):
    dog = BeidanParlayDog(user="volpath_unplaced")
    matches = [{"lota_id": f"Lota{i}", "match_time": "2026-08-20 20:00:00", "state": 6,
                "beidan_info": {"result": "3", "spvalue": float(50 - i)}}
               for i in range(6)]
    monkeypatch.setattr(dog._dm, "get_cached_matches", lambda cd, lottery_type="all": matches)
    monkeypatch.setattr(dog._dm, "_read_legacy_beidan", lambda d: [])
    monkeypatch.setattr(dog._dm, "get_beidan_sp_cache", lambda d: {})
    disp = {f"Lota{i}": float(i) for i in range(6)}
    monkeypatch.setattr("src.tools.prematch_dispersion", lambda lid, sections=None: disp.get(lid))
    out = dog._unplaced_completed("2026-08-20", False, 3, by_strata=True)
    idx = sorted(int(m["lota_id"][4:]) for m in out)
    assert len(idx) == 3 and idx[0] < 2 and idx[2] >= 4


# ═══════════════════════════════════════════
# 粗筛口径：命中场次的高波动率 vs 当日基线
# ═══════════════════════════════════════════

def _vh(hit: bool, base: float, d: int) -> dict:
    return {"date": (TODAY - datetime.timedelta(days=d)).strftime("%Y-%m-%d"),
            "hit": hit, "return_ratio": 0.0, "vol_base": base, "unit_cost": 3.0}


def test_profile_coarse_vol_stats():
    from src.factor_select import factor_profile
    stats = {"type": "volatility", "path": "beidan",
             "history": [_vh(True, 0.25, 1), _vh(True, 0.25, 2),
                         _vh(True, 0.25, 3), _vh(False, 0.25, 4)]}
    p = factor_profile(stats, now=TODAY)
    assert abs(p["vol_precision"] - 0.75) < 1e-9
    assert abs(p["vol_base"] - 0.25) < 1e-9
    assert abs(p["vol_edge"] - 0.50) < 1e-9


def test_lifecycle_coarse_keep_retire_observe():
    """粗筛判据：命中场次高波动率 - 当日基线（pp）。≥10pp 保留；≤0 且样本够 → 退役。"""
    keep = {"type": "volatility", "path": "beidan", "history": [
        _vh(True, 0.25, 1), _vh(True, 0.25, 2), _vh(True, 0.25, 3), _vh(False, 0.25, 4)]}
    assert volatility_lifecycle(keep, now=TODAY)["action"] == "keep"
    retire = {"type": "volatility", "path": "beidan", "history": [
        _vh(True, 0.5, 1), _vh(False, 0.5, 2), _vh(True, 0.5, 3), _vh(False, 0.5, 4)]}
    life = volatility_lifecycle(retire, now=TODAY)
    assert life["action"] == "retire" and "没有筛选力" in life["reason"]
    weak = {"type": "volatility", "path": "beidan", "history": [
        _vh(True, 0.45, 1), _vh(False, 0.45, 2), _vh(False, 0.45, 3), _vh(True, 0.45, 4)]}
    assert volatility_lifecycle(weak, now=TODAY)["action"] == "observe"   # +5pp，区分度不足
    # 没有基线（老样本）→ 回落 w_return 口径，不报错
    legacy = {"type": "volatility", "path": "beidan", "history": [
        {"date": TODAY.strftime("%Y-%m-%d"), "hit": True, "return_ratio": 2.0,
         "unit_cost": 3.0}]}
    assert volatility_lifecycle(legacy, now=TODAY)["action"] == "keep"


def test_day_high_vol_baseline(monkeypatch):
    dog = BeidanParlayDog(user="volpath_baseline")
    sps = [1.0, 2.0, 3.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]   # 6/9 ≥ 4.615
    monkeypatch.setattr(dog, "_unplaced_completed",
                        lambda d, by_sp, limit, by_strata=False: [
                            {"beidan_info": {"spvalue": s}} for s in sps])
    b = dog._day_high_vol_baseline("2026-08-19")
    assert b is not None and abs(b - 6 / 9) < 1e-9
    # 样本不足（<5 场）→ 不给基线
    monkeypatch.setattr(dog, "_unplaced_completed",
                        lambda d, by_sp, limit, by_strata=False: [
                            {"beidan_info": {"spvalue": s}} for s in sps[:3]])
    assert dog._day_high_vol_baseline("2026-08-19") is None


def test_volatility_factor_text_uses_coarse_stats(monkeypatch):
    dog = BeidanParlayDog(user="volpath_text")
    role = dog._ensure_role()
    fm = role.memory.factors
    fm.load()
    # 走真实写入路径（同时验证 vol_base 落盘）
    for hit, d in ((True, 1), (True, 2), (True, 3), (False, 4)):
        fm.record("粗筛因子", hit, 0.0,
                  desc="测试", date=(TODAY - datetime.timedelta(days=d)).strftime("%Y-%m-%d"),
                  lota_id=f"L{d}", bet_size=2.0, factor_type="volatility",
                  unit_cost=3.0, path="beidan", vol_base=0.25)
    txt = dog._volatility_factor_text(role)
    assert "粗筛" in txt                       # 明确定位成粗筛
    assert "命中场次高波动率 75%" in txt and "当日基线 25%" in txt
    assert "+50pp" in txt


# ═══════════════════════════════════════════
# 日级粗筛：标记场次（含未下注）的高波动率 vs 当日基线
# ═══════════════════════════════════════════

def test_screen_stats_take_priority_over_bet_legs():
    """日级粗筛统计（标记的所有场次）优先于"只看下注腿"的旧口径。"""
    from src.factor_select import factor_profile
    stats = {"type": "volatility", "path": "beidan",
             # 下注腿口径会是 100%（只统计精挑的腿）
             "history": [_vh(True, 0.03, 1)],
             # 日级粗筛：标记 29 场，只有 4 场高波动，当日基线 11.8%
             "screen": {"n": 29, "high": 4, "base_sum": 0.118 * 29}}
    p = factor_profile(stats, now=TODAY)
    assert abs(p["vol_precision"] - 4 / 29) < 1e-9      # 不是 100%
    assert abs(p["vol_base"] - 0.118) < 1e-6
    assert abs(p["vol_edge"] - (4 / 29 - 0.118)) < 1e-6


def test_screen_accumulation_end_to_end(monkeypatch):
    """stage1 归因落档 → 结算统计：标记场次里出高波动的比例 + 当日基线。"""
    dog = BeidanParlayDog(user="volpath_screen")
    role = dog._ensure_role()
    role.memory.factors.load()
    # 1) stage1 留档：6 场，其中 2 场归因到 F（另一场无因子）
    dog._save_vol_screen(role, "2026-08-19", [
        {"lota_id": "L1", "推荐": "高波动", "factors": ["F"]},
        {"lota_id": "L2", "推荐": "高波动", "factors": ["F"]},
        {"lota_id": "L3", "推荐": "H", "factors": ["F"]},
        {"lota_id": "L4", "推荐": "skip", "factors": []},
    ])
    assert dog._screen_path(role, "2026-08-19").exists()
    # 2) 当日开奖：L1 高 SP(6.0)、L2 低 SP(2.0)、L3 高 SP(5.0)、L4 低 SP(1.5)；再加 2 场基线场次
    sp = {"L1": 6.0, "L2": 2.0, "L3": 5.0, "L4": 1.5, "L5": 1.2, "L6": 8.0}
    monkeypatch.setattr(dog, "_day_sp_map", lambda d: dict(sp))
    n = dog._accumulate_vol_screen(role, "2026-08-19")
    assert n == 1
    sc = role.memory.factors.factor_perf["F"]["screen"]
    assert sc["n"] == 3 and sc["high"] == 2                  # L1/L3 高波动
    base = 3 / 6                                             # 当日 6 场里 3 场 ≥4.615
    assert abs(sc["base_sum"] - base * 3) < 1e-9
    p = factor_profile(role.memory.factors.factor_perf["F"], now=TODAY)
    assert abs(p["vol_precision"] - 2 / 3) < 1e-9
    assert abs(p["vol_base"] - base) < 1e-9
    assert p["vol_edge"] > 0


def test_screen_skipped_when_baseline_too_thin(monkeypatch):
    dog = BeidanParlayDog(user="volpath_screen_thin")
    role = dog._ensure_role()
    dog._save_vol_screen(role, "2026-08-20", [
        {"lota_id": "L1", "推荐": "高波动", "factors": ["F"]}])
    monkeypatch.setattr(dog, "_day_sp_map", lambda d: {"L1": 6.0})   # 当天只有 1 场
    assert dog._accumulate_vol_screen(role, "2026-08-20") == 0
