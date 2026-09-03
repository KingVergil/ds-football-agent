"""把 mined 出的北单因子库做真·归纳去重（same_name + LLM 判重），输出精简因子库。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-fac", action="store_true", help="跳过孤儿 fac 定义补写")
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    fp = data.get("factor_perf", {})
    print(f"输入因子数: {len(fp)}")

    entries = {}
    for name, e in fp.items():
        e = dict(e)
        e["_name"] = name
        entries[name] = e
    role_of = {name: "bc狗" for name in entries}

    from src.factor_induction import induct_scope
    from src.providers.deepseek import DeepSeekProvider

    provider = None if args.dry_run else DeepSeekProvider()
    res = induct_scope("bc狗", entries, role_of, provider, args.limit,
                       dry_run=args.dry_run)
    print(f"归纳结果: merged={res['merged']} llm_calls={res['llm_calls']} "
          f"fac_created={res['fac_created']} 剩余={len(entries)}")

    if not args.dry_run:
        out = {"updated_at": "2026-08-28", "factor_perf": entries}
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
