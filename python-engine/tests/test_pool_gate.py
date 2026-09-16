"""奖池门测试：腿池门（gl + x≥θ）、关数门（每注 M≥m_star）、shadow/off 模式、账本接线。

全部离线（临时 DS_ROLES_ROOT），不碰线上 data/roles。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="gate_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="gate_sessions_"))
os.environ["DS_BACKTEST_FET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beidan_parlay_dog import BeidanParlayDog  # noqa: E402
from src.pool_ledger import PoolLedger, ledger_path  # noqa: E402


def _cfg(**over) -> dict:
    cfg = {"ticket": "8串1", "single_legs": 3, "cover_legs": 5,
           "cover_mode": "all", "cover_picks": 3,
           **BeidanParlayDog.FLEX_DEFAULTS}
    cfg.update(mode="flex", min_legs=2, max_legs=17, max_per_leg=3,
               max_combos=512, max_stake_pct=10.0, min_leg_v=1.0,
               min_ticket_v=None, gate_basis="p")   # 本文件的通用夹具走 v̂ 口径
    cfg.update(over)
    return cfg


def _leg(lid: str, gl: float = 0.0, x: float = 1.3, v: float = 1.2,
         picks=("H",)) -> dict:
    picks = list(picks)
    return {"lota_id": lid, "picks": picks, "leg_v": v, "x_mkt": x,
            "goal_line": gl, "odds": {p: 2.0 for p in picks},
            "p_hat": {p: 0.6 for p in picks}}


def _policy(mode="enforce", theta=1.1, m_star=3, gl=("gl0",)) -> dict:
    return {"mode": mode, "theta": theta, "m_star": m_star,
            "gl_classes": list(gl), "source": "ledger",
            "y": 1.3, "y_lo": 1.15, "n": 800}


# ── 门 ①：腿池 ──────────────────────────────────────────

def test_gate_drops_handicap_and_low_x():
    dog = BeidanParlayDog(user="gate_drop")
    legs = [_leg("L1", gl=0.0, x=1.25),      # ok
            _leg("L2", gl=-1.0, x=1.40),     # 让球盘 → 拦
            _leg("L3", gl=0.0, x=0.95),      # x 不够 → 拦
            _leg("L4", gl=0.0, x=1.10)]      # 正好等于门限 → 放
    kept, meta = dog._apply_flex_guardrails(_cfg(), legs, capital=10000.0,
                                            pool_policy=_policy())
    assert [l["lota_id"] for l in kept] == ["L1", "L4"]
    why = " | ".join(d["why"] for d in meta["dropped"])
    assert "腿池门" in why and "glN" in why and "x=0.950" in why
    assert meta["pool_gate"]["x_min"] == 1.1


def test_gate_missing_x_is_refused():
    dog = BeidanParlayDog(user="gate_nox")
    leg = _leg("L1"); leg["x_mkt"] = None       # 拿不到锐市场 → 算不出 x
    kept, meta = dog._apply_flex_guardrails(_cfg(), [leg, _leg("L2")],
                                            capital=10000.0, pool_policy=_policy())
    assert [l["lota_id"] for l in kept] == ["L2"]
    assert "算不出 x" in " ".join(d["why"] for d in meta["dropped"])


def test_gate_shadow_keeps_legs_but_records():
    dog = BeidanParlayDog(user="gate_shadow")
    legs = [_leg("L1"), _leg("L2", gl=-1.0), _leg("L3", x=0.9)]
    kept, meta = dog._apply_flex_guardrails(_cfg(), legs, capital=10000.0,
                                            pool_policy=_policy(mode="shadow"))
    assert len(kept) == 3                       # shadow 不拦
    assert len(meta["pool_gate"]["refused"]) == 2
    assert meta["pool_gate"].get("block") is None


# ── 门 ②：关数 ──────────────────────────────────────────

def test_gate_blocks_ticket_below_min_legs():
    dog = BeidanParlayDog(user="gate_depth")
    legs = [_leg(f"L{i}") for i in range(3)]     # 3 关
    _, meta = dog._apply_flex_guardrails(_cfg(), legs, capital=10000.0,
                                         ticket="3串1", pool_policy=_policy(m_star=5))
    assert meta["pool_gate"]["block"] is True
    assert meta["pool_gate"]["m"] == 3 and meta["pool_gate"]["m_star"] == 5
    assert "打平所需 5 关" in meta["pool_gate"]["why"]
    # 门限降到 3 → 不拦
    _, meta2 = dog._apply_flex_guardrails(_cfg(), legs, capital=10000.0,
                                          ticket="3串1", pool_policy=_policy(m_star=3))
    assert meta2["pool_gate"].get("block") is None
    # 容错票按**每注关数** M 算：5过4 的 M=4
    legs5 = [_leg(f"K{i}") for i in range(5)]
    _, meta3 = dog._apply_flex_guardrails(_cfg(max_combos=512), legs5,
                                          capital=10000.0, ticket="5过4",
                                          pool_policy=_policy(m_star=5))
    assert meta3["pool_gate"]["m"] == 4 and meta3["pool_gate"]["block"] is True


def test_gate_absent_when_policy_not_loaded():
    """没传策略（= 门关闭）时，行为与从前完全一致。"""
    dog = BeidanParlayDog(user="gate_off")
    legs = [_leg("L1"), _leg("L2", gl=-1.0, x=0.5)]
    kept, meta = dog._apply_flex_guardrails(_cfg(), legs, capital=10000.0)
    assert len(kept) == 2 and meta["pool_gate"] is None
    assert BeidanParlayDog.FLEX_DEFAULTS["pool_gate"]["mode"] == "off"


def test_pool_gate_policy_returns_none_when_off():
    dog = BeidanParlayDog(user="gate_cfg_off")
    assert dog._pool_gate_policy(_cfg()) is None
    assert dog._pool_gate_policy(
        _cfg(pool_gate={"mode": "off"})) is None


# ── 接线：策略从角色目录的账本读 ────────────────────────

def test_policy_reads_ledger_from_role_dir():
    dog = BeidanParlayDog(user="gate_ledger")
    dog._ensure_role()
    led = PoolLedger(ledger_path("gate_ledger"))
    # 造一个「x≥1.1 有边际、x∈[1.0,1.1) 没边际」的账本（每个桶各自 20 天，日粒度用于 as_of 切片）
    def _days(n: int, z: float) -> dict:
        return {f"2026-07-{d:02d}": {"n": n, "sum_z": z * n, "sum_z2": z * z * n}
                for d in range(1, 21)}
    led.data["buckets"] = {
        "gl0|1.1–1.2": {"n": 400, "sum_z": 520.0, "sum_z2": 676.0,
                        "by_day": _days(20, 1.3)},
        "gl0|1–1.1": {"n": 400, "sum_z": 400.0, "sum_z2": 400.0,
                      "by_day": _days(20, 1.0)},
        "gl0|0.9–1": {"n": 400, "sum_z": 380.0, "sum_z2": 361.0,
                      "by_day": _days(20, 0.95)},
    }
    led.data["ingested"] = {f"X{i}": "2026-07-01" for i in range(800)}
    led.save()

    cfg = _cfg(pool_gate={"mode": "enforce", "gl_classes": ["gl0"], "min_n": 30})
    pol = dog._pool_gate_policy(cfg, as_of="2026-08-01")
    assert pol is not None and pol["source"] == "ledger"
    assert pol["theta"] == 1.1 and pol["y"] > 1.2
    assert pol["m_star"] and pol["m_star"] >= 1
    # as_of 在数据之前 → 用配置默认，不拦
    pol0 = dog._pool_gate_policy(cfg, as_of="2026-06-01")
    assert pol0["source"] == "config_default"
    assert pol0["m_star"] == BeidanParlayDog.FLEX_DEFAULTS["pool_gate"]["default_min_legs"]


def test_settle_update_hook_is_noop_when_off():
    """pool_gate 关闭时，结算里的账本更新直接返回 0（不读盘、不写盘）。"""
    dog = BeidanParlayDog(user="gate_hook_off")
    dog.reset()
    role = dog._ensure_role()
    assert dog._update_pool_ledger(role, "2026-08-01") == 0


# ── 让球线换算兜底 + 观察盘口（2026-09-11 口径）─────────────

def test_market_ref_falls_back_to_line_conversion():
    """gl≠0 且没有「平均欧盘(line)」→ 用 Pinnacle 1X2 换算到该让球线。"""
    dog = BeidanParlayDog(user="mkt_conv")
    dog._dm.get_odds = lambda lid: {}
    tags = {"eu-odds-pinnacle": "欧盘:Pinnacle\nOPt-100m=2.60/3.30/2.90\nΔt+50m=2.50/3.40/2.80"}
    m = {"lota_id": "Lota900010", "beidan_info": {"goal_line": -1}}
    src, p = dog._market_p_hat(m, tags=tags)
    assert src == "Pinnacle换算(line)" and abs(sum(p.values()) - 1.0) < 1e-6
    # 让球后 H 概率必须低于不让球时（主让一球更难）
    src0, p0 = dog._market_p_hat({"lota_id": "Lota900011",
                                  "beidan_info": {"goal_line": 0}}, tags=tags)
    assert p["H"] < p0["H"] and p["A"] > p0["A"]


def test_observe_gl_legs_are_dropped_and_recorded():
    """观察盘口（glN）：进候选、算 x、但不进票，且被记录。"""
    dog = BeidanParlayDog(user="gate_observe")
    legs = [_leg("L1", gl=0.0, x=1.25), _leg("L2", gl=-1.0, x=1.40)]
    pol = dict(_policy(), observe_gl_classes=["glN"])
    kept, meta = dog._apply_flex_guardrails(_cfg(), legs, capital=10000.0,
                                           pool_policy=pol)
    assert [l["lota_id"] for l in kept] == ["L1"]
    g = meta["pool_gate"]
    assert [o["lota_id"] for o in g["observed"]] == ["L2"]
    assert g["observe_gl_classes"] == ["glN"]
    assert any("观察盘口" in d["why"] for d in meta["dropped"])


# ── 成票判据锁死 x（LLM 的 p̂ 不参与成票）──────────────

def test_ticket_gate_locked_to_x_ignores_inflated_p():
    """同一批腿：把 LLM 的 p̂ 抬高（v̂ 变大）也不能把票从"不出"变成"出"。"""
    dog = BeidanParlayDog(user="gate_x_lock")
    # 3 腿，x 乘积 ≈ 1.789（≤ 3 关出票线 1.791）；若用 LLM 的 v̂ 则 ≈ 2.61（会出票）
    legs = []
    for lid, x in (("L1", 1.2463), ("L2", 1.2106), ("L3", 1.1854)):
        leg = _leg(lid, gl=0.0, x=x, v=1.379)      # v̂ = LLM 口径（被抬高）
        legs.append(leg)
    cfg_x = _cfg(gate_basis="x", min_ticket_v=None)
    kept_x, meta_x = dog._apply_flex_guardrails(cfg_x, legs, capital=10000.0,
                                                ticket="3串1")
    assert len(kept_x) == 3
    assert abs(meta_x["ticket_v"] - 1.789) < 0.01          # 判据 = Πx
    assert abs(meta_x["ticket_v_llm"] - 2.62) < 0.02       # 展示用（LLM 口径）
    assert meta_x["ticket_v"] <= meta_x["min_ticket_v"]     # → 不出票
    # 旧口径（gate_basis="p"）会出票 —— 说明锁定确实生效
    cfg_p = _cfg(gate_basis="p", min_ticket_v=None)
    _, meta_p = dog._apply_flex_guardrails(cfg_p, legs, capital=10000.0, ticket="3串1")
    assert meta_p["ticket_v"] > meta_p["min_ticket_v"]
    # 默认口径是 x
    assert BeidanParlayDog.FLEX_DEFAULTS["gate_basis"] == "x"
