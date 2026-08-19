"""
DSFootball Python CLI — 博彩 Agent (LangGraph)

基于 LangGraph StateGraph 的博彩 agent。
状态在节点间流转，每个节点是纯函数 (state → state)。
"""

import sys, os, json, re
from pathlib import Path
from datetime import datetime, timedelta, date, timezone
from typing import Optional, Callable, TypedDict, Annotated, Any
from dataclasses import dataclass, field

from langgraph.graph import StateGraph, END

from .role import Role
from .data_manager import DataManager
from .prompt_builder import PromptBuilder, load_system_prompt
from .order_utils import parse_orders
from .environment import strip_scores, get_football_day, football_day_calendar_dates
from .session_logger import SessionLogger
from .base_llm import BaseLLMProvider
from .store import _get_valid_section_slugs as _valid_section_slugs
from .fund_limits import FundManager, order_limits_for

# 阶段5: 反思 key_slugs 可上报的合法数据段白名单
SECTION_SLUG_WHITELIST = sorted(_valid_section_slugs())
DEFAULT_SECTION_SLUGS = [
    "match-head", "fair-odds", "eu-odds-pinnacle", "asian-handicap-pinnacle",
    "over-under-crown", "betfair-buysell", "discrete-odds",
]


# ═══════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════

class AgentState(TypedDict, total=False):
    """Agent 状态 — 在 graph 节点间流转"""
    user: str
    day_date: str
    live: bool
    jingcai_only: bool  # 只拉竞彩比赛，减少数量加速测试
    prefetched: bool    # 数据已由外部预取（prefetch 命令），跳过强制刷新
    capital: float

    # role 不可序列化，存引用
    role_loaded: bool

    # analyze 阶段
    matches: list[dict]
    safe_matches: list[dict]
    prompt: dict
    llm_response: str
    orders: list[dict]
    placed_count: int

    # settle 阶段
    unsettled_orders: list[dict]
    scores: dict[str, str]
    settlement: dict

    # factor_review 阶段
    review_start_date: str   # 评估窗口起始日（factor_review 用，空=自动7天）

    # 输出
    status_msg: str
    error: str


# ═══════════════════════════════════════════════
# Runtime（跨节点共享，不序列化）
# ═══════════════════════════════════════════════

@dataclass
class AgentRuntime:
    """Agent 运行时 — 跨节点共享的可变状态"""
    role: Optional[Role] = None
    dm: DataManager = field(default_factory=DataManager)
    builder: PromptBuilder = field(default_factory=PromptBuilder)
    provider: Optional[BaseLLMProvider] = None
    session: Optional[SessionLogger] = None
    last_settled_orders: list[dict] = field(default_factory=list)


_runtime: dict[str, AgentRuntime] = {}  # user → runtime


