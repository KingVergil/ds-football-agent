"""
双路径因子飞轮：样本口径（unit_cost）与波动路径 avg_sp 的修正。

背景：北单「单选腿输 = −1」「全包腿输 = −3」注数基数不同，混在同一根 return_ratio 上
会让 avg_sp 反推系统性偏低 (3−1)/0.65 ≈ 3.08（2026-09-11 调查 G1）。
本文件锁定：基数识别只认证据、反推公式按基数、口径不明时不显示 avg_sp。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="facpath_roles_"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.factor_select import (  # noqa: E402
    factor_profile,
    sample_unit_cost,
    sp_from_return_ratio,
)
from src.memory import FactorMemory  # noqa: E402

RR_COVER = 2.155          # 真实 SP=7.93 的全包腿：rr = 0.65×7.93 − 3
RR_DIRECTIONAL = 0.30     # 单注腿命中：rr = 0.65×2.0 − 1


# ═══════════════════════════════════════════
# 注数基数识别（只认证据）
# ═══════════════════════════════════════════

def test_unit_cost_only_when_evidence_is_unanimous():
    # 显式字段与"输样本"证据冲突 → 混合口径 → 因子级返回 None（由逐样本 unit_cost 处理）
    assert sample_unit_cost([{"return_ratio": -3.0}, {"return_ratio": 1.0, "unit_cost": 1.0}]) is None
    assert sample_unit_cost([{"unit_cost": 3.0}]) == 3.0
    assert sample_unit_cost([{"unit_cost": 1.0}, {"unit_cost": 1.0}]) == 1.0
    # 1 注与 3 注混在同一因子（实测 `亚盘盘口反复横跳` = [1,3,3]）→ None
    assert sample_unit_cost([{"unit_cost": 1.0}, {"unit_cost": 3.0}]) is None


def test_unit_cost_from_losing_samples():
    assert sample_unit_cost([{"return_ratio": -3.0, "hit": False}]) == 3.0
    assert sample_unit_cost([{"return_ratio": -1.0, "hit": False}]) == 1.0
    assert sample_unit_cost([{"return_ratio": -2.0, "hit": False}]) is None   # 不像任何一种 → 不猜


def test_unit_cost_unknown_when_all_winners():
    """全命中样本无法区分口径 → 必须返回 None，而不是按因子类型猜。"""
    assert sample_unit_cost([{"return_ratio": 2.0, "hit": True}]) is None
    assert sample_unit_cost([]) is None
    assert sample_unit_cost([{"return_ratio": 0.0, "hit": None}]) is None


# ═══════════════════════════════════════════
# 反推 SP：必须带基数
# ═══════════════════════════════════════════

def test_sp_from_return_ratio_uses_basis():
    # 全包腿：rr = 0.65·sp − 3 → sp = (rr+3)/0.65
    assert abs(sp_from_return_ratio(RR_COVER, 3.0, True) - 7.93) < 0.01
    # 单注腿：rr = 0.65·sp − 1 → sp = (rr+1)/0.65
    assert abs(sp_from_return_ratio(RR_DIRECTIONAL, 1.0, True) - 2.0) < 0.01
    # 旧口径（固定按单注解）对全包腿会低估 2/0.65 ≈ 3.08
    assert abs(sp_from_return_ratio(RR_COVER, 1.0, True) - 4.854) < 0.01
    # 未命中/走水不携带 SP 信息
    assert sp_from_return_ratio(-3.0, 3.0, False) is None
    assert sp_from_return_ratio(-3.0, 3.0, None) is None
    assert sp_from_return_ratio(0.0, 3.0, True) is None


# ═══════════════════════════════════════════
# factor_profile：波动路径 avg_sp 口径
# ═══════════════════════════════════════════

def _vol_factor(hist, **extra):
    return {"type": "volatility", "history": hist, "total": len(hist),
            "hit": len(hist), "push": 0, **extra}


def _h(days_ago: int, rr: float, hit=True, **extra) -> dict:
    d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {"date": d, "hit": hit, "return_ratio": rr, "profit": rr * 2.0,
            "lota_id": f"L{days_ago}", **extra}


def test_avg_sp_cover_basis_without_sp_field():
    """老样本没有 sp，但有「输样本」证据 → 按 3 注口径反推（旧实现固定按 1 注，低 3.08）。"""
    stats = _vol_factor([_h(2, RR_COVER), _h(1, RR_COVER), _h(0, -3.0, hit=False)])
    p = factor_profile(stats)
    assert p["sp_basis"] == 3.0
    assert p["sp_samples"] == 2
    assert abs(p["avg_sp"] - 7.93) < 0.05


def test_avg_sp_uses_explicit_sp_first():
    stats = _vol_factor([_h(1, RR_COVER, sp=6.5), _h(0, RR_COVER, sp=8.5)])
    p = factor_profile(stats)
    assert abs(p["avg_sp"] - 7.5) < 0.3          # 只用 sp，不做反推
    assert p["sp_samples"] == 2


def test_avg_sp_is_none_when_basis_unknown_for_beidan_path():
    """北单因子（path=beidan）全命中且无 sp/unit_cost → 口径不明：avg_sp 为空（宁缺勿错）。"""
    stats = _vol_factor([_h(1, RR_COVER), _h(0, RR_COVER)], path="beidan")
    p = factor_profile(stats)
    assert p["avg_sp"] is None
    assert p["sp_basis"] is None
    assert p["sp_samples"] == 0
    # 标了口径默认值 → 按该口径算
    stats2 = _vol_factor([_h(1, RR_COVER), _h(0, RR_COVER)], path="beidan",
                         unit_cost_default=3.0)
    p2 = factor_profile(stats2)
    assert abs(p2["avg_sp"] - (RR_COVER + 3.0) / 0.65) < 1e-9


def test_non_beidan_factors_keep_legacy_avg_sp():
    """⚠️ 隔离红线：没有 path=beidan 标注的因子（单关狗/竞彩狗等）必须与旧实现逐位一致。"""
    stats = _vol_factor([_h(1, RR_COVER), _h(0, RR_COVER)])
    new = factor_profile(stats)["avg_sp"]
    # 旧实现：固定按单注口径 (rr+1)/0.65 反推
    import src.factor_select as fs
    orig = fs.sp_from_return_ratio
    try:
        fs.sp_from_return_ratio = (
            lambda rr, uc, hit: ((float(rr) + 1.0) / 0.65
                                 if (hit is True and rr and rr > 0) else None))
        old = fs.factor_profile(stats)["avg_sp"]
    finally:
        fs.sp_from_return_ratio = orig
    assert new is not None and abs(new - old) < 1e-12


def test_avg_sp_with_sample_level_unit_cost():
    """新样本自带 unit_cost → 直接按它反推（即使没有"输样本"证据）。"""
    stats = _vol_factor([_h(1, RR_COVER, unit_cost=3.0)])
    p = factor_profile(stats)
    assert p["sp_basis"] == 3.0 and abs(p["avg_sp"] - 7.93) < 0.05


def test_directional_factor_has_no_avg_sp():
    stats = {"type": "directional", "history": [_h(1, RR_DIRECTIONAL)],
             "total": 1, "hit": 1, "push": 0}
    p = factor_profile(stats)
    assert p["avg_sp"] is None and p["sp_basis"] is None


# ═══════════════════════════════════════════
# 写入侧：样本自带口径
# ═══════════════════════════════════════════

def test_memory_record_stores_unit_cost(tmp_path):
    fm = FactorMemory(base_dir=tmp_path)
    fm.load()
    fm.record("测试因子", True, 4.31, desc="d", date="2026-09-01",
              lota_id="L1", bet_size=2.0, factor_type="volatility",
              sp=7.93, unit_cost=3.0)
    fm.record("测试因子", False, -6.0, desc="d", date="2026-09-02",
              lota_id="L2", bet_size=2.0, factor_type="volatility",
              unit_cost=3.0)
    entry = fm.factor_perf["测试因子"]
    assert entry["type"] == "volatility"
    assert [h.get("unit_cost") for h in entry["history"]] == [3.0, 3.0]
    assert entry["history"][0]["sp"] == 7.93
    # 落盘后仍可读回
    raw = json.loads((tmp_path / "factor_memory.json").read_text(encoding="utf-8"))
    assert raw["factor_perf"]["测试因子"]["history"][1]["unit_cost"] == 3.0
    # 不带 unit_cost 的调用（竞彩/老路径）不应写该字段
    fm.record("方向因子", True, 0.6, desc="d", date="2026-09-02", bet_size=2.0)
    assert "unit_cost" not in fm.factor_perf["方向因子"]["history"][0]


# ═══════════════════════════════════════════
# 回填脚本（只认证据）
# ═══════════════════════════════════════════

def test_backfill_script_only_uses_evidence(tmp_path):
    from scripts.backfill_factor_paths import backfill_file

    doc = {"factor_perf": {
        # 有输样本证据（全包口径）→ 补 type=volatility + unit_cost=3
        "覆盖因子": {"history": [{"date": "2026-09-01", "hit": True, "return_ratio": 2.155},
                                 {"date": "2026-09-02", "hit": False, "return_ratio": -3.0}]},
        # 有输样本证据（单注口径）→ type=directional + unit_cost=1
        "方向因子": {"history": [{"date": "2026-09-01", "hit": False, "return_ratio": -1.0}]},
        # 全命中无证据 → 两个字段都不动
        "无证据因子": {"history": [{"date": "2026-09-01", "hit": True, "return_ratio": 1.5}]},
        # 已有 type → 不覆盖
        "老方向因子": {"type": "directional",
                       "history": [{"date": "2026-09-01", "hit": False, "return_ratio": -1.0}]},
    }}
    p = tmp_path / "factor_memory.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    st = backfill_file(p, apply=False)
    # 覆盖 2 条 + 方向 1 条 + 老方向因子 1 条（type 已有，但样本基数照样补）
    assert st["type_filled"] == 2 and st["unit_filled"] == 4 and st["unknown"] == 1
    # dry-run 不写盘
    assert "type" not in json.loads(p.read_text(encoding="utf-8"))["factor_perf"]["覆盖因子"]

    st2 = backfill_file(p, apply=True)
    assert st2.get("backup")
    after = json.loads(p.read_text(encoding="utf-8"))["factor_perf"]
    assert after["覆盖因子"]["type"] == "volatility"
    assert [h["unit_cost"] for h in after["覆盖因子"]["history"]] == [3.0, 3.0]
    assert after["方向因子"]["type"] == "directional"
    assert after["方向因子"]["history"][0]["unit_cost"] == 1.0
    assert "type" not in after["无证据因子"]
    assert "unit_cost" not in after["无证据因子"]["history"][0]
    assert after["老方向因子"]["type"] == "directional"
    # 幂等：再跑一次没有新改动
    st3 = backfill_file(p, apply=True)
    assert st3["type_filled"] == 0 and st3["unit_filled"] == 0
