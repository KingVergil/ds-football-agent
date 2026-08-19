"""
DSFootball Python CLI — 模拟博彩环境

按足球日（12:01→次日12:00）推进：
  1. 获取当天比赛 → 剥离比分 → 喂给角色
  2. 角色（+LLM）输出 order
  3. 创建订单，扣除资金
  4. 次日获取比分 → 结算 → 更新资金
  5. 记录每日操作日志，归纳资金曲线
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, date
from collections import defaultdict
from typing import Optional, Callable

from .data_manager import DataManager
from .prompt_builder import PromptBuilder, load_system_prompt, parse_order
from .memory import AgentMemory
from .role import Role

_dm = DataManager()


def _now() -> str:
    return datetime.now().isoformat()


# ═══════════════════════════════════════════════
# 足球日工具
# ═══════════════════════════════════════════════

def get_football_day(d: date = None) -> tuple[str, str]:
    """
    足球日: 当天 12:01 → 次日 12:00。

    Args:
        d: 基准日期，None=今天

    Returns:
        (start, end) 格式 "YYYY-MM-DD HH:MM:SS"
    """
    if d is None:
        d = date.today()
    base = datetime(d.year, d.month, d.day, 12, 1, 0)
    end = base + timedelta(days=1) - timedelta(minutes=1)  # 次日 12:00
    return base.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def football_day_calendar_dates(d: date = None) -> list[str]:
    """足球日跨越的日历日期（用于 API 查询）"""
    if d is None:
        d = date.today()
    start, end = get_football_day(d)
    dates = {start[:10]}
    if end[:10] != start[:10]:
        dates.add(end[:10])
    return sorted(dates)


# ═══════════════════════════════════════════════
# 比分剥离
# ═══════════════════════════════════════════════

def strip_scores(matches: list[dict]) -> list[dict]:
    """
    剥离比分，确保不泄漏给 agent。

    移除 match dict 中的 score 字段。
    compact_fet 文本天然不含比分（仅赛前数据），做一次安全扫描。
    """
    clean = []
    for m in matches:
        m = dict(m)  # 浅拷贝
        m.pop("score", None)
        m.pop("result", None)  # 某些 API 返回可能有

        # 安全检查：compact_fet 文本中是否意外含 "比分:"
        fet = m.get("compact_fet", "")
        if fet and _scan_score_leak(fet):
            # 用正则移除潜在泄漏行
            import re
            fet = re.sub(r'比分[：:]\s*\d+[：:]\d+.*', '[比分已剥离]', fet)
            m["compact_fet"] = fet

        clean.append(m)
    return clean


def _scan_score_leak(text: str) -> bool:
    """扫描文本是否可能含当前比赛比分（安全校验）"""
    import re
    # 找 "比分: X:Y" 模式，但排除历史交锋中的比分
    # 实际 compact_fet 不含当前比分，这是防御性检查
    return bool(re.search(r'(?:最终)?比分[：:]\s*\d+\s*[：:\-]\s*\d+', text))


# ═══════════════════════════════════════════════
# DayLog
# ═══════════════════════════════════════════════

class DayLog:
    """单日操作记录"""

    def __init__(self, day_date: str):
        self.date = day_date                                         # 足球日基准日期
        self.matches_count: int = 0                                  # 当天比赛数
        self.orders_placed: int = 0                                  # 下注数
        self.orders_skipped: int = 0                                 # 跳过数
        self.total_bet: float = 0.0                                  # 总投注
        self.settled_count: int = 0                                  # 结算数（次日结算前一天）
        self.settled_hit: int = 0
        self.settled_miss: int = 0
        self.settled_push: int = 0
        self.daily_pnl: float = 0.0                                  # 当日盈亏
        self.capital_before: float = 0.0                             # 操作前资金
        self.capital_after: float = 0.0                              # 操作后资金
        self.order_ids: list[str] = []                               # 当天创建的订单 ID
        self.notes: list[str] = []                                   # 备注

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "matches_count": self.matches_count,
            "orders_placed": self.orders_placed,
            "orders_skipped": self.orders_skipped,
            "total_bet": self.total_bet,
            "settled_count": self.settled_count,
            "settled_hit": self.settled_hit,
            "settled_miss": self.settled_miss,
            "settled_push": self.settled_push,
            "daily_pnl": round(self.daily_pnl, 2),
            "capital_before": round(self.capital_before, 2),
            "capital_after": round(self.capital_after, 2),
            "notes": self.notes,
        }


# ═══════════════════════════════════════════════
# Environment
# ═══════════════════════════════════════════════

class Environment:
    """
    模拟博彩环境。

    用法:
      env = Environment(role)
      env.run_day('2026-06-12')               # 单日
      env.run_period('2026-06-12', '2026-06-20')  # 连续多日
      print(env.summary())
      print(env.capital_table())
    """

    def __init__(self, role: Role, provider=None):
        """
        Args:
            role: 角色对象
            provider: BaseLLMProvider 实例，不提供则 dry-run 模式
        """
        self.role = role
        self.provider = provider
        self.logs: list[DayLog] = []
        self._pending_orders: dict[str, list[dict]] = defaultdict(list)  # {day_date: [order_dict]}

        # 加载策略
        self.builder = PromptBuilder()
        self.system_prompt = load_system_prompt(role.system_prompt_name)
        if not self.system_prompt:
            self.system_prompt = self.builder.ensure_baseline()

    # ═══════════════════════════════════════════
    # 数据获取
    # ═══════════════════════════════════════════

    def fetch_day_matches(self, day_date: str = None, lottery_type: str = "all") -> list[dict]:
        """
        获取某足球日的比赛列表。

        Args:
            day_date: "2026-06-12"，None=今天
        """
        if day_date is None:
            d = date.today()
        else:
            d = date.fromisoformat(day_date)

        calendar_dates = football_day_calendar_dates(d)
        start, end = get_football_day(d)

        all_matches = []
        for cd in calendar_dates:
            # 先本地缓存
            matches = _dm.get_cached_matches(cd, lottery_type)
            if not matches:
                # API
                matches = _dm.fetch_matches_by_date(cd, lottery_type)
                if matches:
                    _dm.save_matches_cache(cd, matches)
            all_matches.extend(matches or [])

        # 过滤：只保留在足球日窗口内的比赛
        filtered = []
        for m in all_matches:
            mt = m.get("match_time", "")
            if start <= mt <= end:
                filtered.append(m)

        return filtered

    def fetch_scores(self, lota_ids: list[str]) -> dict[str, str]:
        """
        获取比赛比分（用于次日结算）。

        优先从 features 缓存，没有则 API。

        Returns:
            {lota_id: score_str}
        """
        scores = {}
        for lid in lota_ids:
            feat = _dm.get_cached_compact_fet(lid)
            if feat:
                sc = (feat.get("data") or {}).get("score", "")
                if sc:
                    scores[lid] = sc
                    continue
            # API
            match = _dm.fetch_match_by_id(lid)
            if match and match.get("score"):
                scores[lid] = match["score"]
        return scores

    # ═══════════════════════════════════════════
    # 核心循环
    # ═══════════════════════════════════════════

    def advance_day(self, day_date: str = None) -> DayLog:
        """
        推进一天：获取比赛 → 剥离比分 → 构建 prompt → (LLM) → 创建订单。

        如果 self.llm_call 为 None，只做数据准备（dry-run）。
        """
        if day_date is None:
            d = date.today()
            day_date = d.isoformat()
        else:
            d = date.fromisoformat(day_date)

        log = DayLog(day_date)
        log.capital_before = self.role.capital

        # 1. 获取比赛
        matches = self.fetch_day_matches(day_date)
        if not matches:
            log.notes.append("无比赛")
            self.logs.append(log)
            return log

        log.matches_count = len(matches)

        # 2. 剥离比分
        safe_matches = strip_scores(matches)

        # 3. 构建 prompt
        match_tasks = [{"lota_id": m["lota_id"]} for m in safe_matches if m.get("lota_id")]
        if not match_tasks:
            log.notes.append("无有效 lota_id")
            self.logs.append(log)
            return log

        # 刷新角色记忆
        self.role.memory.refresh_from_role(self.role)

        result = self.builder.build(
            system_prompt=self.system_prompt,
            memory=self.role.memory,
            matches=match_tasks,
        )

        # 4. LLM 调用（可选）
        if self.provider:
            # 同时传入比赛基本信息作为 user message
            user_msg = self._build_user_message(matches)
            response = self.provider.call(
                system=result["system"],
                messages=[{"role": "user", "content": user_msg}] if user_msg else [],
            )
        else:
            response = None
            log.notes.append("dry-run (无 LLM)")

        # 5. 解析 orders
        if response:
            orders = self._extract_orders(response, safe_matches)
        else:
            orders = []

        # 6. 创建订单
        for o in orders:
            if o.get("skip"):
                log.orders_skipped += 1
                continue
            try:
                created = self.role.place_order(o)
                log.order_ids.append(created.get("id", ""))
                log.orders_placed += 1
                log.total_bet += float(created.get("bet_size", 100))
            except ValueError as e:
                log.notes.append(f"下单失败: {e}")

        # 暂存待结算（次日结算）
        day_orders = [o for o in orders if not o.get("skip")]
        if day_orders:
            self._pending_orders[day_date] = day_orders

        log.capital_after = self.role.capital
        log.daily_pnl = log.capital_after - log.capital_before
        if not log.notes and log.orders_placed == 0 and log.orders_skipped > 0:
            log.notes.append(f"跳过 {log.orders_skipped} 场")

        self.logs.append(log)
        return log

    def settle_day(self, day_date: str) -> dict:
        """
        结算指定日期的订单（在次日调用）。
        重新获取比分 → 逐条判定 → 更新资金。
        """
        day_orders = self._pending_orders.pop(day_date, [])
        if not day_orders:
            # 尝试从角色订单中找未结算的
            return {"settled": 0, "hit": 0, "miss": 0, "push": 0, "pnl": 0.0}

        # 获取比分
        lota_ids = list(set(o.get("lota_id", "") for o in day_orders))
        scores = self.fetch_scores(lota_ids)

        hit = miss = push = 0
        total_pnl = 0.0

        for o in day_orders:
            lid = o.get("lota_id", "")
            sc = scores.get(lid)
            if not sc:
                continue  # 无比分则跳过

            settled = self.role.settle_order(o, sc)
            h = settled.get("hit")
            if h is True:       hit += 1
            elif h is False:    miss += 1
            else:               push += 1
            total_pnl += settled.get("profit", 0)

        # 更新当日日志的结算统计
        for log in self.logs:
            if log.date == day_date:
                log.settled_count = hit + miss + push
                log.settled_hit = hit
                log.settled_miss = miss
                log.settled_push = push
                log.daily_pnl += total_pnl
                log.capital_after = self.role.capital
                break

        return {
            "settled": hit + miss + push,
            "hit": hit,
            "miss": miss,
            "push": push,
            "pnl": round(total_pnl, 2),
        }

    def run_day(self, day_date: str = None) -> DayLog:
        """
        完整一天的模拟: advance（当天） + settle（前一天）。
        """
        if day_date is None:
            d = date.today()
            day_date = d.isoformat()

        # 结算前一天
        prev_date = (date.fromisoformat(day_date) - timedelta(days=1)).isoformat()
        self.settle_day(prev_date)

        # 推进当天
        log = self.advance_day(day_date)

        # 保存角色
        self.role.save()
        return log

    def run_period(self, start_date: str, end_date: str) -> list[DayLog]:
        """
        连续多日模拟。
        """
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        logs = []
        d = start
        while d <= end:
            log = self.run_day(d.isoformat())
            logs.append(log)
            d += timedelta(days=1)
        return logs

    # ═══════════════════════════════════════════
    # 归纳输出
    # ═══════════════════════════════════════════

    def summary(self) -> str:
        """最近操作摘要"""
        if not self.logs:
            return "(无操作记录)"

        total_bet = sum(l.total_bet for l in self.logs)
        total_pnl = sum(l.daily_pnl for l in self.logs)
        placed = sum(l.orders_placed for l in self.logs)
        skipped = sum(l.orders_skipped for l in self.logs)
        settled = sum(l.settled_count for l in self.logs)
        hits = sum(l.settled_hit for l in self.logs)
        misses = sum(l.settled_miss for l in self.logs)
        pushes = sum(l.settled_push for l in self.logs)

        lines = [
            f"📊 {self.role.name} 操作摘要 ({len(self.logs)} 天)",
            f"  比赛: {sum(l.matches_count for l in self.logs)} 场",
            f"  下注: {placed} 单 | 跳过: {skipped}",
            f"  投注: {total_bet:.0f} | 盈亏: {total_pnl:+.0f}",
            f"  结算: {settled} | 命中 {hits} | 未中 {misses} | 走水 {pushes}",
            f"  资金: {self.role.initial_capital:.0f} → {self.role.capital:.0f} "
            f"({self.role.pnl():+.0f}, {self.role.pnl()/self.role.initial_capital*100:+.1f}%)",
        ]
        return "\n".join(lines)

    def capital_table(self) -> str:
        """逐日资金表"""
        if not self.logs:
            return "(无记录)"

        lines = [
            f"{'日期':<12} {'比赛':>4} {'下单':>4} {'跳过':>4} {'投注':>8} "
            f"{'结算':>4} {'命中':>4} {'盈亏':>8} {'资金':>8}"
        ]
        lines.append("-" * 70)

        for l in self.logs:
            lines.append(
                f"{l.date:<12} {l.matches_count:>4} {l.orders_placed:>4} {l.orders_skipped:>4} "
                f"{l.total_bet:>8.0f} {l.settled_count:>4} {l.settled_hit:>4} "
                f"{l.daily_pnl:>+8.0f} {l.capital_after:>8.0f}"
            )

        lines.append("-" * 70)
        total_pnl = sum(l.daily_pnl for l in self.logs)
        lines.append(f"{'合计':<12} {'':>4} {sum(l.orders_placed for l in self.logs):>4} "
                     f"{sum(l.orders_skipped for l in self.logs):>4} "
                     f"{sum(l.total_bet for l in self.logs):>8.0f} "
                     f"{sum(l.settled_count for l in self.logs):>4} "
                     f"{sum(l.settled_hit for l in self.logs):>4} "
                     f"{total_pnl:>+8.0f} {self.role.capital:>8.0f}")

        return "\n".join(lines)

    def capital_curve(self) -> list[dict]:
        """逐日资金数据"""
        curve = []
        cap = self.role.initial_capital
        for l in self.logs:
            curve.append({
                "date": l.date,
                "capital": l.capital_after if l.capital_after > 0 else cap,
                "daily_pnl": l.daily_pnl,
            })
            if l.capital_after > 0:
                cap = l.capital_after
        return curve

    # ═══════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════

    def _build_user_message(self, matches: list[dict]) -> str:
        """构建 user message（比赛列表概览）"""
        lines = [f"以下 {len(matches)} 场比赛需要分析决策：\n"]
        for i, m in enumerate(matches):
            lines.append(
                f"{i+1}. {m.get('home_name','?')} vs {m.get('away_name','?')} "
                f"({m.get('league_name','?')} | {m.get('match_time','?')[:16]}) "
                f"lota_id: {m.get('lota_id','')}"
            )
        return "\n".join(lines)

    def _extract_orders(self, response: str, matches: list[dict]) -> list[dict]:
        """从 LLM 响应中提取所有 order"""
        import re
        orders = []
        blocks = re.findall(r'```order\n(.*?)```', response, re.DOTALL)

        # 构建 lota_id → match 映射
        match_map = {m.get("lota_id", ""): m for m in matches}

        for block in blocks:
            parsed = parse_order("```order\n" + block + "\n```")
            if not parsed:
                continue

            # 补全 lota_id（如果 LLM 没写）
            if not parsed.get("lota_id") and len(matches) == 1:
                parsed["lota_id"] = matches[0].get("lota_id", "")

            # 补全赔率
            lid = parsed.get("lota_id", "")
            if lid and not parsed.get("odds"):
                odds = _dm.get_odds(lid)
                bt = parsed.get("bet_type", "")
                pk = parsed.get("pick", "")
                if bt == "胜平负" and odds.get("eu"):
                    parsed["odds"] = odds["eu"].get(pk, 0) if pk in "HDA" else 0
                elif bt == "亚盘" and odds.get("asian"):
                    parsed["odds"] = odds["asian"].get("h" if pk == "H" else "a", 0)
                elif bt == "大小球" and odds.get("ou"):
                    parsed["odds"] = odds["ou"].get("over" if pk == "over" else "under", 0)

            orders.append(parsed)

        return orders


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Simulation Environment")
    sub = p.add_subparsers(dest="cmd")

    # day
    sp_day = sub.add_parser("day", help="查看/推进一天")
    sp_day.add_argument("date", nargs="?", default=None, help="日期 YYYY-MM-DD")
    sp_day.add_argument("--role", default="default", help="角色名")
    sp_day.add_argument("--dry-run", action="store_true", help="只准备数据不调 LLM")

    # period
    sp_period = sub.add_parser("period", help="连续多日")
    sp_period.add_argument("start", help="开始日期")
    sp_period.add_argument("end", help="结束日期")
    sp_period.add_argument("--role", default="default", help="角色名")
    sp_period.add_argument("--dry-run", action="store_true", help="只准备数据不调 LLM")

    # summary
    sp_summary = sub.add_parser("summary", help="查看摘要")
    sp_summary.add_argument("--role", default="default", help="角色名")

    # table
    sp_table = sub.add_parser("table", help="资金表")
    sp_table.add_argument("--role", default="default", help="角色名")

    args = p.parse_args()

    if args.cmd == "day":
        try:
            role = Role.load(args.role)
        except (FileNotFoundError, ValueError):
            print(f"角色 '{args.role}' 不存在，先创建:")
            role = Role(name=args.role, capital=10000)
            role.save()
            print(f"  已创建 '{args.role}' (资金 10000)")

        env = Environment(role)
        log = env.advance_day(args.date) if args.dry_run else env.run_day(args.date)

        print(f"日期: {log.date}")
        print(f"比赛: {log.matches_count} 场 | 下单: {log.orders_placed} | 跳过: {log.orders_skipped}")
        print(f"投注: {log.total_bet:.0f} | 结算: {log.settled_count} "
              f"(命中{log.settled_hit} 未中{log.settled_miss} 走水{log.settled_push})")
        print(f"资金: {log.capital_before:.0f} → {log.capital_after:.0f} (PnL {log.daily_pnl:+.0f})")
        if log.notes:
            print(f"备注: {'; '.join(log.notes)}")

    elif args.cmd == "period":
        try:
            role = Role.load(args.role)
        except (FileNotFoundError, ValueError):
            role = Role(name=args.role, capital=10000)
            role.save()

        env = Environment(role)
        env.run_period(args.start, args.end)
        print(env.capital_table())

    elif args.cmd == "summary":
        try:
            role = Role.load(args.role)
        except (FileNotFoundError, ValueError):
            print(f"角色 '{args.role}' 不存在")
            sys.exit(1)
        env = Environment(role)
        # 加载已有日志
        print(f"角色: {role.name} | 资金: {role.capital:.0f} | PnL: {role.pnl():+.0f}")
        print(role.stats())

    elif args.cmd == "table":
        try:
            role = Role.load(args.role)
        except (FileNotFoundError, ValueError):
            print(f"角色 '{args.role}' 不存在")
            sys.exit(1)
        env = Environment(role)
        print(env.capital_table())

    else:
        p.print_help()
