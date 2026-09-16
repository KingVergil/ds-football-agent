#!/usr/bin/env python3
"""在临时沙箱里**真 LLM 跑一天**（北单串关狗）：stage1 逐场判断 → 引擎组装 → （可选）结算。

为什么需要它
────────────
* 分析链路（stage1 分批判断 + 引擎规则组装 + 池门/关数门/ROI 门）只有跑真模型才能看到真实产物；
* 它把「prompt/回复」全部落盘，便于逐段 review；
* 沙箱是临时目录（复制一份角色目录），**线上角色零影响**；回放切片源保证 per-match 档位，
  账本 `as_of` 保证不吃未来数据。

用法
────
    export DEEPSEEK_API_KEY=...                      # 或从 ~/.zshrc 取
    python3 -m scripts.run_sandbox_day 2026-08-15                    # 只分析（不下注到线上）
    python3 -m scripts.run_sandbox_day 2026-08-15 --settle            # 分析 + 结算 + 报当日盈亏
    python3 -m scripts.run_sandbox_day 2026-08-15 --dog bcl狗 --capital 5000

产物：`docs/prompts/llm_run_<day>/<NN>_<stage>.md`（system/user/response 全文）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_FET = Path("/path/to/fet_txt")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day", help="足球日 YYYY-MM-DD")
    ap.add_argument("--dog", default="bcl狗", help="角色名（默认沙盒狗 bcl狗）")
    ap.add_argument("--capital", type=float, default=5000.0)
    ap.add_argument("--fet-root", default=os.environ.get("DS_FET_TXT_ROOT") or str(DEFAULT_FET))
    ap.add_argument("--out", default="", help="产物目录（默认 docs/prompts/llm_run_<day>）")
    ap.add_argument("--settle", action="store_true", help="分析后再结算并报当日盈亏")
    ap.add_argument("--no-archive", action="store_true", help="不归档旧产物目录")
    args = ap.parse_args(argv)

    day = args.day
    role_src = ROOT / "data" / "roles" / args.dog
    if not role_src.exists():
        print(f"❌ 角色目录不存在: {role_src}")
        return 1
    out = Path(args.out) if args.out else ROOT / "docs" / "prompts" / f"llm_run_{day}"
    if out.exists() and not args.no_archive:
        bak = out.with_name(out.name + time.strftime("_bak%H%M%S"))
        shutil.move(str(out), str(bak))
        print(f"📦 旧产物归档 → {bak}")
    out.mkdir(parents=True, exist_ok=True)

    sandbox = tempfile.mkdtemp(prefix=f"{args.dog}_sandbox_")
    os.environ["DS_ROLES_ROOT"] = sandbox
    os.environ["DS_SESSIONS_ROOT"] = tempfile.mkdtemp(prefix=f"{args.dog}_sess_")
    os.environ["DS_BACKTEST_FET"] = "1"
    os.environ["DS_FET_TXT_ROOT"] = args.fet_root
    for it in role_src.iterdir():
        (shutil.copytree(it, Path(sandbox) / it.name) if it.is_dir()
         else shutil.copy2(it, Path(sandbox) / it.name))
    print(f"🧪 沙箱 {sandbox}（角色 {args.dog}）｜切片源 {args.fet_root}｜日 {day}")

    from src import backtest_fet
    backtest_fet.enable(args.fet_root)
    from src.beidan_parlay_dog import BeidanParlayDog
    from src.providers.deepseek import DeepSeekProvider

    dog = BeidanParlayDog(user=args.dog)
    dog.reset()
    role = dog._ensure_role()
    role.capital = float(args.capital)
    role.save()
    cfg = dog._load_parlay_config()
    pol = dog._pool_gate_policy(cfg, as_of=day)
    print(f"🚪 门策略 @{day}: " + (json.dumps(
        {k: pol.get(k) for k in ("theta", "m_star", "n", "days", "source",
                                 "gl_classes", "observe_gl_classes")},
        ensure_ascii=False) if pol else "未启用"))
    print(f"⚙️ 配置: selector={cfg.get('selector')} ticket_mode={cfg.get('ticket_mode')} "
          f"gate_basis={cfg.get('gate_basis')} x_cap={cfg.get('x_cap')}")

    real = DeepSeekProvider()
    if not real.api_key:
        print("❌ DEEPSEEK_API_KEY 未设置（export 或从 ~/.zshrc 取）")
        return 1
    log: list[dict] = []

    class LoggingProvider:
        """记录 prompt/回复，再转真 provider。"""

        def call(self, system, messages, **kw):
            t0 = time.time()
            try:
                resp = real.call(system, messages, **kw)
            except Exception as e:
                msg = str(e)
                print(f"    ❌ [LLM] 调用失败: {msg[:200]}")
                if "Insufficient Balance" in msg:
                    print("    💰 提示：DeepSeek 余额不足，充值后重跑。")
                raise
            dt = time.time() - t0
            stage = "stage1" if "初筛器" in (system or "") else "stage2"
            log.append({"stage": stage, "system": system or "",
                        "user": "\n".join(str(m.get("content") or "") for m in (messages or [])),
                        "resp": str(resp), "secs": round(dt, 1)})
            f = out / f"{len(log):02d}_{stage}.md"
            f.write_text(f"# {args.dog} · {day} · {stage} #{len(log)}\\n\\n## SYSTEM\\n\\n{system}"
                         + "\n\n## USER\n\n"
                         + "\n".join(str(m.get("content") or "") for m in (messages or []))
                         + f"\n\n## RESPONSE\n\n{resp}\n", encoding="utf-8")
            print(f"    [LLM] {stage} #{len(log)} {dt:.1f}s → {f.name}", flush=True)
            return resp

        def call_fast(self, *a, **k):
            return real.call_fast(*a, **k)

    dog.set_provider(LoggingProvider())
    t0 = time.time()
    try:
        res = dog.analyze(day, live=False, use_llm=True)
    except Exception as e:
        print(f"❌ 分析失败: {str(e)[:300]}")
        return 1
    print(f"\n✅ 分析完成（{time.time()-t0:.0f}s）：场次 {res['matches_count']}｜腿 {res['legs_selected']}"
          f"｜票型 {res['tickets']}｜下单 {res['placed']}")
    for o in res.get("orders") or []:
        pg = (o.get("flex") or {}).get("pool_gate") or {}
        print(f"   票 {o['pick']}｜{o['slip_type']}｜成本 {o['total_stake']} 元｜"
              f"放行腿 {len(pg.get('kept_x') or [])}（x_min={pg.get('x_min')}）"
              f"｜每注 {pg.get('m')} 关/需≥{pg.get('m_star')}")
        for l in o.get("ticket_legs") or []:
            print(f"      {l['lota_id']} {'/'.join(l.get('picks') or [])} x={l.get('x_mkt')} "
                  f"v̂={float(l.get('leg_v') or 0):.3f}")
    if res.get("skipped"):
        print("   跳过:", res["skipped"])
    print(f"   LLM 调用 {len(log)} 次（{sum(1 for c in log if c['stage']=='stage1')} stage1 / "
          f"{sum(1 for c in log if c['stage']=='stage2')} stage2）｜资金 {role.capital:.2f}")

    if args.settle:
        s = dog.settle(day, reflect=False)
        print(f"\n💰 结算：{s.get('settled', 0)} 单｜命中 {s.get('hit', 0)}｜未中 {s.get('miss', 0)}"
              f"｜PnL {s.get('pnl', 0):+.2f}｜资金 {role.capital:.2f}")
        for o in role.get_orders():
            if not o.get("settled_at"):
                continue
            legs = o.get("ticket_legs") or []
            hits = sum(1 for l in legs if l.get("hit"))
            print(f"   {o.get('slip_type')}：命中 {hits}/{len(legs)} → "
                  f"{'中' if o.get('hit') else '不中'}｜派彩 {o.get('return_amount')}｜"
                  f"盈亏 {o.get('profit')}")
    print(f"\n产物目录 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
