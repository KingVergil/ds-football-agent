"""因子归纳独立步骤（阶段 3）。

每日 settle 后运行：把各角色每日生产的因子做统一清洗/合并/补定义，
形成干净的因子库供注册表与 analyze 消费。

调度（已定，见 docs/workflow_tool_groups.md §2.3）：
  - 阶段 A（非 alpha，可并行）：各非 alpha 狗各自归纳（只在角色内部合并）
  - 阶段 B（barrier，串行）：等非 alpha 全部完成后再做 alpha 跨狗统一归纳
    （alpha2狗/alpha狗/均注狗：跨角色合并同模式因子，1 次进全库）
    ⚠️ alpha 必须等"其他因子出现"后操作，不能与非 alpha 并行

合并候选策略（已定）：
  - 不做 kmeans；因子→slugs 是 one-hot 向量，bit 距离（对称差）≤2 的因子对进入 LLM 判重
  - 无 slugs 的孤儿因子用名字相似度（difflib ≥0.6）预筛
  - 同清洗名直接确定性合并（不调 LLM）

用法:
  python -m src.factor_induction --dry-run           # 只报告候选，不调 LLM、不写盘
  python -m src.factor_induction --limit 30          # 最多 30 次 LLM 判重
  python -m src.factor_induction --roles 梭哈2狗     # 只处理指定角色（逗号分隔）
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.store import _get_valid_section_slugs  # noqa: E402
from src.role_registry import all_agents as _all_agents, alpha_agents as _alpha_agents  # noqa: E402

ROLES_DIR = Path(os.environ.get("DS_ROLES_ROOT") or ROOT / "data" / "roles")
# 沙箱隔离：fac_*.json 注册表随角色根一起重定向（沙箱创建时会把线上 factors 复制进 workspace）
FACTORS_DIR = Path(os.environ.get("DS_FACTORS_ROOT") or ROOT / "data" / "factors")
AUDIT_LOG = ROOT / "scripts" / "factor_induction_audit.jsonl"
BAK_SUFFIX = ".bak.20260805_phase3"

# 从角色文件 alpha_mode 派生（设计定稿：alpha 结构化存角色；注册表狗自动并入）
ALPHA_ROLES = set(_alpha_agents())
ALL_ROLES = _all_agents()
BIT_DIST_MAX = 2
NAME_RATIO_MIN = 0.60
BIT_NAME_FLOOR = 0.35      # bit 候选还需名字相似度 ≥ 下限（slugs 共用核心段，纯 bit 距离会爆量）
PER_FACTOR_PAIR_CAP = 3    # 每个因子最多参与的对数（按名字相似度取 top）
VALID_SLUGS = _get_valid_section_slugs()

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


def clean_name(name: str) -> str:
    n = (name or "").strip()
    n = n.strip('"\'“”`')
    n = re.sub(r"[（(][^）)]*[）)]", "", n).strip()
    n = _EMOJI_RE.sub("", n).strip()
    n = re.sub(r"\s+", " ", n)
    return n


def fac_id_for(name: str) -> str:
    return f"fac_{name.lower().replace(' ','_')[:40]}"


def recompute_stats(entry: dict) -> dict:
    """从 history 重算统计（半赢/半输口径与 factor_stats 迁移一致）。"""
    hist = entry.get("history", []) or []
    total = len(hist)
    hit = sum(
        1.0 if h.get("hit") is True else
        (0.5 if h.get("hit") == 0.5 else 0.0)
        for h in hist
    )
    miss = sum(
        1.0 if h.get("hit") is False else
        (0.5 if h.get("hit") == -0.5 else 0.0)
        for h in hist
    )
    push = total - hit - miss
    profit = sum(float(h.get("profit", 0)) for h in hist)
    ret = sum(float(h.get("return_ratio", 0)) for h in hist)
    dates = [h.get("date", "") for h in hist if h.get("date")]
    entry["total"] = total
    entry["hit"] = hit
    entry["miss"] = miss
    entry["push"] = push
    entry["profit"] = round(profit, 2)
    entry["total_return"] = round(ret, 4)
    if dates:
        entry["first_seen"] = min(dates)
        entry["last_seen"] = max(dates)
    return entry


def load_roles() -> dict[str, dict]:
    roles: dict[str, dict] = {}
    for rd in sorted(ROLES_DIR.iterdir()):
        if not rd.is_dir() or "_sim" in rd.name or rd.name.startswith("__"):
            continue
        mp = rd / "memory" / "factor_memory.json"
        if not mp.exists():
            continue
        roles[rd.name] = json.loads(mp.read_text(encoding="utf-8"))
    return roles


def fac_slugs(fac_id: str) -> list[str]:
    p = FACTORS_DIR / f"{fac_id}.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("slugs", [])
    except Exception:
        return []


def entry_slugs(entry: dict) -> list[str]:
    slugs = entry.get("slugs") or []
    if slugs:
        return slugs
    return fac_slugs(entry.get("fac_id") or fac_id_for(entry.get("_name", "")))


def bit_distance(sa: set, sb: set) -> int:
    return len(sa ^ sb)


def find_candidates(entries: dict[str, dict]) -> list[tuple[str, str, str]]:
    """返回候选对列表 [(a, b, kind)]，kind ∈ {same_name, bit, name}。"""
    # 1) 同清洗名（确定性合并，不调 LLM）
    by_clean: dict[str, list[str]] = {}
    for name in entries:
        by_clean.setdefault(clean_name(name), []).append(name)
    same = []
    for names in by_clean.values():
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                same.append((names[i], names[j], "same_name"))

    # 2) bit 距离 ≤2（双方都有 slugs）+ 名字相似度 ≥ 下限，每因子取 top 3
    names = list(entries)
    bit_scores = {}
    for i in range(len(names)):
        sa = set(entry_slugs(entries[names[i]]))
        if not sa:
            continue
        for j in range(i + 1, len(names)):
            sb = set(entry_slugs(entries[names[j]]))
            if not sb:
                continue
            dist = bit_distance(sa, sb)
            if dist <= BIT_DIST_MAX:
                ratio = difflib.SequenceMatcher(None, names[i], names[j]).ratio()
                if ratio >= BIT_NAME_FLOOR:
                    bit_scores[(names[i], names[j])] = (ratio, -dist)
    per_factor: dict[str, int] = {}
    bit_pairs = []
    for (a, b), (ratio, _) in sorted(bit_scores.items(), key=lambda x: -x[1][0]):
        if per_factor.get(a, 0) >= PER_FACTOR_PAIR_CAP or per_factor.get(b, 0) >= PER_FACTOR_PAIR_CAP:
            continue
        bit_pairs.append((a, b, "bit"))
        per_factor[a] = per_factor.get(a, 0) + 1
        per_factor[b] = per_factor.get(b, 0) + 1

    # 3) 孤儿（无 slugs）名字相似度
    name_pairs = []
    for i in range(len(names)):
        if entry_slugs(entries[names[i]]):
            continue
        for j in range(i + 1, len(names)):
            if entry_slugs(entries[names[j]]):
                continue
            if difflib.SequenceMatcher(None, names[i], names[j]).ratio() >= NAME_RATIO_MIN:
                name_pairs.append((names[i], names[j], "name"))

    return same + bit_pairs + name_pairs


JUDGE_SYSTEM = """你是足球因子库管理员。判断两个因子是否为同一模式。
规则：
1. 语义重复（同一模式的不同表述/同义改写）→ merge=true，keep 填样本更多、描述更全的一方
2. 方向相反（上盘vs下盘、让球方vs受让方、追强vs防冷、诱上vs阻上）→ merge=false
3. 两者样本都充足且盈亏方向相反 → 视为经验上不同模式，merge=false
4. 仅名称/描述部分相似但模式不同 → merge=false
只输出严格 JSON，不要多余文字。"""


def llm_judge_pair(provider, a_name: str, a_entry: dict, b_name: str, b_entry: dict) -> dict:
    def _line(name, e):
        return (
            f"{name} | 描述: {e.get('desc','')[:120]} | "
            f"slugs: {', '.join(entry_slugs(e)[:5])} | "
            f"样本{e.get('total',0)} 盈亏{e.get('profit',0):+.0f} 命中率"
            f"{e.get('hit',0)/max(e.get('total',0)-e.get('push',0),1)*100:.0f}%"
        )
    user = f"因子A:\n{_line(a_name, a_entry)}\n\n因子B:\n{_line(b_name, b_entry)}\n\n" \
           '输出 JSON: {"merge": true|false, "keep": "A|B或null", "reason": "一句话"}'
    try:
        # 辅助判断（候选因子两两去重），走 fast 模型 + 关闭 thinking
        raw = provider.call_fast(JUDGE_SYSTEM, [{"role": "user", "content": user}], temperature=0.0)
        raw = re.sub(r"\[thinking\].*?\[/thinking\]\s*", "", raw, flags=re.S).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return {"merge": False, "keep": None, "reason": "LLM 输出无法解析"}
        return json.loads(raw[start:end + 1])
    except Exception as e:
        return {"merge": False, "keep": None, "reason": f"LLM 调用失败: {e}"}


def merge_entries(target: dict, source: dict, source_name: str) -> dict:
    """把 source 的历史并入 target，统计重算，aliases 累计。"""
    t_hist = target.get("history", []) or []
    s_hist = source.get("history", []) or []
    t_hist.extend(s_hist)
    t_hist.sort(key=lambda h: h.get("date", ""))
    target["history"] = t_hist
    recompute_stats(target)
    target_name = target.get("_name", "") or ""
    aliases = list(dict.fromkeys(
        target.get("aliases", []) + ([source_name] if source_name != target_name else [])
    ))
    target["aliases"] = aliases
    if not target.get("fac_id"):
        target["fac_id"] = source.get("fac_id")
    if not target.get("slugs") and source.get("slugs"):
        target["slugs"] = source["slugs"]
    return target


def find_slugs_in_reflections(role: str, factor_name: str) -> list[str]:
    """从角色反思文本提取该因子出现处的有效 slug（用于孤儿补定义）。"""
    ref_path = ROLES_DIR / role / "memory" / "reflection_memory.json"
    if not ref_path.exists():
        return []
    try:
        data = json.loads(ref_path.read_text(encoding="utf-8"))
        refs = data.get("reflections", []) if isinstance(data, dict) else data
    except Exception:
        return []
    found: list[str] = []
    for r in refs:
        text = r.get("reflection", "") or ""
        if factor_name not in text:
            continue
        for line in text.splitlines():
            if "有效slug" not in line and "key_slugs" not in line:
                continue
            seg = re.split(r"[：:]", line, maxsplit=1)
            if len(seg) < 2:
                continue
            cands = re.split(r"[,，、\s]+", seg[1].strip())
            for c in cands:
                c = c.strip()
                if c in VALID_SLUGS and c not in found:
                    found.append(c)
            if found:
                return found[:6]
    return found


def backfill_orphan_fac(role: str, name: str, entry: dict) -> bool:
    """孤儿因子（无 fac 文件）按反思 key_slugs 补写 fac 定义。"""
    fid = entry.get("fac_id") or fac_id_for(name)
    if (FACTORS_DIR / f"{fid}.json").exists():
        return False
    slugs = find_slugs_in_reflections(role, name)
    if not slugs:
        return False
    (FACTORS_DIR / f"{fid}.json").write_text(
        json.dumps({
            "id": fid,
            "slugs": slugs,
            "content": entry.get("desc", "")[:300],
            "updated_at": datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    entry["slugs"] = slugs
    entry["fac_id"] = fid
    return True


def audit(action: str, **kw) -> None:
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(), "action": action, **kw},
                           ensure_ascii=False) + "\n")


def save_roles(roles: dict[str, dict], changed: set[str]) -> None:
    for role in changed:
        mem_path = ROLES_DIR / role / "memory" / "factor_memory.json"
        bak = mem_path.with_name(mem_path.name + BAK_SUFFIX)
        if not bak.exists():
            shutil.copy2(mem_path, bak)
        fp = roles[role].get("factor_perf", {})
        clean = {
            n: {k: v for k, v in e.items() if k != "_name"}
            for n, e in fp.items()
        }
        roles[role]["factor_perf"] = clean
        mem_path.write_text(
            json.dumps(roles[role], ensure_ascii=False, indent=2), encoding="utf-8")


def induct_scope(scope_name: str, entries: dict[str, dict], role_of: dict[str, str],
                 provider, limit: int, dry_run: bool) -> dict:
    """对一组因子做归纳。entries: name→entry（可跨角色）；role_of: name→role。"""
    result = {"merged": 0, "llm_calls": 0, "fac_created": 0, "skipped": []}
    candidates = find_candidates(entries)
    # same_name 确定性合并优先
    for a, b, kind in candidates:
        if kind != "same_name":
            continue
        if a not in entries or b not in entries:
            continue
        ea, eb = entries[a], entries[b]
        keep, drop = (a, b) if ea.get("total", 0) >= eb.get("total", 0) else (b, a)
        if not dry_run:
            merge_entries(entries[keep], entries[drop], drop)
            del entries[drop]
            audit("merge_same", scope=scope_name, source=drop, target=keep,
                  role=role_of.get(keep))
        result["merged"] += 1
        print(f"  🔗 [{scope_name}] 同清洗名合并: {drop} → {keep}")

    # LLM 判重对（bit / name）
    judged = 0
    for a, b, kind in candidates:
        if kind == "same_name":
            continue
        if a not in entries or b not in entries:
            continue
        if judged >= limit:
            result["skipped"].append((a, b, kind))
            continue
        ea, eb = entries[a], entries[b]
        if dry_run:
            print(f"  🔎 [{scope_name}] 候选({kind}): {a} ↔ {b}")
            judged += 1
            continue
        verdict = llm_judge_pair(provider, a, ea, b, eb)
        judged += 1
        result["llm_calls"] += 1
        if not verdict.get("merge"):
            result["skipped"].append((a, b, kind, verdict.get("reason", "")))
            audit("keep_separate", scope=scope_name, a=a, b=b, kind=kind,
                  reason=verdict.get("reason", ""))
            continue
        keep = a if verdict.get("keep") == "A" else (b if verdict.get("keep") == "B" else
              (a if ea.get("total", 0) >= eb.get("total", 0) else b))
        drop = b if keep == a else a
        merge_entries(entries[keep], entries[drop], drop)
        del entries[drop]
        result["merged"] += 1
        audit("merge_llm", scope=scope_name, source=drop, target=keep, kind=kind,
              reason=verdict.get("reason", ""))
        print(f"  🔗 [{scope_name}] LLM合并({kind}): {drop} → {keep} ({verdict.get('reason','')})")

    # 孤儿补定义
    for name, entry in entries.items():
        role = role_of.get(name, "")
        if not role or (FACTORS_DIR / f"{entry.get('fac_id') or fac_id_for(name)}.json").exists():
            continue
        if dry_run:
            print(f"  📄 [{scope_name}] 孤儿因子待补定义: {name}")
            continue
        if backfill_orphan_fac(role, name, entry):
            result["fac_created"] += 1
            audit("create_fac", scope=scope_name, name=name,
                  slugs=entry.get("slugs", []), role=role)
            print(f"  📄 [{scope_name}] 补 fac 定义: {name} → {entry['slugs']}")
    return result


def main(argv: list = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不调 LLM 不写盘")
    ap.add_argument("--limit", type=int, default=30, help="单 scope 最多 LLM 判重次数")
    ap.add_argument("--roles", default="", help="只处理指定角色（逗号分隔），默认全部")
    args = ap.parse_args(argv)

    roles = load_roles()
    if args.roles:
        pick = {r.strip() for r in args.roles.split(",") if r.strip()}
        roles = {k: v for k, v in roles.items() if k in pick}
    if not roles:
        print("没有可处理的角色。")
        return

    if args.dry_run:
        provider = None
    else:
        from src.providers.deepseek import DeepSeekProvider
        provider = DeepSeekProvider()

    changed: set[str] = set()
    summary = {"merged": 0, "llm_calls": 0, "fac_created": 0, "scopes": 0}

    # ── 阶段 A：非 alpha 各自归纳（可并行，这里顺序执行）──
    for r in ALL_ROLES:
        if r in ALPHA_ROLES or r not in roles:
            continue
        entries = {n: {**e, "_name": n} for n, e in roles[r].get("factor_perf", {}).items()}
        if not entries:
            continue
        print(f"\n== {r} 归纳（{len(entries)} 个因子）==")
        res = induct_scope(r, entries, {n: r for n in entries}, provider, args.limit, args.dry_run)
        roles[r]["factor_perf"] = {n: e for n, e in entries.items() if n in entries}
        changed.add(r)
        for k in ("merged", "llm_calls", "fac_created"):
            summary[k] += res[k]
        summary["scopes"] += 1

    # ── 阶段 B（barrier）：非 alpha 完成后，alpha 池跨角色统一归纳（1 次进全库）──
    alpha_roles = [r for r in ALL_ROLES if r in roles and r in ALPHA_ROLES]
    if alpha_roles:
        pool: dict[str, dict] = {}
        role_of: dict[str, str] = {}
        removed: dict[str, list[str]] = {r: [] for r in alpha_roles}
        groups: dict[str, list[tuple[str, str, dict]]] = {}
        for r in alpha_roles:
            for name, entry in roles[r].get("factor_perf", {}).items():
                groups.setdefault(clean_name(name), []).append((r, name, entry))
        # 跨角色同清洗名：确定性合并（不调 LLM），保留样本最多者
        for cname, items in groups.items():
            if len(items) == 1:
                r, name, entry = items[0]
                entry["_name"] = name
                pool[name] = entry
                role_of[name] = r
                continue
            br, bname, bentry = max(items, key=lambda x: x[2].get("total", 0))
            bentry["_name"] = bname
            pool[bname] = bentry
            role_of[bname] = br
            for r, name, entry in items:
                if (r, name) == (br, bname):
                    continue
                merge_entries(bentry, entry, name)
                removed[r].append(name)
                print(f"  🔗 [alpha] 跨角色同清洗名合并: {name} → {bname}（{r} → {br}）")
        for r in alpha_roles:
            roles[r]["factor_perf"] = {
                n: e for n, e in roles[r]["factor_perf"].items() if n not in removed[r]
            }
        print(f"\n== alpha 归纳（{len(alpha_roles)} 狗合并，{len(pool)} 个因子）==")
        res = induct_scope("alpha", pool, role_of, provider, args.limit, args.dry_run)
        for r in alpha_roles:
            roles[r]["factor_perf"] = {
                n: e for n, e in pool.items() if role_of[n] == r and n in pool
            }
            changed.add(r)
        for k in ("merged", "llm_calls", "fac_created"):
            summary[k] += res[k]
        summary["scopes"] += 1

    if not args.dry_run and changed:
        save_roles(roles, changed)

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}完成: scopes={summary['scopes']} "
          f"合并 {summary['merged']}，LLM 判重 {summary['llm_calls']}，补定义 {summary['fac_created']}")
    print(f"审计: {AUDIT_LOG}")
    summary["dry_run"] = args.dry_run
    summary["changed_roles"] = sorted(changed)
    return summary


if __name__ == "__main__":
    main()
