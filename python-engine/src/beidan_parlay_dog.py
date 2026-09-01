"""
北单串关狗 — 长串（6+ 串）+ 每腿 1~3 选（默认双选）+ 开奖SP结算。

在竞彩串关狗（chuan_guan_dog.ChuanGuanDog）骨架之上派生：
  - 数据：data/beidan/*.json 的 beidan_info（goal_line + 胜平负赔率 + result + spvalue）
  - 选腿：规则版，每腿可选 1~3 个选项（单选/双选/全选）
  - 默认票型：一张 8串1，5 个全包腿（胜/平/负全选）+ 3 个单选腿，共 3^5=243 注
  - 北单每注 2 元（UNIT_STAKE），返奖 = 每注 2 元 × 开奖SP连乘 × 65%
  - 组票：N串1 / N过M，多选腿做笛卡尔积展开为单注子单
  - 结算：以官方开奖 result（3=胜/1=平/0=负）判命中，用开奖 spvalue 连乘 × 65% 返奖

用法:
  python3 -m src.beidan_parlay_dog analyze [YYYY-MM-DD] [--picks 2] [--tickets 6串1] [--dry-run] [--stake-pct 5]
  python3 -m src.beidan_parlay_dog settle [YYYY-MM-DD]
  python3 -m src.beidan_parlay_dog pending | status | reset
  python3 -m src.beidan_parlay_dog backtest <start> <end> [--picks 2] [--tickets 6串1]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .beidan_settlement import (
    BEIDAN_RETURN_RATE,
    _round2,
    parlay_combinations,
    parse_ticket_spec,
    settle_parlay_combo,
)
from .chuan_guan_dog import ChuanGuanDog
from .data_manager import DataManager
from .environment import football_day_calendar_dates, get_football_day
from .models import _uid
from .role import Role

_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_bj(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.now(_BEIJING_TZ).strftime(fmt)


class BeidanParlayDog(ChuanGuanDog):
    """北单让球胜平负长串狗（独立角色/资金/订单，默认 user=北单串关狗）。"""

    STAKE_PCT = 5.0              # 每张票占用资金比例（%）
    PICK_N = 8                   # 参与排序选腿的最大场数
    MIN_ODDS = 1.15              # 单腿赛前赔率下限
    MAX_ODDS = 12.0              # 单腿赛前赔率上限
    MIN_CONF = 0.20              # 附加选项（第2/第3选）的隐含概率下限
    MAX_PICKS = 3                # 统一模式每腿默认最多选项数；--picks 可改 1/2/3
    BEIDAN_ODDS_FACTOR = 0.68    # 欧赔兜底换算（北单 65% / Pinnacle ≈ 95%）
    START_CAPITAL = 1000.0
    BET_TYPE = "北单串关"
    UNIT_STAKE = 2.0             # 北单每注 2 元
    DEFAULT_TICKET = "8串1"
    SINGLE_PICK_LEGS = 3         # 默认策略：3 个单选腿（8串1 内）
    FULL_COVER_LEGS = 5          # 默认策略：5 个全包腿（8串1 内）
    COVER_MODE = "all"           # 全包腿模式："all"=胜/平/负全选；"top"=取前 COVER_PICKS 个
    COVER_PICKS = 3              # COVER_MODE="top" 时每腿选项数（2=双选，3=三选）
    REFLECT_HIGH_SP_TOP = 5      # 反思时按开奖SP排序，取最高N个做「高波动全包」因子归纳候选

    ALLOWED_TICKETS = [
        "2串1", "3串1", "4串1", "5串1", "6串1", "7串1", "8串1",
        "3过2", "4过3", "5过4", "6过5", "7过6", "8过7",
    ]
    DECIDE_PRIORITY = ["8串1", "7串1", "6串1", "5串1", "4串1", "3串1", "2串1"]

    def __init__(self, user: str = "北单串关狗", capital: float = START_CAPITAL):
        super().__init__(user=user, capital=capital)
        self._dm = DataManager()
        self._max_picks = self.MAX_PICKS
        self._parlay_cfg = None

    def _load_parlay_config(self) -> dict:
        """从角色目录 parlay.json 读取策略配置；缺失则回退类默认。

        让「票型/单选数/覆盖数/覆盖模式」由人设配置驱动，而不是 hardcode。
        """
        if self._parlay_cfg is None:
            cfg = {
                "ticket": self.DEFAULT_TICKET,
                "single_legs": self.SINGLE_PICK_LEGS,
                "cover_legs": self.FULL_COVER_LEGS,
                "cover_mode": self.COVER_MODE,
                "cover_picks": self.COVER_PICKS,
            }
            try:
                role = self._ensure_role()
                p = role._role_dir / "parlay.json"
                if p.exists():
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        for k in cfg:
                            if k in data and data[k] is not None:
                                cfg[k] = data[k]
            except Exception:
                pass
            self._parlay_cfg = cfg
        return self._parlay_cfg

    # ═══════════════════════════════════════════
    # 数据
    # ═══════════════════════════════════════════

    def _filter_beidan_matches(self, matches: list[dict], start: str, end: str,
                               only_upcoming: bool = False) -> list[dict]:
        """去重并只保留窗口内、带 beidan_info 的北单场次。

        only_upcoming=True（live 分析）时，已开赛（match_time <= 当前时间）的
        场次直接排除，避免把已开球比赛的赔率选进新票。
        """
        seen: set[str] = set()
        result: list[dict] = []
        now = _now_bj() if only_upcoming else ""
        for m in matches or []:
            lid = m.get("lota_id") or ""
            if not m.get("beidan_info"):
                continue
            if lid in seen:
                continue
            if not (start <= m.get("match_time", "") <= end):
                continue
            if only_upcoming and m.get("match_time", "") <= now:
                continue
            seen.add(lid)
            result.append(m)
        return result

    def _prepare_beidan_data(self, day_date: str) -> list[str]:
        """按「北单准备」流程重刷比赛缓存，并预取 compact-fet / tags。

        返回 warnings；用于 live 分析刷新拿不到比赛时兜底，避免直接 0 场。
        比赛缓存与 compact-fet 均走 DataManager 调度器（跨进程单飞），
        与竞彩狗的 prefetch 共享同一套日期锁 / 按场锁。
        """
        d = date.fromisoformat(day_date)
        start, end = get_football_day(d)
        warnings: list[str] = []
        all_matches: list[dict] = []
        for cd in football_day_calendar_dates(d):
            prep = self._dm.prepare_matches(
                cd, live=True, with_beidan_odds=True, owner=f"{self.user}:prepare"
            )
            all_matches.extend(prep.get("matches") or [])

        candidates = self._filter_beidan_matches(all_matches, start, end,
                                                 only_upcoming=True)
        if not candidates:
            warnings.append(f"{day_date} 北单数据获取失败（准备流程也未拿到比赛）")
            return warnings

        feats = self._dm.prepare_features(
            [m.get("lota_id", "") for m in candidates],
            with_tags=True, owner=f"{self.user}:prepare",
        )
        ok = feats.get("fetched", 0) + feats.get("cached", 0)
        fail = feats.get("failed", 0)
        by_lid = {m.get("lota_id", ""): m for m in candidates}
        for lid in feats.get("failed_lids", []):
            m = by_lid.get(lid) or {}
            warnings.append(
                f"{lid} {m.get('home_name', '?')} vs "
                f"{m.get('away_name', '?')} compact-fet 缺失"
            )

        print(
            f"[beidan] 准备北单流程完成：候选 {len(candidates)} 场，"
            f"compact-fet 预取 {ok}/{len(candidates)}"
        )
        if fail:
            warnings.append(f"北单 compact-fet 预取失败 {fail} 场")
        return warnings

    def _beidan_matches(self, day_date: str, live: bool = False) -> tuple[list[dict], list[str]]:
        """读取足球日窗口内、带 beidan_info 的北单场次。

        统一从 matches/<date>.json 后过滤（北单 = beidan_number 非空）；
        live 或缓存缺失 beidan_info 时按需刷新北单赔率，避免依赖过期的 beidan 历史缓存；
        live 模式额外排除已开赛场次（不回放时已开球的不进新票）。
        """
        d = date.fromisoformat(day_date)
        start, end = get_football_day(d)
        out: list[dict] = []
        warnings: list[str] = []
        for cd in football_day_calendar_dates(d):
            # 走调度器：同日期跨进程单飞 + 5 分钟新鲜窗口 + beidan_info 完整性判定；
            # 刷新返回全量比赛 → 再按北单编号过滤，避免非北单场次混入串关选项
            prep = self._dm.prepare_matches(
                cd, live=live, with_beidan_odds=True, owner=f"{self.user}:analyze"
            )
            ms = [m for m in prep.get("matches") or [] if m.get("beidan_number")]
            if not ms:
                # 迁移期 legacy beidan/<date>.json 兜底
                ms = self._dm.get_cached_beidan_matches(cd) or []
            out.extend(ms or [])

        result = self._filter_beidan_matches(out, start, end, only_upcoming=live)
        if live and not result:
            warnings.append(f"{day_date} live 刷新未拿到北单比赛，转走准备北单流程获取数据")
            warnings.extend(self._prepare_beidan_data(day_date))
            cached: list[dict] = []
            for cd in football_day_calendar_dates(d):
                cached.extend(self._dm.get_cached_beidan_matches(cd) or [])
            result = self._filter_beidan_matches(cached, start, end,
                                                 only_upcoming=live)

        if not result:
            warnings.append(f"{day_date} 窗口内无北单比赛（可能缓存缺失或当天无场次）")
        return result, warnings

    def _beidan_odds(self, match: dict) -> dict:
        """北单让球胜平负赔率（goal_line + h/d/a）。

        红线：三路赔率不完整时，单边非 0 可能是赛后只保留的胜方 SP（前视泄漏），
        绝不能拿单边当唯一可选项。
        - 三路完整 → 直接使用；
        - 让 0 且三路不完整 → 用 Pinnacle 欧赔兜底三路；
        - 让球盘（goal_line != 0）三路不完整 → 返回 {}（排除，无法兜底）。
        """
        bi = match.get("beidan_info") or {}
        try:
            gl = float(bi.get("goal_line"))
        except (TypeError, ValueError):
            gl = 0.0

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        h, d, a = _f(bi.get("home_odds")), _f(bi.get("draw_odds")), _f(bi.get("away_odds"))
        if h > 0 and d > 0 and a > 0:
            return {"goal_line": gl, "h": h, "d": d, "a": a}

        if gl != 0:
            return {}  # 让球盘无完整三路赔率，无法用欧赔兜底 → 排除

        eu = (self._dm.get_odds(match.get("lota_id", "")) or {}).get("eu") or {}
        h = _f(eu.get("h")) * self.BEIDAN_ODDS_FACTOR
        d = _f(eu.get("d")) * self.BEIDAN_ODDS_FACTOR
        a = _f(eu.get("a")) * self.BEIDAN_ODDS_FACTOR
        if h > 0 and d > 0 and a > 0:
            return {"goal_line": 0.0, "h": h, "d": d, "a": a}
        return {}

    @staticmethod
    def _implied_prob(odds: float) -> float:
        return 1.0 / odds if odds and odds > 0 else 0.0

    # ═══════════════════════════════════════════
    # 选腿（规则版，支持单选/双选/3选）
    # ═══════════════════════════════════════════

    def _beidan_score_leg(self, match: dict, max_picks: Optional[int] = None,
                          cover_all: bool = False,
                          fixed_picks: Optional[int] = None) -> Optional[dict]:
        bi = match.get("beidan_info") or {}
        odds = self._beidan_odds(match)
        side_odds = {
            "H": odds.get("h", 0.0),
            "D": odds.get("d", 0.0),
            "A": odds.get("a", 0.0),
        }
        side_odds = {k: v for k, v in side_odds.items() if v and v > 0}
        if not side_odds:
            return None

        probs = {s: self._implied_prob(o) for s, o in side_odds.items()}
        norm = sum(probs.values())
        if norm <= 0:
            return None
        for s in probs:
            probs[s] /= norm

        ranked = sorted(probs, key=lambda s: -probs[s])
        if cover_all:
            # 全包：覆盖所有可选项（胜/平/负），不做赔率区间过滤，保证该腿必中。
            picks = list(side_odds.keys())
        elif fixed_picks:
            # 固定取前 N 个高概率方向（双选/三选），不做附加概率阈值。
            n = max(1, min(int(fixed_picks), len(ranked)))
            picks = [s for s in ranked
                     if self.MIN_ODDS <= side_odds[s] <= self.MAX_ODDS][:n]
            if not picks:
                picks = ranked[:n]
        else:
            mp = int(max_picks) if max_picks else self.MAX_PICKS
            mp = max(1, min(3, mp))
            picks: list[str] = []
            for s in ranked:
                o = side_odds[s]
                if o < self.MIN_ODDS or o > self.MAX_ODDS:
                    continue
                if len(picks) >= mp:
                    break
                if len(picks) == 0 or probs[s] >= self.MIN_CONF:
                    picks.append(s)
        if not picks:
            return None

        confidence = round(sum(probs[s] for s in picks), 4)
        return {
            "lota_id": match.get("lota_id", ""),
            "home_name": match.get("home_name", "?"),
            "away_name": match.get("away_name", "?"),
            "league_name": match.get("league_name", ""),
            "match_time": match.get("match_time", ""),
            "beidan_number": match.get("beidan_number", ""),
            "goal_line": odds.get("goal_line", 0.0),
            "picks": picks,
            "odds": {s: round(side_odds[s], 4) for s in picks},
            "confidence": confidence,
            "beidan_info": bi,
        }

    def _select_legs(self, matches: list[dict],
                     max_picks: Optional[int] = None) -> list[dict]:
        legs = []
        for m in matches:
            leg = self._beidan_score_leg(m, max_picks=max_picks)
            if leg:
                legs.append(leg)
        legs.sort(key=lambda x: -x["confidence"])
        return legs

    def _select_legs_mixed(self, matches: list[dict]) -> list[dict]:
        """默认策略：按 top1 隐含概率排序，取最稳的 SINGLE_PICK_LEGS 腿单选，
        其余 FULL_COVER_LEGS 腿按 COVER_MODE 覆盖（全包/双选），组成 DEFAULT_TICKET。"""
        cfg = self._load_parlay_config()
        ranked: list[tuple[float, dict]] = []
        for m in matches:
            top = self._beidan_score_leg(m, max_picks=1)
            if top:
                ranked.append((top["confidence"], m))
        ranked.sort(key=lambda x: -x[0])

        legs: list[dict] = []
        for _, m in ranked[: cfg["single_legs"]]:
            leg = self._beidan_score_leg(m, max_picks=1)
            if leg:
                legs.append(leg)
        for _, m in ranked[cfg["single_legs"]:
                           cfg["single_legs"] + cfg["cover_legs"]]:
            leg = self._score_cover_leg(m)
            if leg:
                legs.append(leg)
        return legs

    def _score_cover_leg(self, match: dict) -> Optional[dict]:
        """全包/双选腿：COVER_MODE="all" → 全选；"top" → 固定取前 COVER_PICKS 个。"""
        cfg = self._load_parlay_config()
        if cfg["cover_mode"] == "all":
            return self._beidan_score_leg(match, cover_all=True)
        return self._beidan_score_leg(match, fixed_picks=cfg["cover_picks"])

    def _available_sides(self, match: dict) -> list[str]:
        """该场让球胜平负可投注方向（赔率>0 的边）。"""
        odds = self._beidan_odds(match)
        if not odds:
            return []
        sides = []
        for side, key in (("H", "h"), ("D", "d"), ("A", "a")):
            try:
                if float(odds.get(key)) > 0:
                    sides.append(side)
            except (TypeError, ValueError):
                pass
        return sides

    def _llm_leg(self, match: dict, picks: list[str]) -> Optional[dict]:
        """按 LLM 给定的 picks 构建腿（单选/全包通用）。"""
        bi = match.get("beidan_info") or {}
        odds = self._beidan_odds(match)
        side_odds = {"H": odds.get("h", 0.0), "D": odds.get("d", 0.0),
                     "A": odds.get("a", 0.0)}
        valid = [p for p in picks if side_odds.get(p, 0.0) > 0]
        if not valid:
            return None
        probs = {s: self._implied_prob(side_odds[s]) for s in side_odds
                 if side_odds[s] > 0}
        norm = sum(probs.values()) or 1.0
        confidence = round(sum(probs.get(p, 0.0) for p in valid) / norm, 4)
        return {
            "lota_id": match.get("lota_id", ""),
            "home_name": match.get("home_name", "?"),
            "away_name": match.get("away_name", "?"),
            "league_name": match.get("league_name", ""),
            "match_time": match.get("match_time", ""),
            "beidan_number": match.get("beidan_number", ""),
            "goal_line": odds.get("goal_line", 0.0),
            "picks": valid,
            "odds": {p: round(side_odds[p], 4) for p in valid},
            "confidence": confidence,
            "beidan_info": bi,
            "llm": True,
        }

    def _select_legs_llm(self, matches: list[dict], day_date: str
                         ) -> tuple[Optional[list[dict]], Optional[list[dict]]]:
        """LLM 分析（对齐 agent.py 的 build_prompt→call_llm→parse 写法，
        不修改 agent.py）：system 放目标/决策要求/人设/场次，user 放一句指令。

        返回 (singles, covers)。(None, None) 表示 LLM 失败（由调用方回退规则）。"""
        import json as _json
        from .providers.deepseek import DeepSeekProvider

        role = self._ensure_role()
        provider = self._runtime().provider
        if provider is None:
            try:
                self.set_provider(DeepSeekProvider())
                provider = self._runtime().provider
            except Exception as e:
                print(f"  ⚠️ LLM 不可用: {e}")
                return None, None
        if not matches:
            return [], []

        valid_matches = []
        for m in matches:
            o = self._beidan_odds(m)
            if o:
                m["_beidan_odds"] = o
                valid_matches.append(m)
        matches = valid_matches
        cand_map = {m.get("lota_id"): m for m in matches if m.get("lota_id")}

        as_of = None
        if day_date:
            try:
                from datetime import datetime as _dt
                as_of = _dt.strptime(day_date, "%Y-%m-%d")
            except ValueError:
                as_of = None

        factor_text = self._self_factor_text(role, as_of)
        factor_slugs = self._self_factor_slugs(role, as_of)
        alpha_data = None
        if role.alpha_mode:
            try:
                alpha_data = self._load_alpha_data(day_date)
            except Exception as e:
                print(f"  ⚠️ alpha 数据加载失败（不影响分析）: {e}")
        lines = []
        match_info_parts = []
        match_features: dict[str, str] = {}
        for m in matches:
            o = m["_beidan_odds"]
            # 全量数据段，不截断（1M 上下文足够）
            sec = self._match_sections_text(m, budget=None,
                                            extra_slugs=factor_slugs)
            lid = m.get("lota_id") or ""
            if lid and sec:
                match_features[lid] = sec
            alpha_txt = ""
            if alpha_data:
                leans = {"H": 0, "D": 0, "A": 0}
                for _dog, _o in (alpha_data.get("orders_by_lota", {})
                                 .get(m.get("lota_id"), {}) or {}).items():
                    side = self._order_side(_o)
                    if side in leans:
                        leans[side] += 1
                if any(leans.values()):
                    alpha_txt = (f" | 单关狗倾向 主{leans['H']}/平{leans['D']}/客{leans['A']}")
            info = (
                f"- {m.get('lota_id')} | {m.get('home_name')} vs {m.get('away_name')} "
                f"[{m.get('league_name', '')}] 让{o.get('goal_line')} "
                f"赔率H/D/A={o.get('h')}/{o.get('d')}/{o.get('a')}"
                f"{alpha_txt}"
            )
            match_info_parts.append(info)
            lines.append(info + "\n   因子数据段:\n" + sec)
        matches_text = "\n".join(lines)
        self._assert_prompt_redacted("\n".join(match_info_parts))
        persona = role.persona_text() or "(未设人设)"
        cfg = self._load_parlay_config()
        n_legs = int(cfg["single_legs"]) + int(cfg["cover_legs"])
        if cfg["cover_mode"] == "all":
            cover_desc = "全包(H/D/A)"
            cover_note = "每场 H/D/A 全选，保证这 {n} 场必中".format(n=cfg["cover_legs"])
        else:
            cover_desc = "双选" if cfg["cover_picks"] == 2 else "三选"
            cover_note = "每场取前 {n} 个高概率方向".format(n=cfg["cover_picks"])
        combo_count = int(cfg["cover_picks"]) ** int(cfg["cover_legs"])
        factor_block = ""
        if factor_text:
            factor_block = (
                "## 你自己的因子库（结算反思产出，供选腿参考；方向型因子支持的方向可做单选）\n"
                + factor_text + "\n"
            )
        factor_cue_block = (
            "## 🧭 因子性质判断（重要：用之前先判断，不要按名字/关键词套）\n"
            "- **用每个因子前，先读它的描述判断性质**：\n"
            "- ① 它给出**明确可下注方向**（主/客/平某一侧）→ 可作**单选方向**，但**仍需离散/资金/盘口同向**。\n"
            "- ② 它只**提示风险/波动/爆冷、需要防冷或全包** → 只能进**全包/防冷**，**绝不拿去单选**。\n"
            "- 🔄反向（负回报）→ 一律规避或全包，绝不单选。\n"
            "- 判断依据是「用它会给你一个方向，还是只提醒你小心」。**读描述语义判断，不要按因子名套用**。\n\n"
        )
        alpha_block = ""
        if alpha_data and alpha_data.get("qualified_factors"):
            qf_lines = "\n".join(
                f"- {name} [{info.get('role')}] 命中{info.get('hit_rate', 0):.0%} "
                f"样本{info.get('total', 0)}"
                for name, info in list(alpha_data["qualified_factors"].items())[:20]
            )
            alpha_block = (
                "## 跨狗合格因子（其他单关狗，命中率≥65% 且样本≥5，用于确认单选方向）\n"
                + qf_lines + "\n"
            )

        system = f"""你是北单串关分析 agent（{role.name}）。

