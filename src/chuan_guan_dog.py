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
  python3 -m src.chuan_guan_dog analyze [YYYY-MM-DD] [--dry-run] [--stake-pct 5]
      # 默认走 LLM 分析（人设+竞彩数据+7狗倾向 → 腿/票型）；--rules 强制规则版
      # 默认翻倍打法（50起步，不中翻倍中了复原）；--stake-pct N 切回固定仓位
      # 也可 --tickets 3串1,3过2 手动覆盖票型
  python3 -m src.chuan_guan_dog alpha [--exclude 均注狗]          # 开启 alpha（跨7狗因子+订单共识）
  python3 -m src.chuan_guan_dog analyze 2026-06-13 --alpha         # 开启后带 alpha 分析
  python3 -m src.chuan_guan_dog backtest 2026-06-11 2026-08-03 [--review-interval 7] [--review-mode llm|sim]
      # 迭代回测（逐日 analyze→settle；默认每7天 LLM 因子退役审查，影响后续 alpha 信任权重）
  python3 -m src.chuan_guan_dog reset        # 重置角色到 1000 起步（清订单）
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
from pathlib import Path
from typing import Optional

from .agent import Agent
from .data_manager import DataManager
from .environment import football_day_calendar_dates, get_football_day
from .models import _uid
from .role import Role

ROLES_ROOT = Path(__file__).parent.parent / "lota_data" / "roles"

