"""腿级线口径守卫（2026-09-15 用户口径）。

官方口径（北京体彩网·帮助中心）：`过关中奖奖金 = 2元 × SP1×SP2×…×SPn × 65%`
—— **0.65 只在整票结算时收一次，与串长无关**。于是：

    整票打平：  Π(SP·p̂) > 1/0.65            = 1.5385
    4 关票每腿：SP·p̂     > (1/0.65)^(1/4)    = 1.11371   ← **腿级门**

`1/0.65 = 1.5385` 只是「1 串 1（单关）」的腿级线。把它当 4 关票的腿级门，
标准被抬高 `1.5385/1.11371 − 1 ≈ 38%` —— 反思样本几乎为空、因子学不到错价。

本文件把三条钉死：
  1. `beidan_high_vol.high_vol_threshold()` 给的是**每腿线**，严格小于单关线；
  2. 反思 prompt 的波动 v2 段写每腿线，且**不再出现**旧文案 `0.65 × 开奖SP × p̂ > 1`；
  3. 出票线表 `_breakeven_table()` 的「每腿最低 v̂」与每腿线同源。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TAKEOUT = 1.0 / 0.65                 # 1.53846...
LEG4 = (1.0 / 0.65) ** 0.25          # 1.11371...


# ── ① 工具层：每腿线 ───────────────────────────────────────────

def test_high_vol_threshold_is_per_leg_line():
    """默认阈值 = `(1/0.65)^(1/4)`，且**严格小于**单关线。"""
    from src.beidan_high_vol import (
        HIGH_VOL_GAP_LEGS, HIGH_VOL_RATIO_THRESHOLD, high_vol_threshold,
    )
    assert HIGH_VOL_GAP_LEGS == 4
    assert high_vol_threshold(4) == pytest.approx(LEG4, abs=1e-9)
    assert HIGH_VOL_RATIO_THRESHOLD == pytest.approx(LEG4, abs=1e-9)
    assert HIGH_VOL_RATIO_THRESHOLD < TAKEOUT, "腿级门必须低于单关线"
    # 抬高标准 ≈ 38%（就是旧口径导致反思样本为空的量级）
    assert TAKEOUT / LEG4 - 1 == pytest.approx(0.3813, abs=0.002)


def test_per_leg_line_is_monotone_in_legs():
    """关数越多，每腿要求越低（`^1/M`）；`^1/1` 才等于单关线。"""
    from src.beidan_high_vol import high_vol_threshold
    assert high_vol_threshold(1) == pytest.approx(TAKEOUT, abs=1e-9)
    vals = [high_vol_threshold(n) for n in (2, 3, 4, 5, 9)]
    assert vals == sorted(vals, reverse=True), f"应随关数递减: {vals}"
    assert high_vol_threshold(5) < high_vol_threshold(4)


# ── ② prompt 层：反思段必须写每腿线 ─────────────────────────────

STALE_CRITERION = "0.65 × 开奖SP × p̂ > 1"


def _reflect_prompt_in_subprocess(parlay_emphasis: bool) -> str:
    code = (
        "import sys, json; sys.path.insert(0, %r);\n"
        "from src.data_manager import DataManager;\n"
        "from src.role import Role;\n"
        "from src.agent import run_reflect;\n"
        "class P:\n"
        "    def __init__(self): self.prompt = ''\n"
        "    def call(self, prompt, messages, **kw):\n"
        "        self.prompt = prompt\n"
        "        return json.dumps({'alpha_factors': [], 'reflection': 'stub',\n"
        "                           'per_match': {}, 'factor_desc': {},\n"
        "                           'factor_attribution': {}, 'money_lesson': ''})\n"
        "p = P(); dm = DataManager();\n"
        "dm.get_sections = lambda lid, slugs=None: '(占位数据)'\n"
        "settled = [{'lota_id': 'Lota0000001', 'hit': True, 'bet_type': '北单串关',\n"
        "            'pick': 'H', 'odds': 2.0, 'bet_size': 2, 'profit': 1.0, 'reason': 'r'}];\n"
        "run_reflect(settled, '2026-06-30', Role('梭哈2狗'), provider=p, dm=dm,\n"
        "            extra_matches=False, save_fac=False, persist=False,\n"
        "            parlay_emphasis=%r);\n"
        "print('<<<PROMPT>>>' + p.prompt)"
        % (str(ROOT), bool(parlay_emphasis))
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=str(ROOT), timeout=180)
    assert r.returncode == 0, f"子进程跑 run_reflect 失败: {r.stderr[-600:]}"
    assert "<<<PROMPT>>>" in r.stdout, f"没拿到 prompt: {r.stdout[-300:]}"
    return r.stdout.split("<<<PROMPT>>>", 1)[1]


def test_parlay_reflect_prompt_states_per_leg_line():
    """波动 v2 段必须给出每腿线 1.11371，而不是单关线口径的错价判据。"""
    prompt = _reflect_prompt_in_subprocess(True)
    assert "波动 v2" in prompt, "应注入波动 v2 反思段"
    assert "1.11371" in prompt, "必须写明每腿线 (1/0.65)^(1/4)=1.11371"
    assert STALE_CRITERION not in prompt, (
        f"旧单关线文案必须清除（它把腿级门抬高 38%）：{STALE_CRITERION}"
    )
    assert "1.5385" in prompt, "整票线仍应保留（用作对照，不是腿级门）"


def test_non_parlay_reflect_prompt_has_no_vol_block():
    """不打开 parlay_emphasis 时不得注入该段（单狗 prompt 不受影响）。"""
    prompt = _reflect_prompt_in_subprocess(False)
    assert "波动 v2" not in prompt


# ── ③ 出票线表：与每腿线同源 ───────────────────────────────────

def test_breakeven_table_row_is_per_leg_line():
    from src.beidan_parlay_dog import BeidanParlayDog
    table = BeidanParlayDog._breakeven_table()
    lines = [ln for ln in table.splitlines() if ln.startswith("| 每腿最低 v̂")]
    assert lines, f"出票线表应含每腿行: {table}"
    vals = [float(v) for v in lines[0].split("|")[2:-1]]
    assert vals[0] == pytest.approx((1 / 0.65) ** (1 / 2), abs=1e-3)
    assert vals[2] == pytest.approx(LEG4, abs=1e-3), "4 关列的每腿线应是 1.114"
    assert all(v < TAKEOUT for v in vals[1:]), "2 关及以上的每腿线都低于单关线"
