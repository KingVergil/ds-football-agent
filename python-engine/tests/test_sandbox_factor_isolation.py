"""因子定义目录口径：store 与 factor_induction 必须一致（沙箱隔离回归测试）。

背景（2026-09-11 实测）：bcl狗 7.11 沙箱 loop 跑完，反思新发现的 2 个因子定义被写进了
**线上全局** `data/factors/`，而因子记忆 `factor_memory.json` 在沙箱里 —— 原因是
`src/store.py::FACTORS_DIR` 写死 `data/factors`，只有 `src/factor_induction.py` 认
`DS_FACTORS_ROOT`。沙箱回放因此漏写线上（破坏「沙箱零线上影响」的红线）。

这里钉住两条：
1. `DS_FACTORS_ROOT` 存在 → store 与 factor_induction 都写该目录（沙箱）；
2. 不存在（线上单狗正常跑）→ 两者都回落 `data/factors`，行为与改造前逐字节一致。
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _resolve_dirs(factors_root: str | None) -> tuple[str, str]:
    """在子进程里取两处 FACTORS_DIR（避免污染当前进程已 import 的模块）。"""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import src.store as st, src.factor_induction as fi\n"
        "print(st.FACTORS_DIR); print(fi.FACTORS_DIR)\n" % str(ROOT)
    )
    env = dict(os.environ)
    env.pop("DS_FACTORS_ROOT", None)
    if factors_root is not None:
        env["DS_FACTORS_ROOT"] = factors_root
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=str(ROOT), check=True).stdout.strip().splitlines()
    return out[0], out[1]


def test_store_and_induction_share_factors_root_in_sandbox(tmp_path):
    """沙箱模式：两处都指向 DS_FACTORS_ROOT（否则因子定义会漏回线上）。"""
    sandbox = str(tmp_path / "factors")
    store_dir, induction_dir = _resolve_dirs(sandbox)
    assert store_dir == sandbox, f"store 未认 DS_FACTORS_ROOT: {store_dir}"
    assert induction_dir == sandbox, f"factor_induction 未认 DS_FACTORS_ROOT: {induction_dir}"
    assert store_dir == induction_dir, "两处口径不一致（沙箱隔离会被破坏）"


def test_online_default_unchanged_without_env():
    """线上（无 DS_FACTORS_ROOT）：两处都回落 data/factors，与改造前一致。"""
    store_dir, induction_dir = _resolve_dirs(None)
    assert store_dir == induction_dir
    assert store_dir.endswith("/python-engine/data/factors"), store_dir
    assert "DS_FACTORS_ROOT" not in os.environ


def test_factor_roundtrip_lands_in_sandbox_root(tmp_path, monkeypatch):
    """落盘验证：沙箱模式下 save_factor 写到沙箱，不碰线上目录。"""
    sandbox = tmp_path / "factors"
    monkeypatch.setenv("DS_FACTORS_ROOT", str(sandbox))
    sys.path.insert(0, str(ROOT))
    import src.store as store
    importlib.reload(store)
    try:
        from src.models import Factor

        fac = Factor(id="fac_测试沙箱隔离", slugs=["discrete-odds"], content="仅测试用")
        store.save_factor(fac)
        saved = sandbox / "fac_测试沙箱隔离.json"
        assert saved.exists(), f"未写入沙箱: {list(sandbox.glob('*.json'))}"
        online = ROOT / "data" / "factors" / "fac_测试沙箱隔离.json"
        assert not online.exists(), f"泄漏到线上全局: {online}"
    finally:
        monkeypatch.delenv("DS_FACTORS_ROOT", raising=False)
        importlib.reload(store)
        online = ROOT / "data" / "factors" / "fac_测试沙箱隔离.json"
        if online.exists():
            online.unlink()