_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_bj(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now(_BEIJING_TZ).strftime(fmt)


class ChuanGuanDog(Agent):
    """竞彩让球胜平负 2/3串1 玩法 agent（独立角色/资金/订单）。"""

    # ── 可调参数 ──
    STAKE_PCT = 5.0                 # 每张票占用资金比例（%）（非翻倍模式）
    USE_MARTINGALE = False          # 翻倍打法开关（保本爆赚玩法用固定仓位，不翻倍）
    MARTINGALE_BASE = 50.0          # 起步注额
    MARTINGALE_DOUBLE = 2.0         # 翻倍倍数
    MARTINGALE_MAX_PCT = 50.0       # 单注上限 = 资金 * 此比例（防爆仓）
    PICK_N = 6                      # 参与排序选腿的最大场数（支持 5过3/6过4）
    MIN_ODDS = 1.30                 # 单腿赔率下限（太低=超重仓无价值）
    MAX_ODDS = 9.0                  # 单腿赔率上限（太高=隐含概率过低）
    MIN_CONF = 0.48                 # 隐含概率置信度下限
    START_CAPITAL = 1000.0          # 与其余狗一致，1000 起步
    ALLOWED_TICKETS = ["2串1", "3串1", "4串1", "3过2", "4过3", "5过3", "6过4"]
    DECIDE_PRIORITY = ["6过4", "5过3", "4过3", "3过2", "2串1"]
    JC_ODDS_FACTOR = 0.9             # 竞彩赔率 ≈ Pinnacle 欧赔 × 0.9（返还率换算）

    # 票型玩法说明（prompt 动态生成，随 ALLOWED_TICKETS/--tickets 变化）
    TICKET_DESC = {
        "2串1": "2腿 1 注：两场全中才中奖，稳字当头",
        "3串1": "3腿 1 注：三场全中才中奖，赔率相乘",
        "4串1": "4腿 1 注：四场全中才中奖",
        "3过2": "3腿拆 3 注 2串1：命中 2/3 回本，3/3 赚 3 倍",
        "4过3": "4腿拆 4 注 3串1：命中 3/4 回本，4/4 赚 4 倍",
        "5过3": "5腿拆 10 注 3串1：命中 3/5 回本，5/5 赚 10 倍",
        "6过4": "6腿拆 15 注 4串1：命中 4/6 回本，6/6 赚 15 倍",
    }

    # ── 因子消费（纯消费，不生产；alpha 模式外也注入数据段供判断）──
    FACTOR_SECTIONS = [
        # 离散放最前：truncate 从尾部切，确保最关键的离散/必发信号不被截断
        "discrete-odds", "betfair-buysell", "asian-handicap-pinnacle",
        "eu-odds-pinnacle", "fair-odds", "over-under-crown", "match-head",
    ]
    SECTIONS_TOKEN_BUDGET = 2000     # 每场数据段 token 预算上限（超出截断）
    SECTIONS_TOTAL_BUDGET = 20000    # 全部比赛数据段总预算（超场次时均摊）
    LEG_FACTOR_MIN_HIT_RATE = 0.65   # 腿门槛：因子整体命中率 ≥65%
    LEG_FACTOR_MIN_SAMPLES = 5       # 腿门槛：因子最少样本 ≥5

    # ── alpha 模式（跨7狗因子 + 订单共识）──
    ALPHA_DOGS = ["alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗"]
    ALPHA_VOTE_W = 0.03             # 每张同向/反向加权票对置信度的贡献
    ALPHA_FACTOR_W = 0.02           # 每笔因子 history 命中/未中对置信度的贡献
    ALPHA_MAX_ADJ = 0.15            # alpha 置信度调整上限
    ALPHA_TRUST_DEFAULT = 1.0

    # ── 回测内因子退役模拟（对齐生产周度审查，仅内存生效，不写线上 factor_memory）──
    REVIEW_INTERVAL = 7             # 每 N 天跑一次模拟退役
    REVIEW_MIN_SAMPLES = 3          # 窗口内最少样本数才可退役
    REVIEW_RETIRE_RETURN = 0.0      # 窗口累计回报 ≤ 该值 → 退役候选

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
        # 派生角色（回测/探针）缺人设时，从基础串关狗复制，保证测试口径一致
        try:
            p = rt.role._persona_path
            if rt.role.name != "串关狗" and not p.exists():
                base_p = ROLES_ROOT / "串关狗" / "persona.md"
                if base_p.exists():
                    p.write_text(base_p.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        return rt.role

    def _runtime(self):
        from .agent import _rt
        return _rt({"user": self.user})

    # ═══════════════════════════════════════════
    # 数据
    # ═══════════════════════════════════════════

    def _jc_matches(self, day_date: str, live: bool = False) -> list[dict]:
        """读取足球日窗口内、带 jc_hhad 的竞彩场次（与 dsfootball_cli.py prefetch 同口径）。

        足球日 = day_date 12:01 → 次日 12:00；遍历窗口覆盖的日历日期文件，
        再按 start <= match_time <= end 过滤，只留本足球日窗口内的比赛。
        """
        d = date.fromisoformat(day_date)
        start, end = get_football_day(d)
        out = []
        for cd in football_day_calendar_dates(d):
            ms = self._dm.get_cached_jc_matches(cd)
            if live or not any(m.get("jc_hhad") for m in ms):
                ms = self._dm.refresh_matches_cache(cd, with_jc_odds=True)
            out.extend(ms or [])
        # 竞彩场次都算候选：让球未开售的也能打不让球腿
        return [m for m in out if start <= m.get("match_time", "") <= end]

    # ═══════════════════════════════════════════
    # 选腿 / 组票（可覆写为 LLM 或其他策略）
    # ═══════════════════════════════════════════

    @staticmethod
    def _implied_prob(odds: float) -> float:
        return 1.0 / odds if odds and odds > 0 else 0.0

    def _jc_odds(self, match: dict) -> dict:
        """不让球胜平负赔率近似 = Pinnacle 欧赔 × 0.9（竞彩返还率换算）。"""
        o = (self._dm.get_odds(match.get("lota_id", "")) or {}).get("eu") or {}
        try:
            return {
                "h": round(float(o["h"]) * self.JC_ODDS_FACTOR, 2),
                "d": round(float(o["d"]) * self.JC_ODDS_FACTOR, 2),
                "a": round(float(o["a"]) * self.JC_ODDS_FACTOR, 2),
            }
        except (KeyError, TypeError, ValueError):
            return {}

    def _jc_hhad_odds(self, match: dict) -> dict:
        """竞彩让球胜平负真实赔率（jc_hhad，含让球线）。"""
        h = match.get("jc_hhad") or {}
        try:
            gl = float(h["goal_line"])
        except (KeyError, TypeError, ValueError):
            gl = 0.0
        try:
            return {
                "goal_line": gl,
                "h": float(h["home_odds"]),
                "d": float(h["draw_odds"]),
                "a": float(h["away_odds"]),
            }
        except (KeyError, TypeError, ValueError):
            return {}

    def _match_sections_text(self, match: dict, budget: int = None) -> str:
        """拉取该场的因子相关数据段（离散/亚盘/必发/欧赔等），供 LLM 判断因子触发。

        参考单关狗阶段2：因子 slugs 决定取哪些段；串关固定取 7 个默认段
        （7 狗因子都建立在这些段上）。数据缺失时提示 prefetch。
        """
        lid = match.get("lota_id", "")
        if not lid:
            return ""
        try:
            text = self._dm.get_sections(lid, self.FACTOR_SECTIONS)
        except Exception:
            return ""
        if not text:
            return "(因子数据段缺失: 需 prefetch compact-fet)"
        from .prompt_builder import count_tokens, truncate_section
        if budget is None:
            budget = self.SECTIONS_TOKEN_BUDGET
        if count_tokens(text) > budget:
            text = truncate_section(text, budget)
        return text

    def _score_leg(self, match: dict) -> Optional[dict]:
        """单场竞彩胜平负（让球/不让球）：取隐含概率最高的一边作为腿；返回 None 表示放弃。"""
        h = match.get("jc_hhad") or {}
        odds = self._jc_odds(match)
        if not odds:
            try:  # 无欧赔时兜底用让球赔率
                odds = {"h": float(h["home_odds"]), "d": float(h["draw_odds"]), "a": float(h["away_odds"])}
            except (KeyError, TypeError, ValueError):
                return None
        try:
            gl = float(h["goal_line"])
        except (KeyError, TypeError, ValueError):
            gl = 0.0  # 不让球

        probs = {"H": self._implied_prob(odds.get("h")),
                 "D": self._implied_prob(odds.get("d")),
                 "A": self._implied_prob(odds.get("a"))}
        norm = sum(probs.values())
        if norm <= 0:
            return None
        for k in probs:
            probs[k] /= norm

        pick = max(probs, key=probs.get)
        leg_odds = odds["h" if pick == "H" else "d" if pick == "D" else "a"]
        conf = probs[pick]
        if conf < self.MIN_CONF or leg_odds < self.MIN_ODDS or leg_odds > self.MAX_ODDS:
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
            "odds": leg_odds,
            "confidence": conf,
            "jc_hhad": h,
        }

    def _select_legs(self, matches: list[dict]) -> list[dict]:
        """选出当天全部合格候选腿（按隐含置信度降序；alpha 调整后由 analyze 截取 PICK_N）。"""
        legs = []
        for m in matches:
            leg = self._score_leg(m)
            if leg:
                legs.append(leg)
        legs.sort(key=lambda x: x["confidence"], reverse=True)
        return legs

    def _select_legs_llm(self, matches: list[dict], day_date: str,
                         alpha_data: dict = None,
                         forced_tickets: list = None) -> tuple[Optional[list[dict]], Optional[list[str]]]:
        """LLM 分析选腿（与 Agent 同套输出契约）。

        人设 + 当天竞彩让球数据 + 7狗倾向 → 每腿一个 ```order 块 + 票型行，
        复用 parse_order 解析 + lota_id/队名校验；LLM 的 pick 直接采用，不做规则阈值过滤。

        返回 (legs, tickets)：legs=[] 表示 LLM 判定空仓（合法），
        (None, None) 表示分析失败（由 analyze 回退规则）。
        """
        import re
        from .prompt_builder import parse_order
        from .providers.deepseek import DeepSeekProvider
        role = self._ensure_role()
        provider = self._runtime().provider
        if provider is None:
            try:
                self.set_provider(DeepSeekProvider())
                provider = self._runtime().provider
            except Exception:
                return None, None
        if not matches:
            return [], None

        cand_map = {m["lota_id"]: m for m in matches if m.get("lota_id")}
        # 数据段预算：总预算按场次均摊，单场不超过上限（防 prompt 膨胀）
        sec_budget = max(
            400,
            min(self.SECTIONS_TOKEN_BUDGET,
                self.SECTIONS_TOTAL_BUDGET // max(len(matches), 1)),
        )
        lines = []
        for m in matches:
            h = m.get("jc_hhad") or {}
            jc = self._jc_odds(m)
            hhad = self._jc_hhad_odds(m)
            alpha_txt = ""
            if alpha_data:
                leans = {"H": 0, "D": 0, "A": 0}
                for dog, o in (alpha_data["orders_by_lota"].get(m["lota_id"], {}) or {}).items():
                    side = self._order_side(o)
                    if side in leans:
                        leans[side] += 1
                if any(leans.values()):
                    alpha_txt = f" | 7狗倾向 主{leans['H']}/平{leans['D']}/客{leans['A']}"
            odds_txt = f"不让球赔率 H/D/A = {jc.get('h','?')}/{jc.get('d','?')}/{jc.get('a','?')}"
            if hhad and hhad["goal_line"] != 0:
                gl = int(hhad["goal_line"])
                odds_txt += (
                    f" | 让球{gl:+d}赔率 H/D/A = "
                    f"{hhad['h']:.2f}/{hhad['d']:.2f}/{hhad['a']:.2f}"
                )
            sec = self._match_sections_text(m, budget=sec_budget)
            lines.append(
                f"- {m.get('lota_id','')} | {m.get('jingcai_number','')} {m.get('home_name','?')} vs {m.get('away_name','?')} "
                f"[{m.get('league_name','?')}] {m.get('match_time','')[:16]} "
                f"{('让球'+str(h.get('goal_line')) if h.get('goal_line') is not None else '不让球')} "
                f"{odds_txt}{alpha_txt}\n"
                f"    因子数据段:\n{sec}"
            )
        persona = role.persona_text()
        # alpha 模式（关闭时为空）：合格因子名单，仅这些因子触发的方向可当腿
        factor_hint = ""
        if alpha_data and alpha_data.get("qualified_factors"):
            qf = alpha_data["qualified_factors"]
            factor_hint = (
                "\n## 合格因子（整体命中率≥65% 且样本≥5，仅这些因子触发的方向可当腿）\n"
                + "\n".join(
                    f"- {n} [{d['role']}] 命中{d['hit_rate']:.0%} 样本{d['total']}"
                    for n, d in list(qf.items())[:15]
                )
                + "\n"
            )
        # ── 票型区（支持专注模式：--tickets 强制票型，prompt 同步收窄）──
        allowed = list(forced_tickets) if forced_tickets else list(self.ALLOWED_TICKETS)
        if forced_tickets:
            need = max(
                (spec[0] for t in allowed
                 if (spec := self._parse_ticket_spec(t)) is not None),
                default=2,
            )
            task_line = (
                f"从上面场次中选出**恰好 {need} 场**作为串关腿（一场最多一腿），"
                f"每腿给出胜平负选择（让球或不让球）；票型只能选：{', '.join(allowed)}。"
            )
            empty_line = f"不足 {need} 场好腿就空仓（只输出 票型: 无，不输出 order 块）。"
        else:
            need = 0
            task_line = (
                "从上面场次中选出 2~6 场作为串关腿（一场最多一腿），"
                "每腿给出胜平负选择（让球或不让球）；票型由你按玩法自主决定。"
            )
            empty_line = "不足 2-3 场好腿就空仓（只输出 票型: 无，不输出 order 块）。"
        desc_lines = "\n".join(
            f"- {t} = {self.TICKET_DESC[t]}" for t in allowed if t in self.TICKET_DESC
        )
        ticket_block = f"""## 任务
{task_line}

玩法理解（当前允许票型）：
{desc_lines}
- 用组合数量换容错与暴击：挂 1-2 场不伤，全中暴击

每个候选腿用一个 ```order 代码块输出（类型固定写 胜平负；让球腿 盘口=该场让球线，不让球腿 盘口=0）：
```order
lota_id: ...
类型: 胜平负
pick: H
盘口: -1
赔率: 1.47
金额: 100
理由: ≤40字
```

最后另起一行输出票型（合法票型：{', '.join(allowed)}；所需腿数不能超过实际腿数）
要求：不追高赔冷门、不吃超低蚊子肉；{empty_line}"""
        prompt = f"""你是竞彩串关分析 agent（串关狗）。

## 人设
{persona}
{factor_hint}

## {day_date} 足球日可投注竞彩胜平负场次（让球/不让球可混串）
{chr(10).join(lines)}

{ticket_block}

要求：
- lota_id 必须来自上面的列表；pick 只能是 H/D/A（H=让球后主胜，D=平，A=让球后客胜）
- 保守原则：不够 2 场好腿就空仓（只输出 票型: 无，不输出任何 order 块）；不追高赔冷门
- 结合人设风格与 7 狗倾向（如有）决策，宁缺毋滥"""
        try:
            response = provider.call(
                prompt,
                [{"role": "user", "content": "分析以上竞彩场次并输出下注决策。"}],
                temperature=0.1,
            )
        except Exception as e:
            print(f"  ⚠️ LLM 分析失败: {e}")
            return None, None
        if not response:
            return None, None

        # 记录 LLM 调用（对齐单狗 session_logger.llm_call，便于复盘/调试）
        try:
            rt = self._runtime()
            if rt.session:
                from .prompt_builder import count_tokens
                rt.session.llm_call(
                    system_prompt=prompt,
                    response=response,
                    tokens_in=count_tokens(prompt),
                    tokens_out=count_tokens(response),
                )
        except Exception:
            pass

        blocks = re.findall(r'```order\n(.*?)(?=```|\Z)', response, re.DOTALL)
        legs, seen = [], set()
        for block in blocks:
            parsed = parse_order("```order\n" + block + "\n```")
            if not parsed or parsed.get("skip"):
                continue
            lid = parsed.get("lota_id", "")
            pick = parsed.get("pick", "")
            reason = (parsed.get("reason", "") or "") + block
            if lid not in cand_map:
                matched = next(
                    (m["lota_id"] for m in matches
                     if (m.get("home_name") or "")[:2] in reason
                     or (m.get("away_name") or "")[:2] in reason),
                    None)
                if not matched:
                    print(f"  ⚠️ LLM 输出未知比赛 {lid}，跳过")
                    continue
                lid = matched
            if lid in seen or pick not in ("H", "D", "A"):
                continue
            seen.add(lid)
            m = cand_map[lid]
            h = m.get("jc_hhad") or {}
            # 盘口非 0 = 让球腿（让球线以数据为准）；0/缺省 = 不让球腿
            if parsed.get("handicap") not in (None, 0):
                try:
                    gl = float(h.get("goal_line")) if h.get("goal_line") is not None else float(parsed["handicap"])
                except (TypeError, ValueError):
                    gl = 0.0
            else:
                gl = 0.0
            # 让球腿 → 真实让球盘赔率（jc_hhad）；不让球腿 → Pinnacle 欧赔×0.9 近似
            if parsed.get("handicap") not in (None, 0):
                odds_map = self._jc_hhad_odds(m)
            else:
                odds_map = self._jc_odds(m)
            if not odds_map:
                try:  # 无欧赔兜底用让球赔率
                    odds_map = {"h": float(h["home_odds"]), "d": float(h["draw_odds"]), "a": float(h["away_odds"])}
                except (KeyError, TypeError, ValueError):
                    continue
            odds = float(odds_map["h" if pick == "H" else "d" if pick == "D" else "a"])
            if odds <= 0:
                continue
            legs.append({
                "lota_id": lid,
                "home_name": m.get("home_name", "?"),
                "away_name": m.get("away_name", "?"),
                "league_name": m.get("league_name", ""),
                "match_time": m.get("match_time", ""),
                "jingcai_number": m.get("jingcai_number", ""),
                "pick": pick,
                "goal_line": gl,
                "odds": odds,
                "confidence": round(self._implied_prob(odds), 4),
                "llm_reason": str(parsed.get("reason", ""))[:80],
                "llm": True,
                "jc_hhad": h,
            })
        if not legs:
            return [], None  # LLM 空仓是合法结论

        tickets = []
        tk_m = re.search(r'票型[：:]\s*([^\n]+)', response)
        if tk_m:
            # 只提取允许的票型 token，忽略 LLM 行尾注释；去重防重复下注
            for tk in re.findall(r"[2-6]串1|[3-6]过[2-5]", tk_m.group(1)):
                spec = self._parse_ticket_spec(tk)
                if (spec and tk in self.ALLOWED_TICKETS
                        and spec[0] <= len(legs)
                        and tk not in tickets):
                    tickets.append(tk)
        return legs[: self.PICK_N], (tickets or None)

    # ═══════════════════════════════════════════
    # alpha 模式 — 跨7狗因子 + 订单共识
    # ═══════════════════════════════════════════

    def enable_alpha(self, exclude_roles: list[str] = None) -> dict:
        """开启 alpha 模式（持久化到角色配置）。"""
        role = self._ensure_role()
        role.alpha_mode = True
        role.cross_factor_exclude = list(exclude_roles or [])
        role.save()
        return {"alpha_mode": True, "exclude": role.cross_factor_exclude}

    @staticmethod
    def _order_side(order: dict) -> Optional[str]:
        """把其他狗的订单方向映射到 H/D/A 框架（大小球无方向返回 None）。"""
        bt = order.get("bet_type", "")
        pick = order.get("pick", "")
        if bt in ("胜平负", "让球胜平负"):
            return pick if pick in ("H", "D", "A") else None
        if bt == "亚盘":
            return pick if pick in ("H", "A") else None
        return None

    def _load_alpha_data(self, day_date: str, sim_retired: set = None) -> dict:
        """聚合 7 狗数据（截至 day_date，避免未来函数）：

        - trust: 每狗信任权重 = 1 + 该狗非退役因子累计 total_return（钳制 0.2~3）
        - factor_by_lota: 因子 history 按 lota_id 索引（该场比赛被谁哪个因子命中/未中）
        - orders_by_lota: 7 狗在该场比赛的最新订单方向
        """
        from .factor_registry import FactorRegistry

        role = self._ensure_role()
        exclude = set(role.cross_factor_exclude or []) | {self.user}
        fr = FactorRegistry(exclude_roles=exclude)
        fr.refresh()
        factors = fr.get_all_factors(before_date=day_date, include_retired=False)
        sim_retired = sim_retired or set()
        factors = [f for f in factors if (f["role"], f["factor_name"]) not in sim_retired]

        trust: dict[str, float] = {}
        for f in factors:
            trust[f["role"]] = trust.get(f["role"], 1.0) + float(f.get("total_return") or 0)
        for r in trust:
            trust[r] = max(0.2, min(3.0, trust[r]))

        factor_by_lota: dict[str, list[dict]] = {}
        qualified_factors: dict[str, dict] = {}
        for f in factors:
            denom = f.get("total", 0) - f.get("push", 0)
            hit_rate = f.get("hit", 0) / denom if denom > 0 else 0.0
            qualified = (
                f.get("total", 0) >= self.LEG_FACTOR_MIN_SAMPLES
                and hit_rate >= self.LEG_FACTOR_MIN_HIT_RATE
            )
            if qualified:
                qualified_factors[f["factor_name"]] = {
                    "role": f["role"],
                    "hit_rate": round(hit_rate, 3),
                    "total": f.get("total", 0),
                }
            for h in f.get("history", []):
                lid = h.get("lota_id", "")
                if not lid:
                    continue
                factor_by_lota.setdefault(lid, []).append({
                    "role": f["role"],
                    "factor": f["factor_name"],
                    "hit": h.get("hit"),
                    "trust": trust.get(f["role"], self.ALPHA_TRUST_DEFAULT),
                    "qualified": qualified,
                    "hit_rate": round(hit_rate, 3),
                })

        orders_by_lota: dict[str, dict[str, dict]] = {}
        for dog in self.ALPHA_DOGS:
            if dog in exclude:
                continue
            try:
                dr = Role.load(dog)
            except Exception:
                continue
            for o in dr.get_orders():
                lid = o.get("lota_id", "")
                if not lid or self._order_side(o) is None:
                    continue
                prev = orders_by_lota.get(lid, {}).get(dog)
                if prev is None or o.get("created_at", "") > prev.get("created_at", ""):
                    orders_by_lota.setdefault(lid, {})[dog] = o

        return {
            "trust": trust,
            "factor_by_lota": factor_by_lota,
            "orders_by_lota": orders_by_lota,
            "qualified_factors": qualified_factors,
        }

    def _sim_factor_review(self, day_date: str, sim_retired: set) -> list[str]:
        """模拟周度因子退役（对齐生产近7天窗口，保守口径，不写线上文件）。

        对 7 狗各自因子库：窗口 (day_date-7, day_date] 内样本 ≥ REVIEW_MIN_SAMPLES
        且累计回报 ≤ REVIEW_RETIRE_RETURN 的因子 → 加入 sim_retired。
        返回本次新退役的 (role, factor) 列表。
        """
        from pathlib import Path
        role = self._ensure_role()
        exclude = set(role.cross_factor_exclude or []) | {self.user}
        roles_root = Path(__file__).parent.parent / "lota_data" / "roles"
        cutoff = (date.fromisoformat(day_date) - timedelta(days=7)).isoformat()
        retired_now = []

        for dog in self.ALPHA_DOGS:
            if dog in exclude:
                continue
            mem_path = roles_root / dog / "memory" / "factor_memory.json"
            try:
                data = json.loads(mem_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for fid, s in (data.get("factor_perf") or {}).items():
                if s.get("status") == "retired":
                    continue
                key = (dog, fid)
                if key in sim_retired:
                    continue
                hist = [h for h in s.get("history", [])
                        if cutoff < h.get("date", "") <= day_date]
                if len(hist) < self.REVIEW_MIN_SAMPLES:
                    continue
                total_return = sum(h.get("return_ratio", 0) for h in hist)
                if total_return <= self.REVIEW_RETIRE_RETURN:
                    sim_retired.add(key)
                    retired_now.append(f"{dog}/{fid}")
        return retired_now

    def _llm_factor_review(self, day_date: str, sim_retired: set) -> list[str]:
        """LLM 周度因子退役审查（对齐生产 review_all_factors.py 的 prompt/口径）。

        对 7 狗各自因子库构建候选列表（全量统计 + 近7天窗口统计 + 人设），
        调 DeepSeek LLM 出 retire/dormant 结论；仅退役结果进内存 sim_retired，
        不写线上 factor_memory.json。
        """
        import re as _re
        from pathlib import Path
        from .providers.deepseek import DeepSeekProvider

        role = self._ensure_role()
        exclude = set(role.cross_factor_exclude or []) | {self.user}
        roles_root = Path(__file__).parent.parent / "lota_data" / "roles"
        cutoff = (date.fromisoformat(day_date) - timedelta(days=7)).isoformat()
        provider = self._runtime().provider
        if provider is None:
            try:
                self.set_provider(DeepSeekProvider())
                provider = self._runtime().provider
            except Exception as e:
                print(f"  ⚠️ LLM 审查不可用({e})，回退确定性模拟")
                return self._sim_factor_review(day_date, sim_retired)

        retired_now = []
        for dog in self.ALPHA_DOGS:
            if dog in exclude:
                continue
            mem_path = roles_root / dog / "memory" / "factor_memory.json"
            try:
                data = json.loads(mem_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            fp = data.get("factor_perf") or {}
            if not fp:
                continue

            candidates: dict[str, dict] = {}
            for fid, s in fp.items():
                if s.get("status") == "retired":
                    continue
                total = int(s.get("total", 0))
                hit = int(s.get("hit", 0))
                denom = total - int(s.get("push", 0))
                hit_rate = f"{hit / denom * 100:.0f}%" if denom > 0 else "无数据"
                hist_w = [h for h in s.get("history", [])
                          if cutoff < h.get("date", "") <= day_date]
                w_ret = sum(h.get("return_ratio", 0) for h in hist_w)
                candidates[fid] = {
                    "status": s.get("status", "active"),
                    "total": total,
                    "hit_rate": hit_rate,
                    "profit": s.get("profit", 0),
                    "desc": s.get("desc", ""),
                    "first_seen": s.get("first_seen", ""),
                    "last_seen": s.get("last_seen", ""),
                    "window_n": len(hist_w),
                    "window_return": round(w_ret, 2),
                }
            if not candidates:
                continue

            persona = ""
            persona_path = roles_root / dog / "persona.md"
            if persona_path.exists():
                persona = persona_path.read_text(encoding="utf-8").strip()
            candidates_text = "\n".join(
                f"  {fid} [{c['status']}]: {c['total']}次 命中{c['hit_rate']} "
                f"盈亏{c['profit']:+.0f} | 首见={c['first_seen']} 最近={c['last_seen']}\n"
                f"    定义: {(c['desc'][:100] if c['desc'] else '(无描述)')}\n"
                f"    近7天({cutoff}~{day_date}): {c['window_n']}次 回报{c['window_return']:+.2f}"
                for fid, c in sorted(candidates.items(), key=lambda x: -x[1]["total"])
            )
            prompt = f"""你是量化足球博彩分析师，负责审查因子库健康度。

## 投注人设
{persona if persona else '(未设)'}

## 待评估因子列表（审查截止 {day_date}）
{candidates_text}

## 评估原则
你的任务不是评估因子"赢了几次"，而是判断**因子的市场假设是否还成立**：
1. 这个因子的核心假设是什么？（从定义推断）
2. 近7天窗口内是否被反复证伪？
3. 这个定价低效是否已被市场修正？
4. 是否已被更精细因子完全替代？

结论三档：retire（假设被证伪/市场修正/被替代）、dormant（近期无触发暂休眠）、active（保留）。
⚠️ 保守原则：宁可多保留，不要误删。

必须输出合法 JSON：
{{"retire": ["因子A"], "dormant": ["因子B"], "rationale": {{"因子A": "理由≤40字"}}}}
因子名必须与上方列表逐字一致。"""
            try:
                response = provider.call(
                    prompt,
                    [{"role": "user", "content": "按 JSON 格式输出。"}],
                    temperature=0.3,
                    response_format={"type": "json_object"},
                )
            except Exception as e:
                print(f"  ⚠️ {dog} LLM 审查失败: {e}")
                continue
            if not response:
                continue
            try:
                verdict = json.loads(self._extract_json(str(response)))
            except Exception:
                print(f"  ⚠️ {dog} LLM 返回非 JSON，跳过")
                continue
            for fid in verdict.get("retire", []):
                if fid in candidates and (dog, fid) not in sim_retired:
                    sim_retired.add((dog, fid))
                    retired_now.append(f"{dog}/{fid}")
        return retired_now

    def _factor_review(self, day_date: str, sim_retired: set, review_mode: str) -> list[str]:
        if review_mode == "llm":
            return self._llm_factor_review(day_date, sim_retired)
        return self._sim_factor_review(day_date, sim_retired)

    def _apply_alpha(self, legs: list[dict], alpha_data: dict) -> list[dict]:
        """结合因子与订单共识调整腿置信度，记录 alpha 信息后按新置信度排序。"""
        orders_by_lota = alpha_data["orders_by_lota"]
        factor_by_lota = alpha_data["factor_by_lota"]
        for leg in legs:
            lid = leg["lota_id"]
            support, oppose = [], []
            score = 0.0
            for dog, o in (orders_by_lota.get(lid) or {}).items():
                side = self._order_side(o)
                if side is None:
                    continue
                trust = alpha_data["trust"].get(dog, self.ALPHA_TRUST_DEFAULT)
                if side == leg["pick"]:
                    support.append(dog)
                    score += trust
                elif leg["pick"] == "D" or side in ("H", "A"):
                    oppose.append(dog)
                    score -= trust
            f_score = 0.0
            for f in factor_by_lota.get(lid, []):
                if not f.get("qualified"):
                    continue  # 腿门槛：只计入整体命中率≥65% 且样本≥5 的因子
                f_score += f["trust"] if f["hit"] is True else (-f["trust"] if f["hit"] is False else 0.0)
            adj = max(-self.ALPHA_MAX_ADJ, min(self.ALPHA_MAX_ADJ,
                      self.ALPHA_VOTE_W * score + self.ALPHA_FACTOR_W * f_score))
            leg["confidence"] = round(leg["confidence"] + adj, 4)
            leg["alpha"] = {
                "adj": round(adj, 4),
                "support": support,
                "oppose": oppose,
                "factor_hits": len(factor_by_lota.get(lid, [])),
            }
        legs.sort(key=lambda x: x["confidence"], reverse=True)
        return legs

    def _decide_tickets(self, legs: list[dict]) -> list[str]:
        """规则兜底票型：按腿数选一张最合适的（6过4 > 5过3 > 4过3 > 3过2 > 2串1）。"""
        for tk in self.DECIDE_PRIORITY:
            spec = self._parse_ticket_spec(tk)
            if spec and spec[0] <= len(legs):
                return [tk]
        return []
        return []

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

    @staticmethod
    def _extract_json(text: str) -> str:
        """从模型响应提取 JSON：剥离 [thinking] 块、代码围栏与前后文字。"""
        import re as _re
        text = _re.sub(r"\[thinking\].*?\[/thinking\]", "", text, flags=_re.S)
        text = _re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=_re.M)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return text[start:end + 1]
        return text.strip()

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

    def _martingale_stake(self, role: Role) -> float:
        """翻倍打法当前注额：从最近已结算串关订单推导连续未中次数。

        中了复原到起步注额，未中翻倍；空仓日/走水不影响阶梯。
        单注上限 = 资金 * MARTINGALE_MAX_PCT（防爆仓）。
        """
        settled = [o for o in role.get_orders()
                   if o.get("settled_at") and o.get("bet_type") == "串关"]
        streak = 0
        for o in reversed(settled):
            if o.get("hit") is False:
                streak += 1
            elif o.get("hit") is True:
                break
            # hit None（走水）跳过不计
        stake = self.MARTINGALE_BASE * (self.MARTINGALE_DOUBLE ** streak)
        cap = role.capital * self.MARTINGALE_MAX_PCT / 100.0
        stake = min(stake, cap)
        if stake < self.MARTINGALE_BASE and role.capital >= self.MARTINGALE_BASE:
            stake = self.MARTINGALE_BASE
        return round(stake, 2)

    # ═══════════════════════════════════════════
    # analyze — 覆写：不跑 LLM 图，规则选腿 + 串关下单
    # ═══════════════════════════════════════════

    def analyze(self, day_date: str = None, live: bool = False, jingcai_only: bool = True,
                prefetched: bool = False, dry_run: bool = False,
                tickets: Optional[list[str]] = None, stake_pct: Optional[float] = None,
                sim_retired: set = None, use_llm: Optional[bool] = None) -> dict:
        day_date = day_date or self._default_day()

        session = self._begin_session("analyze", day_date)
        try:
            role = self._ensure_role()
            matches = self._jc_matches(day_date, live=live)
            source = "rules"
            llm_tickets = None
            if use_llm is None:
                use_llm = True  # 默认走 LLM，无 provider/失败时自动回退规则
            if use_llm:
                alpha_data = self._load_alpha_data(day_date, sim_retired) if role.alpha_mode else None
                legs, llm_tickets = self._select_legs_llm(
                    matches, day_date, alpha_data,
                    forced_tickets=list(tickets) if tickets else None,
                )
                if legs is not None:
                    source = "llm"
                else:
                    print("  → 回退规则选腿")
                    legs = self._select_legs(matches)
            else:
                legs = self._select_legs(matches)
            if role.alpha_mode and source == "rules":
                legs = self._apply_alpha(legs, self._load_alpha_data(day_date, sim_retired))
            legs = legs[: self.PICK_N]
            tickets = list(tickets) if tickets else (llm_tickets or self._decide_tickets(legs))
            slips = self._build_slips(legs, tickets)

            # 翻倍打法硬约束：1 日最多 1 单（只保留第一张票的第一注）
            martingale_one_per_day = self.USE_MARTINGALE and stake_pct is None
            if martingale_one_per_day:
                slips = slips[:1]
                if slips:
                    slips[0]["combos"] = slips[0]["combos"][:1]
                    slips[0]["sub_odds"] = slips[0]["sub_odds"][:1]
                    slips[0]["combos_count"] = 1

            placed, orders, skipped = 0, [], []
            for slip_i, slip in enumerate(slips):
                if self.USE_MARTINGALE and stake_pct is None:
                    slip_bet = self._martingale_stake(role)
                else:
                    pct = stake_pct if stake_pct is not None else self.STAKE_PCT
                    slip_bet = round(role.capital * pct / 100.0, 2)
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
                "tickets": tickets,
                "orders": orders,
                "placed": placed if not dry_run else len(orders),
                "dry_run": dry_run,
                "alpha_mode": role.alpha_mode,
                "source": source,
                "max_one_per_day": martingale_one_per_day,
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
            leg_results.append({
                "lota_id": lid, "score": sc, "pick": leg.get("pick"),
                "goal_line": leg.get("goal_line"), "odds": leg.get("odds"),
                "actual": actual, "hit": leg.get("pick") == actual,
            })

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

    def reset(self, capital: float = None) -> dict:
        """重置角色到初始状态：资金回 START_CAPITAL、清空订单、关闭 alpha。"""
        role = self._ensure_role()
        role.capital = float(capital) if capital else float(self.START_CAPITAL)
        role.initial_capital = role.capital
        role.orders = []
        role.alpha_mode = False
        role.cross_factor_exclude = []
        role.save()
        return {"capital": role.capital, "orders": len(role.orders), "alpha_mode": role.alpha_mode}

    def backtest(self, start_date: str, end_date: str,
                 review_interval: int = None, review_mode: str = "llm",
                 mode: str = "llm", tickets: list = None) -> dict:
        """逐足球日 分析→结算 迭代回测（真实落单，按当天资金滚动仓位）。

        review_interval>0 时，每隔 N 天模拟一次因子退役（仅内存，影响 alpha 信任权重），
        默认 REVIEW_INTERVAL=7，与生产周度审查节奏一致。
        """
        review_interval = self.REVIEW_INTERVAL if review_interval is None else int(review_interval)
        review_mode = review_mode or "llm"
        d = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        rows = []
        sim_retired: set = set()
        day_idx = 0
        while d <= end:
            ds = d.isoformat()
            retired_now = []
            if review_interval > 0 and day_idx > 0 and day_idx % review_interval == 0:
                retired_now = self._factor_review(ds, sim_retired, review_mode)
            a = self.analyze(ds, sim_retired=sim_retired, use_llm=(mode == "llm"),
                             tickets=tickets)
            s = self.settle(ds)
            rows.append({
                "date": ds,
                "matches": a.get("matches_count", 0),
                "legs": a.get("legs_selected", 0),
                "tickets": a.get("tickets", []),
                "placed": a.get("placed", 0),
                "settled": s.get("settled", 0),
                "hit": s.get("hit", 0),
                "miss": s.get("miss", 0),
                "pnl": s.get("pnl", 0.0),
                "capital": self._ensure_role().capital,
                "retired": retired_now,
            })
            d += timedelta(days=1)
            day_idx += 1
        totals = {
            "placed": sum(r["placed"] for r in rows),
            "settled": sum(r["settled"] for r in rows),
            "hit": sum(r["hit"] for r in rows),
            "miss": sum(r["miss"] for r in rows),
            "pnl": round(sum(r["pnl"] for r in rows), 2),
            "capital": rows[-1]["capital"] if rows else 0,
            "empty_days": sum(1 for r in rows if r["placed"] == 0),
        }
        return {"days": rows, "totals": totals}

    @staticmethod
    def _default_day() -> str:
        """默认足球日：与 batch_agents.sh / dsfootball_cli.py 的 live 语义一致（12:00 前 → 昨天）"""
        now = datetime.now(_BEIJING_TZ)
        return now.date().isoformat() if now.hour >= 12 else (now.date() - timedelta(days=1)).isoformat()


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
    p.add_argument("action", choices=["analyze", "settle", "pending", "status", "alpha", "backtest", "reset"])
    p.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD（足球日起始日，默认当天）")
    p.add_argument("end", nargs="?", default=None, help="backtest 结束日 YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="只预览不落单")
    p.add_argument("--tickets", default=None, help="逗号分隔，如 3串1,3过2,4过3；不传则由 agent 自主决定")
    p.add_argument("--stake-pct", type=float, default=None, help="每张票资金占比%%")
    p.add_argument("--alpha", action="store_true", help="开启 alpha 模式（跨7狗因子+订单共识）并持久化")
    p.add_argument("--exclude", default=None, help="alpha 排除的角色，逗号分隔")
    p.add_argument("--rules", action="store_true", help="强制规则版选腿（不调 LLM）")
    p.add_argument("--review-interval", type=int, default=None,
                   help="backtest 因子退役模拟间隔天数（默认7，0=关闭）")
    p.add_argument("--review-mode", choices=["llm", "sim"], default="llm",
                   help="backtest 因子退役审查方式（默认 llm 走 DeepSeek；sim=确定性模拟）")
    p.add_argument("--mode", choices=["llm", "rules"], default="llm",
                   help="backtest 选腿方式（默认 llm 走 DeepSeek 分析；rules=规则版）")
    p.add_argument("--user", default="串关狗", help="角色名（独立资金/订单）")
    args = p.parse_args(argv)

    dog = ChuanGuanDog(user=args.user)
    if args.alpha:
        excl = [x.strip() for x in args.exclude.split(",")] if args.exclude else None
        dog.enable_alpha(exclude_roles=excl)
        print("🐺 alpha 模式已开启（跨7狗因子+订单共识）")
    elif args.action == "alpha":
        excl = [x.strip() for x in args.exclude.split(",")] if args.exclude else None
        dog.enable_alpha(exclude_roles=excl)
        print("🐺 alpha 模式已开启（跨7狗因子+订单共识）")
        return 0

    if args.action == "analyze":
        tickets = [t.strip() for t in args.tickets.split(",")] if args.tickets else None
        r = dog.analyze(args.day, live=False, dry_run=args.dry_run,
                        tickets=tickets, stake_pct=args.stake_pct, use_llm=not args.rules)
        tks = "+".join(r.get("tickets") or []) or "空仓"
        alpha_tag = " 🐺alpha" if r.get("alpha_mode") else ""
        src_tag = "🧠LLM" if r.get("source") == "llm" else "📐规则"
        print(f"📅 {r['date']} {src_tag}{alpha_tag} | 竞彩场次 {r['matches_count']} | 候选腿 {r['legs_selected']} | 票型 {tks}")
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
    elif args.action == "reset":
        r = dog.reset()
        print(f"♻️ 已重置: 资金 {r['capital']:.0f} | 订单 {r['orders']} | alpha {r['alpha_mode']}")
    elif args.action == "backtest":
        if not args.day or not args.end:
            print("用法: python -m src.chuan_guan_dog backtest <start> <end> [--alpha] [--review-interval 7] [--review-mode llm|sim]")
            return 1
        bt_tickets = [t.strip() for t in args.tickets.split(",")] if args.tickets else None
        r = dog.backtest(args.day, args.end, review_interval=args.review_interval,
                         review_mode=args.review_mode, mode=args.mode, tickets=bt_tickets)
        print("日期 | 竞彩 | 腿 | 票型 | 下单 | 结算(中/挂) | 当日PnL | 资金")
        for row in r["days"]:
            tks = "+".join(row["tickets"]) or "空仓"
            rev = f" 🔬退役{len(row['retired'])}" if row.get("retired") else ""
            print(f"{row['date']} | {row['matches']:>2} | {row['legs']} | {tks:<8} | {row['placed']:>2} | "
                  f"{row['settled']}({row['hit']}/{row['miss']}) | {row['pnl']:+.0f} | {row['capital']:.0f}{rev}")
            for rf in row.get("retired", []):
                print(f"      ⚰️ 退役 {rf}")
        t = r["totals"]
        print(f"汇总: 下单{t['placed']} 结算{t['settled']} 中{t['hit']} 挂{t['miss']} 空仓日{t['empty_days']} | "
              f"PnL {t['pnl']:+.0f} | 资金 {t['capital']:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
