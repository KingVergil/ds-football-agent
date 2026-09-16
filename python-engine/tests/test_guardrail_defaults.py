"""护栏默认值必须保守（2026-09-14 修正）。

## 事故

0704 实跑：配置 `max_combos: 0, max_stake_pct: 0` 被旧实现解释成
**不限注数 + 可押全部本金**（`or 10**9` / `or 100.0`），于是 9 腿里 3 条双选把
126 注的票放大到 **456 注 = 912 元/单**（本金 18%），两波 = 1824 元。

## 现在

1. `max_combos` 为 0/缺失 → 保守默认 = **全单选口径 C(N, M)**（不是多选展开数！）
2. `max_stake_pct` 为 0/缺失 → 保守默认 **10%**
3. 超限时**先削多选 pick、再删腿** —— 保住腿数（9 腿就该是 252 元，不该变 8 腿）
4. 回落默认时打警告
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.beidan_parlay_dog import (  # noqa: E402
    DEFAULT_MAX_STAKE_PCT,
    BeidanParlayDog,
)


def _dog() -> BeidanParlayDog:
    """只借方法/常量，不碰角色目录（`_apply_flex_guardrails` 不需要 role）。"""
    return BeidanParlayDog.__new__(BeidanParlayDog)


def _leg(i: int, picks: list[str]) -> dict:
    odds = {"H": 4.0, "D": 4.5, "A": 6.0}
    phat = {"H": 0.30, "D": 0.24, "A": 0.17}
    return {
        "lota_id": f"L{i}", "beidan_number": str(i),
        "picks": list(picks), "rule_picks": list(picks),
        "odds": {k: odds[k] for k in picks},
        "p_hat": {k: phat[k] for k in picks},
        "p_claim": {k: phat[k] for k in picks},
        "leg_v": 1.2,
        "beidan_info": {"goal_line": "0"},
    }


def _pool() -> list[dict]:
    """9 腿：6 条单选 + 3 条双选（复刻 0704 真实形状）。"""
    legs = [_leg(i, ["D"]) for i in range(1, 7)]
    legs += [_leg(i, ["H", "D"]) for i in range(7, 10)]
    return legs


def _bare_cfg(**over) -> dict:
    cfg = {"ticket_tolerance": 4, "max_legs": 17, "max_per_leg": 3,
           "max_combos": 0, "max_stake_pct": 0}
    cfg.update(over)
    return cfg


def test_default_max_combos_is_single_pick_count_not_expanded():
    """保守默认必须是 C(9,5)=126（全单选口径），绝不能是 456（多选展开数）。"""
    kept, meta = _dog()._apply_flex_guardrails(_bare_cfg(), _pool(), 5000.0, ticket="9过5")
    assert meta["combos"] == 126, f"默认上限应压到全单选口径 126 注，实际 {meta['combos']}"
    assert meta["cost"] == 252.0


def test_default_keeps_all_legs_by_slimming_picks():
    """超限时先削多选 pick → 腿数不掉（9 腿就该是 252 元，不该变 8 腿）。"""
    kept, meta = _dog()._apply_flex_guardrails(_bare_cfg(), _pool(), 5000.0, ticket="9过5")
    assert len(kept) == 9, f"腿数不得因削 pick 而减少，实际 {len(kept)}"
    assert not meta.get("dropped"), f"不应删腿: {meta.get('dropped')}"
    assert all(len(l["picks"]) == 1 for l in kept), "多选腿应被收成单选"
    slimmed = [l for l in kept if l.get("slimmed_from")]
    assert len(slimmed) == 3, "应有 3 条双选腿被削"
    for l in slimmed:
        assert len(l["slimmed_from"]) == 2 and len(l["picks"]) == 1


def test_default_stake_pct_allows_all_in():
    """用户口径（2026-09-14）：**允许梭哈**，0/缺失 = 100%（不做保守默认）。"""
    assert DEFAULT_MAX_STAKE_PCT == 100.0
    _, meta = _dog()._apply_flex_guardrails(_bare_cfg(), _pool(), 5000.0, ticket="9过4")
    assert meta["max_stake_pct"] == 100.0
    assert meta["budget"] == 5000.0, "预算应为本金的 100%"


def test_explicit_max_combos_still_allows_multi_pick():
    """显式配大 max_combos → 多选照旧（不能把功能砍掉，只能改默认）。"""
    cfg = _bare_cfg(max_combos=10 ** 9, max_stake_pct=100.0)
    kept, meta = _dog()._apply_flex_guardrails(cfg, _pool(), 5000.0, ticket="9过5")
    assert meta["combos"] == 456, f"显式放宽后应保留多选展开，实际 {meta['combos']}"
    assert any(len(l["picks"]) > 1 for l in kept)


def test_zero_is_not_unlimited_anymore():
    """核心红线：0 不能再等于「无限制」。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert 'int(cfg.get("max_combos") or 0) or 10 ** 9' not in src, \
        "0 → 10亿 注的旧赋值必须移除"
    assert 'float(cfg.get("max_stake_pct") or 0) or 100.0' not in src, \
        "0 → 100% 本金的旧赋值必须移除"
    assert "_slim_multi(" in src, "必须先削 pick 再删腿"


