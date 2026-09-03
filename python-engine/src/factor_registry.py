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
import re
import difflib
from pathlib import Path
from datetime import date as _date

from .factor_select import factor_profile, FACTOR_SAMPLE_WINDOW, FACTOR_SMALL_SAMPLE
from .role_registry import role_scope

ROLES_DIR = Path(__file__).parent.parent / "data" / "roles"
FACTORS_DIR = Path(__file__).parent.parent / "data" / "factors"

# 跨狗因子压制（狗已证伪 → 跨狗同模式因子不再标 ✅）匹配阈值
# 只做"近同名"精确压制，避免误伤好因子；换名不换义的同方向因子靠狗自己的护栏兜底。
_MATCH_BIT_DIST = 2               # slugs 对称差 <= 该值
_MATCH_NAME_RATIO = 0.80          # 有 slugs 时，名字相似度下限（近同名才算同模式）
_MATCH_ORPHAN_NAME_RATIO = 0.85   # 双方无 slugs 的孤儿因子，名字相似度下限


def _clean_name(name: str) -> str:
    n = (name or "").strip().strip('"\'“”`')
    n = re.sub(r"[（(][^）)]*[）)]", "", n).strip()
    n = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]", "", n).strip()
    return re.sub(r"\s+", " ", n)


class FactorRegistry:
    """跨角色因子聚合器"""

    def __init__(self, exclude_roles: set = None, scope: str = None,
                 reference_scopes: list[str] = None):
        self._cache: dict[str, dict] = {}  # {role_name: factor_perf}
        self._cache_ref: dict[str, dict] = {}  # 跨 scope 只读参考因子
        self._factor_defs: dict[str, dict] = {}  # {fac_id: {slugs, content}}
        self._exclude_roles: set = exclude_roles or set()
        self._scope = scope
        self._reference_scopes = list(reference_scopes or [])

    def refresh(self):
        """重新扫描所有角色的因子"""
        self._cache.clear()
        self._cache_ref.clear()
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
                        sc = role_scope(role_dir.name)
                        if self._scope and sc != self._scope:
                            if sc in self._reference_scopes:
                                self._cache_ref[role_dir.name] = fp
                            continue
                        self._cache[role_dir.name] = fp
                except Exception:
                    pass

    def _slugs_of(self, name: str, entry: dict) -> set:
        """解析一个因子的 slugs（优先条目冗余，其次回退全局 fac_*.json）。"""
        slugs = (entry or {}).get("slugs") or []
        if slugs:
            return set(slugs)
        fid = (entry or {}).get("fac_id") or f"fac_{_clean_name(name).lower().replace(' ','_')[:40]}"
        return set(self._factor_defs.get(fid, {}).get("slugs", []))

    def _is_suppressed(self, name: str, entry: dict, suppress: list) -> bool:
        """跨狗因子是否与该狗自己的已证伪(retired)因子同模式。

        匹配口径（与因子归纳的候选预筛一致）：
          - 清洗名精确相等 → 同模式
          - 双方有 slugs：对称差 <=2 且 名字相似 >=0.35 → 同模式
          - 双方都无 slugs（孤儿）：名字相似 >=0.60 → 同模式
        """
        if not suppress:
            return False
        cn = _clean_name(name)
        sa = self._slugs_of(name, entry)
        for sname, sentry in suppress:
            if cn == _clean_name(sname):
                return True
            sb = self._slugs_of(sname, sentry)
            ratio = difflib.SequenceMatcher(None, cn, _clean_name(sname)).ratio()
            if sa and sb and len(sa ^ sb) <= _MATCH_BIT_DIST and ratio >= _MATCH_NAME_RATIO:
                return True
            if not sa and not sb and ratio >= _MATCH_ORPHAN_NAME_RATIO:
                return True
        return False

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

                # 补全因子定义（优先用条目冗余的 fac_id，退回名字归一化）
                slugs, content = [], ""
                fac_id = fdata.get("fac_id") or f"fac_{factor_name.lower().replace(' ','_')[:40]}"
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
                    hit = sum(
                        1.0 if h.get("hit") is True else
                        (0.5 if h.get("hit") == 0.5 else 0.0)
                        for h in history
                    )
                    miss = sum(
                        1.0 if h.get("hit") is False else
                        (0.5 if h.get("hit") == -0.5 else 0.0)
                        for h in history
                    )
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
                          max_factors: int = 25,
                          suppress_entries: dict = None) -> str:
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
            return self._format_adaptive_prompt(
                max_factors=max_factors, suppress_entries=suppress_entries
            )

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

    def _format_adaptive_prompt(self, max_factors: int = 25,
                                suppress_entries: dict = None) -> str:
        """
        自适应版跨 Agent 因子注册表：
          每个因子取最近 N 次触发，指数衰减加权计算单注回报与波动。
          只保留 方向明确 的因子，剔除"平庸"因子（不是无脑取全部）：
            - 近期触发 >=3 次（杜绝 1-2 单"全中"幻觉）
            - 非平庸（|平均回报|≈0 且收缩命中率在硬币区间 → 剔除）
            - 正、负方向都保留：明确能赢的(✅) 与 明确会输/该规避的(🔴)
          按 |加权回报| 降序取 Top-K（赢得多/输得多的都浮出来），波动仅作展示。
          suppress_entries: 该狗自己已证伪(retired)的 {因子名: 条目}；匹配到的
            跨狗同模式因子直接剔除，不再当 ✅ 喂给模型，护栏与跨狗不再打架。
        """
        if not self._cache:
            self.refresh()
        from .factor_select import (
            CROSS_LOW_INFO_HIT_HI,
            CROSS_LOW_INFO_HIT_LO,
            CROSS_MIN_EDGE_RETURN,
            CROSS_MIN_SAMPLE,
            factor_profile,
        )
        rows = []
        suppress = list((suppress_entries or {}).items())
        suppressed_set: set[str] = set()
        for role_name, factor_perf in self._cache.items():
            for factor_name, fdata in factor_perf.items():
                if fdata.get("status") in ("retired", "dormant"):
                    continue
                if self._is_suppressed(factor_name, fdata, suppress):
                    suppressed_set.add(factor_name)
                    continue
                p = factor_profile(fdata)
                if p is None or p["dormant"]:
                    continue
                if p["n"] < CROSS_MIN_SAMPLE:
                    continue
                # 平庸因子：命中率≈五五开 且 |回报| 不够强 → 方向不明确，剔除
                in_coin_band = (
                    CROSS_LOW_INFO_HIT_LO <= p["shrunk_rate"] <= CROSS_LOW_INFO_HIT_HI
                )
                weak_edge = abs(p["w_return"]) < CROSS_MIN_EDGE_RETURN
                mediocre = in_coin_band and weak_edge
                if mediocre:
                    continue
                rows.append((factor_name, role_name, fdata, p))
        rows.sort(key=lambda x: -abs(x[3]["w_return"]))
        rows = rows[:max_factors]

        ref_block = self._format_reference_block()

        if not rows:
            return ("(跨Agent因子注册表: 近窗口内无方向明确的因子)"
                    + ref_block)

        lines = [
            "## 📐 跨Agent因子注册表（自适应: 最近N单·衰减加权·方向明确·含正负）",
            f"  {len(rows)} 个因子 | 来自 {len({r[1] for r in rows})} 个Agent",
            "  ⚠️ 样本<5 的因子仅作方向参考，仓位减半/试探",
        ]
        if suppressed_set:
            lines.append(
                f"  ⛔ 已抑制 {len(suppressed_set)} 个与本人已证伪模式冲突的跨狗因子："
                f"{'、'.join(sorted(suppressed_set)[:6])}"
                + ("…" if len(suppressed_set) > 6 else "")
            )
        lines.append("")
        for factor_name, role_name, fdata, p in rows:
            small = f" ⚠️样本少({p['n']}单)" if p["n"] < FACTOR_SMALL_SAMPLE else ""
            sign = "✅" if p["w_return"] > 0 else "🔴"
            lines.append(
                f"  {sign} `{factor_name}` [{role_name}] 近{p['n']}单 命中{p['hits']}/{p['n']} "
                f"加权回报{p['w_return']:+.2f} 波动{p['volatility']:.2f} "
                f"收缩命中{p['shrunk_rate']:.0%}{small}"
            )
            desc = fdata.get("desc", "")
            if desc:
                lines.append(f"     {desc[:100]}")
            slugs = fdata.get("slugs") or []
            if not slugs:
                fac_id = fdata.get("fac_id") or f"fac_{factor_name.lower().replace(' ','_')[:40]}"
                slugs = self._factor_defs.get(fac_id, {}).get("slugs", [])
            if slugs:
                lines.append(f"     slugs: {', '.join(slugs[:5])}")
        if ref_block:
            lines.append(ref_block)
        return "\n".join(lines)

    def _format_reference_block(self, max_factors: int = 15) -> str:
        """跨 scope 只读参考区：只展示，不参与统计 / 排序 / 信任权重。"""
        if not self._cache_ref:
            return ""
        rows: list[tuple[str, str, dict, dict]] = []
        for role_name, factor_perf in self._cache_ref.items():
            for factor_name, fdata in factor_perf.items():
                if fdata.get("status") in ("retired", "dormant"):
                    continue
                p = factor_profile(fdata)
                if p is None or p["dormant"]:
                    continue
                rows.append((factor_name, role_name, fdata, p))
        if not rows:
            return ""
        rows.sort(key=lambda x: -x[3]["w_return"])
        rows = rows[:max_factors]
        lines = [
            "",
            "## 🔍 跨池参考因子（只读，不参与统计/排序，勿作下注重仓依据）",
            f"  {len(rows)} 个跨 scope 因子，仅供对照当前场次盘口信号。",
        ]
        for factor_name, role_name, fdata, p in rows:
            lines.append(
                f"  · `{factor_name}` [{role_name}] 近{p['n']}单 "
                f"收缩命中{p['shrunk_rate']:.0%} 加权回报{p['w_return']:+.2f}"
            )
            desc = fdata.get("desc", "")
            if desc:
                lines.append(f"     {desc[:80]}")
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
