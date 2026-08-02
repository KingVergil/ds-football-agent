"""
跨 Agent 因子注册表 — alpha 狗用来读取所有狗的因子，按时间维度聚合。

用法:
  from factor_registry import FactorRegistry
  fr = FactorRegistry()

  # 获取截止某日期前所有因子
  factors = fr.get_all_factors(before_date="2026-06-15")

  # 格式化注入 prompt
  text = fr.format_for_prompt(current_date="2026-06-16")
"""

import json
from pathlib import Path
from datetime import date as _date

from .factor_select import factor_profile, FACTOR_SAMPLE_WINDOW, FACTOR_SMALL_SAMPLE

ROLES_DIR = Path(__file__).parent.parent / "lota_data" / "roles"
FACTORS_DIR = Path(__file__).parent.parent / "lota_data" / "factors"


class FactorRegistry:
    """跨角色因子聚合器"""

    def __init__(self, exclude_roles: set = None):
        self._cache: dict[str, dict] = {}  # {role_name: factor_perf}
        self._factor_defs: dict[str, dict] = {}  # {fac_id: {slugs, content}}
        self._exclude_roles: set = exclude_roles or set()

    def refresh(self):
        """重新扫描所有角色的因子"""
        self._cache.clear()
        self._factor_defs.clear()

        # 因子定义（全局 fac_*.json）
        if FACTORS_DIR.exists():
            for fpath in FACTORS_DIR.glob("fac_*.json"):
                try:
                    d = json.loads(fpath.read_text(encoding="utf-8"))
                    self._factor_defs[d["id"]] = d
                except Exception:
                    pass

        # 各角色因子表现
        if ROLES_DIR.exists():
            for role_dir in sorted(ROLES_DIR.iterdir()):
                if not role_dir.is_dir():
                    continue
                if role_dir.name in self._exclude_roles:
                    continue
                mem_path = role_dir / "memory" / "factor_memory.json"
                if not mem_path.exists():
                    continue
                try:
                    data = json.loads(mem_path.read_text(encoding="utf-8"))
                    fp = data.get("factor_perf", {})
                    if fp:
                        self._cache[role_dir.name] = fp
                except Exception:
                    pass

    def get_all_factors(self, before_date: str = None,
                        include_retired: bool = False,
                        window_days: int = 0,
                        min_samples: int = 0) -> list[dict]:
        """
        获取所有因子，按首次发现日期排序。

        Args:
            before_date: 只返回在此日期之前发现的因子，且仅用 <=该日期的
                         history 条目重算统计指标 (ISO format "2026-06-15")
            include_retired: 是否包含已退役因子
            window_days: >0 时仅用 [before_date - window_days, before_date]
                         窗口内的 history 条目重算指标，并过滤掉窗口内样本不足
                         或回报 <=0 的因子
            min_samples: window_days>0 时，窗口内最少样本数（与 window_days 配合使用）
        Returns:
            [{factor_name, role, first_seen, last_seen, total, hit, miss, profit,
              total_return, status, desc, slugs, content, history}]
        """
        if not self._cache:
            self.refresh()

        result = []
        for role_name, factor_perf in self._cache.items():
            for factor_name, fdata in factor_perf.items():
                if not include_retired and fdata.get("status") == "retired":
                    continue
                first = fdata.get("first_seen", "?")
                if before_date and first != "?" and first > before_date:
                    continue  # 因子在回测日期之后才发现，跳过

                # 补全因子定义
                slugs, content = [], ""
                fac_id = f"fac_{factor_name.lower().replace(' ','_')[:40]}"
                fac_def = self._factor_defs.get(fac_id, {})
                slugs = fac_def.get("slugs", [])
                content = fdata.get("desc", "") or fac_def.get("content", "")

                raw_history = fdata.get("history", [])

                # ── 按 before_date 过滤 history 并重算指标 ──
                if before_date:
                    history = [h for h in raw_history
                               if h.get("date", "") <= before_date]

                    # 滚动窗口：仅保留窗口内的记录并重算
                    if window_days > 0 and before_date:
                        from datetime import date as _dt, timedelta as _td
                        try:
                            cutoff = _dt.fromisoformat(before_date) - _td(days=window_days)
                            cutoff_str = cutoff.isoformat()
                        except ValueError:
                            cutoff_str = ""
                        if cutoff_str:
                            history = [h for h in history
                                       if h.get("date", "") > cutoff_str]

                    total = len(history)
                    hit = sum(1 for h in history if h.get("hit") is True)
                    miss = sum(1 for h in history if h.get("hit") is False)
                    push = sum(1 for h in history if h.get("hit") is None)
                    profit = sum(h.get("profit", 0) for h in history)
                    total_return = sum(h.get("return_ratio", 0) for h in history)
                    last_seen = before_date

                    # 窗口过滤：样本不足或回报非正 → 跳过
                    if window_days > 0:
                        if total < min_samples:
                            continue
                        if total_return <= 0:
                            continue
                else:
                    history = raw_history
                    total = fdata.get("total", 0)
                    hit = fdata.get("hit", 0)
                    miss = fdata.get("miss", 0)
                    push = fdata.get("push", 0)
                    profit = fdata.get("profit", 0)
                    total_return = fdata.get("total_return") if "total_return" in fdata else fdata.get("profit", 0.0)
                    last_seen = fdata.get("last_seen", "?")

                result.append({
                    "factor_name": factor_name,
                    "role": role_name,
                    "first_seen": first,
                    "last_seen": last_seen,
                    "total": total,
                    "hit": hit,
                    "miss": miss,
                    "push": push,
                    "profit": profit,
                    "total_return": total_return,
                    "status": fdata.get("status", "active"),
                    "desc": fdata.get("desc", ""),
                    "slugs": slugs,
                    "content": content,
                    "history": history,
                })

        result.sort(key=lambda x: x["first_seen"])
        return result

    def format_for_prompt(self, current_date: str = None,
                          include_retired: bool = False,
                          window_days: int = 0,
                          min_samples: int = 0,
                          adaptive: bool = False,
                          max_factors: int = 25) -> str:
        """
        格式化因子注册表 → LLM prompt 可用文本。
        按时间分组，标注来源角色。

        Args:
            window_days: >0 时启用滚动窗口过滤，仅展示窗口内 ≥min_samples
                         且总回报 >0 的因子。统计数字也基于窗口重算。
            min_samples: 窗口内最低样本数（配合 window_days 使用）。
            adaptive: 用自适应因子选择（最近 N 单 + 衰减加权 + 休眠过滤），
                      替代固定时间窗口；用于 live prompt。
            max_factors: adaptive 模式下的最大展示数量。
        """
        if adaptive:
            return self._format_adaptive_prompt(max_factors=max_factors)

        factors = self.get_all_factors(before_date=current_date,
                                       include_retired=include_retired,
                                       window_days=window_days,
                                       min_samples=min_samples)
        if not factors:
            return "(因子注册表为空)"

        # 按发现日期分组
        by_date: dict[str, list[dict]] = {}
        for f in factors:
            d = f["first_seen"][:10] if f["first_seen"] else "?"
            by_date.setdefault(d, []).append(f)

        window_label = f"近{window_days}天 ≥{min_samples}单 回报>0" if window_days > 0 else "全量"
        lines = ["## 📐 跨Agent因子注册表", ""]
        lines.append(f"  {window_label} | {len(factors)} 个因子 | "
                     f"{len(by_date)} 个发现日 | "
                     f"来自 {len(set(f['role'] for f in factors))} 个Agent")
        lines.append("")

        for date in sorted(by_date.keys()):
            day_factors = by_date[date]
            lines.append(f"### {date} ({len(day_factors)} 因子)")
            for f in sorted(day_factors, key=lambda x: -x["total"]):
                denom = f["total"] - f["push"]
                rate = f"{f['hit']/denom*100:.0f}%" if denom > 0 else "-"
                ret = f.get("total_return", f.get("profit", 0))
                color = "✅" if ret > 0 else ("❌" if ret < 0 else "➖")
                lines.append(
                    f"  {color} `{f['factor_name']}` [{f['role']}] "
                    f"{f['total']}次 命中{rate} 回报率{ret:+.2f}"
                )
                if f.get("desc"):
                    lines.append(f"     {f['desc'][:120]}")
                if f.get("slugs"):
                    lines.append(f"     slugs: {', '.join(f['slugs'][:5])}")
            lines.append("")

        return "\n".join(lines)

    def _format_adaptive_prompt(self, max_factors: int = 25) -> str:
        """
        自适应版跨 Agent 因子注册表：
          每个因子取最近 N 次触发，指数衰减加权计算单注回报，
          只展示 近期触发 >=2 次 且 加权回报 >0 的因子，按回报降序取 Top-K。
        """
        if not self._cache:
            self.refresh()
        rows = []
        for role_name, factor_perf in self._cache.items():
            for factor_name, fdata in factor_perf.items():
                if fdata.get("status") in ("retired", "dormant"):
                    continue
                p = factor_profile(fdata)
                if p is None or p["dormant"]:
                    continue
                if p["n"] < 2 or p["w_return"] <= 0:
                    continue
                rows.append((factor_name, role_name, fdata, p))
        rows.sort(key=lambda x: -x[3]["w_return"])
        rows = rows[:max_factors]

        if not rows:
            return "(跨Agent因子注册表: 近窗口内无正回报因子)"

        lines = [
            "## 📐 跨Agent因子注册表（自适应: 最近N单·衰减加权·仅正回报）",
            f"  {len(rows)} 个因子 | 来自 {len({r[1] for r in rows})} 个Agent",
            "  ⚠️ 样本<5 的因子仅作方向参考，仓位减半/试探",
            "",
        ]
        for factor_name, role_name, fdata, p in rows:
            small = f" ⚠️样本少({p['n']}单)" if p["n"] < FACTOR_SMALL_SAMPLE else ""
            lines.append(
                f"  ✅ `{factor_name}` [{role_name}] 近{p['n']}单 命中{p['hits']}/{p['n']} "
                f"加权回报{p['w_return']:+.2f} 收缩命中{p['shrunk_rate']:.0%}{small}"
            )
            desc = fdata.get("desc", "")
            if desc:
                lines.append(f"     {desc[:100]}")
            slugs = fdata.get("slugs") or []
            if slugs:
                lines.append(f"     slugs: {', '.join(slugs[:5])}")
        return "\n".join(lines)

    def summary(self) -> str:
        """简短摘要"""
        self.refresh()
        active = sum(
            1 for fp in self._cache.values()
            for f in fp.values() if f.get("status") != "retired"
        )
        return (f"跨Agent因子注册表: {len(self._cache)} 个Agent, "
                f"{active} 个活跃因子")


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    fr = FactorRegistry()
    date_cutoff = sys.argv[1] if len(sys.argv) > 1 else None

    if date_cutoff:
        print(f"=== 截止 {date_cutoff} 的跨Agent因子 ===\n")

    print(fr.format_for_prompt(current_date=date_cutoff))

    if not date_cutoff:
        print("用法: python factor_registry.py [截止日期]  例: python factor_registry.py 2026-06-15")
