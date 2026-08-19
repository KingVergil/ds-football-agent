#!/usr/bin/env python3
"""阶段1迁移：为所有角色 factor_memory 条目回填 fac_id + slugs。

规则：
  - fac_id = fac_{name.lower().replace(' ','_')[:40]}（与 record/save_factor 一致）
  - slugs 从 data/factors/fac_*.json 读取；文件缺失则为孤儿（fac_id 仍写入，slugs 留空，由归纳补定义）

用法: python scripts/backfill_factor_fac_id.py
"""

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROLES_DIR = ROOT / "data" / "roles"
FACTORS_DIR = ROOT / "data" / "factors"
BAK_SUFFIX = ".bak.20260805_phase1"


def fac_id_for(name: str) -> str:
    return f"fac_{name.lower().replace(' ','_')[:40]}"


def load_slugs(fac_id: str) -> list[str]:
    p = FACTORS_DIR / f"{fac_id}.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("slugs", [])
    except Exception:
        return []


def main() -> None:
    total = matched = orphan = 0
    changed_files = 0
    for role_dir in sorted(ROLES_DIR.iterdir()):
        if not role_dir.is_dir() or "_sim" in role_dir.name or role_dir.name.startswith("__"):
            continue
        mem_path = role_dir / "memory" / "factor_memory.json"
        if not mem_path.exists():
            continue
        data = json.loads(mem_path.read_text(encoding="utf-8"))
        fp = data.get("factor_perf", {})
        dirty = False
        for name, entry in fp.items():
            total += 1
            fid = fac_id_for(name)
            entry.setdefault("fac_id", fid)
            if not entry.get("slugs"):
                slugs = load_slugs(fid)
                entry["slugs"] = slugs
                if slugs:
                    matched += 1
                else:
                    orphan += 1
            else:
                matched += 1
            dirty = True
        if dirty:
            bak = mem_path.with_name(mem_path.name + BAK_SUFFIX)
            if not bak.exists():
                shutil.copy2(mem_path, bak)
            mem_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            changed_files += 1
            print(f"  ✅ {role_dir.name}: {len(fp)} 条回填")

    print(f"\n总计 {total} 条: 有 slugs {matched}（{matched/max(total,1)*100:.0f}%），"
          f"孤儿（无 fac 定义）{orphan}（{orphan/max(total,1)*100:.0f}%）")
    print(f"修改文件 {changed_files} 个，备份后缀 {BAK_SUFFIX}")


if __name__ == "__main__":
    main()
