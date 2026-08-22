#!/usr/bin/env python3
"""dsh ↔ python-engine 统一桥接入口（NDJSON 协议，仿 place_orders 桥）。

用法（dsh 侧固定 argv 直接 spawn，不拼 shell）:
    python3 -m src.bridge

stdin: 单行 JSON 请求
    {
      "func": "prepare|analyze|settle|factor-induction|factor-review|status|refresh|reset",
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
    ]

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
        "prefetched_ok": ok,
        "failed": fail,
        "matches_fetched": fetched_dates,
        "features_prefetched": ok,
        "warnings": warnings,
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
    skip_llm = bool(opts.get("skip_llm", False))

    agent = _agent(dog)
    # 回放（live=false）同样需要 LLM 决策：node_call_llm 在 rt.provider 为空时直接返回空响应
    if not skip_llm:
        _provider(agent)
    _progress("分析中（LLM 决策）" if not skip_llm else "分析中（演示模式·跳过 LLM）", detail=f"{dog} {day}")
    result = agent.analyze(day, live=live, jingcai_only=jingcai_only, prefetched=prefetched)
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
    agent = _agent(dog)
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


FUNCS = {
    "prepare": _do_prepare,
    "analyze": _do_analyze,
    "settle": _do_settle,
    "factor-induction": _do_induction,
    "factor-review": _do_review,
    "status": _do_status,
    "refresh": _do_refresh,
    "reset": _do_reset,
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
