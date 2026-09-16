"""
北单回测取数：fet_txt 时间切片源（沙箱回放专用）

## 为什么需要它

回放（沙箱模型）跑历史足球日时，DataManager 原先只走线上 `compact-fet`。
线上接口 `fet_from_disk()` 的目录优先级是 `live → pass_2_hours → ... → pass_12_hours`，
而 `live/` 是**开赛前持续更新的终盘快照**（≈赛前 15 分钟）。于是回放里
「17:00 那一波分析」拿到的其实是赛前 15 分钟的数据——**前视泄漏**：
实盘在 17:00 时，20:30 开赛那场只有赛前 6 小时档的数据，次日 03:00 那场只有赛前 12 小时档。

本模块把「开赛前的哪一个快照档」显式化：

    访问时刻 T、开赛 K → gap = K - T
      gap ≤ 6h  → pass_6_hours   （线上 0~6h 档）
      gap ≤ 12h → pass_12_hours  （线上 6~12h 档）
      gap ≤ 24h → pass_1_day     （线上 12~24h 档）
      gap ≤ 0   → 无数据（该波访问时已开赛，实盘也不会拿它选腿）

线上生成规则（`online/utils.check_time_diff` + `auto_water_level`）：
每个阶段**只在比赛落入该 gap 区间时生成一次**，所以 `pass_6_hours/<lid>.txt`
的文件时间 ≈ 开赛前 6 小时、`pass_12_hours` ≈ 12 小时、`pass_1_day` ≈ 24 小时
（实测中位数 5.96h / 11.96h / 23.9h）。切片按 lid 命名，内容与线上 compact-fet
**同一份文本**（线上就是直接读这些文件），所以下游 `compact_fet_to_tags` /
`extract_odds` 无需任何改造。

## 访问时刻（波次）口径

固定习惯波次（北京时间，按**足球日起始日 D** 的星期）：

    周六 / 周日 ：16:30、20:30 两波
    周一 ~ 周五 ：22:30 一波

（可用 `DS_BACKTEST_WAVES_WEEKDAY` / `DS_BACKTEST_WAVES_WEEKEND` 覆盖，
  如 `DS_BACKTEST_WAVES_WEEKEND=16:30,20:30,23:30`；`DS_BACKTEST_WAVES` 覆盖全部。）

回放在一个足球日内**逐波跑**：每一波只分析「该波时刻尚未开赛」的场次，
所以周末会产出两张票（第一波：16:30 尚未开赛的所有场次；第二波：20:30 仍未开赛的场次），
同一场晚场在两个波次里拿到的档位可能不同（16:30 → pass_12_hours，20:30 → pass_6_hours），
这正是实盘两波各自看到的数据。

## 切片范围与索引

切片只覆盖 bc狗 的北单场次（默认 2026-07-01 ~ 2026-09-08），范围写在
`<root>/.bc_backtest_index.json`：`{lid: {kickoff, stages}}`。

- **在索引里** → 本模块是该场的**唯一**数据源：档位缺文件时只回退到**更旧**的档
  （绝不回退到更新的档，否则又是前视）；三档都没有 → 该场没有数据，直接不进分析。
- **不在索引里**（竞彩、其它日期）→ 返回 None，DataManager 按原逻辑读缓存/线上，行为不变。

## 启用方式

沙箱回放（`DS_ROLES_ROOT` 存在）时**自动启用**；线上分析/prefetch 不受影响。
`DS_BACKTEST_FET=0` 可强制关闭。切片根目录：`DS_FET_TXT_ROOT`，缺省取同级的
`deepseek_lota/data/runtime/fet_txt`。

## CLI

    python -m src.backtest_fet index  --start 2026-07-01 --end 2026-09-08   # 重建索引
    python -m src.backtest_fet check  --day 2026-08-22                      # 抽查某日取数档位
    python -m src.backtest_fet resolve --lid Lota4555190 --at 2026-08-22T16:30
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

_BJ = timezone(timedelta(hours=8))

# 线上阶段目录（按时间从新到旧）
STAGES: tuple[str, ...] = ("pass_6_hours", "pass_12_hours", "pass_1_day")

# gap（小时，上界）→ 线上阶段；与 online/utils.check_time_diff 一致
STAGE_MAX_GAP_HOURS: tuple[tuple[float, str], ...] = (
    (6.0, "pass_6_hours"),
    (12.0, "pass_12_hours"),
    (24.0, "pass_1_day"),
)

DEFAULT_WAVES_WEEKDAY: tuple[str, ...] = ("22:30",)
DEFAULT_WAVES_WEEKEND: tuple[str, ...] = ("16:30", "20:30")

INDEX_NAME = ".bc_backtest_index.json"
DEFAULT_RANGE = ("2026-07-01", "2026-09-08")

_KICKOFF_RE = re.compile(r"时间[：:]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


# ═══════════════════════════════════════════════
# 纯函数：足球日 / 波次 / 档位
# ═══════════════════════════════════════════════

def football_day_of(kickoff: datetime) -> date:
    """开赛时刻所属足球日（窗口 [D 12:01, D+1 12:00]）。"""
    if kickoff.time() >= time(12, 1):
        return kickoff.date()
    return kickoff.date() - timedelta(days=1)


def parse_hhmm(s: str) -> time:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s).strip())
    if not m:
        raise ValueError(f"波次格式应为 HH:MM，得到 {s!r}")
    return time(int(m.group(1)), int(m.group(2)))


def stage_for_gap(gap_hours: float) -> Optional[str]:
    """gap（小时）→ 线上阶段；gap ≤ 0 或 > 24h 返回 None。"""
    if gap_hours <= 0:
        return None
    for upper, stage in STAGE_MAX_GAP_HOURS:
        if gap_hours <= upper:
            return stage
    return None


def split_hhmm_list(raw: str) -> tuple[str, ...]:
    items = tuple(x.strip() for x in str(raw or "").split(",") if x.strip())
    return items


# ═══════════════════════════════════════════════
# 单场解析结果
# ═══════════════════════════════════════════════

@dataclass
class Resolution:
    lota_id: str
    kickoff: datetime
    access_time: datetime
    gap_hours: float
    stage: Optional[str]          # gap 对应的理论档位
    used_stage: str               # 实际读取的档位（可能回退到更旧档）
    path: Path
    fallback: bool = False        # used_stage != stage

    def as_dict(self) -> dict:
        return {
            "lota_id": self.lota_id,
            "kickoff": self.kickoff.strftime("%Y-%m-%d %H:%M:%S"),
            "access_time": self.access_time.strftime("%Y-%m-%d %H:%M"),
            "gap_hours": round(self.gap_hours, 3),
            "stage": self.stage,
            "used_stage": self.used_stage,
            "fallback": self.fallback,
            "path": str(self.path),
        }


# ═══════════════════════════════════════════════
# 数据源
# ═══════════════════════════════════════════════

class BacktestFetSource:
    """本地 fet_txt 切片 → compact-fet（回测专用，只读）。"""

    def __init__(
        self,
        root: Path | str,
        *,
        waves_weekday: tuple[str, ...] = DEFAULT_WAVES_WEEKDAY,
        waves_weekend: tuple[str, ...] = DEFAULT_WAVES_WEEKEND,
        index_name: str = INDEX_NAME,
        allow_newer_fallback: bool = False,
    ):
        self.root = Path(root)
        self.waves_weekday = tuple(waves_weekday or DEFAULT_WAVES_WEEKDAY)
        self.waves_weekend = tuple(waves_weekend or DEFAULT_WAVES_WEEKEND)
        self.index_path = self.root / index_name
        self.allow_newer_fallback = allow_newer_fallback
        self._index: dict[str, dict] = {}
        self._text_cache: dict[str, str] = {}
        self._res_cache: dict[tuple[str, str], Optional[Resolution]] = {}
        self.stats: dict[str, int] = {
            "hit": 0, "fallback": 0, "no_access": 0, "no_slice": 0,
            "out_of_scope": 0, "in_scope": 0,
        }
        self._examples: list[dict] = []
        self._load_index()

    # ── 索引 ──────────────────────────────────

    @property
    def available(self) -> bool:
        return bool(self._index)

    def _load_index(self) -> None:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        matches = raw.get("matches") if isinstance(raw, dict) else None
        if isinstance(matches, dict):
            self._index = matches
        elif isinstance(raw, dict) and raw:  # 兼容 {lid: {...}} 裸格式
            self._index = {k: v for k, v in raw.items() if isinstance(v, dict)}
        else:
            self._index = {}
        self.meta = raw if isinstance(raw, dict) else {}

    def in_scope(self, lota_id: str) -> bool:
        return lota_id in self._index

    def kickoff_of(self, lota_id: str) -> Optional[datetime]:
        entry = self._index.get(lota_id) or {}
        kt = entry.get("kickoff")
        if not kt:
            return None
        try:
            return datetime.strptime(str(kt)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def stages_of(self, lota_id: str) -> list[str]:
        entry = self._index.get(lota_id) or {}
        stages = entry.get("stages") or []
        return [s for s in STAGES if s in stages]

    def scope_items(self) -> list[tuple[str, str]]:
        """索引内全部 (lota_id, kickoff) 条目。"""
        return [(lid, str(e.get("kickoff") or "")) for lid, e in self._index.items()]

    # ── 波次 ──────────────────────────────────

    def waves(self, day: str | date) -> list[datetime]:
        """某足球日的固定访问时刻（北京时间 naive datetime，升序）。"""
        d = date.fromisoformat(day) if isinstance(day, str) else day
        raw = self.waves_weekend if d.weekday() >= 5 else self.waves_weekday
        out = [datetime.combine(d, parse_hhmm(x)) for x in raw]
        return sorted(out)

    def access_time_for(self, kickoff: datetime) -> Optional[datetime]:
        """缺省访问时刻：该场开赛前最后一次固定波次（单机自用时用）。"""
        waves = self.waves(football_day_of(kickoff))
        before = [w for w in waves if w < kickoff]
        return max(before) if before else None

    # ── 档位解析 ──────────────────────────────

    def resolve(
        self,
        lota_id: str,
        access_time: Optional[datetime] = None,
        kickoff: Optional[datetime] = None,
    ) -> Optional[Resolution]:
        """解析某场在该访问时刻该读哪个档位的切片；无数据返回 None。"""
        if not self.in_scope(lota_id):
            self.stats["out_of_scope"] += 1
            return None
        self.stats["in_scope"] += 1

        kick = kickoff or self.kickoff_of(lota_id)
        if kick is None:
            kick = self._parse_kickoff_from_file(lota_id)
        if kick is None:
            self.stats["no_slice"] += 1
            return None

        acc = access_time or _current_access_time() or self.access_time_for(kick)
        if acc is None:
            self.stats["no_access"] += 1
            return None
        key = (lota_id, acc.strftime("%Y-%m-%d %H:%M"))
        if key in self._res_cache:
            return self._res_cache[key]

        gap = (kick - acc).total_seconds() / 3600.0
        stage = stage_for_gap(gap)
        res: Optional[Resolution] = None
        if stage is None:
            self.stats["no_access" if gap <= 0 else "no_slice"] += 1
        else:
            order = self._fallback_order(stage)
            for cand in order:
                path = self.root / cand / f"{lota_id}.txt"
                if not path.exists():
                    continue
                res = Resolution(
                    lota_id=lota_id, kickoff=kick, access_time=acc, gap_hours=gap,
                    stage=stage, used_stage=cand, path=path, fallback=(cand != stage),
                )
                break
            if res is None:
                self.stats["no_slice"] += 1
            elif res.fallback:
                self.stats["fallback"] += 1
            else:
                self.stats["hit"] += 1

        self._res_cache[key] = res
        if res is None and len(self._examples) < 20:
            self._examples.append({
                "lota_id": lota_id,
                "kickoff": kick.strftime("%Y-%m-%d %H:%M"),
                "access": acc.strftime("%Y-%m-%d %H:%M"),
                "gap_hours": round(gap, 2),
                "stage": stage,
                "stages_have": self.stages_of(lota_id),
            })
        return res

    def _fallback_order(self, stage: str) -> list[str]:
        """目标档 → 依次回退到更旧的档（默认绝不回退到更新的档）。"""
        i = STAGES.index(stage)
        older = list(STAGES[i:])
        if self.allow_newer_fallback:
            newer = list(STAGES[:i])
            return older + newer
        return older

    def _parse_kickoff_from_file(self, lota_id: str) -> Optional[datetime]:
        for stage in STAGES:
            p = self.root / stage / f"{lota_id}.txt"
            if not p.exists():
                continue
            m = _KICKOFF_RE.search(self._read_text_cached(lota_id, p))
            if m:
                return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        return None

    # ── 读取 ──────────────────────────────────

    def _read_text_cached(self, lota_id: str, path: Path) -> str:
        key = f"{path}"
        if key not in self._text_cache:
            try:
                self._text_cache[key] = path.read_text(encoding="utf-8")
            except Exception:
                self._text_cache[key] = ""
        return self._text_cache[key]

    def text(
        self,
        lota_id: str,
        access_time: Optional[datetime] = None,
        kickoff: Optional[datetime] = None,
    ) -> Optional[str]:
        res = self.resolve(lota_id, access_time=access_time, kickoff=kickoff)
        if res is None:
            return None
        txt = self._read_text_cached(lota_id, res.path)
        return txt or None

    def compact_fet(
        self,
        lota_id: str,
        access_time: Optional[datetime] = None,
        kickoff: Optional[datetime] = None,
    ) -> Optional[dict]:
        """构造与线上 compact-fet 缓存同形状的 payload（下游无需改造）。"""
        res = self.resolve(lota_id, access_time=access_time, kickoff=kickoff)
        if res is None:
            return None
        txt = self._read_text_cached(lota_id, res.path)
        if not txt.strip():
            return None
        return {
            "lota_id": lota_id,
            "compact_fet": txt,
            "data": {"compact_fet": txt, "score": ""},
            "_cached_at": datetime.now().isoformat(),
            "_backtest_fet": res.as_dict(),
        }

    def sections(
        self,
        lota_id: str,
        access_time: Optional[datetime] = None,
        kickoff: Optional[datetime] = None,
    ) -> dict[str, str]:
        payload = self.compact_fet(lota_id, access_time=access_time, kickoff=kickoff)
        if not payload:
            return {}
        from .tools import compact_fet_to_tags
        return compact_fet_to_tags(lota_id, payload)

    # ── 汇报 ──────────────────────────────────

    def report(self) -> dict:
        return {
            "root": str(self.root),
            "index": str(self.index_path),
            "in_scope": len(self._index),
            "stats": dict(self.stats),
            "examples": list(self._examples),
        }

    def print_report(self, prefix: str = "[backtest-fet]") -> None:
        s = self.stats
        total = s["hit"] + s["fallback"] + s["no_access"] + s["no_slice"]
        print(
            f"{prefix} 切片源 {self.root} | 命中 {s['hit']} 场"
            f"，回退更旧档 {s['fallback']} 场，无可用档 {s['no_slice']} 场"
            f"，访问时刻已开赛 {s['no_access']} 场（共解析 {total} 次）",
            flush=True,
        )
        for ex in self._examples[:5]:
            print(f"{prefix}   ⚠️ {ex['lota_id']} 开赛 {ex['kickoff']} 访问 {ex['access']} "
                  f"gap {ex['gap_hours']}h 目标档 {ex['stage']} 已有 {ex['stages_have']}",
                  flush=True)


# ═══════════════════════════════════════════════
# 索引构建（从切片目录 + 比赛缓存反推）
# ═══════════════════════════════════════════════

def default_root() -> Path:
    """切片根目录：DS_FET_TXT_ROOT > 就近的 deepseek_lota/data/runtime/fet_txt。"""
    env = os.environ.get("DS_FET_TXT_ROOT")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve()
    fallback: Optional[Path] = None
    for up in here.parents[:5]:
        cand = up / "deepseek_lota" / "data" / "runtime" / "fet_txt"
        if not cand.is_dir():
            continue
        if fallback is None:
            fallback = cand
        if any((cand / s).is_dir() for s in STAGES):
            return cand
    if fallback is not None:
        return fallback
    return here.parents[3] / "deepseek_lota" / "data" / "runtime" / "fet_txt"


def collect_beidan_lids(start: str, end: str, matches_dir: Path) -> dict[str, str]:
    """从 python-engine/data/matches/<date>.json 收集窗口内北单场次 lid → match_time。"""
    out: dict[str, str] = {}
    d = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while d <= stop:
        p = matches_dir / f"{d.isoformat()}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = []
            for m in data or []:
                lid = m.get("lota_id")
                if lid and m.get("beidan_number"):
                    out.setdefault(lid, m.get("match_time", ""))
        d += timedelta(days=1)
    return out


def build_index(
    root: Path | str,
    matches_dir: Path | str,
    start: str = DEFAULT_RANGE[0],
    end: str = DEFAULT_RANGE[1],
    note: str = "",
) -> dict:
    """扫描切片目录，为范围内的北单场次建索引（缺切片的场次也记录，标记为空档）。"""
    root = Path(root)
    matches_dir = Path(matches_dir)
    lids = collect_beidan_lids(start, end, matches_dir)
    matches: dict[str, dict] = {}
    stage_count = {s: 0 for s in STAGES}
    for lid, mt in sorted(lids.items()):
        stages = [s for s in STAGES if (root / s / f"{lid}.txt").exists()]
        kickoff = ""
        for s in stages:
            m = _KICKOFF_RE.search((root / s / f"{lid}.txt").read_text(encoding="utf-8", errors="ignore"))
            if m:
                kickoff = m.group(1)
                break
        matches[lid] = {"kickoff": kickoff or mt, "stages": stages}
        for s in stages:
            stage_count[s] += 1
    payload = {
        "generated_at": datetime.now(_BJ).strftime("%Y-%m-%d %H:%M:%S"),
        "note": note or "bc狗北单 fet_txt 时间切片索引（pass_6_hours/pass_12_hours/pass_1_day）",
        "range": [start, end],
        "stages": stage_count,
        "matches": matches,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / INDEX_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return payload


# ═══════════════════════════════════════════════
# 模块级单例（DataManager 挂钩用）
# ═══════════════════════════════════════════════

_CURRENT: Optional[BacktestFetSource] = None
_ACCESS_TIME: Optional[datetime] = None
_AUTO_TRIED = False


def _current_access_time() -> Optional[datetime]:
    return _ACCESS_TIME


def set_access_time(ts: Optional[datetime]) -> None:
    """设置当前波次时刻（回放逐波调用）；None = 交回缺省推导。"""
    global _ACCESS_TIME
    _ACCESS_TIME = ts


def access_time() -> Optional[datetime]:
    return _ACCESS_TIME


def current() -> Optional[BacktestFetSource]:
    return _CURRENT


def enable(
    root: Path | str | None = None,
    *,
    waves_weekday: tuple[str, ...] | None = None,
    waves_weekend: tuple[str, ...] | None = None,
) -> Optional[BacktestFetSource]:
    """显式启用切片源；返回源对象（目录/索引缺失时返回 None）。"""
    global _CURRENT, _AUTO_TRIED
    _AUTO_TRIED = True
    r = Path(root) if root else default_root()
    if not r.is_dir():
        print(f"[backtest-fet] ⚠️ 切片目录不存在，回测取数保持原逻辑: {r}", flush=True)
        _CURRENT = None
        return None

    env_all = split_hhmm_list(os.environ.get("DS_BACKTEST_WAVES", ""))
    env_wd = split_hhmm_list(os.environ.get("DS_BACKTEST_WAVES_WEEKDAY", ""))
    env_we = split_hhmm_list(os.environ.get("DS_BACKTEST_WAVES_WEEKEND", ""))
    src = BacktestFetSource(
        r,
        waves_weekday=waves_weekday or env_wd or (env_all or DEFAULT_WAVES_WEEKDAY),
        waves_weekend=waves_weekend or env_we or (env_all or DEFAULT_WAVES_WEEKEND),
    )
    if not src.available:
        print(
            f"[backtest-fet] ⚠️ 切片索引缺失/为空，回测取数保持原逻辑: {src.index_path}"
            "（可执行 python -m src.backtest_fet index 重建）",
            flush=True,
        )
    _CURRENT = src
    return src


def disable() -> None:
    global _CURRENT, _AUTO_TRIED
    _CURRENT = None
    _AUTO_TRIED = False


def _maybe_auto_enable() -> None:
    """启用规则：

      DS_BACKTEST_FET=0/off/false/no → 强制关闭
      DS_BACKTEST_FET=1/on/true/yes  → 强制开启（手动离线脚本用）
      未设置 → 沙箱回放（DS_ROLES_ROOT 存在）时自动开启，线上不启用
    """
    global _AUTO_TRIED
    if _CURRENT is not None or _AUTO_TRIED:
        return
    _AUTO_TRIED = True
    flag = os.environ.get("DS_BACKTEST_FET", "").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return
    if flag in ("1", "on", "true", "yes"):
        enable(os.environ.get("DS_BACKTEST_FET_ROOT") or os.environ.get("DS_FET_TXT_ROOT"))
        return
    if not os.environ.get("DS_ROLES_ROOT"):
        return
    if os.environ.get("DS_BACKTEST_FET_ROOT"):
        enable(os.environ["DS_BACKTEST_FET_ROOT"])
    else:
        enable()


def active() -> bool:
    _maybe_auto_enable()
    return _CURRENT is not None and _CURRENT.available


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

def _cli(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="backtest_fet", description="北单 fet_txt 切片源")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="重建切片索引")
    pi.add_argument("--root", default=None)
    pi.add_argument("--start", default=DEFAULT_RANGE[0])
    pi.add_argument("--end", default=DEFAULT_RANGE[1])
    pi.add_argument("--matches-dir",
                    default=str(Path(__file__).resolve().parent.parent / "data" / "matches"))

    pc = sub.add_parser("check", help="抽查某日各波次取到的档位")
    pc.add_argument("--root", default=None)
    pc.add_argument("--day", required=True, help="足球日 YYYY-MM-DD")
    pc.add_argument("--limit", type=int, default=0)

    pr = sub.add_parser("resolve", help="解析单场")
    pr.add_argument("--root", default=None)
    pr.add_argument("--lid", required=True)
    pr.add_argument("--at", default=None, help="访问时刻 YYYY-MM-DDTHH:MM")

    pl = sub.add_parser("lids", help="打印范围内北单场次 lid（同步脚本用）")
    pl.add_argument("--start", default=DEFAULT_RANGE[0])
    pl.add_argument("--end", default=DEFAULT_RANGE[1])
    pl.add_argument("--matches-dir",
                    default=str(Path(__file__).resolve().parent.parent / "data" / "matches"))

    args = p.parse_args(argv)
    root = Path(args.root) if getattr(args, "root", None) else default_root()

    if args.cmd == "lids":
        lids = collect_beidan_lids(args.start, args.end, Path(args.matches_dir))
        for lid in sorted(lids):
            print(lid)
        print(f"# {len(lids)} 场北单（{args.start} ~ {args.end}）", file=sys.stderr)
        return 0

    if args.cmd == "index":
        payload = build_index(root, args.matches_dir, args.start, args.end)
        print(f"✅ 索引写入 {root / INDEX_NAME}")
        print(f"   范围 {payload['range'][0]} ~ {payload['range'][1]}"
              f" | 北单场次 {len(payload['matches'])}"
              f" | 阶段覆盖 {payload['stages']}")
        return 0

    src = BacktestFetSource(root)
    if not src.available:
        print(f"❌ 索引不可用: {src.index_path}")
        return 1

    if args.cmd == "resolve":
        at = datetime.fromisoformat(args.at).replace(tzinfo=None) if args.at else None
        res = src.resolve(args.lid, access_time=at)
        print(json.dumps(res.as_dict() if res else {"lota_id": args.lid, "result": None},
                         ensure_ascii=False, indent=2))
        return 0 if res else 1

    # check
    day = args.day
    waves = src.waves(day)
    print(f"足球日 {day}（{'周末' if date.fromisoformat(day).weekday() >= 5 else '工作日'}）"
          f" 波次: {[w.strftime('%H:%M') for w in waves]}")
    start, end = f"{day} 12:01:00", _football_end(day)
    lids = [lid for lid, kt in src.scope_items() if start <= kt[:19] <= end]
    if args.limit:
        lids = lids[: args.limit]
    for w in waves:
        src.stats.update({k: 0 for k in src.stats})
        by_stage: dict[str, int] = {}
        no_data = 0
        for lid in lids:
            res = src.resolve(lid, access_time=w)
            if res is None:
                no_data += 1
            else:
                by_stage[res.used_stage] = by_stage.get(res.used_stage, 0) + 1
        print(f"  {w.strftime('%H:%M')}: 该日北单 {len(lids)} 场 → {by_stage}；无数据 {no_data}")
    return 0


def _football_end(day: str) -> str:
    d = date.fromisoformat(day) + timedelta(days=1)
    return f"{d.isoformat()} 12:00:00"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
