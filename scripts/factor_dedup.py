#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子归纳去重原型（LLM 判重版）。

确定性部分只做通用候选预筛（difflib 序列相似度，无中文词表），
语义是否重复 / 方向是否冲突 / 合并到谁，交给 LLM 判断。

用法:
  DEEPSEEK_API_KEY=xxx python3 scripts/factor_dedup.py            # 全库 as-if 回放
  DEEPSEEK_API_KEY=xxx python3 scripts/factor_dedup.py --limit 20 # 只处理前 N 个候选
"""

import difflib
import json
import sys
from datetime import date
from pathlib import Path

ROLE = Path("lota_data/roles/梭哈2狗/memory/factor_memory.json")
PRE_FILTER = 0.45  # 通用序列相似度预筛阈值（非语义判断）


def load_provider():
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src.providers.deepseek import DeepSeekProvider
    return DeepSeekProvider()


def seq_ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def shortlist(existing, cand_name, top=6):
    scored = [(seq_ratio(e["name"], cand_name), e) for e in existing]
    scored.sort(key=lambda x: -x[0])
    return [e for s, e in scored if s >= PRE_FILTER][:top]


JUDGE_SYSTEM = """你是足球因子库管理员。判断"候选因子"是否与现有因子重复。
规则：
1. 语义重复（同一模式的不同表述/同义改写）→ action=merge，target 填最匹配的现有因子名。
2. 方向相反（如上盘 vs 下盘、让球方 vs 受让方、追强 vs 防冷）→ 绝不合并，action=create。
3. 候选与 retired 状态的现有因子重复 → action=suppress（不新建、不复活已退役因子），target 填该因子名。
4. 候选与某现有因子样本都充足且盈亏方向相反 → 视为经验上不同的模式，action=create。
5. 候选是全新模式 → action=create。
只输出 JSON，不要多余文字。"""


def llm_judge(provider, cand, existing_list):
    cand_line = f"候选因子: {cand['name']} | {cand.get('desc','')[:120]}"
    lib_lines = []
    for i, e in enumerate(existing_list, 1):
        st = e.get("status", "active")
        lib_lines.append(
            f"{i}. {e['name']} [状态:{st}] | {e.get('desc','')[:80]} "
            f"(样本{e.get('total',0)} 盈亏{e.get('profit',0):+.0f})"
        )
    user = (
        cand_line + "\n\n现有因子:\n" + "\n".join(lib_lines) + "\n\n"
        '输出 JSON: {"action": "merge|create|suppress", "target": "因子名或null", "reason": "一句话"}'
    )
    raw = provider.call(JUDGE_SYSTEM, [{"role": "user", "content": user}], temperature=0.0)
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return {"action": "create", "target": None, "reason": "LLM 输出无法解析，保守新建"}
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {"action": "create", "target": None, "reason": "LLM JSON 解析失败，保守新建"}


def merge_stats(target, cand_entry):
    """target=现有因子 data dict；cand_entry=候选条目（含 name/data）。"""
    cand = cand_entry["data"]
    hist = {h.get("lota_id", "") + "|" + (h.get("date") or ""): h for h in target.get("history", [])}
    for h in cand.get("history", []):
        hist[h.get("lota_id", "") + "|" + (h.get("date") or "")] = h
    merged = sorted(hist.values(), key=lambda h: h.get("date") or "")
    dates = [h.get("date", "")[:10] for h in merged if h.get("date")]
    target.update({
        "total": len(merged),
        "hit": sum(1 for h in merged if h.get("hit") is True),
        "miss": sum(1 for h in merged if h.get("hit") is False),
        "push": sum(1 for h in merged if h.get("hit") is None and (h.get("profit") or 0) != 0),
        "profit": sum(float(h.get("profit") or 0) for h in merged),
        "total_return": round(sum(float(h.get("return_ratio") or 0) for h in merged), 2),
        "first_seen": min(dates) if dates else target.get("first_seen"),
        "last_seen": max(dates) if dates else target.get("last_seen"),
        "history": merged,
    })
    aliases = set(target.get("aliases") or []) | {cand_entry["name"]}
    target["aliases"] = sorted(a for a in aliases if a)
    if target.get("status") == "dormant":
        target["status"] = "active"
    return target


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    mem = json.loads(ROLE.read_text(encoding="utf-8"))["factor_perf"]
    entries = []
    for fid, s in mem.items():
        first = (s.get("first_seen") or "")[:10]
        try:
            fd = date.fromisoformat(first) if first else None
        except ValueError:
            fd = None
        entries.append({"name": fid, "desc": s.get("desc", ""), "status": s.get("status", "active"),
                        "first_seen": fd, "data": dict(s)})
    entries.sort(key=lambda e: e["first_seen"] or date.max)
    if limit:
        entries = entries[:limit]

    provider = load_provider()
    library = []
    stats = {"created": 0, "merged": 0, "suppressed": 0}
    groups = {}
    for e in entries:
        sl = shortlist(library, e["name"])
        if not sl:
            library.append(e)
            stats["created"] += 1
            continue
        verdict = llm_judge(provider, e, sl)
        action = verdict.get("action", "create")
        target_name = verdict.get("target")
        if action == "suppress" and target_name:
            stats["suppressed"] += 1
            groups.setdefault(target_name, []).append(f"[压制]{e['name']}")
        elif action == "merge" and target_name:
            target = next((x for x in library if x["name"] == target_name), None)
            if target:
                merge_stats(target["data"], e)
                stats["merged"] += 1
                groups.setdefault(target_name, []).append(e["name"])
            else:
                library.append(e)
                stats["created"] += 1
        else:
            library.append(e)
            stats["created"] += 1

    print(f"处理 {len(entries)} 个候选 → 独立因子 {len(library)}")
    print(f"新建 {stats['created']} | 合并 {stats['merged']} | 压制 {stats['suppressed']}")
    for k, v in sorted(groups.items(), key=lambda x: -len(x[1])):
        if v:
            print(f"  {k}  ←  {', '.join(v[:5])}")


if __name__ == "__main__":
    main()
