#!/usr/bin/env python3
"""冷启动后的「带因子顺序回放」：把并行 init 的因子库注入沙箱，逐日推进。

和 run_beidan_loop.py 的区别
──────────────────────────
* 可注入**并行 init 产出的因子库**（`--inject-library`）：
  - `factor_memory.json`（26 个候选因子的统计/历史）拷进沙箱 `memory/`
  - 库因子的 `fac_*.json` 定义（slugs/content）补齐到沙箱 `factors/`
    （缺定义 → 分析时因子清单拿不到 slugs → 校准 k 与选腿排序都会退化）
* 支持 `--each-day` 逐日推进：每天独立进程、即时打印当日窗口/订单/因子/PnL，
  适合「day by day 观察飞轮迭代」。
* 余额护栏：每轮结束查 DeepSeek 余额并落盘；低于阈值（或 API 报余额不足）时
  调用 macOS `say` 语音提示（`--say-threshold` / `--no-say`）。

用法
───
    # 注入因子库并跑 07-16 一天
    python3 -m scripts.run_beidan_seq --days 2026-07-16 --inject-library \
        --library data/beidan_factor_test/library_july/factor_memory.json

    # 逐日推进 07-16~07-31（每天一个进程）
    python3 -m scripts.run_beidan_seq --each-day --days 2026-07-16 2026-07-17 ...
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_FET = "/path/to/fet_txt"


def _say(text: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        subprocess.Popen(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _balance() -> float | None:
    """查 DeepSeek 余额（元）；失败返回 None。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return None
    try:
        import requests
        r = requests.get("https://api.deepseek.com/user/balance",
                         headers={"Authorization": f"Bearer {key}"}, timeout=20)
        j = r.json()
        return float(j["balance_infos"][0]["total_balance"])
    except Exception:
        return None


def _capture_library(lib_path: Path) -> dict:
    """从库 + 挖矿明细里抽出每个因子的 fac 定义（slugs/content）。"""
    lib = json.loads(lib_path.read_text(encoding="utf-8")).get("factor_perf") or {}
    defs: dict[str, dict] = {}
    # 挖矿明细里带 slugs（reduce 只并统计，slugs 在因子条目里）
    mined = Path("data/beidan_factor_test/mined/bcl狗")
    for mp in sorted(glob.glob(str(mined / "*/factor_memory.json"))):
        try:
            fp = json.loads(Path(mp).read_text(encoding="utf-8")).get("factor_perf") or {}
        except Exception:
            continue
        for n, e in fp.items():
            if n in lib:
                fid = e.get("fac_id") or f"fac_{n.lower().replace(' ', '_')[:40]}"
                d = defs.setdefault(fid, {"id": fid, "slugs": [], "content": ""})
                if e.get("slugs") and not d["slugs"]:
                    d["slugs"] = list(e["slugs"])
                if e.get("desc") and not d["content"]:
                    d["content"] = str(e["desc"])[:200]
    for n, e in lib.items():
        fid = e.get("fac_id") or f"fac_{n.lower().replace(' ', '_')[:40]}"
        d = defs.setdefault(fid, {"id": fid, "slugs": [], "content": ""})
        if e.get("slugs") and not d["slugs"]:
            d["slugs"] = list(e["slugs"])
        if e.get("desc") and not d["content"]:
            d["content"] = str(e["desc"])[:200]
    return lib, defs


