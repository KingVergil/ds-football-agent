"""人设分离 / 结算-反思解耦 / 反思输入补全 的单狗红线守卫。

工单（2026-09-13，用户 4 条）
───────────────────────────
1. 人设分离：下注人设 `persona.md` 与反思人设 `persona_reflect.md` 分开插入 prompt；
2. 结算与因子产生解耦：可"只结算"或"只产因子"；
3. 反思输入补 比分/goal_line（此前是 `比分:?` 且没有让球）；
4. **以上都不得影响单关狗**（2026-09-16 追加：单狗同样支持「只结算 / 只产因子」两个独立动作）。

本文件用「默认路径不读新文件 / 默认人设逐字节不变 / 非串关狗被显式拒绝」三条把第 4 条钉死。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 单狗（无 parlay.json）
SINGLE = "梭哈2狗"
# 串关狗（有 parlay.json）
PARLAY = "bcl狗"


def _sandbox(dog: str) -> Path:
    src = ROOT / "data" / "roles" / dog
    sb = Path(tempfile.mkdtemp(prefix=f"redline_{dog}_"))
    for it in src.iterdir():
        if it.name.startswith(("persona_reflect", "parlay.json.bak")):
            continue
        (shutil.copytree(it, sb / it.name) if it.is_dir() else shutil.copy2(it, sb / it.name))
    return sb


@pytest.fixture()
def sandbox(monkeypatch):
    """把 DS_ROLES_ROOT 指向单狗沙箱（复刻单狗运行环境）。

    ⚠️ `ROLES_DIR` 由 `src.role_registry` 在 **import 时**读 `DS_ROLES_ROOT` 求值。
    为避免污染同进程的其它测试（以及被其它测试污染），此处只设环境变量；
    真正需要读人设的用例改用 `_persona_in_subprocess()` 在**子进程**里跑。
    """
    sb = _sandbox(SINGLE)
    monkeypatch.setenv("DS_ROLES_ROOT", str(sb))
    monkeypatch.setenv("DS_FACTORS_ROOT", str(sb / "factors"))
    monkeypatch.setenv("DS_SESSIONS_ROOT", str(sb / "sessions"))
    return sb


def _persona_in_subprocess(sandbox: Path, dog: str, mode: str) -> str:
    """在干净子进程里取人设（模块级 ROLES_DIR 不会被上一个测试污染）。"""
    import subprocess
    code = (
        "import os, sys; sys.path.insert(0, %r);\n"
        "import importlib, src.role_registry as rr, src.role as rl;\n"
        "importlib.reload(rr); importlib.reload(rl);\n"
        "from src.role import Role;\n"
        "print(Role(%r).persona_text(mode=%r), end='')\n" % (str(ROOT), dog, mode)
    )
    env = dict(os.environ)
    env["DS_ROLES_ROOT"] = str(sandbox)
    env["DS_FACTORS_ROOT"] = str(sandbox / "factors")
    env["DS_SESSIONS_ROOT"] = str(sandbox / "sessions")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, f"子进程取人设失败: {r.stderr[-300:]}"
    return r.stdout


def test_default_persona_unchanged_without_reflect_file(sandbox):
    """① 没写 persona_reflect.md 时，reflect 人设 == 下注人设（旧行为不变）。"""
    bet = _persona_in_subprocess(sandbox, SINGLE, "bet")
    ref = _persona_in_subprocess(sandbox, SINGLE, "reflect")
    assert bet == ref, "缺 persona_reflect.md 时不得改变人设注入"
    assert bet.startswith("## 🎯 个人偏好")


def test_existing_persona_untouched_by_reflect_mode(sandbox):
    """① 反射模式**不得**去改 persona.md（只多读一个可选文件）。"""
    p = sandbox / "persona.md"
    before = p.read_text(encoding="utf-8")
    _persona_in_subprocess(sandbox, SINGLE, "reflect")
    assert p.read_text(encoding="utf-8") == before


def test_reflect_persona_only_when_file_exists(sandbox):
    """① 写了 persona_reflect.md 才生效，且只影响 reflect 模式。"""
    (sandbox / "persona_reflect.md").write_text("反思专用人设XYZ", encoding="utf-8")
    assert "反思专用人设XYZ" in _persona_in_subprocess(sandbox, SINGLE, "reflect")
    assert "反思专用人设XYZ" not in _persona_in_subprocess(sandbox, SINGLE, "bet")


def test_single_dog_has_no_reflect_persona_by_default():
    """① 线上单狗目录里不应存在 persona_reflect.md（改造不改单狗资产）。"""
    for dog in ("梭哈2狗", "平局狗", "跟风狗", "alpha狗"):
        assert not (ROOT / "data" / "roles" / dog / "persona_reflect.md").exists()


def test_reflect_func_supports_single_dog_with_key_guard(sandbox):
    """② 独立 reflect 入口：北单串关 + 单狗**都支持**（2026-09-16 用户口径）。

    守卫点变成「没有 API key 必须显式报错」——不允许静默无效果地"产因子"。
    """
    import subprocess
    code = (
        "import sys; sys.path.insert(0, %r);\n"
        "import importlib, src.role_registry as rr, src.role as rl, src.bridge as br;\n"
        "importlib.reload(rr); importlib.reload(rl); importlib.reload(br);\n"
        "try:\n"
        "    br._do_reflect({'dog': %r, 'day': '', 'opts': {}})\n"
        "    print('NO_RAISE')\n"
        "except br.BridgeError as e:\n"
        "    print('RAISED:' + str(e)[:60])\n" % (str(ROOT), SINGLE)
    )
    env = dict(os.environ)
    env["DS_ROLES_ROOT"] = str(sandbox)
    env["DS_FACTORS_ROOT"] = str(sandbox / "factors")
    env["DS_SESSIONS_ROOT"] = str(sandbox / "sessions")
    env.pop("DEEPSEEK_API_KEY", None)          # 没有 key ⇒ 必须显式报错
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, cwd=str(ROOT), timeout=120)
    assert r.stdout.startswith("RAISED:"), f"无 key 时必须显式报错，实际: {r.stdout!r} {r.stderr[-200:]}"
    assert "DEEPSEEK_API_KEY" in r.stdout, f"报错要指明缺 key，实际: {r.stdout!r}"
    assert "仅支持北单串关狗" not in r.stdout, "单狗不再被拒绝（2026-09-16 解耦）"


def test_single_dog_settle_and_reflect_are_decoupled():
    """① 单狗：settle 可只结算（reflect=False）/ 另有 reflect_only；桥与 UI 对称。"""
    agent_src = (ROOT / "src" / "agent.py").read_text(encoding="utf-8")
    assert 'def settle(self, day_date: str = None, jingcai_only: bool = False,' in agent_src
    assert 'reflect: bool = True' in agent_src
    assert '"reflect": bool(reflect)' in agent_src, "settle 要把 reflect 传进状态"
    assert 'def settle_only(self, day_date' in agent_src
    assert 'def reflect_only(self, day_date' in agent_src
    assert '"reflect" if s.get("reflect", True) else END' in agent_src, "settle 图要有条件边"
    br = (ROOT / "src" / "bridge.py").read_text(encoding="utf-8")
    assert 'stage == "settle"' in br and 'reflect=False' in br, "桥：stage=settle → 只结算"
    assert 'agent.reflect_only(day)' in br, "桥：单狗 stage=reflect / func=reflect → 只产因子"
    ui = (ROOT.parent / "harness-plugin" / "client.js").read_text(encoding="utf-8")
    assert 'func: "reflect"' not in ui, "UI 不得再发 func=\"reflect\"（不在桥白名单里）"
    assert 'opts: { stage: "reflect" }' in ui, "产因子按钮要走 settle+stage=reflect"
    assert 'opts: { stage: "settle" }' in ui, "结算按钮要只结算"


def test_settle_stage_default_is_both(monkeypatch):
    """② 桥的 stage 缺省 = both（结算+反思），保持改造前语义。"""
    from src.bridge import _do_settle
    captured = {}

    class FakeDog:
        def __init__(self, user=None):
            captured["dog"] = user

        def settle(self, day, reflect=True):
            captured["reflect"] = reflect
            return {"settled": 0}

        def settle_only(self, day):
            captured["stage"] = "settle_only"
            return {"settled": 0}

        def _get_capital(self):
            return 0

    import src.bridge as br
    monkeypatch.setattr(br, "_is_parlay_dog", lambda d: True)
    monkeypatch.setattr(br, "_ensure_dog", lambda d: None)
    monkeypatch.setattr(br, "_role_of", lambda d: type("R", (), {"stats": staticmethod(lambda: {})})())
    import src.beidan_parlay_dog as bpd
    monkeypatch.setattr(bpd, "BeidanParlayDog", FakeDog)
    _do_settle({"dog": PARLAY, "day": "2026-07-25", "opts": {}})
    assert captured.get("reflect") is True, "缺省必须是 reflect=True（改造前行为）"


def test_reflection_header_includes_goal_line():
    """③ 反思 header 带让球线的代码路径存在（已结算 + 补充样本共两处）。"""
    src = (ROOT / "src" / "agent.py").read_text(encoding="utf-8")
    assert src.count("让球0(平手)") >= 2


# ── ③ + ④ 真·行为红线：反思输入补 比分/让球线，且**默认关**（单狗 prompt 不变）──

# data/beidan/*.json 里真实带 goal_line 的北单比赛（rich=True 才回填）
RICH_LID = "Lota4469357"
RICH_SCORE = "1:0"
RICH_GL = -1.0


def _reflect_prompt_in_subprocess(rich: bool, system_rules: str = "",
                                  observation: bool = False) -> str:
    """在干净子进程里**真跑** run_reflect，取回它构造的真实 prompt。

    假 provider 只负责捕获 prompt；DM 用真实 DataManager（`get_sections` 返回占位
    文本，避免依赖 tag 缓存）。订单**故意不带 score**，以复刻用户报障的
    「比分:? 且没有 goal_line」。
    """
    import subprocess
    code = (
        "import os, sys, json; sys.path.insert(0, %r);\n"
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
        "settled = [{'lota_id': %r, 'hit': True, 'bet_type': '单关', 'pick': 'H',\n"
        "            'odds': 2.0, 'bet_size': 100, 'profit': 100, 'reason': 'r'}];\n"
        "if %r: settled[0]['observation'] = True\n"
        "run_reflect(settled, '2026-06-30', Role(%r), provider=p, dm=dm,\n"
        "            extra_matches=False, save_fac=False, persist=False,\n"
        "            rich_match_info=%r, system_rules=%r);\n"
        "print('<<<PROMPT>>>' + p.prompt)"
        % (str(ROOT), RICH_LID, bool(observation), SINGLE, rich, system_rules or "")
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=str(ROOT), timeout=180)
    assert r.returncode == 0, f"子进程跑 run_reflect 失败: {r.stderr[-500:]}"
    assert "<<<PROMPT>>>" in r.stdout, f"没拿到 prompt: {r.stdout[-300:]}"
    return r.stdout.split("<<<PROMPT>>>", 1)[1]


def _headers(prompt: str) -> list[str]:
    return [ln for ln in prompt.splitlines() if ln.startswith("### lota_id=")]


def test_single_dog_reflect_prompt_untouched_by_default():
    """④ 红线：默认（rich 关闭）单狗反思片段仍是 `比分=?`、**无让球线**。"""
    hs = _headers(_reflect_prompt_in_subprocess(False))
    assert hs, "反思 prompt 必须含比赛片段 header"
    assert all("比分=?" in h for h in hs), f"默认应为比分:?（改造前行为）: {hs}"
    assert all("让球" not in h for h in hs), f"默认不得注入让球线: {hs}"


def test_parlay_rich_reflect_prompt_includes_score_and_goal_line():
    """③ 串关/沙盒显式打开后，片段带真实比分 + 北单让球线（用户报障已修）。"""
    hs = _headers(_reflect_prompt_in_subprocess(True))
    assert hs, "反思 prompt 必须含比赛片段 header"
    assert any(f"比分={RICH_SCORE}" in h for h in hs), f"应回填真实比分: {hs}"
    assert any("让球让1" in h for h in hs), f"应带北单让球线: {hs}"


def test_match_info_backfill_is_opt_in():
    """③+④ 数据层回填必须 opt-in：rich=False 时不得改 score / 不给 goal_line。"""
    from src.data_manager import DataManager
    dm = DataManager()
    legacy = dm.get_match_context(RICH_LID, rich=False)["match"]
    rich = dm.get_match_context(RICH_LID, rich=True)["match"]
    assert legacy.get("goal_line") is None, "默认路径不得输出 goal_line"
    assert legacy.get("score") == "", "默认路径不得回填赛后比分"
    assert rich.get("goal_line") == RICH_GL
    assert rich.get("score") == RICH_SCORE


def test_analysis_prompt_builder_never_reads_score():
    """④ 红线：下单/分析 prompt 只读 home/away/league/time —— 赛后比分不得入内（后视）。"""
    src = (ROOT / "src" / "prompt_builder.py").read_text(encoding="utf-8")
    assert 'match_info.get("score"' not in src
    assert "match_info.get('score'" not in src


# ── ⑤ 反思 prompt build 修正（2026-09-14 用户报障 2 条）──

def _gl_rule_fixture() -> str:
    """真实让球口径块（从北单模块取，保证测的是真文本）。"""
    from src.beidan_parlay_dog import _REFLECT_GL_RULE
    return _REFLECT_GL_RULE


def test_single_dog_reflect_prompt_keeps_legacy_persona_header():
    """④ 红线：单狗（rich=False）仍是改造前的**双标题**，逐字节不变。"""
    p = _reflect_prompt_in_subprocess(False)
    assert "## 🎯 投注人设（所有下单基于此人设）" in p, "单狗必须保持旧标题"
    assert "## 🎯 个人偏好" in p, "单狗必须保持 persona_text 自带标题（双标题=旧行为）"
    assert "🎭 人设（本次反思沿用下注人设" not in p, "新标题不得泄漏到单狗"
    assert "让球线换算" not in p, "让球口径块不得泄漏到单狗"


def test_parlay_reflect_prompt_has_single_persona_header():
    """⑤-a 串关路径人设段只有一个标题，且不再写"所有下单基于此人设"。"""
    p = _reflect_prompt_in_subprocess(True, system_rules=_gl_rule_fixture())
    assert "## 🎯 投注人设（所有下单基于此人设）" not in p, "反思 prompt 不得再挂下注人设标题"
    assert "## 🎭 人设（本次反思沿用下注人设" in p
    seg = p.split("## 🎭 人设", 1)[1][:150]
    assert "## 🎯 个人偏好" not in seg, f"仍在叠双标题: {seg[:100]!r}"


def test_parlay_reflect_prompt_injects_goal_line_semantics():
    """⑤-b H/D/A 必须说明是「让球后」口径（否则 主让1球 的 D/A 读不懂）。"""
    p = _reflect_prompt_in_subprocess(True, system_rules=_gl_rule_fixture())
    assert "H/D/A 与开奖 SP 都是「让球后」口径" in p
    assert "让球线换算" in p, "应带上换算表"
    assert "让球后**赛果" in p


def test_observation_samples_get_accurate_data_header():
    """⑤-c 0 落单日不得把观察样本写成「已结算投注」。"""
    p = _reflect_prompt_in_subprocess(True, system_rules=_gl_rule_fixture(),
                                      observation=True)
    assert "## 观察样本及原始数据" in p
    assert "## 已结算投注及原始数据" not in p


def test_goal_line_rule_reuses_single_source_of_truth():
    """⑤-b `_REFLECT_GL_RULE` 必须复用 `_GL_TRANSLATION_RULE`（不重复维护换算表）。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "_REFLECT_GL_RULE = " in src
    assert "+ _GL_TRANSLATION_RULE" in src, "口径块必须在换算表基础上拼"
    assert src.count('"system_rules": _REFLECT_GL_RULE') == 4, "4 处反思调用都要带口径块"


