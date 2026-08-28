#!/usr/bin/env python3
"""dsh ↔ python-engine 统一桥接入口（NDJSON 协议，仿 place_orders 桥）。

用法（dsh 侧固定 argv 直接 spawn，不拼 shell）:
    python3 -m src.bridge

stdin: 单行 JSON 请求
    {
      "func": "prepare|analyze|settle|factor-induction|factor-review|status|refresh|reset|tavern",
      "dog": "梭哈2狗",                 # analyze/settle/induction/review/status/refresh/reset 必填
      "day": "YYYY-MM-DD",              # prepare/analyze/settle/refresh 必填；factor-review 可用 end/start
      "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
      "opts": {
        "mode": "live|replay",          # prepare
        "jingcai_only": true,           # prepare/analyze
        "prefetched": true,             # analyze：数据已由 prepare 预取
        "live": false,                  # analyze：live 语义（刷新订单 + 严格数据）
        "start_date": "",               # factor-review 窗口起点（空=自动 7 天）
        "user_notes": "",               # factor-review 用户调整意见
        "capital": 10000,               # reset
        "reset_mode": "soft|full"       # reset
      }
    }

stdout: NDJSON（每行一个 JSON 对象）:
    {"type":"progress","phase":"...","done":n,"total":n,"detail":"..."}
    {"type":"result","func":"...","data":{...}}
    {"type":"error","func":"...","message":"..."}

stderr: 诊断/内部 print（一律重定向到这里，stdout 只出 NDJSON）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

# 模块加载时的真实 stdout —— redirect_stdout 只替换 sys.stdout，
# NDJSON 事件必须走这个保存的引用，保证永远不被内部 print 污染。
_OUT = sys.stdout


class BridgeError(Exception):
    """可预期错误：转成 error 事件，不打印 traceback。"""


def _emit(obj: dict) -> None:
    _OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _OUT.flush()


def _progress(phase: str, done: int = None, total: int = None, detail: str = "") -> None:
    ev = {"type": "progress", "phase": phase}
    if done is not None:
        ev["done"] = done
    if total is not None:
        ev["total"] = total
    if detail:
        ev["detail"] = detail
    _emit(ev)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_date(s) -> bool:
    if not isinstance(s, str) or not _DATE_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _need(req: dict, key: str, label: str = None) -> str:
    v = req.get(key)
    if not v:
        raise BridgeError(f"缺少参数 {label or key}")
    return str(v)


def _role_dir() -> Path:
    from src.role_registry import ROLES_DIR
    return ROLES_DIR


def _is_parlay_dog(dog: str) -> bool:
    """串关狗判定：角色目录存在 parlay.json（北单 8串1 / 7串1 双狗）。"""
    return (_role_dir() / dog / "parlay.json").exists()


def _ensure_dog(dog: str) -> None:
    """狗名必须真实存在（角色 json 存在），防止拼错狗名被 _ensure_role 静默新建。"""
    if not dog or not dog.strip():
        raise BridgeError("缺少参数 dog")
    path = _role_dir() / dog / f"{dog}.json"
    if not path.exists() and os.environ.get("DS_ROLES_ROOT"):
        # 沙箱回放：role_root 是单狗平铺目录，角色 json 直接位于根下
        path = _role_dir() / f"{dog}.json"
    if not path.exists():
        raise BridgeError(f"角色不存在: {dog}（roles/{dog}/{dog}.json 缺失，先 role-sync）")


def _agent(dog: str):
    from src.agent import Agent
    return Agent(user=dog)


def _role_of(dog: str):
    """取已加载的角色对象（调用方需先 _ensure_role）。"""
    from src.agent import _rt
    rt = _rt({"user": dog})
    if rt.role is None:
        raise BridgeError(f"角色加载失败: {dog}")
    return rt.role


def _provider(agent) -> None:
    from src.providers.deepseek import DeepSeekProvider
    agent.set_provider(DeepSeekProvider())


def _factor_summary(dog: str) -> dict:
    """从因子记忆汇总状态分布（active/retired/dormant），供状态卡片与退役建议用。"""
    from src.role_registry import ROLES_DIR
    path = ROLES_DIR / dog / "memory" / "factor_memory.json"
    if not path.exists() and os.environ.get("DS_ROLES_ROOT"):
        path = ROLES_DIR / "memory" / "factor_memory.json"
    counts = {"active": 0, "retired": 0, "dormant": 0, "testing": 0, "other": 0}
    names = {"active": [], "retired": [], "dormant": [], "testing": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        fp = data.get("factor_perf") or {}
        for fid, s in fp.items():
            st = s.get("status", "active") if isinstance(s, dict) else "active"
            if st not in counts:
                st = "other"
            counts[st] += 1
            if st in names:
                names[st].append({
                    "id": fid,
                    "status": st,
                    "total": s.get("total", 0),
                    "profit": round(s.get("profit", 0), 2),
                })
    except Exception:
        pass
    counts["total"] = sum(counts.values())
    for k in names:
        names[k].sort(key=lambda x: x["profit"])
    return {"counts": counts, "by_status": names}


def _pnl_trend(dog: str, start: str = "", end: str = "") -> list[dict]:
    """资金曲线截取 [start, end] 窗口（空 start=全量），回放/退役建议的 PnL 上下文。"""
    agent = _agent(dog)
    agent._ensure_role()
    role = _role_of(dog)
    hist = role.get_capital_history()
    out = []
    for h in hist:
        d = str(h.get("date", ""))
        if start and d < start:
            continue
        if end and d > end:
            continue
        out.append({"date": d, "capital": h.get("capital"), "pnl": h.get("pnl")})
    return out


def _last_review_md(dog: str, tail_chars: int = 6000) -> dict:
    """最近一次 factor_review 的 session 文件路径 + 尾部内容（建议草稿上下文）。"""
    from src.session_logger import SESSIONS_DIR
    files = sorted((SESSIONS_DIR / dog).glob("*factor_review_*.md")) if (SESSIONS_DIR / dog).exists() else []
    if not files:
        return {"file": "", "date": "", "tail": ""}
    p = files[-1]
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        text = ""
    # 文件名形如 <ts>_factor_review_<end_date>.md → 取最后一个日期片段
    m = re.search(r"factor_review_(\d{4}-\d{2}-\d{2})\.md$", p.name)
    return {"file": str(p), "date": m.group(1) if m else "", "tail": text[-tail_chars:]}


def _order_views(agent, orders: list[dict]) -> list[dict]:
    out = []
    for o in orders or []:
        m = agent._get_match_info(o.get("lota_id", ""))
        view = {
            "lota_id": o.get("lota_id", ""),
            "match": f"{m.get('home') or '?'} vs {m.get('away') or '?'}",
            "league": m.get("league", ""),
            "bet_type": o.get("bet_type", ""),
            "pick": o.get("pick", ""),
            "handicap": o.get("handicap"),
            "odds": o.get("odds"),
            "bet_size": o.get("bet_size"),
            "reason": (o.get("reason") or "")[:120],
            "skip": bool(o.get("skip")),
            "created_at": o.get("created_at", ""),
        }
        if o.get("settled_at"):
            view.update({"settled_at": o.get("settled_at"), "hit": o.get("hit"), "profit": o.get("profit"), "return_amount": o.get("return_amount")})
        out.append(view)
    return out


def _parlay_order_views(orders: list[dict]) -> list[dict]:
    """北单串关订单视图：以 slip_id 聚合为一张票，带完整腿（goal_line+胜平负+赔率）。"""
    from collections import OrderedDict
    slips: dict[str, dict] = OrderedDict()
    for o in orders or []:
        sid = o.get("slip_id") or o.get("id") or ""
        if not sid:
            continue
        s = slips.setdefault(sid, {
            "slip_id": sid,
            "ticket": o.get("slip_type") or o.get("ticket_type") or "",
            "bet_type": o.get("bet_type", ""),
            "combos_count": o.get("combos_count") or 0,
            "ticket_legs": o.get("ticket_legs") or [],
            "legs": o.get("legs") or [],
            "total_stake": 0.0,
            "orders": [],
        })
        s["total_stake"] += float(o.get("bet_size") or 0)
        s["orders"].append(o)
    return list(slips.values())


# ── func 实现 ──────────────────────────────────────────────

def _do_prepare(req: dict) -> dict:
    day = _need(req, "day")
    if not _valid_date(day):
        raise BridgeError(f"日期格式错误: {day}")
    opts = req.get("opts") or {}
    mode = opts.get("mode", "live")
    if mode not in ("live", "replay"):
        raise BridgeError(f"prepare mode 必须是 live/replay: {mode!r}")
    jingcai_only = bool(opts.get("jingcai_only", True))
    beidan_only = bool(opts.get("beidan_only", False))
    if beidan_only:
        jingcai_only = False
    beidan_only = bool(opts.get("beidan_only", False))
    if beidan_only:
        jingcai_only = False

    from src.environment import get_football_day, football_day_calendar_dates
    from src.data_manager import DataManager
    from src.tools import compact_fet_to_tags, save_tagged_sections

    d = date.fromisoformat(day)
    window_start, window_end = get_football_day(d)
    cal_dates = football_day_calendar_dates(d)
    dm = DataManager()
    if mode == "live":
        dm.set_live_mode(True)

    all_matches = []
    fetched_dates = []
    for i, cd in enumerate(cal_dates):
        _progress("拉取比赛缓存", done=i, total=len(cal_dates), detail=cd)
        ms = dm.get_cached_matches(cd, lottery_type="all")
        if mode == "live" or not ms:
            ms = dm.refresh_matches_cache(cd, with_jc_odds=jingcai_only) or []
            fetched_dates.append(cd)
        all_matches += ms

    candidates = [
        m for m in all_matches
        if window_start <= str(m.get("match_time", ""))[:16] <= window_end
        and m.get("lota_id")
        and m.get("home_name", "?") not in ("", "?")
        and m.get("away_name", "?") not in ("", "?")
        and (not jingcai_only or m.get("jingcai_number"))
        and (not beidan_only or m.get("beidan_number"))
    ]

    # 回放多窗口：返回去重后的候选列表（lota_id + match_time），由 harness 按智能窗口分批分析
    seen_lids: set[str] = set()
    matches_view = []
    for m in sorted(candidates, key=lambda x: str(x.get("match_time", ""))):
        lid = m.get("lota_id")
        if lid in seen_lids:
            continue
        seen_lids.add(lid)
        matches_view.append({"lota_id": lid, "match_time": str(m.get("match_time", ""))[:16]})

    ok = fail = 0
    warnings = []
    for m in candidates:
        lid = m["lota_id"]
        data = dm.get_compact_fet(lid)
        if not data:
            fail += 1
            warnings.append(f"{lid} {m.get('home_name', '?')} vs {m.get('away_name', '?')} compact-fet 缺失")
            continue
        sections = compact_fet_to_tags(lid, data)
        if sections:
            save_tagged_sections(lid, sections)
        ok += 1
    dm.set_live_mode(False)

    if mode == "live" and fail:
        warnings.append(f"live 预取失败 {fail} 场（LLM 看不到对应赔率段）")
    if not candidates:
        warnings.append("窗口内无竞彩比赛（可能缓存缺失或当天无竞彩场次）")

    return {
        "day": day,
        "mode": mode,
        "jingcai_only": jingcai_only,
        "window": f"{window_start[:10]} 12:01 → {(date.fromisoformat(day) + timedelta(days=1)).isoformat()} 12:00",
        "calendar_dates": cal_dates,
        "candidates": len(candidates),
        "matches": matches_view,
        "prefetched_ok": ok,
        "failed": fail,
        "matches_fetched": fetched_dates,
        "features_prefetched": ok,
        "warnings": warnings,
    }


def _do_prepare_range(req: dict) -> dict:
    """回放前全量预取（非因子数据）：一次范围拉取写满 start~end 足球日比赛缓存 +
    预取特征/标签。之后逐日 prepare 只读已备好的缓存，保证回放全程数据一致，
    不会出现"跑到某天缓存缺失→临时拉取返回空→0 场"的问题。"""
    start = _need(req, "start")
    end = _need(req, "end")
    if not _valid_date(start) or not _valid_date(end) or start > end:
        raise BridgeError(f"日期范围无效: {start}~{end}")
    opts = req.get("opts") or {}
    jingcai_only = bool(opts.get("jingcai_only", True))

    from src.data_manager import DataManager
    from src.tools import compact_fet_to_tags, save_tagged_sections
    from src.environment import get_football_day

    dm = DataManager()
    _progress("范围拉取比赛缓存", detail=f"{start} ~ {end}")
    # 与 live prepare 同口径：按日历日逐个强制刷新（避免 refresh_matches_range 的
    # 足球日分桶与逐日读取的日历键控不一致导致写错文件）
    written = {}
    cd = date.fromisoformat(start)
    end_cd = date.fromisoformat(end) + timedelta(days=1)
    while cd <= end_cd:
        key = cd.isoformat()
        ms = dm.refresh_matches_cache(key, with_jc_odds=jingcai_only) or []
        written[key] = len(ms)
        cd += timedelta(days=1)

    days = []
    total_candidates = total_fail = 0
    warnings = []
    d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    while d <= end_d:
        day = d.isoformat()
        window_start, window_end = get_football_day(d)
        all_matches = []
        for cd in (day, (d + timedelta(days=1)).isoformat()):
            all_matches += dm.get_cached_matches(cd, lottery_type="all")
        candidates = [
            m for m in all_matches
            if window_start <= str(m.get("match_time", ""))[:16] <= window_end
            and m.get("lota_id")
            and m.get("home_name", "?") not in ("", "?")
        and m.get("away_name", "?") not in ("", "?")
        and (not jingcai_only or m.get("jingcai_number"))
        and (not beidan_only or m.get("beidan_number"))
    ]
        ok = fail = 0
        for m in candidates:
            lid = m["lota_id"]
            data = dm.get_compact_fet(lid)
            if not data:
                fail += 1
                warnings.append(f"{day} {lid} compact-fet 缺失")
                continue
            sections = compact_fet_to_tags(lid, data)
            if sections:
                save_tagged_sections(lid, sections)
            ok += 1
        total_candidates += len(candidates)
        total_fail += fail
        days.append({"day": day, "candidates": len(candidates),
                     "prefetched_ok": ok, "failed": fail})
        d += timedelta(days=1)

    return {
        "start": start, "end": end, "jingcai_only": jingcai_only,
        "range_written": written, "days": days,
        "total_candidates": total_candidates, "total_failed": total_fail,
        "warnings": warnings[:20],
    }


def _do_analyze(req: dict) -> dict:
    dog = _need(req, "dog")
    day = _need(req, "day")
    if not _valid_date(day):
        raise BridgeError(f"日期格式错误: {day}")
    _ensure_dog(dog)
    opts = req.get("opts") or {}
    live = bool(opts.get("live", False))
    prefetched = bool(opts.get("prefetched", False))
    jingcai_only = bool(opts.get("jingcai_only", True))
    beidan_only = bool(opts.get("beidan_only", False))
    if beidan_only:
        jingcai_only = False
    skip_llm = bool(opts.get("skip_llm", False))
    window = opts.get("window")
    if window is not None and not isinstance(window, dict):
        raise BridgeError(f"window 必须是对象: {window!r}")
    if window is not None and not window.get("match_ids"):
        raise BridgeError("window 需要非空 match_ids")

    if _is_parlay_dog(dog):
        from src.beidan_parlay_dog import BeidanParlayDog
        pdog = BeidanParlayDog(user=dog)
        _progress("分析中（北单串关 · LLM 决策）" if not skip_llm else "分析中（北单串关 · 演示模式）",
                  detail=f"{dog} {day}")
        result = pdog.analyze(day, live=live, use_llm=not skip_llm)
        return {
            "user": dog,
            "date": day,
            "parlay": True,
            "matches_count": result.get("matches_count", 0),
            "legs_selected": result.get("legs_selected", 0),
            "tickets": result.get("tickets", []),
            "llm_used": result.get("llm_used", False),
            "orders": _parlay_order_views(result.get("orders", [])),
            "placed": result.get("placed", 0),
            "capital": pdog._get_capital(),
            "session_path": result.get("session_path", ""),
            "llm_skipped": skip_llm,
        }

    agent = _agent(dog)
    # 回放（live=false）同样需要 LLM 决策：node_call_llm 在 rt.provider 为空时直接返回空响应
    if not skip_llm:
        _provider(agent)
    _progress("分析中（LLM 决策）" if not skip_llm else "分析中（演示模式·跳过 LLM）", detail=f"{dog} {day}")
    result = agent.analyze(day, live=live, jingcai_only=jingcai_only,
                           prefetched=prefetched, window=window, beidan_only=beidan_only)
    _progress("分析写盘完成", detail=f"{dog} {day}")
    return {
        "user": dog,
        "date": day,
        "matches_count": result.get("matches_count", 0),
        "prompt_tokens": result.get("prompt_tokens", 0),
        "orders": _order_views(agent, result.get("orders", [])),
        "placed": result.get("placed", 0),
        "capital": agent._get_capital(),
        "session_path": result.get("session_path", ""),
        "llm_response": (result.get("llm_response") or "")[:800],
        "llm_skipped": skip_llm,
    }


def _do_settle(req: dict) -> dict:
    dog = _need(req, "dog")
    day = _need(req, "day")
    if not _valid_date(day):
        raise BridgeError(f"日期格式错误: {day}")
    _ensure_dog(dog)
    opts = req.get("opts") or {}

    if _is_parlay_dog(dog):
        from src.beidan_parlay_dog import BeidanParlayDog
        pdog = BeidanParlayDog(user=dog)
        _progress("结算中（北单串关）", detail=f"{dog} {day}")
        s = pdog.settle(day, reflect=not bool(opts.get("skip_llm")))
        return {
            "user": dog,
            "day": day,
            "parlay": True,
            "settlement": s,
            "capital": pdog._get_capital(),
            "stats": _role_of(dog).stats(),
        }

    agent = _agent(dog)
    # 结算后反思（归因 → 更新因子表现与 last_seen 有效期）需要 LLM：
    # 与 _do_analyze 对齐，否则 node_reflect 因 rt.provider 为空被跳过，
    # 回放/看板桥接结算从不更新因子有效期（2026-08-25 修复）。
    if not opts.get("skip_llm"):
        _provider(agent)
    _progress("结算中", detail=f"{dog} {day}")
    s = agent.settle(day, jingcai_only=bool(opts.get("jingcai_only", False)))
    # live 狗结算后落「结算后/因子前」检查点（沙箱回放复制用；沙箱内由 replay.js 管检查点）
    if not os.environ.get("DS_ROLES_ROOT"):
        _write_pre_factor_checkpoint(dog, day)
    return {
        "user": dog,
        "day": day,
        "settlement": s,
        "capital": agent._get_capital(),
        "stats": _role_of(dog).stats(),
    }


def _write_pre_factor_checkpoint(dog: str, day: str) -> None:
    """把角色目录复制到 roles/<狗>/history/<day>__pre-factor/（结算后、因子归纳前）。"""
    from src.role_registry import ROLES_DIR
    src = ROLES_DIR / dog
    if not src.exists():
        return
    dest = src / "history" / f"{day}__pre-factor"
    try:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("history"))
    except Exception as e:  # noqa: BLE001 —— 检查点失败不阻断结算
        print(f"  ⚠️ pre-factor 检查点写入失败: {e}")


def _do_induction(req: dict) -> dict:
    day = req.get("day") or ""
    from src.factor_induction import main as induction_main
    opts = req.get("opts") or {}
    roles_arg = opts.get("roles")
    if roles_arg:
        # alpha barrier：一次调用传多只 alpha 狗（逗号分隔），触发跨狗统一归纳
        roles_arg = ",".join(str(r).strip() for r in roles_arg if str(r).strip()) if isinstance(roles_arg, (list, tuple)) else str(roles_arg).strip()
        for r in roles_arg.split(","):
            if r:
                _ensure_dog(r)
        dog = roles_arg
    else:
        dog = _need(req, "dog")
        _ensure_dog(dog)
    _progress("因子归纳中", detail=dog)
    summary = induction_main(["--roles", dog])
    summary = summary or {"merged": 0, "llm_calls": 0, "fac_created": 0, "scopes": 0}
    return {"user": dog, "day": day, "summary": summary, "factors": _factor_summary(dog)}


def _do_review(req: dict) -> dict:
    dog = _need(req, "dog")
    _ensure_dog(dog)
    opts = req.get("opts") or {}
    end_date = req.get("end") or req.get("day") or ""
    if not _valid_date(end_date):
        raise BridgeError(f"factor-review 需要 end 日期: {end_date!r}")
    start_date = str(req.get("start") or opts.get("start_date") or "")
    if start_date and not _valid_date(start_date):
        raise BridgeError(f"start_date 格式错误: {start_date!r}")
    if start_date and start_date > end_date:
        raise BridgeError(f"start_date({start_date}) 不能晚于 end_date({end_date})")
    user_notes = str(opts.get("user_notes", "") or "")
    skip_llm = bool(opts.get("skip_llm", False))

    agent = _agent(dog)
    if not skip_llm:
        _provider(agent)
    before = _factor_summary(dog)
    _progress("因子退役评估中（LLM）" if not skip_llm else "因子退役评估中（演示模式·跳过 LLM）", detail=f"{dog} 至 {end_date}")
    r = agent.factor_review(end_date, start_date=start_date, user_notes=user_notes)
    _progress("退役评估完成", detail=dog)
    after = _factor_summary(dog)
    review_md = _last_review_md(dog)
    # 本周期状态变化（退役/休眠/恢复），供回放建议草稿与结果卡片
    before_status = {f["id"]: f for f in before.get("by_status", {}).get("active", [])}
    for st in ("retired", "dormant", "testing"):
        for f in before.get("by_status", {}).get(st, []):
            before_status[f["id"]] = f
    cycle_changes = []
    for st in ("active", "retired", "dormant", "testing"):
        for f in after.get("by_status", {}).get(st, []):
            prev = before_status.get(f["id"])
            if prev is None:
                cycle_changes.append({"id": f["id"], "from": "(new)", "to": st})
            elif prev["status"] != st:
                cycle_changes.append({"id": f["id"], "from": prev["status"], "to": st})
    return {
        **r,
        "factor_summary": _factor_summary(dog),
        "pnl_trend": _pnl_trend(dog, start_date, end_date),
        "cycle_changes": cycle_changes,
        "review_file": review_md["file"],
        "review_date": review_md["date"],
        "review_md_tail": review_md["tail"],
        "llm_skipped": skip_llm,
    }


def _do_status(req: dict) -> dict:
    dog = _need(req, "dog")
    _ensure_dog(dog)
    agent = _agent(dog)
    agent._ensure_role()
    role = _role_of(dog)
    stats = role.stats()
    hist = role.get_capital_history()
    review = _last_review_md(dog, tail_chars=0)
    factors = _factor_summary(dog)
    pending = [o for o in role.get_orders() if not o.get("settled_at")]
    return {
        "user": dog,
        "capital": role.capital,
        "initial_capital": role.initial_capital,
        "pnl": round(role.pnl(), 2),
        "stats": stats,
        "pending_orders": _order_views(agent, pending[:20]),
        "pending_count": len(pending),
        "factors": factors,
        "capital_history": hist,
        "last_factor_review": review["date"],
        "alpha_mode": bool(role.alpha_mode),
        "scope": role.scope,
        "enabled": bool(role.enabled),
        "status": role.status,
    }


def _do_refresh(req: dict) -> dict:
    dog = _need(req, "dog")
    day = _need(req, "day")
    if not _valid_date(day):
        raise BridgeError(f"日期格式错误: {day}")
    _ensure_dog(dog)
    agent = _agent(dog)
    _progress("刷新订单组", detail=f"{dog} {day}")
    r = agent.refresh_orders(day)
    return {"user": dog, **r}


def _do_reset(req: dict) -> dict:
    dog = _need(req, "dog")
    _ensure_dog(dog)
    opts = req.get("opts") or {}
    mode = opts.get("reset_mode", "soft")
    if mode not in ("soft", "full"):
        raise BridgeError(f"reset_mode 必须是 soft/full: {mode!r}")
    capital = opts.get("capital")
    if capital is not None:
        capital = float(capital)
    agent = _agent(dog)
    agent._ensure_role()
    role = _role_of(dog)
    if mode == "full":
        role.reset(capital=capital)
    else:
        role.soft_reset(capital=capital)
    return {
        "user": dog,
        "mode": mode,
        "capital": role.capital,
        "initial_capital": role.initial_capital,
    }


def _do_tavern(req: dict) -> dict:
    """LLM 多狗酒馆（纯聊天模式）：老板主持，各狗按人设聊天/互怼。

    ⚠️ 酒馆禁止启动分析/出单：本函数绝不调用 analyze、绝不写任何订单。
    客人要单时各狗只按人设回应（可调侃、可劝客人去斗狗场点「⚡ 分析」）。

    opts: {
      day: "YYYY-MM-DD",
      dogs: [{name, tagline}],        // 出场顺序
      slate: [{home, away, league, time}],
      picks: {name: [{match,pick,betSize,odds,settled,hit,profit}]},  // 今日已有下注（仅作聊天话题）
      history: [{name, text}],        // 上一轮，用于延续
      user_text: "客人的话",          // 本轮客人发言（有则回应客人）
    }
    """
    from src.role_registry import ROLES_DIR
    from src.providers.deepseek import DeepSeekProvider

    opts = req.get("opts") or {}
    day = str(opts.get("day") or "")
    dogs = opts.get("dogs") or []
    slate = opts.get("slate") or []
    picks = opts.get("picks") or {}
    history = opts.get("history") or []
    user_text = str(opts.get("user_text") or "").strip()

    def _persona(name):
        try:
            p = ROLES_DIR / name / "persona.md"
            if not p.exists() and os.environ.get("DS_ROLES_ROOT"):
                p = ROLES_DIR / "persona.md"
            txt = p.read_text(encoding="utf-8") if p.exists() else ""
            return txt[:600]
        except Exception:
            return ""

    def _pick_text(name):
        plist = picks.get(name) or []
        if not plist:
            return "今天没出手。"
        parts = []
        for o in plist[:3]:
            m = o.get("match") or ""
            pick = o.get("pick") or o.get("pickLabel") or ""
            size = o.get("betSize")
            odds = o.get("odds")
            if o.get("settled"):
                res = "中了" if o.get("hit") else ("走水" if o.get("profit") == 0 else "栽了")
            else:
                res = "待开"
            parts.append("「%s」%s%s%s——%s" % (
                m, pick, (" %s" % size) if size is not None else "",
                (" @ %s" % odds) if odds is not None else "", res))
        return "；".join(parts)

    lines = []
    if day:
        lines.append("【今晚档期 · 足球日 %s】" % day)
    if slate:
        lines.append("场次：")
        for s in slate[:20]:
            lines.append("  - %s %s vs %s" % (
                s.get("league") or "", s.get("home") or "", s.get("away") or ""))
    lines.append("【各狗今日下注（只作聊天话题，酒馆内不会新增任何单）】")
    for d in dogs:
        name = d.get("name") if isinstance(d, dict) else d
        lines.append("- %s：%s" % (name, _pick_text(name)))
    lines.append("【出场角色（名号 + 人设）】")
    for d in dogs:
        name = d.get("name") if isinstance(d, dict) else d
        tag = d.get("tagline") if isinstance(d, dict) else ""
        lines.append("· %s（%s）：%s" % (name, tag or "招牌", _persona(name)))
    if history:
        lines.append("【上一轮对话（请自然延续）】")
        for h in history[-12:]:
            who = h.get("name") or ""
            lines.append("%s：%s" % ("客人" if who == "你" else who, h.get("text") or ""))
    if user_text:
        lines.append("【客人的话（务必回应）】%s" % user_text)

    system = (
        "你是「深夜酒馆」的老板，主持一场赌狗深夜酒局。下面是今天真实的比赛、各位今日下注、"
        "每位角色的名号与人设"
        + ("、客人刚才说的话" if user_text else "")
        + "。请代入每位角色，按各自人设说话：聊天、抬杠、调侃都行，像老朋友在酒桌上。\n"
        "规则：\n"
        "1. 每只狗只说一句，力度要足，有人味，可损对手可自嘲。\n"
        "2. 有【客人的话】时：被点名的狗必须直接回应客人的问题，其他狗可插嘴，至少 2 只狗开口。\n"
        "3. 没有【客人的话】时：开一轮，让它们就真实场次吵起来，至少 3-5 只狗开口，别冷场。\n"
        "4. 本酒馆只聊天、绝不下单：客人就算喊「来一单」「今晚买什么」，也只按人设回应（可调侃、可劝他去斗狗场点「⚡ 分析」），不许声称自己刚下了单。\n"
        "5. 禁止编造【各狗今日下注】之外的下注（那里没列你的单=你今天没出手），禁止替客人下单，禁止重复人设原文。\n"
        "6. 只输出本轮对话，每行严格为：狗名: 内容\n"
        "7. 不要旁白、不要说明。\n\n" + "\n".join(lines)
    )

    provider = DeepSeekProvider()
    raw = provider.call(
        system,
        [{"role": "user", "content": ("开一轮，回应客人。" if user_text else "开一轮，让它们吵起来。")}],
        temperature=1.05,
        thinking=False,
    )
    raw = re.sub(r'\[thinking\].*?\[/thinking\]\s*', '', raw, flags=re.DOTALL).strip()

    known = set()
    for d in dogs:
        known.add(d.get("name") if isinstance(d, dict) else d)

    messages = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r'^([^：:]{1,20})[:：](.+)$', ln)
        if not m:
            continue
        name = m.group(1).strip()
        text = m.group(2).strip()
        if not text or not name:
            continue
        if known and name not in known and name != "酒馆老板":
            continue
        messages.append({"name": name, "text": text})
    if not messages:
        messages.append({"name": "酒馆老板", "text": raw[:400]})
    return {"messages": messages}


FUNCS = {
    "prepare": _do_prepare,
    "prepare-range": _do_prepare_range,
    "analyze": _do_analyze,
    "settle": _do_settle,
    "factor-induction": _do_induction,
    "factor-review": _do_review,
    "status": _do_status,
    "refresh": _do_refresh,
    "reset": _do_reset,
    "tavern": _do_tavern,
}


def main() -> int:
    try:
        raw = sys.stdin.read() or "{}"
        req = json.loads(raw)
        if not isinstance(req, dict):
            raise BridgeError("请求必须是 JSON 对象")
    except BridgeError as e:
        _emit({"type": "error", "func": "", "message": str(e)})
        return 1
    except Exception as e:
        _emit({"type": "error", "func": "", "message": f"请求解析失败: {e}"})
        return 1

    func = req.get("func")
    if func not in FUNCS:
        _emit({"type": "error", "func": str(func), "message": f"未知 func: {func!r}（可用: {', '.join(FUNCS)}）"})
        return 1

    # ── 沙箱角色根覆盖：在 handler（惰性 import src.*）之前设置 env ──
    role_root = (req.get("opts") or {}).get("role_root")
    if role_root:
        role_root = str(role_root)
        if not os.path.isabs(role_root):
            _emit({"type": "error", "func": func, "message": f"role_root 必须是绝对路径: {role_root!r}"})
            return 1
        os.environ["DS_ROLES_ROOT"] = role_root
        os.environ["DS_SESSIONS_ROOT"] = os.path.join(role_root, "sessions")
        os.environ["DS_FACTORS_ROOT"] = os.path.join(role_root, "factors")

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            data = FUNCS[func](req)
        _OUT.flush()
        if buf.getvalue():
            sys.stderr.write(buf.getvalue())
        _emit({"type": "result", "func": func, "data": data})
        return 0
    except BridgeError as e:
        _OUT.flush()
        if buf.getvalue():
            sys.stderr.write(buf.getvalue())
        _emit({"type": "error", "func": func, "message": str(e)})
        return 1
    except Exception as e:  # noqa: BLE001 —— 桥接层兜底，恒回合法 JSON
        _OUT.flush()
        if buf.getvalue():
            sys.stderr.write(buf.getvalue())
        sys.stderr.write(traceback.format_exc())
        _emit({"type": "error", "func": func, "message": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
