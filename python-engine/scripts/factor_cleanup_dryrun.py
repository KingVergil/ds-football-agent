#!/usr/bin/env python3
"""
因子归纳清理 —— 只读 dry-run 统计。

职责：把每只狗的 factor_memory.json 全部读一遍，按确定性的生命周期规则
给出每只因子的建议动作（晋升 / 退役 / 归档 / 休眠 / 唤醒 / 保持），
只输出报告，绝不写回 data/roles/*/memory/factor_memory.json 或 data/factors/*。

规则与现有系统对齐：
  * 低信息噪声退役：与 node_factor_review 的确定性退役一致
    （样本>=5 且 命中率落在 [0.35,0.65] 且 |平均单注回报|<0.15 → 退役）
  * 稳定负回报不是退役理由，而是反向信号，继续保留
  * 30 天零触发 → 休眠（大样本强因子除外）
  * 样本不足(decided<MIN_ACTIONABLE)且过龄 → 归档（噪声候选）
  * 样本成熟且方向明确 → testing 建议晋升 active

用法:
  python3 scripts/factor_cleanup_dryrun.py                     # 全部角色，控制台摘要
  python3 scripts/factor_cleanup_dryrun.py --roles 跟风狗       # 只看指定角色
  python3 scripts/factor_cleanup_dryrun.py --out-dir /tmp/x     # 同时写 JSON/CSV 报告
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.factor_select import factor_profile, volatility_lifecycle  # noqa: E402

ROLES_DIR = ROOT / "data" / "roles"

# ── 阈值（与 factor_select / node_factor_review 对齐，可命令行覆盖）──
MIN_ACTIONABLE = 10          # 晋升/进入可行动区的已决策样本下限
GC_DAYS = 21                 # 候选因子从首次出现到过龄归档的天数
DORMANT_DAYS = 30            # 零触发休眠天数
LOW_INFO_MIN_SAMPLES = 5     # 低信息噪声判定最小样本
LOW_INFO_AVG_RETURN = 0.15   # |平均单注回报|低于该值视为≈0
LOW_INFO_HIT_LO = 0.35
LOW_INFO_HIT_HI = 0.65
NOISE_W_RETURN = 0.10        # 加权回报噪声线
REVIVE_DECIDED = 20          # 休眠强因子唤醒的最小样本
REVIVE_HIT_RATE = 0.55
# 方向因子归一化排序：回报波动惩罚 + 样本惩罚。波动因子不走该分数。
SCORE_VOL_Z = 1.0
SCORE_SAMPLE_PENALTY = 0.5


def _parse_date(s: str) -> datetime | None:
    s = (s or "").strip()[:10]
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def _age_days(dt: datetime | None, as_of: datetime) -> int | None:
    if dt is None:
        return None
    return max((as_of.date() - dt.date()).days, 0)


def _avg_return(entry: dict) -> float:
    hist = entry.get("history") or []
    if not hist:
        return 0.0
    return sum(float(h.get("return_ratio", 0)) for h in hist) / len(hist)


def _direction(score: float | None, avg_ret: float) -> int:
    """优先看归一化 score；score 在噪声带内视为方向不明确，不再回退到平均回报。"""
    if score is not None:
        if score >= NOISE_W_RETURN:
            return 1
        if score <= -NOISE_W_RETURN:
            return -1
        return 0
    if abs(avg_ret) >= LOW_INFO_AVG_RETURN:
        return 1 if avg_ret > 0 else -1
    return 0


def _normalized_score(prof: dict | None, decided: int) -> float | None:
    """方向因子归一化评分：加权单注回报 - 波动惩罚 - 样本惩罚。

    score = w_return - z * volatility / sqrt(recent_n) - sample_penalty / sqrt(decided)
    """
    if not prof or prof.get("n", 0) <= 0:
        return None
    n = float(prof["n"])
    vol = float(prof.get("volatility") or 0)
    w_return = float(prof.get("w_return") or 0)
    sample_penalty = SCORE_SAMPLE_PENALTY / math.sqrt(max(decided, 1))
    return w_return - SCORE_VOL_Z * vol / math.sqrt(n) - sample_penalty


def classify(entry: dict, as_of: datetime, gc_days: int, min_actionable: int) -> tuple[str, str]:
    """返回 (action, detail)。action ∈ promote/retire/archive/dormant/revive/keep。"""
    factor_type = entry.get("type", "directional")
    if factor_type == "volatility":
        # 波动路径的验收口径与方向路径不同（看"覆盖值不值"，不看方向命中率）。
        # ⚠️ 隔离红线：只对**北单串关标注过 path=beidan** 的因子启用新规则；
        # 其它狗的 volatility 因子保持历来的 keep（行为与历史一致）。
        if str(entry.get("path") or "") == "beidan":
            life = volatility_lifecycle(entry, now=as_of)
            return (life["action"], f"[波动/北单] {life['reason']}")
        return ("keep", "波动因子（全包/覆盖腿）本轮不纳入方向因子清理")

    total = int(entry.get("total", 0) or 0)
    push = int(entry.get("push", 0) or 0)
    decided = total - push
    hit = float(entry.get("hit", 0) or 0)
    hit_rate = hit / decided if decided > 0 else 0.0
    status = entry.get("status", "active")
    first_seen = entry.get("first_seen", "")
    last_seen = entry.get("last_seen", "")
    prof = factor_profile(entry, now=as_of)
    w_return = prof.get("w_return") if prof else None
    score = _normalized_score(prof, decided) if prof else None
    strong_large = bool(prof.get("strong_large")) if prof else False
    age_first = _age_days(_parse_date(first_seen), as_of)
    age_last = _age_days(_parse_date(last_seen), as_of)

    if status == "retired":
        return ("keep", "已退役")

    if status == "dormant":
        if decided >= REVIVE_DECIDED and hit_rate > REVIVE_HIT_RATE:
            return ("revive", f"休眠但历史强（{decided} 样本 命中率 {hit_rate:.0%}），建议唤醒")
        return ("keep", "已休眠")

    # 30 天零触发 → 休眠（大样本强因子保护）
    if age_last is not None and age_last > DORMANT_DAYS and total > 0 and not strong_large:
        return ("dormant", f"最近触发已 {age_last} 天，建议休眠")

    # 候选未成熟
    if decided < min_actionable:
        clear_direction = decided >= 3 and _direction(score if score is not None else w_return, _avg_return(entry)) != 0
        if age_first is not None and age_first > gc_days and not clear_direction:
            if decided < 3:
                return ("archive", f"候选仅 {decided} 样本且已存在 {age_first} 天、样本过少无法确认方向，建议归档")
            return ("archive", f"候选样本 {decided}<{min_actionable} 且已存在 {age_first} 天、无清晰方向，建议归档")
        return ("observe", f"候选未成熟（已决策样本 {decided}/{min_actionable}）")

    # 成熟：先按低信息噪声判退役（与现有 node_factor_review 一致）
    avg_ret = _avg_return(entry)
    if decided >= LOW_INFO_MIN_SAMPLES and abs(avg_ret) < LOW_INFO_AVG_RETURN and LOW_INFO_HIT_LO <= hit_rate <= LOW_INFO_HIT_HI:
        return ("retire", f"成熟但命中 {hit_rate:.0%} 接近五五开、平均回报 {avg_ret:+.2f}≈0，无信息噪声")

    # 方向明确
    d = _direction(score if score is not None else w_return, avg_ret)
    action = "promote" if status == "testing" else "keep"
    if d > 0:
        direction = (
            f"正向信号（归一化 {score:+.2f} / 加权 {w_return:+.2f} / 平均 {avg_ret:+.2f}）"
            if score is not None
            else f"正向信号（平均 {avg_ret:+.2f}）"
        )
        return (action, direction)
    if d < 0:
        direction = (
            f"稳定负向/反买规避信号（归一化 {score:+.2f} / 加权 {w_return:+.2f} / 平均 {avg_ret:+.2f}）"
            if score is not None
            else f"稳定负向/反买规避信号（平均 {avg_ret:+.2f}）"
        )
        action = "promote" if status == "testing" else "keep"
        return (action, direction)

    return ("keep", f"成熟但归一化方向不明确（score 在噪声带，平均回报 {avg_ret:+.2f}），保持现状观察")


def iter_roles(roles_filter: list[str] | None):
    if not ROLES_DIR.exists():
        return
    for rd in sorted(ROLES_DIR.iterdir()):
        if not rd.is_dir():
            continue
        name = rd.name
        if "_sim" in name or name.startswith("__"):
            continue
        if roles_filter and name not in roles_filter:
            continue
        mp = rd / "memory" / "factor_memory.json"
        if not mp.exists():
            continue
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
            fp = data.get("factor_perf", {})
        except Exception as e:
            print(f"  ⚠️ 跳过 {name}: {e}")
            continue
        yield name, fp


def build_report(roles_filter: list[str] | None, as_of: datetime, gc_days: int, min_actionable: int) -> list[dict]:
    rows = []
    for role, fp in iter_roles(roles_filter):
        for fid, entry in fp.items():
            action, detail = classify(entry, as_of, gc_days, min_actionable)
            total = int(entry.get("total", 0) or 0)
            push = int(entry.get("push", 0) or 0)
            decided = total - push
            hit = float(entry.get("hit", 0) or 0)
            hit_rate = hit / decided if decided > 0 else 0.0
            prof = factor_profile(entry, now=as_of)
            score = _normalized_score(prof, decided) if prof else None
            rows.append({
                "role": role,
                "factor": fid,
                "type": entry.get("type", "directional"),
                "status": entry.get("status", "active"),
                "total": total,
                "decided": decided,
                "hit": hit,
                "hit_rate": round(hit_rate, 4),
                "avg_return": round(_avg_return(entry), 4),
                "w_return": round(prof["w_return"], 4) if prof else None,
                "recent_n": prof["n"] if prof else None,
                "volatility": round(prof["volatility"], 4) if prof else None,
                "score": round(score, 4) if score is not None else None,
                "shrunk_rate": round(prof["shrunk_rate"], 4) if prof else None,
                "first_seen": entry.get("first_seen", ""),
                "last_seen": entry.get("last_seen", ""),
                "desc": (entry.get("desc", "") or "")[:80],
                "action": action,
                "detail": detail,
            })
    return rows


def _fmt_action(a: str) -> str:
    return {
        "promote": "晋升",
        "retire": "退役",
        "archive": "归档",
        "dormant": "休眠",
        "revive": "唤醒",
        "observe": "观察",
        "keep": "保持",
    }.get(a, a)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roles", default="", help="逗号分隔的角色名，默认全部")
    ap.add_argument("--as-of", default="", help="评估基准日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--out-dir", default="", help="报告输出目录；留空则不落盘")
    ap.add_argument("--gc-days", type=int, default=GC_DAYS)
    ap.add_argument("--min-actionable", type=int, default=MIN_ACTIONABLE)
    args = ap.parse_args(argv)

    as_of = _parse_date(args.as_of) or datetime.now()
    roles_filter = [r.strip() for r in args.roles.split(",") if r.strip()] or None

    rows = build_report(roles_filter, as_of, args.gc_days, args.min_actionable)
    if not rows:
        print("没有可处理的因子。")
        return

    by_role: dict[str, Counter] = defaultdict(Counter)
    by_action: Counter = Counter()
    for r in rows:
        by_role[r["role"]][r["action"]] += 1
        by_action[r["action"]] += 1

    print(f"评估基准日期: {as_of.date().isoformat()}")
    print(f"角色数: {len(by_role)}  因子总数: {len(rows)}\n")

    print("== 每个角色的动作分布 ==")
    print(f"{'角色':<12} {'晋升':>4} {'退役':>4} {'归档':>4} {'休眠':>4} {'唤醒':>4} {'观察':>4} {'保持':>4}  合计")
    for role in sorted(by_role):
        c = by_role[role]
        total = sum(c.values())
        print(f"{role:<12} {c['promote']:>4} {c['retire']:>4} {c['archive']:>4} "
              f"{c['dormant']:>4} {c['revive']:>4} {c['observe']:>4} {c['keep']:>4} {total:>5}")
    total_all = sum(by_action.values())
    print(f"{'合计':<12} {by_action['promote']:>4} {by_action['retire']:>4} {by_action['archive']:>4} "
          f"{by_action['dormant']:>4} {by_action['revive']:>4} {by_action['observe']:>4} {by_action['keep']:>4} {total_all:>5}")

    # 把“需要动作/关注”的因子明细打出来；keep（已验证状态）不刷屏
    changed = [r for r in rows if r["action"] != "keep"]
    order = {"retire": 0, "archive": 1, "dormant": 2, "observe": 3, "revive": 4, "promote": 5}
    changed.sort(key=lambda r: (r["role"], order[r["action"]]))

    print(f"\n== 需要动作/关注的因子明细（共 {len(changed)} 个；已验证状态 keep 已省略）==\n")
    for r in changed:
        wr = f"{r['w_return']:+.2f}" if r["w_return"] is not None else "  —"
        ar = f"{r['avg_return']:+.2f}"
        print(f"[{_fmt_action(r['action'])}] {r['role']}/{r['factor']}")
        print(f"    状态:{r['status']}  样本:{r['decided']}(hit {r['hit']}) 命中率:{r['hit_rate']:.0%}  "
              f"均回报:{ar} 加权:{wr}")
        print(f"    {r['detail']}")
        if r["desc"]:
            print(f"    desc: {r['desc']}")

    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "factor_cleanup_report.json").write_text(
            json.dumps({"as_of": as_of.date().isoformat(), "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        import csv
        cols = ["role", "factor", "type", "status", "decided", "hit_rate", "avg_return",
                "w_return", "recent_n", "volatility", "score", "shrunk_rate",
                "first_seen", "last_seen", "action", "detail", "desc"]
        with (out / "factor_cleanup_report.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