def _inject(ws: Path, lib_path: Path) -> dict:
    """把库注入沙箱：factor_memory.json + fac_*.json 定义。返回注入统计。"""
    lib, defs = _capture_library(lib_path)
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    fm = ws / "memory" / "factor_memory.json"
    if fm.exists():
        bak = ws / "memory" / f"factor_memory.json.bak.inject_{time.strftime('%H%M%S')}"
        shutil.copy2(fm, bak)
    fm.write_text(json.dumps({"factor_perf": lib}, ensure_ascii=False, indent=2), encoding="utf-8")

    facdir = ws / "factors"
    facdir.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for fid, d in defs.items():
        slugs = [s for s in (d.get("slugs") or []) if s]
        if not slugs:
            skipped += 1
            continue
        (facdir / f"{fid}.json").write_text(json.dumps({
            "id": fid, "slugs": slugs,
            "content": d.get("content") or f"{fid}（初始化库导入）",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
    return {"factors": len(lib), "defs_written": written, "defs_skipped_no_slug": skipped}


def _run_day(day: str, cfg: dict) -> dict:
    """在已有沙箱里跑一天：分析 → 结算(反思) → 归纳。"""
    ws = cfg["ws"]
    os.environ["DS_ROLES_ROOT"] = str(ws)
    os.environ["DS_SESSIONS_ROOT"] = str(ws / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(ws / "factors")
    os.environ["DS_BACKTEST_FET"] = "1"
    os.environ["DS_FET_TXT_ROOT"] = cfg["fet_root"]

    from src import backtest_fet
    from src.beidan_parlay_dog import BeidanParlayDog
    from src.providers.deepseek import DeepSeekProvider

    src = backtest_fet.enable(cfg["fet_root"])
    waves = [w.strftime("%H:%M") for w in src.waves(day)] if (src and src.available) else []

    dog = BeidanParlayDog(user=cfg["dog"])
    role = dog._ensure_role()
    fp_before = len((role.memory.factors.factor_perf if role.memory.factors._loaded
                     else (role.memory.factors.load() or role.memory.factors.factor_perf)) or {})
    cfg_parlay = dog._load_parlay_config()
    pol = dog._pool_gate_policy(cfg_parlay, as_of=day) or {}

    real = DeepSeekProvider()
    if not real.api_key:
        return {"day": day, "error": "DEEPSEEK_API_KEY 未设置"}
    dog.set_provider(real)

    print(f"\n{'─'*72}\n📅 {day}｜波次 {waves}｜资金 {role.capital:.0f}｜"
          f"因子(前) {fp_before}｜门 θ={pol.get('theta')} m_star={pol.get('m_star')} "
          f"src={pol.get('source')}\n{'─'*72}", flush=True)
    t0 = time.time()
    try:
        a = dog.analyze(day, live=False, use_llm=True)
    except Exception as e:
        return {"day": day, "error": f"分析失败: {str(e)[:200]}"}
    tin = time.time() - t0
    for o in a.get("orders") or []:
        pg = (o.get("flex") or {}).get("pool_gate") or {}
        print(f"   🎫 {o.get('slip_type')} 成本{o.get('total_stake')} "
              f"腿{len(pg.get('kept_x') or [])}(x_min={pg.get('x_min')}) "
              f"每注{pg.get('m')}关/需≥{pg.get('m_star')}")
    if not (a.get("orders") or []):
        print(f"   ⛔ 空仓：{a.get('skipped') or '无过门腿'}")

    t1 = time.time()
    s = dog.settle(day, reflect=True)
    ts = time.time() - t1
    role = dog._ensure_role()
    ind = 0
    if not cfg["no_induct"]:
        from src.factor_induction import main as induction_main
        try:
            induction_main(["--roles", cfg["dog"]])
        except Exception as e:
            print(f"   ⚠️ 归纳失败: {str(e)[:120]}")
    fp = role.memory.factors.factor_perf or {}
    refl = role.memory.reflections.reflections or []
    pnl = float(s.get("pnl") or 0)
    print(f"   💰 结算 {s.get('settled',0)}单 PnL{pnl:+.0f}（{ts:.0f}s）｜"
          f"🧬 因子 {fp_before} → {len(fp)}｜反思 {len(refl)} 条｜"
          f"分析 {tin:.0f}s｜资金 {role.capital:.0f}", flush=True)
    return {"day": day, "placed": a.get("placed", 0), "settled": s.get("settled", 0),
            "pnl": pnl, "factors": len(fp), "reflections": len(refl),
            "capital": role.capital, "secs": round(time.time() - t0, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True, help="足球日列表 YYYY-MM-DD ...")
    ap.add_argument("--dog", default="bcl狗")
    ap.add_argument("--sandbox", default="", help="沙箱目录（默认 /tmp/beidan_seq_<狗>）")
    ap.add_argument("--fet-root", default=os.environ.get("DS_FET_TXT_ROOT") or DEFAULT_FET)
    ap.add_argument("--inject-library", action="store_true", help="注入因子库（仅第一次）")
    ap.add_argument("--library", default="data/beidan_factor_test/library_july/factor_memory.json")
    ap.add_argument("--no-induct", action="store_true", help="跳过因子归纳（省 token）")
    ap.add_argument("--model", default="", help="覆盖 DEEPSEEK_MODEL（如 deepseek-v4-flash）")
    ap.add_argument("--say-threshold", type=float, default=5.0, help="余额低于此值语音告警")
    ap.add_argument("--no-say", action="store_true")
    ap.add_argument("--each-day", action="store_true", help="每天都重开进程跑（day by day）")
    args = ap.parse_args(argv)

    ws = Path(args.sandbox) if args.sandbox else Path(f"/tmp/beidan_seq_{args.dog}")
    ws.mkdir(parents=True, exist_ok=True)
    if not (ws / f"{args.dog}.json").exists():
        src = ROOT / "data" / "roles" / args.dog
        for it in src.iterdir():
            (shutil.copytree(it, ws / it.name, dirs_exist_ok=True)
             if it.is_dir() else shutil.copy2(it, ws / it.name))
        print(f"🧪 沙箱初始化：{ws}（角色 {args.dog}）", flush=True)

    if args.model:
        os.environ["DEEPSEEK_MODEL"] = args.model
        print(f"🧠 模型覆盖：DEEPSEEK_MODEL={args.model}", flush=True)

    say_on = not args.no_say
    if args.inject_library:
        lib_path = Path(args.library) if Path(args.library).is_absolute() else ROOT / args.library
        st = _inject(ws, lib_path)
        print(f"💉 注入因子库 {lib_path.name}：{st['factors']} 个因子｜"
              f"补定义 {st['defs_written']}｜无 slug 跳过 {st['defs_skipped_no_slug']}", flush=True)

    cfg = {"ws": ws, "dog": args.dog, "fet_root": args.fet_root, "no_induct": args.no_induct}
    results = []
    for i, day in enumerate(args.days, 1):
        if args.each_day or i == 1:
            # 每日独立进程：环境变量与模块缓存干净
            env = dict(os.environ)
            code = (
                "import sys; sys.path.insert(0, %r);"
                "from scripts.run_beidan_seq import _run_one; _run_one(%r, %r)"
                % (str(ROOT), day, str(ws))
            )
            env["DS_ROLES_ROOT"] = str(ws)
            env["DS_SESSIONS_ROOT"] = str(ws / "sessions")
            env["DS_FACTORS_ROOT"] = str(ws / "factors")
            env["DS_BACKTEST_FET"] = "1"
            env["DS_FET_TXT_ROOT"] = args.fet_root
            env["DS_SEQ_DOG"] = args.dog
            env["DS_SEQ_NO_INDUCT"] = "1" if args.no_induct else "0"
            r = subprocess.run([sys.executable, "-c", code], env=env)
            if r.returncode != 0:
                _say("余额可能不足，回放中断", say_on)
        bal = _balance()
        if bal is not None:
            print(f"   💳 余额 {bal:.2f} 元", flush=True)
            (ws / "balance_log.jsonl").open("a", encoding="utf-8").write(
                json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "day": day,
                            "balance": bal}) + "\n")
            if bal < args.say_threshold:
                _say(f"注意，DeepSeek 余额只剩 {bal:.1f} 元", say_on)
    return 0


def _run_one(day: str, ws: str) -> None:
    """子进程入口：跑一天（--each-day 用）。"""
    _run_day(day, {"ws": Path(ws), "dog": os.environ.get("DS_SEQ_DOG", "bcl狗"),
                   "fet_root": os.environ.get("DS_FET_TXT_ROOT") or DEFAULT_FET,
                   "no_induct": os.environ.get("DS_SEQ_NO_INDUCT") == "1"})


if __name__ == "__main__":
    raise SystemExit(main())