# ── `max_per_leg` 必须在**规则路径**也生效（2026-09-14）──

def test_cap_picks_keeps_highest_x():
    """超限时留 x 最高的侧，并保持 H/D/A 稳定顺序。"""
    from src.beidan_parlay_dog import cap_picks_by_x
    xs = {"H": 1.16, "D": 1.35, "A": 0.90}
    assert cap_picks_by_x(["H", "D"], xs, 1) == ["D"], "应留 x 更高的 D"
    assert cap_picks_by_x(["H", "D"], xs, 2) == ["H", "D"]
    # 未超限 → 原样返回（不重排，保持上游顺序）
    assert cap_picks_by_x(["A", "H", "D"], xs, 3) == ["A", "H", "D"]
    # 超限 → 按 H/D/A 稳定顺序返回
    assert cap_picks_by_x(["A", "H", "D"], xs, 2) == ["H", "D"], "超限后应稳定排序"
    assert cap_picks_by_x(["H"], xs, 1) == ["H"]
    assert cap_picks_by_x([], xs, 1) == []


def test_rule_path_actually_calls_the_cap():
    """红线：规则路径必须真的调用它，不能再无视 max_per_leg。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "cap_picks_by_x(picks, xs, int(cfg.get(\"max_per_leg\") or 3))" in src, \
        "规则路径必须按 max_per_leg 裁侧"


def test_config_max_per_leg_one_yields_126_notes():
    """`max_per_leg=1` + 9 腿 → 3 条双选腿在**组装前**就是单选 → 126 注 = 252 元。"""
    from src.beidan_parlay_dog import cap_picks_by_x
    xs = {"H": 1.16, "D": 1.35, "A": 0.90}
    # 模拟规则路径：每场都过门两侧
    capped = [cap_picks_by_x(["H", "D"], xs, 1) for _ in range(9)]
    assert all(len(p) == 1 for p in capped), "每腿应只剩 1 侧"
    legs = [_leg(i, p) for i, p in enumerate(capped, 1)]
    _, meta = _dog()._apply_flex_guardrails(_bare_cfg(), legs, 5000.0, ticket="9过5")
    assert meta["combos"] == 126 and meta["cost"] == 252.0
    assert not meta["slimmed"], "组装前已单侧 ⇒ 护栏无需再削"


# ── prompt 里不得再出现「全包/覆盖」旧口径（M过N 下没有覆盖腿，2026-09-14）──

def test_analysis_prompt_has_no_stale_cover_wording():
    """波动型因子的正确用法 = **只用于否决**；分析 prompt 不得再教 LLM「放全包/覆盖」。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    for bad in ("宁可放全包/防冷", "用来决定哪几场必须全包", "规避或降为全包",
                "用来决定覆盖/多选", "不处理覆盖腿", "仅供判断哪些场次要全包"):
        assert bad not in src, f"旧「全包/覆盖」文案残留: {bad}"
    assert "正向信号" in src, "波动型因子应标为正向信号（错价大 ⇒ 更该买）"
    assert "没有全包/覆盖腿" in src, "必须明确告知 LLM 本票型没有覆盖腿"
    assert "只能用于否决" not in src, "否决口径已被证伪（错价大=更该买）"


def test_persona_does_not_prescribe_ticket_form():
    """人设不得规定票型（票型由引擎按配置定，LLM 不该被引导）。"""
    p = ROOT / "data" / "roles" / "94狗" / "persona.md"
    txt = p.read_text(encoding="utf-8")
    assert "9过5" not in txt and "9过4" not in txt, "人设不得再写具体票型"
    assert "票型（几串几 / 容错几关）由引擎按配置决定" in txt
    assert "抽掉最好日仍为正" not in txt, "已失效的论证不得留在人设里"


# ── 腿序由 LLM 决策（rank）决定，引擎不自己打分（2026-09-14 用户口径）──

def test_stage1_prompt_asks_for_rank_with_priority_rule():
    """prompt 必须要求 rank，并写明「波动+方向同向触发」排最前。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert '"rank":1' in src, "stage1 输出契约必须含 rank"
    assert "波动型因子与方向型因子「同向触发」的场次排最前" in src, "必须写明排序规则"
    assert "引擎按 rank 取前 N 条腿" in src, "必须说明 rank 的实际作用"


def test_engine_orders_legs_by_llm_rank():
    """引擎只尊重 rank，不自己按 x 打分排序。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "stage1_items.sort(key=lambda it: (0, _rank_of(it))" in src
    # 缺 rank 的排后面（稳定），不能被当成 0
    assert "else (1, 0.0))" in src


