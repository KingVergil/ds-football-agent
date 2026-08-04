"""
DSFootball Python CLI — Agent 记忆模块

三种记忆，从已有订单/预测数据初始化，持久化 JSON：
  1. OrderMemory  — 最近订单 + 按类型统计 + 连胜/连败
  2. LossMemory   — 大额亏损追踪 + 模式统计
  3. FactorMemory — 因子表现（伴随 Factor 使用逐步积累）

AgentMemory 统一入口，按 config 选择性注入到 prompt。
"""

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

from .factor_select import (
    factor_profile,
    FACTOR_SAMPLE_WINDOW,
    FACTOR_SMALL_SAMPLE,
    FACTOR_MAX_MAIN,
)


DATA_ROOT = Path(__file__).parent.parent / "lota_data"
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

    def _consolidate_candidate(self, factor_id: str, desc: str) -> tuple:
        """LLM 判重：候选因子 vs 现有因子（difflib 预筛 + LLM 语义判断）。失败安全=create。"""
        import difflib
        names = list(self.factor_perf.keys())
        if not names:
            return ("create", None)
        scored = sorted(((difflib.SequenceMatcher(None, factor_id, n).ratio(), n) for n in names), reverse=True)
        short = [n for s, n in scored if s >= 0.45][:6]
        if not short:
            return ("create", None)
        try:
            from src.providers.deepseek import DeepSeekProvider
            import json as _json
            provider = DeepSeekProvider()
            lib_lines = "\n".join(
                f"{i}. {n} [状态:{self.factor_perf[n].get('status','active')}] | "
                f"{self.factor_perf[n].get('desc','')[:80]} "
                f"(样本{self.factor_perf[n].get('total',0)} 盈亏{self.factor_perf[n].get('profit',0):+.0f})"
                for i, n in enumerate(short, 1)
            )
            system = ("你是足球因子库管理员。判断候选因子是否与现有因子重复。"
                      "规则: 语义重复(同模式/同义改写)→merge并填最匹配的现有因子名; "
                      "方向相反(上盘vs下盘/让球方vs受让方/追强vs防冷)→绝不合并create; "
                      "与retired因子重复→suppress; 双方样本都充足且盈亏方向相反→create; 全新→create。"
                      "只输出JSON。")
            user = (f"候选因子: {factor_id} | {desc[:120]}\n\n现有因子:\n{lib_lines}\n\n"
                    '输出 JSON: {"action":"merge|create|suppress","target":"因子名或null","reason":"一句话"}')
            raw = provider.call(system, [{"role": "user", "content": user}], temperature=0.0)
            start, end = raw.find("{"), raw.rfind("}")
            verdict = _json.loads(raw[start:end + 1]) if start != -1 and end != -1 else {"action": "create"}
            action = verdict.get("action", "create")
            target = verdict.get("target")
            if action == "merge" and target in self.factor_perf:
                return ("merge", target)
            if action == "suppress" and target in self.factor_perf:
                return ("suppress", target)
            return ("create", None)
        except Exception:
            return ("create", None)

    def record(self, factor_id: str, hit: bool | None, profit: float,
               desc: str = "", date: str = "", lota_id: str = "",
               bet_size: float = 0) -> None:
        if not self._loaded:  # 防止覆盖磁盘上的因子库
            self.load()
        return_ratio = profit / bet_size if bet_size > 0 else 0.0
        if factor_id not in self.factor_perf:
            action, target = self._consolidate_candidate(factor_id, desc)
            if action == "suppress":
                return  # 命中已退役因子，不新建、不复活
            if action == "merge" and target and target in self.factor_perf:
                t = self.factor_perf[target]
                t.setdefault("aliases", [])
                if factor_id not in t["aliases"]:
                    t["aliases"].append(factor_id)
                if t.get("status") == "dormant":
                    t["status"] = "active"
                factor_id = target  # 归因到现有因子
            else:
                self.factor_perf[factor_id] = {
                    "total": 0, "hit": 0, "miss": 0, "push": 0,
                    "profit": 0.0, "total_return": 0.0,
                    "status": "active", "desc": desc,
                    "first_seen": date, "last_seen": date,
                    "history": [], "aliases": [],
                }
        p = self.factor_perf[factor_id]
        p["total"] += 1
        if hit is True:       p["hit"] += 1
        elif hit is False:    p["miss"] += 1
        else:                 p["push"] += 1
        p["profit"] += profit
        p["total_return"] = p.get("total_return", 0.0) + return_ratio
        if desc:
            p["desc"] = desc
        if date:
            if not p.get("first_seen"):
                p["first_seen"] = date
            p["last_seen"] = date
            p.setdefault("history", []).append({
                "date": date, "hit": hit,
                "profit": profit, "return_ratio": return_ratio,
                "lota_id": lota_id,
            })
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

    def selected_active(self) -> tuple[list[tuple[str, dict, dict]], list[tuple[str, dict, dict]], int]:
        """
        返回 (main, aux, dormant_count)：
          main — 窗口内 n>=2 且加权回报>0 的活跃因子（按加权回报降序，最多 12 个）
          aux  — 窗口内样本不足 / 加权回报<=0 的因子（观察区，慎用）
          dormant_count — 超过 3×平均触发间隔未触发（或已被 review 标记 dormant）的因子数
        """
        if not self._loaded or not self.factor_perf:
            return [], [], 0
        main, aux, dormant_count = [], [], 0
        for fid, s in self.factor_perf.items():
            status = s.get("status", "active")
            if status == "retired":
                continue
            if status == "dormant":
                dormant_count += 1
                continue
            prof = factor_profile(s)
            if prof is None:
                continue
            if prof["dormant"]:
                dormant_count += 1
                continue
            item = (fid, s, prof)
            if prof["n"] >= 2 and prof["w_return"] > 0:
                main.append(item)
            else:
                aux.append(item)
        main.sort(key=lambda x: -x[2]["w_return"])
        aux.sort(key=lambda x: -x[2]["w_return"] if x[2] else 0)
        return main[:FACTOR_MAX_MAIN], aux[:6], dormant_count

    def perf_text(self) -> str:
        """分层注入：L1 负例护栏 + L2 正例(自适应main) + L3 观察摘要 + L4 休眠计数。"""
        if not self._loaded or not self.factor_perf:
            return ""
        main, aux, dormant_count = self.selected_active()
        retired = sorted(
            ((fid, s0) for fid, s0 in self.factor_perf.items() if s0.get("status") == "retired"),
            key=lambda x: -float(x[1].get("profit") or 0),
        )[:8]
        lines = []
        if retired:
            lines.append("🪦 已证伪模式（负例护栏，勿用）:")
            for fid, s0 in retired:
                lines.append(f"  ❌ {fid} (累计{float(s0.get('profit') or 0):+.0f})")
        if main:
            lines.append("📐 活跃因子（按自适应得分）:")
            for fid, s0, p0 in main:
                small = f" ⚠️样本少({p0['n']}单)" if p0["n"] < 5 else ""
                lines.append(f"  {fid} [近{p0['n']}单 命中{p0['hits']}/{p0['n']} 加权回报{p0['w_return']:+.2f}{small}]")
                desc = s0.get("desc", "")
                if desc:
                    lines.append(f"     {desc[:80]}")
        if aux:
            lines.append("📉 观察（样本不足/走弱，勿重仓）:")
            for fid, s0, p0 in aux[:15]:
                wr = p0["w_return"] if p0 else 0.0
                n = p0["n"] if p0 else 0
                lines.append(f"  ⚠️ {fid}: 近{n}单 加权回报{wr:+.2f}")
        if dormant_count:
            lines.append(f"  (另有 {dormant_count} 个休眠因子)")
        return "\n".join(lines)

    def factor_desc_text(self) -> str:
        """L2 正例完整定义：只输出自适应 main 的定义（预算内，库再大不膨胀）。"""
        main, _, _ = self.selected_active()
        active_names = {fid.lower() for fid, _, _ in main}
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
                    if an.lower().replace("_", "") == fid.replace("fac_", "").replace("_", ""):
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
        # 按角色隔离记忆: roles/{name}/memory/
        if role_name:
            base_dir = DATA_ROOT / "roles" / role_name / "memory"
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
            blocks.append(self.factors.perf_text())
            # 附加因子详细定义（从 fac_*.json 读取）
            desc_text = self.factors.factor_desc_text()
            if desc_text:
                blocks.append(desc_text)

        # Slug 表现
        if config.get("include_slug_perf", False):
            blocks.append(self.slugs.slug_perf_text())

        # Alpha 反思
        if config.get("include_reflections", True):
            ref_text = self.reflections.format_for_prompt()
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

    def add_reflection(self, date: str, reflection_text: str) -> None:
        """添加一条反思"""
        self.reflections.append({
            "date": date,
            "reflection": reflection_text,
            "recorded_at": _now(),
        })
        self._loaded = True
        self._save()

    def recent_reflections(self, n: int = 5) -> list[dict]:
        return self.reflections[-n:] if self.reflections else []

    def format_for_prompt(self) -> str:
        """注入 prompt 的反思文本"""
        if not self._loaded or not self.reflections:
            return ""
        lines = ["## 📝 历史反思（Alpha 因子积累）", ""]
        for r in self.reflections[-5:]:
            lines.append(f"### {r['date']}")
            lines.append(r["reflection"])
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
