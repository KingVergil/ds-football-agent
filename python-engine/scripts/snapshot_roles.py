#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""roles + factors 快照/回退（跑因子归纳等变更前用，可回退到归纳之前）。

用法:
  python scripts/snapshot_roles.py snapshot <tag>   # 备份 data/roles + data/factors → data/backups/<tag>/
  python scripts/snapshot_roles.py restore <tag>    # 从 data/backups/<tag>/ 还原（先删当前再复制）
  python scripts/snapshot_roles.py list             # 列出全部备份
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "data" / "backups"


def snapshot(tag: str) -> int:
    dst = BACKUP / tag
    if dst.exists():
        print(f"❌ 备份已存在: {dst}（换个 tag 或先手动处理）")
        return 1
    for name in ("roles", "factors"):
        src = ROOT / "data" / name
        if src.exists():
            shutil.copytree(src, dst / name)
    print(f"✅ 快照完成: {dst}")
    return 0


def restore(tag: str) -> int:
    src = BACKUP / tag
    if not src.exists():
        print(f"❌ 备份不存在: {src}")
        return 1
    for name in ("roles", "factors"):
        s = src / name
        d = ROOT / "data" / name
        if s.exists():
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d)
    print(f"✅ 已还原: {src}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot" and len(sys.argv) > 2:
        return snapshot(sys.argv[2])
    if cmd == "restore" and len(sys.argv) > 2:
        return restore(sys.argv[2])
    if cmd == "list":
        for d in sorted(BACKUP.iterdir()) if BACKUP.exists() else []:
            print(d.name)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
