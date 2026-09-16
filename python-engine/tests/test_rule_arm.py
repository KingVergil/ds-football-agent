"""0-LLM 规则臂（selector="x_top"）测试：选腿、排序、门交互、analyze 全程不调 LLM。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="rule_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="rule_sessions_"))
os.environ["DS_BACKTEST_FET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beidan_parlay_dog import BeidanParlayDog  # noqa: E402
from pathlib import Path  # noqa: E402
from src.pool_ledger import PoolLedger, ledger_path  # noqa: E402


def _cfg(**over) -> dict:
    cfg = {"ticket": "8串1", "single_legs": 3, "cover_legs": 5,
           "cover_mode": "all", "cover_picks": 3,
           **BeidanParlayDog.FLEX_DEFAULTS}
    cfg.update(mode="flex", max_legs=17, max_per_leg=3, max_combos=512,
               max_stake_pct=10.0, min_leg_v=1.0, min_ticket_v=None,
               selector="x_top", rule_legs=9,
               pool_gate={"mode": "enforce", "gl_classes": ["gl0"], "min_n": 30})
    cfg.update(over)
    return cfg


def _match(lid: str, gl: float = 0.0, odds=(6.0, 3.4, 2.0)) -> dict:
    return {"lota_id": lid, "home_name": "H", "away_name": "A", "league_name": "L",
            "match_time": "2026-08-01 20:00:00", "beidan_number": "1",
            "beidan_info": {"goal_line": gl, "home_odds": odds[0],
                            "draw_odds": odds[1], "away_odds": odds[2]}}


def _arm(dog: BeidanParlayDog, p_mkt=None):
    """把市场参考/赔率接上（免读缓存）：p_mkt 决定 x = p_mkt[side] × 赔率。"""
    pm = p_mkt or {"H": 0.20, "D": 0.30, "A": 0.50}
    dog._market_p_hat = lambda m, tags=None: ("test", dict(pm))
    dog._beidan_odds = lambda m: {"h": float((m["beidan_info"] or {})["home_odds"]),
                                  "d": float((m["beidan_info"] or {})["draw_odds"]),
                                  "a": float((m["beidan_info"] or {})["away_odds"]),
                                  "goal_line": (m["beidan_info"] or {})["goal_line"]}


def test_rule_arm_picks_top_x_no_handicap():
    dog = BeidanParlayDog(user="rule_pick")
    _arm(dog)
    matches = [_match("L1", gl=0.0, odds=(6.0, 3.4, 2.0)),   # x: H1.20 D1.02 A1.00
               _match("L2", gl=0.0, odds=(5.4, 3.6, 2.3)),   # x: H1.08 D1.08 A1.15
               _match("L3", gl=-1.0, odds=(9.0, 4.0, 2.0))]  # 让球盘 → 不选
    legs = dog._select_legs_x_top(matches, _cfg(rule_legs=2))
    assert [l["lota_id"] for l in legs] == ["L1", "L2"]      # 让球盘被排除
    assert legs[0]["picks"] == ["H"] and abs(legs[0]["leg_v"] - 1.20) < 1e-6
    assert legs[1]["picks"] == ["A"] and abs(legs[1]["leg_v"] - 1.15) < 1e-6
    assert all(l["k_cal"] == 1.0 and l["factors"] == [] for l in legs)


def test_rule_arm_respects_theta_and_depth():
    dog = BeidanParlayDog(user="rule_theta")
    _arm(dog)
    # θ 由策略给（这里只有 A 侧 x=1.15 过 1.10 门）
    legs = dog._select_legs_x_top([_match("L1", odds=(3.0, 3.4, 2.3))], _cfg())
    assert len(legs) == 1 and legs[0]["picks"] == ["A"]
    kept, meta = dog._apply_flex_guardrails(_cfg(), legs, capital=5000.0,
                                            pool_policy={"mode": "enforce", "theta": 1.1,
                                                         "m_star": 4, "gl_classes": ["gl0"],
                                                         "source": "ledger", "y": 1.3,
                                                         "y_lo": 1.15, "n": 800})
    assert meta["pool_gate"]["block"] is True          # 1 关 < 4 关 → 空仓


def test_rule_arm_analyze_never_calls_llm():
    """selector=x_top 时 analyze 全程不调 LLM（`_select_legs_llm` 若被调会直接抛错）。"""
    dog = BeidanParlayDog(user="rule_nollm")
    dog.reset()
    role = dog._ensure_role()
    role.capital = 5000.0
    role.save()
    _arm(dog)
    # 4 条 x(H)=1.20 过门，1 条 x(H)=1.05 会被 θ=1.1 拦掉
    matches = [_match(f"L{i}", odds=(6.0, 3.4, 2.0)) for i in range(4)]
    matches.append(_match("L4", odds=(5.25, 3.4, 2.0)))
    dog._beidan_matches = lambda day, live=True, as_of=None: (matches, [])

    def _boom(*a, **k):
        raise AssertionError("规则臂不应该调用 LLM")
    dog._select_legs_llm = _boom

    # 写一份账本（x≥1.1 有边际）→ 策略 θ=1.1 / m_star=2
    led = PoolLedger(ledger_path("rule_nollm"))
    by_day = {f"2026-07-{d:02d}": {"n": 40, "sum_z": 52.0, "sum_z2": 67.6}
              for d in range(1, 11)}
    led.data["buckets"] = {"gl0|1.1–1.2": {"n": 400, "sum_z": 520.0,
                                           "sum_z2": 676.0, "by_day": by_day}}
    led.data["ingested"] = {}
    led.save()

    # 直接注入 flex + 规则臂配置（**不写 parlay.json**：DS_ROLES_ROOT 是共享的扁平角色目录，
    # 写文件会污染其它测试模块的角色）
    dog._parlay_cfg = _cfg(rule_legs=5)
    dog._parlay_cfg["mode"] = "flex"
    res = dog.analyze("2026-08-01", live=False, use_llm=True)
    assert res.get("llm_used") is False
    orders = res.get("orders") or []
    assert orders, "规则臂应该出票"
    o = orders[0]
    assert o["slip_type"].endswith("串1") and len(o["ticket_legs"]) == 4
    # 没到关数门的腿被拦（5 条里只有 4 条 x≥1.1：A 侧 x=1.0）
    assert all(abs(float(l.get("goal_line") or 0)) < 1e-9 for l in o["ticket_legs"])


def test_rule_arm_is_off_by_default():
    assert BeidanParlayDog.FLEX_DEFAULTS["selector"] == "llm"


# ── 反「写死 8串1（5包3单）」回归 ─────────────────────────

def test_flex_dog_without_llm_never_uses_legacy_template():
    """flex 狗在 use_llm=False（skip_llm/演示）下必须空仓，不得套 8串1/3单5包 模板。"""
    dog = BeidanParlayDog(user="flex_nollm")
    dog.reset()
    dog._parlay_cfg = _cfg(selector="llm")            # flex + gate enforce
    dog._beidan_matches = lambda day, live=True, as_of=None: (
        [_match(f"L{i}") for i in range(8)], [])
    res = dog.analyze("2026-08-01", live=False, use_llm=False)
    assert res["orders"] == [] and res["tickets"] == []
    assert "8串1" not in " ".join(res.get("tickets") or [])


def test_flex_dog_llm_failure_never_falls_back_to_template():
    """flex 狗 LLM 失败 → 空仓（历史行为是回退 3单+5包，已禁止）。"""
    dog = BeidanParlayDog(user="flex_llmfail")
    dog.reset()
    dog._parlay_cfg = _cfg(selector="llm")
    dog._beidan_matches = lambda day, live=True, as_of=None: (
        [_match(f"L{i}") for i in range(8)], [])
    dog._select_legs_llm = lambda matches, day: (None, None)
    res = dog.analyze("2026-08-01", live=False, use_llm=True)
    assert res["orders"] == [] and res["tickets"] == []


def test_prompt_dump_builds_without_llm(tmp_path=None):
    """prompt dump：只构建 prompt、不调 LLM、不落单。"""
    import tempfile
    out = Path(tempfile.mkdtemp(prefix="promptdump_"))
    dog = BeidanParlayDog(user="prompt_dump")
    dog.reset()
    dog._parlay_cfg = _cfg(selector="llm")
    dog._beidan_matches = lambda day, live=True, as_of=None: (
        [_match(f"L{i}") for i in range(3)], [])
    r = dog.dump_prompts("2026-08-01", out_dir=str(out))
    assert r["orders"] == 0 and r["files"]
    text = Path(r["files"][0]).read_text(encoding="utf-8")
    assert "## SYSTEM" in text and "## USER" in text
    assert any(c["stage"] == "stage1" for c in r["calls"])


# ── 规则组装（ticket_mode="rule"：veto 剔除 / 自动多选 / extra_picks / x_cap）──

def test_assemble_legs_rule_full_rules():
    dog = BeidanParlayDog(user="rule_assemble")
    _arm(dog)
    matches = [_match("L1", gl=0.0, odds=(5.0, 3.4, 2.6)),    # x: H1.00 D1.02 A1.30 → A 过门
               _match("L2", gl=0.0, odds=(6.0, 3.4, 2.0)),    # 被 veto
               _match("L3", gl=0.0, odds=(5.95, 3.9, 1.5)),   # H1.19 ✅ 且 D1.17 ✅ → 双选
               _match("L4", gl=0.0, odds=(5.95, 3.4, 1.5)),   # H1.19 ✅ + extra A(x0.75) → 摊薄 → 丢
               _match("L5", gl=-1.0, odds=(6.0, 3.4, 2.0)),   # 让球盘 → 不进池
               _match("L6", gl=0.0, odds=(9.0, 3.4, 2.0))]    # H1.80（>x_cap）
    cand = {m["lota_id"]: m for m in matches}
    items = [{"lota_id": "L1", "推荐": "A", "factors": ["F1"], "veto": ""},
             {"lota_id": "L2", "推荐": "H", "factors": [], "veto": "盘口异动，便宜不成立"},
             {"lota_id": "L3", "推荐": "H", "factors": ["F3"], "veto": "", "extra_picks": ["D"]},
             {"lota_id": "L4", "推荐": "H", "factors": [], "veto": "", "extra_picks": ["A"]},
             {"lota_id": "L5", "推荐": "H", "factors": [], "veto": ""},
             {"lota_id": "L6", "推荐": "H", "factors": [], "veto": ""}]
    cfg = _cfg(ticket_mode="rule", x_cap=1.45,
               pool_gate={"mode": "enforce", "gl_classes": ["gl0"], "min_n": 30})
    legs = dog._assemble_legs_rule(items, cand, cfg, "2026-08-01")
    got = {l["lota_id"]: l["picks"] for l in legs}
    assert set(got) == {"L1", "L3"}                  # L2 veto、L4 摊薄、L5 让球、L6 超 x_cap
    assert got["L1"] == ["A"]                        # 只取过门侧
    assert got["L3"] == ["H", "D"]                   # 自动双选（过门侧 + extra_picks）
    assert [l for l in legs if l["lota_id"] == "L3"][0]["factors"] == ["F3"]


def test_assemble_legs_rule_multi_pick_is_gated_by_mean_x():
    """LLM 主动要多买一侧，但平均 x 掉到门限下 → 引擎自动丢弃（无需纪律）。"""
    dog = BeidanParlayDog(user="rule_mean_gate")
    _arm(dog)
    m = _match("L1", gl=0.0, odds=(5.95, 3.4, 2.0))   # H1.19 ✅ / A0.80
    cfg = _cfg(ticket_mode="rule",
               pool_gate={"mode": "enforce", "gl_classes": ["gl0"], "min_n": 30})
    items = [{"lota_id": "L1", "推荐": "H", "factors": [], "veto": "", "extra_picks": ["A"]}]
    assert dog._assemble_legs_rule(items, {"L1": m}, cfg, "2026-08-01") == []


def test_assemble_legs_rule_caps_to_nine_and_sets_nparlay():
    """腿数 > 9 时按 stage1 顺序保留前 9（不按 x 择优），且票型固定为 N串1。"""
    dog = BeidanParlayDog(user="rule_cap9")
    _arm(dog)
    matches, items = [], []
    for i in range(12):
        lid = f"L{i:02d}"
        matches.append(_match(lid, gl=0.0, odds=(6.0, 3.4, 2.0)))     # H x=1.20 ✅
        items.append({"lota_id": lid, "推荐": "H", "factors": [], "veto": ""})
    cand = {m["lota_id"]: m for m in matches}
    cfg = _cfg(ticket_mode="rule",
               pool_gate={"mode": "enforce", "gl_classes": ["gl0"], "min_n": 30})
    legs = dog._assemble_legs_rule(items, cand, cfg, "2026-08-01")
    assert len(legs) == 9
    assert [l["lota_id"] for l in legs] == [f"L{i:02d}" for i in range(9)]   # 前 9 条
    assert dog._flex_plan_ticket == "9串1"                                     # 不是容错兜底
