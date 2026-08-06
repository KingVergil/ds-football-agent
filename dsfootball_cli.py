#!/usr/bin/env python3
"""
DSFootball — 足球日虚拟博彩环境 Agent CLI（按 12:01→次日12:00 足球日推进）
  默认 --live 模式 (DeepSeekProvider + 实时数据). 用 --no-live 切换到回测模式.

用法:
  # 历史回测
  python dsfootball_cli.py agent jy runall 2026-06-11 2026-07-01 --no-live

  # Live 当日 (自动 refresh + analyze, 可多次跑)
  python dsfootball_cli.py agent 梭哈狗 analyze 2026-07-02
  python dsfootball_cli.py agent 梭哈狗 analyze 2026-07-02   # 晚上重跑: 退回未开赛 → 保留已开赛 → 重新下单

  # 管理
  python dsfootball_cli.py agent jy status
  python dsfootball_cli.py agent jy persona "我是保守型..."
  python dsfootball_cli.py agent jy reset
"""

import json
import sys
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from src.agent import Agent, _rt


def _check_review_due(agent: Agent):
    """live 模式: 检查上次 factor_review 是否超过 7 天，超时提示"""
    sessions_dir = Path("lota_data/sessions") / agent.user
    review_files = sorted(sessions_dir.glob("*factor_review_*.md"))
    if not review_files:
        print(f"  ⚠️ 尚未执行因子退役评估, 建议: python dsfootball_cli.py agent {agent.user} factor-review")
        return
    last_date_str = review_files[-1].stem.split("_")[-1]
    try:
        last_date = date.fromisoformat(last_date_str)
        days_ago = (date.today() - last_date).days
        if days_ago > 7:
            print(f"  ⚠️ 上次因子退役: {days_ago} 天前 ({last_date_str}), 建议: python dsfootball_cli.py agent {agent.user} factor-review")
    except ValueError:
        pass


def cmd_init(agent: Agent, args: list, alpha: bool = False):
    capital = float(args[0]) if args else 10000
    agent.init_role(capital=capital)
    if alpha:
        agent._ensure_role()
        rt = _rt({"user": agent.user})
        rt.role.alpha_mode = True
        rt.role.save()
    from src.providers.deepseek import DeepSeekProvider
    agent.set_provider(DeepSeekProvider())
    tag = " 🐺 alpha模式" if alpha else ""
    print(f"用户 '{agent.user}' 已初始化 (资金 {capital}){tag}")


def cmd_analyze(agent: Agent, day: str, live: bool = False, jingcai_only: bool = False,
                prefetched: bool = False):
    if live:
        from src.providers.deepseek import DeepSeekProvider
        agent.set_provider(DeepSeekProvider())

    print(f"足球日 {day}")
    result = agent.analyze(day, live=live, jingcai_only=jingcai_only, prefetched=prefetched)
    print(f"  比赛: {result['matches_count']} 场")
    print(f"  Prompt: {result['prompt_tokens']} tokens")

    orders = result.get("orders", [])
    if not orders:
        print("  无订单")
        return

    for o in orders:
        lid = o.get('lota_id', '?')
        # 查找比赛名
        m = agent._get_match_info(lid)
        match_label = f"{m['home']} vs {m['away']}" if m.get('home') and m.get('away') else lid
        if m.get('league'):
            match_label += f" ({m['league']})"

        if o.get("skip"):
            print(f"  ⏭ {match_label} | skip: {o.get('reason','')[:60]}")
        else:
            cap = agent._get_capital()
            pct = o.get('bet_size', 0) / cap * 100 if cap > 0 else 0
            print(f"  ✅ {match_label} | {o.get('bet_type','')} {o.get('pick','')} "
                  f"@{o.get('odds',0):.2f} bet {o.get('bet_size',0):.0f} ({pct:.0f}%) "
                  f"— {o.get('reason','')[:50]}")

    print(f"  已下单: {result.get('placed', 0)} 单")
    if result.get("session_path"):
        print(f"  📝 Session: {result['session_path']}")
    print(agent.status())

    if result.get("llm_response"):
        print(f"\n  LLM 响应 (前 400 chars):\n  {result['llm_response'][:400]}")

    if live:
        _check_review_due(agent)


