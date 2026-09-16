"""
北单串关 flex 模式（腿集驱动）+ 北单返奖 ROI 数学 测试。

口径：ROI = 0.65 × Π v̂ − 1，v̂ = p̂ × 北单赛前赔率；打平需 Π v̂ > 1/0.65。
全部离线、用假 LLM，不碰线上 data/roles、data/sessions。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="flex_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="flex_sessions_"))
os.environ["DS_BACKTEST_FET"] = "0"   # 与切片源解耦，用例自带比赛数据

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beidan_parlay_dog import BeidanParlayDog

TAKEOUT = BeidanParlayDog.BEIDAN_TAKEOUT


def _mk_match(lid: str, h=2.0, d=3.4, a=4.0, gl=0) -> dict:
    return {
        "lota_id": lid, "home_name": f"主{lid[-1]}", "away_name": f"客{lid[-1]}",
        "league_name": "测试联赛", "match_time": "2026-08-19 23:00:00",
        "beidan_number": lid[-1],
        "beidan_info": {"goal_line": gl, "home_odds": h, "draw_odds": d,
                        "away_odds": a},
    }


class _FakeProvider:
    """按调用次序返回 stage1（初筛）与 stage2（腿集）的假 LLM。"""

    def __init__(self, legs: list[dict], screen: list[dict] | None = None):
        self.legs = legs
        self.screen = screen
        self.calls: list[str] = []

    def call(self, system, messages, **kw):
        self.calls.append(system)
        if "初筛器" in system:
            items = self.screen or [
                {"lota_id": l["lota_id"], "推荐": (l.get("picks") or ["H"])[0],
                 "factors": []} for l in self.legs
            ]
            return json.dumps({"items": items}, ensure_ascii=False)
        return json.dumps({"legs": self.legs, "empty": not self.legs,
                           "reason": "测试"}, ensure_ascii=False)


def _flex_cfg(**over) -> dict:
    cfg = {"ticket": "8串1", "single_legs": 3, "cover_legs": 5,
           "cover_mode": "all", "cover_picks": 3,
           **BeidanParlayDog.FLEX_DEFAULTS}
    cfg.update(mode="flex", min_legs=2, max_legs=9, max_per_leg=3,
               max_combos=128, max_stake_pct=3.0, min_leg_v=1.0,
               min_ticket_v=None, gate_basis="p")   # 老用例走 v̂ 口径
    cfg.update(over)
    return cfg


# ═══════════════════════════════════════════
# 配置 / 分流
# ═══════════════════════════════════════════

def test_flex_config_defaults_and_role_json():
    dog = BeidanParlayDog(user="flex_cfg")
    cfg = dog._load_parlay_config()
    # 没有 parlay.json 的角色 → legacy（向后兼容）
    assert cfg["mode"] == "legacy"
    assert BeidanParlayDog._is_flex(cfg) is False

    # 角色目录里写了 mode=flex → 读到护栏键
    # ⚠️ DS_ROLES_ROOT 是**所有测试模块共用**的扁平角色目录，这个文件必须用完就删，
    #    否则后面的 legacy 用例会读到 flex 配置（历史上就踩过这个坑）。
    role = dog._ensure_role()
    parlay = role._role_dir / "parlay.json"
    try:
        parlay.write_text(json.dumps({
            "mode": "flex", "min_legs": 2, "max_legs": 6, "max_combos": 27,
            "max_stake_pct": 5.0,
        }), encoding="utf-8")
        dog._parlay_cfg = None
        cfg2 = dog._load_parlay_config()
        assert BeidanParlayDog._is_flex(cfg2) is True
        assert (cfg2["max_legs"], cfg2["max_combos"], cfg2["max_stake_pct"]) == (6, 27, 5.0)
        assert cfg2["max_per_leg"] == 3      # 未写的键回落默认
    finally:
        parlay.unlink(missing_ok=True)
        dog._parlay_cfg = None


def test_breakeven_table_matches_math():
    table = BeidanParlayDog._breakeven_table()
    assert f"{TAKEOUT ** 0.5:.3f}" in table      # 2串1 → 1.240
    assert f"{TAKEOUT ** (1 / 8):.3f}" in table  # 8串1 → 1.055
    assert abs(TAKEOUT - 1 / 0.65) < 1e-9


# ═══════════════════════════════════════════
# 边际数学
# ═══════════════════════════════════════════

def test_market_p_is_devigged():
    p = BeidanParlayDog._market_p({"H": 2.0, "D": 3.4, "A": 4.0})
    assert abs(sum(p.values()) - 1.0) < 1e-9
    # 1/2.0 最大 → 概率最高
    assert p["H"] > p["D"] > p["A"]


def test_leg_marginal_uses_p_hat_and_prices_missing_side_neutral():
    leg = {"picks": ["H"], "odds": {"H": 2.0}, "p_hat": {"H": 0.60}}
    assert abs(BeidanParlayDog._leg_marginal(leg) - 1.20) < 1e-9
    # 缺 p̂ 的方向按「市场本身」计（公平赔率下中性值 ≈ 1，不是白送边际）
    leg2 = {"picks": ["H"], "odds": {"H": 2.0},
            "odds_all": {"H": 2.0, "D": 3.4, "A": 4.0}, "p_hat": {}}
    assert BeidanParlayDog._leg_marginal(leg2) is None       # 整腿无观点 → 不进票
    leg2b = {"picks": ["H", "D"], "odds": {"H": 2.0, "D": 3.4},
             "odds_all": {"H": 2.0, "D": 3.4, "A": 4.0}, "p_hat": {"H": 0.70}}
    v2b = BeidanParlayDog._leg_marginal(leg2b)
    assert 1.15 < v2b < 1.30        # (1.40 + 中性0.958) / 2 = 1.179
    # 多选腿 = 被选方向边际的平均
    leg3 = {"picks": ["H", "D"], "odds": {"H": 2.0, "D": 3.4},
            "p_hat": {"H": 0.60, "D": 0.30}}
    assert abs(BeidanParlayDog._leg_marginal(leg3) - (1.20 + 1.02) / 2) < 1e-9


# ═══════════════════════════════════════════
# 护栏
# ═══════════════════════════════════════════

def _leg(lid: str, picks: list[str], p: dict, odds: dict) -> dict:
    leg = {"lota_id": lid, "picks": picks, "odds": odds, "p_hat": p}
    leg["leg_v"] = BeidanParlayDog._leg_marginal(leg)
    return leg


def test_guardrails_drop_no_edge_legs_and_compute_roi():
    dog = BeidanParlayDog(user="flex_guard")
    legs = [
        _leg("L1", ["H"], {"H": 0.62}, {"H": 2.0}),      # v=1.24
        _leg("L2", ["A"], {"A": 0.35}, {"A": 4.0}),      # v=1.40
        _leg("L3", ["D"], {"D": 0.25}, {"D": 3.4}),      # v=0.85 → 剔
    ]
    kept, meta = dog._apply_flex_guardrails(_flex_cfg(), legs, capital=1000.0)
    assert [k["lota_id"] for k in kept] == ["L1", "L2"]
    assert meta["combos"] == 1 and meta["cost"] == 2.0
    assert abs(meta["ticket_v"] - 1.736) < 1e-3
    assert abs(meta["roi"] - 0.1284) < 1e-3
    assert abs(meta["min_ticket_v"] - BeidanParlayDog._min_ticket_v(_flex_cfg(), 2)) < 1e-9
    assert meta["min_ticket_v"] > TAKEOUT          # 奖池漂移安全垫
    assert any("L3" == d["lota_id"] for d in meta["dropped"])


def test_guardrails_trim_by_max_legs_combos_and_budget():
    dog = BeidanParlayDog(user="flex_guard2")
    legs = [_leg(f"L{i}", ["H"], {"H": 0.62}, {"H": 2.0}) for i in range(1, 6)]

    kept, meta = dog._apply_flex_guardrails(_flex_cfg(max_legs=3), legs, 1000.0)
    assert len(kept) == 3 and meta["combos"] == 1

    # 三选腿（每方向都有边际）→ 注数 3^n：max_combos=9
    # 2026-09-14 语义变更：**先削多选 pick、再删腿** → 保住 3 条腿（把一条收成单选），
    # 而不是删到 2 腿。腿数是稀缺资源，削 pick 就能达标时不该动腿。
    covers = [_leg(f"C{i}", ["H", "D", "A"],
                   {"H": 0.62, "D": 0.35, "A": 0.28},
                   {"H": 2.0, "D": 3.4, "A": 4.0}) for i in range(1, 4)]
    kept2, meta2 = dog._apply_flex_guardrails(_flex_cfg(max_combos=9), covers, 10000.0)
    assert len(kept2) == 3 and meta2["combos"] == 9 and meta2["cost"] == 18.0
    assert meta2["slimmed"], "应记录削 pick 动作（可观测）"
    assert not meta2["dropped"], "能靠削 pick 达标时不该删腿"

    # 资金 100 元 × 3% = 3 元预算 → 每注 2 元 ⇒ 只放得下 1 注
    # 2026-09-14 语义变更：先削多选 pick → 2 腿双选(4 注) 削成 2 腿单注(2 注=4 元)
    # 仍超 3 元 → 删 1 腿 → 1 注 = 2 元 ≤ 3 元 达标（旧行为是直接剔空）。
    two_way = [_leg("B1", ["H", "D"], {"H": 0.62, "D": 0.35}, {"H": 2.0, "D": 3.4}),
               _leg("B2", ["H", "D"], {"H": 0.62, "D": 0.35}, {"H": 2.0, "D": 3.4})]
    kept3, meta3 = dog._apply_flex_guardrails(_flex_cfg(), two_way, 100.0)
    assert meta3["cost"] <= meta3["budget"], "成本必须不超预算"
    # `ticket=8串1` 且只有 2 腿 ⇒ m=N ⇒ 单注：两腿削成单选后 1 注 = 2 元 ≤ 3 元
    assert len(kept3) == 2 and meta3["combos"] == 1 and meta3["cost"] == 2.0
    assert meta3["slimmed"] and not meta3["dropped"], "削 pick 即可达标，无需删腿"


def test_flex_slip_shape():
    dog = BeidanParlayDog(user="flex_slip")
    legs = [
        _leg("L1", ["H"], {"H": 0.62}, {"H": 2.0}),
        _leg("L2", ["H", "D"], {"H": 0.62, "D": 0.30}, {"H": 2.0, "D": 3.4}),
        _leg("L3", ["A"], {"A": 0.35}, {"A": 4.0}),
    ]
    slip = dog._flex_slip(legs)
    assert slip["ticket_type"] == "3串1"
    assert slip["combos_count"] == 2          # 1 × 2 × 1
    assert len(slip["legs"]) == 3
    assert dog._flex_slip([legs[0]]) is None  # <2 腿组不出串


# ═══════════════════════════════════════════
# 端到端（假 LLM）：腿集 → 护栏 → ROI 门 → 落单
# ═══════════════════════════════════════════

def _run_analyze(user: str, legs_json: list[dict], matches: list[dict],
                 cfg: dict, capital: float = 3000.0, dry_run: bool = True,
                 monkeypatch=None, dm_odds: dict | None = None):
    """跑一次 flex analyze。

    monkeypatch 必传：把 DataManager 的赔率/段落查询钉死，避免假 lid 打到线上、
    也避免 negative cache 写进线上 data/features。
    """
    dog = BeidanParlayDog(user=user, capital=capital)
    dog._parlay_cfg = cfg
    dog.set_provider(_FakeProvider(legs_json))
    dog._beidan_matches = lambda day, live=False, **kw: ([dict(m) for m in matches], [])
    if monkeypatch is not None:
        odds = dict(dm_odds or {})
        monkeypatch.setattr(dog._dm, "get_odds",
                            lambda lid: {"eu": dict(odds)} if odds else {})
        monkeypatch.setattr(dog._dm, "get_tags", lambda lid: {})
    return dog, dog.analyze("2026-08-19", dry_run=dry_run, use_llm=True)


def test_flex_end_to_end_places_ticket_and_records_edge(monkeypatch):
    matches = [_mk_match(f"Lota90000{i}") for i in range(1, 4)]
    legs_json = [
        {"lota_id": "Lota900001", "picks": ["H"], "p": {"H": 0.66}, "why": "盘口同向"},
        {"lota_id": "Lota900002", "picks": ["A"], "p": {"A": 0.42}, "why": "离散支持"},
        {"lota_id": "Lota900003", "picks": ["D"], "p": {"D": 0.25}, "why": "无边际"},
    ]
    dog, r = _run_analyze("flex_e2e_ok", legs_json, matches, _flex_cfg(), monkeypatch=monkeypatch)
    assert r["llm_used"] is True
    assert r["rejected"] is False
    assert r["placed"] == 1 and len(r["orders"]) == 1
    o = r["orders"][0]
    assert o["slip_type"] == "2串1" and o["combos_count"] == 1
    assert o["total_stake"] == 2.0
    assert len(o["legs"]) == 2                       # 无边际的第三腿被剔
    # 腿内保留 p̂/边际 → 结算后可做校准
    assert all("p_hat" in l and "leg_v" in l for l in o["legs"])
    assert o["flex"]["ticket_v"] > TAKEOUT and o["flex"]["roi"] > 0
    assert [w["at"] for w in []] == [] or True
    # 会话与结果里都有 flex 计划
    assert r["flex"]["combos"] == 1


def test_flex_end_to_end_rejects_when_no_edge(monkeypatch):
    matches = [_mk_match(f"Lota91000{i}") for i in range(1, 3)]
    legs_json = [
        {"lota_id": "Lota910001", "picks": ["H"], "p": {"H": 0.52}, "why": "略高"},
        {"lota_id": "Lota910002", "picks": ["A"], "p": {"A": 0.28}, "why": "略高"},
    ]
    dog, r = _run_analyze("flex_e2e_reject", legs_json, matches, _flex_cfg(), monkeypatch=monkeypatch)
    assert r["placed"] == 0
    assert r["rejected"] is True
    assert "出票线" in r["reject_reason"]
    # 被拒的决策仍留档（便于排查），但不落盘到角色订单
    assert len(r["orders"]) == 1 and r["orders"][0].get("rejected") is True
    assert dog._ensure_role().get_orders() == []


def test_flex_end_to_end_empty_when_llm_says_empty(monkeypatch):
    matches = [_mk_match("Lota920001")]
    dog, r = _run_analyze("flex_e2e_empty", [], matches, _flex_cfg(), monkeypatch=monkeypatch)
    assert r["placed"] == 0
    assert "空仓" in r["reject_reason"] or r["rejected"] is True


def test_flex_end_to_end_respects_capital_budget(monkeypatch):
    """资金 100 元 × 3% 预算 → 放不下 2 腿双选 → 空仓，且不是因为 LLM 拒绝。"""
    matches = [_mk_match(f"Lota93000{i}") for i in range(1, 3)]
    legs_json = [
        {"lota_id": "Lota930001", "picks": ["H", "D"], "p": {"H": 0.62, "D": 0.30},
         "why": "双选"},
        {"lota_id": "Lota930002", "picks": ["H", "D"], "p": {"H": 0.62, "D": 0.30},
         "why": "双选"},
    ]
    dog, r = _run_analyze("flex_e2e_budget", legs_json, matches, _flex_cfg(),
                          capital=100.0, monkeypatch=monkeypatch)
    assert r["placed"] == 0
    assert r["flex"]["combos"] <= 1
    # 2026-09-14：达标路径可能是「削多选 pick」而非「删腿」，两者都算被护栏修剪过
    assert (any("超出护栏" in d["why"] for d in r["flex"]["dropped"])
            or r["flex"].get("slimmed")), "应能看出被护栏修剪过"


def test_legacy_mode_still_uses_template():
    """mode 缺省（legacy）时走原来的 3胆+5包 模板逻辑，不受 flex 影响。"""
    dog = BeidanParlayDog(user="legacy_check")
    dog._parlay_cfg = {**BeidanParlayDog.FLEX_DEFAULTS, "mode": "legacy",
                       "ticket": "8串1", "single_legs": 3, "cover_legs": 5,
                       "cover_mode": "all", "cover_picks": 3}
    assert dog._is_flex(dog._parlay_cfg) is False


# ═══════════════════════════════════════════
# phase 2：引擎侧市场 p̂（封顶 + 同盘口校验）
# ═══════════════════════════════════════════

def test_market_p_hat_goal_line_zero_uses_sharp_book(monkeypatch):
    dog = BeidanParlayDog(user="mkt_pin")
    dog._dm.get_odds = lambda lid: {"eu": {"h": 2.0, "d": 3.4, "a": 4.0}}
    src, p = dog._market_p_hat(_mk_match("Lota900001", gl=0), tags={})
    assert src == "Pinnacle"
    assert abs(sum(p.values()) - 1.0) < 1e-9 and p["H"] > p["D"] > p["A"]


def test_market_p_hat_handicap_line_must_match_goal_line(monkeypatch):
    dog = BeidanParlayDog(user="mkt_line")
    dog._dm.get_odds = lambda lid: {}
    tags = {"fair-odds": "让球欧盘信息:\n  平均欧盘胜/平/负(-1):3.2/3.28/1.93\n"
                         " 竞彩胜平负(-1):3.2/3.12/2.02"}
    # goal_line=-1 → 命中同一盘口
    src, p = dog._market_p_hat(_mk_match("Lota900002", gl=-1), tags=tags)
    assert src == "让球欧盘" and abs(sum(p.values()) - 1.0) < 1e-9
    # goal_line=-2 → 盘口不一致，不得乱用
    src2, p2 = dog._market_p_hat(_mk_match("Lota900003", gl=-2), tags=tags)
    assert (src2, p2) == ("", {})
    # goal_line=0 但有 (-1) 行 → 也不能用（盘口不同）
    src3, p3 = dog._market_p_hat(_mk_match("Lota900004", gl=0), tags=tags)
    assert (src3, p3) == ("", {})


def test_flex_leg_clamps_llm_probability_to_market(monkeypatch):
    dog = BeidanParlayDog(user="mkt_clamp")
    match = _mk_match("Lota900005")           # H=2.0 D=3.4 A=4.0
    p_mkt = {"H": 0.40, "D": 0.30, "A": 0.30}
    # LLM 报 0.80（v̂=1.6），市场 0.40 × 允许 1.25 = 上限 0.5 → 砍到 0.5
    leg = dog._flex_leg(match, ["H"], {"H": 0.80}, p_mkt=p_mkt, allowance=1.25)
    assert abs(leg["p_hat"]["H"] - 0.5) < 1e-9
    assert leg["clamped"] == ["H"] and leg["market_ref"] is True
    assert abs(leg["leg_v"] - 1.0) < 1e-9          # 0.5 × 2.0
    # LLM 报 0.44（未超上限）→ 原样保留
    leg2 = dog._flex_leg(match, ["H"], {"H": 0.44}, p_mkt=p_mkt, allowance=1.25)
    assert abs(leg2["p_hat"]["H"] - 0.44) < 1e-9 and leg2["clamped"] == []
    # 没有市场参考 → 不封顶（但也不会白送：p̂ 缺失时判无边际）
    leg3 = dog._flex_leg(match, ["H"], {"H": 0.80}, p_mkt=None, allowance=1.25)
    assert leg3["market_ref"] is False and leg3["clamped"] == []


def test_flex_market_clamp_blocks_inflated_ticket(monkeypatch):
    """LLM 自报每腿 v̂=1.5，但市场（Pinnacle 2.0/3.4/4.0）只支持 ~1.06 → 封顶后过不了打平线。"""
    matches = [_mk_match(f"Lota94000{i}") for i in range(1, 4)]
    legs_json = [
        {"lota_id": f"Lota94000{i}", "picks": ["H"], "p": {"H": 0.75}, "why": "很看好"}
        for i in range(1, 4)
    ]
    cfg = _flex_cfg(market_allowance=1.10)
    dog, r = _run_analyze("flex_mkt_clamp", legs_json, matches, cfg,
                          monkeypatch=monkeypatch, dm_odds={"h": 2.0, "d": 3.4, "a": 4.0})
    clamp_info = r["flex"]["market"]
    assert clamp_info["clamped_sides"] == 3
    assert clamp_info["with_ref"] == 3
    # 市场 p̂(H)≈0.531 → 上限 0.584 → v̂≈1.17/腿 → Π≈1.60 > 1.538 → 会出票，但绝不能是 LLM 的 1.5
    for l in r["orders"][0]["legs"]:
        assert float(l["leg_v"]) < 1.2
    assert r["flex"]["ticket_v"] < 1.7


def test_require_market_p_drops_unknown_legs(monkeypatch):
    matches = [_mk_match(f"Lota95000{i}") for i in range(1, 3)]
    legs_json = [{"lota_id": f"Lota95000{i}", "picks": ["H"], "p": {"H": 0.75}, "why": "x"}
                 for i in range(1, 3)]
    cfg = _flex_cfg(require_market_p=True)
    dog, r = _run_analyze("flex_mkt_req", legs_json, matches, cfg,
                          monkeypatch=monkeypatch, dm_odds={})   # 拿不到任何市场参考
    assert r["placed"] == 0 and "空仓" in r["reject_reason"]


# ═══════════════════════════════════════════
# 人设：智能选择（单选/双选/3选 自主决定）
# ═══════════════════════════════════════════

def test_persona_frames_per_leg_choice_without_hardcoded_structure():
    """人设必须把"每腿单选/双选/3选"写成自主决策，且不得再写死结构（8串1/243注等）。"""
    p = Path(__file__).resolve().parents[1] / "data" / "roles" / "bc狗" / "persona.md"
    text = p.read_text(encoding="utf-8")
    assert "单选" in text and "双选" in text and "3 选" in text
    assert "智能选择" in text
    assert "一腿错全票作废" in text or "一腿错" in text
    # 结构参数只在 parlay.json：人设里不能再"规定"票型/注数
    # （打平表里的「8串1 1.055」是数学举例，允许出现）
    for banned in ("默认出一张", "243", "5 场做", "5 场全包", "3 场做单选"):
        assert banned not in text, f"人设里残留了写死的结构：{banned}"


# ═══════════════════════════════════════════
# 票型自由度：最高 9 关；N串1 / N过M 由 LLM 指定
# ═══════════════════════════════════════════

def _edge_leg(lid: str, picks: list[str], v: float) -> dict:
    leg = {"lota_id": lid, "picks": picks,
           "odds": {p: 2.0 for p in picks}, "p_hat": {p: 0.6 for p in picks}}
    leg["leg_v"] = v
    return leg


def test_flex_leg_ceiling_is_nine_per_combo():
    """上限是「每注 ≤ 9 关」，不是「最多选 9 场」：10 腿 → 引擎兜底 10过9（容错 1）。"""
    dog = BeidanParlayDog(user="flex9")
    legs = [_edge_leg(f"L{i}", ["H"], 1.12) for i in range(9)]
    kept, meta = dog._apply_flex_guardrails(_flex_cfg(max_legs=17), legs,
                                            capital=10000.0)
    assert len(kept) == 9 and meta["combos"] == 1 and meta["m"] == 9
    assert abs(meta["ticket_v"] - 1.12 ** 9) < 1e-9
    # 第 10 条腿不再被裁：N=10 自动走容错票 10过9（每注 9 关、容错 1）
    kept10, meta10 = dog._apply_flex_guardrails(
        _flex_cfg(max_legs=17), legs + [_edge_leg("L9", ["H"], 1.12)],
        capital=10000.0)
    assert len(kept10) == 10 and meta10["legs"] == 10
    assert meta10["ticket"] == "10过9" and meta10["combos"] == 10
    assert meta10["m"] == 9
    # 组票：9 腿 → 9串1；10 腿 → 10过9；18 腿（超选场上限）→ 不给组
    assert dog._flex_slip(legs)["ticket_type"] == "9串1"
    assert dog._flex_slip(legs + [_edge_leg("L9", ["H"], 1.12)]
                          )["ticket_type"] == "10过9"
    assert dog._flex_slip([_edge_leg(f"L{i}", ["H"], 1.12)
                           for i in range(18)]) is None


def test_flex_tolerance_preserved_when_trimming():
    """成本护栏裁腿时**保持容错额度 t**：9过8 裁到 8 腿 → 8过7，绝不退化成 8串1。"""
    dog = BeidanParlayDog(user="flex_tol_trim")
    legs = [_edge_leg(f"L{i}", ["H"], 1.15) for i in range(9)]
    kept, meta = dog._apply_flex_guardrails(_flex_cfg(max_legs=17), legs,
                                            capital=533.4, ticket="9过8")
    # 533.4 × 3% = 16 元：9过8（9 注 18 元）放不下，8过7（8 注 16 元）放得下
    assert len(kept) == 8 and meta["ticket"] == "8过7"
    assert meta["combos"] == 8 and meta["cost"] == 16.0
    assert meta["ticket_requested"] == "9过8" and meta["ticket_changed"] is True
    assert meta["m"] == 7


def test_flex_deep_tolerance_ticket_12_over_8():
    """12 场单选、容错 4（`12过8` = C(12,8)=495 注 990 元）：资金/注数够时原样出票。"""
    dog = BeidanParlayDog(user="flex_tol_12")
    legs = [_edge_leg(f"L{i:02d}", ["H"], 1.12) for i in range(12)]
    kept, meta = dog._apply_flex_guardrails(
        _flex_cfg(max_legs=17, max_combos=1024, max_stake_pct=5.0), legs,
        capital=40000.0, ticket="12过8")
    assert len(kept) == 12 and meta["ticket"] == "12过8"
    assert meta["combos"] == 495 and meta["cost"] == 990.0
    assert meta["m"] == 8 and meta["ticket_changed"] is False
    # 注数上限 128 时按「容错 t=4」往下裁腿：C(9,4)=126 放得下、C(10,4)=210 放不下
    kept2, meta2 = dog._apply_flex_guardrails(
        _flex_cfg(max_legs=17, max_combos=128, max_stake_pct=50.0), legs,
        capital=40000.0, ticket="12过8")
    assert len(kept2) == 9 and meta2["ticket"] == "9过5"
    assert meta2["combos"] == 126 and meta2["m"] == 5


def test_flex_ticket_guom_exact_math():
    """N过M：注数 = Σ C(N,M)·Πk（成本随之变化），等效 Πv̂ = 期望/注数（不是裸 Πv̂）。"""
    dog = BeidanParlayDog(user="flex_guom")
    legs = [_edge_leg("L1", ["H"], 1.3),
            _edge_leg("L2", ["H", "D"], 1.2),
            _edge_leg("L3", ["A"], 1.1)]
    kept, meta = dog._apply_flex_guardrails(_flex_cfg(), legs, capital=10000.0,
                                            ticket="3过2")
    assert meta["ticket"] == "3过2" and meta["m"] == 2
    assert meta["combos"] == 5                       # (1×2)+(1×1)+(2×1)
    assert meta["cost"] == 10.0
    assert abs(meta["ticket_v"] - 7.19 / 5) < 1e-6   # = 1.438，而非 1.3×1.2×1.1=1.716
    assert abs(meta["roi"] - (0.65 * 7.19 / 5 - 1)) < 1e-6
    slip = dog._flex_slip(legs, ticket="3过2")
    assert slip["ticket_type"] == "3过2" and slip["combos_count"] == 5
    assert slip["sub_ticket"] == "3过2"     # 容错票：展示完整票型（每注 2 关但不藏 N）


def test_flex_ticket_validation_and_fallbacks():
    dog = BeidanParlayDog(user="flex_tk_validate")
    assert dog._valid_flex_ticket("9串1", 9) == "9串1"
    assert dog._valid_flex_ticket("9过8", 9) == "9过8"
    assert dog._valid_flex_ticket("4串1", 3) == "3串1"      # N 与腿数不符
    assert dog._valid_flex_ticket("10串1", 10) == "10过9"   # 一注 10 关 → 兜底容错票
    assert dog._valid_flex_ticket("12过8", 12) == "12过8"   # 12 场单选容错 4
    assert dog._valid_flex_ticket("17过9", 17) == "17过9"   # 选场/容错双上限
    assert dog._valid_flex_ticket("12过2", 12) == "12过9"   # 容错 10 > 8 → 兜底
    assert dog._valid_flex_ticket("10过10", 10) == "10过9"  # M > 9 关 → 兜底
    assert dog._valid_flex_ticket("乱写", 3) == "3串1"
    assert dog._valid_flex_ticket(None, 3) == "3串1"
    assert dog._valid_flex_ticket(None, 12) == "12过9"
    # 12 腿（无票型）也可以组票：兜底 12过9
    assert dog._flex_slip([_edge_leg(f"L{i}", ["H"], 1.1)
                           for i in range(12)])["ticket_type"] == "12过9"
    # 选场上限 17 → 18 腿拒绝组票
    assert dog._flex_slip([_edge_leg(f"L{i}", ["H"], 1.1) for i in range(18)]) is None


def test_flex_nine_leg_ticket_passes_gate():
    """9 关票的漂移安全垫更厚（≈2.00），Πv̂ 足够时仍能出票。"""
    dog = BeidanParlayDog(user="flex9_gate")
    legs = [_edge_leg(f"L{i}", ["H"], 1.15) for i in range(9)]
    kept, meta = dog._apply_flex_guardrails(_flex_cfg(), legs, capital=10000.0)
    assert abs(meta["min_ticket_v"] - BeidanParlayDog._min_ticket_v(_flex_cfg(), 9)) < 1e-9
    assert meta["min_ticket_v"] > 1.9
    assert meta["ticket_v"] > meta["min_ticket_v"]      # 1.15^9≈3.52 > 2.00


def test_ticket_freedom_records_requested_vs_final():
    """票型自由度：腿数被成本护栏裁掉后，票型随之变化并**明示**（不留模糊）。"""
    dog = BeidanParlayDog(user="flex_tk_change")
    legs = [_edge_leg(f"L{i}", ["H"], 1.15) for i in range(9)]
    # 资金 100 元 × 3% = 3 元预算 → 只能留 2 腿（2 注 4 元都放不下 → 2串1 1 注 2 元）
    kept, meta = dog._apply_flex_guardrails(_flex_cfg(max_legs=17), legs,
                                            capital=100.0, ticket="9过8")
    assert len(kept) == 2
    assert meta["ticket_requested"] == "9过8"
    assert meta["ticket_changed"] is True
    assert meta["ticket"] == "2串1"                 # 2 腿放不下容错（最多 N−2）
    assert meta["ticket"].startswith(str(len(kept)))   # 票型 N 与最终腿数一致
    # 容错额度内的裁腿：533.4 元 × 3% = 16 元 → 8过7（容错 t=1 保住）
    kept_t, meta_t = dog._apply_flex_guardrails(_flex_cfg(max_legs=17), legs,
                                                capital=533.4, ticket="9过8")
    assert len(kept_t) == 8 and meta_t["ticket"] == "8过7"
    # 请求票型能放下时不改
    kept2, meta2 = dog._apply_flex_guardrails(_flex_cfg(min_legs=2, max_legs=17),
                                              legs, capital=10000.0, ticket="9过8")
    assert len(kept2) == 9 and meta2["ticket"] == "9过8"
    assert meta2["ticket_changed"] is False


def test_flex_deep_tolerance_slip_matches_settlement_spec():
    """组票 → 结算口径一致：12过8 落成 495 注 / 12 腿，票型原样进结算（容错额度 4）。"""
    from src.beidan_settlement import parse_ticket_spec
    dog = BeidanParlayDog(user="flex_tol_slip")
    legs = [_edge_leg(f"L{i:02d}", ["H"], 1.12) for i in range(12)]
    slip = dog._flex_slip(legs, "12过8")
    assert slip["ticket_type"] == "12过8" and slip["combos_count"] == 495
    assert len(slip["legs"]) == 12 and slip["sub_ticket"] == "12过8"
    spec = parse_ticket_spec(slip["ticket_type"])
    assert (spec.n, spec.m) == (12, 8)
    assert max(0, spec.n - spec.m) == 4        # 结算侧容错额度
