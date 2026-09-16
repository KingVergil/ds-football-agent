"""单狗红线守卫：串关改造（票型容错 / 退役口径）不得影响任何单狗。

规则（2026-09-13）
────────────────
* **票型容错** 只在串关狗的 `parlay.json` 读（`ticket_tolerance`）；单狗走 Agent 链路，
  根本不读该键 → 行为不变。
* **退役口径**（无倾向 |ROI| ≤ eps 即退）只在 `parlay.json` 里显式写了 `retire` 段的
  角色启用；其它角色（含全部单狗）保持改造前的双条件（|ROI| < 0.15 且 命中率 0.35~0.65）。
* 本测试把这两条**钉成可执行的断言**，防止以后有人把阈值写进共用代码路径。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROLES = ROOT / "data" / "roles"

# 单狗样本（无 parlay.json，必须完全不受影响）
SINGLE_DOGS = ["梭哈2狗", "平局狗", "跟风狗"]


def _cfg(dog: str) -> dict:
    p = ROLES / dog / "parlay.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_single_dogs_have_no_parlay_config():
    """单狗目录里不存在 parlay.json → 不会被串关逻辑读到任何新键。"""
    for dog in SINGLE_DOGS:
        assert not (ROLES / dog / "parlay.json").exists(), f"{dog} 不应有 parlay.json"


def test_ticket_tolerance_only_on_parlay_dogs():
    """ticket_tolerance 只出现在串关狗配置里。"""
    for dog in SINGLE_DOGS:
        assert "ticket_tolerance" not in _cfg(dog)
    for dog in ("bc狗", "bcl狗"):
        if (ROLES / dog).exists():
            assert int(_cfg(dog).get("ticket_tolerance") or 0) >= 0


def test_retire_rule_opt_in_only():
    """新退役口径必须由配置显式开启；未开启的角色走改造前阈值。"""
    for dog in SINGLE_DOGS:
        assert "retire" not in _cfg(dog), f"{dog} 不得启用新退役口径"
    for dog in ("bc狗", "bcl狗"):
        if (ROLES / dog).exists():
            r = _cfg(dog).get("retire") or {}
            assert float(r.get("eps_flat", 0)) == 0.20, f"{dog} 应显式配置 retire.eps_flat"


def test_legacy_defaults_unchanged():
    """改造前的单狗默认值必须原样保留在代码里（回归护栏）。"""
    src = (ROOT / "src" / "agent.py").read_text(encoding="utf-8")
    # 单狗分支（else）必须仍用 0.15 / 0.35~0.65
    assert "legacy_eps_flat" in src and "0.15" in src
    assert "legacy_hit_band" in src and "0.35, 0.65" in src
    # 新口径必须在"配置开启"分支内
    assert "_parlay_retire" in src
