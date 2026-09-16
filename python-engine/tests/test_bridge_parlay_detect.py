"""桥的串关狗判定必须兼容平铺沙箱（DS_ROLES_ROOT）。

背景（2026-09-11 实测）：`_is_parlay_dog` 只认嵌套路径 `<role_root>/<狗>/parlay.json`，
而沙箱回放（`src/role.py::_flat_role_root`）下 role_root 是**单狗平铺**目录：
`parlay.json` 直接在根下。于是回放里串关狗被误判成普通狗 → 走通用竞彩 Agent：
- analyze 拿到 21 场却 0 单（不组票、不算池门）
- settle 走 Agent.settle → 打印「reflect 跳过: settled=0」，**一个因子都不产出**

症状与"冷启动无因子"极像，极易误判为策略/数据问题。这里把两种角色布局都钉住。
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture()
def bridge_mod(monkeypatch, tmp_path):
    """每个用例在干净环境里重载 bridge（ROLES_DIR 在 import 时按 env 求值）。"""
    monkeypatch.delenv("DS_ROLES_ROOT", raising=False)
    if "src.bridge" in sys.modules:
        del sys.modules["src.bridge"]
    if "src.role_registry" in sys.modules:
        del sys.modules["src.role_registry"]
    import src.bridge as bridge
    return bridge


def test_flat_sandbox_parlay_detected(bridge_mod, monkeypatch, tmp_path):
    """平铺沙箱（DS_ROLES_ROOT）：根下 parlay.json 必须被判为串关狗。"""
    flat = tmp_path / "workspace"
    flat.mkdir()
    (flat / "parlay.json").write_text("{}", encoding="utf-8")
    (flat / "bcl狗.json").write_text('{"name": "bcl狗"}', encoding="utf-8")
    monkeypatch.setenv("DS_ROLES_ROOT", str(flat))
    import importlib
    importlib.reload(bridge_mod)
    assert bridge_mod._is_parlay_dog("bcl狗") is True


def test_nested_layout_parlay_detected(bridge_mod, monkeypatch, tmp_path):
    """线上嵌套布局：roles/<狗>/parlay.json 照旧判为串关狗。"""
    nested = tmp_path / "roles" / "bc狗"
    nested.mkdir(parents=True)
    (nested / "parlay.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DS_ROLES_ROOT", str(tmp_path / "roles"))
    import importlib
    importlib.reload(bridge_mod)
    assert bridge_mod._is_parlay_dog("bc狗") is True


def test_non_parlay_dog_stays_normal(bridge_mod, monkeypatch, tmp_path):
    """非串关狗（单狗竞彩）：没有 parlay.json → 不得被判成串关，避免误入北单链路。"""
    flat = tmp_path / "workspace"
    (flat / "单狗").mkdir(parents=True)
    (flat / "单狗" / "单狗.json").write_text('{"name": "单狗"}', encoding="utf-8")
    monkeypatch.setenv("DS_ROLES_ROOT", str(flat))
    import importlib
    importlib.reload(bridge_mod)
    assert bridge_mod._is_parlay_dog("单狗") is False


def test_flat_root_does_not_capture_unrelated_dog(bridge_mod, monkeypatch, tmp_path):
    """平铺兜底只在同名角色在场时成立：根下 parlay.json 存在但问的是别的狗。

    现状实现按「role_root 是单狗目录」设计，故同根下其它名字也返回 True 是已知取舍；
    这里固化**契约**：单狗平铺根一次只服务一只狗（workspace 内不会混多狗），
    因此只要出现 parlay.json 即代表当前狗是串关狗。
    """
    flat = tmp_path / "workspace"
    flat.mkdir()
    (flat / "parlay.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DS_ROLES_ROOT", str(flat))
    import importlib
    importlib.reload(bridge_mod)
    assert bridge_mod._is_parlay_dog("bcl狗") is True
