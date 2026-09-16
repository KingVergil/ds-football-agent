#!/usr/bin/env python3
"""
一次性应用 factor_cleanup_dryrun 的清理动作（不是日常自动飞轮）。

用法:
  python3 scripts/factor_cleanup_apply.py --report /tmp/factor_cleanup_report.json --apply

只改 roles/<dog>/memory/factor_memory.json 的 status 与清理标记，
不改 factors/* 定义，不改订单/资金。每个文件应用前会备份为:
  factor_memory.json.bak.factor_cleanup_<timestamp>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROLES_DIR = ROOT / "data" / "roles"

ACTION_STATUS = {
    "promote": "active",
    "revive": "active",
    "observe": "testing",
    "dormant": "dormant",
    "retire": "retired",
}


def load_report(report: Path) -> list[dict]:
    data = json.loads(report.read_text(encoding="utf-8"))
    return data.get("rows", []) if isinstance(data, dict) else data


def apply_actions(rows: list[dict], apply: bool) -> dict:
    changed = []
    by_role = {}
    for row in rows:
        role = row["role"]
        factor = row["factor"]
        action = row["action"]
        if action in ("keep",):
            continue
        path = ROLES_DIR / role / "memory" / "factor_memory.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        fp = data.get("factor_perf", {})
        entry = fp.get(factor)
        if not isinstance(entry, dict):
            continue

        old_status = entry.get("status", "active")
        if action == "archive":
            # 归档：不再进任何 prompt/主区；用 dormant + cleanup_action 标记，
            # 避免把小样本噪声误当成「已证伪模式」。
            new_status = "dormant"
            entry["cleanup_action"] = "archive"
        else:
            new_status = ACTION_STATUS.get(action, old_status)
            entry["cleanup_action"] = action

        entry["status"] = new_status
        entry["cleanup_at"] = datetime.now().isoformat(timespec="seconds")

        changed.append({
            "role": role,
            "factor": factor,
            "action": action,
            "old_status": old_status,
            "new_status": new_status,
        })
        by_role[role] = by_role.get(role, 0) + 1

        if apply:
            bak = path.with_name(
                f"factor_memory.json.bak.factor_cleanup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            if not bak.exists():
                shutil.copy2(path, bak)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return {"changed": changed, "by_role": by_role}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="factor_cleanup_report.json 路径")
    ap.add_argument("--apply", action="store_true", help="真正写回；不传则只预览")
    args = ap.parse_args(argv)

    rows = load_report(Path(args.report))
    result = apply_actions(rows, apply=args.apply)
    changed = result["changed"]
    print(f"报告因子数: {len(rows)}  需变更: {len(changed)}  mode={'APPLY' if args.apply else 'PREVIEW'}")
    for role, n in sorted(result["by_role"].items()):
        print(f"  {role}: {n} 个状态变更")
    for c in changed:
        print(
            f"  [{c['action']}] {c['role']}/{c['factor']}: "
            f"{c['old_status']} -> {c['new_status']}"
        )
    if not args.apply:
        print("\n未写回。确认无误后加 --apply 执行。")
    else:
        print("\n已写回，并在每个 factor_memory.json 旁生成备份文件。")


if __name__ == "__main__":
    main()