# 比赛 match_time 是北京时间(UTC+8)。now 必须同时区，否则宿主机在 UTC 等时区时
# match_time > now 会整体偏 8 小时（已完赛当未开、live 过滤/结算判断全错）。
# 这里把 now 钉死成北京时间，不依赖宿主机系统时区。
_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_bj(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """当前北京时间(UTC+8)字符串，与 match_time 同时区。"""
    return datetime.now(_BEIJING_TZ).strftime(fmt)


def _rt(state: AgentState) -> AgentRuntime:
    user = state.get("user", "default")
    if user not in _runtime:
        _runtime[user] = AgentRuntime()
    return _runtime[user]


# ═══════════════════════════════════════════════
# Nodes — analyze
# ═══════════════════════════════════════════════

def node_load_role(state: AgentState) -> AgentState:
    """加载或创建角色"""
    rt = _rt(state)
    user = state["user"]

    if rt.role is None:
        try:
            rt.role = Role.load(user)
        except (FileNotFoundError, ValueError):
            rt.role = Role(name=user, capital=state.get("capital", 10000))
            rt.role.save()

    rt.role.memory.refresh_from_role(rt.role)
    return {**state, "role_loaded": True}


def node_fetch_matches(state: AgentState) -> AgentState:
    """从 DataManager 获取足球日比赛"""
    rt = _rt(state)
    day_date = state["day_date"]

    print(f"  🔍 拉取比赛...", end=" ", flush=True)
    d = date.fromisoformat(day_date)

    cal_dates = football_day_calendar_dates(d)
    start, end = get_football_day(d)

    ltype = "all"  # 总是拉全部，jingcai 过滤在下面做

    all_matches = []
    for cd in cal_dates:
        matches = rt.dm.get_cached_matches(cd, lottery_type=ltype)
        # live模式强制刷新缓存（避免用过期数据）；prefetched 波次已由外部预取，直接用缓存
        if not state.get("prefetched") and (state.get("live") or not matches):
            matches = rt.dm.fetch_matches_by_date(cd, lottery_type=ltype)
            if matches:
                rt.dm.save_matches_cache(cd, matches)
        all_matches.extend(matches or [])

    matches = [m for m in all_matches if start <= m.get("match_time", "") <= end]

    # 去重：同一场（lota_id）可能同时出现在相邻日历日缓存（API 对相邻 date 返回重叠块），
    # 只保留第一条，避免 LLM 拿到重复比赛（token 浪费 + 重复下单风险）。
    seen_lids: set[str] = set()
    unique: list[dict] = []
    for m in matches:
        lid = m.get("lota_id")
        if lid in seen_lids:
            continue
        seen_lids.add(lid)
        unique.append(m)
    matches = unique

    # 过滤掉数据缺失的比赛（队名为空或 ?）
    matches = [
        m for m in matches
        if m.get("home_name", "?") not in ("", "?")
        and m.get("away_name", "?") not in ("", "?")
        and m.get("lota_id")
    ]

    # 竞彩过滤：只保留有 jingcai_number 的比赛
    if state.get("jingcai_only"):
        matches = [m for m in matches if m.get("jingcai_number")]

    # live 模式：当 now 仍落在该足球日窗口内时，保留全部比赛（含已开赛）。
    # 已开赛比赛标注 _live_started=True，供 prompt & place_orders 使用：
    #   - prompt: 展示全量比赛 + 已有持仓标注，LLM 可对比信号强度重新分配资金
    #   - place_orders: 只更新未开赛比赛的订单，已开赛的维持原仓
    # 回测（历史日期，now 早已越过窗口末）→ 不触发，全量保留。
    if state.get("live"):
        now_str = _now_bj()
        if start <= now_str <= end:
            for m in matches:
                m["_live_started"] = m.get("match_time", "") <= now_str

    print(f"{len(matches)} 场")

    if rt.session:
        rt.session.tool_call("fetch_matches", {
            "day_date": day_date,
            "calendar_dates": cal_dates,
            "window": f"{start} ~ {end}",
        }, f"{len(matches)} matches in football day window")

    return {**state, "matches": matches}


def node_strip_scores(state: AgentState) -> AgentState:
    """剥离比分"""
    return {**state, "safe_matches": strip_scores(state.get("matches", []))}


def node_fetch_features(state: AgentState) -> AgentState:
    """预取 compact-fet → 本地缓存（批量，避免 prompt 阶段逐场 API 调用）

    使用 get_compact_fet（cache-first），失败时自动写入 negative cache，
    后续 prompt 阶段的 get_compact_fet / get_tags 不会重复请求。
    """
    rt = _rt(state)
    safe = state.get("safe_matches", [])
    cached = 0
    fetched = 0
    failed = 0

    for m in safe:
        lid = m.get("lota_id", "")
        if not lid:
            continue

        # get_compact_fet 内部已有 TTL 策略：
        #   - 已完场 → 缓存永久有效
        #   - live/upcoming → 缓存 120s，过期自动刷新
        data = rt.dm.get_compact_fet(lid)
        if data is None:
            failed += 1
        elif data.get("_cached_at") and not data.get("_api_failed"):
            cached += 1
        else:
            fetched += 1

    if rt.session and (cached + fetched + failed) > 0:
        rt.session.tool_call("fetch_features", {
            "total": len(safe),
            "cached": cached,
            "fetched": fetched,
            "failed": failed,
        }, f"预取 compact-fet: {cached} cached, {fetched} fetched, {failed} failed")

    if failed > 0:
        print(f"[features] {failed}/{len(safe)} 场 compact-fet 获取失败（将用 match list 兜底）")

    # 在拉完 matches + features 之后再检查数据新鲜度（避免基于旧缓存误报）
    rt.dm.check_data_freshness(state["day_date"])

    return state


def _load_labels_for_matches(match_tasks: list[dict]) -> str:
    """train mode: 从标注文件读取已知比赛结果, 注入 prompt"""
    from pathlib import Path as _Path
    labels_dir = _Path(__file__).parent.parent / "data" / "labels"
    if not labels_dir.exists():
        return ""
    # 找最新的 labels 文件
    label_files = sorted(labels_dir.glob("labels_*.json"), reverse=True)
    if not label_files:
        return ""
    try:
        all_labels = json.loads(label_files[0].read_text(encoding="utf-8"))
    except Exception:
        return ""

    lines = ["## 🏷️ 已知比赛结果 (train mode)", ""]
    found = 0
    for t in match_tasks:
        lid = t.get("lota_id", "")
        lbl = all_labels.get(lid)
        if not lbl:
            continue
        found += 1
        sc = lbl.get("score", "?")
        home = lbl.get("home", "?")
        away = lbl.get("away", "?")
        asian_line = lbl.get("odds", {}).get("asian", {})
        hc = asian_line.get("handicap", 0)
        hc_key = f"{hc:+.2f}"
        asian_out = lbl.get("outcomes", {}).get("asian", {}).get(hc_key, {})
        ou_line = lbl.get("odds", {}).get("ou", {})
        ou_th = ou_line.get("threshold", 2.5)
        ou_key = f"{ou_th:.2f}"
        ou_out = lbl.get("outcomes", {}).get("ou", {}).get(ou_key, {})
        x12 = lbl.get("outcomes", {}).get("1x2", {})

        h_profit = asian_out.get("H", 0)
        a_profit = asian_out.get("A", 0)
        over_profit = ou_out.get("over", 0)
        under_profit = ou_out.get("under", 0)

        h_icon = "✅" if h_profit > 0 else ("❌" if h_profit < 0 else "➖")
        a_icon = "✅" if a_profit > 0 else ("❌" if a_profit < 0 else "➖")
        o_icon = "✅" if over_profit > 0 else ("❌" if over_profit < 0 else "➖")
        u_icon = "✅" if under_profit > 0 else ("❌" if under_profit < 0 else "➖")

        lines.append(
            f"  {home} vs {away} | 比分 {sc} | "
            f"亚盘 H{hc_key} {h_icon}({h_profit:+.2f}/¥1) A {a_icon}({a_profit:+.2f}/¥1)"
        )
        x12_winner = "H" if x12.get("H") else ("D" if x12.get("D") else "A")
        lines.append(
            f"    1X2={x12_winner} | "
            f"大小球 O{ou_key} {o_icon}({over_profit:+.2f}/¥1) U {u_icon}({under_profit:+.2f}/¥1)"
        )

    if found:
        lines.append("")
        lines.append("⚠️ 以上结果已确定。你的任务是分析这些结果与赔率数据之间的规律，提炼可复用的因子。不需要预测结果。")
        return "\n".join(lines)
    return ""


def node_build_prompt(state: AgentState) -> AgentState:
    """构建 system prompt（含累积记忆 + 昨日回顾）"""
    rt = _rt(state)

    sp = load_system_prompt(rt.role.system_prompt_name)
    if not sp:
        sp = rt.builder.ensure_baseline()

    safe = state.get("safe_matches", [])

    # 🔒 过滤无特征数据（_api_failed）的比赛：不进 LLM prompt
    clean_safe = []
    api_failed_count = 0
    for m in safe:
        lid = m.get("lota_id", "")
        if not lid:
            continue
        feat = rt.dm.get_compact_fet(lid)
        if feat is None:
            # node_fetch_features 已尝试拉取，None = API 失败或 negative cache（_api_failed）
            api_failed_count += 1
            print(f"  ⚠️ 跳过无特征数据比赛: {lid} ({m.get('home_name', '')} vs {m.get('away_name', '')})")
        else:
            clean_safe.append(m)

    if api_failed_count:
        print(f"  🔒 过滤 {api_failed_count} 场无特征数据比赛，剩余 {len(clean_safe)} 场进入 LLM")

    # ── 阶段2: 按 active 因子的 slugs 扩展每场数据段 ──
    # PromptBuilder 会把 factors[].slugs 追加进该场 sections（默认 7 段 + 因子 slugs）。
    # 用独立 FactorMemory 实例读取，不改动主 memory 的加载状态（角色因子段是否注入另议）。
    factor_objs: list = []
    try:
        if rt.role and rt.role.memory:
            from types import SimpleNamespace
            from .memory import FactorMemory as _FM
            _fm = _FM(rt.role.memory.factors.path.parent)
            _fm.load()
            main, _, _ = _fm.selected_active()
            factor_objs = [
                SimpleNamespace(slugs=fdata.get("slugs") or [])
                for _, fdata, _ in main
            ]
    except Exception as e:
        print(f"  ⚠️ 因子 slugs 扩展失败（不影响分析）: {e}")

    match_tasks = [
        {"lota_id": m["lota_id"], "factors": factor_objs}
        for m in clean_safe if m.get("lota_id")
    ]

    # 获取刚结算的订单（优先 runtime，fallback 磁盘）
    settled_orders = rt.last_settled_orders
    if not settled_orders:
        all_orders = rt.role.get_orders()
        settled_orders = [
            o for o in all_orders if o.get("settled_at")
        ]
        settled_orders.sort(
            key=lambda o: o.get("settled_at", ""), reverse=True
        )
        # 只取最近一批（同一天结算的）
        if settled_orders:
            latest_ts = settled_orders[0].get("settled_at", "")[:10]
            settled_orders = [
                o for o in settled_orders
                if o.get("settled_at", "")[:10] == latest_ts
            ]

    day_date = state.get("day_date", "")

    # ── 全金额 = 已锁定敞口 + 可用余额，让 LLM 按全部资金做比例分配 ──
    capital = rt.role.capital if rt.role else 10000
    locked_exposure = sum(
        o.get("bet_size", 0) for o in rt.role.get_orders()
        if not o.get("settled_at")
    )
    full_capital = capital + locked_exposure  # 全金额

    # ── train mode: 注入已标注的比赛结果 ──
    labels_text = _load_labels_for_matches(match_tasks) if match_tasks else ""

    result = rt.builder.build(
        system_prompt=sp,
        memory=rt.role.memory,
        matches=match_tasks,
        settled_orders=settled_orders,
        day_date=day_date,
        persona_text=rt.role.persona_text() if rt.role else "",
        capital=full_capital,
        alpha_mode=rt.role.alpha_mode if rt.role else False,
        cross_factor_exclude=rt.role.cross_factor_exclude if rt.role else [],
        extra_context=labels_text if labels_text else None,
    )

    # 记录今天使用的 slugs（供明天 settle 后回填 PnL）
    if hasattr(rt.role.memory, 'slugs'):
        rt.role.memory.slugs.record_day_slugs(day_date, sp.default_slugs)

    if rt.session:
        rt.session.tool_call("build_prompt", {
            "match_count": len(match_tasks),
            "strategy": sp.name if sp else "baseline-v1",
            "settlement_review": len(settled_orders),
        }, f"{result['token_count']} tokens (budget {result['budget']})")

    return {**state, "prompt": result, "safe_matches": clean_safe}


def node_call_llm(state: AgentState) -> AgentState:
    """调用 LLM"""
    rt = _rt(state)
    prompt = state.get("prompt", {})
    safe = state.get("safe_matches", [])

    if not rt.provider:
        return {**state, "llm_response": ""}

    user_msg = f"分析以下 {len(safe)} 场比赛并给出下注决策。"
    system = prompt.get("system", "")
    tokens_in = prompt.get("token_count", 0)

    response = rt.provider.call(system, [{"role": "user", "content": user_msg}], temperature=0.1)

    # 估算 response tokens (中文 ≈1.5 tok/char)
    tokens_out = int(len(response) * 1.5) if response else 0

    # 分组 token 统计: sys / mem / tools / data / user
    from .prompt_builder import count_tokens
    bd = prompt.get("breakdown", {})
    token_breakdown = {
        "sys": bd.get("role", 0),
        "mem": bd.get("memory", 0) + bd.get("settlement_review", 0),
        "tools": bd.get("frameworks", 0) + bd.get("output", 0),
        "data": bd.get("matches", 0),
        "user": count_tokens(user_msg),
    }

    if rt.session:
        rt.session.llm_call(
            system_prompt=system,
            response=response or "(LLM 返回空响应)",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            token_breakdown=token_breakdown,
            match_features=prompt.get("match_features"),
        )

    return {**state, "llm_response": response}


def node_parse_orders(state: AgentState) -> AgentState:
    """解析 LLM 响应 → orders"""
    rt = _rt(state)
    response = state.get("llm_response", "")
    safe = state.get("safe_matches", [])

    if not response:
        return {**state, "orders": []}

    orders = parse_orders(response, safe, rt.dm)
    return {**state, "orders": orders}


# ── 下单后数据尾点打印（仅供人工核对数据新鲜度，不进 LLM）──

_FET_SERIES_ORDER = [
    "离散指数",
    "欧盘:Pinnacle",
    "欧盘:澳门",
    "亚盘:Crown",
    "亚盘:澳门",
    "亚盘:Pinnacle",
    "大小球:Pinnacle",
    "大小球:Crown",
    "大小球:澳门",
]

_FET_SECTION_RE = re.compile(r"(离散指数|欧盘|亚盘|大小球)(:[^ ]*)?\s+t=")
_FET_POINT_RE = re.compile(r"(?:Δt\+|OPt-)(\d+)m")


def _parse_fet_series(text: str) -> dict[str, list[str]]:
    """解析 compact-fet 各赔率系列（兼容"数据点与下一段表头挤一行"的脏文本）。"""
    sections: dict[str, list[str]] = {}
    cur = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        hdrs = list(_FET_SECTION_RE.finditer(s))
        if hdrs:
            # 表头前的残留（可能是上一段最后的数据点，服务器缺换行导致）
            pre = s[: hdrs[0].start()].strip()
            if _FET_POINT_RE.match(pre) and cur:
                sections.setdefault(cur, []).append(pre)
            for i, h in enumerate(hdrs):
                cur = s[h.start() : h.end()].split(" t=")[0].strip()
                sections.setdefault(cur, [])
                nxt = hdrs[i + 1].start() if i + 1 < len(hdrs) else len(s)
                mid = s[h.end() : nxt].strip()
                if _FET_POINT_RE.match(mid):
                    sections.setdefault(cur, []).append(mid)
        elif _FET_POINT_RE.match(s) and cur:
            sections.setdefault(cur, []).append(s)
    return sections


def _fet_last_clock(sections: dict[str, list[str]], name: str, kickoff) -> datetime | None:
    """该系列最新数据点对应的实际时间（开赛时间 - Δt 分钟）。"""
    pts = sections.get(name) or []
    vals = []
    for p in pts:
        m = _FET_POINT_RE.match(p)
        if m:
            vals.append(int(m.group(1)))
    if not vals or kickoff is None:
        return None
    return kickoff - timedelta(minutes=vals[-1])


def _fet_odds_tail(text: str, per_series: int = 2) -> list[str]:
    """从 compact-fet 文本提取各赔率系列最近的几个数据点（Δt/OPt 行）。"""
    sections = _parse_fet_series(text)
    out = []
    for name in _FET_SERIES_ORDER:
        pts = sections.get(name) or []
        if pts:
            out.append(f"{name}: " + " | ".join(p[:90] for p in pts[-per_series:]))
    if not out:
        out = [ln.strip()[:120] for ln in text.strip().splitlines()[-3:]]
    return out


def _print_placed_fet_tail(rt, lid: str) -> None:
    """打印该场 compact-fet 最近赔率点 + 缓存时间，方便感知数据是否新鲜。"""
    feat = rt.dm.get_cached_compact_fet(lid) or {}
    text = feat.get("compact_fet", "") or (feat.get("data") or {}).get("compact_fet", "")
    cached_at = (feat.get("_cached_at") or "?")[:19]
    m = rt.dm.get_cached_match(lid) or {}
    home = m.get("home_name", "?")
    away = m.get("away_name", "?")
    mt = (m.get("match_time") or "?")[5:16]
    if not text:
        print(f"  📊 [{lid}] {home} vs {away} {mt} | fet_txt 无数据 (cached {cached_at})")
        return
    sections = _parse_fet_series(text)

    # 开赛时间（从 fet 文本"时间:"字段解析）
    kickoff = None
    mtxt = re.search(r"时间[：:]\s*([\d\- :]+)", text)
    if mtxt:
        try:
            kickoff = datetime.strptime(mtxt.group(1).strip()[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            kickoff = None

    clocks = {
        name: _fet_last_clock(sections, name, kickoff)
        for name in _FET_SERIES_ORDER
    }
    freshest = max((c for c in clocks.values() if c), default=None)

    print(f"  📊 [{lid}] {home} vs {away} {mt} | cached {cached_at}")
    stale_any = False
    for name in _FET_SERIES_ORDER:
        pts = sections.get(name) or []
        if not pts:
            continue
        flag = ""
        last_clock = clocks.get(name)
        if last_clock and freshest and (freshest - last_clock) > timedelta(minutes=60):
            flag = " ⚠️断更"
            stale_any = True
        line = " | ".join(p[:90] for p in pts[-2:])
        print(f"      {name}: {line}{flag}")
    if stale_any:
        print("      ⚠️ 有系列最新点比最全系列晚 60 分钟以上，数据疑似断更")


def node_place_orders(state: AgentState) -> AgentState:
    """下单"""
    rt = _rt(state)
    orders = state.get("orders", [])
    placed = 0
    placed_lids: list[str] = []

    # 已有未结算订单的「市场」集合 (lota_id, bet_type) → 同一场同一盘口类型不重复下单。
    # 注意用 (lota_id, bet_type) 而非仅 lota_id：同一场亚盘+大小球是合法双单，不能误挡。
    pending_markets = {
        (o.get("lota_id"), o.get("bet_type"))
        for o in rt.role.get_orders()
        if not o.get("settled_at") and o.get("lota_id")
    }

    # 已开赛比赛的 lota_id 集合 → 这些比赛的订单不更新，维持原仓
    started_lids = {
        m["lota_id"] for m in state.get("safe_matches", [])
        if m.get("_live_started")
    }

    # ── 分离：已开赛跳过 / 未开赛纳入 ──
    new_orders = []
    for o in orders:
        if o.get("skip"):
            continue
        lid = o.get("lota_id", "")
        if lid in started_lids:
            print(f"  🔒 跳过已开赛: {lid}（维持原仓）")
            continue
        market = (lid, o.get("bet_type"))
        if market in pending_markets:
            print(f"  ⏭ 跳过重复单: {market[0]} {market[1]}（已有未结算订单）")
            continue
        new_orders.append(o)

    # ── 比例折算：LLM按全金额分配 → 按实际余额缩放 ──
    # 全金额 = 锁定敞口 + 余额  (与 node_build_prompt 一致)
    capital = rt.role.capital
    locked_exposure = sum(
        o.get("bet_size", 0) for o in rt.role.get_orders()
        if not o.get("settled_at")
    )

    # ── 资金管理硬约束（按狗配置；未配置的狗保持旧行为）──
    limits = order_limits_for(rt.role.name)
    if limits.enabled:
        new_orders, _dropped = FundManager(limits).apply(new_orders, capital)
    else:
        full_amount = capital + locked_exposure
        new_total = sum(o.get("bet_size", 0) for o in new_orders)
        if new_total > 0 and full_amount > 0:
            # 每单: LLM分配金额 / 全金额 * 余额(=capital)
            scale = capital / full_amount if full_amount > 0 else 1.0
            print(f"  📐 资金折算: 锁定¥{locked_exposure:,.0f} + 余额¥{capital:,.0f} = 全金额¥{full_amount:,.0f}")
            print(f"  📐 LLM分配¥{new_total:,.0f} → 折算×{scale:.2f} → 实下¥{int(new_total * scale):,.0f}")
            for o in new_orders:
                o["bet_size"] = int(o["bet_size"] * scale)

    for o in new_orders:
        try:
            rt.role.place_order(o)
            pending_markets.add((o.get("lota_id"), o.get("bet_type")))
            placed += 1
            placed_lids.append(o.get("lota_id", ""))
        except ValueError:
            break  # 资金不够，后续订单不再尝试

    # 下单后自动打印 fet_txt 最近赔率点（不进 LLM，人工核对数据用）
    if placed_lids:
        print("\n  📊 下单数据尾点 (fet_txt):")
        for lid in dict.fromkeys(placed_lids):
            _print_placed_fet_tail(rt, lid)

    rt.role.save()

    if rt.session:
        rt.session.orders(orders)

    return {**state, "placed_count": placed}


# ═══════════════════════════════════════════════
# Nodes — settle
# ═══════════════════════════════════════════════

def node_load_unsettled(state: AgentState) -> AgentState:
    """加载所有未结算订单。

    结算 = 结算 now 之前已开赛的对应订单。不再按足球日窗口过滤 ——
    只要是 pending 状态就纳入结算流程，比分查询步骤会自然跳过还没出比分的比赛。
    """
    rt = _rt(state)
    orders = rt.role.get_orders()
    unsettled = [o for o in orders if not o.get("settled_at")]

    print(f"\n  📋 全部未结算: {len(unsettled)} 单")

    # 标注订单所属比赛时间，方便排查
    if unsettled:
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        matched = 0
        future = 0
        for o in unsettled:
            lid = o.get("lota_id", "")
            m = rt.dm.get_cached_match(lid)
            if m:
                mt = m.get("match_time", "")
                if mt and mt > now:
                    future += 1
                else:
                    matched += 1
        if future:
            print(f"  ⏳ {future} 单比赛未开始（比分未出时会跳过）")
        print(f"  🎯 {len(unsettled)} 单待结算 (已开赛≈{matched}, 未开始≈{future})")

    return {**state, "unsettled_orders": unsettled}


def node_fetch_scores(state: AgentState) -> AgentState:
    """获取比分 — 本地缓存为准，API 兜底:
    1. 本地 matches/{date}.json 扫描（已完场的权威比分）
    2. API 强制刷新（仅补缺）
    3. compact-fet 缓存兜底
    """
    rt = _rt(state)
    unsettled = state.get("unsettled_orders", [])
    day_date = state.get("day_date", "")

    rt.dm.check_data_freshness(day_date)

    if not unsettled:
        return {**state, "scores": {}}

    scores = {}
    lids_needed = set(o.get("lota_id", "") for o in unsettled)

    # 收集需要查的日期
    from datetime import date as _date, timedelta
    dates_to_check = set()
    if day_date:
        dates_to_check.add(day_date)
        dates_to_check.add((_date.fromisoformat(day_date) + timedelta(days=1)).isoformat())
        dates_to_check.add((_date.fromisoformat(day_date) - timedelta(days=1)).isoformat())

    # 1. 本地缓存优先 — matches/{date}.json（仅 state==6 完场才是权威比分）
    for d in sorted(dates_to_check):
        try:
            matches = rt.dm.get_cached_matches(d, lottery_type="all")
            for m in matches:
                lid = m.get("lota_id", "")
                sc = m.get("score", "")
                if lid in lids_needed and sc and m.get("state") == 6:
                    scores[lid] = sc
        except Exception:
            pass

    # 2. API 补缺 — 本地没有的才去拉（仅 state==6 完场才取比分）
    missing = lids_needed - set(scores.keys())
    if missing:
        for d in sorted(dates_to_check):
            try:
                matches = rt.dm.fetch_matches_by_date(d, lottery_type="all")
                if matches:
                    rt.dm.save_matches_cache(d, matches)
                    for m in matches:
                        lid = m.get("lota_id", "")
                        if lid in missing and m.get("state") == 6:
                            sc = m.get("score", "")
                            if sc:
                                scores[lid] = sc
            except Exception:
                pass

    # 2.5. 逐 ID API 补缺 — 日期查询覆盖不到的（如缓存 state=-1 的坏数据），直接用 match by ID 拉
    still_missing = lids_needed - set(scores.keys())
    if still_missing:
        for lid in list(still_missing):
            try:
                refreshed = rt.dm.refresh_score_match(lid)
                if refreshed:
                    sc = refreshed.get("score", "")
                    if sc and refreshed.get("state") == 6:
                        scores[lid] = sc
            except Exception:
                pass

    # 3. compact-fet 兜底（同样仅限已完场 state==6，避免用进行中/未开的比分误结算）
    for o in unsettled:
        lid = o.get("lota_id", "")
        if lid in scores:
            continue
        cm = rt.dm.get_cached_match(lid) or {}
        if cm.get("state") != 6:
            continue
        feat = rt.dm.get_cached_compact_fet(lid)
        if feat and not feat.get("_api_failed"):
            sc = (feat.get("data") or {}).get("score", feat.get("score", ""))
            if sc:
                scores[lid] = sc

    found = sum(1 for lid in lids_needed if lid in scores)
    missing_final = len(lids_needed) - found
    if missing_final:
        print(f"  🔍 比分: {found}/{len(lids_needed)} 找到(缓存), {missing_final} 缺失")

    if rt.session:
        rt.session.tool_call("fetch_scores", {
            "total": len(lids_needed),
            "found": found,
            "missing": missing_final,
            "cached": found - sum(1 for lid in (lids_needed - missing) if lid not in scores),  # rough
            "dates_checked": sorted(dates_to_check),
        }, f"{found}/{len(lids_needed)} scores found")

    return {**state, "scores": scores}


def node_settle_orders(state: AgentState) -> AgentState:
    """逐条结算"""
    rt = _rt(state)
    unsettled = state.get("unsettled_orders", [])
    scores = state.get("scores", {})

    hit = miss = push = 0
    pnl = 0.0
    settled_list = []
    skipped_no_score = 0
    skipped_error = 0
    for o in unsettled:
        lid = o.get("lota_id", "")
        sc = scores.get(lid)
        if not sc:
            skipped_no_score += 1
            continue
        try:
            settled = rt.role.settle_order(o, sc)
            settled_list.append(settled)
        except Exception as e:
            skipped_error += 1
            print(f"  ❌ 结算异常 {lid}: {e}")
            continue
        h = settled.get("hit")
        profit = settled.get("profit", 0)
        # 赢半/输半用利润判断图标（hit=None 时 profit≠0）
        if profit > 0:
            icon = "✅"
        elif profit < 0:
            icon = "❌"
        else:
            icon = "➖"
        # 查找比赛名
        m = rt.dm.get_cached_match(lid) or {}
        home = m.get("home_name", "") or m.get("home", "")
        away = m.get("away_name", "") or m.get("away", "")
        league = m.get("league_name", "") or m.get("league", "")
        match_label = f"{home} vs {away}" if home and away else lid
        if league:
            match_label += f" ({league})"
        # 亚盘/大小球/让球胜平负显示盘口
        hc = o.get("handicap", 0) or 0
        show_hc = o.get("bet_type") in ("亚盘", "大小球", "让球胜平负")
        hc_str = f" {hc:+.2f}" if hc and show_hc else ""
        print(f"  {icon} {match_label} {sc} | {o.get('bet_type','')} {o.get('pick','')}{hc_str} "
              f"@{o.get('odds',0):.2f} bet{o.get('bet_size',0):.0f} → {profit:+.0f}")
        # 赢半/输半（hit=None 但 profit≠0）按方向计入命中/未中，走水只留真走水(profit=0)
        if h is True or (h is None and profit > 0):       hit += 1
        elif h is False or (h is None and profit < 0):    miss += 1
        else:                                             push += 1
        pnl += profit

    if skipped_no_score:
        print(f"  ⏭ {skipped_no_score} 单无比分跳过")
    if skipped_error:
        print(f"  ❌ {skipped_error} 单结算异常跳过")
    if hit + miss + push > 0:
        print(f"  💰 余额: {rt.role.capital:.0f}")

    rt.role.save()
    rt.last_settled_orders = settled_list

    settlement = {
        "settled": hit + miss + push,
        "hit": hit, "miss": miss, "push": push,
        "pnl": round(pnl, 2),
    }
    return {**state, "settlement": settlement}


# ═══════════════════════════════════════════════
# Node — reflect (alpha factor discovery)
# ═══════════════════════════════════════════════

def _extra_reflect_matches(dm: DataManager, day_date: str, exclude_lids: set,
                           max_extra: int = 3) -> list[str]:
    """当天足球日窗口内、已完场但未下单的比赛，随机挑几场作为反思补充样本。"""
    import random

    if not day_date:
        return []
    try:
        # settle 的 day_date 是窗口结束日（起始日+1），窗口起始日 = day_date - 1
        end_d = date.fromisoformat(day_date)
    except ValueError:
        return []
    start_d = end_d - timedelta(days=1)
    cal_dates = football_day_calendar_dates(start_d)
    start, end = get_football_day(start_d)

    candidates: list[dict] = []
    for cd in cal_dates:
        for m in dm.get_cached_matches(cd, lottery_type="all"):
            lid = m.get("lota_id", "")
            if not lid or lid in exclude_lids:
                continue
            mt = m.get("match_time", "")
            if not (start <= mt <= end):
                continue
            if m.get("state") != 6:
                continue
            candidates.append(m)

    if not candidates:
        return []
    random.shuffle(candidates)
    return [m["lota_id"] for m in candidates[:max_extra]]


def run_reflect(settled: list[dict], day_date: str, role,
                provider=None, dm=None,
                use_slug_history: bool = False,
                slug_history_max: int = 8,
                slug_history_tokens: int = 3200,
                slug_history_days: int = 90,
                extra_matches: bool = True,
                save_fac: bool = True) -> dict:
    """结算后反思核心 — 可被 node_reflect（线上）与对照组脚本复用。

    归因逻辑: LLM 判断每笔订单受哪些 alpha 因子影响 → 按实际 hit/miss/profit
    更新 FactorMemory，使因子统计数据反映真实表现。

    use_slug_history=True 时，额外注入「历史同信号比赛回顾」（时间从近到远、
    含赛果），让因子思考不只看当天订单，还看同 slug 信号在历史完场比赛中的表现。

    role 只需提供: alpha_mode / cross_factor_exclude / system_prompt_name /
    persona_text() / memory.factors(FactorMemory) / memory.reflections(ReflectionMemory)。
    """
    from .data_manager import DataManager
    from .providers.deepseek import DeepSeekProvider

    if provider is None:
        provider = DeepSeekProvider()
    dm = dm or DataManager()

    # 构建反思 prompt（订单编号供归因使用）
    orders_text = ""
    for i, o in enumerate(settled):
        h = o.get("hit")
        profit = o.get("profit", 0)
        if profit > 0:      icon = "✅"
        elif profit < 0:    icon = "❌"
        else:               icon = "➖"
        orders_text += (
            f"  order_{i}: {icon} {o.get('bet_type','')} {o.get('pick','')} "
            f"@{o.get('odds',0):.2f} bet{o.get('bet_size',0):.0f} → {o.get('profit',0):+.0f}\n"
            f"          lota_id={o.get('lota_id','')} 理由: {o.get('reason','')[:150]}\n"
        )

    # 已有因子状态（仅供退役参考）
    existing_summary = ""
    if role:
        if not role.memory.factors._loaded:
            role.memory.factors.load()
        fp = role.memory.factors.factor_perf
        if fp:
            existing_summary = "\n".join(
                f"  {fid}: {s.get('total',0)}次 状态={s.get('status','active')}"
                for fid, s in sorted(fp.items(), key=lambda x: -x[1].get("total", 0))[:20]
            )
    else:
        fp = {}

    # alpha 模式：注入跨狗因子注册表，避免重复发现
    cross_factors_text = ""
    if role and role.alpha_mode:
        from .factor_registry import FactorRegistry
        fr = FactorRegistry()
        cross_factors_text = fr.format_for_prompt(current_date=day_date)[:3000]
        if cross_factors_text:
            cross_factors_text = (
                "## 🐺 跨狗因子注册表（其他狗的已发现因子，避免重复发现）\n"
                + cross_factors_text
            )

    # ── 回溯每笔结算订单的 compact-fet 数据（按 lota_id 去重）──
    REFLECT_TOKENS_PER_MATCH = 1200
    from .prompt_builder import count_tokens, truncate_section
    # 取所有相关 slug（从 system_prompt 加载，fallback 默认列表）
    from .prompt_builder import load_system_prompt
    sp = load_system_prompt(role.system_prompt_name) if role and role.system_prompt_name else None
    reflect_slugs = list(dict.fromkeys(
        sp.default_slugs if sp
        else ["match-head", "fair-odds", "eu-odds-pinnacle", "asian-handicap-pinnacle",
              "over-under-crown", "betfair-buysell", "discrete-odds"]
    ))
    # 按 lota_id 去重，记录关联的 order 索引
    seen_lids: dict[str, list[int]] = {}  # lota_id → [order_index, ...]
    for i, o in enumerate(settled):
        lid = o.get("lota_id", "")
        seen_lids.setdefault(lid, []).append(i)

    match_data_blocks = []
    match_features: dict[str, str] = {}
    for lid, order_indices in seen_lids.items():
        ctx = dm.get_match_context(lid)
        match_info = ctx.get("match", {})
        data_text = dm.get_sections(lid, reflect_slugs) if lid else ""
        if count_tokens(data_text) > REFLECT_TOKENS_PER_MATCH:
            data_text = truncate_section(data_text, REFLECT_TOKENS_PER_MATCH)
        match_features[lid] = data_text
        # 比分: 优先 match_info，fallback order 里的 score
        score = match_info.get("score", "") or settled[order_indices[0]].get("score", "?")
        header = (
            f"### lota_id={lid} | {match_info.get('home','?')} vs {match_info.get('away','?')} | 比分={score}\n"
        )
        for idx in order_indices:
            o = settled[idx]
            icon = "✅" if o.get("hit") is True else ("❌" if o.get("hit") is False else "➖")
            header += (
                f"  order_{idx}: {icon} {o.get('bet_type','')} {o.get('pick','')} "
                f"@{o.get('odds',0):.2f} bet{o.get('bet_size',0):.0f} → {o.get('profit',0):+.0f}\n"
                f"    理由: {o.get('reason','')[:200]}\n"
            )
        match_data_blocks.append(header + "\n" + data_text)

    # 随机补充当天窗口内完场但未下单的比赛，扩大反思样本（LLM 只参考，不参与归因）
    extra_lids: list[str] = []
    if extra_matches:
        extra_lids = _extra_reflect_matches(dm, day_date, set(seen_lids), max_extra=3)
        for lid in extra_lids:
            ctx = dm.get_match_context(lid)
            match_info = ctx.get("match", {})
            data_text = dm.get_sections(lid, reflect_slugs) if lid else ""
            if count_tokens(data_text) > REFLECT_TOKENS_PER_MATCH:
                data_text = truncate_section(data_text, REFLECT_TOKENS_PER_MATCH)
            match_features[lid] = data_text
            score = match_info.get("score", "?")
            header = (
                f"### lota_id={lid} | {match_info.get('home','?')} vs {match_info.get('away','?')} "
                f"| 比分={score}（未下单，仅供参考）\n"
            )
            match_data_blocks.append(header + "\n" + data_text)
        if extra_lids:
            print(f"  🎲 反思补充未下单比赛: {len(extra_lids)} 场 ({', '.join(extra_lids)})")

    match_data_text = "\n\n---\n\n".join(match_data_blocks) if match_data_blocks else "(无数据)"

    # ── 历史同信号比赛回顾（对照实验用，默认关）──
    history_block = ""
    history_slugs: list[str] = []
    if use_slug_history:
        from .slug_history import build_history_block as _build_hist
        history_slugs = list(dict.fromkeys(
            reflect_slugs + [s for p in fp.values() for s in (p.get("slugs") or [])]
        ))
        before = day_date + " 12:00:00"
        try:
            history_block = _build_hist(
                history_slugs, before,
                max_matches=slug_history_max,
                budget_tokens=slug_history_tokens,
                history_days=slug_history_days,
            )
        except Exception as e:
            print(f"  ⚠️ 历史同信号回顾生成失败（不影响分析）: {e}")

    # 人设 + 因子定义（与分析 prompt 对齐）
    persona_text = role.persona_text() if role else ""
    factor_desc_text = role.memory.factors.factor_desc_text() if role else ""

    reflect_prompt = f"""你是量化足球博彩分析师。你的任务是从已结算比赛中**发现可复用的投注因子**。

## 🎯 投注人设（所有下单基于此人设）
{persona_text if persona_text else '(未设)'}

## 已结算投注及原始数据
{match_data_text}

{history_block if history_block else ''}

## 当前已有因子（仅名字，统计不重要）
{existing_summary if existing_summary else '(空)'}
{factor_desc_text if factor_desc_text else ''}
{cross_factors_text}

## 任务 — 案例驱动的因子发现

{'**Step 0 — 历史同信号回顾（先看这个）**\n- 先看「历史同信号比赛回顾」：同样的信号模式在近期历史完赛中的命中/亏损如何？\n- 历史稳定盈利的模式才值得提炼为因子；历史同模式亏损居多时，把该模式标为失效/反向（在 per_match 注明），不要提炼成正因子。\n\n' if use_slug_history else ''}
**Step 1 — 跨场对比（先做这个）**
- 赢的场次之间有什么共同的信号模式？
- 输的场次是否触发了同样的信号（=因子失效）还是完全不同的信号（=因子没覆盖到）？
- 单场孤例不构成因子。一个因子必须能解释**至少 2-3 场**的共性结果。

**Step 2 — 模式抽象**
- 把能解释多个结果的信号提炼为因子
- 如果某场比赛是孤立事件，不强行提取因子，在 `per_match` 中标注即可

**Step 3 — 因子命名与描述**
- ⚠️ 因子名 ≤ 12 个字！这是硬约束。例: "离散凝聚上盘" "赔率下沉追强" "深盘低水保护" "退盘示弱小球"
- 不要把多个信号拼成一个长名字。如果一个因子需要 15+ 字才能描述 → 拆成多个独立因子
- ❌ 禁止写数值（">3" "<0.85"），数值是过拟合
- ✅ 用方向词: "碾压" "极端低位" "大幅收紧" "急剧下沉"

**Step 4 — 资金管理反思**
- 回顾今日盈亏：哪些注该下大、哪些该下小？
- 连续命中时是否过于保守？连续亏损时是否应该收手？
- 仓位和置信度是否匹配？今天最大的注是否真的是最有信心的？
- 总结 1-2 条资金管理教训，写入 `money_lesson` 字段

**Step 5 — slugs 标注（key_slugs 必须从白名单选）**
- 白名单: {', '.join(SECTION_SLUG_WHITELIST)}
- 默认常用段: {', '.join(DEFAULT_SECTION_SLUGS)}
- 因子依赖非默认段时才上报（这是为了分析时自动加载对应数据段）:
  亚盘澳门/皇冠(asian-handicap-macau/crown)、必发欧赔(betfair-eu)、
  近期状态(home-recent/away-recent)、首发(lineup)、历史交锋(match-history)、
  排名(rank-info)、进球/比分彩金(goal-bonus/score-bonus)、大小球澳门(over-under-macau)
- ⚠️ 只报该因子真正依赖的段，不要全选；`noise_slugs` 报对判断无用的段

输出格式 — 必须输出合法 JSON（使用 JSON Output 模式）：

```json
{{
  "per_match": {{"order_0": "离散凝聚,资金同向", "order_1": "赔率下沉,深盘低水"}},
  "alpha_factors": ["离散凝聚深盘顺向", "深盘低水保护"],
  "key_slugs": ["discrete-odds", "fair-odds"],
  "noise_slugs": ["match-head"],
  "factor_desc": {{"离散凝聚深盘顺向": "一句话描述"}},
  "factor_attribution": {{"order_0": ["离散凝聚深盘顺向"], "order_1": ["深盘低水保护"]}},
  "money_lesson": "资金管理教训 ≤80字",
  "reflection": "跨场规律总结 ≤200字"
}}
```"""

    # 初始化所有变量（LLM 调用失败时走兜底）
    data = None
    alpha = ""
    key_slugs_str = ""
    noise_slugs_str = ""
    summary = ""
    attr_map: dict[int, list[str]] = {}
    desc_map: dict[str, str] = {}
    per_match: dict = {}
    key_slugs_list: list[str] = []
    noise_slugs_list: list[str] = []
    money_lesson = ""
    new_factors: list[str] = []

    try:
        reflection = provider.call(
            reflect_prompt,
            [{"role": "user", "content": "按 JSON 格式输出。"}],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
    except Exception:
        reflection = ""

    if reflection and role:
        clean = BaseLLMProvider.strip_thinking(reflection)

        # JSON 解析
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            # 容错：尝试提取 JSON 块
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    print(f"  ⚠️ reflect JSON 解析失败")
                    data = None
            else:
                print(f"  ⚠️ reflect JSON 解析失败")
                data = None

        if data is None:
            return {
                "ok": False, "error": "json_parse_failed",
                "prompt": reflect_prompt, "reflection": reflection,
                "data": None, "alpha": "", "key_slugs_str": "",
                "attr_map": {}, "new_factors": [], "summary": "",
                "per_match": {}, "key_slugs_list": [], "noise_slugs_list": [],
                "money_lesson": "", "extra_lids": extra_lids,
                "match_features": match_features, "seen_lids": seen_lids,
                "history_slugs": history_slugs, "history_block": history_block,
            }

        # ── 辅助：清洗因子名 ──
        def _clean_name(name: str) -> str:
            n = name.strip()
            # 去包裹引号（LLM 常把因子名带上引号输出）
            n = n.strip('"\'“”`')
            n = re.sub(r'[（(][^）)]*[）)]', '', n).strip()
            # 去 emoji / 符号（✅❌➖ 等易混入因子名）
            n = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]', '', n).strip()
            return n

        def _valid(name: str) -> bool:
            n = _clean_name(name)
            if not n or n.lower() in ("无", "none", "null", "n/a", "-", "无新因子"):
                return False
            if ":" in n:
                return False
            if n.lower().startswith("key_"):
                return False
            return True

        # ── 提取字段 ──
        alpha = ", ".join(data.get("alpha_factors", []))
        key_slugs_str = ", ".join(data.get("key_slugs", []))
        noise_slugs_str = ", ".join(data.get("noise_slugs", []))
        summary = data.get("reflection", clean[:200])

        desc_map: dict[str, str] = data.get("factor_desc", {}) or {}

        # per_match 作为 reflection 的上下文记录
        per_match = data.get("per_match", {}) or {}

        # factor_attribution: {"order_0": ["因子A"], ...} → attr_map
        attr_map: dict[int, list[str]] = {}
        raw_attr = data.get("factor_attribution", {}) or {}
        for key, factors in raw_attr.items():
            m = re.match(r'order_(\d+)', key)
            if m:
                idx = int(m.group(1))
                if 0 <= idx < len(settled):
                    attr_map[idx] = [
                        _clean_name(f) for f in (factors if isinstance(factors, list) else [factors])
                        if _valid(f)
                    ]

        # ── 更新 FactorMemory ──
        # 1. 归因驱动
        for idx, factors in attr_map.items():
            o = settled[idx]
            hit = o.get("hit")
            profit = o.get("profit", 0)
            for fn in factors:
                desc = desc_map.get(fn, "")
                role.memory.factors.record(
                    fn, hit, profit, desc=desc,
                    date=day_date, lota_id=o.get("lota_id", ""),
                    bet_size=o.get("bet_size", 0),
                )

        # 2. 兜底：LLM 提到但未归因的因子
        new_factors = []
        if alpha:
            all_attributed = set()
            for factors in attr_map.values():
                all_attributed.update(f.strip().lower() for f in factors)
            existing_names = {n.lower() for n in fp.keys()}
            new_factors = []
            for raw in [_clean_name(f) for f in data.get("alpha_factors", []) if _valid(f)]:
                fn_lower = raw.lower()
                is_dup = False
                for en in existing_names:
                    if fn_lower == en or fn_lower in en or en in fn_lower:
                        is_dup = True
                        break
                if not is_dup:
                    new_factors.append(raw)
                    existing_names.add(fn_lower)
            for factor_name in new_factors:
                if factor_name.lower() not in all_attributed:
                    role.memory.factors.record(
                        factor_name, None, 0, desc=desc_map.get(factor_name, ""),
                        date=day_date,
                    )

        # 2.5 保存新 Factor 模型
        if new_factors and save_fac:
            from src.store import save_factor
            from src.models import Factor
            for factor_name in new_factors:
                try:
                    save_factor(Factor(
                        id=f"fac_{factor_name.lower().replace(' ','_')[:40]}",
                        slugs=[s.strip() for s in key_slugs_str.split(",") if s.strip()],
                        content=desc_map.get(factor_name, summary[:300])
                    ))
                except Exception:
                    pass

        # slug 列表
        key_slugs_list = data.get("key_slugs", [])
        noise_slugs_list = data.get("noise_slugs", [])

        # 保存反思（带样本场数，低样本自动打标，防止后续分析锚定弱先例）
        role.memory.reflections.add_reflection(
            day_date, summary, sample_count=len(seen_lids)
        )
        if key_slugs_list or noise_slugs_list:
            slug_note = f"\n📡 有效slug: {', '.join(key_slugs_list) if key_slugs_list else '无'}"
            if noise_slugs_list:
                slug_note += f"\n🔇 噪声slug: {', '.join(noise_slugs_list)}"
            if attr_map:
                slug_note += f"\n📐 因子归因: {len(attr_map)} 笔订单已关联到因子"
            if role.memory.reflections.reflections:
                last = role.memory.reflections.reflections[-1]
                last["reflection"] = last.get("reflection", "") + slug_note
                role.memory.reflections._save()

        # money_lesson
        money_lesson = data.get("money_lesson", "")
        if money_lesson and role.memory.reflections.reflections:
            last = role.memory.reflections.reflections[-1]
            last["reflection"] = last.get("reflection", "") + f"\n💰 资金教训: {money_lesson}"
            role.memory.reflections._save()

    if alpha:
        print(f"  🧠 反思: alpha={alpha[:80]}{'...' if len(alpha)>80 else ''}")
    if attr_map:
        print(f"  📐 因子归因: {sum(len(v) for v in attr_map.values())} 次关联, {len(attr_map)} 笔订单")

    return {
        "ok": True, "error": None,
        "prompt": reflect_prompt, "reflection": reflection,
        "data": data, "alpha": alpha, "key_slugs_str": key_slugs_str,
        "attr_map": attr_map, "new_factors": new_factors,
        "summary": summary, "per_match": per_match,
        "key_slugs_list": key_slugs_list, "noise_slugs_list": noise_slugs_list,
        "money_lesson": money_lesson, "extra_lids": extra_lids,
        "match_features": match_features, "seen_lids": seen_lids,
        "history_slugs": history_slugs, "history_block": history_block,
    }


def node_reflect(state: AgentState) -> AgentState:
    """结算后反思 — 线上入口，包一层 run_reflect + 会话日志。"""
    import os

    rt = _rt(state)
    settled = rt.last_settled_orders
    day_date = state.get("day_date", "")

    if not settled or not rt.provider:
        print(f"  ⚠️ reflect 跳过: settled={len(settled)} provider={'有' if rt.provider else '无'}")
        return state

    # 新方案（历史同信号回顾）已切为默认开启；设 REFLECT_SLUG_HISTORY=0/off/false 可回退
    use_hist = os.environ.get("REFLECT_SLUG_HISTORY", "1").strip().lower() in (
        "1", "true", "on", "yes"
    )
    def _env_int(key: str, default: int) -> int:
        try:
            return int(os.environ.get(key, str(default)))
        except ValueError:
            return default
    hist_max = _env_int("REFLECT_SLUG_HISTORY_MAX", 8)
    hist_tokens = _env_int("REFLECT_SLUG_HISTORY_TOKENS", 3200)
    hist_days = _env_int("REFLECT_SLUG_HISTORY_DAYS", 90)
    extra_matches = os.environ.get("REFLECT_EXTRA_MATCHES", "1").strip().lower() in (
        "1", "true", "on", "yes"
    )
    res = run_reflect(
        settled, day_date, rt.role,
        provider=rt.provider, dm=rt.dm,
        use_slug_history=use_hist,
        slug_history_max=hist_max,
        slug_history_tokens=hist_tokens,
        slug_history_days=hist_days,
        extra_matches=extra_matches,
    )

    if rt.session:
        params = {
            "settled_count": len(settled),
            "day_date": day_date,
            "alpha": res["alpha"],
            "key_slugs": res["key_slugs_str"],
            "attributed_orders": len(res["attr_map"]) if res["reflection"] else 0,
            "extra_matches": res["extra_lids"],
        }
        if res.get("error"):
            params["error"] = res["error"]
        rt.session.reflect_call(
            reflect_prompt=res["prompt"],
            response=res["reflection"] if res["reflection"] else "(无反思输出)",
            params=params,
            match_features=res["match_features"],
        )

    return state


# ═══════════════════════════════════════════════
# Helpers — factor review
# ═══════════════════════════════════════════════

def _days_since(date_str: str, ref_date: str = "") -> int:
    """两个日期之间的天数差，date_str 无效时返回 999"""
    if not date_str:
        return 999
    try:
        end = date.fromisoformat(ref_date) if ref_date else date.today()
        start = date.fromisoformat(date_str[:10])
        return (end - start).days
    except Exception:
        return 999


def _get_recent_reflections(rt: AgentRuntime, days: int = 7, ref_date: str = "", start_date: str = "") -> list[dict]:
    """获取指定窗口内的反思记录。

    优先使用 start_date ~ ref_date 范围；未提供 start_date 时退回到 ref_date 往前 days 天。
    """
    from datetime import timedelta
    if not rt.role:
        return []
    try:
        end = date.fromisoformat(ref_date) if ref_date else date.today()
        if start_date:
            start = date.fromisoformat(start_date)
        else:
            start = end - timedelta(days=days)
        reflections = getattr(rt.role.memory.reflections, "reflections", []) or []
        return [
            r for r in reflections
            if start.isoformat() <= r.get("date", r.get("day_date", ""))[:10] <= end.isoformat()
        ]
    except Exception:
        return []


# ═══════════════════════════════════════════════
# Node — factor_review (weekly structural evaluation)
# ═══════════════════════════════════════════════

def node_factor_review(state: AgentState) -> AgentState:
    """每周因子结构性评估 — 外部控制调用频率。"""
    rt = _rt(state)
    if not rt.role or not rt.provider:
        print("  ⚠️ factor_review 跳过: role 或 provider 未初始化")
        return state

    print("\n  🔬 ── factor_review: 周度因子退役评估 ──")

    rt.role.memory.factors.load()
    fp = rt.role.memory.factors.factor_perf
    if not fp:
        print("  ⚠️ factor_review 跳过: 无因子数据")
        return state

    week_end = state.get("day_date", date.today().isoformat())
    review_start = state.get("review_start_date", "")
    user_notes = state.get("user_notes", "")

    # ── 代码门控: 14天零触发 → 自动 dormant ──
    auto_dormant: list[str] = []
    for fid, s in fp.items():
        if s.get("status") == "retired":
            continue
        if _days_since(s.get("last_seen", ""), week_end) > 14 and s.get("total", 0) > 0:
            rt.role.memory.factors.set_status(fid, "dormant")
            auto_dormant.append(fid)

    # ── 阶段4: 低信息因子确定性退役（既不赚钱也不亏大钱 + 来来回回≈掷硬币）──
    # 波动大/强方向的因子有信息保留；只有 |每单平均回报|≈0 且命中率在硬币区间才退役。
    LOW_INFO_MIN_SAMPLES = 5
    LOW_INFO_AVG_RETURN = 0.15
    LOW_INFO_HIT_LO, LOW_INFO_HIT_HI = 0.35, 0.65
    low_info_retired: list[str] = []
    for fid, s in fp.items():
        if s.get("status") == "retired":
            continue
        hist = s.get("history", []) or []
        if len(hist) < LOW_INFO_MIN_SAMPLES:
            continue
        rets = [h.get("return_ratio", 0) for h in hist]
        avg = sum(rets) / len(rets)
        denom = len(hist) - s.get("push", 0)
        hit_rate = s.get("hit", 0) / denom if denom > 0 else 0
        if abs(avg) < LOW_INFO_AVG_RETURN and LOW_INFO_HIT_LO <= hit_rate <= LOW_INFO_HIT_HI:
            rt.role.memory.factors.set_status(fid, "retired")
            low_info_retired.append(fid)

    # ── 构建评估候选（active + dormant，排除已退役）──
    candidates: dict[str, dict] = {}
    fp_fresh = rt.role.memory.factors.factor_perf  # 重新读（auto_dormant 可能已更新）
    for fid, s in fp_fresh.items():
        if s.get("status") == "retired":
            continue
        total = s.get("total", 0)
        hit = s.get("hit", 0)
        denom = total - s.get("push", 0)
        hit_rate_str = f"{hit / denom * 100:.0f}%" if denom > 0 else "无数据"
        candidates[fid] = {
            "status": s.get("status", "active"),
            "total": total,
            "hit_rate": hit_rate_str,
            "profit": s.get("profit", 0),
            "desc": s.get("desc", ""),
            "first_seen": s.get("first_seen", ""),
            "last_seen": s.get("last_seen", ""),
        }

    if not candidates:
        print("  ✅ factor_review: 无活跃因子需要评估")
        return state

    # ── 近期反思文本（作为结构性判断的观察证据）──
    recent_refs = _get_recent_reflections(rt, days=7, ref_date=week_end, start_date=review_start)
    window_desc = f"{review_start} ~ {week_end}" if review_start else f"近7天 (~{week_end})"
    # 🔧 清洗反思文本中的 verdict 图标，防止 LLM 把 ✅/❌/⇒赢/⇒输 抄进因子名
    def _sanitize_reflection(text: str) -> str:
        for icon in ("✅", "❌", "➖", "🔴", "⚫", "⬜"):
            text = text.replace(icon, "")
        return re.sub(r'⇒\s*\S+', '', text)  # 去掉 "⇒赢" "⇒输" 等后缀
    refs_text = "\n".join(
        f"  [{r.get('date', r.get('day_date', '?'))}] {_sanitize_reflection(r.get('reflection', '')[:300])}"
        for r in recent_refs
    ) if recent_refs else "(窗口内无反思记录)"

    candidates_text = "\n".join(
        f"  {fid} [{info['status']}]: {info['total']}次 命中{info['hit_rate']} "
        f"盈亏{info['profit']:+.0f} | 首见={info['first_seen']} 最近={info['last_seen']}\n"
        f"    定义: {info['desc'][:100] if info['desc'] else '(无描述)'}"
        for fid, info in sorted(candidates.items(), key=lambda x: -x[1]["total"])
    )

    persona_text = rt.role.persona_text() if rt.role else ""
    user_notes_text = (user_notes or "").strip()

    review_prompt = f"""你是量化足球博彩分析师，负责审查因子库健康度。

## 投注人设
{persona_text if persona_text else '(未设)'}

## 用户调整意见（回放/人工干预时注入，优先参考）
{user_notes_text if user_notes_text else '(无)'}

## 反思记录（评估窗口: {window_desc}，共 {len(recent_refs)} 条）
{refs_text}

## 待评估因子列表
{candidates_text}

## 评估原则
你的任务不是评估因子"赢了几次"，而是判断**因子的市场假设是否还成立**。

对每个因子，依次考量：
1. 这个因子的核心假设是什么？（从定义推断）
2. 近7天的反思中，这个假设是否被反复证伪？
3. 这个定价低效是否已被市场修正（信号已被博彩公司定价进去）？
4. 这个因子是否已被新发现的更精细因子完全替代？

判断结论三档：
- `retire`: 假设已被证伪，或市场已修正，或被更好因子完全替代
- `dormant`: 逻辑可能有效，但近期无触发场景，暂时休眠
- `active`: 保留继续使用（不需要列出，只列需要变更的）

⚠️ 保守原则：宁可多保留，不要误删。只有确实有结构性原因时才 retire。

输出格式 — 必须输出合法 JSON（使用 JSON Output 模式）：

⚠️ **因子名硬约束**：`retire` 和 `dormant` 中的每个名字必须与上方【待评估因子列表】中的因子名**逐字一致**。禁止添加 ✅ ❌ ➖ 等图标，禁止添加"⇒赢""⇒输"等后缀，禁止自创因子名。不匹配的名字会被系统忽略。

```json
{{
  "retire": ["因子A", "因子B"],
  "dormant": ["因子C"],
  "rationale": {{"因子A": "判断理由 ≤40字", "因子B": "判断理由 ≤40字"}}
}}
```"""

    try:
        response = rt.provider.call(
            review_prompt,
            [{"role": "user", "content": "按 JSON 格式输出。"}],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        print(f"  ⚠️ factor_review LLM 调用失败: {e}")
        response = ""

    # 记录 LLM 调用到 session（prompt + response），与 analyze 流程一致
    if response and rt.session:
        tokens_in = int(len(review_prompt) * 1.5)
        tokens_out = int(len(response) * 1.5)
        rt.session.llm_call(
            system_prompt=review_prompt,
            response=response,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    actually_retired: list[str] = []
    llm_dormant: list[str] = []

    if response and rt.role:
        clean = BaseLLMProvider.strip_thinking(response)

        # JSON 解析
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None
            else:
                data = None

        if data:
            # 🔧 后处理：清洗 LLM 可能混入的图标/后缀，尝试匹配真实因子名
            def _clean_factor_name(raw: str) -> str | None:
                """清洗 LLM 输出的因子名，返回匹配的 fp_fresh key 或 None"""
                raw = raw.strip()
                if raw in ("", "无", "none", "-"):
                    return None
                # 直接匹配
                if raw in fp_fresh:
                    return raw
                # 尝试清洗后匹配：去掉 verdict 图标 + ⇒后缀
                cleaned = raw
                for icon in ("✅", "❌", "➖", "🔴", "⚫", "⬜"):
                    cleaned = cleaned.replace(icon, "")
                cleaned = re.sub(r'⇒\s*\S+', '', cleaned).strip()
                if cleaned and cleaned in fp_fresh:
                    return cleaned
                # 尝试模糊匹配：去掉末尾括号/空格等
                cleaned2 = re.sub(r'[\s（(][^)）]*$', '', cleaned).strip()
                if cleaned2 and cleaned2 in fp_fresh:
                    return cleaned2
                return None  # 实在匹配不上，丢弃

            for fn in data.get("retire", []):
                matched = _clean_factor_name(fn)
                if matched:
                    rt.role.memory.factors.set_status(matched, "retired")
                    actually_retired.append(matched)

            for fn in data.get("dormant", []):
                matched = _clean_factor_name(fn)
                if matched and matched not in auto_dormant:
                    rt.role.memory.factors.set_status(matched, "dormant")
                    llm_dormant.append(matched)

        rt.role.save()

    if auto_dormant:
        print(f"  💤 自动休眠(14天零触发): {', '.join(auto_dormant)}")
    if low_info_retired:
        print(f"  🗑️ 低信息退役({len(low_info_retired)}): {', '.join(low_info_retired)}")
    if llm_dormant:
        print(f"  💤 LLM建议休眠: {', '.join(llm_dormant)}")
    if actually_retired:
        print(f"  🪦 结构性退役: {', '.join(actually_retired)}")
    if not auto_dormant and not llm_dormant and not actually_retired:
        print(f"  ✅ factor_review: {len(candidates)} 个因子前提有效，无需调整")

    if rt.session:
        rt.session.tool_call("factor_review", {
            "window": window_desc,
            "reflection_count": len(recent_refs),
            "candidates": len(candidates),
            "auto_dormant": auto_dormant,
            "retired": actually_retired,
            "dormant": auto_dormant + llm_dormant,
        }, f"评估 {len(candidates)} 因子: 退役 {len(actually_retired)} 休眠 {len(auto_dormant) + len(llm_dormant)}")

    return state

def should_call_llm(state: AgentState) -> str:
    # 只要注入了 provider 就调 LLM（live 与回放/历史回测一致）。
    # 旧逻辑要求 live=True：导致 runall --no-live 历史回放永远跳过 LLM → 0 订单。
    if _rt(state).provider:
        return "call_llm"
    return "parse_orders"


def has_matches(state: AgentState) -> str:
    if state.get("matches"):
        return "strip_scores"
    return END


# ═══════════════════════════════════════════════
# Graph builders
# ═══════════════════════════════════════════════

def build_analyze_graph() -> StateGraph:
    """构建 analyze_day 子图"""
    g = StateGraph(AgentState)

    g.add_node("load_role", node_load_role)
    g.add_node("fetch_matches", node_fetch_matches)
    g.add_node("strip_scores", node_strip_scores)
    g.add_node("fetch_features", node_fetch_features)
    g.add_node("build_prompt", node_build_prompt)
    g.add_node("call_llm", node_call_llm)
    g.add_node("parse_orders", node_parse_orders)
    g.add_node("place_orders", node_place_orders)

    g.set_entry_point("load_role")
    g.add_edge("load_role", "fetch_matches")

    g.add_conditional_edges("fetch_matches", has_matches, {
        "strip_scores": "strip_scores",
        END: END,
    })
    g.add_edge("strip_scores", "fetch_features")
    g.add_edge("fetch_features", "build_prompt")

    g.add_conditional_edges("build_prompt", should_call_llm, {
        "call_llm": "call_llm",
        "parse_orders": "parse_orders",
    })
    g.add_edge("call_llm", "parse_orders")
    g.add_edge("parse_orders", "place_orders")
    g.add_edge("place_orders", END)

    return g.compile()


def build_settle_graph() -> StateGraph:
    """构建 settle_day 子图（结算 + 反思）"""
    g = StateGraph(AgentState)

    g.add_node("load_role", node_load_role)
    g.add_node("load_unsettled", node_load_unsettled)
    g.add_node("fetch_scores", node_fetch_scores)
    g.add_node("settle_orders", node_settle_orders)
    g.add_node("reflect", node_reflect)

    g.set_entry_point("load_role")
    g.add_edge("load_role", "load_unsettled")
    g.add_edge("load_unsettled", "fetch_scores")
    g.add_edge("fetch_scores", "settle_orders")
    g.add_edge("settle_orders", "reflect")
    g.add_edge("reflect", END)

    return g.compile()


def build_factor_review_graph() -> StateGraph:
    """构建每周因子评估子图 — 外部按周调用"""
    g = StateGraph(AgentState)

    g.add_node("load_role", node_load_role)
    g.add_node("factor_review", node_factor_review)

    g.set_entry_point("load_role")
    g.add_edge("load_role", "factor_review")
    g.add_edge("factor_review", END)

    return g.compile()


# ═══════════════════════════════════════════════
# Agent (LangGraph 包装)
# ═══════════════════════════════════════════════

class Agent:
    """
    博彩 Agent — LangGraph 实现。

    用法:
      agent = Agent(user='jy')
      agent.register_llm()

      # analyze
      result = agent.analyze('2026-06-11', live=True)

      # settle
      result = agent.settle('2026-06-11')

      # status
      print(agent.status())
    """

    def __init__(self, user: str = "default"):
        self.user = user
        self._analyze_graph = build_analyze_graph()
        self._settle_graph = build_settle_graph()
        self._factor_review_graph = build_factor_review_graph()

    def set_provider(self, provider: BaseLLMProvider):
        """设置 LLM provider（如 DeepSeekProvider）。"""
        _rt({"user": self.user}).provider = provider

    def init_role(self, capital: float = 10000):
        """初始化角色"""
        rt = _rt({"user": self.user})
        try:
            rt.role = Role.load(self.user)
        except (FileNotFoundError, ValueError):
            rt.role = Role(name=self.user, capital=capital)
            rt.role.save()

    def analyze(self, day_date: str, live: bool = False, jingcai_only: bool = False,
                prefetched: bool = False) -> dict:
        """分析一天 → 返回 orders。

        live=True 时：
          1. 先 refresh_orders（退回未开赛订单，保留已开赛）
          2. 再调用 LLM 分析下单
        prefetched=True 时：比赛列表/compact-fet/tags 已由外部预取，跳过强制刷新。
        """
        # live 模式：自动刷新当天订单
        if live:
            self.refresh_orders(day_date)

        session = self._begin_session("analyze", day_date)

        try:
            state: AgentState = {
                "user": self.user,
                "day_date": day_date,
                "live": live,
                "jingcai_only": jingcai_only,
                "prefetched": prefetched,
            }
            # live 模式：禁止 get_compact_fet 回退过期缓存（旧赔率进提示词）
            DataManager().set_live_mode(live)
            result = self._analyze_graph.invoke(state)
        finally:
            DataManager().set_live_mode(False)
            self._end_session(session)

        return {
            "date": day_date,
            "matches_count": len(result.get("matches", [])),
            "prompt_tokens": result.get("prompt", {}).get("token_count", 0),
            "orders": result.get("orders", []),
            "placed": result.get("placed_count", 0),
            "llm_response": result.get("llm_response", ""),
            "session_path": str(session._path),
        }

    def settle(self, day_date: str = None, jingcai_only: bool = False) -> dict:
        """结算未结算订单"""
        session = self._begin_session("settle", day_date or "all")

        try:
            state: AgentState = {
                "user": self.user,
                "day_date": day_date or "",
                "jingcai_only": jingcai_only,
            }
            result = self._settle_graph.invoke(state)
            settlement = result.get("settlement", {"settled": 0})
            session.settlement(settlement)
        finally:
            self._end_session(session)

        return settlement

    def factor_review(self, end_date: str, start_date: str = "", user_notes: str = "") -> dict:
        """因子结构性评估 — 由外部控制调用时机。

        Args:
            end_date:   评估窗口结束日（通常是今天或最后一个结算日）
            start_date: 评估窗口起始日（空=自动取 end_date 往前7天）。
                        首次执行可传入最早的运行日期，覆盖全部历史反思。
            user_notes: 用户调整意见（回放/人工干预时注入评估 prompt，调整人设/因子思考方向）。

        Example:
            agent.factor_review("2026-07-07")                     # 近7天
            agent.factor_review("2026-07-07", start_date="2026-06-11")  # 全量历史
        """
        session = self._begin_session("factor_review", end_date)
        try:
            state: AgentState = {
                "user": self.user,
                "day_date": end_date,
                "review_start_date": start_date,
                "user_notes": user_notes,
            }
            self._factor_review_graph.invoke(state)
        finally:
            self._end_session(session)
        return {"end_date": end_date, "start_date": start_date or "(auto-7d)", "user_notes": user_notes, "user": self.user, "status": "ok"}

    def _ensure_role(self):
        rt = _rt({"user": self.user})
        if rt.role is None:
            try:
                rt.role = Role.load(self.user)
            except (FileNotFoundError, ValueError):
                rt.role = Role(name=self.user, capital=10000)
                rt.role.save()

    def _begin_session(self, action: str, day_date: str) -> SessionLogger:
        """开始 session 并注入 runtime。用 try-finally 确保清理。"""
        rt = _rt({"user": self.user})
        self._ensure_role()

        session = SessionLogger(self.user)
        session.start(action, day_date or "", rt.role.capital)
        rt.session = session
        return session

    def _end_session(self, session: SessionLogger):
        """结束 session，写入文件并清理 runtime 引用。"""
        rt = _rt({"user": self.user})
        try:
            session.finish(rt.role.capital, rt.role.stats() if rt.role else None)
        finally:
            rt.session = None

    def refresh_orders(self, day_date: str) -> dict:
        """
        刷新当天订单组：退回未开赛订单金额，保留已开赛订单，然后重新分析。

        逻辑:
          1. 找出 day_date 足球日窗口内的所有未结算订单
          2. 对于每单，检查比赛 match_time:
             - match_time > now: 比赛未开始 → 退回 bet_size 到余额，删除订单
             - match_time <= now: 比赛已开始 → 保留不动
          3. 返回刷新统计
        """
        from datetime import datetime as _dt, date as _date, timedelta

        rt = _rt({"user": self.user})
        self._ensure_role()

        dm = DataManager()
        now = _now_bj("%Y-%m-%d %H:%M")

        # 足球日窗口
        d = _date.fromisoformat(day_date)
        window_start = f"{day_date} 12:01"
        next_day = (d + timedelta(days=1)).isoformat()
        window_end = f"{next_day} 12:00"

        orders = rt.role.get_orders()
        pending = [o for o in orders if not o.get("settled_at")]

        def _norm_time(t: str) -> str:
            """统一 match_time 到 YYYY-MM-DD HH:MM 格式用于比较"""
            if not t:
                return ""
            return t.replace("T", " ")[:16]

        refunded = 0
        kept = 0
        total_refund = 0.0

        for o in pending:
            lid = o.get("lota_id", "")
            m = dm.get_cached_match(lid) or {}
            mt = _norm_time(m.get("match_time", ""))

            # 判断是否在当天足球日窗口内
            in_window = window_start <= mt <= window_end if mt else False
            if not in_window:
                continue

            bet_size = float(o.get("bet_size", 0))

            # 比赛是否已开始（now > match_time → 已开赛不可退）
            match_started = mt <= now if mt else False

            if match_started:
                # 已开赛 → 保留
                kept += 1
                print(f"  🔒 保留 | {m.get('home_name','?')} vs {m.get('away_name','?')} "
                      f"({mt}) | {o.get('bet_type')} {o.get('pick')} bet{bet_size:.0f} — 已开赛")
            else:
                # 未开赛 → 退回金额，删除订单
                rt.role.deposit(bet_size)
                rt.role.remove_order(o.get("id", ""))
                refunded += 1
                total_refund += bet_size
                print(f"  ↩ 退回 | {m.get('home_name','?')} vs {m.get('away_name','?')} "
                      f"({mt}) | {o.get('bet_type')} {o.get('pick')} bet{bet_size:.0f} — 未开赛")

        rt.role.save()

        print(f"\n  💰 刷新完成: 退回 {refunded} 单 ¥{total_refund:,.0f} | "
              f"保留 {kept} 单 (已开赛) | 余额: ¥{rt.role.capital:,.0f}")

        return {
            "day": day_date,
            "window": f"{window_start} ~ {window_end}",
            "refunded": refunded,
            "kept": kept,
            "total_refund": total_refund,
            "capital": rt.role.capital,
        }

    def run_day(self, day_date: str, live: bool = False, jingcai_only: bool = False) -> dict:
        """完整一天: 结算 + 分析（含 slug 表现回填）"""
        from datetime import date as _date, timedelta

        settlement = self.settle(day_date, jingcai_only=jingcai_only)

        # 回填前一天的 slug PnL
        rt = _rt({"user": self.user})
        prev_date = (
            _date.fromisoformat(day_date) - timedelta(days=1)
        ).isoformat()
        if rt.role and hasattr(rt.role.memory, 'slugs'):
            rt.role.memory.slugs.record_day_pnl(
                prev_date,
                settlement.get("pnl", 0.0)
            )

        analysis = self.analyze(day_date, live=live, jingcai_only=jingcai_only)

        # 记录资金快照（结算后）
        if rt.role:
            rt.role.record_capital_snapshot(day_date)

        return {"date": day_date, "settlement": settlement, "analysis": analysis}

    def _get_match_info(self, lota_id: str) -> dict:
        """查找比赛基本信息（供外部展示用）"""
        dm = DataManager()
        m = dm.get_cached_match(lota_id) or {}
        return {
            "home": m.get("home_name", "") or m.get("home", ""),
            "away": m.get("away_name", "") or m.get("away", ""),
            "league": m.get("league_name", "") or m.get("league", ""),
        }

    def _get_capital(self) -> float:
        """获取当前资金"""
        self._ensure_role()
        rt = _rt({"user": self.user})
        return rt.role.capital if rt.role else 0

    def _get_active_factor_count(self) -> int:
        """当前活跃（未退役）因子数，供回放轨迹报告用。"""
        self._ensure_role()
        rt = _rt({"user": self.user})
        if not rt.role:
            return 0
        try:
            rt.role.memory.factors.load()
            fp = rt.role.memory.factors.factor_perf
            return sum(1 for s in fp.values() if s.get("status", "active") != "retired")
        except Exception:
            return 0

    def _get_unsettled_orders(self) -> list:
        """获取未结算订单"""
        self._ensure_role()
        rt = _rt({"user": self.user})
        return rt.role.get_orders() if rt.role else []

    def status(self) -> str:
        """角色状态"""
        rt = _rt({"user": self.user})
        if rt.role is None:
            try:
                rt.role = Role.load(self.user)
            except (FileNotFoundError, ValueError):
                return "(未初始化)"

        role = rt.role
        s = role.stats()
        lines = [
            f"用户: {self.user}",
            f"资金: {role.initial_capital:.0f} → {role.capital:.0f} "
            f"(PnL {role.pnl():+.0f}, {role.pnl()/role.initial_capital*100:+.1f}%)",
            f"订单: {s['total_orders']} 总 / {s['settled']} 已结算 / {s['pending']} 待定",
        ]
        if s['total_bet'] > 0:
            lines.append(f"总投注: {s['total_bet']:.0f}  总返还: {s['total_return']:.0f}  ROI: {s['roi']:+.1f}%")
        for bt, st in s.get("by_type", {}).items():
            lines.append(f"  {bt}: {st['total']}单 命中{st['hit_rate']}% 盈亏{st['profit']:+.0f}  ROI{st['roi']:+.1f}%")

        # ── 因子状态 ──
        role.memory.factors.load()
        fp = role.memory.factors.factor_perf
        if fp:
            active = {k: v for k, v in fp.items() if v.get("status", "active") != "retired"}
            retired = {k: v for k, v in fp.items() if v.get("status", "active") == "retired"}
            if active:
                lines.append("")
                lines.append("📐 活跃因子:")
                for fid, s2 in sorted(active.items(), key=lambda x: -x[1]["total"]):
                    denom = s2["total"] - s2["push"]
                    rate = f"{s2['hit']/denom*100:.0f}%" if denom > 0 else "-"
                    desc = s2.get("desc", "")
                    desc_str = f" — {desc[:80]}" if desc else ""
                    first = s2.get("first_seen", "")
                    last = s2.get("last_seen", "")
                    date_str = f" [{first}" + (f"~{last}" if last != first else "") + "]" if first else ""
                    lines.append(f"  {fid}{date_str}: {s2['total']}次 命中{rate} 盈亏{s2['profit']:+.0f}{desc_str}")
            if retired:
                lines.append(f"  🪦 退役: {', '.join(retired.keys())}")

        return "\n".join(lines)
