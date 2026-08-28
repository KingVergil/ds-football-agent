#!/usr/bin/env python3
"""把矿出的亚盘/大小因子注入「梭哈2狗」复制角色（0801 起点回放用）。

口径：
  - 因子证据严格 < cutoff（默认 2026-08-01），剔除矿样里 8 月及之后的样本；
  - 过滤后 n>=min_n（默认 5）才保留，统计按过滤后历史重算；
  - 复制角色起点 = 库里最近的 0801 前状态（0724 角色 json + 0805 因子记忆，
    历史最大日期 0804；无精确 0731 快照，报告里注明）；
  - fac_*.json 写入 data/factors/（注册表 join 用），角色注册进 dogs.json（观察期）。
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ENGINE_DATA = Path("/Users/cjy/Desktop/code/ds_agents/python-engine/data")
DEFAULT_CAND_ASIAN = "/Users/cjy/Desktop/code/deepseek_lota/data/factor_mining_20260823/factor_candidates_asian.json"
DEFAULT_CAND_OU = "/Users/cjy/Desktop/code/deepseek_lota/data/factor_mining_20260823/factor_candidates_ou.json"

SLUG_CN = {
    "match-head": "联赛环境", "match-history": "历史交锋", "rank-info": "积分排名",
    "home-recent": "主队近期", "away-recent": "客队近期", "lineup": "阵容",
    "betfair-eu": "必发欧盘", "fair-odds": "公平盘", "discrete-odds": "离散指数",
    "betfair-buysell": "必发买卖盘", "eu-odds-pinnacle": "Pinnacle欧赔",
    "asian-handicap-crown": "皇冠亚盘", "asian-handicap-macau": "澳门亚盘",
    "asian-handicap-pinnacle": "Pinnacle亚盘", "over-under-crown": "皇冠大小球",
    "over-under-macau": "澳门大小球", "goal-bonus": "进球数据", "score-bonus": "比分数据",
}


def fac_id(name: str) -> str:
    return "fac_" + re.sub(r"\W+", "_", name)[:40]


def load_filtered(path: str, label: str, cutoff: str, min_n: int, replay_start: str) -> list[dict]:
    cands = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for c in cands:
        if c["kind"] == "slug":
            name = "矿%s:%s数据支持%s" % (label, SLUG_CN.get(c["slug"], c["slug"]), c.get("favors") or "?")
            slugs = [c["slug"]] if c.get("slug") else []
        else:
            name = "矿%s文本:%s" % (label, c["name"])
            slugs = (c.get("slugs") or [])[:5]
        hist = [h for h in c.get("history", []) if h.get("date", "") < cutoff]
        n = len(hist)
        if n < min_n:
            continue
        hit = sum(1 for h in hist if h.get("hit"))
        profit = round(sum(float(h.get("profit", 0)) for h in hist), 2)
        total_return = round(sum(float(h.get("return_ratio", 0)) for h in hist), 4)
        dates = sorted(h["date"] for h in hist)
        out.append({
            "name": name,
            "fac_id": fac_id(name),
            "slugs": slugs,
            "desc": name,
            "total": n, "hit": hit, "miss": n - hit, "push": 0,
            "profit": profit, "total_return": total_return,
            "first_seen": dates[0],
            # last_seen 设为回放起点：避免注入因子在回放早期因「14天零触发」被自动休眠
            "last_seen": replay_start,
            "status": "active", "aliases": [],
            "history": [
                {"lota_id": h.get("lota_id", ""), "date": h["date"], "hit": bool(h.get("hit")),
                 "profit": round(float(h.get("profit", 0)), 2),
                 "return_ratio": round(float(h.get("return_ratio", 0)), 4)}
                for h in hist
            ],
        })
    return out


def write_fac_files(factors: list[dict], factors_dir: Path, dry_run: bool) -> int:
    factors_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for f in factors:
        fp = factors_dir / (f["fac_id"] + ".json")
        content = "%s（矿出：n=%d, hit=%.1f%%, avg_return=%+.3f）" % (
            f["name"], f["total"], f["hit"] / f["total"] * 100 if f["total"] else 0,
            f["total_return"] / f["total"] if f["total"] else 0,
        )
        if not dry_run:
            fp.write_text(json.dumps({
                "id": f["fac_id"], "slugs": f["slugs"], "content": content,
                "updated_at": "2026-08-23T00:00:00",
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        written += 1
    return written


def select_curated(factors: list[dict], top_pos: int, top_neg: int, min_n: int) -> list[dict]:
    """按矿矿统计精选：n≥min_n 里取正回报 top_pos（顺向）+ 负回报 top_neg（反买/规避）。
    静态快照——候选文件更新后重跑本脚本即重新精选；运行期因子展示另按窗口回报动态重排。"""
    pool = [f for f in factors if f["total"] >= min_n]
    pos = sorted([f for f in pool if f["total_return"] / f["total"] > 0],
                 key=lambda x: -(x["total_return"] / x["total"]))[:top_pos]
    neg = sorted([f for f in pool if f["total_return"] / f["total"] < 0],
                 key=lambda x: x["total_return"] / x["total"])[:top_neg]
    return pos + neg


def register_dog(dogs_path: Path, name: str, dry_run: bool) -> None:
    dogs = []
    if dogs_path.exists():
        try:
            dogs = json.loads(dogs_path.read_text(encoding="utf-8"))
        except Exception:
            dogs = []
    if any(d.get("name") == name for d in dogs):
        return
    dogs.append({
        "name": name, "scope": "jc", "initial_capital": None,
        "alpha_mode": False, "limits": {"max_exposure_pct": 40},
        "enabled": False, "emoji": "⛏️", "c1": "#8b5cf6", "c2": "#f59e0b",
        "created_at": "2026-08-23",
    })
    if not dry_run:
        tmp = dogs_path.with_name(dogs_path.name + ".tmp")
        tmp.write_text(json.dumps(dogs, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dogs_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-asian", default=DEFAULT_CAND_ASIAN)
    ap.add_argument("--candidates-ou", default=DEFAULT_CAND_OU)
    ap.add_argument("--target", default="梭哈2狗_矿0801")
    ap.add_argument("--base-role", default="梭哈2狗")
    ap.add_argument("--base-json", default="梭哈2狗.json.bak.20260724")
    ap.add_argument("--base-factor-memory", default="factor_memory.json.bak.20260805_114540")
    ap.add_argument("--cutoff", default="2026-08-01")
    ap.add_argument("--replay-start", default="2026-08-01")
    ap.add_argument("--min-n", type=int, default=5)
    ap.add_argument("--select-min-n", type=int, default=30, help="精选最低样本（默认 30）")
    ap.add_argument("--top-pos", type=int, default=8, help="精选正回报 top N（默认 8）")
    ap.add_argument("--top-neg", type=int, default=4, help="精选负回报 top N（默认 4）")
    ap.add_argument("--all", action="store_true", help="注入全部过滤后因子（不精选）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    roles_dir = ENGINE_DATA / "roles"
    src = roles_dir / args.base_role
    dst = roles_dir / args.target
    if dst.exists() and not args.dry_run:
        print("目标角色已存在，先备份到 %s.bak" % dst)
        shutil.move(str(dst), str(dst) + ".bak")

    asian = load_filtered(args.candidates_asian, "亚盘", args.cutoff, args.min_n, args.replay_start)
    ou = load_filtered(args.candidates_ou, "大小", args.cutoff, args.min_n, args.replay_start)
    injected = asian + ou
    print("矿因子过滤（<%s, n>=%d）: 亚盘 %d → %d | 大小 %d → %d | 合计 %d" % (
        args.cutoff, args.min_n, len(json.loads(Path(args.candidates_asian).read_text(encoding="utf-8"))),
        len(asian), len(json.loads(Path(args.candidates_ou).read_text(encoding="utf-8"))), len(ou), len(injected)))
    if not args.all:
        injected = select_curated(injected, args.top_pos, args.top_neg, args.select_min_n)
        pos = [f for f in injected if f["total_return"] / f["total"] > 0]
        neg = [f for f in injected if f["total_return"] / f["total"] < 0]
        print("精选（n>=%d, 正 top%d + 负 top%d）: 合计 %d" % (
            args.select_min_n, args.top_pos, args.top_neg, len(injected)))
        for f in pos:
            print("  [正] %+.3f n=%d %s" % (f["total_return"] / f["total"], f["total"], f["name"]))
        for f in neg:
            print("  [负] %+.3f n=%d %s" % (f["total_return"] / f["total"], f["total"], f["name"]))

    if args.dry_run:
        print("[dry-run] 不写任何文件。")
        return

    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("history", "__pycache__", "*.bak", "*.bak.*"),
    )
    # 起点角色不带源角色历史检查点（未来日期），回放会从 0801 完整日管线开始
    (dst / "history").mkdir(exist_ok=True)
    # 角色 json：改名 + 更新 updated_at
    # 起点状态 = 库里最近的 0801 前角色快照（默认 梭哈2狗.json.bak.20260724），
    # 不是当前线上 json（含 8 月订单/资金，会泄漏到回放起点）。
    src_json = src / args.base_json
    rj = json.loads(src_json.read_text(encoding="utf-8"))
    rj["name"] = args.target
    rj["updated_at"] = "2026-08-23T00:00:00"
    (dst / (args.target + ".json")).write_text(json.dumps(rj, ensure_ascii=False, indent=2), encoding="utf-8")
    # copytree 带过来的线上 json（旧名）删掉
    old_json = dst / (args.base_role + ".json")
    if old_json.exists():
        old_json.unlink()

    # 因子记忆：基线（0805） + 注入矿因子
    base_fm = json.loads((src / "memory" / args.base_factor_memory).read_text(encoding="utf-8"))
    base_fm = dict(base_fm)
    base_fm.setdefault("factor_perf", {})
    for f in injected:
        base_fm["factor_perf"][f["name"]] = {
            k: f[k] for k in ("total", "hit", "miss", "push", "profit", "total_return",
                              "status", "desc", "first_seen", "last_seen", "history",
                              "aliases", "fac_id", "slugs")
        }
    base_fm["updated_at"] = "2026-08-23T00:00:00"
    (dst / "memory" / "factor_memory.json").write_text(
        json.dumps(base_fm, ensure_ascii=False, indent=2), encoding="utf-8")

    # 反思记忆：优先当前文件；只有显式指定旧基座（--base-json 为 .bak）时才用旧备份
    ref_src = src / "memory" / "reflection_memory.json"
    if ".bak" in args.base_json:
        ref_src = src / "memory" / "reflection_memory.json.bak.reflection_samples"
    if ref_src.exists():
        shutil.copyfile(ref_src, dst / "memory" / "reflection_memory.json")

    # fac 注册表 + 狗注册表
    n_fac = write_fac_files(injected, ENGINE_DATA / "factors", dry_run=False)
    register_dog(ENGINE_DATA / "dogs.json", args.target, dry_run=False)

    print("已创建:", dst)
    print("因子注入:", len(injected), "| 新增 fac 文件:", n_fac)
    print("角色注册: dogs.json += %s（观察期 enabled=false）" % args.target)


if __name__ == "__main__":
    main()
