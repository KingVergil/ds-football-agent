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
from datetime import date, datetime, timedelta

from . import tools
from .environment import get_football_day
from .email_sender import send_email

SNAPSHOT_DIR = Path(__file__).parent.parent / "lota_data" / "email_snapshots"
ROLES_DIR = Path(__file__).parent.parent / "lota_data" / "roles"


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
    result = {"new": [], "changed": [], "has_changes": False}

    if snapshot is None:
        return result

    snapshot_map = {s["lota_id"]: s for s in snapshot.get("orders", [])}

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
            if changed:
                result["changed"].append(entry)
                result["has_changes"] = True

    return result


# ═══════════════════════════════════════════════
# 邮件正文
# ═══════════════════════════════════════════════

def _pick_display(order: dict) -> str:
    """将 pick 转为队名显示。"""
    pick = order["pick"]
    home = order.get("home_name", "")
    away = order.get("away_name", "")
    if pick == "H":
        return home
    elif pick == "A":
        return away
    # 大小球 / 胜平负 保留原值
    return pick


def _old_pick_display(old_pick: str, order: dict) -> str:
    """将旧 pick 转为队名。"""
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
            <td colspan="9" style="padding:6px 10px;font-size:12px;color:#888;text-align:center">
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
        home = o.get("home_name", "?")
        away = o.get("away_name", "?")
        pick = _pick_display(o)
        hc_str = _fmt_hc(o.get("handicap"))
        odds = o.get("odds") or "-"
        score = o.get("score", "")
        score_str = f' <span style="color:#999;font-size:11px">{score}</span>' if score else ""

        new_badge = '<span style="background:#27ae60;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;margin-left:4px">NEW</span>' if lid in new_ids else ""

        rows_html += f"""
        <tr style="background:{bg}">
            <td style="padding:6px 8px;text-align:center;color:{'#bbb' if is_finished_row else '#999'}">{i}{new_badge}</td>
            <td style="padding:6px 6px;white-space:nowrap;color:{text_color}">{match_time_short}</td>
            <td style="padding:6px 6px;color:{'#bbb' if is_finished_row else '#666'}">{league}</td>
            <td style="padding:6px 6px;text-align:right;max-width:80px;color:{text_color}">{home}</td>
            <td style="padding:6px 2px;text-align:center;color:#ccc">vs</td>
            <td style="padding:6px 6px;max-width:80px;color:{text_color}">{away}{score_str}</td>
            <td style="padding:6px 12px;font-weight:bold;color:{'#bbb' if is_finished_row else '#c0392b'};min-width:90px">{pick}</td>
            <td style="padding:6px 12px;text-align:center;min-width:60px;color:{text_color}">{hc_str}</td>
            <td style="padding:6px 12px;text-align:center;min-width:60px;color:{text_color}">{odds}</td>
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

            rows_html += f"""
        <tr style="background:{sub_bg};font-size:11px;color:#e74c3c">
            <td style="padding:1px 8px;text-align:center">↳</td>
            <td style="padding:1px 6px" colspan="5"></td>
            <td style="padding:1px 12px">{pick_cell}</td>
            <td style="padding:1px 12px;text-align:center">{hc_cell}</td>
            <td style="padding:1px 12px;text-align:center">{odds_cell}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,SF Pro Display,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#333;max-width:780px;margin:0 auto;padding:20px">

    <div style="margin-bottom:16px">
        <span style="font-size:18px;font-weight:bold">{agent_name}</span>
        <span style="color:#999;margin:0 8px">·</span>
        <span>足球日 {football_day}</span>
        <span style="color:#999;margin:0 8px">·</span>
        <span style="color:#e67e22">{len(orders)} 单待结算</span>
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
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>

    <div style="margin-top:20px;font-size:12px;color:#aaa">
        {agent_name} · 自动发送 | {datetime.now().strftime('%Y-%m-%d %H:%M')}
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

    changes = detect_changes(orders, snapshot)

    # 4. 构建邮件
    body = build_email_body(agent_name, football_day, orders, changes)
    subject = f"[{agent_name}] 足球日 {football_day} 待结算订单 ({len(orders)}单)"

    # 5. 发送
    ok = send_email(subject, body, mail_cfg="163", is_html=True)
    if not ok:
        return False

    # 6. 保存快照
    save_snapshot(agent_name, football_day, orders)
    return True