def test_extra_reflect_dedup_is_opt_in():
    """③ 补充样本去重：足球日窗口跨两个日历日 → 同一场会被重复选中。

    dedup=False（单狗默认）保持旧行为（可能重复）；dedup=True 保证互不相同。
    """
    from src.agent import _extra_reflect_matches

    class FakeDM:
        def get_cached_matches(self, d, lottery_type=None):
            # 两个日历日文件里都有同样两场 → 候选天然重复
            return [{"lota_id": "L1", "match_time": MT, "state": 6},
                    {"lota_id": "L2", "match_time": MT, "state": 6}]

    day = "2026-08-16"
    MT = "2026-08-15 13:00:00"

    legacy = _extra_reflect_matches(FakeDM(), day, set(), max_extra=3)
    assert len(legacy) == 3 and len(set(legacy)) < len(legacy), \
        f"默认（旧行为）应允许重复，实际 {legacy}"

    deduped = _extra_reflect_matches(FakeDM(), day, set(), max_extra=3, dedup=True)
    assert len(deduped) == len(set(deduped)) == 2, f"dedup 后应互不相同，实际 {deduped}"


def test_extra_reflect_only_beidan_is_opt_in():
    """③ 北单狗补充样本不得混入竞彩（彩池 vs 固定赔率是两套口径）。"""
    from src.agent import _extra_reflect_matches

    class FakeDM:
        def get_cached_matches(self, d, lottery_type=None):
            return [{"lota_id": "B1", "match_time": MT, "state": 6, "beidan_number": "26069_1"},
                    {"lota_id": "J1", "match_time": MT, "state": 6, "jingcai_number": "周六001"},
                    {"lota_id": "J2", "match_time": MT, "state": 6, "jingcai_number": "周六002"}]

    day = "2026-08-16"
    MT = "2026-08-15 13:00:00"

    legacy = _extra_reflect_matches(FakeDM(), day, set(), max_extra=3)
    assert len(legacy) == 3, f"默认（旧行为）抽 3 场不限口径，实际 {legacy}"

    bd = _extra_reflect_matches(FakeDM(), day, set(), max_extra=3,
                                only_beidan=True, dedup=True)
    assert bd == ["B1"], f"只应保留北单场次（去重后），实际 {bd}"


def test_rich_mode_falls_back_to_question_mark_for_missing_score():
    """③ 脏数据（state=6 但无赛果）不得渲染成 `比分=`；rich 模式回落 `?`。"""
    src = (ROOT / "src" / "agent.py").read_text(encoding="utf-8")
    assert 'if rich_match_info and not score:' in src, "缺比分回落只在 rich 分支"