def test_rank_sort_is_stable_and_optional():
    """无 rank / 混合 rank 时不得崩，且无 rank 的排在有 rank 之后。"""
    items = [{"lota_id": "A"}, {"lota_id": "B", "rank": 2}, {"lota_id": "C", "rank": 1}]
    if any(isinstance(it.get("rank"), (int, float)) for it in items):
        items.sort(key=lambda it: (0, float(it["rank"]))
                   if isinstance(it.get("rank"), (int, float)) else (1, 0.0))
    assert [i["lota_id"] for i in items] == ["C", "B", "A"]
    empty = [{"lota_id": "X"}, {"lota_id": "Y"}]
    if any(isinstance(it.get("rank"), (int, float)) for it in empty):
        empty.sort(key=lambda it: (0, float(it["rank"]))
                   if isinstance(it.get("rank"), (int, float)) else (1, 0.0))
    assert [i["lota_id"] for i in empty] == ["X", "Y"], "全无 rank ⇒ 原顺序不变"


# ── R2：跨波不得重复下单同一场（2026-09-14 实测 07-04 重叠 7/9）──

def test_assemble_skips_already_ordered_matches():
    """已在本日下过单的场次，第二波必须跳过（不再加倍下注）。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "本日已下单，跳过（跨波去重）" in src, "必须有事次去重逻辑"
    assert "_already_used" in src
    # 定义必须落在 _assemble_legs_rule 内（曾误插到 _apply_flex_guardrails 导致 NameError）
    i_fn = src.index("def _assemble_legs_rule(")
    i_use = src.index("if lid in _already_used:")
    i_def = src.index("_already_used: set[str] = set()")
    assert i_fn < i_def < i_use, "定义必须位于 _assemble_legs_rule 内、使用点之前"


# ── 不得再用 Pinnacle 兜底北单赔率（会让 x≡1.00 静默失效，2026-09-14）──

def test_beidan_odds_no_circular_pinnacle_fallback():
    """兜底源与市场参考同源 ⇒ x = p̂×(1/p̂) ≡ 1.00，错价检测整体退化。

    实测后果：07-05~07-13 连续 8 天 x 全 1.00 → 无一侧过门 → 全部 skip → 空仓。
    现在三路缺失必须**排除该场**并告警。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "去水成「公平赔率」再兜底" not in src, "旧的 Pinnacle 兜底必须移除"
    assert "不再用 Pinnacle 兜底" in src, "必须有明确注释说明为什么不兜底"
    assert "_odds_missing" in src, "必须计数告警（可观测）"
    # 排除语义：三路缺失 → 返回 {}
    i = src.index("def _beidan_odds(")
    seg = src[i:i + 2600]
    assert "return {}" in seg


# ── 自适应档位 ticket_m：固定每注 M 关、腿数随当天浮动（2026-09-14 用户口径）──

def _ticket_for(n_legs: int, cfg: dict) -> str:
    """复刻规则路径的票型选择逻辑（与源码同构）。"""
    tm = int(cfg.get("ticket_m") or 0)
    tol = int(cfg.get("ticket_tolerance") or 0)
    if tm > 0:
        return f"{n_legs}过{tm}" if (n_legs >= 5 and n_legs > tm) else ""
    if tol > 0 and n_legs > 2:
        tol = min(tol, n_legs - 2, 8)
        return f"{n_legs}过{n_legs - tol}"
    return f"{n_legs}串1"


def test_adaptive_ticket_m_gives_n_guo_4():
    """ticket_m=4：≥5 腿就出 `n过4`（5过4 / 6过4 / 9过4…），<5 腿空仓。"""
    cfg = _bare_cfg(ticket_m=4, ticket_tolerance=0, max_stake_pct=0)
    assert _ticket_for(9, cfg) == "9过4"
    assert _ticket_for(7, cfg) == "7过4"
    assert _ticket_for(5, cfg) == "5过4"
    assert _ticket_for(4, cfg) == "", "腿数不足 5 → 空仓（用户口径）"


def test_config_94dog_uses_ticket_m_4_and_all_in():
    """94狗 配置：固定每注 4 关 + 允许梭哈 + 每腿单选（原名 95狗 来自 9过5 时代）。"""
    d = json.loads((ROOT / "data" / "roles" / "94狗" / "parlay.json").read_text(encoding="utf-8"))
    assert d.get("ticket_m") == 4
    assert d.get("ticket_tolerance") == 0
    assert d.get("max_stake_pct") == 0, "0 ⇒ 梭哈（100%）"
    assert d.get("max_per_leg") == 1, "每腿只买 1 侧"


