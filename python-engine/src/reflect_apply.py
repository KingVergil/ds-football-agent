"""
效果 A 的反思结果应用桥 —— 供 harness agent 的 ds_reflect 工具调用。

职责（确定性，无 LLM）：harness agent 已经完成 reflect 的 LLM 因子发现
（产出 factor JSON），本模块只把 JSON 结果确定性写回：
  FactorMemory 归因 + 新因子模型(save_factor) + 反思记忆 + money_lesson。

与 LangGraph 的 run_reflect 对齐：run_reflect = 本模块的「写回」+ LLM 推理；
效果 A 把 LLM 推理搬到 harness agent，本模块保留确定性写回。

stdin:  {"user","day","reflect":<factor JSON>,"settled":[<结算单>...]}
stdout: {"ok","attributed","new_factors","summary"} 或 {"error"}

运行:  echo '<json>' | python3 -m src.reflect_apply
"""

from __future__ import annotations

import json
import re
import sys

from .models import Factor
from .role import Role
from .store import save_factor


def _clean_name(name: str) -> str:
    """清洗因子名（去引号/括号注释/emoji），与 run_reflect 一致。"""
    n = (name or "").strip()
    n = n.strip('"\'“”`')
    n = re.sub(r'[（(][^）)]*[）)]', '', n).strip()
    n = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]', '', n).strip()
    return n


def _valid(name: str) -> bool:
    n = _clean_name(name)
    if not n or n.lower() in ("无", "none", "null", "n/a", "-", "无新因子"):
        return False
    if ":" in n or n.lower().startswith("key_"):
        return False
    return True


def apply_reflection(user: str, day_date: str, data: dict,
                     settled: list[dict]) -> dict:
    """把 factor JSON 确定性写回某只狗的因子/反思记忆。"""
    try:
        role = Role.load(user)
    except (FileNotFoundError, ValueError):
        return {"ok": False, "error": f"角色 {user} 不存在"}

    factors = role.memory.factors
    if not factors._loaded:
        factors.load()
    existing_names = {n.lower() for n in factors.factor_perf.keys()}

    desc_map: dict = data.get("factor_desc", {}) or {}
    summary = data.get("reflection", "") or ""
    money_lesson = data.get("money_lesson", "") or ""
    key_slugs_list: list = data.get("key_slugs", []) or []
    noise_slugs_list: list = data.get("noise_slugs", []) or []
    key_slugs_str = ", ".join(key_slugs_list)

    # 归因映射 order_N -> factors
    attr_map: dict[int, list[str]] = {}
    raw_attr = data.get("factor_attribution", {}) or {}
    for key, fs in raw_attr.items():
        m = re.match(r'order_(\d+)', key)
        if not m:
            continue
        idx = int(m.group(1))
        if 0 <= idx < len(settled):
            attr_map[idx] = [
                _clean_name(f) for f in (fs if isinstance(fs, list) else [fs])
                if _valid(f)
            ]

    # 1. 归因驱动 record（hit/profit 取自结算单）
    for idx, fns in attr_map.items():
        o = settled[idx]
        for fn in fns:
            factors.record(
                fn, o.get("hit"), o.get("profit", 0),
                desc=desc_map.get(fn, ""), date=day_date,
                lota_id=o.get("lota_id", ""), bet_size=o.get("bet_size", 0),
            )

    # 2. 兜底：LLM 提到但未归因的新因子
    new_factors: list[str] = []
    all_attributed = {f.strip().lower() for fns in attr_map.values() for f in fns}
    for raw in (_clean_name(f) for f in data.get("alpha_factors", []) if _valid(f)):
        fn_lower = raw.lower()
        is_dup = any(fn_lower == en or fn_lower in en or en in fn_lower
                     for en in existing_names)
        if not is_dup:
            new_factors.append(raw)
            existing_names.add(fn_lower)
    for fn in new_factors:
        if fn.lower() not in all_attributed:
            factors.record(fn, None, 0, desc=desc_map.get(fn, ""), date=day_date)

    # 2.5 保存新 Factor 模型
    for fn in new_factors:
        try:
            save_factor(Factor(
                id=f"fac_{fn.lower().replace(' ', '_')[:40]}",
                slugs=[s.strip() for s in key_slugs_str.split(",") if s.strip()],
                content=desc_map.get(fn, summary[:300]),
            ))
        except Exception:
            pass

    # 3. 反思记忆（含低样本标）
    sample_count = len({o.get("lota_id") for o in settled if o.get("lota_id")})
    role.memory.reflections.add_reflection(day_date, summary, sample_count=sample_count)
    if key_slugs_list or noise_slugs_list:
        slug_note = f"\n📡 有效slug: {key_slugs_str if key_slugs_str else '无'}"
        if noise_slugs_list:
            slug_note += f"\n🔇 噪声slug: {', '.join(noise_slugs_list)}"
        if attr_map:
            slug_note += f"\n📐 因子归因: {len(attr_map)} 笔订单已关联到因子"
        if role.memory.reflections.reflections:
            last = role.memory.reflections.reflections[-1]
            last["reflection"] = last.get("reflection", "") + slug_note
            role.memory.reflections._save()
    if money_lesson and role.memory.reflections.reflections:
        last = role.memory.reflections.reflections[-1]
        last["reflection"] = last.get("reflection", "") + f"\n💰 资金教训: {money_lesson}"
        role.memory.reflections._save()

    role.save()

    return {
        "ok": True,
        "user": user,
        "day": day_date,
        "attributed": sum(len(v) for v in attr_map.values()),
        "new_factors": new_factors,
        "summary": summary[:200],
    }


if __name__ == "__main__":
    result: dict
    try:
        data = json.loads(sys.stdin.read() or "{}")
        result = apply_reflection(
            data.get("user", "default"),
            data.get("day", ""),
            data.get("reflect", {}),
            data.get("settled", []),
        )
    except Exception as e:  # noqa: BLE001 —— 桥接层把异常转成 JSON，恒返回合法 JSON
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, ensure_ascii=False))