## 目标（goal）
用北单让球胜平负出一张 {cfg['ticket']}：{cfg['cover_legs']} 场{cover_desc} + {cfg['single_legs']} 场单选，
共 {cfg['cover_picks']}^{cfg['cover_legs']} = {combo_count} 注，每注 2 元。
- 单选：选 {cfg['single_legs']} 场最有把握的，每场只选一个方向 H/D/A（H=让球后主胜，D=平，A=让球后客胜）。
- {cover_desc}：另选 {cfg['cover_legs']} 场，{cover_note}。
- 单选场次只吃确定性高的方向，不吃超低蚊子肉、不追高赔冷门。

## 决策要求
1. lota_id 必须来自下面列表。
2. 只输出合法 JSON，不要输出任何解释，格式固定：
{{"singles":[{{"lota_id":"Lota...","pick":"H"}}, ...], "covers":["Lota...","Lota..."], "empty":false}}
3. singles 恰好 {cfg['single_legs']} 条；covers 恰好 {cfg['cover_legs']} 个不同 lota_id，且与 singles 不重复。
4. 凑不齐 {n_legs} 场好腿就 empty:true，singles 和 covers 都为空数组。

## 思考步骤（输出前必做，思考过程写在 thinking 里，最终只输出 JSON）
1. 逐场分类：把「离散/资金/盘口方向一致、低水保护」的稳定场放进单选候选；
   把「离散矛盾、资金背离、盘口反复、可能爆冷」的场放进覆盖候选。