def test_ticket_m_is_in_config_whitelist():
    """回归：`ticket_m` 必须在 `_load_parlay_config` 白名单里。

    曾漏加 ⇒ 配置被丢弃 ⇒ 回落 `9串1`（2 元单注票），实测 07-17 出了 1 张 2 元的 9串1。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    i = src.index("def _load_parlay_config")
    seg = src[i:i + 3000]
    assert '"ticket_m"' in seg, "ticket_m 必须进白名单，否则配置被静默丢弃"


def test_empty_ticket_form_means_truly_flat():
    """回归：`ticket_m` 下腿数不足 5 时必须**真空仓**。

    曾只用 `_flex_plan_ticket = ""` 表示空仓，但下游把空串回落成 `N串1`
    —— 实测 07-21 出了 1 张 `4串1`（2 元）的票。现在直接返回空腿集。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "票型为空（腿数" in src and "return []" in src, "空票型必须返回空腿集"


def test_rule_ticket_mode_ignores_llm_ticket():
    """回归：`ticket_mode="rule"` 时引擎定票型，LLM 不得覆盖。

    曾实测：07-25 两波各 9 腿（富波），引擎算出 `9过4`，却被 stage2 LLM 的
    `ticket` 字段盖成「每注 2 关」→ 池门（≥3 关）拒单 → 富波空仓。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "忽略 LLM 票型" in src, "必须有忽略提示（可观测）"
    i = src.index("忽略 LLM 票型")
    seg = src[max(0, i - 900):i]
    assert '_tmode != "rule"' in seg, "覆盖必须被 ticket_mode 守卫"


def test_default_max_combos_uses_ticket_m_when_set():
    """回归：默认 `max_combos` 必须优先用 `ticket_m`。

    只看 `ticket_tolerance`（=0）会算出 m=n ⇒ C(9,9)=1，把注数上限压成 1 注（自伤）。
    实测 07-26 打印「保守默认 1 注（C(9,9)）」。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "_tm = int(cfg.get(\"ticket_m\") or 0)" in src
    assert "_m = _tm if _tm > 0 else max(len(legs) - _tol, 2)" in src


def test_rank_is_coerced_and_persisted():
    """rank 容错 + 落进腿记录（复盘不能再靠挖 md）。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert 'float(str(it.get("rank")).strip())' in src, "rank 必须容错（数字/字符串）"
    assert 'leg["llm_rank"]' in src, "rank 必须落进腿记录，便于复盘对照"


def test_leg_decision_is_dumped_for_ab():
    """决策落盘：rank 有效性 A/B 必须能直接读，不能靠挖 session md。"""
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "leg_decision_" in src, "必须按日落盘有序候选"
    assert '"kept_ids"' in src and '"candidates"' in src, "需含入选腿与有序候选"


def test_every_stage1_batch_response_is_dumped():
    """回归：stage1 **每个 batch** 的响应都必须落盘。

    此前只落最后一个 batch（实测 batch1/2 全沙箱无落盘），导致 rank/腿序
    对照只能看到 1/3 样本（10/50、5/45、14/54），无法复原决策。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    assert "stage1_resp_" in src, "每个 batch 的响应必须按日落盘"
    i = src.index("stage1_resp_")
    seg = src[max(0, i - 400):i + 200]
    assert "batch_no" in seg, "落盘需带 batch 号"
    assert '"response": resp' in seg, "落盘需含原始响应"


def test_decision_dump_rebuilds_input_idx_and_is_not_silent():
    """回归：决策落盘必须自建 `_input_idx`，且失败不得静默。

    曾直接引用 `_select_legs_llm` 的局部 `_input_idx` ⇒ NameError 被 `except: pass`
    吞掉 ⇒ leg_decision 文件静默缺失（08-02/08-03 实测，无任何提示）。
    """
    src = (ROOT / "src" / "beidan_parlay_dog.py").read_text(encoding="utf-8")
    i = src.index("leg_decision_")
    seg = src[max(0, i - 1500):i + 1200]
    assert "_input_idx = {it.get(\"lota_id\"): i for i, it in enumerate(stage1_items)}" in seg, \
        "必须在 _assemble_legs_rule 内重建 _input_idx"
    assert "决策落盘失败" in seg, "落盘失败必须打印（不得静默）"
    assert "strftime('%H%M%S')" in seg, "文件名须带时刻，否则多波次互相覆盖"
    assert "default=str" in seg
    j = src.index("stage1_resp_")
    seg2 = src[max(0, j - 800):j + 800]
    assert "stage1 批次落盘失败" in seg2, "stage1 落盘同样不得静默"
