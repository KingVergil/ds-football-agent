"""
角色注册表派生 + 同步（Python 侧配置权威）。

设计定稿（2026-08-18 grill 定稿）：
  - persona 唯一源 = roles/<狗>/persona.md；
  - limits 结构化存 roles/<狗>/<狗>.json，LangGraph 优先读文件、缺省用代码默认；
  - enabled 控制是否进全量默认列表（分析/结算/回放）；新建狗默认观察期（enabled=false）；
  - dogs.json 只存结构化配置（name/scope/initial_capital/alpha_mode/limits/enabled/emoji/颜色）。

用法:
  python -m src.role_registry              # 打印 live 默认狗列表（每行一个）
  python -m src.role_registry all          # 打印全部狗（本地已有角色的内置默认狗 + 注册表 + 串关2狗）
  python -m src.role_registry alpha        # 打印 alpha 狗（从角色文件 alpha_mode 派生）
  python -m src.role_registry sync         # 迁移：补建缺失角色 + 注册表旧 persona 迁移到 persona.md
  python -m src.role_registry sync --dry-run

公开仓库不携带任何狗数据：内置默认狗只在本地 roles/<狗>/<狗>.json 存在时才计入，
全量/群体操作（analyze-all / settle-all / factor-dedup）的狗列表完全由本地角色派生。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ROLES_DIR = Path(os.environ.get("DS_ROLES_ROOT") or DATA / "roles")

DEFAULT_AGENTS = ["alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗"]
EXTRA_AGENTS = ["串关2狗"]  # 参与结算/归纳，不进默认分析全量
FALLBACK_ALPHA = {"alpha2狗", "alpha狗", "均注狗"}


def _read_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _write_json(path: Path, data, atomic: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if atomic:
        tmp = path.with_name(f"{path.name}.tmp-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    else:
        path.write_text(payload, encoding="utf-8")


def registry_dogs() -> list[dict]:
    """读 dsh 注册表 dogs.json（缺失/损坏返回 []）。"""
    raw = _read_json(DATA / "dogs.json")
    return [d for d in (raw or []) if isinstance(d, dict) and d.get("name")]


def all_agents(include_extra: bool = True) -> list[str]:
    """全部狗：本地存在的内置默认狗 + 注册表狗（去重）+ 可选串关2狗（本地存在才计入）。"""
    out = [n for n in DEFAULT_AGENTS if (ROLES_DIR / n / f"{n}.json").exists()]
    for d in registry_dogs():
        if d["name"] not in out:
            out.append(d["name"])
    if include_extra:
        for n in EXTRA_AGENTS:
            if n not in out and (ROLES_DIR / n / f"{n}.json").exists():
                out.append(n)
    return out


def role_status(name: str) -> str:
    """狗状态：live（线上在用）/ sandbox（沙箱待转正）/ archived（归档）。enabled 是旧字段，迁移后废弃。"""
    role = _read_json(ROLES_DIR / name / f"{name}.json")
    if role and isinstance(role, dict):
        st = role.get("status")
        if st in ("live", "sandbox", "archived"):
            return st
        return "live" if role.get("enabled", True) else "sandbox"
    for d in registry_dogs():
        if d.get("name") == name and d.get("status") in ("live", "sandbox", "archived"):
            return d["status"]
    return "live"


def live_agents() -> list[str]:
    """全量默认列表 = status==live（只统计本地已有角色文件的狗；沙箱/归档狗不默认进）。"""
    out = [n for n in DEFAULT_AGENTS if (ROLES_DIR / n / f"{n}.json").exists() and role_status(n) == "live"]
    for d in registry_dogs():
        if d.get("status") == "live" and d["name"] not in out:
            out.append(d["name"])
    return out


def enabled_agents() -> list[str]:
    """兼容旧调用（= live_agents）。"""
    return live_agents()


def alpha_agents() -> list[str]:
    """从 roles/<狗>/<狗>.json 的 alpha_mode 派生（缺文件回退默认集合）。"""
    alphas = set()
    for name in all_agents(include_extra=False):
        role = _read_json(ROLES_DIR / name / f"{name}.json")
        if role is None:
            if name in FALLBACK_ALPHA:
                alphas.add(name)
            continue
        if role.get("alpha_mode"):
            alphas.add(name)
    return sorted(alphas)


def role_limits(agent_name: str):
    """读 roles/<狗>/<狗>.json 的 limits（缺省 None = 走代码默认）。"""
    role = _read_json(ROLES_DIR / agent_name / f"{agent_name}.json")
    if role and isinstance(role.get("limits"), dict):
        return role["limits"]
    return None


def sync_from_registry(dry_run: bool = False) -> dict:
    """
    迁移入口（幂等）：
      1) 注册表旧 persona 字段 → roles/<狗>/persona.md（文件缺失/空才写）；
      2) 缺 <狗>.json 的角色按注册表补建（资金/订单不动已有的）；
      3) 注册表条目规范化（补 limits/enabled 默认、剥掉 persona），原子重写 dogs.json。
    返回 {created: [...], registry_updated: bool}。
    """
    dogs = registry_dogs()
    created: list[str] = []
    new_dogs: list[dict] = []
    for d in dogs:
        name = d.get("name", "")
        if not name:
            continue
        entry = dict(d)
        legacy_persona = entry.pop("persona", None)
        role_dir = ROLES_DIR / name
        persona_path = role_dir / "persona.md"
        role_path = role_dir / f"{name}.json"
        role_dir.mkdir(parents=True, exist_ok=True)

        # persona：旧字段迁移到 persona.md（唯一人设源），随后从注册表剥掉
        if isinstance(legacy_persona, str) and legacy_persona.strip():
            if not persona_path.exists() or not persona_path.read_text(encoding="utf-8").strip():
                if not dry_run:
                    persona_path.write_text(legacy_persona.strip() + "\n", encoding="utf-8")
                created.append(f"{name}/persona.md")

        # <狗>.json 补建（已存在不动，保留资金/订单）
        if not role_path.exists():
            if not dry_run:
                initial = float(entry.get("initial_capital") or 10000)
                _write_json(role_path, {
                    "name": name,
                    "capital": initial,
                    "initial_capital": initial,
                    "system_prompt_name": "baseline-v1",
                    "alpha_mode": bool(entry.get("alpha_mode")),
                    "cross_factor_exclude": [],
                    "limits": entry.get("limits") or {"max_exposure_pct": 40},
                    "scope": entry.get("scope", "jc"),
                    "enabled": bool(entry.get("enabled")),
                    "status": entry.get("status") or ("live" if entry.get("enabled") else "sandbox"),
                    "orders": [],
                    "updated_at": datetime.now().isoformat(),
                })
            created.append(f"{name}/{name}.json")
        elif not dry_run:
            # 迁移：已有角色文件缺 status → 由 enabled 派生（enabled 后续废弃）
            data = _read_json(role_path)
            if data and isinstance(data, dict) and not data.get("status"):
                data["status"] = "live" if data.get("enabled", True) else "sandbox"
                _write_json(role_path, data)

        # 注册表条目规范化：剥 persona、补 limits/enabled 默认
        entry.setdefault("limits", {"max_exposure_pct": 40})
        entry.setdefault("enabled", False)
        entry.setdefault("status", "live" if entry.get("enabled") else "sandbox")
        new_dogs.append(entry)

    registry_updated = new_dogs != dogs
    if not dry_run and registry_updated:
        _write_json(DATA / "dogs.json", new_dogs)
    return {"created": created, "registry_updated": registry_updated}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "enabled"
    if cmd == "all":
        for n in all_agents():
            print(n)
        return 0
    if cmd == "alpha":
        for n in alpha_agents():
            print(n)
        return 0
    if cmd == "live":
        for n in live_agents():
            print(n)
        return 0
    if cmd == "sync":
        dry_run = "--dry-run" in argv
        res = sync_from_registry(dry_run=dry_run)
        print("迁移结果:")
        print(f"  补建角色文件: {len(res['created'])} 个 → {', '.join(res['created']) or '—'}")
        print(f"  注册表规范化重写: {'是' if res['registry_updated'] else '否（无需变更）'}")
        if dry_run:
            print("  （--dry-run，未写盘）")
        return 0
    # 默认：enabled 默认列表
    for n in enabled_agents():
        print(n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
