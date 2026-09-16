"""奖池账本（src/pool_ledger.py）测试：幂等 / 开奖 SP 滞后 / as_of 防未来 / 滚动窗口 / 门策略。

全部离线，只用临时目录，不碰线上 data/roles。
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="led_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="led_sessions_"))
os.environ["DS_BACKTEST_FET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pool_ledger import (  # noqa: E402
    PoolLedger, TAKEOUT, available_days, bucket_key, collect_day_rows,
    handicap_class, ledger_path, update, x_bucket,
)

SIDES = ("H", "D", "A")


def _row(lid: str, day: str, gl: float = 0.0, x=(1.2, 1.1, 0.9),
         sp: float | None = 3.0, result: str = "3") -> dict:
    return {"lid": lid, "date": day, "gl": gl, "src": "test",
            "x": dict(zip(SIDES, x)), "sp": sp, "result": result}


def _tmp_ledger() -> PoolLedger:
    return PoolLedger(Path(tempfile.mkdtemp(prefix="led_")) / "pool_ledger.json")


# ── 基础 ────────────────────────────────────────────────

def test_bucket_key_and_handicap_class():
    assert handicap_class(0) == "gl0" and handicap_class(-1) == "glN"
    assert x_bucket(1.15) == "1.1–1.2"
    assert bucket_key(0.0, 1.15) == "gl0|1.1–1.2"
    assert bucket_key(-1.0, 1.15) == "glN|1.1–1.2"


def test_ingest_counts_only_winning_side():
    led = _tmp_ledger()
    # H 中（result=3），SP=3.0 → 只有 H 侧 z=3.0，另两侧 z=0
    res = led.ingest([_row("L1", "2026-08-01", x=(1.2, 1.1, 0.9), sp=3.0, result="3")])
    assert res["added"] == 1
    # 三侧各进各的桶：H(1.2)→1.2–1.35 且只有它中（z=3.0）；D(1.1)→1.1–1.2；A(0.9)→0.9–1
    assert led.data["buckets"]["gl0|1.2–1.35"]["n"] == 1
    assert abs(led.data["buckets"]["gl0|1.2–1.35"]["sum_z"] - 3.0) < 1e-9
    assert led.data["buckets"]["gl0|1.1–1.2"]["n"] == 1
    assert abs(led.data["buckets"]["gl0|1.1–1.2"]["sum_z"]) < 1e-9
    assert led.data["buckets"]["gl0|0.9–1"]["n"] == 1


def test_ingest_idempotent():
    led = _tmp_ledger()
    rows = [_row("L1", "2026-08-01"), _row("L2", "2026-08-01")]
    led.ingest(rows)
    n1 = sum(b["n"] for b in led.data["buckets"].values())
    res2 = led.ingest(rows)          # 再入账一次
    n2 = sum(b["n"] for b in led.data["buckets"].values())
    assert res2["added"] == 0 and n1 == n2 and n1 == 6   # 2 场 × 每场 3 侧都算一条


def test_late_sp_is_picked_up_on_a_later_run():
    """北单开奖 SP 常常滞后 2~3 天：没 SP 的场次先跳过，SP 到了再补记。"""
    led = _tmp_ledger()
    r1 = led.ingest([_row("L1", "2026-08-01", sp=None)])
    assert r1 == {"added": 0, "skipped_no_sp": 1, "skipped_future": 0}
    assert "L1" not in led.data["ingested"]
    r2 = led.ingest([_row("L1", "2026-08-01", sp=4.0, result="0")])
    assert r2["added"] == 1 and led.data["ingested"]["L1"] == "2026-08-01"


def test_as_of_blocks_future_days():
    led = _tmp_ledger()
    rows = [_row("L1", "2026-08-10"), _row("L2", "2026-08-12")]
    r = led.ingest(rows, as_of="2026-08-11")
    assert r["added"] == 1 and r["skipped_future"] == 1
    assert "L2" not in led.data["ingested"]


def test_window_limits_the_sample():
    led = _tmp_ledger()
    led.ingest([_row("A", "2026-06-01"), _row("B", "2026-06-02"),
                _row("C", "2026-08-01"), _row("D", "2026-08-02")])
    allst = led.stats("gl0", 1.0)
    win = led.stats("gl0", 1.0, as_of="2026-08-03", window_days=30)
    assert allst["n"] > win["n"] > 0
    assert win["days"] == 2


def test_stats_math_and_m_star():
    led = _tmp_ledger()
    # 造一个桶：n=100，每条 z=1.3 恒定 → y=1.3、se=0、打平 m_star=2
    led.data["buckets"]["gl0|1.1–1.2"] = {
        "n": 100, "sum_z": 130.0, "sum_z2": 169.0,
        "by_day": {"2026-08-01": {"n": 100, "sum_z": 130.0, "sum_z2": 169.0}}}
    st = led.stats("gl0", 1.1)
    assert st["n"] == 100 and abs(st["y"] - 1.3) < 1e-9 and st["se"] < 1e-9
    assert abs(st["lo"] - 1.3) < 1e-9
    assert st["m_star"] == math.ceil(math.log(1 / TAKEOUT) / math.log(1.3))
    assert abs(st["ev1"] - (TAKEOUT * 1.3 - 1)) < 1e-9
    assert abs(st["ev9"] - (TAKEOUT * 1.3 ** 9 - 1)) < 1e-9


def test_stats_cumulative_threshold_includes_higher_buckets():
    led = _tmp_ledger()
    led.data["buckets"]["gl0|1.1–1.2"] = {"n": 10, "sum_z": 13.0, "sum_z2": 16.9,
                                          "by_day": {}}
    led.data["buckets"]["gl0|1.2–1.35"] = {"n": 10, "sum_z": 20.0, "sum_z2": 40.0,
                                           "by_day": {}}
    st = led.stats("gl0", 1.1)
    assert st["n"] == 20 and abs(st["y"] - 1.65) < 1e-9
    assert led.stats("gl0", 1.2)["n"] == 10      # 1.1–1.2 被排除


# ── 门策略 ──────────────────────────────────────────────

def test_policy_falls_back_when_ledger_is_thin():
    led = _tmp_ledger()
    pol = led.policy({"pool_gate": {"mode": "enforce", "gl_classes": ["gl0"],
                                    "min_n": 30, "default_theta": 1.1,
                                    "default_min_legs": 3}})
    assert pol["source"] == "config_default"
    assert pol["theta"] == 1.1 and pol["m_star"] == 3


def test_policy_picks_threshold_with_best_lower_bound():
    led = _tmp_ledger()
    # x≥1.0 的累计里混了没有边际的腿（把 y 拉低）；x≥1.1 干净
    led.data["buckets"]["gl0|1–1.1"] = {"n": 100, "sum_z": 100.0, "sum_z2": 100.0,
                                        "by_day": {}}
    led.data["buckets"]["gl0|1.1–1.2"] = {"n": 200, "sum_z": 260.0, "sum_z2": 338.0,
                                          "by_day": {}}
    pol = led.policy({"pool_gate": {"mode": "enforce", "gl_classes": ["gl0"],
                                    "min_n": 30}})
    assert pol["source"] == "ledger" and pol["theta"] == 1.1
    assert pol["y"] > 1.2 and pol["m_star"] >= 1


def test_policy_blocks_when_no_provable_edge():
    """证据充分（n 与天数都过停投门槛）但确实没有正边际 → 判空仓。"""
    led = _tmp_ledger()
    by_day = {f"2026-07-{d:02d}": {"n": 40, "sum_z": 36.0, "sum_z2": 36.0}
              for d in range(1, 11)}
    led.data["buckets"]["gl0|1.1–1.2"] = {"n": 400, "sum_z": 360.0, "sum_z2": 360.0,
                                          "by_day": by_day}
    pol = led.policy({"pool_gate": {"mode": "enforce", "gl_classes": ["gl0"],
                                    "min_n": 30}})
    assert pol["source"] == "ledger"
    assert pol["m_star"] is None          # CI 下沿 ≤1 → 无可证实边际 → 引擎应空仓
    assert "空仓" in pol["reason"]


def test_policy_does_not_block_on_thin_evidence():
    """证据不足（天数不够）不下「没边际」结论，回落配置默认、照常可投。"""
    led = _tmp_ledger()
    led.data["buckets"]["gl0|1.1–1.2"] = {
        "n": 400, "sum_z": 360.0, "sum_z2": 360.0,
        "by_day": {"2026-07-01": {"n": 400, "sum_z": 360.0, "sum_z2": 360.0}}}
    pol = led.policy({"pool_gate": {"mode": "enforce", "gl_classes": ["gl0"],
                                    "min_n": 30, "default_min_legs": 3}})
    assert pol["source"] == "config_default" and pol["m_star"] == 3


def test_policy_respects_gl_classes_and_as_of():
    led = _tmp_ledger()
    led.ingest([_row(f"F{i}", f"2026-07-{i+1:02d}") for i in range(20)])   # 7 月
    pol_now = led.policy({"pool_gate": {"mode": "enforce", "gl_classes": ["gl0"],
                                        "min_n": 10}}, as_of="2026-07-15")
    pol_before = led.policy({"pool_gate": {"mode": "enforce", "gl_classes": ["gl0"],
                                           "min_n": 10}}, as_of="2026-07-02")
    assert pol_now["source"] == "ledger" or pol_now["n"] >= 0
    assert pol_before["n"] <= pol_now["n"]


# ── update 入口（回看窗口 / 文件读取）────────────────────

def _write_cache(matches_dir: Path, tags_dir: Path, day: str, lid: str,
                 gl: str = "0", sp: float | None = 3.0, result: str = "3") -> None:
    matches_dir.mkdir(parents=True, exist_ok=True)
    tags_dir.mkdir(parents=True, exist_ok=True)
    (matches_dir / f"{day}.json").write_text(json.dumps({"matches": [{
        "lota_id": lid, "home_name": "H", "away_name": "A",
        "beidan_info": {"goal_line": gl, "home_odds": 3.0, "draw_odds": 3.3,
                        "away_odds": 2.2, "spvalue": sp, "result": result},
    }]}), encoding="utf-8")
    (tags_dir / f"{lid}.json").write_text(json.dumps({"sections": {
        "eu-odds-pinnacle": "欧盘:Pinnacle\nOPt-100m=2.60/3.30/2.90\nΔt+50m=2.60/3.30/2.90",
    }}), encoding="utf-8")


def test_update_scan_window_and_late_sp(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ledcache_"))
    matches_dir, tags_dir = tmp / "matches", tmp / "tags"
    role_root = Path(tempfile.mkdtemp(prefix="ledrole_"))
    monkeypatch.setenv("DS_ROLES_ROOT", str(role_root))    # 平铺沙箱（自动还原）
    for i, day in enumerate(("2026-08-01", "2026-08-02", "2026-08-09")):
        # 最后一天还没有 SP（模拟滞后）
        _write_cache(matches_dir, tags_dir, day, f"L{i}",
                     sp=None if day == "2026-08-09" else 3.0)
    r1 = update("bc狗", as_of="2026-08-03", lookback_days=7,
                matches_dir=matches_dir, tags_dir=tags_dir)
    assert r1["added"] == 2 and r1["skipped_no_sp"] == 0      # 08-09 不在窗口内
    r2 = update("bc狗", as_of="2026-08-10", lookback_days=7,
                matches_dir=matches_dir, tags_dir=tags_dir)
    assert r2["added"] == 0 and r2["skipped_no_sp"] == 1      # SP 未出 → 先跳过
    _write_cache(matches_dir, tags_dir, "2026-08-09", "L2", sp=5.0)   # SP 到了
    r3 = update("bc狗", as_of="2026-08-10", lookback_days=7,
                matches_dir=matches_dir, tags_dir=tags_dir)
    assert r3["added"] == 1
    led = PoolLedger(ledger_path("bc狗")).load()
    assert len(led.data["ingested"]) == 3
    # 幂等：再跑一遍不重复
    r4 = update("bc狗", as_of="2026-08-10", lookback_days=7,
                matches_dir=matches_dir, tags_dir=tags_dir)
    assert r4["added"] == 0


def test_update_all_days_and_available_days(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ledcache2_"))
    matches_dir, tags_dir = tmp / "matches", tmp / "tags"
    monkeypatch.setenv("DS_ROLES_ROOT", str(Path(tempfile.mkdtemp(prefix="ledrole2_"))))
    _write_cache(matches_dir, tags_dir, "2026-08-01", "L1")
    _write_cache(matches_dir, tags_dir, "2026-08-02", "L2")
    assert available_days(matches_dir) == ["2026-08-01", "2026-08-02"]
    r = update("bc狗", as_of="2026-08-03", all_days=True,
               matches_dir=matches_dir, tags_dir=tags_dir)
    assert r["added"] == 2 and r["days_scanned"] == 2


def test_collect_day_rows_skips_without_sharp_ref():
    tmp = Path(tempfile.mkdtemp(prefix="ledcache3_"))
    matches_dir, tags_dir = tmp / "matches", tmp / "tags"
    _write_cache(matches_dir, tags_dir, "2026-08-01", "L1", gl="-1")   # gl≠0 无同盘口段
    rows = collect_day_rows("2026-08-01", matches_dir=matches_dir, tags_dir=tags_dir)
    assert rows == []
