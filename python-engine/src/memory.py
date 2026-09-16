"""
DSFootball Python CLI — Agent 记忆模块

三种记忆，从已有订单/预测数据初始化，持久化 JSON：
  1. OrderMemory  — 最近订单 + 按类型统计 + 连胜/连败
  2. LossMemory   — 大额亏损追踪 + 模式统计
  3. FactorMemory — 因子表现（伴随 Factor 使用逐步积累）

AgentMemory 统一入口，按 config 选择性注入到 prompt。
"""

import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

from .factor_select import (
    factor_profile,
    FACTOR_SAMPLE_WINDOW,
    FACTOR_SMALL_SAMPLE,
    FACTOR_MIN_ACTIONABLE,
    FACTOR_MAX_MAIN,
    FACTOR_MAX_MAIN_POS,
    FACTOR_MAX_MAIN_NEG,
    FACTOR_NOISE_W_RETURN,
)


DATA_ROOT = Path(__file__).parent.parent / "data"
# MEMORY_DIR 改为按角色隔离: roles/{name}/memory/
# 见 AgentMemory.__init__


def _now() -> str:
    return datetime.now().isoformat()


def _read_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════
# OrderMemory
# ═══════════════════════════════════════════════

class OrderMemory:
    """订单记忆 — 最近订单 + 摘要统计 + 连胜/连败"""

    def __init__(self, base_dir: Path = None):
        if base_dir is None:
            base_dir = DATA_ROOT / "agent_memory"  # 兼容旧代码
        base_dir.mkdir(parents=True, exist_ok=True)
        self.path = base_dir / "order_memory.json"
        self.recent_orders: list[dict] = []
        self.win_streak: int = 0
        self.lose_streak: int = 0
        self.stats: dict = {}          # {by_type: {...}, overall: {...}}
        self.total_pnl: float = 0.0
        self._loaded = False

    def refresh(self) -> None:
        """从订单数据重新初始化记忆"""
        orders = self._load_all_orders()
        if not orders:
            return

        # 按时间排序
        orders.sort(key=lambda o: o.get("created_at", ""))

        # 最近订单
        self.recent_orders = []
        for o in orders:
            self.recent_orders.append({
                "lota_id": o.get("lota_id", ""),
                "bet_type": o.get("bet_type", ""),
                "pick": o.get("pick", ""),
                "odds": o.get("odds", 0),
                "handicap": o.get("handicap"),
                "bet_size": o.get("bet_size", 100),
                "hit": o.get("hit"),
                "profit": o.get("profit", 0),
                "created_at": o.get("created_at", ""),
            })

        # 统计
        by_type = defaultdict(lambda: {"total": 0, "hit": 0, "miss": 0, "push": 0, "profit": 0.0})
        for o in orders:
            bt = o.get("bet_type", "其他")
            by_type[bt]["total"] += 1
            h = o.get("hit")
            if h is True:      by_type[bt]["hit"] += 1
            elif h is False:   by_type[bt]["miss"] += 1
            else:              by_type[bt]["push"] += 1
            by_type[bt]["profit"] += o.get("profit", 0)

        self.stats = {
            bt: {
                "total": s["total"],
                "hit": s["hit"],
                "miss": s["miss"],
                "push": s["push"],
                "profit": round(s["profit"], 2),
                "hit_rate": round(s["hit"] / (s["total"] - s["push"]) * 100, 1) if (s["total"] - s["push"]) > 0 else 0,
                "roi": round(s["profit"] / (s["total"] * 100) * 100, 1) if s["total"] > 0 else 0,
            }
            for bt, s in by_type.items()
        }

        total_profit = sum(s["profit"] for s in by_type.values())
        total_orders = sum(s["total"] for s in by_type.values())
        self.total_pnl = round(total_profit, 2)
        self.stats["overall"] = {
            "total": total_orders,
            "profit": self.total_pnl,
            "roi": round(total_profit / (total_orders * 100) * 100, 1) if total_orders > 0 else 0,
        }

        # 连胜/连败
        self.win_streak, self.lose_streak = self._calc_streaks(orders)

        self._loaded = True
        self._save()

    def _load_all_orders(self) -> list[dict]:
        orders_dir = DATA_ROOT / "orders"
        orders = []
        if orders_dir.exists():
            for fpath in sorted(orders_dir.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        orders.extend(data)
                except Exception:
                    pass
        return orders

    def _calc_streaks(self, orders: list[dict]) -> tuple[int, int]:
        win_streak = lose_streak = 0
        for o in reversed(orders):
            h = o.get("hit")
            if h is True:
                if lose_streak == 0:
                    win_streak += 1
                else:
                    break
            elif h is False:
                if win_streak == 0:
                    lose_streak += 1
                else:
                    break
            # push 不打断 streak
        return win_streak, lose_streak

    def recent(self, n: int) -> list[dict]:
        return self.recent_orders[-n:] if self.recent_orders else []

    def summary_text(self) -> str:
        if not self._loaded:
            return "(无订单记忆)"
        lines = ["📊 订单统计"]
        for bt in ["胜平负", "亚盘", "大小球"]:
            s = self.stats.get(bt)
            if not s or s["total"] == 0:
                continue
            lines.append(
                f"  {bt}: {s['total']}单 命中{s['hit_rate']}% "
                f"盈亏{s['profit']:+.0f} ROI{s['roi']:+.1f}%"
            )
        ov = self.stats.get("overall", {})
        lines.append(f"  总计: {ov.get('total',0)}单 总盈亏{self.total_pnl:+.0f} ROI{ov.get('roi',0):+.1f}%")
        return "\n".join(lines)

    def streak_text(self) -> str:
        if not self._loaded:
            return ""
        parts = []
        if self.win_streak >= 2:
            parts.append(f"🔥 连胜 {self.win_streak} 场")
        if self.lose_streak >= 2:
            parts.append(f"🔻 连败 {self.lose_streak} 场")
        return " | ".join(parts) if parts else ""

    def recent_text(self, n: int = 20) -> str:
        recent = self.recent(n)
        if not recent:
            return "(无最近订单)"
        lines = ["📋 最近订单:"]
        for o in recent:
            h = "✅" if o["hit"] is True else ("❌" if o["hit"] is False else "➖")
            bt = o["bet_type"]
            lines.append(
                f"  {h} {bt} {o['pick']} @{o['odds']:.2f} "
                f"bet {o['bet_size']:.0f} → {o['profit']:+.0f}"
            )
        return "\n".join(lines)

    def _save(self) -> None:
        _write_json(self.path, {
            "updated_at": _now(),
            "recent_orders": self.recent_orders[-50:],
            "win_streak": self.win_streak,
            "lose_streak": self.lose_streak,
            "stats": self.stats,
            "total_pnl": self.total_pnl,
        })

    def load(self) -> None:
        data = _read_json(self.path)
        if data:
            self.recent_orders = data.get("recent_orders", [])
            self.win_streak = data.get("win_streak", 0)
            self.lose_streak = data.get("lose_streak", 0)
            self.stats = data.get("stats", {})
            self.total_pnl = data.get("total_pnl", 0.0)
            self._loaded = True


# ═══════════════════════════════════════════════
# LossMemory
# ═══════════════════════════════════════════════

class LossMemory:
    """损失记忆 — 大额亏损 + 模式标签"""

    def __init__(self, base_dir: Path = None):
        if base_dir is None:
            base_dir = DATA_ROOT / "agent_memory"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.path = base_dir / "loss_memory.json"
        self.notable_losses: list[dict] = []
        self.patterns: dict[str, int] = {}    # {tag: count}
        self.max_single_loss: float = 0.0
        self._loaded = False

    def refresh(self) -> None:
        orders = self._load_all_orders()
        if not orders:
            return

        self.notable_losses = []
        self.max_single_loss = 0.0
        patterns: dict[str, int] = defaultdict(int)

        for o in orders:
            profit = o.get("profit", 0)
            if profit < 0:
                # 标记亏损模式
                bt = o.get("bet_type", "")
                pick = o.get("pick", "")
                handicap = o.get("handicap")
                tag = f"{bt}:{pick}"
                patterns[tag] += 1

                if profit <= -100:  # 全额亏损
                    loss_entry = {
                        "lota_id": o.get("lota_id", ""),
                        "bet_type": bt,
                        "pick": pick,
                        "odds": o.get("odds", 0),
                        "handicap": handicap,
                        "bet_size": o.get("bet_size", 100),
                        "profit": profit,
                        "created_at": o.get("created_at", ""),
                    }
                    self.notable_losses.append(loss_entry)

                if profit < self.max_single_loss:
                    self.max_single_loss = profit

        # 排序：损失最大的在前
        self.notable_losses.sort(key=lambda x: x["profit"])
        self.patterns = dict(sorted(patterns.items(), key=lambda x: -x[1]))
        self._loaded = True
        self._save()

    def _load_all_orders(self) -> list[dict]:
        orders_dir = DATA_ROOT / "orders"
        orders = []
        if orders_dir.exists():
            for fpath in sorted(orders_dir.glob("*.json")):
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        orders.extend(data)
                except Exception:
                    pass
        return orders

    def patterns_text(self) -> str:
        if not self._loaded:
            return ""
        if not self.patterns:
            return "📉 无亏损记录"
        lines = ["📉 亏损模式:"]
        for tag, count in list(self.patterns.items())[:5]:
            lines.append(f"  {tag}: {count}次")
        if self.max_single_loss < 0:
            lines.append(f"  最大单笔亏损: {self.max_single_loss:+.0f}")
        return "\n".join(lines)

    def _save(self) -> None:
        _write_json(self.path, {
            "updated_at": _now(),
            "notable_losses": self.notable_losses[:30],
            "patterns": self.patterns,
            "max_single_loss": self.max_single_loss,
        })

    def load(self) -> None:
        data = _read_json(self.path)
        if data:
            self.notable_losses = data.get("notable_losses", [])
            self.patterns = data.get("patterns", {})
            self.max_single_loss = data.get("max_single_loss", 0.0)
            self._loaded = True


# ═══════════════════════════════════════════════
# FactorMemory
# ═══════════════════════════════════════════════

class FactorMemory:
    """因子记忆 — 各 Factor 的表现统计（按 factor_id / slug）"""

    def __init__(self, base_dir: Path = None):
        if base_dir is None:
            base_dir = DATA_ROOT / "agent_memory"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.path = base_dir / "factor_memory.json"
        self.factor_perf: dict[str, dict] = {}  # {factor_id: {total,hit,miss,profit}}
        self._loaded = False

    @staticmethod
    def fac_id_for(name: str) -> str:
        """因子名 → fac 定义 id（与 agent.py / store.py 的命名规则一致）。"""
        return f"fac_{name.lower().replace(' ','_')[:40]}"

    def _load_slugs(self, fac_id: str) -> list[str]:
        """从 data/factors/fac_*.json 读取 slugs，文件缺失返回空。"""
        p = DATA_ROOT / "factors" / f"{fac_id}.json"
        if not p.exists():
            return []
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d.get("slugs", [])
        except Exception:
            return []

    def _backfill_fac_link(self, factor_id: str) -> None:
        """为条目补齐 fac_id 与 slugs（名称 join fac 定义文件）。"""
        p = self.factor_perf.get(factor_id)
        if not p:
            return
        p.setdefault("fac_id", self.fac_id_for(factor_id))
        if not p.get("slugs"):
            p["slugs"] = self._load_slugs(p["fac_id"])

    def _consolidate_candidate(self, factor_id: str, desc: str) -> tuple:
        """LLM 判重（宽松：shortlist 到 15 个、全 desc、严格 schema）+ 确定性兜底。

        兜底：LLM 判 create 或调用失败时，若候选与某 retired 因子名称相似度≥0.8
        且 desc 语义一致（bigram Jaccard / 序列相似度 ≥0.25）→ 强制 suppress。
        返回 ('create'|'merge'|'suppress', target_id)。"""
        import difflib, json as _json
        from pathlib import Path as _P
        names = list(self.factor_perf.keys())
        if not names:
            return ("create", None)
        scored = sorted(((difflib.SequenceMatcher(None, factor_id, n).ratio(), n) for n in names), reverse=True)
        short = [n for s, n in scored if s >= 0.45][:15]
        if not short:
            return ("create", None)

        def _det_suppress():
            """确定性兜底：名称≥0.8 且 desc 相似≥0.25 的 retired 因子。"""
            best, best_score = None, 0.0
            for n in names:
                v = self.factor_perf[n]
                if v.get("status") != "retired":
                    continue
                nr = difflib.SequenceMatcher(None, factor_id, n).ratio()
                if nr < 0.8:
                    continue
                vd = v.get("desc", "") or ""
                bj = _desc_bigram_jaccard(desc, vd)
                sr = difflib.SequenceMatcher(None, desc, vd).ratio()
                dv = max(bj, sr)
                if dv < 0.25:
                    continue
                score = 0.6 * nr + 0.4 * dv
                if score > best_score:
                    best, best_score = n, score
            return best

        def _avg_return(e):
            denom = float(e.get("total", 0) or 0) - float(e.get("push", 0) or 0)
            total_return = float(
                e.get("total_return")
                if "total_return" in e
                else e.get("profit", 0) or 0
            )
            return total_return / denom if denom > 0 else 0.0

        verdict = None
        try:
            from src.providers.deepseek import DeepSeekProvider
            provider = DeepSeekProvider()
            lib_lines = "\n".join(
                f"{i2}. {n} [状态:{self.factor_perf[n].get('status','active')}] | "
                f"{self.factor_perf[n].get('desc','')[:200]} "
                f"(样本{self.factor_perf[n].get('total',0)} 单注{_avg_return(self.factor_perf[n]):+.2f})"
                for i2, n in enumerate(short, 1)
            )
            system = ("你是足球因子库管理员。判断候选因子是否与现有因子重复。\n"
                      "规则：\n"
                      "1. 同模式不同表述（名称或描述高度一致）→ merge，target 填最匹配的现有因子名；\n"
                      "2. 方向相反（上盘vs下盘/让球方vs受让方/阻上vs诱上/追强vs防冷）→ 绝不合并，create；\n"
                      "3. 与 retired 因子高度一致（名称与描述均匹配）→ suppress；\n"
                      "4. 与现有因子样本都充足且盈亏方向相反 → create（经验上不同模式）；\n"
                      "5. 全新模式 → create。\n"
                      "使用语义理解判断，不要只看字面。只输出严格 JSON，不要多余文字。")
            user = (f"候选因子: {factor_id} | {desc[:200]}\n\n现有因子(共{len(short)}个):\n{lib_lines}\n\n"
                    '输出严格 JSON: {"action":"merge|create|suppress","target":"现有因子名或null","reason":"一句话"}')
            # 辅助判断（因子去重/合并），走 fast 模型 + 关闭 thinking
            raw = provider.call_fast(system, [{"role": "user", "content": user}], temperature=0.0)
            raw = __import__("re").sub(r"\[thinking\].*?\[/thinking\]\s*", "", raw, flags=__import__("re").S).strip()
            start, end = raw.find("{"), raw.rfind("}")
            verdict = _json.loads(raw[start:end + 1]) if start != -1 and end != -1 else None
        except Exception:
            verdict = None

        action = verdict.get("action", "create") if verdict else "create"
        target = verdict.get("target") if verdict else None
        reason = verdict.get("reason", "") if verdict else "LLM调用失败"
        # 确定性兜底：判 create 或失败时，若命中 retired 近亲 → suppress
        if action == "create":
            det = _det_suppress()
            if det:
                action, target, reason = "suppress", det, f"确定性兜底(desc一致+retired): {reason}"
        if action == "suppress" and target in self.factor_perf:
            _log_dedup(factor_id, action, target, reason)
            return ("suppress", target)
        if action == "merge" and target in self.factor_perf:
            _log_dedup(factor_id, action, target, reason)
            return ("merge", target)
        _log_dedup(factor_id, "create", None, reason)
        return ("create", None)

    def record(self, factor_id: str, hit: bool | None, profit: float,
               desc: str = "", date: str = "", lota_id: str = "",
               bet_size: float = 0, factor_type: str = "directional",
               sp: float | None = None, low_sample: bool = False,
               unit_cost: float | None = None,
               path: str | None = None,
               unit_cost_default: float | None = None,
               vol_base: float | None = None) -> None:
        """记一条因子样本。

        unit_cost：该样本的**注数基数**（单选腿 1、北单全包腿 3；缺省 None=按方向口径 1）。
        不同基数的样本混在同一根 return_ratio 上不可比（全包腿输 = −3、单选腿输 = −1），
        所以样本自带基数，供 factor_select 反推 SP 与分路径统计。

        path / unit_cost_default：**按狗隔离的显式标注**（目前只有北单串关写 `path="beidan"`）。
        只有带该标注的因子才会启用「口径不一致就不算」等北单专用统计语义；
        其它狗（单关狗/竞彩狗等）不传 → 统计口径与历史行为完全一致。
        """
        if not self._loaded:  # 防止覆盖磁盘上的因子库
            self.load()
        return_ratio = profit / bet_size if bet_size > 0 else 0.0
        # 半赢/半输归一化：结算层用 hit=None 同时表示走水与半盘（quarter-ball），
        # 这里按 profit 区分——profit≠0 的 None 是赢半/输半，不是走水。
        if hit is None and profit > 0:
            eff_hit = 0.5       # 赢半：按 0.5 命中统计
        elif hit is None and profit < 0:
            eff_hit = -0.5      # 输半：按 0.5 未中统计
        else:
            eff_hit = hit       # True / False / None(真走水)
        # 阶段3 职责归位：record 只记不判（内嵌 LLM 判重已交给独立归纳步骤 factor_induction）
        if factor_id not in self.factor_perf:
            self.factor_perf[factor_id] = {
                "total": 0, "hit": 0, "miss": 0, "push": 0,
                "profit": 0.0, "total_return": 0.0,
                "status": "testing", "desc": desc,
                "type": factor_type,
                "low_sample": low_sample,
                "first_seen": date, "last_seen": date,
                "history": [], "aliases": [],
                "fac_id": self.fac_id_for(factor_id),
            }
        p = self.factor_perf[factor_id]
        p.setdefault("type", factor_type)
        if path:
            p.setdefault("path", str(path))
        if unit_cost_default is not None:
            p.setdefault("unit_cost_default", float(unit_cost_default))
        if low_sample:
            p["low_sample"] = True
        self._backfill_fac_link(factor_id)
        p["total"] += 1
        if eff_hit is True:       p["hit"] += 1
        elif eff_hit is False:    p["miss"] += 1
        elif eff_hit == 0.5:      p["hit"] += 0.5
        elif eff_hit == -0.5:     p["miss"] += 0.5
        else:                     p["push"] += 1
        p["profit"] += profit
        p["total_return"] = p.get("total_return", 0.0) + return_ratio
        if desc:
            p["desc"] = desc
        if date:
            if not p.get("first_seen"):
                p["first_seen"] = date
            p["last_seen"] = date
            hist_entry = {
                "date": date, "hit": eff_hit,
                "profit": profit, "return_ratio": return_ratio,
                "lota_id": lota_id,
            }
            if sp is not None:
                hist_entry["sp"] = float(sp)
            if unit_cost is not None:
                hist_entry["unit_cost"] = float(unit_cost)
            if vol_base is not None:
                # 波动路径的粗筛对照：当日「高波动场次占比」基线
                hist_entry["vol_base"] = float(vol_base)
            p.setdefault("history", []).append(hist_entry)
        self._save()

    def record_screen(self, factor_id: str, n: int, high: int,
                      base_sum: float) -> None:
        """波动路径专用（粗筛）：把"这个因子当天标记过的场次"里出高波动的比例累加。

        与 record() 的区别：计数对象是**当天所有被该因子标记的场次**（不管有没有下注），
        所以能避免"只统计自己下过的腿"造成的选择偏差（下过的腿本来就是精挑的）。
        其它狗不调用 → 不产生该字段。
        """
        if not self._loaded:
            self.load()
        if n <= 0:
            return
        p = self.factor_perf.get(factor_id)
        if p is None:
            p = {"total": 0, "hit": 0, "miss": 0, "push": 0, "profit": 0.0,
                 "status": "testing", "desc": "", "type": "volatility",
                 "history": [], "aliases": [],
                 "fac_id": self.fac_id_for(factor_id)}
            self.factor_perf[factor_id] = p
        sc = p.setdefault("screen", {"n": 0, "high": 0, "base_sum": 0.0})
        sc["n"] = int(sc.get("n", 0)) + int(n)
        sc["high"] = int(sc.get("high", 0)) + int(high)
        sc["base_sum"] = float(sc.get("base_sum", 0.0)) + float(base_sum)
        self._save()

    def set_status(self, factor_id: str, status: str) -> None:
        """LLM 可调用: 标记因子状态 active / retired / testing"""
        if factor_id not in self.factor_perf:
            self.factor_perf[factor_id] = {"total": 0, "hit": 0, "miss": 0, "push": 0, "profit": 0.0}
        self.factor_perf[factor_id]["status"] = status
        self._save()

    def get_performance(self, factor_id: str) -> dict:
        return self.factor_perf.get(factor_id, {"total": 0, "hit": 0, "miss": 0, "push": 0, "profit": 0.0, "status": "active"})

    # ── 因子选择（注入 prompt 前）：样本窗 + 衰减加权 + 自适应休眠 ──

    def selected_active(self, as_of=None, max_main=None, max_aux=None,
                        max_vol=None, include_volatility: bool = False):
        """
        返回 (main, aux, volatility, dormant_count)：
          main — 窗口内 n>=2 且加权回报>0 的活跃因子（按加权回报降序，最多 12 个）
          aux  — 窗口内样本不足 / 加权回报<=0 的因子（观察区，慎用）
          volatility — 波动性因子（SP收益口径，不参与方向命中率排序）
          dormant_count — 超过 3×平均触发间隔未触发（或已被 review 标记 dormant）的因子数
        as_of: 评估基准时间（datetime）；历史回放时传模拟当日，默认用真实当前时间。
        max_main / max_aux / max_vol: 可选，覆盖各类因子进入 prompt 的条数上限；
          传入较大值等价于不截断（用于北单串关等长上下文路径）。
        include_volatility: 是否返回波动因子。单场预测的单狗默认 False；
          只有北单串关/覆盖腿选腿路径需要显式传 True。
        """
        if not self._loaded or not self.factor_perf:
            return [], [], [], 0
        main_pos, main_neg, aux, volatility, dormant_count = [], [], [], [], 0
        for fid, s in self.factor_perf.items():
            status = s.get("status", "active")
            if status == "retired":
                continue
            prof = factor_profile(s, now=as_of)
            if prof is None:
                continue
            strong_large = bool(prof.get("strong_large"))
            if status == "dormant" and not strong_large:
                dormant_count += 1
                continue
            if prof["dormant"] and not strong_large:
                dormant_count += 1
                continue
            if strong_large and status == "dormant":
                s["status"] = "active"
                status = "active"
            item = (fid, s, prof)
            n = prof["n"]
            # 方向因子用归一化评分分类（已扣除近期回报波动和样本惩罚）；
            # w_return 只用于展示，不再作为进入主区顺/反向的门槛。
            score = float(prof.get("rank_score") or prof.get("w_return") or 0)

            if prof["factor_type"] == "volatility":
                if include_volatility:
                    volatility.append(item)
                continue

            if n < 2:
                aux.append(item)
            elif score >= FACTOR_NOISE_W_RETURN:
                prof["sign"] = "pos"
                main_pos.append(item)
            elif score <= -FACTOR_NOISE_W_RETURN:
                prof["sign"] = "neg"
                main_neg.append(item)
            # else: 0 回报附近噪声 → 不展示
        main_pos.sort(key=lambda x: -x[2]["rank_score"])
        main_neg.sort(key=lambda x: x[2]["rank_score"])
        aux.sort(key=lambda x: -x[2]["w_return"] if x[2] else 0)
        volatility.sort(key=lambda x: -(x[2].get("avg_sp") or x[2].get("w_return") or 0))
        cap_main = FACTOR_MAX_MAIN if max_main is None else max_main
        cap_main_pos = FACTOR_MAX_MAIN_POS if max_main is None else max_main
        cap_main_neg = FACTOR_MAX_MAIN_NEG if max_main is None else max_main
        cap_aux = 10 if max_aux is None else max_aux
        cap_vol = 10 if max_vol is None else max_vol
        main = main_pos[:cap_main_pos] + main_neg[:cap_main_neg]
        return main[:cap_main], aux[:cap_aux], volatility[:cap_vol], dormant_count

    def perf_text(self, as_of=None) -> str:
        """分层注入：L1 负例护栏 + L2 顺向(正回报) + L3 反向(负回报/反买) + L4 观察 + 噪声/休眠计数。"""
        if not self._loaded or not self.factor_perf:
            return ""
        main, aux, volatility, dormant_count = self.selected_active(as_of)
        noise_count = 0
        for fid, s in self.factor_perf.items():
            status = s.get("status", "active")
            if status in ("retired", "dormant"):
                continue
            prof = factor_profile(s, now=as_of)
            if prof is None or prof["dormant"] or prof["n"] < 2:
                continue
            if abs(prof["w_return"]) < FACTOR_NOISE_W_RETURN:
                noise_count += 1
        def _avg_return(s0):
            denom = float(s0.get("total", 0) or 0) - float(s0.get("push", 0) or 0)
            total_return = float(
                s0.get("total_return")
                if "total_return" in s0
                else s0.get("profit", 0) or 0
            )
            return total_return / denom if denom > 0 else 0.0

        retired = sorted(
            ((fid, s0) for fid, s0 in self.factor_perf.items() if s0.get("status") == "retired"),
            key=lambda x: _avg_return(x[1]),
        )[:8]
        lines = []
        if retired:
            lines.append("🪦 已证伪模式（负例护栏，勿用）:")
            for fid, s0 in retired:
                lines.append(
                    f"  ❌ {fid} (单注{_avg_return(s0):+.2f} / 样本{s0.get('total', 0)})"
                )
        pos = [x for x in main if x[2].get("sign") == "pos"]
        neg = [x for x in main if x[2].get("sign") == "neg"]
        if pos:
            lines.append("📈 顺向因子（正回报，按自适应得分）:")
            for fid, s0, p0 in pos:
                total = s0.get("total", 0)
                hit = s0.get("hit", 0)
                miss = s0.get("miss", 0)
                push = s0.get("push", 0)
                status = s0.get("status", "active")
                decided = p0.get("decided", 0)
                tag = ""
                if status == "testing":
                    tag = " 🧪未验证"
                elif s0.get("low_sample") or (0 < decided < FACTOR_SMALL_SAMPLE):
                    tag = " ⚠️样本少"
                score = float(p0.get("rank_score") or p0.get("w_return") or 0)
                lines.append(
                    f"  {fid} [近{p0['n']}单 命中{p0['hits']:g}/{p0['n']} "
                    f"收缩命中{p0['shrunk_rate']:.0%} 归一化{score:+.2f} "
                    f"加权回报{p0['w_return']:+.2f} "
                    f"| 全样本 {total:g}单 {hit:g}胜/{miss:g}负/{push:g}走]{tag}"
                )
                desc = s0.get("desc", "")
                if desc:
                    lines.append(f"     {desc[:80]}")
        if neg:
            lines.append("🔄 反向因子（负回报，反买/规避信号）:")
            for fid, s0, p0 in neg:
                total = s0.get("total", 0)
                hit = s0.get("hit", 0)
                miss = s0.get("miss", 0)
                push = s0.get("push", 0)
                status = s0.get("status", "active")
                decided = p0.get("decided", 0)
                tag = ""
                if status == "testing":
                    tag = " 🧪未验证"
                elif s0.get("low_sample") or (0 < decided < FACTOR_SMALL_SAMPLE):
                    tag = " ⚠️样本少"
                score = float(p0.get("rank_score") or p0.get("w_return") or 0)
                lines.append(
                    f"  {fid} [近{p0['n']}单 命中{p0['hits']:g}/{p0['n']} "
                    f"收缩命中{p0['shrunk_rate']:.0%} 归一化{score:+.2f} "
                    f"加权回报{p0['w_return']:+.2f} "
                    f"| 全样本 {total:g}单 {hit:g}胜/{miss:g}负/{push:g}走]{tag}"
                )
                desc = s0.get("desc", "")
                if desc:
                    lines.append(f"     {desc[:80]}")
        if aux:
            lines.append("📉 观察（样本不足，最大仓位 5-10%，禁止重仓）:")
            for fid, s0, p0 in aux[:10]:
                wr = p0["w_return"] if p0 else 0.0
                n = p0["n"] if p0 else 0
                lines.append(f"  ⚠️ {fid}: 近{n}单 加权回报{wr:+.2f}")
        if noise_count:
            lines.append(f"  (另有 {noise_count} 个 0 回报附近噪声因子已过滤)")
        dormant_strong = []
        for fid, s0 in self.factor_perf.items():
            if s0.get("status") != "dormant":
                continue
            decided = s0.get("total", 0) - s0.get("push", 0)
            hit_rate = s0.get("hit", 0) / decided if decided > 0 else 0.0
            if decided >= 20 and hit_rate > 0.55:
                dormant_strong.append((fid, s0))
        if dormant_strong:
            dormant_strong.sort(key=lambda x: -x[1].get("total", 0))
            lines.append("💤 休眠但历史强（保留参考，不参与排序）:")
            for fid, s0 in dormant_strong:
                total = s0.get("total", 0)
                hit = s0.get("hit", 0)
                miss = s0.get("miss", 0)
                push = s0.get("push", 0)
                lines.append(
                    f"  {fid} [全样本 {total:g}单 {hit:g}胜/{miss:g}负/{push:g}走]"
                )
        if dormant_count:
            lines.append(f"  (另有 {dormant_count} 个休眠因子)")
        return "\n".join(lines)

    def factor_desc_text(self, as_of=None) -> str:
        """L2 正例完整定义：只输出自适应 main 的定义（预算内，库再大不膨胀）。"""
        main, _, _, _ = self.selected_active(as_of)
        active_names = {fid for fid, _, _ in main}
        if not active_names:
            return ""
        factors_dir = DATA_ROOT / "factors"
        if not factors_dir.exists():
            return ""
        lines = ["📐 因子定义:"]
        for fpath in sorted(factors_dir.glob("fac_*.json")):
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                fid = data.get("id", "")
                content = data.get("content", "")
                slugs = data.get("slugs", [])
                matched = False
                for an in active_names:
                    s = self.factor_perf.get(an, {})
                    expected = s.get("fac_id") or self.fac_id_for(an)
                    if fid == expected:
                        matched = True
                        break
                if not matched:
                    continue
                slug_str = f" [slugs: {', '.join(slugs[:4])}]" if slugs else ""
                desc = content[:200] if content else ""
                lines.append(f"  {fid}{slug_str}")
                if desc:
                    lines.append(f"    {desc}")
            except Exception:
                pass
        return "\n".join(lines) if len(lines) > 1 else ""

    def _save(self) -> None:
        _write_json(self.path, {
            "updated_at": _now(),
            "factor_perf": self.factor_perf,
        })

    def load(self) -> None:
        data = _read_json(self.path)
        if data:
            self.factor_perf = data.get("factor_perf", {})
            self._loaded = True


# ═══════════════════════════════════════════════
# SlugMemory — 数据段有效性追踪
# ═══════════════════════════════════════════════

class SlugMemory:
    """
    Slug 记忆 — 记录每天使用了哪些数据段(slug)及当日盈亏。

    用于回答: "哪些数据信号可靠？"
    分两步调用: analyze 时 record_day_slugs → 第二天 settle 时 record_day_pnl。
    """

    def __init__(self, base_dir: Path = None):
        if base_dir is None:
            base_dir = DATA_ROOT / "agent_memory"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.path = base_dir / "slug_memory.json"
        self.slug_stats: dict[str, dict] = {}   # {slug: {appearances, profitable_days, loss_days, flat_days}}
        self.day_slugs: dict[str, list[str]] = {}  # {date: [slug, ...]}  延迟回填 PnL
        self._loaded = False

    def record_day_slugs(self, date: str, slugs: list[str]) -> None:
        """记录某天用了哪些 slug（analyze 时调用）"""
        if not date or not slugs:
            return
        self.day_slugs[date] = list(slugs)
        self._loaded = True
        self._save()

    def record_day_pnl(self, date: str, pnl: float) -> None:
        """
        回填某天的 PnL，更新 slug 统计数据（settle 时调用）。
        某天没有 slugs 记录或 pnl 为 0 时跳过。
        """
        slugs = self.day_slugs.get(date, [])
        if not slugs:
            return
        for slug in slugs:
            if slug not in self.slug_stats:
                self.slug_stats[slug] = {
                    "appearances": 0, "profitable_days": 0,
                    "loss_days": 0, "flat_days": 0,
                }
            s = self.slug_stats[slug]
            s["appearances"] += 1
            if pnl > 0:
                s["profitable_days"] += 1
            elif pnl < 0:
                s["loss_days"] += 1
            else:
                s["flat_days"] += 1
        self._save()

    def slug_perf_text(self) -> str:
        """生成 slug 表现摘要（注入 prompt）"""
        if not self._loaded or not self.slug_stats:
            return ""
        lines = ["📡 数据段表现（盈利日/使用天）:"]
        items = sorted(
            self.slug_stats.items(),
            key=lambda x: -(x[1]["appearances"])
        )
        for slug, s in items[:8]:
            total = s["appearances"]
            profit_days = s["profitable_days"]
            lines.append(
                f"  {slug}: 盈利{profit_days}/{total}天"
            )
        return "\n".join(lines)

    def load(self) -> None:
        data = _read_json(self.path)
        if data:
            self.slug_stats = data.get("slug_stats", {})
            self.day_slugs = data.get("day_slugs", {})
            self._loaded = True

    def _save(self) -> None:
        _write_json(self.path, {
            "updated_at": _now(),
            "slug_stats": self.slug_stats,
            "day_slugs": self.day_slugs,
        })


# ═══════════════════════════════════════════════
# AgentMemory — 统一入口
# ═══════════════════════════════════════════════

class AgentMemory:
    """
    Agent 统一记忆入口。

    用法:
      mem = AgentMemory()
      mem.refresh()                          # 从订单数据初始化
      text = mem.format_for_prompt({
          "max_recent_orders": 20,
          "include_summary": True,
          "include_streaks": True,
      })
    """

    def __init__(self, role_name: str = ""):
        self.as_of = None  # 回放/模拟时注入的评估基准时间；None=真实当前时间
        # 按角色隔离记忆: roles/{name}/memory/
        if role_name:
            roles_root = Path(os.environ.get("DS_ROLES_ROOT") or DATA_ROOT / "roles")
            if os.environ.get("DS_ROLES_ROOT"):
                # 沙箱回放：role_root 已是单狗平铺目录，记忆直接在 <root>/memory/
                base_dir = roles_root / "memory"
            else:
                base_dir = roles_root / role_name / "memory"
        else:
            base_dir = DATA_ROOT / "agent_memory"  # 兼容旧代码
        self.orders = OrderMemory(base_dir)
        self.losses = LossMemory(base_dir)
        self.factors = FactorMemory(base_dir)
        self.slugs = SlugMemory(base_dir)
        self.reflections = ReflectionMemory(base_dir)
        self._role_name = role_name

    def refresh(self) -> None:
        """从已有订单重新初始化所有记忆"""
        self.orders.refresh()
        self.losses.refresh()
        self.slugs.load()
        self.reflections.load()
        # FactorMemory 不从订单自动初始化（需显式 record）

    def load(self) -> None:
        """从磁盘恢复记忆"""
        self.orders.load()
        self.losses.load()
        self.factors.load()
        self.slugs.load()
        self.reflections.load()

    def format_for_prompt(self, config: dict) -> str:
        """
        按 config 选择性拼接记忆文本。

        config 来自 SystemPrompt.memory_config:
          - max_recent_orders: int  最近订单数
          - include_summary: bool   统计摘要
          - include_streaks: bool   连胜/连败
          - include_loss_patterns: bool  亏损模式
          - include_factor_perf: bool    因子表现
        """
        blocks = []

        # 统计摘要
        if config.get("include_summary", True):
            blocks.append(self.orders.summary_text())

        # 连胜/连败
        if config.get("include_streaks", True):
            streak = self.orders.streak_text()
            if streak:
                blocks.append(streak)

        # 最近订单
        max_n = config.get("max_recent_orders", 20)
        if max_n > 0:
            blocks.append(self.orders.recent_text(max_n))

        # 亏损模式
        if config.get("include_loss_patterns", False):
            blocks.append(self.losses.patterns_text())

        # 因子表现
        if config.get("include_factor_perf", False):
            blocks.append(self.factors.perf_text(self.as_of))
            # 附加因子详细定义（从 fac_*.json 读取）
            desc_text = self.factors.factor_desc_text(self.as_of)
            if desc_text:
                blocks.append(desc_text)

        # Slug 表现
        if config.get("include_slug_perf", False):
            blocks.append(self.slugs.slug_perf_text())

        # Alpha 反思
        if config.get("include_reflections", True):
            ref_text = self.reflections.format_for_prompt(self.as_of)
            if ref_text:
                blocks.append(ref_text)

        return "\n\n".join(b for b in blocks if b)

    def record_factor(self, factor_id: str, hit: bool | None, profit: float) -> None:
        self.factors.record(factor_id, hit, profit)


# ═══════════════════════════════════════════════
# ReflectionMemory — 每日结算后的 alpha 反思
# ═══════════════════════════════════════════════

class ReflectionMemory:
    """
    反思记忆 — 每次结算后 LLM 自我反思，记录 alpha 因子发现。

    独立于 OrderMemory，专注于"学到了什么"，而非"做了什么"。
    """

    def __init__(self, base_dir: Path = None):
        if base_dir is None:
            base_dir = DATA_ROOT / "agent_memory"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.path = base_dir / "reflection_memory.json"
        self.reflections: list[dict] = []  # [{date, reflection, alpha_factors, lessons}]
        self._loaded = False

    def add_reflection(self, date: str, reflection_text: str,
                       sample_count: int = None) -> None:
        """添加一条反思。

        sample_count: 本次反思涉及的比赛场数。样本 <3 时自动在文本中
        打低样本标记，避免后续分析把它当成强先例锚定。
        """
        entry = {
            "date": date,
            "reflection": reflection_text,
            "recorded_at": _now(),
        }
        if sample_count is not None:
            entry["sample_count"] = int(sample_count)
            if int(sample_count) < 3:
                entry["reflection"] += (
                    f"\n⚠️ 低样本（仅 {int(sample_count)} 场），结论待验证，"
                    "不得作为重注/铁律依据"
                )
        self.reflections.append(entry)
        self._loaded = True
        self._save()

    def recent_reflections(self, n: int = 5) -> list[dict]:
        return self.reflections[-n:] if self.reflections else []

    def format_for_prompt(self, as_of=None) -> str:
        """注入 prompt 的反思文本。

        as_of: 评估基准时间（datetime 或 date）。传值时只保留 date <= as_of 的反思，
        避免预加载/并行挖掘场景下未来反思泄漏到历史 analyze prompt。
        """
        if not self._loaded or not self.reflections:
            return ""
        refs = self.reflections
        if as_of is not None:
            try:
                cutoff = (
                    as_of.strftime("%Y-%m-%d")
                    if hasattr(as_of, "strftime")
                    else str(as_of)[:10]
                )
            except Exception:
                cutoff = str(as_of)[:10]
            refs = [
                r for r in refs
                if (r.get("date", "") or "")[:10] <= cutoff
            ]
        refs = refs[-5:]
        if not refs:
            return ""
        lines = ["## 📝 历史反思（Alpha 因子积累）", ""]
        for r in refs:
            lines.append(f"### {r['date']}")
            sc = r.get("sample_count")
            text = r.get("reflection", "")
            # 写入时已在文本中打低样本标（见 add_reflection），这里只补警告前缀，
            # 避免同一场反思出现两行重复警告
            if sc is not None and int(sc) < 3 and "低样本" not in text:
                lines.append(
                    f"⚠️ 低样本（仅 {int(sc)} 场）：此条反思参考价值有限，"
                    "禁止作为重仓/铁律依据"
                )
            # 已打低样本标的反思自带降权说明，不再重复做自怀疑检测
            if "低样本" not in text and _reflection_self_doubting(text):
                lines.append(
                    "⚠️ 该条反思自带不确定性表述（如'样本不足/不可靠/需谨慎'），"
                    "参考时降权"
                )
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    def load(self) -> None:
        data = _read_json(self.path)
        if data:
            if isinstance(data, list):
                self.reflections = data
            else:
                self.reflections = data.get("reflections", [])
            self._loaded = True

    def _save(self) -> None:
        _write_json(self.path, {
            "updated_at": _now(),
            "reflections": self.reflections[-20:],
        })


def _desc_bigram_jaccard(a: str, b: str) -> float:
    """中文描述字符二元组 Jaccard（无词表，语言无关）。"""
    def bigrams(t):
        t = (t or "").replace(" ", "")
        return {t[k:k+2] for k in range(len(t)-1)} if len(t) >= 2 else set()
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / len(A | B) if (A | B) else 0.0


def _reflection_self_doubting(text: str) -> bool:
    """检测反思文本中是否自带不确定性/自我怀疑表述。

    低置信归纳容易被 LLM 当作强先例锚定，这里识别出这类文本，
    注入 prompt 时附加降权警告。
    """
    if not text:
        return False
    markers = (
        "样本不足", "样本仅", "仅.*场", "不可靠", "待验证", "需谨慎",
        "需进一步", "未必", "不一定", "难以", "模糊", "矛盾", "冲突",
        "不能构成", "不构成因子", "存疑", "待观察", "过拟合",
    )
    import re as _re
    for m in markers:
        if _re.search(m, text):
            return True
    return False


def _log_dedup(cand: str, action: str, target: str, reason: str) -> None:
    """判重日志（审计用），追加到 data/factor_dedup_log.jsonl。"""
    import json as _json
    from datetime import datetime as _dt
    from pathlib import Path as _P
    try:
        line = {"ts": _dt.now().isoformat(timespec="seconds"),
                "candidate": cand, "action": action, "target": target, "reason": reason}
        with open(_P(__file__).parent.parent / "data" / "factor_dedup_log.jsonl", "a", encoding="utf-8") as f:
            f.write(_json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass
