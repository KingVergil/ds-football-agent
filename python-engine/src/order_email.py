"""订单邮件构建 & 变化检测。

核心入口：`send_order_email(agent_name, football_day)` ——
1. 从 role JSON 加载未结算订单
2. 按当前足球日过滤
3. 解析队名（lota_id → home/away name）
4. 检测较上次发送的变化
5. 构建邮件正文 → 发送 → 保存快照
"""

import json
import sys
from pathlib import Path
from collections import OrderedDict
from datetime import date, datetime, timedelta

from . import tools
from .environment import get_football_day
from .email_sender import send_email

SNAPSHOT_DIR = Path(__file__).parent.parent / "data" / "email_snapshots"
ROLES_DIR = Path(__file__).parent.parent / "data" / "roles"

# 距开赛超过该小时数的比赛，其预测视为「可能变」（尚未定型）
PROVISIONAL_HOURS = 3


# ═══════════════════════════════════════════════
# 订单加载
# ═══════════════════════════════════════════════

def _load_role_json(agent_name: str) -> dict:
    """加载角色 JSON 文件。"""
    path = ROLES_DIR / agent_name / f"{agent_name}.json"
    if not path.exists():
        print(f"[order_email] 角色文件不存在: {path}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def get_pending_orders(agent_name: str, day_start: str, day_end: str) -> list[dict]:
    """
    获取指定 agent 在当前足球日窗口内的未结算订单。

    返回列表，每项包含：
      lota_id, bet_type, pick, handicap, bet_size, reason,
      home_name, away_name, league_name, match_time
    按 match_time 升序排列。
    """
    role = _load_role_json(agent_name)
    orders = role.get("orders", [])
    if not orders:
        return []

    pending = []
    for o in orders:
        # 已结算的跳过
        if o.get("settled_at"):
            continue
        # 空盘跳过
        if not o.get("bet_type"):
            continue

        lota_id = o.get("lota_id", "")
        match = tools.lookup_match(lota_id)
        if not match:
            # 无法解析的跳过（可能数据还没拉）
            continue

        match_time = match.get("match_time", "")
        # 只保留在当前足球日窗口内的
        if not (day_start <= match_time <= day_end):
            continue

        pending.append({
            "lota_id": lota_id,
            "bet_type": o.get("bet_type", ""),
            "pick": o.get("pick", ""),
            "handicap": o.get("handicap"),
            "odds": o.get("odds"),
            "bet_size": o.get("bet_size", 0),
            "reason": o.get("reason", ""),
            "legs": o.get("legs", []),
            "home_name": match.get("home_name", ""),
            "away_name": match.get("away_name", ""),
            "league_name": match.get("league_name", ""),
            "match_time": match_time,
            "state": match.get("state", 0),
            "score": match.get("score", ""),
        })

    # 两段排序：进行中（按时间从近到远）→ 已结束（沉底）
    now = datetime.now()
    active, finished = [], []
    for o in pending:
        state = o.get("state", 0)
        if state == 6:
            finished.append(o)
            continue
        # 开始时间 + 2h < 现在 也视为已结束
        try:
            mt = datetime.strptime(o["match_time"], "%Y-%m-%d %H:%M:%S")
            if mt + timedelta(hours=2) < now:
                finished.append(o)
                continue
        except ValueError:
            pass
        active.append(o)

    active.sort(key=lambda x: x["match_time"])
    finished.sort(key=lambda x: x["match_time"])
    return active + finished


# ═══════════════════════════════════════════════
# 变化检测 & 快照
# ═══════════════════════════════════════════════

def _snapshot_path(agent_name: str) -> Path:
    """快照文件路径。"""
    return SNAPSHOT_DIR / f"{agent_name}_latest.json"


def load_snapshot(agent_name: str) -> dict | None:
    """加载上一次发送的快照。"""
    path = _snapshot_path(agent_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_snapshot(agent_name: str, football_day: str, orders: list[dict]) -> None:
    """保存本次发送的快照（只存 key 字段）。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "football_day": football_day,
        "orders": [
            {
                "lota_id": o["lota_id"],
                "pick": o["pick"],
                "handicap": o.get("handicap"),
                "odds": o.get("odds"),
                "bet_size": o.get("bet_size"),
            }
            for o in orders
        ],
    }
    _snapshot_path(agent_name).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def detect_changes(
    current_orders: list[dict],
    snapshot: dict | None,
    settled_ids: set | None = None,
) -> dict:
    """
    对比当前订单和快照，检测变化。

    返回:
      {
        "new": [...],              # 新增订单
        "changed": [               # 有变化的订单
          {"order": ..., "old_pick": "H", "old_handicap": -0.5, "old_odds": 0.95},
        ],
        "has_changes": bool
      }
    """
    result = {"new": [], "changed": [], "removed": [], "has_changes": False}
    settled_ids = settled_ids or set()

    if snapshot is None:
        return result

    snapshot_map = {s["lota_id"]: s for s in snapshot.get("orders", [])}
    current_ids = {o["lota_id"] for o in current_orders}

    # 检测移除：上次快照有、当前不在 pending 中的
    for sid, s in snapshot_map.items():
        # 订单被"结算"后才离开 pending → 不是取消，不标"取消/不下"
        if sid not in current_ids and sid not in settled_ids:
            result["removed"].append(s)
            result["has_changes"] = True

    for o in current_orders:
        lid = o["lota_id"]
        if lid not in snapshot_map:
            result["new"].append(o)
            result["has_changes"] = True
        else:
            old = snapshot_map[lid]
            changed = False
            entry = {"order": o}
            if old.get("pick") != o.get("pick"):
                entry["old_pick"] = old.get("pick", "")
                changed = True
            if old.get("handicap") != o.get("handicap"):
                entry["old_handicap"] = old.get("handicap")
                changed = True
            if old.get("odds") != o.get("odds"):
                entry["old_odds"] = old.get("odds")
                changed = True
            if old.get("bet_size") != o.get("bet_size"):
                entry["old_bet_size"] = old.get("bet_size")
                changed = True
            if changed:
                result["changed"].append(entry)
                result["has_changes"] = True

    return result


# ═══════════════════════════════════════════════
# 邮件正文
# ═══════════════════════════════════════════════

_SPF_MAP = {"H": "主胜", "D": "平", "A": "客胜"}  # 胜平负 pick → 中文


def _pick_display(order: dict) -> str:
    """将 pick 转为显示文本。让球→队名，胜平负→主胜/平/客胜，大小球保留原值。"""
    pick = order["pick"]
    bet_type = order.get("bet_type", "")
    if bet_type in ("胜平负", "让球胜平负"):
        return _SPF_MAP.get(pick, pick)
    home = order.get("home_name", "")
    away = order.get("away_name", "")
    if pick == "H":
        return home
    elif pick == "A":
        return away
    return pick


def _old_pick_display(old_pick: str, order: dict) -> str:
    """将旧 pick 转为显示文本。"""
    bet_type = order.get("bet_type", "")
    if bet_type in ("胜平负", "让球胜平负"):
        return _SPF_MAP.get(old_pick, old_pick)
    if old_pick == "H":
        return order.get("home_name", "H")
    elif old_pick == "A":
        return order.get("away_name", "A")
    return old_pick


def _fmt_hc(hc) -> str:
    """格式化让球：保留符号，去冗余小数，修复 -0.0。"""
    if hc is None:
        return "-"
    # 消除负零
    if abs(hc) < 0.001:
        hc = 0.0
    if hc == int(hc):
        return f"{hc:+.0f}"
    return f"{hc:+.2f}"


def _is_provisional(order: dict, now: datetime) -> bool:
    """距开赛超过 PROVISIONAL_HOURS 小时且未完场 → 该预测后续可能调整（标注「可能变」）。"""
    if order.get("state", 0) == 6:
        return False
    try:
        mt = datetime.strptime(order["match_time"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return False
    return mt > now + timedelta(hours=PROVISIONAL_HOURS)


def _match_label(order: dict) -> str:
    """比赛标签：主队 vs 客队 (MM-DD HH:MM)。"""
    home = order.get("home_name", "?")
    away = order.get("away_name", "?")
    mt = (order.get("match_time", "") or "")[5:16]
    return f"{home} vs {away}" + (f" <span style='color:#999'>({mt})</span>" if mt else "")


def _build_change_summary(changes: dict, capital) -> str:
    """构建「本封更新」摘要 HTML：清晰写明哪些比赛变更/取消。无变更则返回空串。"""
    changed = changes.get("changed", [])
    removed = changes.get("removed", [])
    if not changed and not removed:
        return ""

    items = []
    for c in changed:
        o = c["order"]
        parts = []
        if c.get("old_pick") is not None:
            parts.append(f"选择 {_old_pick_display(c['old_pick'], o)} → {_pick_display(o)}")
        if c.get("old_handicap") is not None:
            parts.append(f"让球 {_fmt_hc(c['old_handicap'])} → {_fmt_hc(o.get('handicap'))}")
        if c.get("old_odds") is not None:
            parts.append(f"赔率 {c['old_odds']} → {o.get('odds') or '-'}")
        if c.get("old_bet_size") is not None and capital and capital > 0:
            old_pct = f"{c['old_bet_size'] / capital * 100:.1f}%"
            new_pct = f"{o.get('bet_size', 0) / capital * 100:.1f}%"
            parts.append(f"仓位 {old_pct} → {new_pct}")
        items.append(("变更", "#e67e22", _match_label(o), "；".join(parts)))

    for r in removed:
        lid = r.get("lota_id", "")
        match = tools.lookup_match(lid) or {}
        label = f"{match.get('home_name', '?')} vs {match.get('away_name', '?')}"
        items.append(("取消", "#e74c3c", label, "取消 / 不下"))

    rows = ""
    for badge, color, label, detail in items:
        rows += (
            f'<div style="margin:4px 0">'
            f'<span style="background:{color};color:#fff;font-size:11px;padding:1px 7px;border-radius:3px">{badge}</span> '
            f'{label} <span style="color:#999">·</span> {detail}</div>'
        )

    return f"""
    <div style="background:#fdf2f2;border:1px solid #f0b0b0;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#8a4b4b">
        📩 <b>本封更新（相对上一封 {len(changed) + len(removed)} 处）：</b>{rows}
    </div>"""


def build_email_body(
    agent_name: str,
    football_day: str,
    orders: list[dict],
    changes: dict,
) -> str:
    """构建HTML邮件正文（富文本表格，变化内联在主表行下方）。"""

    # 变化查找表
    new_ids = {o["lota_id"] for o in changes.get("new", [])}
    changed_map = {c["order"]["lota_id"]: c for c in changes.get("changed", [])}

    # ── 计算仓位百分比（需要资金数据）──
    # role.capital 是已扣下注后的可用余额，总资金 = 可用 + 当前已锁
    capital = None
    try:
        role_path = ROLES_DIR / agent_name / f"{agent_name}.json"
        if role_path.exists():
            role_data = json.loads(role_path.read_text(encoding="utf-8"))
            pending_bets = sum(o.get("bet_size", 0) for o in orders)
            capital = (role_data.get("capital") or 0) + pending_bets
    except Exception:
        pass

    now = datetime.now()
    rows_html = ""
    row_idx = 0
    separator_inserted = False
    for i, o in enumerate(orders, 1):
        lid = o["lota_id"]

        # 进入已结束段前插入分隔行
        if not separator_inserted:
            state = o.get("state", 0)
            is_finished = (state == 6)
            if not is_finished:
                try:
                    mt = datetime.strptime(o["match_time"], "%Y-%m-%d %H:%M:%S")
                    if mt + timedelta(hours=2) < now:
                        is_finished = True
                except ValueError:
                    pass
            if is_finished:
                finished_count = sum(1 for x in orders[i-1:])  # 包括当前
                rows_html += f"""
        <tr style="background:#eee">
            <td colspan="10" style="padding:6px 10px;font-size:12px;color:#888;text-align:center">
                ── 已结束 ({finished_count} 场) ──
            </td>
        </tr>"""
                separator_inserted = True

        row_idx += 1
        is_finished_row = separator_inserted
        bg = "#fafafa" if is_finished_row else ("#f9f9f9" if row_idx % 2 == 1 else "#ffffff")
        text_color = "#aaa" if is_finished_row else "#333"

        match_time_short = o["match_time"][5:16]
        league = o.get("league_name", "?")
        legs = o.get("legs") or []
        is_parlay = o.get("bet_type") == "串关" and bool(legs)
        if is_parlay:
            # 串关：每腿一行展示（对阵/选择/让球），赔率为整票连乘
            home = "<br>".join(
                f"{l.get('home_name','?')} vs {l.get('away_name','?')}"
                + (f" <span style='color:#999;font-size:11px'>{l.get('score','')}</span>" if l.get("score") else "")
                for l in legs
            )
            away = ""
            def _pick_cn(leg: dict) -> str:
                """腿方向中文：让球盘 → 让胜/让平/让负；不让球 → 胜/平/负。"""
                pk = leg.get("pick", "?")
                gl = leg.get("goal_line")
                if not isinstance(gl, (int, float)):
                    return pk
                if gl == 0:
                    return {"H": "胜", "D": "平", "A": "负"}.get(pk, pk)
                return {"H": "让胜", "D": "让平", "A": "让负"}.get(pk, pk)
            pick = "<br>".join(_pick_cn(l) for l in legs)
            hc_str = "<br>".join(_fmt_hc(l.get("goal_line")) for l in legs)
        else:
            home = o.get("home_name", "?")
            away = o.get("away_name", "?")
            pick = _pick_display(o)
            hc_str = _fmt_hc(o.get("handicap"))
        odds = o.get("odds") or "-"
        score = o.get("score", "")
        score_str = f' <span style="color:#999;font-size:11px">{score}</span>' if score else ""

        new_badge = '<span style="background:#27ae60;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;margin-left:4px">NEW</span>' if lid in new_ids else ""

        provisional_badge = (
            '<span style="background:#f39c12;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;margin-left:4px" '
            f'title="距开赛超过{PROVISIONAL_HOURS}小时，预测后续可能调整或取消">可能变</span>'
        ) if _is_provisional(o, now) else ""

        pct = f"{o.get('bet_size', 0) / capital * 100:.1f}%" if capital and capital > 0 else "-"
        rows_html += f"""
        <tr style="background:{bg}">
            <td style="padding:6px 8px;text-align:center;color:{'#bbb' if is_finished_row else '#999'}">{i}{new_badge}</td>
            <td style="padding:6px 6px;white-space:nowrap;color:{text_color}">{match_time_short}{provisional_badge}</td>
            <td style="padding:6px 6px;color:{'#bbb' if is_finished_row else '#666'}">{league}</td>
            <td style="padding:6px 6px;text-align:right;max-width:80px;color:{text_color}">{home}</td>
            <td style="padding:6px 2px;text-align:center;color:#ccc">vs</td>
            <td style="padding:6px 6px;max-width:80px;color:{text_color}">{away}{score_str}</td>
            <td style="padding:6px 12px;font-weight:bold;color:{'#bbb' if is_finished_row else '#c0392b'};min-width:90px">{pick}</td>
            <td style="padding:6px 12px;text-align:center;min-width:60px;color:{text_color}">{hc_str}</td>
            <td style="padding:6px 12px;text-align:center;min-width:60px;color:{text_color}">{odds}</td>
            <td style="padding:6px 12px;text-align:center;min-width:50px;font-weight:bold;color:{text_color}">{pct}</td>
        </tr>"""

        # 变化子行：选择/让球/赔率 各列显示旧值
        if lid in changed_map:
            row_idx += 1
            c = changed_map[lid]
            sub_bg = "#fff5f5"

            # 选择列
            old_pick = c.get("old_pick")
            if old_pick is not None:
                pick_cell = f'{_old_pick_display(old_pick, o)} → {_pick_display(o)}'
            else:
                pick_cell = ""

            # 让球列
            old_hc = c.get("old_handicap")
            if old_hc is not None:
                hc_cell = f'{_fmt_hc(old_hc)} → {_fmt_hc(o.get("handicap"))}'
            else:
                hc_cell = ""

            # 赔率列
            old_odds = c.get("old_odds")
            if old_odds is not None:
                odds_cell = f'{old_odds} → {o.get("odds") or "-"}'
            else:
                odds_cell = ""

            # 仓位列 (bet_size 变动 → 仓位%变化)
            old_bet = c.get("old_bet_size")
            if old_bet is not None and capital and capital > 0:
                old_pct = f"{old_bet / capital * 100:.1f}%"
                new_pct = f"{o.get('bet_size', 0) / capital * 100:.1f}%"
                pct_cell = f"{old_pct} → {new_pct}"
            else:
                pct_cell = ""

            rows_html += f"""
        <tr style="background:{sub_bg};font-size:11px;color:#e74c3c">
            <td style="padding:1px 8px;text-align:center">↳</td>
            <td style="padding:1px 6px" colspan="5"></td>
            <td style="padding:1px 12px">{pick_cell}</td>
            <td style="padding:1px 12px;text-align:center">{hc_cell}</td>
            <td style="padding:1px 12px;text-align:center">{odds_cell}</td>
            <td style="padding:1px 12px;text-align:center">{pct_cell}</td>
        </tr>"""

    # ── 计算近 3h 订单数 & 「可能变」数量 ──
    two_h = now + timedelta(hours=PROVISIONAL_HOURS)
    near_count = 0
    provisional_count = 0
    for o in orders:
        if _is_provisional(o, now):
            provisional_count += 1
        try:
            mt = datetime.strptime(o["match_time"], "%Y-%m-%d %H:%M:%S")
            if mt <= two_h:
                near_count += 1
        except ValueError:
            pass
    time_hint = f"近{PROVISIONAL_HOURS}h {near_count}单" if near_count > 0 else f"剩余 {len(orders)}单"

    # ── 顶部提示：说明「可能变」比赛 ──
    if provisional_count > 0:
        hint_html = (
            f'⏰ <b>提示：</b>有 <b>{provisional_count} 场</b>比赛距开赛超过 {PROVISIONAL_HOURS} 小时，'
            f'表中已标「<span style="color:#b9770e">可能变</span>」：<b>预测后续可能调整或取消</b>，'
            '请以临近该场开赛时点的邮件为准。'
        )
    else:
        hint_html = (
            f'⏰ <b>提示：</b>所有待结算比赛距开赛均在 {PROVISIONAL_HOURS} 小时内，预测已进入锁定阶段。'
        )

    # ── 本封更新摘要（变更 / 取消）──
    summary_html = _build_change_summary(changes, capital)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,SF Pro Display,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#333;max-width:900px;margin:0 auto;padding:20px">

    {summary_html}

    <div style="background:#fff8e1;border:1px solid #f0c36d;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#7a5c00">
        {hint_html}
    </div>

    <div style="margin-bottom:16px">
        <span style="font-size:18px;font-weight:bold">{agent_name}</span>
        <span style="color:#999;margin:0 8px">·</span>
        <span>足球日 {football_day}</span>
        <span style="color:#999;margin:0 8px">·</span>
        <span style="color:#e67e22">{time_hint} 待结算</span>
    </div>

    <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead>
            <tr style="background:#2c3e50;color:#fff">
                <th style="padding:8px 10px;text-align:center">#</th>
                <th style="padding:8px 10px;text-align:left">时间</th>
                <th style="padding:8px 10px;text-align:left">联赛</th>
                <th style="padding:8px 10px;text-align:right">主队</th>
                <th style="padding:8px 4px"></th>
                <th style="padding:8px 10px;text-align:left">客队</th>
                <th style="padding:8px 10px;text-align:left">选择</th>
                <th style="padding:8px 10px;text-align:center">让球</th>
                <th style="padding:8px 10px;text-align:center">赔率</th>
                <th style="padding:8px 10px;text-align:center">仓位</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>

"""

    # ── 已移除订单 ──
    removed = changes.get("removed", [])
    if removed:
        removed_rows = ""
        for r in removed:
            lid = r.get("lota_id", "")
            match = tools.lookup_match(lid) or {}
            home = match.get("home_name", "?")
            away = match.get("away_name", "?")
            pick = r.get("pick", "")
            if pick == "H": pick_name = home
            elif pick == "A": pick_name = away
            else: pick_name = pick
            old_pct = f"{r.get('bet_size', 0) / capital * 100:.1f}%" if capital and capital > 0 else "-"
            removed_rows += f"""
        <tr style="background:#fafafa;color:#bbb;font-size:12px">
            <td style="padding:4px 8px;text-align:center">✕</td>
            <td style="padding:4px 6px" colspan="4">{home} vs {away}</td>
            <td style="padding:4px 12px;text-decoration:line-through">{pick_name}</td>
            <td style="padding:4px 12px;text-align:center" colspan="2">-</td>
            <td style="padding:4px 12px;text-align:center">{old_pct}</td>
        </tr>"""

        removed_html = f"""
    <div style="margin-top:20px">
        <div style="font-size:13px;color:#999;margin-bottom:8px">🗑 已移除 ({len(removed)} 单)</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead>
                <tr style="background:#e0e0e0;color:#999">
                    <th style="padding:4px 8px;text-align:center"></th>
                    <th style="padding:4px 6px;text-align:left" colspan="4">比赛</th>
                    <th style="padding:4px 12px;text-align:left">选择</th>
                    <th style="padding:4px 12px;text-align:center" colspan="2"></th>
                    <th style="padding:4px 12px;text-align:center">原仓位</th>
                </tr>
            </thead>
            <tbody>{removed_rows}</tbody>
        </table>
    </div>"""
    else:
        removed_html = ""

    html += removed_html

    html += f"""
    <div style="margin-top:20px;font-size:12px;color:#aaa">
        {agent_name} · 自动发送 | {datetime.now().strftime('%Y-%m-%d %H:%M')}
    </div>
</body></html>"""
    return html


# ═══════════════════════════════════════════════
# bc狗邮件（按票聚合：北单编号 + 最晚可购买时间）
# ═══════════════════════════════════════════════

def get_beidan_parlay_slips(agent_name: str, day_start: str, day_end: str) -> list[dict]:
    """获取bc狗当前足球日窗口内、未结算的串关票（按 slip_id 聚合）。

    每票返回:
      slip_id, ticket_type, combos_count, unit_stake, total_stake,
      leg_count, created_at, earliest_kickoff, latest_purchase, legs
    按 earliest_kickoff 升序。
    """
    role = _load_role_json(agent_name)
    orders = role.get("orders", [])
    if not orders:
        return []

    slips: "OrderedDict[str, dict]" = OrderedDict()
    for o in orders:
        if o.get("settled_at"):
            continue
        if o.get("bet_type") != "北单串关":
            continue
        sid = o.get("slip_id") or o.get("id")
        if not sid or sid in slips:
            continue
        legs = o.get("ticket_legs") or o.get("legs") or []
        if not legs:
            continue
        times = [l.get("match_time", "") for l in legs if l.get("match_time")]
        if not times:
            continue
        # 至少一腿落在本足球日窗口
        if not any(day_start <= t <= day_end for t in times):
            continue
        earliest = min(times)
        try:
            latest = (datetime.strptime(earliest, "%Y-%m-%d %H:%M:%S")
                      - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            latest = earliest
        combos = int(o.get("combos_count") or 0)
        unit = float(o.get("bet_size") or 2.0)
        slips[sid] = {
            "slip_id": sid,
            "ticket_type": o.get("ticket_type") or "8串1",
            "combos_count": combos,
            "unit_stake": unit,
            "total_stake": round(combos * unit, 2),
            "leg_count": len(legs),
            "created_at": o.get("created_at", ""),
            "earliest_kickoff": earliest,
            "latest_purchase": latest,
            "legs": legs,
        }
    result = list(slips.values())
    result.sort(key=lambda x: x["earliest_kickoff"])
    return result


def _beidan_pick_cn(leg: dict) -> str:
    """北单让球胜平负：H→胜, D→平, A→负；多选 → 用 / 连接。"""
    mp = {"H": "胜", "D": "平", "A": "负"}
    picks = leg.get("picks") or ([leg.get("pick")] if leg.get("pick") else [])
    names = [mp.get(p, p) for p in picks]
    return "/".join(names) if names else "—"


def _beidan_odds_str(leg: dict) -> str:
    od = leg.get("odds") or {}
    if isinstance(od, dict):
        mp = {"H": "胜", "D": "平", "A": "负"}
        return " / ".join(f"{mp.get(k, k)}@{v}" for k, v in od.items())
    return str(od or "-")


def build_beidan_parlay_email_body(agent_name: str, football_day: str,
                                   slips: list[dict]) -> str:
    """构建bc狗邮件正文：每票一块，含北单编号 + 最晚可购买时间。"""
    total_stake = sum(s["total_stake"] for s in slips)
    now = datetime.now()

    slip_blocks = ""
    for si, s in enumerate(slips, 1):
        legs_html = ""
        for li, l in enumerate(s["legs"], 1):
            mt = str(l.get("match_time", ""))[5:16]
            goal = l.get("goal_line")
            gl_str = f"{goal:+.1f}" if isinstance(goal, (int, float)) else "—"
            bg = "#f9f9f9" if li % 2 == 1 else "#ffffff"
            legs_html += f"""
        <tr style="background:{bg}">
            <td style="padding:5px 8px;text-align:center;color:#555">{l.get('beidan_number','?')}</td>
            <td style="padding:5px 8px;white-space:nowrap;color:#888">{mt}</td>
            <td style="padding:5px 8px;color:#888">{l.get('league_name','?')}</td>
            <td style="padding:5px 8px;text-align:right;color:#333">{l.get('home_name','?')}</td>
            <td style="padding:5px 2px;text-align:center;color:#ccc">vs</td>
            <td style="padding:5px 8px;color:#333">{l.get('away_name','?')}</td>
            <td style="padding:5px 8px;text-align:center;color:#666">{gl_str}</td>
            <td style="padding:5px 8px;text-align:center;font-weight:bold;color:#c0392b">{_beidan_pick_cn(l)}</td>
            <td style="padding:5px 8px;text-align:center;color:#333">{_beidan_odds_str(l)}</td>
        </tr>"""
        slip_blocks += f"""
    <div style="border:1px solid #e0e0e0;border-radius:8px;margin-bottom:18px;overflow:hidden">
        <div style="background:#2c3e50;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">
            <span><b>票{si}</b> · {s['ticket_type']} · {s['combos_count']}注 · {s['total_stake']:.0f}元</span>
            <span style="color:#ffd98a">⏰ 最晚可购买：<b>{s['latest_purchase']}</b>（最早开赛 {s['earliest_kickoff'][5:16]} 前5分钟）</span>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead>
                <tr style="background:#f0f0f0;color:#999">
                    <th style="padding:6px 8px;text-align:center">北单编号</th>
                    <th style="padding:6px 8px;text-align:center">时间</th>
                    <th style="padding:6px 8px;text-align:left">联赛</th>
                    <th style="padding:6px 8px;text-align:right">主队</th>
                    <th style="padding:6px 2px"></th>
                    <th style="padding:6px 8px;text-align:left">客队</th>
                    <th style="padding:6px 8px;text-align:center">让球</th>
                    <th style="padding:6px 8px;text-align:center">选择</th>
                    <th style="padding:6px 8px;text-align:center">赔率</th>
                </tr>
            </thead>
            <tbody>{legs_html}</tbody>
        </table>
    </div>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,SF Pro Display,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#333;max-width:900px;margin:0 auto;padding:20px">

    <div style="background:#fff8e1;border:1px solid #f0c36d;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#7a5c00">
        ⏰ <b>购买提醒：</b>每张票的「最晚可购买时间」= 该票<b>最早开赛场次的比赛时间 - 5分钟</b>；
        到点后整票无法再投，请以临近开赛的邮件为准。
    </div>

    <div style="margin-bottom:16px">
        <span style="font-size:18px;font-weight:bold">{agent_name}</span>
        <span style="color:#999;margin:0 8px">·</span>
        <span>足球日 {football_day}</span>
        <span style="color:#999;margin:0 8px">·</span>
        <span style="color:#e67e22">{len(slips)} 票待结算</span>
        <span style="color:#999;margin:0 8px">·</span>
        <span>合计 <b>{total_stake:.0f}元</b></span>
    </div>
    <div style="margin-bottom:16px;font-size:13px;color:#888">
        每票为北单让球胜平负长串（默认 8串1 · 5全包+3单选）；全包腿任选结果即中，单选腿须全部命中。
    </div>

    {slip_blocks}

    <div style="margin-top:20px;font-size:12px;color:#aaa">
        {agent_name} · 自动发送 | {now.strftime('%Y-%m-%d %H:%M')}
    </div>
</body></html>"""
    return html


# ═══════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════

def send_order_email(agent_name: str = "均注狗", day_str: str | None = None) -> bool:
    """
    拉取 agent 的当前足球日未结算订单并发送邮件。

    Args:
        agent_name: 角色名，默认 "均注狗"
        day_str: 足球日日期 "YYYY-MM-DD"，None 则自动推算

    Returns:
        True 表示发送成功
    """
    # 1. 确定足球日
    if day_str:
        try:
            d = date.fromisoformat(day_str)
        except ValueError:
            print(f"[order_email] 日期格式错误: {day_str}，应为 YYYY-MM-DD")
            return False
        start, end = get_football_day(d)
    else:
        start, end = get_football_day()

    football_day = start[:10]  # 取窗口起始日期作为标签

    # ── bc狗：按票聚合（北单编号 + 最晚可购买时间）──
    if agent_name == "bc狗":
        slips = get_beidan_parlay_slips(agent_name, start, end)
        if not slips:
            print(f"[order_email] {agent_name} 足球日 {football_day} 无待结算串关票，跳过发送")
            return True
        body = build_beidan_parlay_email_body(agent_name, football_day, slips)
        subject = f"[{agent_name}] 足球日 {football_day} 北单串关待结算 ({len(slips)}票)"
        ok = send_email(subject, body, mail_cfg="163", is_html=True, agent_name=agent_name)
        if not ok:
            return False
        # 北单暂不写快照（票固定，无逐单变化检测需求）
        return True

    # 2. 加载未结算订单
    orders = get_pending_orders(agent_name, start, end)
    if not orders:
        print(f"[order_email] {agent_name} 足球日 {football_day} 无待结算订单，跳过发送")
        return True

    # 3. 加载快照 & 检测变化
    # 新的一天（football_day 和快照不同）→ 视为第一天，不标变化
    snapshot = load_snapshot(agent_name)
    if snapshot and snapshot.get("football_day") != football_day:
        snapshot = None

    # 已结算离开 pending 的 lota_id：这些是"结算了"不是"取消"，不能标"取消/不下"
    settled_ids: set = set()
    try:
        _role_data = _load_role_json(agent_name)
        settled_ids = {
            o.get("lota_id") for o in (_role_data.get("orders") or [])
            if o.get("settled_at") and o.get("lota_id")
        }
    except Exception:
        settled_ids = set()

    changes = detect_changes(orders, snapshot, settled_ids=settled_ids)

    # 4. 构建邮件
    body = build_email_body(agent_name, football_day, orders, changes)
    subject = f"[{agent_name}] 足球日 {football_day} 待结算订单 ({len(orders)}单)"

    # 5. 发送
    ok = send_email(subject, body, mail_cfg="163", is_html=True, agent_name=agent_name)
    if not ok:
        return False

    # 6. 保存快照
    save_snapshot(agent_name, football_day, orders)
    return True
