"""
竞彩串关狗 — 独立于原有 agent 的串关玩法实现。

通过继承 Agent 覆写 analyze / settle 行为，不改任何原有 agent 代码。
独立角色/资金/订单（默认 user="串关狗"，落在 lota_data/roles/串关狗/）。

玩法（v1 规则版）:
  - 数据: 从本地 matches 缓存读取竞彩场次（jc_hhad: goal_line + 主/平/客赔率）
  - 选腿: 对每场让球胜平负取隐含概率最高的一边，过滤低质/极端盘口后按置信度排序
  - 下注: 票型支持 N串1 与 N过M（M串N 拆子单），赔率连乘，
          每张票占用资金按仓位百分比，子单均分
  - 结算: 每张子单仍是“全腿命中才中”（diff+goal_line 判 H/D/A）；
          N过M 命中 ≥M 场即有过关子单回本/盈利

用法:
  python3 -m src.chuan_guan_dog analyze [YYYY-MM-DD] [--dry-run] [--tickets 3串1,3过2,4过3] [--stake-pct 5]
  python3 -m src.chuan_guan_dog settle [YYYY-MM-DD]
  python3 -m src.chuan_guan_dog pending
  python3 -m src.chuan_guan_dog status
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from typing import Optional

from .agent import Agent
from .data_manager import DataManager
from .environment import football_day_calendar_dates, get_football_day
from .models import _uid
from .role import Role


_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_bj(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now(_BEIJING_TZ).strftime(fmt)


class ChuanGuanDog(Agent):
    """竞彩让球胜平负 2/3串1 玩法 agent（独立角色/资金/订单）。"""

    # ── 可调参数 ──
    DEFAULT_TICKETS = ["3串1"]      # 每天打的票型，可多张
    STAKE_PCT = 5.0                 # 每张票占用资金比例（%）
    PICK_N = 3                      # 参与排序选腿的最大场数
    MIN_ODDS = 1.30                 # 单腿赔率下限（太低=超重仓无价值）
    MAX_ODDS = 9.0                  # 单腿赔率上限（太高=隐含概率过低）
    MIN_CONF = 0.48                 # 隐含概率置信度下限
    START_CAPITAL = 10000.0

    def __init__(self, user: str = "串关狗", capital: float = START_CAPITAL):
        super().__init__(user=user)
        self._capital = capital
        self._dm = DataManager()

    # ═══════════════════════════════════════════
    # 角色 / 资金
    # ═══════════════════════════════════════════

    def _ensure_role(self):
        rt = self._runtime()
        if rt.role is None:
            try:
                rt.role = Role.load(self.user)
            except (FileNotFoundError, ValueError):
                rt.role = Role(name=self.user, capital=self._capital)
                rt.role.save()
        return rt.role

    def _runtime(self):
        from .agent import _rt
        return _rt({"user": self.user})

    # ═══════════════════════════════════════════
    # 数据
    # ═══════════════════════════════════════════

    def _jc_matches(self, day_date: str, live: bool = False) -> list[dict]:
        """读取足球日窗口内、带 jc_hhad 的竞彩场次（缓存优先，live/缺失时刷新）。"""
        d = date.fromisoformat(day_date)
        start, end = get_football_day(d)
        out = []
        for cd in football_day_calendar_dates(d):
            ms = self._dm.get_cached_jc_matches(cd)
            if live or not any(m.get("jc_hhad") for m in ms):
                ms = self._dm.refresh_matches_cache(cd, with_jc_odds=True)
            out.extend(ms or [])
        return [
            m for m in out
            if start <= m.get("match_time", "") <= end
            and m.get("jc_hhad")
            and m.get("jc_hhad").get("goal_line") is not None
        ]

    # ═══════════════════════════════════════════
    # 选腿 / 组票（可覆写为 LLM 或其他策略）
    # ═══════════════════════════════════════════

    @staticmethod
    def _implied_prob(odds: float) -> float:
        return 1.0 / odds if odds and odds > 0 else 0.0

    def _score_leg(self, match: dict) -> Optional[dict]:
        """单场让球胜平负：取隐含概率最高的一边作为腿；返回 None 表示放弃。"""
        h = match.get("jc_hhad") or {}
        try:
            ho, do, ao = float(h["home_odds"]), float(h["draw_odds"]), float(h["away_odds"])
            gl = float(h["goal_line"])
        except (KeyError, TypeError, ValueError):
            return None

        probs = {"H": self._implied_prob(ho), "D": self._implied_prob(do), "A": self._implied_prob(ao)}
        norm = sum(probs.values())
        if norm <= 0:
            return None
        for k in probs:
            probs[k] /= norm

        pick = max(probs, key=probs.get)
        odds = {"H": ho, "D": do, "A": ao}[pick]
        conf = probs[pick]
        if conf < self.MIN_CONF or odds < self.MIN_ODDS or odds > self.MAX_ODDS:
            return None

        return {
            "lota_id": match.get("lota_id", ""),
            "home_name": match.get("home_name", "?"),
            "away_name": match.get("away_name", "?"),
            "league_name": match.get("league_name", ""),
            "match_time": match.get("match_time", ""),
            "jingcai_number": match.get("jingcai_number", ""),
            "pick": pick,
            "goal_line": gl,
            "odds": odds,
            "confidence": conf,
            "jc_hhad": h,
        }

    def _select_legs(self, matches: list[dict]) -> list[dict]:
        """选出当天候选腿（按隐含置信度降序，取前 PICK_N）。"""
        legs = []
        for m in matches:
            leg = self._score_leg(m)
            if leg:
                legs.append(leg)
        legs.sort(key=lambda x: x["confidence"], reverse=True)
        return legs[: self.PICK_N]

    @staticmethod
    def _parse_ticket_spec(spec: str) -> Optional[tuple[int, int, str]]:
        """解析票型: "3串1" -> (3,3,"串"), "3过2" -> (3,2,"过")。

        返回 (腿数, 组合尺寸, 类型)：
          - N串1: 组合尺寸 = N（一注全过）
          - N过M: 组合尺寸 = M（C(N,M) 张 M串1 子单）
        """
        for sep in ("串", "过"):
            if sep in spec:
                try:
                    n = int(spec.split(sep)[0])
                    m = int(spec.split(sep)[1]) if sep == "过" else n
                    return n, m, sep
                except (ValueError, IndexError):
                    return None
        return None

    def _build_slips(self, legs: list[dict], tickets: list[str]) -> list[dict]:
        """把候选腿组票，返回若干“票单”（slip），每票含展开后的子单组合。

        - N串1: 1 张子单（全部 N 腿）
        - N过M: C(N,M) 张子单（所有 M 腿组合），如 3过2=3 张 2串1、4过3=4 张 3串1
        """
        built = []
        for tk in tickets:
            spec = self._parse_ticket_spec(tk)
            if not spec:
                continue
            n, m, sep = spec
            if m < 2 or n < m or n > len(legs):
                continue
            chosen = legs[:n]
            combos = list(combinations(chosen, m))
            sub_odds = []
            for combo in combos:
                odds = 1.0
                for leg in combo:
                    odds *= leg["odds"]
                sub_odds.append(round(odds, 4))
            built.append({
                "ticket_type": tk,
                "legs": chosen,
                "sub_ticket": f"{m}串1",
                "combos": [list(c) for c in combos],
                "sub_odds": sub_odds,
                "combos_count": len(combos),
            })
        return built

    # ═══════════════════════════════════════════
    # analyze — 覆写：不跑 LLM 图，规则选腿 + 串关下单
    # ═══════════════════════════════════════════

    def analyze(self, day_date: str = None, live: bool = False, jingcai_only: bool = True,
                prefetched: bool = False, dry_run: bool = False,
                tickets: Optional[list[str]] = None, stake_pct: Optional[float] = None) -> dict:
        day_date = day_date or self._default_day()
        tickets = tickets or list(self.DEFAULT_TICKETS)
        stake_pct = stake_pct if stake_pct is not None else self.STAKE_PCT

        session = self._begin_session("analyze", day_date)
        try:
            role = self._ensure_role()
            matches = self._jc_matches(day_date, live=live)
            legs = self._select_legs(matches)
            slips = self._build_slips(legs, tickets)

            placed, orders, skipped = 0, [], []
            for slip_i, slip in enumerate(slips):
                slip_bet = round(role.capital * stake_pct / 100.0, 2)
                if slip_bet <= 0 or slip_bet > role.capital:
                    skipped.append(f"{slip['ticket_type']} 资金不足")
                    continue
                slip_id = _uid("slip_")
                sub_bet = round(slip_bet / slip["combos_count"], 2)
                for idx, (combo, combo_odds) in enumerate(zip(slip["combos"], slip["sub_odds"]), 1):
                    order = {
                        "id": _uid("ord_"),
                        "slip_id": slip_id,
                        "slip_type": slip["ticket_type"],
                        "slip_index": idx,
                        "predict_id": "",
                        "lota_id": combo[0]["lota_id"],
                        "bet_type": "串关",
                        "ticket_type": slip["sub_ticket"],
                        "pick": "+".join(f"{l['pick']}" for l in combo),
                        "odds": combo_odds,
                        "bet_size": sub_bet,
                        "legs": list(combo),
                        "created_at": _now_bj(),
                        "settled_at": None,
                    }
                    orders.append(order)
                    if not dry_run:
                        role.place_order(order)
                    placed += 1

            return {
                "date": day_date,
                "matches_count": len(matches),
                "legs_selected": len(legs),
                "orders": orders,
                "placed": placed if not dry_run else len(orders),
                "dry_run": dry_run,
                "skipped": skipped,
                "session_path": str(session._path),
            }
        finally:
            self._end_session(session)

    # ═══════════════════════════════════════════
    # settle — 覆写：串关结算（子单全腿命中=中；N过M 命中≥M 场即有过关子单）
    # ═══════════════════════════════════════════

    def settle(self, day_date: str = None, jingcai_only: bool = True) -> dict:
        session = self._begin_session("settle", day_date or "all")
        try:
            role = self._ensure_role()
            unsettled = [o for o in role.get_orders()
                         if not o.get("settled_at") and o.get("bet_type") == "串关"]
            scores = self._fetch_scores(day_date, {lid for o in unsettled for lid in self._leg_ids(o)})

            summary = {"settled": 0, "hit": 0, "miss": 0, "push": 0, "pnl": 0.0}
            settled_orders = []
            for o in unsettled:
                result = self._settle_one(role, o, scores)
                if result is None:
                    continue  # 有腿未开赛/无比分，等下一轮
                summary["settled"] += 1
                summary["hit" if result["hit"] else "miss"] += 1
                summary["pnl"] += result["profit"]
                settled_orders.append(o)

            # 票单级汇总：N过M 命中≥M 张子单=过关；N串1 需全部子单命中
            by_slip: dict[str, list[dict]] = {}
            for o in settled_orders:
                by_slip.setdefault(o.get("slip_id", ""), []).append(o)
            slips_passed = slips_failed = 0
            for os in by_slip.values():
                spec = self._parse_ticket_spec(os[0].get("slip_type", ""))
                if spec and spec[2] == "过":
                    need = spec[1]  # N过M: 命中 ≥M 张子单
                else:
                    need = len(os)  # N串1: 全部子单命中
                hits = sum(1 for o in os if o.get("hit"))
                if hits >= need:
                    slips_passed += 1
                else:
                    slips_failed += 1
            summary["slips_passed"] = slips_passed
            summary["slips_failed"] = slips_failed
            session.settlement(summary)
            return summary
        finally:
            self._end_session(session)

    @staticmethod
    def _leg_ids(order: dict) -> list[str]:
        return [l.get("lota_id", "") for l in order.get("legs", []) if l.get("lota_id")]

    def _fetch_scores(self, day_date: Optional[str], lids: set[str]) -> dict[str, str]:
        """只取已完场(state==6)的权威比分；缓存优先，缺失逐场 API 刷新。"""
        scores = {}
        if not lids:
            return scores

        dates = set()
        if day_date:
            d = date.fromisoformat(day_date)
            dates = {day_date, (d + timedelta(days=1)).isoformat(), (d - timedelta(days=1)).isoformat()}
        else:
            dates = {(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)}

        for ds in dates:
            for m in self._dm.get_cached_matches(ds, lottery_type="all"):
                lid = m.get("lota_id", "")
                if lid in lids and m.get("state") == 6 and m.get("score"):
                    scores[lid] = m["score"]

        for lid in lids - set(scores):
            try:
                m = self._dm.refresh_score_match(lid)
                if m and m.get("state") == 6 and m.get("score"):
                    scores[lid] = m["score"]
            except Exception:
                continue
        return scores

    def _settle_one(self, role: Role, order: dict, scores: dict[str, str]) -> Optional[dict]:
        """结算一张串关子单：全部腿命中=中。任一腿无比分→跳过(返回 None)。"""
        legs = order.get("legs", [])
        leg_results = []
        for leg in legs:
            lid = leg.get("lota_id", "")
            sc = scores.get(lid, "")
            if not sc:
                return None
            h, a = (int(x) for x in sc.split(":"))
            adj = (h - a) + float(leg.get("goal_line") or 0)
            actual = "H" if adj > 0 else ("A" if adj < 0 else "D")
            leg_results.append({"lota_id": lid, "score": sc, "pick": leg.get("pick"),
                                "actual": actual, "hit": leg.get("pick") == actual})

        hit = bool(leg_results) and all(r["hit"] for r in leg_results)
        bet_size = float(order.get("bet_size") or 0)
        return_amount = round(bet_size * float(order.get("odds") or 0), 2) if hit else 0.0
        profit = round(return_amount - bet_size, 2)

        order["hit"] = hit
        order["return_amount"] = return_amount
        order["profit"] = profit
        order["legs"] = leg_results  # 写入每腿比分/判定
        order["settled_at"] = _now_bj()
        role.deposit(return_amount)
        role.save_order(order)
        role.save()
        return {"hit": hit, "profit": profit}

    # ═══════════════════════════════════════════
    # 状态辅助
    # ═══════════════════════════════════════════

    def pending(self) -> list[dict]:
        role = self._ensure_role()
        return [o for o in role.get_orders() if not o.get("settled_at")]

    def status(self) -> dict:
        role = self._ensure_role()
        orders = role.get_orders()
        settled = [o for o in orders if o.get("settled_at")]
        return {
            "user": self.user,
            "capital": role.capital,
            "total_orders": len(orders),
            "settled": len(settled),
            "pending": len(orders) - len(settled),
            "pnl": sum(o.get("profit", 0) for o in settled),
        }

    @staticmethod
    def _default_day() -> str:
        now = datetime.now(_BEIJING_TZ)
        d = now.date() if now.hour >= 12 else now.date() - timedelta(days=1)
        return d.isoformat()


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

def _fmt_order(o: dict) -> str:
    legs = o.get("legs", [])
    leg_txt = " + ".join(
        f"{l.get('home_name','?')}vs{l.get('away_name','?')} {l.get('pick','')}"
        f"({l.get('goal_line',''):+})@{l.get('odds','')}" if isinstance(l.get('goal_line'), (int, float)) else
        f"{l.get('home_name','?')}vs{l.get('away_name','?')} {l.get('pick','')}@{l.get('odds','')}"
        for l in legs
    )
    slip = o.get("slip_type", "")
    tag = f"[{slip} 第{o.get('slip_index',1)}注]" if slip else f"[{o.get('ticket_type','串关')}]"
    return (f"{tag} {o.get('ticket_type','串关')} 买 {leg_txt} "
            f"| 总赔率 {o.get('odds',0):.2f} 投注 {o.get('bet_size',0):.0f}")


def main(argv: list[str] = None) -> int:
    p = argparse.ArgumentParser(prog="chuan_guan_dog", description="竞彩串关狗")
    p.add_argument("action", choices=["analyze", "settle", "pending", "status"])
    p.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD（足球日起始日，默认当天）")
    p.add_argument("--dry-run", action="store_true", help="只预览不落单")
    p.add_argument("--tickets", default=None, help="逗号分隔，如 3串1,3过2,4过3")
    p.add_argument("--stake-pct", type=float, default=None, help="每张票资金占比%%")
    p.add_argument("--user", default="串关狗", help="角色名（独立资金/订单）")
    args = p.parse_args(argv)

    dog = ChuanGuanDog(user=args.user)
    if args.action == "analyze":
        tickets = [t.strip() for t in args.tickets.split(",")] if args.tickets else None
        r = dog.analyze(args.day, live=False, dry_run=args.dry_run,
                        tickets=tickets, stake_pct=args.stake_pct)
        print(f"📅 {r['date']} | 竞彩场次 {r['matches_count']} | 候选腿 {r['legs_selected']}")
        for o in r["orders"]:
            print("  " + _fmt_order(o))
        if r["skipped"]:
            print("  跳过:", "; ".join(r["skipped"]))
        print(f"{'🔍 dry-run 预览' if r['dry_run'] else '✅ 已下单'}: {r['placed']} 张")
    elif args.action == "settle":
        r = dog.settle(args.day)
        print(f"📊 结算: {r['settled']} 张子单 命中{r['hit']} 未中{r['miss']} PnL {r['pnl']:+.0f}"
              + (f" | 票单过关 {r['slips_passed']} 未过 {r['slips_failed']}" if r.get("slips_passed") is not None else ""))
    elif args.action == "pending":
        pend = dog.pending()
        if not pend:
            print("今日暂无待结算串关")
        for o in pend:
            print("  ⏳ " + _fmt_order(o))
    elif args.action == "status":
        s = dog.status()
        print(f"💰 资金 {s['capital']:.0f} | 订单 {s['total_orders']} (已结{s['settled']}/待{s['pending']}) | PnL {s['pnl']:+.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
