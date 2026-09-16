"""回填因子库的「路径」字段：样本级 unit_cost（注数基数）与因子级 type。

## 为什么

`factor_memory.json` 里两条信息缺得很厉害（2026-09-11 调查）：

- **type**：多条离线/回放语料（`roles/*/history/*__pre-factor/`、`data/beidan_walkforward/*`、
  `data/beidan_factor_test/library/*`）**100% 缺** → `factor_select` 回落 `directional`
  → 重跑/回放时**波动路径被静默关闭**；
- **unit_cost**：老样本没记注数基数，而北单「单选腿输 = −1」「全包腿输 = −3」口径不同，
  混在一根 `return_ratio` 上会让 `avg_sp` 反推系统性偏低 (3−1)/0.65 ≈ **3.08**。

## 只认证据，不猜

| 证据 | 结论 |
|---|---|
| 样本自带 `unit_cost` | 直接用 |
| **订单记录**（`<角色>/<狗>.json` 的 `orders[].ticket_legs[]` 里该 lid 的 `picks` 数） | 1 个方向 → 单选腿（1 注）；3 个方向 → 全包腿（3 注） |
| 历史里有 `return_ratio ≤ −2.5` 的样本 | 全包腿口径 → `unit_cost=3` |
| 历史里有 `return_ratio ∈ (−1.5, −0.5]` 的样本 | 单注口径 → `unit_cost=1` |
| 都无法判定 | **不动**（宁缺勿错：avg_sp 显示为空，而不是显示一个错数） |

**注数基数是「样本级」不是「因子级」**：实测同一因子的历史会混着 1 注与 3 注样本
（例：`亚盘盘口反复横跳` = [1, 3, 3]），所以只能逐样本落字段，不能按因子类型一刀切。
`type` 只在证据一致时补（全 3 → volatility、全 1 → directional；混合 → 保持不动并计入 mixed）。

已存在的 `type` 不会被覆盖（只补缺）。

## 用法

```bash
cd python-engine
python3 -m scripts.backfill_factor_paths                      # dry-run：只打印将改动的统计
python3 -m scripts.backfill_factor_paths --apply              # 写盘（就地 .bak.<ts> 备份）
python3 -m scripts.backfill_factor_paths --path <factor_memory.json> --apply
python3 -m scripts.backfill_factor_paths --dir data/beidan_walkforward --apply   # 递归找所有因子库
```
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.factor_select import (  # noqa: E402
    UNIT_COST_COVER,
    UNIT_COST_DIRECTIONAL,
    sample_unit_cost,
)


def _infer_from_history(hist: list[dict]) -> tuple[float | None, str | None, str]:
    """→ (unit_cost, type, 证据说明)。只认确定性证据，不做类型猜测。"""
    for h in reversed(hist or []):
        uc = h.get("unit_cost")
        if uc:
            try:
                b = float(uc)
            except (TypeError, ValueError):
                continue
            return b, ("volatility" if b >= 3 else "directional"), "样本自带 unit_cost"
    for h in (hist or []):
        try:
            rr = float(h.get("return_ratio"))
        except (TypeError, ValueError):
            continue
        if rr <= -2.5:
            return UNIT_COST_COVER, "volatility", f"输样本 rr={rr:+.2f}（全包 3 注口径）"
        if -1.5 < rr <= -0.5:
            return UNIT_COST_DIRECTIONAL, "directional", f"输样本 rr={rr:+.2f}（单注口径）"
    return None, None, "无证据（全为命中样本）"


def _orders_basis(role_json: Path) -> dict[str, float]:
    """从角色订单解析 `lota_id → 注数基数`（票里该腿的 picks 数：1=单选，N=全包）。"""
    out: dict[str, float] = {}
    try:
        role = json.loads(role_json.read_text(encoding="utf-8"))
    except Exception:
        return out
    for o in (role.get("orders") or []):
        for l in (o.get("ticket_legs") or o.get("legs") or []):
            lid = l.get("lota_id")
            if not lid:
                continue
            picks = l.get("picks") or ([l["pick"]] if l.get("pick") else [])
            if picks:
                out.setdefault(lid, float(len(picks)))
    return out


def _role_json_for(memory_path: Path) -> Path | None:
    """factor_memory.json 同角色目录下的 <狗>.json（历史快照目录里通常没有）。"""
    role_dir = memory_path.parent.parent
    if not role_dir.is_dir():
        return None
    for p in sorted(role_dir.glob("*.json")):
        if p.name in ("parlay.json",):
            continue
        return p
    return None


def backfill_file(path: Path, apply: bool = False,
                  basis_by_lid: dict[str, float] | None = None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"path": str(path), "error": str(e)}
    fp = data.get("factor_perf") if isinstance(data, dict) else None
    if not isinstance(fp, dict):
        return {"path": str(path), "error": "无 factor_perf"}
    basis_by_lid = basis_by_lid or {}

    stat = {"factors": len(fp), "type_filled": 0, "unit_filled": 0, "unknown": 0,
            "history_samples": 0, "existing_type": 0, "from_orders": 0,
            "mixed": 0, "examples": []}
    for name, entry in fp.items():
        hist = entry.get("history") or []
        stat["history_samples"] += len(hist)
        if entry.get("type"):
            stat["existing_type"] += 1

        # 1) 逐样本落 unit_cost：订单证据优先，其次因子级证据
        fallback, ftype, why = _infer_from_history(hist)
        bases: list[float] = []
        added = 0
        for h in hist:
            if h.get("unit_cost") is not None:
                try:
                    bases.append(float(h["unit_cost"]))
                except (TypeError, ValueError):
                    pass
                continue
            b = basis_by_lid.get(h.get("lota_id") or "")
            if b is not None:
                h["unit_cost"] = float(b)
                bases.append(float(b))
                added += 1
                stat["from_orders"] += 1
            elif fallback is not None:
                h["unit_cost"] = float(fallback)
                bases.append(float(fallback))
                added += 1
        stat["unit_filled"] += added

        if not bases:
            stat["unknown"] += 1
            if len(stat["examples"]) < 5:
                stat["examples"].append({"factor": name, "type": entry.get("type"),
                                         "unit_cost": None, "why": why,
                                         "samples": len(hist)})
            continue

        # 2) type：只在证据一致时补（混合就不动）
        uniq = {b for b in bases}
        if not entry.get("type"):
            if uniq == {UNIT_COST_COVER}:
                entry["type"] = "volatility"
                stat["type_filled"] += 1
            elif uniq == {UNIT_COST_DIRECTIONAL}:
                entry["type"] = "directional"
                stat["type_filled"] += 1
            else:
                stat["mixed"] += 1
        if len(stat["examples"]) < 5:
            stat["examples"].append({"factor": name, "type": entry.get("type"),
                                     "unit_cost": (sorted(uniq)[0] if len(uniq) == 1 else None),
                                     "bases": sorted(uniq) if len(uniq) > 1 else None,
                                     "why": ("订单解析" if stat["from_orders"]
                                             else why),
                                     "samples": len(hist)})

    if apply and (stat["type_filled"] or stat["unit_filled"]):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak.{stamp}"))
        data["path_backfilled_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        stat["backup"] = str(path.with_suffix(path.suffix + f".bak.{stamp}"))
    stat["path"] = str(path)
    return stat



# ── 波动路径粗筛基线回填（vol_base：当日高波动场次占比）──────────────

def _day_high_vol_baseline(day: str, matches_dir: Path) -> float | None:
    """当日「高波动场次占比」——与 BeidanParlayDog._day_high_vol_baseline 同口径。

    高波动 = 开奖 SP ≥ 覆盖成本线（3/0.65≈4.615）。样本不足 5 场 → None。
    """
    from src.beidan_parlay_dog import BeidanParlayDog
    from src.environment import football_day_calendar_dates, get_football_day
    try:
        start_d = date.fromisoformat(day)
    except ValueError:
        return None
    start, end = get_football_day(start_d)
    sps: list[float] = []
    for cd in football_day_calendar_dates(start_d):
        p = Path(matches_dir) / f"{cd}.json"
        if not p.exists():
            continue
        try:
            ms = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in ms or []:
            if not m.get("beidan_number"):
                continue
            mt = m.get("match_time", "")
            if not (start <= mt <= end):
                continue
            try:
                v = float((m.get("beidan_info") or {}).get("spvalue") or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                sps.append(v)
    if len(sps) < 5:
        return None
    thr = BeidanParlayDog.HIGH_VOL_SP
    return sum(1 for v in sps if v >= thr) / len(sps)


def backfill_vol_base(path: Path, matches_dir: Path, apply: bool = False) -> dict:
    """给波动因子的历史样本补 `vol_base`（只补缺，不覆盖）。

    只对**北单串货因子**生效（判据：样本带 unit_cost，或因子标了 path=beidan）——
    其它狗的因子一律不动。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"path": str(path), "error": str(e)}
    fp = data.get("factor_perf") if isinstance(data, dict) else None
    if not isinstance(fp, dict):
        return {"path": str(path), "error": "无 factor_perf"}

    cache: dict[str, float | None] = {}
    stat = {"factors": 0, "samples": 0, "filled": 0, "skipped_non_beidan": 0,
            "no_baseline": 0, "examples": []}
    for name, entry in fp.items():
        hist = entry.get("history") or []
        if not hist:
            continue
        is_beidan = (str(entry.get("path") or "") == "beidan"
                     or any(h.get("unit_cost") is not None for h in hist))
        if not is_beidan:
            stat["skipped_non_beidan"] += 1
            continue
        stat["factors"] += 1
        for h in hist:
            if h.get("vol_base") is not None:
                continue
            stat["samples"] += 1
            d = (h.get("date") or "")[:10]
            if d not in cache:
                cache[d] = _day_high_vol_baseline(d, matches_dir) if d else None
            b = cache[d]
            if b is None:
                stat["no_baseline"] += 1
                continue
            h["vol_base"] = round(float(b), 4)
            stat["filled"] += 1
        if len(stat["examples"]) < 5 and entry.get("type") == "volatility":
            bases = [h.get("vol_base") for h in hist if h.get("vol_base") is not None]
            if bases:
                stat["examples"].append({"factor": name, "n": len(bases),
                                         "base_mean": round(sum(bases) / len(bases), 3)})
    if apply and stat["filled"]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak.{stamp}"))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        stat["backup"] = str(path.with_suffix(path.suffix + f".bak.{stamp}"))
    stat["path"] = str(path)
    return stat