def cmd_settle(agent: Agent, day: str, live: bool = False):
    if live:
        from src.providers.deepseek import DeepSeekProvider
        agent.set_provider(DeepSeekProvider())

    print(f"═══ 结算 {day} ═══")
    s = agent.settle(day)
    print(f"\n📊 结算结果: {s.get('settled', 0)} 单 "
          f"✅{s.get('hit', 0)} ❌{s.get('miss', 0)} ➖{s.get('push', 0)} "
          f"PnL {s.get('pnl', 0):+.0f}")
    print(agent.status())
    if live:
        _check_review_due(agent)


def cmd_factor_review(agent: Agent, rest: list):
    """因子结构性评估 — 判断因子的市场假设是否还成立"""
    from src.providers.deepseek import DeepSeekProvider
    agent.set_provider(DeepSeekProvider())
    end_date = rest[0] if rest else None
    start_date = rest[1] if len(rest) > 1 else ""
    r = agent.factor_review(end_date=end_date, start_date=start_date)
    print(f"✅ factor_review 完成: {r['end_date']} | 窗口起始={r['start_date']}")


def cmd_factor_induction(rest: list):
    """因子归纳 — 统一清洗/合并/补定义每日因子（alpha 跨狗 1 次，非 alpha 各自）"""
    from src.factor_induction import main as induction_main
    induction_main(rest)


def cmd_run(agent: Agent, day: str, live: bool = False, jingcai_only: bool = False):
    if live:
        from src.providers.deepseek import DeepSeekProvider
        agent.set_provider(DeepSeekProvider())

    print(f"═══ 足球日 {day} ═══")
    result = agent.run_day(day, live=live, jingcai_only=jingcai_only)

    s = result.get("settlement", {})
    if s.get("settled", 0) > 0:
        print(f"📊 结算: {s['settled']} 单 命中{s.get('hit',0)} 未中{s.get('miss',0)} 走水{s.get('push',0)} PnL {s.get('pnl',0):+.0f}")

    a = result.get("analysis", {})
    print(f"📈 分析: {a.get('matches_count',0)} 场比赛 → 下单 {a.get('placed',0)} 单")
    orders = a.get("orders", [])
    for o in orders:
        if not o.get("skip"):
            lid = o.get('lota_id', '?')
            m = agent._get_match_info(lid)
            label = f"{m['home']} vs {m['away']}" if m.get('home') else lid
            cap = agent._get_capital()
            pct = o.get('bet_size', 0) / cap * 100 if cap > 0 else 0
            print(f"  ✅ {label} | {o.get('bet_type','')} {o.get('pick','')} "
                  f"@{o.get('odds',0):.2f} bet {o.get('bet_size',0):.0f} ({pct:.0f}%)")

    print(agent.status())


def cmd_refresh(agent: Agent, day: str, live: bool = False, jingcai_only: bool = False):
    """刷新当天订单组：退回未开赛订单，保留已开赛订单，重新分析下单"""
    r = agent.refresh_orders(day)
    print(f"\n🔄 刷新完成: {r['refunded']} 单退回 ¥{r['total_refund']:,.0f} | "
          f"{r['kept']} 单保留 | 余额 ¥{r['capital']:,.0f}")

    # 重新分析
    print(f"\n📊 重新分析 {day}...")
    cmd_analyze(agent, day, live=live, jingcai_only=jingcai_only)


def cmd_runall(agent: Agent, start: str, end: str, live: bool = False, jingcai_only: bool = False):
    if live:
        from src.providers.deepseek import DeepSeekProvider
        agent.set_provider(DeepSeekProvider())

    d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    day_count = 0
    while d <= end_d:
        day_count += 1
        day_str = d.isoformat()
        result = agent.run_day(day_str, live=live, jingcai_only=jingcai_only)

        s = result.get("settlement", {})
        a = result.get("analysis", {})

        settlement_str = ""
        if s.get("settled", 0) > 0:
            settlement_str = f" | 结算{s['settled']}单 命中{s['hit']}/{s['miss']}/{s['push']} PnL{s['pnl']:+.0f}"

        print(f"{day_str} | {a.get('matches_count',0)}场 → "
              f"下单{a.get('placed',0)} | {a.get('prompt_tokens',0)}t{settlement_str}")

        # 每7天触发因子结构性评估
        if day_count % 7 == 0:
            agent.factor_review(end_date=day_str)

        # 破产检查：资金 < 50 且无待结算订单 = 真破产
        capital = agent._get_capital()
        if capital < 50:
            pending = [o for o in agent._get_unsettled_orders() if not o.get("settled_at")]
            if not pending:
                print(f"🪦 资金耗尽 (¥{capital:.0f})，game over.")
                break
            else:
                print(f"⚠️ 资金不足 ¥{capital:.0f}（{len(pending)} 单待结算，等待比赛结果）")

        d += timedelta(days=1)

    print(f"\n{agent.status()}")


