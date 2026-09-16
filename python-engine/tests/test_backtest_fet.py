"""
回测取数（fet_txt 时间切片源）测试。

全部用临时目录造切片，不依赖本地 deepseek_lota 数据，也不触网。
覆盖：gap→档位映射、波次表、档位回退方向（只回退更旧档）、
回测模式接管 DataManager 的 compact-fet / tags 读取。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DS_ROLES_ROOT", tempfile.mkdtemp(prefix="btfet_roles_"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import backtest_fet as bf
from src import data_manager as dm_mod

_HEAD = "⏰时间: {kickoff}｜星期六\n"
_BODY = (
    "离散指数 t=Δt±m odds=h/d/a\n{marker}Δt+60m→↑↑↑1.75/3.54/5.43\n\n"
    "亚盘:Pinnacle t=Δt±m odds=h/handicap/a/r(rrr%)\n"
    "Δt+14m↑→↓↓0.82/一球/1.11/97.72\n"
)


def _write_slice(root: Path, stage: str, lota_id: str, kickoff: str,
                 marker: str = "") -> None:
    d = root / stage
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lota_id}.txt").write_text(
        _HEAD.format(kickoff=kickoff) + _BODY.format(marker=marker), encoding="utf-8"
    )


def _mk_root(tmp: Path, kicks: dict[str, dict]) -> Path:
    """kicks: {lota_id: {"kickoff": "...", "stages": [...]}}"""
    root = tmp / "fet_txt"
    matches = {}
    for lid, cfg in kicks.items():
        for st in cfg["stages"]:
            _write_slice(root, st, lid, cfg["kickoff"],
                         (cfg.get("markers") or {}).get(st, ""))
        matches[lid] = {"kickoff": cfg["kickoff"], "stages": list(cfg["stages"])}
    root.mkdir(parents=True, exist_ok=True)
    (root / bf.INDEX_NAME).write_text(
        json.dumps({"matches": matches}, ensure_ascii=False), encoding="utf-8"
    )
    return root


# ═══════════════════════════════════════════
# 纯函数：gap → 档位 / 波次表
# ═══════════════════════════════════════════

def test_stage_for_gap_boundaries():
    assert bf.stage_for_gap(0.1) == "pass_6_hours"
    assert bf.stage_for_gap(6.0) == "pass_6_hours"
    assert bf.stage_for_gap(6.01) == "pass_12_hours"
    assert bf.stage_for_gap(12.0) == "pass_12_hours"
    assert bf.stage_for_gap(12.01) == "pass_1_day"
    assert bf.stage_for_gap(24.0) == "pass_1_day"
    assert bf.stage_for_gap(24.01) is None
    assert bf.stage_for_gap(0.0) is None      # 该波访问时已开赛
    assert bf.stage_for_gap(-3.0) is None


def test_wave_schedule_weekday_and_weekend():
    root = _mk_root(Path(tempfile.mkdtemp(prefix="btfet_wave_")), {})
    src = bf.BacktestFetSource(root)
    # 2026-08-19 周三 / 2026-08-21 周五 → 22:30 一波
    assert [w.strftime("%H:%M") for w in src.waves("2026-08-19")] == ["22:30"]
    assert [w.strftime("%H:%M") for w in src.waves("2026-08-21")] == ["22:30"]
    # 2026-08-22 周六 / 2026-08-23 周日 → 16:30 + 20:30 两波
    assert [w.strftime("%H:%M") for w in src.waves("2026-08-22")] == ["16:30", "20:30"]
    assert [w.strftime("%H:%M") for w in src.waves("2026-08-23")] == ["16:30", "20:30"]
    # 波次可覆盖（环境变量由 enable() 读取，这里直接构造）
    src2 = bf.BacktestFetSource(root, waves_weekday=("21:00", "23:30"))
    assert [w.strftime("%H:%M") for w in src2.waves("2026-08-19")] == ["21:00", "23:30"]


def test_football_day_of():
    assert bf.football_day_of(datetime(2026, 8, 22, 17, 0)).isoformat() == "2026-08-22"
    assert bf.football_day_of(datetime(2026, 8, 23, 3, 0)).isoformat() == "2026-08-22"
    assert bf.football_day_of(datetime(2026, 8, 23, 11, 59)).isoformat() == "2026-08-22"
    assert bf.football_day_of(datetime(2026, 8, 23, 12, 1)).isoformat() == "2026-08-23"


# ═══════════════════════════════════════════
# 档位解析
# ═══════════════════════════════════════════

def test_resolve_matches_dog_gap_example():
    """用户口径：17:00/16:30 波次里 A(20:30 开赛)→pass_6_hours，B(次日 03:00)→pass_12_hours。"""
    tmp = Path(tempfile.mkdtemp(prefix="btfet_gap_"))
    root = _mk_root(tmp, {
        "LotaA": {"kickoff": "2026-08-22 20:30:00",
                  "stages": ["pass_6_hours", "pass_12_hours", "pass_1_day"]},
        "LotaB": {"kickoff": "2026-08-23 03:00:00",
                  "stages": ["pass_6_hours", "pass_12_hours", "pass_1_day"]},
        "LotaC": {"kickoff": "2026-08-22 16:00:00",
                  "stages": ["pass_6_hours", "pass_12_hours", "pass_1_day"]},
    })
    src = bf.BacktestFetSource(root)
    at = datetime(2026, 8, 22, 16, 30)
    a = src.resolve("LotaA", access_time=at)
    b = src.resolve("LotaB", access_time=at)
    assert (a.stage, a.used_stage, a.fallback) == ("pass_6_hours", "pass_6_hours", False)
    assert (b.stage, b.used_stage) == ("pass_12_hours", "pass_12_hours")
    assert round(b.gap_hours, 1) == 10.5
    # 访问时刻已开赛 → 无数据（实盘该波也不会拿它选腿）
    assert src.resolve("LotaC", access_time=at) is None


def test_resolve_falls_back_only_to_older_stage():
    """档位缺文件 → 只回退更旧档；绝不回退更新档（更新档=前视）。"""
    tmp = Path(tempfile.mkdtemp(prefix="btfet_fb_"))
    root = _mk_root(tmp, {
        # gap=0.5h 要 pass_6_hours，但只有更旧的 pass_12_hours
        "LotaOld": {"kickoff": "2026-08-22 17:00:00", "stages": ["pass_12_hours"]},
        # gap=13h 要 pass_1_day，但只有更新的 pass_6_hours → 必须判无数据
        "LotaNew": {"kickoff": "2026-08-23 05:30:00", "stages": ["pass_6_hours"]},
    })
    src = bf.BacktestFetSource(root)
    at = datetime(2026, 8, 22, 16, 30)
    old = src.resolve("LotaOld", access_time=at)
    assert old is not None and old.used_stage == "pass_12_hours" and old.fallback is True
    assert old.stage == "pass_6_hours"
    assert src.resolve("LotaNew", access_time=at) is None

    # 显式允许时才回退更新档（默认关闭）
    src2 = bf.BacktestFetSource(root, allow_newer_fallback=True)
    newer = src2.resolve("LotaNew", access_time=at)
    assert newer is not None and newer.used_stage == "pass_6_hours"


def test_out_of_scope_returns_none():
    tmp = Path(tempfile.mkdtemp(prefix="btfet_scope_"))
    root = _mk_root(tmp, {"LotaA": {"kickoff": "2026-08-22 20:30:00",
                                    "stages": ["pass_6_hours"]}})
    src = bf.BacktestFetSource(root)
    assert src.in_scope("LotaA") is True
    assert src.in_scope("LotaZ") is False
    assert src.resolve("LotaZ", access_time=datetime(2026, 8, 22, 16, 30)) is None
    assert src.stats["out_of_scope"] == 1


def test_compact_fet_payload_and_sections():
    tmp = Path(tempfile.mkdtemp(prefix="btfet_payload_"))
    root = _mk_root(tmp, {"LotaA": {"kickoff": "2026-08-22 20:30:00",
                                    "stages": ["pass_6_hours"]}})
    src = bf.BacktestFetSource(root)
    at = datetime(2026, 8, 22, 16, 30)
    payload = src.compact_fet("LotaA", access_time=at)
    assert payload["lota_id"] == "LotaA"
    assert "⏰时间: 2026-08-22 20:30:00" in payload["compact_fet"]
    assert payload["data"]["compact_fet"] == payload["compact_fet"]
    assert payload["_backtest_fet"]["used_stage"] == "pass_6_hours"
    secs = src.sections("LotaA", access_time=at)
    assert "discrete-odds" in secs and "asian-handicap-pinnacle" in secs


# ═══════════════════════════════════════════
# DataManager 接管
# ═══════════════════════════════════════════

def _enable_tmp_source(tmp_path: Path, monkeypatch) -> bf.BacktestFetSource:
    root = _mk_root(tmp_path, {
        "LotaSlice": {"kickoff": "2026-08-22 20:30:00",
                      "stages": ["pass_6_hours"]},
    })
    src = bf.enable(root)
    assert src is not None and src.available
    monkeypatch.setattr(dm_mod, "FEATURES_DIR", tmp_path / "features")
    monkeypatch.setattr(dm_mod, "TAGS_DIR", tmp_path / "tags")
    (tmp_path / "features").mkdir(exist_ok=True)
    (tmp_path / "tags").mkdir(exist_ok=True)
    return src


def test_data_manager_reads_slice_for_in_scope(monkeypatch, tmp_path):
    src = _enable_tmp_source(tmp_path, monkeypatch)
    try:
        bf.set_access_time(datetime(2026, 8, 22, 16, 30))
        dm = dm_mod.DataManager()
        # 线上 features 缓存里有同一场的“实时”数据 → 回测必须无视它
        (tmp_path / "features" / "LotaSlice.json").write_text(
            json.dumps({"lota_id": "LotaSlice", "compact_fet": "线上实时数据"}), encoding="utf-8")
        got = dm.get_cached_compact_fet("LotaSlice")
        assert got["compact_fet"] != "线上实时数据"
        assert got["_backtest_fet"]["used_stage"] == "pass_6_hours"
        # tags 也走切片、且不落盘
        secs = dm.get_sections("LotaSlice", ["discrete-odds"])
        assert "离散指数" in secs
        assert not (tmp_path / "tags" / "LotaSlice.json").exists()
        assert dm.has_usable_compact_fet("LotaSlice") is True
        # 索引内但该访问时刻已开赛（20:30 波次）→ 无数据
        bf.set_access_time(datetime(2026, 8, 22, 21, 30))
        assert dm.get_cached_compact_fet("LotaSlice") is None
        bf.set_access_time(datetime(2026, 8, 22, 20, 31))
        assert dm.get_cached_compact_fet("LotaSlice") is None
    finally:
        bf.set_access_time(None)
        bf.disable()


def test_data_manager_untouched_out_of_scope(monkeypatch, tmp_path):
    _enable_tmp_source(tmp_path, monkeypatch)
    try:
        dm = dm_mod.DataManager()
        (tmp_path / "features" / "LotaOther.json").write_text(
            json.dumps({"lota_id": "LotaOther", "compact_fet": "线上缓存"}), encoding="utf-8")
        got = dm.get_cached_compact_fet("LotaOther")
        assert got and got["compact_fet"] == "线上缓存"
    finally:
        bf.disable()


def test_auto_enable_requires_sandbox_env(monkeypatch):
    bf.disable()
    monkeypatch.delenv("DS_ROLES_ROOT", raising=False)
    monkeypatch.delenv("DS_BACKTEST_FET", raising=False)
    assert bf.active() is False
    monkeypatch.setenv("DS_BACKTEST_FET", "0")
    monkeypatch.setenv("DS_ROLES_ROOT", "/tmp/whatever")
    bf.disable()
    assert bf.active() is False


# ═══════════════════════════════════════════
# 端到端：回放逐波分析时，进 prompt 的数据来自对应档位切片
# ═══════════════════════════════════════════

class _FakeProvider:
    """记录 prompt 的假 LLM：stage1 推荐 H，stage2 给出 1 条单选。"""

    def __init__(self, lid: str):
        self.lid = lid
        self.prompts: list[str] = []

    def call(self, system, messages, **kw):
        text = system + "\n" + (messages[0]["content"] if messages else "")
        self.prompts.append(text)
        if "初筛器" in system:
            return json.dumps({"items": [{"lota_id": self.lid, "推荐": "H", "factors": []}]},
                              ensure_ascii=False)
        return json.dumps({"singles": [{"lota_id": self.lid, "pick": "H"}], "empty": False},
                          ensure_ascii=False)


def test_analyze_prompts_use_slice_per_wave(monkeypatch, tmp_path):
    """周末两波：16:30 波用 pass_12_hours、20:30 波用 pass_6_hours，且绝不出现线上缓存文本。"""

    from src.beidan_parlay_dog import BeidanParlayDog

    root = _mk_root(tmp_path, {
        "LotaSlice": {
            "kickoff": "2026-08-22 23:00:00",
            "stages": ["pass_6_hours", "pass_12_hours"],
            "markers": {"pass_6_hours": "[SLICE_6H]", "pass_12_hours": "[SLICE_12H]"},
        },
    })
    assert bf.enable(root) is not None
    monkeypatch.setattr(dm_mod, "FEATURES_DIR", tmp_path / "features")
    monkeypatch.setattr(dm_mod, "TAGS_DIR", tmp_path / "tags")
    (tmp_path / "features").mkdir(exist_ok=True)
    (tmp_path / "tags").mkdir(exist_ok=True)
    # 线上缓存里有同一场的实时数据 → 绝不能被读进 prompt
    (tmp_path / "features" / "LotaSlice.json").write_text(
        json.dumps({"lota_id": "LotaSlice", "compact_fet": "[LIVE_LOOKAHEAD] 亚盘:Pinnacle 终盘"}),
        encoding="utf-8")
    (tmp_path / "tags" / "LotaSlice.json").write_text(
        json.dumps({"lota_id": "LotaSlice", "sections": {"discrete-odds": "[LIVE_LOOKAHEAD]"}}),
        encoding="utf-8")

    fake = _FakeProvider("LotaSlice")
    match = {
        "lota_id": "LotaSlice", "home_name": "甲", "away_name": "乙",
        "league_name": "测试联赛", "match_time": "2026-08-22 23:00:00",
        "beidan_number": "1",
        "beidan_info": {"goal_line": 0, "home_odds": 2.0, "draw_odds": 3.0, "away_odds": 4.0},
    }
    try:
        dog = BeidanParlayDog(user="pytest_bcfet_slice")
        dog.set_provider(fake)
        dog._beidan_matches = lambda day, live=False, **kw: ([dict(match)], [])
        r = dog.analyze("2026-08-22", dry_run=True, use_llm=True)
        assert [w["at"] for w in r["waves"]] == ["2026-08-22 16:30", "2026-08-22 20:30"]
    finally:
        bf.set_access_time(None)
        bf.disable()

    blob = "\n".join(fake.prompts)
    assert fake.prompts, "未捕获到 LLM prompt"
    assert "[SLICE_12H]" in blob and "[SLICE_6H]" in blob
    assert "[LIVE_LOOKAHEAD]" not in blob
    assert "Δt+60m→↑↑↑1.75/3.54/5.43" in blob

