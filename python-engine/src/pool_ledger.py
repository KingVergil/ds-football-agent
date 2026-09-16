"""奖池错价账本（census ledger）：按「盘口类型 × x 区间」数开奖派彩，给引擎两道硬门。

## 它是什么

北单是奖池型玩法，返奖 `2元 × 65% × ΠSP`（0.65 只乘一次）。账本每天把**全部已开奖的
北单场次**按桶累计两个数：

    x = p_锐市场(去水) × o_北单赛前赔率      ← 赛前可算：奖池比锐市场便宜多少
    z = 开奖SP × 1{该侧中了}                  ← 开奖后才知：这条腿实际每 1 元拿回多少

    y = E[z]        单腿每 1 元期望 = 0.65·y − 1
    M 关串每 1 元期望 = 0.65·y^M − 1     ← 0.65 只乘一次，所以长串才可能为正
    打平关数 m_star = ceil( ln(1/0.65) / ln(y) )

计数量只有 `n / Σz / Σz²`，所以 y、SE、95%CI 都能直接算，不用存样本、不调 LLM。

## 为什么是"普查"而不是"我们下过的腿"

只统计自己下过的腿会带上选择偏差（下过的腿本来就是精挑的）。账本吃**当天所有北单场次**，
所以样本量是每天几十~上百条，而不是每因子个位数 —— 这是它能收敛的唯一原因。

## 工程要点（踩过的坑）

1. **SP 滞后**：北单多数要等整期结束（约 3 天）才出开奖 SP，当天常常拿不到前一天的 SP。
   所以更新接口是 **回看窗口**（默认 7 天，`--days`），每天反复扫，SP 到了就补记；绝不绑死"前一天"。
2. **幂等**：每个 `lota_id` 只入账一次（`ingested`），所以反复扫同一天不会重复计数。
3. **防未来**：只入账 **足球日 < as_of** 的场次。回放里 `as_of` = 回放当天，因此即使本地缓存里
   有后续日期的结果，也不会被吃进来（回放不偷看未来）。
4. **桶定义冻结**：`X_BUCKETS` + `gl0 / glN` 是预先登记的，不许边跑边调（防 p-hacking）。

## 用法

    python3 -m src.pool_ledger update --role bc狗 --days 7      # 日常（回看 7 天）
    python3 -m src.pool_ledger update --role bc狗 --all         # 首次回填全部缓存
    python3 -m src.pool_ledger report --role bc狗               # 看各桶 y / CI / 打平关数
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Iterable, Optional

TAKEOUT = 0.65
SIDES = ("H", "D", "A")

# 预先登记的 x 网格（错价比率），与 docs/mispricing_bucket_report.md 一致
X_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.0, 0.9), (0.9, 1.0), (1.0, 1.1), (1.1, 1.2), (1.2, 1.35), (1.35, 1.6), (1.6, 99.0))
X_CANDIDATES: tuple[float, ...] = (1.0, 1.1, 1.2, 1.35)   # 只用登记过的桶边界

FAIR_LINE_RE = re.compile(
    r"(?:平均欧盘胜/平/负|竞彩胜平负)\s*\(([-+]?\d+(?:\.\d+)?)\)\s*[:：]\s*"
    r"([\d.]+)\s*[/／]\s*([\d.]+)\s*[/／]\s*([\d.]+)")
PINNACLE_TRIPLE_RE = re.compile(r"([\d.]+)\s*/\s*([\d.]+)\s*/\s*([\d.]+)")


# ─────────────────────────── 基础 ───────────────────────────

def devig(odds: Iterable[float]) -> list[float]:
    """赔率 → 去水概率（比例法）：p_i = (1/o_i) / Σ_j (1/o_j)。"""
    inv = [1.0 / o for o in odds if o and o > 0]
    tot = sum(inv)
    return [v / tot for v in inv] if tot > 0 else []


def sharp_ref(tags: dict, goal_line: float) -> tuple[str, list[float]]:
    """与北单 goal_line **同一盘口**的锐市场赔率（拿不到返回 ("", [])）。

    gl=0 → Pinnacle 1X2（`eu-odds-pinnacle` 段最后一行 = 最接近开赛）；
    gl≠0 → 公平盘「平均欧盘胜/平/负(<line>)」且 line == goal_line。
    """
    secs = tags.get("sections") or {}
    if goal_line != 0.0:
        txt = secs.get("fair-odds") or ""
        if not isinstance(txt, str):
            txt = json.dumps(txt, ensure_ascii=False)
        for m in FAIR_LINE_RE.finditer(txt):
            if abs(float(m.group(1)) - goal_line) < 1e-9:
                o = [float(m.group(i)) for i in (2, 3, 4)]
                if min(o) > 0:
                    return "让球欧盘(line 对齐)", o
        return "", []
    txt = secs.get("eu-odds-pinnacle") or ""
    if not isinstance(txt, str):
        txt = json.dumps(txt, ensure_ascii=False)
    trips = [t for t in PINNACLE_TRIPLE_RE.findall(txt) if all(float(v) > 1.0 for v in t)]
    return ("Pinnacle 1X2", [float(v) for v in trips[-1]]) if trips else ("", [])


def x_bucket(x: float) -> str:
    for lo, hi in X_BUCKETS:
        if lo <= x < hi:
            return f"{lo:g}–{hi:g}"
    return "?"


def x_bucket_lo(label: str) -> float:
    return float(str(label).split("–")[0])


def handicap_class(goal_line: float) -> str:
    """盘口类型：gl0（不让球）/ glN（让球）。"""
    return "gl0" if abs(float(goal_line or 0.0)) < 1e-9 else "glN"


def bucket_key(goal_line: float, x: float) -> str:
    return f"{handicap_class(goal_line)}|{x_bucket(x)}"


def role_dir(role: str) -> Path:
    """角色目录：沙箱（DS_ROLES_ROOT）是单狗平铺，线上是 data/roles/<角色>。"""
    root = os.environ.get("DS_ROLES_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parent.parent / "data" / "roles" / role


def ledger_path(role: str) -> Path:
    return role_dir(role) / "memory" / "pool_ledger.json"


# ─────────────────────────── 采样 ───────────────────────────

def collect_day_rows(day: str, matches_dir: Optional[Path] = None,
                     tags_dir: Optional[Path] = None,
                     skip: Optional[set[str]] = None) -> list[dict]:
    """读某足球日缓存 → 每场一行（带三侧 x、开奖 result/SP，若已有）。

    拿不到同盘口锐市场参考的场次直接跳过（无法算 x）。
    """
    from .data_manager import MATCHES_DIR, TAGS_DIR
    matches_dir = matches_dir or MATCHES_DIR
    tags_dir = tags_dir or TAGS_DIR
    p = Path(matches_dir) / f"{day}.json"
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = payload if isinstance(payload, list) else (payload.get("matches") or [])
    out: list[dict] = []
    for m in items:
        lid = m.get("lota_id") or m.get("id")
        bi = m.get("beidan_info") or {}
        if not lid or not bi or (skip and lid in skip):
            continue
        o = [bi.get("home_odds"), bi.get("draw_odds"), bi.get("away_odds")]
        if not all(o):
            continue
        try:
            gl = float(bi.get("goal_line"))
        except (TypeError, ValueError):
            gl = 0.0
        tags = None
        # 回放模式：段落一律从 fet_txt 切片即时切分（尊重当前 access_time），
        # 与「分析决策」同源；绝不读线上终盘 tags 缓存（那会前视）。
        try:
            from . import backtest_fet as _bf
            src = _bf.current() if _bf.active() else None
            if src is not None and src.in_scope(lid):
                tags = {"sections": src.sections(lid)}
        except Exception:
            tags = None
        if tags is None:
            tp = Path(tags_dir) / f"{lid}.json"
            if not tp.exists():
                continue
            try:
                tags = json.loads(tp.read_text(encoding="utf-8"))
            except Exception:
                continue
        src, s_odds = sharp_ref(tags, gl)
        if not src:
            continue
        p_sharp, p_pool = devig(s_odds), devig([float(v) for v in o])
        if len(p_sharp) != 3 or len(p_pool) != 3:
            continue
        sp = bi.get("spvalue")
        out.append({
            "lid": lid, "date": day, "gl": gl, "src": src,
            "league": m.get("league_name") or "",
            "x": {s: p_sharp[i] * float(o[i]) for i, s in enumerate(SIDES)},
            "p_sharp": dict(zip(SIDES, p_sharp)),
            "o_pool": {s: float(o[i]) for i, s in enumerate(SIDES)},
            "sp": float(sp) if sp else None,
            "result": bi.get("result"),
        })
    return out


def available_days(matches_dir: Optional[Path] = None) -> list[str]:
    from .data_manager import MATCHES_DIR
    matches_dir = matches_dir or MATCHES_DIR
    return sorted(p.stem for p in Path(matches_dir).glob("*.json")
                  if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))


# ─────────────────────────── 账本 ───────────────────────────

class PoolLedger:
    """按桶计数的奖池账本（幂等、as_of 防未来、支持滚动窗口）。"""

    VERSION = 1

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.data: dict = {"version": self.VERSION, "buckets": {}, "ingested": {},
                           "meta": {}}
        self._loaded = False

    # ── 读写 ────────────────────────────────
    def load(self) -> "PoolLedger":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = {"version": raw.get("version", self.VERSION),
                             "buckets": raw.get("buckets") or {},
                             "ingested": raw.get("ingested") or {},
                             "meta": raw.get("meta") or {}}
        except Exception:
            pass
        self._loaded = True
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["meta"]["updated_at"] = _now()
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ── 入账 ────────────────────────────────
    def ingest(self, rows: list[dict], as_of: Optional[str] = None) -> dict:
        """把若干行（每行一场）入账。幂等；`as_of` 之后（含当天）的场次一律不吃。"""
        from .beidan_settlement import result_code_to_pick
        added = skipped_sp = skipped_future = 0
        ingested = self.data["ingested"]
        for r in rows:
            lid = r.get("lid")
            day = str(r.get("date") or "")
            if not lid or lid in ingested:
                continue
            if as_of and not (day < str(as_of)):
                skipped_future += 1
                continue
            win = result_code_to_pick(r.get("result"))
            sp = r.get("sp")
            if win not in SIDES or not sp or sp <= 0:
                skipped_sp += 1
                continue
            for s in SIDES:
                x = float((r.get("x") or {}).get(s) or 0.0)
                if x <= 0:
                    continue
                key = bucket_key(r.get("gl") or 0.0, x)
                b = self.data["buckets"].setdefault(key, {
                    "n": 0, "sum_z": 0.0, "sum_z2": 0.0, "by_day": {}})
                z = float(sp) if s == win else 0.0
                b["n"] += 1
                b["sum_z"] += z
                b["sum_z2"] += z * z
                d = b["by_day"].setdefault(day, {"n": 0, "sum_z": 0.0, "sum_z2": 0.0})
                d["n"] += 1
                d["sum_z"] += z
                d["sum_z2"] += z * z
            ingested[lid] = day
            added += 1
        self.data["meta"]["last_ingest"] = {
            "at": _now(), "as_of": as_of, "added": added,
            "skipped_no_sp": skipped_sp, "skipped_future": skipped_future}
        return {"added": added, "skipped_no_sp": skipped_sp,
                "skipped_future": skipped_future}

    # ── 查询 ────────────────────────────────
    def _agg(self, gl_class: str, x_lo: float, as_of: Optional[str] = None,
             window_days: Optional[int] = None, only_label: Optional[str] = None) -> dict:
        n = 0
        s1 = s2 = 0.0
        days: list[str] = []
        lo_bound = None
        if as_of and window_days:
            lo_bound = (_date.fromisoformat(str(as_of)[:10])
                        - timedelta(days=int(window_days))).isoformat()
        for key, b in (self.data.get("buckets") or {}).items():
            cls, _, lab = str(key).partition("|")
            if cls != gl_class:
                continue
            if only_label is not None:
                if lab != only_label:
                    continue
            else:
                try:
                    if x_bucket_lo(lab) < x_lo - 1e-9:
                        continue
                except ValueError:
                    continue
            by_day = b.get("by_day") or {}
            if by_day:
                for day, d in by_day.items():
                    if as_of and not (day < str(as_of)):
                        continue
                    if lo_bound and day < lo_bound:
                        continue
                    n += int(d.get("n") or 0)
                    s1 += float(d.get("sum_z") or 0.0)
                    s2 += float(d.get("sum_z2") or 0.0)
                    days.append(day)
            elif not as_of and not lo_bound:
                # 没有日粒度（手写/旧数据）：只在不做时间切片时用桶总量
                n += int(b.get("n") or 0)
                s1 += float(b.get("sum_z") or 0.0)
                s2 += float(b.get("sum_z2") or 0.0)
        if n <= 0:
            return {"n": 0, "y": 0.0, "se": 0.0, "lo": 0.0, "hi": 0.0,
                    "days": 0, "m_star": None,
                    "ev1": None, "ev5": None, "ev9": None,
                    "ev5_lo": None, "ev9_lo": None}
        y = s1 / n
        var = max(0.0, s2 / n - y * y)
        se = math.sqrt(var / n)
        lo, hi = y - 1.96 * se, y + 1.96 * se
        m_star = (math.ceil(math.log(1 / TAKEOUT) / math.log(lo))
                  if lo > 1.0 else None)
        return {"n": int(n), "y": y, "se": se, "lo": lo, "hi": hi,
                "days": len(set(days)), "m_star": m_star,
                "ev1": TAKEOUT * y - 1.0, "ev5": TAKEOUT * y ** 5 - 1.0,
                "ev9": TAKEOUT * y ** 9 - 1.0,
                "ev5_lo": TAKEOUT * lo ** 5 - 1.0, "ev9_lo": TAKEOUT * lo ** 9 - 1.0}

    def bucket_stats(self, gl_class: str, label: str,
                     as_of: Optional[str] = None,
                     window_days: Optional[int] = None) -> dict:
        """单个桶（如 gl0 / 1.1–1.2）的统计。"""
        return self._agg(gl_class, 0.0, as_of=as_of, window_days=window_days,
                         only_label=label)

    def stats(self, gl_class: str = "gl0", x_lo: float = 1.1,
              as_of: Optional[str] = None, window_days: Optional[int] = None) -> dict:
        """x ≥ x_lo 的累计统计（按 `as_of` 之前、可选滚动窗口）。"""
        return self._agg(gl_class, x_lo, as_of=as_of, window_days=window_days)

    def policy(self, cfg: Optional[dict] = None,
               as_of: Optional[str] = None) -> dict:
        """给引擎用的两道门策略：腿池门（gl + x ≥ θ）+ 关数门（每注关数 ≥ m_star）。

        账本数据不足（n < min_n）→ 回落配置默认 θ / 默认最小关数，**不因为没数据就停投**；
        数据充足但没有正边际（y 的 CI 下沿 ≤ 1）→ `m_star = None`，引擎应空仓（自动降级）。
        """
        pc = (cfg or {}).get("pool_gate") or {}
        gl_classes = [str(c) for c in (pc.get("gl_classes") or ["gl0"])]
        min_n = int(pc.get("min_n") or 30)
        window_days = pc.get("window_days")
        window_days = int(window_days) if window_days else None
        default_theta = float(pc.get("default_theta") or 1.1)
        default_min_legs = int(pc.get("default_min_legs") or 3)
        max_min_legs = int(pc.get("max_min_legs") or 9)
        gl_class = gl_classes[0]

        # 在登记过的阈值里挑「CI 下沿最高」的那个（= 最保守目标），避免把 x≈1.0
        # 这种没有边际的腿混进来把 y 稀释掉。
        theta = None
        st = None
        for cand in (pc.get("theta_candidates") or X_CANDIDATES):
            s = self.stats(gl_class, float(cand), as_of=as_of, window_days=window_days)
            if s["n"] < min_n or s["lo"] <= 1.0:
                continue
            if st is None or s["lo"] >= st["lo"] - 1e-12:
                # 同一下沿时取**更严格**的阈值（候选升序 → 后者优先）
                theta, st = float(cand), s
        if theta is None:
            widest = float((pc.get("theta_candidates") or X_CANDIDATES)[0])
            ref = self.stats(gl_class, widest, as_of=as_of, window_days=window_days)
            mode = str(pc.get("mode") or "off").lower()
            block_n = int(pc.get("block_min_n") or 200)
            block_d = int(pc.get("block_min_days") or 8)
            if ref["n"] >= max(min_n, block_n) and ref["days"] >= block_d:
                # 有足够数据但没有可证实的正边际 → 自动降级：不出票（空仓）
                return {"mode": mode, "gl_classes": gl_classes,
                        "theta": default_theta, "y": ref["y"], "y_lo": ref["lo"],
                        "n": ref["n"], "days": ref["days"], "m_star": None,
                        "source": "ledger",
                        "reason": (f"账本（{gl_class} x≥{widest:g}: n={ref['n']}, "
                                   f"y={ref['y']:.3f} [{ref['lo']:.3f},{ref['hi']:.3f}]）"
                                   "里没有 CI 下沿 >1 的桶 → 无可证实的正边际，空仓")}
            return {"mode": mode, "gl_classes": gl_classes, "theta": default_theta,
                    "y": ref["y"], "y_lo": ref["lo"], "n": ref["n"],
                    "days": ref["days"], "m_star": default_min_legs,
                    "source": "config_default",
                    "reason": (f"账本证据不足（{gl_class} x≥{widest:g}: n={ref['n']}, "
                               f"{ref['days']} 天，未达停投门槛 n≥{max(min_n, block_n)}"
                               f" 且 ≥{block_d} 天）→ 不下「无边际」结论，"
                               f"用配置默认 θ={default_theta:g}、最小 {default_min_legs} 关")}
        m_star = st["m_star"]
        if m_star is not None:
            m_star = max(1, min(int(m_star), max_min_legs))
        return {"mode": str(pc.get("mode") or "off").lower(),
                "gl_classes": gl_classes, "theta": theta,
                "y": st["y"], "y_lo": st["lo"], "n": st["n"], "days": st["days"],
                "m_star": m_star, "source": "ledger",
                "reason": (f"账本 {gl_class} x≥{theta:g}: n={st['n']}（{st['days']} 天）"
                           f" y={st['y']:.3f} [{st['lo']:.3f},{st['hi']:.3f}]"
                           + (f" → 每注至少 {m_star} 关才打平" if m_star
                              else " → CI 下沿 ≤1，无可证实的正边际，空仓"))}

    def report(self, min_n: int = 20, as_of: Optional[str] = None,
               window_days: Optional[int] = None) -> str:
        meta = self.data.get("meta") or {}
        lines = [f"📒 奖池账本 {self.path}",
                 f"   更新 {meta.get('updated_at', '—')}"
                 f" | 已入账 {len(self.data.get('ingested') or {})} 场"
                 + (f" | 口径 as_of={as_of} 窗口{window_days}天" if as_of else "")]
        for cls in ("gl0", "glN"):
            for lo, hi in X_BUCKETS:
                lab = f"{lo:g}–{hi:g}"
                st = self.bucket_stats(cls, lab, as_of=as_of, window_days=window_days)
                if st["n"] < min_n:
                    continue
                lines.append(f"   {cls} x{lab:<9} n={st['n']:>5d} y={st['y']:.3f} "
                             f"[{st['lo']:.3f},{st['hi']:.3f}] 单腿{st['ev1']:+.0%} "
                             f"5关{st['ev5']:+.0%} 9关{st['ev9']:+.0%} "
                             f"打平≥{st['m_star'] if st['m_star'] else '∞'}关")
        return "\n".join(lines)


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ─────────────────────────── 更新入口 ───────────────────────────

def update(role: str, as_of: Optional[str] = None, lookback_days: int = 14,
           all_days: bool = False, dry_run: bool = False,
           matches_dir: Optional[Path] = None,
           tags_dir: Optional[Path] = None) -> dict:
    """更新账本：默认回看 `as_of` 之前 `lookback_days` 天（解决开奖 SP 滞后 3 天）。

    * `as_of=None` → 用今天（真实日期）；
    * 只吃 **足球日 < as_of** 的场次（回放防未来）；
    * 幂等：已入账的 lid 不会重复计数。
    """
    as_of = str(as_of or _date.today().isoformat())[:10]
    days = available_days(matches_dir)
    if all_days:
        picks = [d for d in days if d < as_of]
    else:
        lo = (_date.fromisoformat(as_of) - timedelta(days=int(lookback_days))).isoformat()
        picks = [d for d in days if lo <= d < as_of]
    led = PoolLedger(ledger_path(role)).load()
    skip = set((led.data.get("ingested") or {}).keys())
    rows: list[dict] = []
    for d in picks:
        rows.extend(collect_day_rows(d, matches_dir=matches_dir,
                                     tags_dir=tags_dir, skip=skip))
    res = led.ingest(rows, as_of=as_of)
    res.update({"role": role, "as_of": as_of, "days_scanned": len(picks),
                "rows": len(rows), "scanned": picks[:3] + (["…"] if len(picks) > 3 else [])})
    if not dry_run:
        led.save()
    return res


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(prog="pool_ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update", help="更新账本（回看窗口）")
    up.add_argument("--role", required=True)
    up.add_argument("--as-of", default="")
    up.add_argument("--days", type=int, default=14,
                    help="回看天数（默认 14；北单 SP 常滞后 ~3 天，留足余量）")
    up.add_argument("--all", action="store_true", help="回填全部缓存日期")
    up.add_argument("--dry-run", action="store_true")
    rp = sub.add_parser("report", help="打印账本")
    rp.add_argument("--role", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "update":
        res = update(args.role, as_of=args.as_of or None, lookback_days=args.days,
                     all_days=args.all, dry_run=args.dry_run)
        print(f"📒 账本更新 {res['role']} as_of={res['as_of']} "
              f"扫描 {res['days_scanned']} 天（{res['scanned']}）→ 新增 {res['added']} 场，"
              f"SP 未出 {res['skipped_no_sp']} 场，越界跳过 {res['skipped_future']} 场"
              f"{'（dry-run）' if args.dry_run else ''}")
        led = PoolLedger(ledger_path(args.role)).load()
        print(led.report())
    else:
        print(PoolLedger(ledger_path(args.role)).load().report())


if __name__ == "__main__":
    main()