def cmd_status(agent: Agent):
    print(agent.status())


def cmd_pending(agent: Agent):
    """查看待结算订单"""
    agent._ensure_role()
    rt = _rt({"user": agent.user})
    from src.data_manager import DataManager
    dm = DataManager()
    pending = [o for o in rt.role.get_orders() if not o.get("settled_at")]
    if not pending:
        print("(无待结算订单)")
        print(f"资金: {rt.role.capital:.0f}")
        return
    print(f"资金: {rt.role.capital:.0f} | 待结算: {len(pending)} 单")
    print()
    for o in pending:
        lid = o.get("lota_id", "")
        m = dm.get_cached_match(lid) or {}
        print(f"{m.get('home_name','?')} vs {m.get('away_name','?')} ({m.get('league_name','?')})")
        print(f"  {o.get('bet_type')} {o.get('pick')} @{o.get('odds')} bet{o.get('bet_size',0):.0f}")
        print(f"  match_time: {m.get('match_time','?')}")
        print(f"  理由: {o.get('reason','')[:100]}")
        print()


def cmd_soft_reset(agent: Agent, args: list):
    """轻量重置：只清空订单+资金，保留因子记忆

    用法:
      python dsfootball_cli.py agent jy soft-reset        # 恢复到初始资金
      python dsfootball_cli.py agent jy soft-reset 5000   # 重置到指定金额
    """
    agent._ensure_role()
    rt = _rt({"user": agent.user})
    role = rt.role

    capital = float(args[0]) if args else None
    role.soft_reset(capital=capital)
    target = capital if capital is not None else role.initial_capital
    print(f"\n✅ 用户 '{agent.user}' 已轻量重置 (资金 → {target:,.0f}) — 因子记忆保留\n{agent.status()}")


def cmd_reset(agent: Agent, args: list):
    """快速清空订单/预测/记忆/features，重置资金，方便测试

    用法:
      python dsfootball_cli.py agent jy reset          # 恢复到初始资金
      python dsfootball_cli.py agent jy reset 10000    # 重置到指定金额
    """
    agent._ensure_role()
    rt = _rt({"user": agent.user})
    role = rt.role

    # 角色自身重置（订单/预测/资金/资金曲线/记忆）
    capital = float(args[0]) if args else None
    role.reset(capital=capital)
    target = capital if capital is not None else role.initial_capital
    print(f"\n✅ 用户 '{agent.user}' 已重置 (资金 → {target:,.0f}) — 可重新测试\n{agent.status()}")


def cmd_live(agent: Agent, lota_id: str):
    """走地分析 — LLM 综合赛前compact-fet + 走地赔率 + 比分 给出判断"""
    if not lota_id:
        print("用法: python dsfootball_cli.py agent <name> live <lota_id>")
        return
    from src.providers.deepseek import DeepSeekProvider
    agent.set_provider(DeepSeekProvider())
    result = agent.analyze_live(lota_id)
    if result:
        print(result)


def cmd_cross_factors(agent: Agent):
    """开关跨Agent因子读取（alpha模式）— 修改角色自身配置"""
    agent._ensure_role()
    rt = _rt({"user": agent.user})
    role = rt.role

    current = role.alpha_mode
    role.alpha_mode = not current
    role.save()

    status = "🟢 开启 (alpha模式)" if role.alpha_mode else "🔒 关闭"
    print(f"\n{status} 跨Agent因子读取")
    print(f"  角色: {agent.user}")
    if role.alpha_mode:
        print(f"  ⚠️ 此角色现在可以在 prompt 中看到所有Agent的因子")



