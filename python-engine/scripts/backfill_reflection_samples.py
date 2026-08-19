#!/usr/bin/env python3
"""回填历史反思的 sample_count：从反思文本提取场次词，低样本（<3 场）自动打标。

背景：
  add_reflection 现在会在写入时记录 sample_count（见 src/memory.py），
  但历史反思没有该字段。本脚本一次性扫描所有角色的 reflection_memory.json，
  从文本中提取场次词（"两场"/"3场"等），若 <3 则补上 sample_count 字段，
  并在反思文本末尾追加低样本警告（与 add_reflection 的格式一致）。

规则：
  - 已有 sample_count 字段 → 跳过
  - 文本中能提取出场次词且 <3 → 补 sample_count + 文本标记
  - 其余（无法提取 / >=3 场）→ 不动

用法: python scripts/backfill_reflection_samples.py [--dry-run]
"""

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
ROLES_DIR = ROOT / "data" / "roles"
BAK_SUFFIX = ".bak.reflection_samples"

NUM_MAP = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def extract_sample_count(text: str) -> Optional[int]:
    """从反思文本提取涉及的比赛场数（'两场'/'3场'等），无法判断返回 None。"""
    if not text:
        return None
    m = re.search(r'([一两二三四五六七八九十\d]+)\s*场', text)
    if not m:
        return None
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    return NUM_MAP.get(raw)


def low_sample_mark(n: int) -> str:
    return (
        f"\n⚠️ 低样本（仅 {n} 场），结论待验证，"
        "不得作为重注/铁律依据"
    )


def process_file(path: Path, dry_run: bool) -> tuple[int, int]:
    """返回 (已修复条数, 跳过条数)"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️ 读取失败 {path}: {e}")
        return 0, 0

    refs = data.get("reflections", [])
    if not isinstance(refs, list):
        print(f"  ⚠️ 格式异常（reflections 非 list）: {path}")
        return 0, 0

    fixed = skipped = 0
    for r in refs:
        if not isinstance(r, dict):
            continue
        if r.get("sample_count") is not None:
            skipped += 1
            continue
        text = r.get("reflection", "") or ""
        n = extract_sample_count(text)
        if n is None or n >= 3:
            skipped += 1
            continue
        if "低样本" in text:
            # 文本已带标记但缺字段 → 只补字段
            r["sample_count"] = n
            fixed += 1
            continue
        r["sample_count"] = n
        r["reflection"] = text + low_sample_mark(n)
        fixed += 1

    if fixed and not dry_run:
        # 先备份再写回（保留原结构）
        bak = path.with_name(path.name + BAK_SUFFIX)
        if not bak.exists():
            shutil.copy2(path, bak)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return fixed, skipped


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    files = sorted(ROLES_DIR.glob("*/memory/reflection_memory.json"))
    print(f"扫描 {len(files)} 个反思文件（{'dry-run，不写盘' if dry_run else '将写盘'}）")

    total_fixed = total_skipped = 0
    for path in files:
        fixed, skipped = process_file(path, dry_run)
        total_fixed += fixed
        total_skipped += skipped
        if fixed:
            flag = "  [dry-run]" if dry_run else ""
            print(f"  {path.relative_to(ROOT)}: 修复 {fixed} 条{flag}")
    print(f"完成: 修复 {total_fixed} 条，跳过 {total_skipped} 条")


if __name__ == "__main__":
    main()