def _find_targets(args) -> list[Path]:
    if args.path:
        return [Path(args.path)]
    base = Path(args.dir) if args.dir else (ROOT / "data" / "roles")
    if base.is_file():
        return [base]
    if not base.is_dir():
        return []
    if base.name == "memory" and (base / "factor_memory.json").exists():
        return [base / "factor_memory.json"]
    return sorted(base.rglob("factor_memory.json"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填因子库 type / unit_cost（只认证据）")
    ap.add_argument("--path", help="单个 factor_memory.json")
    ap.add_argument("--dir", help="递归目录（默认 data/roles）")
    ap.add_argument("--orders", help="指定订单来源角色 json（缺省自动找同角色目录的 <狗>.json）")
    ap.add_argument("--no-orders", action="store_true", help="不使用订单证据（只用负回报启发式）")
    ap.add_argument("--apply", action="store_true", help="写盘（默认 dry-run）")
    ap.add_argument("--vol-base", action="store_true",
                    help="同时回填波动路径的当日高波动基线 vol_base（只补缺）")
    ap.add_argument("--matches-dir", default=str(ROOT / "data" / "matches"))
    args = ap.parse_args(argv)

    targets = _find_targets(args)
    if not targets:
        print("未找到 factor_memory.json")
        return 1
    print(f"{'APPLY' if args.apply else 'DRY-RUN'}：{len(targets)} 个因子库")
    tot = {"type_filled": 0, "unit_filled": 0, "unknown": 0,
           "history_samples": 0, "existing_type": 0, "from_orders": 0, "mixed": 0}
    for p in targets:
        basis: dict[str, float] = {}
        src = ""
        if not args.no_orders:
            rj = Path(args.orders) if args.orders else _role_json_for(p)
            if rj and rj.exists():
                basis = _orders_basis(rj)
                src = f"｜订单腿 {len(basis)}" if basis else ""
        st = backfill_file(p, apply=args.apply, basis_by_lid=basis)
        if args.vol_base:
            vb = backfill_vol_base(p, Path(args.matches_dir), apply=args.apply)
            if vb.get("error"):
                print(f"   ⚠️ vol_base: {vb['error']}")
            else:
                print(f"   vol_base：北单因子 {vb['factors']}｜补 {vb['filled']}"
                      f"｜无基线 {vb['no_baseline']}"
                      f"｜跳过非北单因子 {vb['skipped_non_beidan']}")
                for ex in vb["examples"]:
                    print(f"       · {ex['factor'][:30]:30s} {ex['n']} 样本｜均基线 {ex['base_mean']:.0%}")
        if st.get("error"):
            print(f"  ⚠️ {st['path']}: {st['error']}")
            continue
        for k in tot:
            tot[k] += st.get(k, 0)
        flag = "" if (st["type_filled"] or st["unit_filled"]) else "（无需改动）"
        print(f"   {st['path']}{src}\n     因子 {st['factors']}｜补 type {st['type_filled']}"
              f"｜补 unit_cost {st['unit_filled']} 条样本（其中订单解析 {st['from_orders']}）"
              f"｜无证据 {st['unknown']}｜混合口径 {st['mixed']}"
              f"｜已有 type {st['existing_type']} {flag}")
        for ex in st["examples"]:
            uc = ("混合%s" % ex["bases"]) if ex.get("bases") else (
                f"{ex['unit_cost']:g}" if ex.get("unit_cost") else "—")
            print(f"       · {ex['factor'][:34]:34s} → {str(ex['type']):12s} "
                  f"unit_cost={uc:>10s}（{ex['why']}, {ex['samples']} 样本）")
        if st.get("backup"):
            print(f"       备份: {st['backup']}")
    print("\n合计：补 type %d 个｜补 unit_cost %d 条样本（订单解析 %d）"
          "｜无证据 %d 个｜混合口径 %d 个｜已有 type %d 个"
          % (tot["type_filled"], tot["unit_filled"], tot["from_orders"],
             tot["unknown"], tot["mixed"], tot["existing_type"]))
    if not args.apply:
        print("（dry-run 未写盘；确认无误后加 --apply）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
