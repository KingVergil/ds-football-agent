"""
方向路径飞轮（3a）：因子归因 + 「声称概率 vs 实际命中」校准。

口径：k_f = (Σhit + a) / (Σp̂ + a)，无样本 k=1（中性）；一条腿取各归因因子 k 的最小值。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="pcal_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="pcal_sessions_"))
os.environ["DS_BACKTEST_FET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beidan_parlay_dog import BeidanParlayDog  # noqa: E402
from src.p_calibration import PCalibration, mode_of_leg  # noqa: E402


# ═══════════════════════════════════════════
# 校准库本身
# ═══════════════════════════════════════════

def test_mode_of_leg():
    assert mode_of_leg(["H"]) == "direction"
    assert mode_of_leg(["H", "D"]) == "cover"
    assert mode_of_leg([]) == "direction"


def test_k_is_neutral_without_samples():
    cal = PCalibration(tempfile.mkdtemp())
    assert cal.k("未知因子") == 1.0
    k_leg, detail = cal.k_for_leg(["A", "B"], "direction")
    assert k_leg == 1.0 and set(detail) == {"A", "B"}
    assert all(v == 1.0 for v in detail.values())
    assert cal.k_for_leg([], "direction") == (1.0, {})


def test_k_shrinks_toward_one_and_detects_overconfidence():
    cal = PCalibration(tempfile.mkdtemp())          # 先验 a=2
    # 一次"声称 80% 实际没中"：k = (0+2)/(0.8+2) = 0.714（被先验拉住，不会归零）
    cal.record_leg(["F"], "direction", {"H": 0.8}, actual="A")
    assert abs(cal.k("F") - 2 / 2.8) < 1e-9
    # 再叠 9 次同样样本：k = 2/(0.8*10+2) = 0.2
    for _ in range(9):
        cal.record_leg(["F"], "direction", {"H": 0.8}, actual="A")
    assert abs(cal.k("F") - 2 / 10.0) < 1e-9
    # 校准良好的因子 → k ≈ 1
    cal2 = PCalibration(tempfile.mkdtemp())
    for i in range(10):
        cal2.record_leg(["G"], "direction", {"H": 0.5}, actual=("H" if i < 5 else "A"))
    assert abs(cal2.k("G") - 1.0) < 1e-9


def test_k_is_mode_aware_and_leg_takes_min():
    cal = PCalibration(tempfile.mkdtemp())
    # 单选路径高估
    for _ in range(6):
        cal.record_leg(["方向因子"], "direction", {"H": 0.9}, actual="A")
    # 多选路径校准良好
    for _ in range(6):
        cal.record_leg(["覆盖因子"], "cover", {"H": 0.5, "D": 0.5}, actual="H")
    k_dir = cal.k("方向因子", "direction")
    k_cov = cal.k("覆盖因子", "cover")
    assert k_dir < 0.4 and abs(k_cov - 1.0) < 0.15
    # 一条腿只按自己的路径取 k；多个因子取最小（最保守）
    k_leg, detail = cal.k_for_leg(["方向因子", "覆盖因子"], "direction")
    assert abs(k_leg - k_dir) < 1e-3 and set(detail) == {"方向因子", "覆盖因子"}


def test_record_leg_skips_void_and_invalid_claims():
    cal = PCalibration(tempfile.mkdtemp())
    assert cal.record_leg(["F"], "direction", {"H": 0.5}, actual=None) == 0   # 走水不计
    assert cal.record_leg(["F"], "direction", {"H": 1.5}, actual="H") == 0   # 非法概率
    assert cal.record_leg([], "direction", {"H": 0.5}, actual="H") == 1      # 无因子 → __no_factor__
    assert cal.data["legs"]["no_factor"] == 1


def test_persistence_and_summary(tmp_path):
    cal = PCalibration(tmp_path)
    cal.record_leg(["F"], "direction", {"H": 0.6}, actual="H")
    cal.save()
    assert cal.path.exists()
    cal2 = PCalibration(tmp_path).load()
    assert abs(cal2.k("F") - 3 / 2.6) < 1e-9
    s = cal2.summary()
    assert s["factors"]["F"]["direction"]["n"] == 1
    assert s["factors"]["F"]["direction"]["claimed"] == 0.6
    assert s["factors"]["F"]["direction"]["realized"] == 1.0


# ═══════════════════════════════════════════
# 引擎接线：腿集里的因子归因 + k 压低
# ═══════════════════════════════════════════

def _mk_match(lid: str, h=2.0, d=3.4, a=4.0, gl=0) -> dict:
    return {"lota_id": lid, "home_name": f"主{lid[-1]}", "away_name": f"客{lid[-1]}",
            "league_name": "T", "match_time": "2026-08-19 23:00:00",
            "beidan_number": lid[-1],
            "beidan_info": {"goal_line": gl, "home_odds": h, "draw_odds": d, "away_odds": a}}


def test_flex_leg_applies_calibration():
    dog = BeidanParlayDog(user="pcal_leg")
    m = _mk_match("Lota970001")
    # k=0.5：声称 0.66 → 0.33；v̂ = 0.33×2.0 = 0.66（无边际 → 会被护栏剔）
    leg = dog._flex_leg(m, ["H"], {"H": 0.66}, k_cal=0.5)
    assert abs(leg["p_hat"]["H"] - 0.33) < 1e-9
    assert abs(leg["p_claim"]["H"] - 0.66) < 1e-9
    assert abs(leg["leg_v"] - 0.66) < 1e-9 and leg["k_cal"] == 0.5
    # k=1（无样本）不改变声称值
    leg2 = dog._flex_leg(m, ["H"], {"H": 0.66}, k_cal=1.0)
    assert abs(leg2["p_hat"]["H"] - 0.66) < 1e-9
    # 校准压低后仍受市场封顶约束
    leg3 = dog._flex_leg(m, ["H"], {"H": 0.9}, k_cal=0.9,
                         p_mkt={"H": 0.4, "D": 0.3, "A": 0.3}, allowance=1.25)
    assert abs(leg3["p_hat"]["H"] - 0.5) < 1e-9      # min(0.81, 0.4×1.25)


class _FakeProvider:
    def __init__(self, legs):
        self.legs = legs

    def call(self, system, messages, **kw):
        if "初筛器" in system:
            return json.dumps({"items": [{"lota_id": l["lota_id"], "推荐": "H",
                                          "factors": []} for l in self.legs]},
                              ensure_ascii=False)
        return json.dumps({"legs": self.legs, "empty": not self.legs}, ensure_ascii=False)


def _flex_cfg(**over):
    cfg = {**BeidanParlayDog.FLEX_DEFAULTS, "mode": "flex", "ticket": "8串1",
           "single_legs": 3, "cover_legs": 5, "cover_mode": "all", "cover_picks": 3}
    cfg.update(over)
    return cfg


def _seed_factor(dog, name: str, *, claimed: float, hits: int, n: int) -> None:
    """在角色目录里造一个因子 + 校准样本（k = (hits+a)/(claimed·n+a)）。"""
    role = dog._ensure_role()
    role.memory.factors.load()
    role.memory.factors.factor_perf.setdefault(name, {"total": 0, "hit": 0, "miss": 0,
                                                      "push": 0, "profit": 0.0,
                                                      "history": [], "aliases": []})
    role.memory.factors._save()
    cal = PCalibration(Path(role._role_dir) / "memory")
    for i in range(n):
        cal.record_leg([name], "direction", {"H": claimed},
                       actual=("H" if i < hits else "A"))
    cal.save()


def test_analyze_uses_calibrated_factor_and_reports(monkeypatch):
    """腿集给 factors → 引擎按该因子 k 压低 p̂；报出的 flex meta 带校准信息。"""
    dog = BeidanParlayDog(user="pcal_e2e", capital=3000.0)
    dog._parlay_cfg = _flex_cfg()
    _seed_factor(dog, "高估因子", claimed=0.9, hits=0, n=6)     # k ≈ 2/7.4 = 0.27
    legs_json = [
        {"lota_id": "Lota980001", "picks": ["H"], "p": {"H": 0.9},
         "factors": ["高估因子"], "why": "x"},
    ]
    dog.set_provider(_FakeProvider(legs_json))
    dog._beidan_matches = lambda day, live=False, **kw: ([_mk_match("Lota980001")], [])
    monkeypatch.setattr(dog._dm, "get_odds", lambda lid: {})
    monkeypatch.setattr(dog._dm, "get_tags", lambda lid: {})
    r = dog.analyze("2026-08-19", dry_run=True, use_llm=True)
    # 0.9 × 0.27 = 0.243 → v̂ < 1 → 被"无边际"剔除 → 空仓
    assert r["placed"] == 0
    assert r["flex"]["calibration"]["legs_with_k"] == 1
    assert r["flex"]["calibration"]["min_k"] < 0.3


def test_factor_names_are_validated(monkeypatch):
    """不存在的因子名（幻觉）不得进腿、也不影响 k。"""
    dog = BeidanParlayDog(user="pcal_validate", capital=3000.0)
    dog._parlay_cfg = _flex_cfg()
    legs_json = [
        {"lota_id": "Lota981001", "picks": ["H"], "p": {"H": 0.62},
         "factors": ["根本不存在的因子"], "why": "x"},
        {"lota_id": "Lota981002", "picks": ["A"], "p": {"A": 0.42},
         "factors": [], "why": "y"},
    ]
    dog.set_provider(_FakeProvider(legs_json))
    dog._beidan_matches = lambda day, live=False, **kw: (
        [_mk_match("Lota981001"), _mk_match("Lota981002")], [])
    monkeypatch.setattr(dog._dm, "get_odds", lambda lid: {})
    monkeypatch.setattr(dog._dm, "get_tags", lambda lid: {})
    r = dog.analyze("2026-08-19", dry_run=True, use_llm=True)
    legs = r["orders"][0]["legs"] if r["orders"] else []
    assert legs, "应出票"
    assert all(l.get("factors") == [] for l in legs)
    assert all(float(l.get("k_cal") or 1.0) == 1.0 for l in legs)


# ═══════════════════════════════════════════
# 结算时累积
# ═══════════════════════════════════════════

def test_settle_accumulates_calibration(tmp_path):
    dog = BeidanParlayDog(user="pcal_settle", capital=3000.0)
    role = dog._ensure_role()
    orders = [{
        "id": "ord_1", "settled_at": "2026-09-11 12:00:00",
        "ticket_legs": [
            {"lota_id": "L1", "picks": ["H"], "p_hat": {"H": 0.8},
             "factors": ["因子A"], "actual": "A"},
            {"lota_id": "L2", "picks": ["H", "D"], "p_hat": {"H": 0.5, "D": 0.5},
             "factors": ["因子B"], "actual": "H"},
        ],
    }]
    # 沙箱是"平铺"布局（所有角色共用一个 ROLES_DIR），所以用增量断言
    cal0 = PCalibration(Path(role._role_dir) / "memory").load()
    base_total = (cal0.data.get("legs") or {}).get("total", 0)
    n = dog._accumulate_p_calibration(role, orders)
    assert n == 3
    cal = PCalibration(Path(role._role_dir) / "memory").load()
    assert cal.data["legs"]["total"] == base_total + 2
    assert cal.data["legs"]["direction"] >= 1 and cal.data["legs"]["cover"] >= 1
    # 单选腿：声称 0.8 实际没中 → k = 2/2.8；多选腿：0.5/0.5 中一个 → 两个 (p̂,hit) 对
    assert abs(cal.k("因子A", "direction") - 2 / 2.8) < 1e-9
    st = cal.data["factors"]["因子B"]["cover"]
    assert st["n"] == 2 and abs(st["sum_p"] - 1.0) < 1e-9 and st["sum_hit"] == 1.0
    # 没有 p_hat 的腿（老订单/规则路径）不参与
    assert dog._accumulate_p_calibration(role, [{"ticket_legs": [{"lota_id": "L3"}]}]) == 0
