"""波次边界：正好在波次时刻开赛的场次不得进入该波（曾经因字符串比较被误放进来）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="wave_roles_"))
os.environ.setdefault("DS_SESSIONS_ROOT", tempfile.mkdtemp(prefix="wave_sessions_"))
os.environ["DS_BACKTEST_FET"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beidan_parlay_dog import BeidanParlayDog  # noqa: E402


def _m(lid: str, mt: str) -> dict:
    return {"lota_id": lid, "match_time": mt, "beidan_info": {"home_odds": 2.0}}


def test_match_kicking_off_exactly_at_wave_is_excluded():
    dog = BeidanParlayDog(user="wave_boundary")
    ms = [_m("A", "2026-08-15 20:30:00"),     # 正好 20:30 → 必须排除
          _m("B", "2026-08-15 20:29:59"),     # 已开赛 → 排除
          _m("C", "2026-08-15 20:31:00"),     # 晚 1 分钟 → 保留
          _m("D", "2026-08-15 22:00:00")]     # 更晚 → 保留
    kept = dog._filter_beidan_matches(ms, "2026-08-15 12:01", "2026-08-16 12:00",
                                      only_upcoming=True, as_of="2026-08-15 20:30")
    assert [m["lota_id"] for m in kept] == ["C", "D"]


def test_wave_boundary_at_every_common_wave():
    """粒度是**分钟**：同一分钟里 :00 / :59 都算"已开赛"，晚一分钟才算未开赛。"""
    from datetime import datetime, timedelta
    dog = BeidanParlayDog(user="wave_boundary2")
    for wave in ("16:30", "20:30", "22:30"):
        t = datetime.strptime(f"2026-08-15 {wave}", "%Y-%m-%d %H:%M")
        nxt = (t + timedelta(minutes=1)).strftime("%H:%M")
        ms = [_m("X", f"2026-08-15 {wave}:00"), _m("Y", f"2026-08-15 {wave}:59"),
              _m("Z", f"2026-08-15 {nxt}:00")]
        kept = dog._filter_beidan_matches(ms, "2026-08-15 12:01", "2026-08-16 12:00",
                                          only_upcoming=True,
                                          as_of=f"2026-08-15 {wave}")
        assert [m["lota_id"] for m in kept] == ["Z"], wave


def test_live_mode_still_excludes_started_matches():
    """live（as_of=None，用当前时间）同样不能放已开赛的场次进来。"""
    dog = BeidanParlayDog(user="wave_boundary3")
    ms = [_m("OLD", "2020-01-01 12:00:00"), _m("FUT", "2099-01-01 12:00:00")]
    kept = dog._filter_beidan_matches(ms, "2020-01-01 12:01", "2099-01-02 12:00",
                                      only_upcoming=True, as_of=None)
    assert [m["lota_id"] for m in kept] == ["FUT"]
