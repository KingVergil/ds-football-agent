#!/usr/bin/env python3
"""离线重建「每日候选腿池」（纯确定性，0 次 LLM 调用）。

为什么需要它
───────────
组合层（方案 C）是腿池的纯函数，可以用历史数据仿真；但**腿池必须包含引擎当天算过、
只是没被选中的那些腿**，否则只能在"已下注腿"上做选择偏差很大的伪仿真。

本脚本复用引擎自己的确定性链路重建腿池：

    逐「足球日 × 波次」（周末 16:30/20:30，工作日 22:30）
      ├─ backtest_fet 切片（该波时刻 → 开赛前档位）                       ← 不可省：防前视
      ├─ BeidanParlayDog._beidan_matches(day, live=False, as_of=波次)      ← 场次与赔率
      ├─ _market_p_hat(m) = 锐市场去水 p̂（Pinnacle，来自该波切片）
      ├─ _beidan_odds(m)  = 三侧北单赛前赔率
      ├─ x(侧) = 市场p̂(侧) × 该侧北单赔率
      └─ 结果/SP 用 data/beidan|beidan_sp|matches 三源合并反查（该场已完场时）
    落盘成 leg_pool/<day>/<wave>.json

产出的腿带 `x`（三侧）、`picks`（≥min_x 的侧）、`actual`、`sp`、命中标记，
供 scripts/sim_ticket_plans.py 扫「选择层 × 组合层」而无需再调模型。

用法
───
    python3 -m scripts.rebuild_leg_pool --start 2026-07-16 --end 2026-08-15 \
        --dog bcl狗 --out data/leg_pool --min-x 1.00
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SIDES = ("H", "D", "A")
DEFAULT_FET = "/path/to/fet_txt"


def _date_range(start: str, end: str) -> list[str]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _prep_sandbox(dog: str, fet_root: str) -> Path:
    """沙箱角色根：只为让 DS_ROLES_ROOT 生效（切片源自动启用），不做任何写入到线上。"""
    sb = Path(tempfile.mkdtemp(prefix=f"legpool_{dog}_"))
    src = ROOT / "data" / "roles" / dog
    if src.exists():
        import shutil
        for it in src.iterdir():
            (shutil.copytree(it, sb / it.name) if it.is_dir() else shutil.copy2(it, sb / it.name))
    os.environ["DS_ROLES_ROOT"] = str(sb)
    os.environ["DS_SESSIONS_ROOT"] = str(sb / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(sb / "factors")
    os.environ["DS_BACKTEST_FET"] = "1"
    os.environ["DS_FET_TXT_ROOT"] = fet_root
    return sb


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dog", default="bcl狗")
    ap.add_argument("--out", default="data/leg_pool")
    ap.add_argument("--fet-root", default=os.environ.get("DS_FET_TXT_ROOT") or DEFAULT_FET)
    ap.add_argument("--min-x", type=float, default=1.00,
                    help="入库门槛（低于该 x 的侧不落盘）；建议低于 θ，便于扫 θ")
    args = ap.parse_args(argv)

    sb = _prep_sandbox(args.dog, args.fet_root)
    from src import backtest_fet
    from src.beidan_parlay_dog import BeidanParlayDog
    from src.environment import get_football_day, football_day_calendar_dates
    from src.beidan_settlement import result_code_to_pick

    src_fet = backtest_fet.enable(args.fet_root)
    if not src_fet or not src_fet.available:
        print("❌ 切片源不可用")
        return 1
    dog = BeidanParlayDog(user=args.dog)
    dm = dog._dm
    out_root = Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out
    out_root.mkdir(parents=True, exist_ok=True)

    # 结果/SP 合并索引（legacy beidan + beidan_sp + matches），按 lota_id 反查
    def result_of(lid: str, via_matches: dict) -> tuple[str, float, str]:
        info = dict(via_matches.get(lid) or {})
        raw = info.get("result")
        sp = float(info.get("spvalue") or 0.0)
        act = result_code_to_pick(str(raw).strip()) if raw not in (None, "") else ""
        return act, sp, str(raw)

    total_days = leg_days = total_legs = 0
    for day in _date_range(args.start, args.end):
        total_days += 1
        d0 = date.fromisoformat(day)
        # 该足球日全部北单场（含 result/sp），用于反查
        via_matches: dict[str, dict] = {}
        for cd in football_day_calendar_dates(d0):
            for m in dm.get_cached_matches(cd, lottery_type="all"):
                if m.get("beidan_number"):
                    via_matches[m["lota_id"]] = dict(m.get("beidan_info") or {})
            for lm in (dm._read_legacy_beidan(cd) or []):
                lid = lm.get("lota_id")
                if lid and (lm.get("beidan_info") or {}).get("result") not in (None, ""):
                    via_matches.setdefault(lid, {}).update(lm.get("beidan_info") or {})
            for lid, v in (dm.get_beidan_sp_cache(cd) or {}).items():
                via_matches.setdefault(lid, {}).update(v or {})

        day_legs = []
        waves = src_fet.waves(day)
        for w in waves:
            backtest_fet.set_access_time(w)
            as_of = w.strftime("%Y-%m-%d %H:%M")
            try:
                matches, _warns = dog._beidan_matches(day, live=False, as_of=as_of)
            except Exception as e:
                print(f"  ⚠️ {day} {as_of} 取数失败: {str(e)[:80]}")
                continue
            for m in matches:
                lid = m.get("lota_id") or ""
                if not lid:
                    continue
                src, p_mkt = dog._market_p_hat(m)
                if not p_mkt:
                    continue
                o = dog._beidan_odds(m) or {}
                gl = float((m.get("beidan_info") or {}).get("goal_line") or 0.0)
                xs = {}
                for side, key in (("H", "h"), ("D", "d"), ("A", "a")):
                    try:
                        odds = float(o.get(key) or 0.0)
                        pm = float(p_mkt.get(side) or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if odds > 0 and pm > 0:
                        xs[side] = pm * odds
                if not xs:
                    continue
                act, sp, raw = result_of(lid, via_matches)
                # 赛前稳定性信号（完全来自该波切片，不含后视）：
                #   disp = 离散指数首行→末行最大相对变动（引擎 prematch_dispersion）
                #   odds_h/d/a = 三侧北单赛前赔率（算隐含抽水/离散用）
                try:
                    from src.tools import prematch_dispersion
                    disp = prematch_dispersion(lid)
                except Exception:
                    disp = None
                odds_all = {k: float(o.get(k) or 0.0) for k in ("h", "d", "a")}
                for side, x in xs.items():
                    if x < args.min_x:
                        continue
                    day_legs.append({
                        "day": day, "wave": as_of, "lota_id": lid,
                        "goal_line": gl, "side": side, "x": round(x, 6),
                        "market_p": round(float(p_mkt.get(side) or 0.0), 6),
                        "beidan_odds": round(float({"H": o.get("h"), "D": o.get("d"), "A": o.get("a")}.get(side) or 0.0), 4),
                        "disp": (round(float(disp), 6) if disp is not None else None),
                        "odds_h": round(odds_all["h"], 4), "odds_d": round(odds_all["d"], 4),
                        "odds_a": round(odds_all["a"], 4),
                        "n_stages": len(src_fet.stages_of(lid)),
                        "mkt_src": src,
                        "actual": act, "raw_result": raw, "sp": sp,
                        "settled": bool(act and sp > 0),
                        "hit": bool(act and act == side),
                    })
        if day_legs:
            leg_days += 1
            total_legs += len(day_legs)
            (out_root / f"{day}.json").write_text(
                json.dumps(day_legs, ensure_ascii=False), encoding="utf-8")
            settled = sum(1 for l in day_legs if l["settled"])
            print(f"  {day}: 波 {len(waves)}｜腿(侧) {len(day_legs)}｜带结果 {settled}")
        else:
            print(f"  {day}: 无候选腿")
    backtest_fet.set_access_time(None)
    print(f"\n✅ 重建完成：{leg_days}/{total_days} 天有腿｜共 {total_legs} 条(侧)腿"
          f"｜输出 {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
