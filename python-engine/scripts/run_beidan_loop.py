#!/usr/bin/env python3
"""北单串关「单日完整 loop」：分析 → 结算(含反思) → 因子归纳。

为什么需要它
────────────
`run_sandbox_day.py` 是**只看分析**的一次性脚本（`--settle` 还写死 `reflect=False`，
即不产生反思→不产生因子），`run_beidan_walkforward.py` 是跨天回放，
两者都不适合「盯着一天的因子数据飞轮转一圈」：

    分析（stage1 逐场 → 引擎规则组装 → 下单）
      ↓
    结算（对账开奖 SP → 每腿 actual/hit/profit → 反思：归因 + 发现新因子候选）
      ↓
    因子归纳（去重合并 + 补 fac 定义）

本脚本把这三天步放在**同一个沙箱**里跑完（沙箱目录持久化，便于逐段 review 产物），
并把每一步的产物打出来，方便观察「因子从 0 长出来 / 样本累积 / 被归纳」。

沙箱隔离
───────
`DS_ROLES_ROOT` 指向沙箱（单狗平铺角色目录），引擎自 2026-08-22 起在平铺根下读写；
线上 `data/roles/<狗>/` 全程只读。比赛数据走 `src/backtest_fet.py` 的 fet_txt 切片源
（`DS_ROLES_ROOT` 存在时自动启用），按「访问时刻 → 开赛前档位」逐波取数——不是
harness 的 `prepare`（北单没有 prepare 阶段，见 harness-plugin/docs/replay_mode.md）。

用法
───
    python3 -m scripts.run_beidan_loop --day 2026-07-11 --dog bcl狗
    python3 -m scripts.run_beidan_loop --day 2026-07-11 --dog bcl狗 --sandbox /tmp/x --settle-first

产物
───
    <sandbox>/                      沙箱角色目录（bcl狗.json / memory/ / factors/ ...）
    docs/prompts/beidan_loop_<day>/ stage1 prompt+回复全文
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_FET = "/path/to/fet_txt"


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def _section(title: str) -> None:
    _log("\n" + "═" * 72)
    _log(f"  {title}")
    _log("═" * 72)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _copy_role(dog: str, sandbox: Path) -> None:
    """线上角色 → 沙箱（只读复制；已有沙箱则复用，便于观察累积）。"""
    src = ROOT / "data" / "roles" / dog
    if not src.exists():
        raise SystemExit(f"❌ 角色目录不存在: {src}")
    sandbox.mkdir(parents=True, exist_ok=True)
    for it in src.iterdir():
        dst = sandbox / it.name
        if it.is_dir():
            shutil.copytree(it, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(it, dst)


class RecordingProvider:
    """记录 stage1/stage2 的 prompt+回复（转真 provider），便于逐段 review。"""

    def __init__(self, real, out_dir: Path):
        self.real = real
        self.out = out_dir
        self.log: list[dict] = []
        out_dir.mkdir(parents=True, exist_ok=True)

    def call(self, system, messages, **kw):
        t0 = time.time()
        try:
            resp = self.real.call(system, messages, **kw)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            _log(f"    ❌ [LLM] 调用失败: {msg[:200]}")
            if "Insufficient Balance" in msg:
                _log("    💰 DeepSeek 余额不足，充值后重跑。")
            raise
        dt = time.time() - t0
        stage = "stage1" if "初筛器" in (system or "") else "stage2"
        self.log.append({"stage": stage, "secs": round(dt, 1)})
        f = self.out / f"{len(self.log):02d}_{stage}.md"
        f.write_text(
            f"# {stage} #{len(self.log)}\n\n## SYSTEM\n\n{system}\n\n## USER\n\n"
            + "\n".join(str(m.get("content") or "") for m in (messages or []))
            + f"\n\n## RESPONSE\n\n{resp}\n",
            encoding="utf-8",
        )
        _log(f"    [LLM] {stage} #{len(self.log)} {dt:.1f}s → {f.name}")
        return resp

    def call_fast(self, *a, **k):
        return self.real.call_fast(*a, **k)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True, help="足球日 YYYY-MM-DD")
    ap.add_argument("--dog", default="bcl狗", help="角色名（默认沙箱狗 bcl狗）")
    ap.add_argument("--sandbox", default="", help="沙箱目录（默认 <tmp>/<狗>_loop_<day>）")
    ap.add_argument("--capital", type=float, default=5000.0)
    ap.add_argument("--fet-root", default=os.environ.get("DS_FET_TXT_ROOT") or DEFAULT_FET)
    ap.add_argument("--out", default="", help="prompt 产物目录（默认 docs/prompts/beidan_loop_<day>）")
    ap.add_argument("--no-reflect", action="store_true", help="结算不做反思（看不到因子生成）")
    ap.add_argument("--dedupe-only", action="store_true",
                    help="归纳只做确定性去重合并，不调 LLM 判重（省 token）")
    ap.add_argument("--reset", action="store_true",
                    help="重置沙箱资金=--capital + 清订单（清记忆请手动删沙箱目录）")
    args = ap.parse_args(argv)

    day = args.day
    out = Path(args.out) if args.out else ROOT / "docs" / "prompts" / f"beidan_loop_{day}"
    sandbox = Path(args.sandbox) if args.sandbox else Path(
        f"/tmp/beidan_loop_{args.dog}_{day.replace('-', '')}")
    _copy_role(args.dog, sandbox)

    os.environ["DS_ROLES_ROOT"] = str(sandbox)
    os.environ["DS_SESSIONS_ROOT"] = str(sandbox / "sessions")
    os.environ["DS_FACTORS_ROOT"] = str(sandbox / "factors")
    os.environ["DS_BACKTEST_FET"] = "1"
    os.environ["DS_FET_TXT_ROOT"] = args.fet_root

    _section(f"沙箱 loop · {args.dog} · {day}")
    _log(f"沙箱角色根 : {sandbox}")
    _log(f"切片源     : {args.fet_root}")
    _log(f"prompt 产物: {out}")

    from src import backtest_fet
    from src.beidan_parlay_dog import BeidanParlayDog
    from src.providers.deepseek import DeepSeekProvider

    src = backtest_fet.enable(args.fet_root)
    if src is None or not src.available:
        _log("❌ 回测切片源不可用（缺 .bc_backtest_index.json）——先跑 "
             "python3 -m src.backtest_fet index")
        return 1
    waves = [w.strftime("%H:%M") for w in src.waves(day)]
    _log(f"波次       : {waves or '(无)'}")

    dog = BeidanParlayDog(user=args.dog)
    role = dog._ensure_role()
    if args.reset:
        dog.reset(capital=args.capital)
        role = dog._ensure_role()
    cfg = dog._load_parlay_config()
    pol = dog._pool_gate_policy(cfg, as_of=day) or {}
    _log(f"资金       : {role.capital:.2f}")
    _log(f"配置       : selector={cfg.get('selector')} ticket_mode={cfg.get('ticket_mode')} "
         f"gate_basis={cfg.get('gate_basis')} x_cap={cfg.get('x_cap')}")
    _log(f"门策略@{day}: " + json.dumps(
        {k: pol.get(k) for k in ("theta", "m_star", "n", "days", "source", "gl_classes")},
        ensure_ascii=False))

    real = DeepSeekProvider()
    if not real.api_key:
        _log("❌ DEEPSEEK_API_KEY 未设置")
        return 1
    recorder = RecordingProvider(real, out)
    dog.set_provider(recorder)

    # ── 步 1：分析 ──────────────────────────────
    _section(f"① 分析 {day}（stage1 逐场 → 引擎规则组装 → 下单）")
    fp_before = len((_read_json(sandbox / "memory" / "factor_memory.json") or {})
                    .get("factor_perf") or {})
    t0 = time.time()
    try:
        res = dog.analyze(day, live=False, use_llm=True)
    except Exception as e:  # noqa: BLE001
        _log(f"❌ 分析失败: {str(e)[:300]}")
        return 1
    _log(f"\n✅ 分析完成（{time.time() - t0:.0f}s）: 场次 {res['matches_count']} | "
         f"腿 {res['legs_selected']} | 票型 {res['tickets']} | 下单 {res['placed']}")
    for o in res.get("orders") or []:
        pg = (o.get("flex") or {}).get("pool_gate") or {}
        _log(f"   🎫 {o.get('slip_type')} 成本 {o.get('total_stake')} 元 | "
             f"放行腿 {len(pg.get('kept_x') or [])}（x_min={pg.get('x_min')}）| "
             f"每注 {pg.get('m')} 关/需≥{pg.get('m_star')}")
        for l in o.get("ticket_legs") or []:
            _log(f"      {l['lota_id']} {'/'.join(l.get('picks') or [])} "
                 f"x={l.get('x_mkt')} v̂={float(l.get('leg_v') or 0):.3f}")
    if res.get("skipped"):
        _log(f"   ⛔ 跳过: {res['skipped']}")
    _log(f"   因子库（分析前）: {fp_before} 个")

    # ── 步 2：结算 + 反思 ───────────────────────
    _section(f"② 结算 {day}（对账开奖 SP → 反思归因/发现因子）")
    t0 = time.time()
    s = dog.settle(day, reflect=not args.no_reflect)
    _log(f"💰 结算（{time.time() - t0:.0f}s）: {s.get('settled', 0)} 单 | 命中 {s.get('hit', 0)} | "
         f"未中 {s.get('miss', 0)} | PnL {float(s.get('pnl', 0)):+.2f} | 资金 {role.capital:.2f}")
    for o in role.get_orders():
        if not o.get("settled_at"):
            continue
        legs = o.get("ticket_legs") or []
        hits = sum(1 for l in legs if l.get("hit"))
        _log(f"   {o.get('slip_type')}: 命中 {hits}/{len(legs)} → "
             f"{'中' if o.get('hit') else '不中'} | 派彩 {o.get('return_amount')} | "
             f"盈亏 {o.get('profit')}")

    # ── 步 3：因子归纳 ──────────────────────────
    _section("③ 因子归纳（去重合并 + 补 fac 定义）")
    if args.dedupe_only:
        from src.factor_induction import clean_name, merge_entries
        role.memory.factors.load()
        fp = role.memory.factors.factor_perf
        groups: dict[str, list[tuple[str, dict]]] = {}
        for name, entry in fp.items():
            groups.setdefault(clean_name(name), []).append((name, entry))
        new_fp: dict[str, dict] = {}
        for items in groups.values():
            items.sort(key=lambda x: -(x[1].get("total", 0) or 0))
            base_name, base_entry = items[0]
            base_entry["_name"] = base_name
            for name, entry in items[1:]:
                merge_entries(base_entry, entry, name)
            new_fp[base_name] = base_entry
        role.memory.factors.factor_perf = new_fp
        role.memory.factors._save()
        _log(f"确定性去重后因子数: {len(new_fp)}")
    else:
        from src.factor_induction import main as induction_main
        t0 = time.time()
        induction_main(["--roles", args.dog])
        _log(f"（归纳耗时 {time.time() - t0:.0f}s）")

    # ── 当日终态 ────────────────────────────────
    _section("④ 飞轮快照")
    fm = _read_json(sandbox / "memory" / "factor_memory.json") or {}
    perf = fm.get("factor_perf") or {}
    refl = _read_json(sandbox / "memory" / "reflection_memory.json") or {}
    refls = refl.get("reflections") if isinstance(refl, dict) else refl
    _log(f"因子库    : {len(perf)} 个（分析前 {fp_before} 个 → 本轮新增 {len(perf) - fp_before}）")
    for name, e in sorted(perf.items(), key=lambda x: -(x[1].get("total", 0) or 0)):
        _log(f"   · {name} | 状态 {e.get('status')} | 类型 {e.get('type')} | "
             f"样本 {e.get('total', 0)} | 命中 {e.get('hit', 0)} | 盈亏 {e.get('profit', 0)}")
    _log(f"反思记录  : {len(refls or [])} 条")
    _log(f"LLM 调用  : {len(recorder.log)} 次（stage1 "
         f"{sum(1 for c in recorder.log if c['stage'] == 'stage1')} / stage2 "
         f"{sum(1 for c in recorder.log if c['stage'] == 'stage2')}）")
    _log(f"资金      : {role.capital:.2f}（初始 {role.initial_capital}）")
    _log(f"\n产物: 沙箱 {sandbox}\n      prompt {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
