#!/usr/bin/env python3
"""一次性迁移：修复因子记忆的统计口径。

背景：quarter-ball 盘口结算把赢半/输半/真走水统一记为 hit=None，
导致因子统计把赢半/输半当“走水”（push），却仍累加其 profit，
出现“命中0 利润+3760”这类矛盾条目；同时 LLM 归因产生带引号/emoji
的脏因子名，把同一因子拆成多个 key。

本脚本（确定性、不调 LLM）：
  1. 备份 factor_memory.json → .bak.<timestamp>
  2. 清洗因子名（去引号/emoji/括号）
  3. 清洗后重名因子合并（history 合并、状态取优）
  4. history 归一化：hit=None 且 profit>0 → 0.5(赢半)；profit<0 → -0.5(输半)
  5. 按 history 重算 total/hit/miss/push/profit/total_return/first_seen/last_seen

用法: python scripts/fix_factor_stats.py [--dry-run]
"""

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLES_DIR = ROOT / "data" / "roles"


def clean_name(name: str) -> str:
    n = (name or "").strip()
    n = n.strip('"\'“”`')
    n = re.sub(r"[（(][^）)]*[）)]", "", n).strip()
    n = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]", "", n).strip()
    n = re.sub(r"\s+", " ", n)
    return n


def normalize_hit(hit, profit: float):
    if hit is None and profit > 0:
        return 0.5          # 赢半
    if hit is None and profit < 0:
        return -0.5         # 输半
    return hit              # True / False / None(真走水)


def recompute(f: dict) -> dict:
    hist = sorted(f.get("history", []), key=lambda h: h.get("date", ""))
    total = len(hist)
    hit = miss = push = 0.0
    profit = 0.0
    total_return = 0.0
    first_seen = None
    last_seen = None
    for h in hist:
        hv = h.get("hit")
        if hv is True or hv == 0.5:
            hit += 1.0 if hv is True else 0.5
        elif hv is False or hv == -0.5:
            miss += 1.0 if hv is False else 0.5
        else:
            push += 1
        profit += h.get("profit", 0.0)
        total_return += h.get("return_ratio", 0.0)
        d = h.get("date", "")
        if d:
            if first_seen is None or d < first_seen:
                first_seen = d
            if last_seen is None or d > last_seen:
                last_seen = d
    f["total"] = total
    f["hit"] = hit
    f["miss"] = miss
    f["push"] = push
    f["profit"] = round(profit, 2)
    f["total_return"] = round(total_return, 4)
    f["first_seen"] = first_seen or f.get("first_seen", "?")
    f["last_seen"] = last_seen or f.get("last_seen", "?")
    f["history"] = hist
    return f


def status_priority(status: str) -> int:
    return {"active": 2, "dormant": 1, "retired": 0}.get(status, 1)


def main():
    dry = "--dry-run" in sys.argv
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    total_factors = 0
    cleaned = 0
    merged = 0
    half_fixed = 0

    for mem_path in sorted(ROLES_DIR.glob("*/memory/factor_memory.json")):
        role = mem_path.parent.parent.name
        try:
            data = json.loads(mem_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[skip] {role}: {e}")
            continue
        fp = data.get("factor_perf", {})
        if not fp:
            continue

        # 1) 清洗 + 合并重名
        merged_fp: dict[str, dict] = {}
        for raw_name, f in fp.items():
            name = clean_name(raw_name)
            if not name:
                cleaned += 1
                continue
            if name != raw_name:
                cleaned += 1
            if name in merged_fp:
                t = merged_fp[name]
                t["history"] = (t.get("history") or []) + (f.get("history") or [])
                if f.get("desc") and not t.get("desc"):
                    t["desc"] = f["desc"]
                if status_priority(f.get("status", "active")) > status_priority(t.get("status", "active")):
                    t["status"] = f.get("status", "active")
                t.setdefault("aliases", []).append(raw_name)
                if t.setdefault("aliases", []):
                    pass
                merged += 1
            else:
                merged_fp[name] = dict(f)

        # 2) history 归一化 + 3) 重算
        for name, f in merged_fp.items():
            for h in f.get("history", []):
                nh = normalize_hit(h.get("hit"), h.get("profit", 0.0))
                if nh != h.get("hit"):
                    h["hit"] = nh
                    half_fixed += 1
            merged_fp[name] = recompute(f)
            total_factors += 1

        data["factor_perf"] = merged_fp
        data["updated_at"] = datetime.now().isoformat()

        if dry:
            print(f"[dry] {role}: {len(fp)} → {len(merged_fp)} 因子, "
                  f"清洗{cleaned} 合并{merged} 半盘{half_fixed} (累计)")
            continue

        bak = mem_path.with_name(mem_path.name + f".bak.{ts}")
        shutil.copy2(mem_path, bak)
        mem_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] {role}: {len(fp)} → {len(merged_fp)} 因子 | 备份 {bak.name}")

    print(f"\n总计: {total_factors} 因子, 清洗 {cleaned}, 合并 {merged}, 半盘修正 {half_fixed}"
          f"{' (dry-run 未写盘)' if dry else ''}")


if __name__ == "__main__":
    main()
