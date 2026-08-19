#!/usr/bin/env python3
"""Export DSH JS storage domains back to Python data files.

Mirrors harness-plugin/storage.js exportToPython:
  ~/.dsh/storages/ds_roles.json            -> data/roles/<dog>/<dog>.json
  ~/.dsh/storages/ds_factors.json          -> data/roles/<dog>/memory/factor_memory.json
  ~/.dsh/storages/ds_reflections.json      -> data/roles/<dog>/memory/reflection_memory.json
  ~/.dsh/storages/ds_slugs.json            -> data/roles/<dog>/memory/slug_memory.json
  ~/.dsh/storages/ds_factor_registry.json  -> data/factors/fac_*.json

Default: only the 7 real dogs (skips _simwk/_sim0804/_snapshot/__mt_*/test_verify).
Reads local JSON only; no DSH runtime needed.
"""
import argparse
import json
import os
import sys
from pathlib import Path

DS_REAL_DOGS = ["alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗"]

TABLE_FILE_MAP = [
    ("ds_roles", "roles"),
    ("ds_factors", "factors"),
    ("ds_reflections", "reflections"),
    ("ds_slugs", "slugs"),
]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"  warning {path}: bad json: {e}", file=sys.stderr)
        return None


def read_table(storage_dir: Path, domain: str, table: str):
    doc = read_json(storage_dir / f"{domain}.json")
    if doc is None:
        return None
    tables = doc.get("tables", {}) if isinstance(doc, dict) else {}
    t = tables.get(table, {})
    return t if isinstance(t, dict) else {}


def atomic_write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="JS storage -> Python data files")
    ap.add_argument("--storage-dir", default=os.path.expanduser("~/.dsh/storages"))
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--dogs", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    storage_dir = Path(args.storage_dir).expanduser()
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else (Path(__file__).resolve().parent.parent / "data")
    roles_dir = data_dir / "roles"
    factors_dir = data_dir / "factors"

    print(f"storage_dir = {storage_dir}")
    print(f"data_dir    = {data_dir}")
    print(f"dry_run     = {args.dry_run}")

    tables = {domain: read_table(storage_dir, domain, table) for domain, table in TABLE_FILE_MAP}
    roles_table = tables.get("ds_roles")
    if roles_table is None:
        print("ERROR: ds_roles.json not found under storage_dir")
        sys.exit(1)

    if args.all:
        dogs = sorted(roles_table.keys())
    elif args.dogs:
        dogs = [d.strip() for d in args.dogs.split(",") if d.strip()]
    else:
        dogs = [d for d in DS_REAL_DOGS if d in roles_table]

    print(f"dogs = {dogs}")

    result = {
        "dogs": [], "rolesExported": 0, "factorsExported": 0,
        "reflectionsExported": 0, "slugsExported": 0, "registryExported": 0, "errors": [],
    }

    for dog in dogs:
        role = roles_table.get(dog)
        if not role:
            result["errors"].append(f"{dog}: no ds_roles record")
            continue
        fm = (tables.get("ds_factors") or {}).get(dog)
        rm = (tables.get("ds_reflections") or {}).get(dog)
        sm = (tables.get("ds_slugs") or {}).get(dog)

        if not args.dry_run:
            atomic_write_json(roles_dir / dog / f"{dog}.json", role)
            if fm:
                atomic_write_json(roles_dir / dog / "memory" / "factor_memory.json", fm)
            if rm:
                atomic_write_json(roles_dir / dog / "memory" / "reflection_memory.json", rm)
            if sm:
                atomic_write_json(roles_dir / dog / "memory" / "slug_memory.json", sm)

        result["dogs"].append(dog)
        result["rolesExported"] += 1
        if fm:
            result["factorsExported"] += 1
        if rm:
            result["reflectionsExported"] += 1
        if sm:
            result["slugsExported"] += 1
        print(f"  ok {dog}: factor_perf={len((fm or {}).get('factor_perf', {}))} "
              f"reflections={len((rm or {}).get('reflections', []))} slugs={len((sm or {}).get('slug_stats', {}))}")

    registry_table = read_table(storage_dir, "ds_factor_registry", "factors")
    if registry_table:
        for fac_id, defn in registry_table.items():
            if not isinstance(defn, dict):
                continue
            if not args.dry_run:
                atomic_write_json(factors_dir / f"{fac_id}.json", defn)
            result["registryExported"] += 1
        print(f"  ok ds_factor_registry: {result['registryExported']} fac files")
    else:
        print("  - ds_factor_registry.json not found, skip")

    print("\n===== result =====")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        for e in result["errors"]:
            print(f"  - {e}")
        sys.exit(2)
    print("DONE" if not args.dry_run else "DRY-RUN DONE")


if __name__ == "__main__":
    main()