def cmd_persona(agent: Agent, args: list):
    """查看或编辑 persona.md"""
    agent._ensure_role()
    rt = _rt({"user": agent.user})
    path = rt.role._persona_path

    if not args:
        # 显示当前 persona
        if path.exists():
            print(f"📄 {path}")
            print(path.read_text(encoding="utf-8"))
        else:
            print(f"(无 persona.md — 创建文件即可生效: {path})")
            print("示例内容:")
            print("  我是抓冷型玩家，只投受让半球以上的下盘。")
            print("  偏好赔率 2.0+，不追热门。")
        return

    if args[0] == "edit":
        import subprocess
        subprocess.run(["open", str(path)])
        return

    # 直接写入: persona <文本内容>
    text = " ".join(args)
    path.write_text(text, encoding="utf-8")
    print(f"✅ persona 已写入 {path}")
    print(f"内容: {text[:100]}{'...' if len(text)>100 else ''}")


def cmd_email_orders(agent: Agent, args: list):
    """发送当前足球日未结算订单邮件"""
    from src.order_email import send_order_email
    day_str = args[0] if args else None
    send_order_email(agent.user, day_str)


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    # ── 独立命令（不需要 agent 参数）──
    if cmd == "dashboard":
        import json, re
        from collections import defaultdict
        from datetime import datetime as _dt, timezone, timedelta
        from src.data_manager import DataManager
        dm = DataManager()


        def _extract_match(lid, order_score=""):
            """从缓存或 features 文件提取比赛信息，优先用订单已存比分"""
            m = dm.get_cached_match(lid) or {}
            if m.get("home_name"):
                return m.get("home_name","?"), m.get("away_name","?"), \
                       m.get("league_name",""), str(m.get("match_time",""))[:16], \
                       order_score or m.get("score","") or ""
            # fallback: 从 features 文件解析
            feat_path = Path(f"lota_data/features/{lid}.json")
            if feat_path.exists():
                try:
                    feat = json.loads(feat_path.read_text(encoding="utf-8"))
                    fet = feat.get("compact_fet", "")
                    # ⚔️对战: 法国 🆚 摩洛哥
                    vs_m = re.search(r'对战[：:]\s*(.+?)\s*🆚\s*(.+?)(?:\n|$)', fet)
                    home = vs_m.group(1).strip() if vs_m else "?"
                    away = vs_m.group(2).strip() if vs_m else "?"
                    # ▋联赛类型: 世界杯
                    lg_m = re.search(r'联赛类型[：:]\s*(.+?)(?:\s*[｜|])', fet)
                    league = lg_m.group(1).strip() if lg_m else ""
                    # ⏰时间: 2026-07-10 04:00:00
                    tm_m = re.search(r'时间[：:]\s*([\d\-:\s]+)', fet)
                    time = tm_m.group(1).strip()[:16] if tm_m else ""
                    return home, away, league, time, order_score or ""
                except Exception:
                    pass
            return "?", "?", "", "", ""

        def _football_day(mt: str) -> str:
            """比赛开赛时间(YYYY-MM-DD HH:MM) → 足球日(当日12:01→次日12:00)。12:00前算前一足球日。"""
            from datetime import datetime as _dt, timedelta as _td
            try:
                t = _dt.strptime(mt[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                return mt[:10]
            d = t.date() if (t.hour, t.minute) >= (12, 1) else t.date() - _td(days=1)
            return d.isoformat()

        agents_list = ["alpha2狗","alpha狗","梭哈2狗","梭哈3狗","平局狗","跟风狗","均注狗","串关狗"]
        all_orders = []
        curves = {}  # agent → [{date, capital}, ...]

        for a_name in agents_list:
            a = Agent(user=a_name)
            a._ensure_role()
            rt = _rt({"user": a_name})
            capital = rt.role.capital
            role_orders = rt.role.get_orders()

            for o in role_orders:
                lid = o.get("lota_id", "")
                order_stored_score = o.get("score", "")
                legs = o.get("legs") or []
                is_parlay = bool(legs) and o.get("bet_type") == "串关"
                pick_disp = o.get("pick", "")
                hc_disp = o.get("handicap")
                reason_disp = (o.get("reason", "") or "")[:60]

                if is_parlay:
                    # 串关：按 legs 展开（每腿 对阵/选择/让球），比分取各腿
                    leg_parts, pick_parts, hc_parts = [], [], []
                    league = time = ""
                    for l in legs:
                        h, a, lg, mt, sc = _extract_match(l.get("lota_id", ""), l.get("score", ""))
                        leg_parts.append(f"{h} vs {a}" + (f" {sc}" if sc else ""))
                        pick_parts.append(l.get("pick", "?"))
                        gl = l.get("goal_line")
                        hc_parts.append(f"{float(gl):+.0f}" if isinstance(gl, (int, float)) else "-")
                        league = lg or league
                        time = mt or time
                    home = " | ".join(leg_parts)
                    away = ""
                    score = ""
                    pick_disp = " | ".join(pick_parts)
                    hc_disp = " | ".join(hc_parts)
                    reason_disp = " | ".join(
                        (l.get("llm_reason") or "")[:50] for l in legs if l.get("llm_reason")
                    )[:120]
                else:
                    home, away, league, time, score = _extract_match(lid, order_stored_score)

                    if score == '-' or len(score) < 3:
                        refreshed = dm.refresh_score_match(lid)
                        if refreshed:
                            api_score = refreshed.get("score") or f"{refreshed.get('home_score','')}:{refreshed.get('away_score','')}"
                            api_state = refreshed.get("state", 0)
                            score = api_score
                            o["score"] = score
                            if home in ("?", "") and refreshed.get("home_name"):
                                home = refreshed.get("home_name", home)
                            if away in ("?", "") and refreshed.get("away_name"):
                                away = refreshed.get("away_name", away)
                            if score and len(score) >= 3:
                                print(f"  ✅ score补全: {lid} {home} vs {away} → {score}")
                        if not score or len(score) < 3:
                            # 未开赛的不报 warning（比分空缺是正常的）
                            now_bj = _dt.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
                            is_future = False
                            if time:
                                try:
                                    if _dt.strptime(time, "%Y-%m-%d %H:%M") > now_bj:
                                        is_future = True
                                except ValueError:
                                    pass
                            if not is_future:
                                print(f"  ⚠️ score缺失: {lid} {home} vs {away} (order_score={order_stored_score!r})")
    
                all_orders.append(dict(
                    agent=a_name, capital=capital,
                    lota_id=lid,
                    match=home if is_parlay else f"{home} vs {away}",
                    league=league,
                    time=time,
                    score=score,
                    bet_type=o.get("bet_type", ""), pick=pick_disp,
                    handicap=hc_disp, odds=o.get("odds"),
                    bet_size=o.get("bet_size", 0),
                    hit=o.get("hit"), profit=o.get("profit"),
                    settled=bool(o.get("settled_at")),
                    settled_at=str(o.get("settled_at", ""))[:10],
                    reason=reason_disp,
                ))

            # ── 日资金曲线（仅已结算，按比赛日期聚合）──
            settled_orders = [o for o in role_orders if o.get("settled_at")]
            if settled_orders:
                daily_pnl = defaultdict(float)
                for o in settled_orders:
                    lid = o.get("lota_id", "")
                    _, _, _, mt, _ = _extract_match(lid, "")
                    if mt:
                        daily_pnl[_football_day(mt)] += float(o.get("profit", 0) or 0)
                # 倒推初始资金：current capital = initial + sum(all pnl)
                total_pnl = sum(daily_pnl.values())
                initial = capital - total_pnl
                # 按日期排序累加
                curve = []
                running = initial
                for dt in sorted(daily_pnl.keys()):
                    running += daily_pnl[dt]
                    curve.append({"date": dt, "capital": round(running, 0)})
                curves[a_name] = curve

        # ── 因子面板数据（自适应选择结果）──
        from src.memory import AgentMemory
        from src.factor_registry import FactorRegistry
        factors = {}
        for a_name in agents_list:
            try:
                mem = AgentMemory(a_name)
                mem.load()
                main, aux, dormant = mem.factors.selected_active()
                fp = mem.factors.factor_perf
                factors[a_name] = {
                    "total": len(fp),
                    "dormant": dormant,
                    "retired": sum(1 for v in fp.values() if v.get("status") == "retired"),
                    "main": [
                        {
                            "name": fid, "n": p["n"], "hits": p["hits"],
                            "w_return": round(p["w_return"], 2),
                            "shrunk": round(p["shrunk_rate"], 3),
                            "desc": (s.get("desc", "") or "")[:70],
                        }
                        for fid, s, p in main
                    ],
                    "aux": [
                        {
                            "name": fid,
                            "n": p["n"] if p else 0,
                            "w_return": round(p["w_return"], 2) if p else 0.0,
                        }
                        for fid, s, p in aux
                    ],
                }
            except Exception as e:
                factors[a_name] = {"error": str(e)}
        cross_text = FactorRegistry().format_for_prompt(adaptive=True)

        payload = json.dumps({
            "orders": all_orders,
            "curves": curves,
            "factors": factors,
            "cross_factors": cross_text,
            "generated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, default=str)

        html_path = Path("lota_data/dashboard.html")

        def _write_dashboard(payload_str: str, meta_refresh: str = "") -> None:
            html = html_path.read_text(encoding="utf-8")
            EMBED_MARKER = "/*__DATA_EMBED__*/"
            if EMBED_MARKER in html:
                html = html.replace(f"const EMBED = {EMBED_MARKER}", f"const EMBED = {payload_str}")
            else:
                import re
                # 用函数替换：re.sub 的字符串替换会把 payload 里的 \n 转义解释成真实换行
                html = re.sub(
                    r"const EMBED = \{.*?\};",
                    lambda m: f"const EMBED = {payload_str};",
                    html,
                    flags=re.DOTALL,
                )
            if meta_refresh:
                html = html.replace("<!--__META_REFRESH__-->", meta_refresh)
            html_path.write_text(html, encoding="utf-8")

        _write_dashboard(payload)
        print(f"✅ dashboard.html 已刷新 ({len(all_orders)} 条订单, {sum(len(v) for v in curves.values())} 个资金数据点)")

        # ── watch 模式：每 N 分钟自动重新生成 + 页面 meta 自动刷新 ──
        watch_minutes = 0
        if "--watch" in sys.argv:
            try:
                watch_minutes = int(sys.argv[sys.argv.index("--watch") + 1])
            except (ValueError, IndexError):
                watch_minutes = 10
        if watch_minutes > 0:
            import time as _t
            import os
            import subprocess
            meta = f'<meta http-equiv="refresh" content="{watch_minutes * 60}">'
            _write_dashboard(payload, meta_refresh=meta)
            os.system(f'open "{html_path}"')
            print(f"⏱ watch 模式: 每 {watch_minutes} 分钟自动刷新 (Ctrl+C 退出)")
            root = str(Path(__file__).resolve().parent)
            while True:
                _t.sleep(watch_minutes * 60)
                try:
                    subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), "dashboard"],
                        cwd=root,
                    )
                    print(f"  ⏱ {_dt.now().strftime('%H:%M:%S')} 已自动刷新")
                except Exception as e:
                    print(f"  ⚠️ watch 刷新失败: {e}")
        sys.exit(0)

    if cmd == "prefetch":
        """预取足球日窗口内所有候选比赛的 compact-fet + tags，供并发 analyze 共用。

        用法: python dsfootball_cli.py prefetch [YYYY-MM-DD]
        """
        from datetime import date as _date
        from src.environment import get_football_day, football_day_calendar_dates
        from src.data_manager import DataManager
        from src.tools import compact_fet_to_tags, save_tagged_sections

        day_str = sys.argv[2] if len(sys.argv) > 2 else None
        if day_str:
            try:
                d = _date.fromisoformat(day_str)
            except ValueError:
                print(f"[prefetch] 日期格式错误: {day_str}")
                sys.exit(1)
        else:
            # 与 batch_agents.sh 的 live 语义一致：12:00 前 → 昨天
            now = _dt.now()
            d = now.date() if now.hour >= 12 else now.date() - timedelta(days=1)
        start, end = get_football_day(d)
        cal_dates = football_day_calendar_dates(d)

        dm = DataManager()
        dm.set_live_mode(True)  # 未开赛场次强制刷新，拒绝旧缓存
        jingcai_only = "--jingcai" in sys.argv
        with_jc_odds = "--jingcai-odds" in sys.argv  # 附带竞彩让球(goal_line/赔率)

        all_matches = []
        for cd in cal_dates:
            ms = dm.refresh_matches_cache(cd, with_jc_odds=with_jc_odds)
            all_matches += ms or []

        candidates = [
            m for m in all_matches
            if start <= m.get("match_time", "") <= end
            and m.get("lota_id")
            and m.get("home_name", "?") not in ("", "?")
            and m.get("away_name", "?") not in ("", "?")
            and (not jingcai_only or m.get("jingcai_number"))
        ]
        ok = fail = 0
        for m in candidates:
            lid = m["lota_id"]
            data = dm.get_compact_fet(lid)
            if not data:
                fail += 1
                print(f"  ⚠️ prefetch 失败: {lid} {m.get('home_name','?')} vs {m.get('away_name','?')}")
                continue
            sections = compact_fet_to_tags(lid, data)
            if sections:
                save_tagged_sections(lid, sections)
            ok += 1
        dm.set_live_mode(False)
        print(f"✅ prefetch 完成: {ok}/{len(candidates)} 场 compact-fet + tags 已缓存 (窗口 {start[:10]})")
        sys.exit(0)

    if cmd == "factor-induction":
        """因子归纳 — 统一清洗/合并/补定义每日因子（alpha 跨狗 1 次，非 alpha 各自）

        用法: python dsfootball_cli.py factor-induction [--dry-run] [--limit N] [--roles a,b]
        """
        from src.factor_induction import main as induction_main
        induction_main(sys.argv[2:])
        sys.exit(0)

    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    if cmd != "agent":
        print("用法: python dsfootball_cli.py agent <user> <action> [...]")
        sys.exit(1)

    user = sys.argv[2]
    agent = Agent(user=user)

    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    action = sys.argv[3]
    rest = sys.argv[4:]

    # 检查 flags（--live 是默认行为，--no-live 切回测模式）
    live = "--no-live" not in rest
    jingcai = "--jingcai" in rest
    alpha = "--alpha" in rest
    prefetched = "--prefetched" in rest
    rest = [a for a in rest if a not in ("--live", "--no-live", "--jingcai", "--alpha", "--prefetched")]

    if action == "init":
        cmd_init(agent, rest, alpha=alpha)
    elif action == "analyze":
        cmd_analyze(agent, rest[0] if rest else None, live=live, jingcai_only=jingcai,
                    prefetched=prefetched)
    elif action == "settle":
        cmd_settle(agent, rest[0] if rest else None, live=live)
    elif action == "factor-review":
        cmd_factor_review(agent, rest)
    elif action == "factor-induction":
        cmd_factor_induction(rest)
    elif action == "run":
        cmd_run(agent, rest[0] if rest else None, live=live, jingcai_only=jingcai)
    elif action == "runall":
        if len(rest) >= 2:
            cmd_runall(agent, rest[0], rest[1], live=live, jingcai_only=jingcai)
        else:
            print("用法: python dsfootball_cli.py agent jy runall <start> <end> [--no-live] [--jingcai]")
    elif action == "status":
        cmd_status(agent)
    elif action == "persona":
        cmd_persona(agent, rest)
    elif action == "pending":
        cmd_pending(agent)
    elif action == "refresh":
        cmd_refresh(agent, rest[0] if rest else None, live=live, jingcai_only=jingcai)
    elif action == "soft-reset":
        cmd_soft_reset(agent, rest)
    elif action == "reset":
        cmd_reset(agent, rest)
    elif action == "cross-factors":
        cmd_cross_factors(agent)
    elif action == "factor-list":
        from src.factor_registry import FactorRegistry
        fr = FactorRegistry()
        date_cutoff = rest[0] if rest else None
        print(fr.format_for_prompt(current_date=date_cutoff))
    elif action == "live":
        cmd_live(agent, rest[0] if rest else None)
    elif action == "email-orders":
        cmd_email_orders(agent, rest)
    else:
        print(f"未知 action: {action}")
        print("可用: init, analyze, settle, run, runall, refresh, status, pending, persona, reset, cross-factors, factor-list, email-orders")