2. 单选定方向：只从稳定候选里选 {cfg['single_legs']} 场，方向取信号最一致的一边，不吃蚊子肉、不追高赔。
3. 覆盖选 {cfg['cover_legs']} 场：优先把最可能爆冷的 {cfg['cover_legs']} 场放进 covers（{cover_note}）。
4. 校验：singles {cfg['single_legs']} 条、covers {cfg['cover_legs']} 条、不重复；凑不齐就 empty:true。

## 人设
{persona}
{factor_block}
{factor_cue_block}
{alpha_block}

## {day_date} 北单可投注场次（含让球线 goal_line）
{matches_text}
"""
        user_msg = f"分析以上 {len(matches)} 场北单比赛并输出串关决策。"
        from .prompt_builder import count_tokens
        tokens_in = count_tokens(system)
        data_tokens = count_tokens(matches_text)
        mem_tokens = count_tokens(factor_block) + count_tokens(alpha_block)
        user_tokens = count_tokens(user_msg)
        token_breakdown = {
            "sys": max(0, tokens_in - data_tokens - mem_tokens - user_tokens),
            "mem": mem_tokens,
            "tools": 0,
            "data": data_tokens,
            "user": user_tokens,
        }
        # 对齐 agent 的 build_prompt 工具调用记录（复盘用）
        try:
            rt = self._runtime()
            if rt.session:
                rt.session.tool_call("build_prompt", {
                    "match_count": len(matches),
                    "ticket": cfg["ticket"],
                    "single_legs": cfg["single_legs"],
                    "cover_legs": cfg["cover_legs"],
                }, f"{tokens_in} tokens")
        except Exception:
            pass

        try:
            response = provider.call(
                system,
                [{"role": "user", "content": user_msg}],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            print(f"  ⚠️ LLM 分析失败: {e}")
            return None, None
        if not response:
            return None, None

        # 记录 LLM 调用（对齐 agent node_call_llm / 父类 chuan_guan_dog 的 llm_call）
        try:
            rt = self._runtime()
            if rt.session:
                rt.session.llm_call(
                    system_prompt=system,
                    response=response,
                    tokens_in=tokens_in,
                    tokens_out=count_tokens(response),
                    token_breakdown=token_breakdown,
                    match_features=match_features,
                )
        except Exception:
            pass

        try:
            data = _json.loads(self._extract_json(str(response)))
        except Exception as e:
            print(f"  ⚠️ LLM 返回非 JSON: {e}\n{str(response)[:400]}")
            return None, None

        if data.get("empty"):
            return [], []

        singles: list[dict] = []
        covers: list[dict] = []
        seen: set[str] = set()
        for item in (data.get("singles") or []):
            if not isinstance(item, dict):
                continue
            lid = item.get("lota_id")
            pick = item.get("pick")
            m = cand_map.get(lid)
            if not m or lid in seen or pick not in ("H", "D", "A"):
                continue
            leg = self._llm_leg(m, [pick])
            if leg:
                seen.add(lid)
                singles.append(leg)
        for lid in (data.get("covers") or []):
            m = cand_map.get(lid)
            if not m or lid in seen:
                continue
            # 覆盖腿按配置构建（全包/双选/三选），不写死全包
            leg = self._score_cover_leg(m)
            if leg:
                seen.add(lid)
                covers.append(leg)
        return singles, covers

    @staticmethod
    def _assert_prompt_redacted(text: str) -> None:
        """红线护栏：北单 analyze prompt 不得出现开奖结果/SP/比分等未来字段。"""
        for token in ("result", "spvalue", "score", "draw_datetime", "result_des"):
            if token in text:
                raise RuntimeError(f"北单 analyze prompt 泄漏未来字段: {token}")

    # ═══════════════════════════════════════════
    # 组票（多选笛卡尔积展开）
    # ═══════════════════════════════════════════

    def _build_slips(self, legs: list[dict], tickets: list[str]) -> list[dict]:
        built: list[dict] = []
        for tk in tickets:
            spec = parse_ticket_spec(tk)
            if not spec:
                continue
            if spec.m < 2 or spec.n < spec.m or spec.n > len(legs):
                continue
            chosen = legs[: spec.n]
            combos = parlay_combinations(chosen, spec)
            if not combos:
                continue
            sub_odds = []
            for combo in combos:
                o = 1.0
                for leg in combo:
                    o *= float(leg.get("odds") or 0.0)
                sub_odds.append(round(o, 4))
            built.append({
                "ticket_type": tk,
                "legs": chosen,
                "sub_ticket": f"{spec.m}串1",
                "combos": combos,
                "sub_odds": sub_odds,
                "combos_count": len(combos),
            })
        return built

    def _build_default_slips(self, singles: list[dict],
                             covers: list[dict]) -> list[dict]:
        """默认策略组票：覆盖腿 + 单选腿组成一张 DEFAULT_TICKET。"""
        legs = list(singles) + list(covers)
        cfg = self._load_parlay_config()
        return self._build_slips(legs, [cfg["ticket"]])

    def _decide_tickets(self, legs: list[dict]) -> list[str]:
        for tk in self.DECIDE_PRIORITY:
            spec = parse_ticket_spec(tk)
            if spec and spec.n <= len(legs):
                return [tk]
        return []

    # ═══════════════════════════════════════════
    # analyze — 规则选腿 + 串关下单
    # ═══════════════════════════════════════════

    def analyze(self, day_date: str = None, live: bool = False,
                dry_run: bool = False, tickets: Optional[list[str]] = None,
                stake_pct: Optional[float] = None, max_picks: Optional[int] = None,
                use_llm: bool = False) -> dict:
        day_date = day_date or self._default_day()
        self._max_picks = 3

        session = self._begin_session("analyze", day_date)
        try:
            role = self._ensure_role()
            matches, data_warnings = self._beidan_matches(day_date, live=live)
            if live:
                matches = self._live_clean_matches(matches)
            llm_used = False
            if use_llm:
                singles, covers = self._select_legs_llm(matches, day_date)
                if singles is None:
                    print("  → LLM 失败，回退规则 8串1(5全包+3单选)")
                    legs = self._select_legs_mixed(matches)
                    singles = legs[: self.SINGLE_PICK_LEGS]
                    covers = legs[self.SINGLE_PICK_LEGS:
                                  self.SINGLE_PICK_LEGS + self.FULL_COVER_LEGS]
                else:
                    llm_used = True
                slips = self._build_default_slips(singles, covers)
                tickets = [self.DEFAULT_TICKET]
                legs_selected = len(singles) + len(covers)
            elif max_picks is None and tickets is None:
                legs = self._select_legs_mixed(matches)
                singles = legs[: self.SINGLE_PICK_LEGS]
                covers = legs[self.SINGLE_PICK_LEGS:
                              self.SINGLE_PICK_LEGS + self.FULL_COVER_LEGS]
                slips = self._build_default_slips(singles, covers)
                tickets = [self.DEFAULT_TICKET]
                legs_selected = len(singles) + len(covers)
            else:
                self._max_picks = int(max_picks) if max_picks else self.MAX_PICKS
                self._max_picks = max(1, min(3, self._max_picks))
                legs = self._select_legs(matches, max_picks=self._max_picks)
                tickets = list(tickets) if tickets else self._decide_tickets(legs)
                legs = legs[: self.PICK_N]
                slips = self._build_slips(legs, tickets)
                legs_selected = len(legs)

            placed, orders, skipped = 0, [], []
            for slip in slips:
                if slip["combos_count"] <= 0:
                    skipped.append(f"{slip['ticket_type']} 无有效组合")
                    continue
                # 北单固定每注 2 元
                slip_cost = self.UNIT_STAKE * slip["combos_count"]
                if slip_cost > role.capital:
                    skipped.append(f"{slip['ticket_type']} 资金不足(需{slip_cost:.0f})")
                    continue
                slip_id = _uid("slip_")
                bet_type = self.BET_TYPE
                # 一张票 = 一条 slip 级订单：k 条腿（含 picks 列表）存一次；
                # 笛卡尔积组合不再落盘，结算时现算（见 settle()）。
                combo_odds = slip["sub_odds"] or []
                order = {
                    "id": _uid("ord_"),
                    "slip_id": slip_id,
                    "slip_type": slip["ticket_type"],
                    "slip_index": 1,
                    "combos_count": slip["combos_count"],
                    "unit_stake": self.UNIT_STAKE,
                    "total_stake": _round2(slip_cost),
                    "ticket_legs": list(slip["legs"]),
                    "predict_id": "",
                    "lota_id": slip["legs"][0]["lota_id"] if slip["legs"] else "",
                    "bet_type": bet_type,
                    "ticket_type": slip["sub_ticket"],
                    "pick": f"{slip['ticket_type']} x{slip['combos_count']}注",
                    "odds": max(combo_odds) if combo_odds else 0.0,
                    "bet_size": self.UNIT_STAKE,
                    "legs": list(slip["legs"]),
                    "created_at": _now_bj(),
                    "settled_at": None,
                }
                orders.append(order)
                if not dry_run:
                    role.withdraw(slip_cost)
                    role.save_order(order)
                placed += 1

            return {
                "date": day_date,
                "matches_count": len(matches),
                "legs_selected": legs_selected,
                "tickets": tickets,
                "max_picks": self._max_picks,
                "llm_used": llm_used,
                "orders": orders,
                "placed": placed if not dry_run else len(orders),
                "dry_run": dry_run,
                "skipped": skipped,
                "warnings": data_warnings,
                "session_path": str(session._path),
            }
        finally:
            self._end_session(session)

    # ═══════════════════════════════════════════
    # settle — 北单开奖 result + spvalue 结算（65% 返奖）
    # ═══════════════════════════════════════════

    def _fetch_beidan_results(self, day_date: Optional[str],
                              lids: set[str]) -> dict[str, dict]:
        """结算前先通过 beidan/sp 接口把开奖 result/spvalue 合并进本地缓存，再读回。

        走 DataManager.prepare_beidan_sp：同一 sp_date 跨进程单飞，
        并发结算（多只北单狗）只拉一次线上，窗口内重复结算直接读本地。
        """
        if day_date:
            try:
                # settle 的 day_date 是窗口结束日（起始日 + 1）；beidan/sp 按足球日起始日键控，故 -1 天
                sp_date = (date.fromisoformat(day_date) - timedelta(days=1)).isoformat()
                prep = self._dm.prepare_beidan_sp(sp_date, owner=f"{self.user}:settle")
                if prep.get("warning"):
                    print(f"  ⚠️ {prep['warning']}")
            except Exception as e:
                print(f"  ⚠️ 开奖SP拉取失败（继续用本地缓存）: {e}")
        return self._dm.get_cached_beidan_results(lids)

    def _settle_one(self, role: Role, order: dict,
                    beidan_map: dict[str, dict]) -> Optional[dict]:
        legs = [dict(l) for l in order.get("legs", [])]
        bet_size = float(order.get("bet_size") or 0)
        res = settle_parlay_combo(legs, beidan_map, bet_size)
        if not res.get("ready"):
            return None
        order["hit"] = res.get("hit")
        order["push"] = res.get("push", False)
        order["all_void"] = res.get("all_void", False)
        order["sp_product"] = res.get("sp_product", 1.0)
        order["return_amount"] = res.get("return_amount", 0.0)
        order["profit"] = res.get("profit", 0.0)
        order["settlement_rate"] = BEIDAN_RETURN_RATE
        order["legs"] = res.get("leg_results", legs)
        order["settled_at"] = _now_bj()
        role.deposit(res.get("return_amount", 0.0))
        role.save_order(order)
        role.save()
        return {"hit": res.get("hit"), "profit": res.get("profit", 0.0)}

    def _judge_ticket_legs(self, ticket_legs: list[dict],
                           beidan_map: dict[str, dict]) -> Optional[dict]:
        """只判一次：根据 ticket_legs 判断整张 8串1 的腿级结果。

        单选腿命中才可能中奖；全包腿（H/D/A）天然覆盖，故只需判断单选腿是否打出。
        返回 {leg_map, single_hit, sp_product}；缺失开奖或未到结算时点返回 None。
        """
        from .beidan_settlement import VOID_SP, settle_leg

        leg_map: dict[str, dict] = {}
        for leg in ticket_legs:
            lid = leg.get("lota_id") or ""
            info = beidan_map.get(lid)
            if info is None:
                return None
            picks = leg.get("picks") or ([leg.get("pick")] if leg.get("pick") else [])
            probe = picks[0] if picks else "H"
            sl = settle_leg(probe, info)
            if not sl.get("ready"):
                return None
            leg_map[lid] = sl

        single_hit = True
        for leg in ticket_legs:
            picks = leg.get("picks") or ([leg.get("pick")] if leg.get("pick") else [])
            if len(picks) == 1:
                sl = leg_map[leg.get("lota_id")]
                if not sl.get("hit"):
                    single_hit = False
                    break

        sp_product = 1.0
        for leg in ticket_legs:
            sl = leg_map[leg.get("lota_id")]
            sp_product *= VOID_SP if sl.get("push") else sl.get("sp", 0.0)

        return {"leg_map": leg_map, "single_hit": single_hit,
                "sp_product": sp_product}

    def settle(self, day_date: str = None, reflect: bool = True) -> dict:
        session = self._begin_session("settle", day_date or "all")
        try:
            role = self._ensure_role()
            unsettled = [o for o in role.get_orders()
                         if not o.get("settled_at")
                         and o.get("bet_type") == self.BET_TYPE]
            if not unsettled:
                summary = {"settled": 0, "hit": 0, "miss": 0, "push": 0,
                           "pnl": 0.0, "slips_any_hit": 0, "slips_total": 0}
                session.settlement(summary)
                return summary

            lids = {lid for o in unsettled for lid in self._leg_ids(o)}
            beidan_map = self._fetch_beidan_results(day_date, lids)

            summary = {"settled": 0, "hit": 0, "miss": 0, "push": 0, "pnl": 0.0,
                       "slips_any_hit": 0, "slips_total": 0}
            settled_orders: list[dict] = []
            total_return = 0.0

            # 每张票 = 一条 slip 级 order。组合不落盘：settle 时用 parlay_combinations
            # 现算每注（每腿单选），再逐注走 settle_parlay_combo 判定与派彩。
            for o in unsettled:
                legs = o.get("legs") or []
                ticket_legs = o.get("ticket_legs") or legs
                spec = parse_ticket_spec(o.get("slip_type") or o.get("ticket_type"))
                combos = parlay_combinations(legs, spec) if spec else []
                if not combos:
                    continue
                unit_stake = float(o.get("bet_size") or self.UNIT_STAKE)
                combos_count = int(o.get("combos_count") or len(combos))
                total_stake = _round2(float(o.get("total_stake")
                                           or unit_stake * len(combos)))

                # 先判整票腿级结果（ready 才可结算；同时回填每腿 actual/sp/push）
                judged = self._judge_ticket_legs(ticket_legs, beidan_map)
                if judged is None:
                    continue  # 未到结算时点，整票跳过
                leg_map = judged["leg_map"]
                for l in legs:
                    sl = leg_map.get(l.get("lota_id"))
                    if not sl:
                        continue
                    l["actual"] = sl.get("actual")
                    l["sp"] = sl.get("sp")
                    l["push"] = sl.get("push", False)
                    l["hit"] = bool(sl.get("hit"))

                # 票级 SP 连乘（用于快照/展示；串1 下即中奖注的 SP 连乘）
                sp_product = 1.0
                for l in legs:
                    sp_product *= float(l.get("sp") or 1.0)

                slip_return = 0.0
                hit_combos = 0
                all_void = True
                slip_ready = True
                for combo in combos:
                    r = settle_parlay_combo(combo, beidan_map, unit_stake)
                    if not r.get("ready"):
                        slip_ready = False
                        break
                    slip_return += r.get("return_amount", 0.0)
                    if r.get("hit"):
                        hit_combos += 1
                    if not r.get("all_void"):
                        all_void = False
                if not slip_ready:
                    continue  # 组合内某腿未开奖 → 整票跳过

                slip_hit = hit_combos > 0
                slip_return = _round2(slip_return)
                slip_pnl = _round2(slip_return - total_stake)
                o["settled_at"] = _now_bj()
                o["sp_product"] = round(sp_product, 6)
                o["settlement_rate"] = BEIDAN_RETURN_RATE
                o["hit"] = slip_hit
                o["push"] = all_void
                o["all_void"] = all_void
                o["return_amount"] = slip_return
                o["profit"] = slip_pnl
                settled_orders.append(o)
                total_return += slip_return
                summary["settled"] += 1
                summary["slips_total"] += 1
                if slip_hit:
                    summary["hit"] += 1
                    summary["slips_any_hit"] += 1
                else:
                    summary["miss"] += 1
                summary["pnl"] = _round2(summary["pnl"] + slip_pnl)

            if total_return:
                role.deposit(total_return)
            role.save()

            summary["pnl"] = _round2(summary["pnl"])
            if reflect and settled_orders:
                try:
                    self._reflect_settled(role, settled_orders, day_date)
                except Exception as e:
                    print(f"  ⚠️ 因子反思失败（不影响结算）: {e}")
            session.settlement(summary)
            return summary
        finally:
            self._end_session(session)

    def _reflect_settled(self, role: Role, settled_orders: list[dict],
                         day_date: Optional[str]) -> None:
        """北单串关反思：把 8 串 1 子单按「每场比赛」去重成一个样本，
        显式标出「单选/全包 类型 + 实际中奖方向 actual + 开奖SP」，
        再走 agent.py node_reflect/run_reflect（不修改 agent.py）。

        修复点：
        1. 去重：leg_samples 由"每注每腿"改为"每场一条"，杜绝同一场比赛被
           combo 重复刷屏，使「高波动全包候选」能真正覆盖多场高 SP，而非全被
           单场最高 SP 的分身占满。
        2. 露真实结果：每条样本带 actual(实际方向) / sp_value(开奖SP) /
           role(单选/全包)，供反思与「全包低SP拖累」判断使用。
        """
        if not settled_orders:
            return

        # 跨 slip 判断每场是单选还是全包：某场只要在任意一张票中被单选，
        # 就以「单选」视角归因（单选看 pick 是否命中）；否则视为全包。
        # （同一次 settle 可能同时结算多张票，不同票里同一场可能是单选/全包，
        #   必须按 slip_id 分开建 role 映射，不能只看第一张票。）
        slip_role: dict[str, dict[str, str]] = {}
        for o in settled_orders:
            sid = o.get("slip_id") or o.get("id")
            if sid in slip_role:
                continue
            m: dict[str, str] = {}
            for tl in (o.get("ticket_legs") or []):
                lid = tl.get("lota_id")
                if not lid:
                    continue
                picks = tl.get("picks") or ([tl["pick"]] if tl.get("pick") else [])
                m[lid] = "单选" if len(picks) == 1 else "全包"
            slip_role[sid] = m

        all_lids: set[str] = set()
        single_lids: set[str] = set()
        leg_single_pick: dict[str, str] = {}
        for o in settled_orders:
            sid = o.get("slip_id") or o.get("id")
            for tl in (o.get("ticket_legs") or []):
                lid = tl.get("lota_id")
                if not lid:
                    continue
                all_lids.add(lid)
                if slip_role.get(sid, {}).get(lid) == "单选":
                    single_lids.add(lid)
                    picks = tl.get("picks") or ([tl["pick"]] if tl.get("pick") else [])
                    leg_single_pick.setdefault(lid, picks[0] if picks else "")
        leg_role: dict[str, str] = {
            lid: ("单选" if lid in single_lids else "全包") for lid in all_lids
        }

        seen: dict[str, dict] = {}
        for o in settled_orders:
            for leg in o.get("legs", []):
                lid = leg.get("lota_id")
                if not lid or lid in seen:
                    continue
                sp = float(leg.get("sp", 0.0) or 0.0)
                actual = leg.get("actual") or ""
                pick = leg.get("pick", "")
                role_label = leg_role.get(lid, "全包")
                if role_label == "单选":
                    # 单选取该场在票中的单选方向（可能与 leg 的 pick 一致）
                    pick = leg_single_pick.get(lid, pick)
                if leg.get("push"):
                    profit = 0.0
                    hit = None
                elif role_label == "单选":
                    hit = (pick == actual)
                    profit = round(2.0 * sp * BEIDAN_RETURN_RATE - 2.0, 2) if hit else -2.0
                else:
                    # 全包腿天然覆盖，命中视为 True；利润用开奖 SP 衡量该腿价值
                    hit = True
                    profit = round(2.0 * sp * BEIDAN_RETURN_RATE - 2.0, 2)
                seen[lid] = {
                    "id": f"leg_{lid}",
                    "lota_id": lid,
                    "bet_type": "北单腿",
                    "pick": pick,
                    "actual": actual,
                    "role": role_label,
                    "sp_value": sp,
                    "odds": sp,
                    "bet_size": 2.0,
                    "profit": profit,
                    "hit": hit,
                    "reason": (f"{leg.get('home_name', '')} vs "
                               f"{leg.get('away_name', '')} 让{leg.get('goal_line')} "
                               f"| 类型:{role_label} | 实际:{actual or '?'} | "
                               f"开奖SP:{sp:.2f}"),
                }
        leg_samples = list(seen.values())
        if not leg_samples:
            return

        # 高波动全包候选：按开奖 SP 降序，取 TOP-N 个【不同场次】做候选
        leg_samples.sort(key=lambda x: -float(x.get("odds") or 0.0))
        for i, s in enumerate(leg_samples):
            if i < self.REFLECT_HIGH_SP_TOP and s.get("role") == "全包":
                s["reason"] = (f"【高波动全包候选 SP={s.get('odds', 0):.2f}】 "
                               + s.get("reason", ""))

        # 全包腿开奖SP分布（低SP占比 -> 拖累维度），由 run_reflect 注入反思 prompt
        covers = [s for s in leg_samples if s.get("role") == "全包"]
        if covers:
            sps = sorted(float(s.get("odds") or 0.0) for s in covers)
            low_n = sum(1 for x in sps if x < 3.0)
            leg_samples[0]["cover_sp_summary"] = (
                f"本单全包腿({len(sps)}场)开奖SP: {', '.join(f'{x:.2f}' for x in sps)}；"
                f"其中 {low_n}/{len(sps)} 场 <3.0（低值）。"
            )

        from .agent import _rt, node_reflect
        rt = _rt({"user": self.user})
        if rt.provider is None:
            from .providers.deepseek import DeepSeekProvider
            self.set_provider(DeepSeekProvider())
        rt.role = role
        rt.last_settled_orders = leg_samples
        node_reflect({
            "user": self.user,
            "day_date": day_date or "",
            "reflect_extra": {
                "extra_by_sp": True,
                "extra_max": self.REFLECT_HIGH_SP_TOP,
                "parlay_emphasis": True,
            },
        })

    # ═══════════════════════════════════════════
    # 状态 / 回测
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
            "pnl": round(sum(float(o.get("profit", 0)) for o in settled), 2),
        }

    def reset(self, capital: float = None) -> dict:
        role = self._ensure_role()
        role.capital = float(capital) if capital else float(self.START_CAPITAL)
        role.initial_capital = role.capital
        role.orders = []
        role.save()
        return {"capital": role.capital, "orders": len(role.orders)}

    def compact(self) -> dict:
        """把 role.orders 压缩为 slip 级（一张票一条，8 条腿存一次），并落盘。"""
        role = self._ensure_role()
        before = len(role.orders)
        role.orders = compact_slip_orders(role.orders)
        role.save()
        return {"before": before, "after": len(role.orders)}

    def backtest(self, start_date: str, end_date: str,
                 max_picks: int = None, tickets: list[str] = None) -> dict:
        d = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        rows = []
        while d <= end:
            ds = d.isoformat()
            a = self.analyze(ds, max_picks=max_picks, tickets=tickets)
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
                "push": s.get("push", 0),
                "pnl": s.get("pnl", 0.0),
                "capital": self._ensure_role().capital,
            })
            d += timedelta(days=1)
        totals = {
            "placed": sum(r["placed"] for r in rows),
            "settled": sum(r["settled"] for r in rows),
            "hit": sum(r["hit"] for r in rows),
            "miss": sum(r["miss"] for r in rows),
            "push": sum(r["push"] for r in rows),
            "pnl": round(sum(r["pnl"] for r in rows), 2),
            "capital": rows[-1]["capital"] if rows else 0,
            "empty_days": sum(1 for r in rows if r["placed"] == 0),
        }
        return {"days": rows, "totals": totals}


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

def compact_slip_orders(orders: list[dict]) -> list[dict]:
    """把旧版「每注一条 order」压缩成「每票一条 slip order」。

    旧版（展开组合）会把一张 8串1 拆成 3^5=243 条 order，每条都重复存储
    ticket_legs/legs 的完整腿信息，导致 role.json 无限膨胀到几十 MB。
    此函数按 slip_id 归并为一张票：k 条腿（含 picks 列表）只存一次，
    组合不再落盘（settle 现算），并从各注回填腿级结算结果/票级汇总。
    """
    from collections import OrderedDict

    groups: dict[str, list[dict]] = OrderedDict()
    for o in orders or []:
        sid = o.get("slip_id") or o.get("id") or ""
        if sid:
            groups.setdefault(sid, []).append(o)
        else:
            groups.setdefault(o.get("id") or f"_k{len(groups)}", []).append(o)

    out: list[dict] = []
    for sid, grp in groups.items():
        first = grp[0]
        ticket_legs = list(first.get("ticket_legs") or first.get("legs") or [])
        unit = float(first.get("bet_size") or first.get("unit_stake") or 2.0)
        combos_count = int(first.get("combos_count") or len(grp))

        # 回填腿级结果：从任意已结算注的 legs 取 actual/sp/push/hit 按 lota_id 归并
        by_lid: dict[str, dict] = {}
        for o in grp:
            for l in (o.get("legs") or []):
                lid = l.get("lota_id") or ""
                if lid and lid not in by_lid:
                    by_lid[lid] = dict(l)
        for l in ticket_legs:
            lid = l.get("lota_id") or ""
            src = by_lid.get(lid)
            if not src:
                continue
            for k in ("actual", "sp", "push", "hit"):
                if k in src and src[k] is not None:
                    l[k] = src[k]

        settled_at = ""
        for o in grp:
            sa = o.get("settled_at") or ""
            if sa and sa > settled_at:
                settled_at = sa
        hit = any(bool(o.get("hit")) for o in grp)
        all_void = bool(grp) and all(bool(o.get("all_void")) for o in grp)
        return_amount = _round2(sum(float(o.get("return_amount") or 0.0) for o in grp))
        profit = _round2(sum(float(o.get("profit") or 0.0) for o in grp))
        sp_product = 1.0
        for l in ticket_legs:
            sp_product *= float(l.get("sp") or 1.0)

        out.append({
            "id": first.get("id") or _uid("ord_"),
            "slip_id": sid,
            "slip_type": first.get("slip_type") or first.get("ticket_type") or "",
            "slip_index": 1,
            "combos_count": combos_count,
            "unit_stake": unit,
            "total_stake": _round2(float(first.get("total_stake") or unit * combos_count)),
            "ticket_legs": ticket_legs,
            "predict_id": first.get("predict_id", ""),
            "lota_id": (ticket_legs[0]["lota_id"] if ticket_legs
                        else (first.get("lota_id") or "")),
            "bet_type": first.get("bet_type", ""),
            "ticket_type": first.get("ticket_type") or first.get("slip_type") or "",
            "pick": first.get("pick", ""),
            "odds": first.get("odds", 0.0),
            "bet_size": first.get("bet_size", unit),
            "legs": ticket_legs,
            "created_at": first.get("created_at", ""),
            "settled_at": settled_at or None,
            "hit": hit,
            "all_void": all_void,
            "return_amount": return_amount,
            "profit": profit,
            "settlement_rate": first.get("settlement_rate"),
            "sp_product": round(sp_product, 6),
        })
    return out


def _fmt_order(o: dict) -> str:
    legs = o.get("legs", [])
    def _leg_pick(l: dict) -> str:
        picks = l.get("picks") or ([l.get("pick")] if l.get("pick") else [])
        return "/".join(picks)

    def _leg_hc(l: dict) -> str:
        v = l.get("goal_line")
        return str(v) if isinstance(v, (int, float)) else ""

    leg_txt = " + ".join(
        f"{l.get('home_name','?')}vs{l.get('away_name','?')} {_leg_pick(l)}"
        f"({_leg_hc(l)})"
        for l in legs
    )
    slip = o.get("slip_type", "")
    tag = f"[{slip} {o.get('combos_count','')}注]" if slip else f"[{o.get('ticket_type','串关')}]"
    return f"{tag} {o.get('ticket_type','北单串关')} 买 {leg_txt} | 总赔率 {o.get('odds',0):.2f} 投注 {o.get('total_stake', o.get('bet_size',0)):.2f}"


def main(argv: list[str] = None) -> int:
    p = argparse.ArgumentParser(prog="beidan_parlay_dog", description="北单串关狗")
    p.add_argument("action", choices=["analyze", "settle", "pending", "status", "reset", "compact", "backtest"])
    p.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD（足球日起始日，默认当天）")
    p.add_argument("end", nargs="?", default=None, help="backtest 结束日 YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="只预览不落单")
    p.add_argument("--llm", action="store_true", help="用 LLM 分析选腿（默认规则版）")
    p.add_argument("--tickets", default=None, help="逗号分隔，如 6串1,7串1")
    p.add_argument("--picks", type=int, default=None,
                   help="统一模式每腿最多选项数 1/2/3（不传则用默认 8串1:5全包+3单选）")
    p.add_argument("--stake-pct", type=float, default=None,
                   help="北单每注固定 2 元，此参数仅兼容保留，不参与计算")
    p.add_argument("--user", default="北单串关狗", help="角色名（独立资金/订单）")
    args = p.parse_args(argv)

    dog = BeidanParlayDog(user=args.user)
    if args.action == "analyze":
        tickets = [t.strip() for t in args.tickets.split(",")] if args.tickets else None
        r = dog.analyze(args.day, dry_run=args.dry_run, tickets=tickets,
                        stake_pct=args.stake_pct, max_picks=args.picks,
                        use_llm=args.llm)
        tks = "+".join(r.get("tickets") or []) or "空仓"
        src_tag = "🧠LLM" if r.get("llm_used") else "📐规则"
        print(f"📅 {r['date']} 北单场次 {r['matches_count']} | 候选腿 {r['legs_selected']} "
              f"| {src_tag} | 每腿≤{r['max_picks']}选 | 票型 {tks}")
        for o in r["orders"]:
            print("  " + _fmt_order(o))
        if r["skipped"]:
            print("  跳过:", "; ".join(r["skipped"]))
        total_bet = sum(float(o["bet_size"]) for o in r["orders"])
        print(f"{'🔍 dry-run 预览' if r['dry_run'] else '✅ 已下单'}: "
              f"{r['placed']} 注 | 总投注 {total_bet:.0f} 元")
    elif args.action == "settle":
        r = dog.settle(args.day)
        print(f"📊 结算: {r['settled']} 张子单 命中{r['hit']} 未中{r['miss']} "
              f"退款{r['push']} PnL {r['pnl']:+.2f}")
    elif args.action == "pending":
        for o in dog.pending():
            print("  ⏳ " + _fmt_order(o))
    elif args.action == "status":
        s = dog.status()
        print(f"💰 资金 {s['capital']:.2f} | 订单 {s['total_orders']} "
              f"(已结{s['settled']}/待{s['pending']}) | PnL {s['pnl']:+.2f}")
    elif args.action == "reset":
        r = dog.reset()
        print(f"♻️ 已重置: 资金 {r['capital']:.2f} | 订单 {r['orders']}")
    elif args.action == "compact":
        r = dog.compact()
        print(f"♻️ 已压缩: {r['before']} -> {r['after']} 条 (slip 级)")
    elif args.action == "backtest":
        if not args.day or not args.end:
            print("用法: python -m src.beidan_parlay_dog backtest <start> <end> [--picks 2] [--tickets 6串1]")
            return 1
        tickets = [t.strip() for t in args.tickets.split(",")] if args.tickets else None
        r = dog.backtest(args.day, args.end, max_picks=args.picks, tickets=tickets)
        print("日期 | 北单 | 腿 | 票型 | 子单 | 结算(中/挂/退) | 当日PnL | 资金")
        for row in r["days"]:
            tks = "+".join(row["tickets"]) or "空仓"
            print(f"{row['date']} | {row['matches']:>2} | {row['legs']} | {tks:<6} | "
                  f"{row['placed']:>3} | {row['settled']}({row['hit']}/{row['miss']}/{row['push']}) | "
                  f"{row['pnl']:+.2f} | {row['capital']:.2f}")
        t = r["totals"]
        print(f"汇总: 子单{t['placed']} 结算{t['settled']} 中{t['hit']} 挂{t['miss']} "
              f"退{t['push']} 空仓日{t['empty_days']} | PnL {t['pnl']:+.2f} | 资金 {t['capital']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
