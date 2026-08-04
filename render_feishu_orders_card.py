#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把邮件发送的那种"待结算订单"内容渲染成飞书 interactive 卡片。

数据源与 src/order_email.py 完全一致（get_pending_orders + 快照变化检测），
输出飞书卡片 v2 JSON，供 Chat-Codex 通过 BRIDGE_SEND_FEISHU_CARD 协议直接发出。

用法:
    python3 render_feishu_orders_card.py                # 默认 梭哈2狗 均注狗，当前足球日
    python3 render_feishu_orders_card.py --day 2026-08-02
    python3 render_feishu_orders_card.py 梭哈2狗 均注狗

输出:
    lota_data/orders_card.json              # 全部 agent 合并卡片
    lota_data/orders_card_<agent>.json      # 每个 agent 单张卡片
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from src import order_email
from src.environment import get_football_day

ROOT = Path(__file__).parent
CARD_DIR = ROOT / "lota_data"

DEFAULT_AGENTS = ["梭哈2狗", "均注狗", "alpha2狗"]


# ── 邮件正文同款展示逻辑（保持与邮件一致） ──
def _pick_display(order: dict) -> str:
    return order_email._pick_display(order)


def _fmt_hc(hc) -> str:
    return order_email._fmt_hc(hc)


def _old_pick_display(old_pick: str, order: dict) -> str:
    return order_email._old_pick_display(old_pick, order)


def _capital(agent_name: str, orders: list[dict]) -> float | None:
    """与 build_email_body 相同：总资金 = 可用余额 + 已锁仓位。"""
    try:
        role_path = order_email.ROLES_DIR / agent_name / f"{agent_name}.json"
        if role_path.exists():
            role_data = json.loads(role_path.read_text(encoding="utf-8"))
            pending_bets = sum(o.get("bet_size", 0) for o in orders)
            return (role_data.get("capital") or 0) + pending_bets
    except Exception:
        pass
    return None


def _time_hint(orders: list[dict]) -> str:
    now = datetime.now()
    two_h = now + timedelta(hours=2)
    near_count = 0
    for o in orders:
        try:
            mt = datetime.strptime(o["match_time"], "%Y-%m-%d %H:%M:%S")
            if mt <= two_h:
                near_count += 1
        except ValueError:
            pass
    return f"近2h {near_count}单" if near_count > 0 else f"剩余 {len(orders)}单"


def _is_finished(o: dict) -> bool:
    if o.get("state") == 6:
        return True
    try:
        mt = datetime.strptime(o["match_time"], "%Y-%m-%d %H:%M:%S")
        return mt + timedelta(hours=2) < datetime.now()
    except ValueError:
        return False


def _order_line(idx: int, o: dict, capital: float | None, is_new: bool) -> str:
    """单行订单展示：时间 联赛 对阵 → 选择 让球 @赔率 仓位 [🆕]"""
    t = o["match_time"][5:16]  # MM-DD HH:MM
    league = (o.get("league_name") or "?").strip()
    home, away = o.get("home_name", "?"), o.get("away_name", "?")
    pick = _pick_display(o)
    hc = _fmt_hc(o.get("handicap"))
    odds = o.get("odds") or "-"
    pct = f"{o.get('bet_size', 0) / capital * 100:.1f}%" if capital and capital > 0 else "-"
    score = o.get("score", "")
    score_str = f" ({score})" if score else ""

    if _is_finished(o):
        line = f"~~{idx}. {t} {league} {home} vs {away}{score_str} → {pick} {hc} @{odds} {pct}~~"
    else:
        line = f"{idx}. **{t}** {league} {home} vs {away} → **{pick}** {hc} @{odds} {pct}"
    if is_new:
        line += " 🆕"
    return line


def _change_subline(o: dict, capital: float | None) -> str:
    """变化子行：原选择/让球/赔率/仓位 → 新值（与邮件一致，只列有变化的列）。"""
    entry = o.get("_change")
    if not entry:
        return ""
    cells = []
    old_pick = entry.get("old_pick")
    if old_pick is not None:
        cells.append(f"选 {_old_pick_display(old_pick, o)} → {_pick_display(o)}")
    old_hc = entry.get("old_handicap")
    if old_hc is not None:
        cells.append(f"让 {_fmt_hc(old_hc)} → {_fmt_hc(o.get('handicap'))}")
    old_odds = entry.get("old_odds")
    if old_odds is not None:
        cells.append(f"赔 {old_odds} → {o.get('odds') or '-'}")
    old_bet = entry.get("old_bet_size")
    if old_bet is not None and capital and capital > 0:
        cells.append(f"仓 {old_bet / capital * 100:.1f}% → {o.get('bet_size', 0) / capital * 100:.1f}%")
    return ("↳ 变化: " + " · ".join(cells)) if cells else ""


def _agent_table(agent_name: str, day_label: str, orders: list[dict], changes: dict, element_id: str) -> dict:
    """把单个 agent 的待结算订单渲染成飞书 table 组件（schema V2）。

    table 组件只能放在卡片根节点下，不能嵌套；一张卡片最多 5 个 table。
    列：时间 / 对阵 / 选择（含让球、赔率、仓位）/ 状态（彩色标签）。
    """
    capital = _capital(agent_name, orders)
    new_ids = {x["lota_id"] for x in changes.get("new", [])}
    changed_map = {c["order"]["lota_id"]: c for c in changes.get("changed", [])}

    rows = []
    for i, o in enumerate(orders, 1):
        if o["lota_id"] in changed_map:
            o = {**o, "_change": changed_map[o["lota_id"]]}
        t = o["match_time"][5:16]
        league = (o.get("league_name") or "?").strip()
        home, away = o.get("home_name", "?"), o.get("away_name", "?")
        pick = _pick_display(o)
        hc = _fmt_hc(o.get("handicap"))
        odds = o.get("odds") or "-"
        pct = f"{o.get('bet_size', 0) / capital * 100:.1f}%" if capital and capital > 0 else "-"
        score = o.get("score", "")
        score_str = f" ({score})" if score else ""
        finished = _is_finished(o)
        strike = "~~" if finished else ""

        time_cell = t
        # 联赛一行、对阵一行；重点（主队）加粗，完场加删除线
        matchup_cell = f"{strike}{league}{strike}\n{strike}**{home}** vs {away}{score_str}{strike}"
        pick_cell = f"{strike}**{pick}** {hc} @{odds} · **{pct}**{strike}"
        sub = _change_subline(o, capital)
        if sub:
            pick_cell += f"\n{sub}"

        status_tags = []
        if o["lota_id"] in new_ids:
            status_tags.append({"text": "🆕 新单", "color": "blue"})
        if o["lota_id"] in changed_map:
            status_tags.append({"text": "⇄ 变化", "color": "orange"})
        if finished:
            status_tags.append({"text": "完场", "color": "grey"})

        row: dict = {"time": time_cell, "matchup": matchup_cell, "pick": pick_cell}
        if status_tags:
            row["status"] = status_tags
        rows.append(row)

    return {
        "tag": "table",
        "element_id": element_id,
        "page_size": 10,
        "row_height": "auto",
        "header_style": {
            "text_align": "center",
            "text_size": "normal",
            "background_style": "grey",
            "text_color": "default",
            "bold": True,
        },
        "columns": [
            {"name": "time", "display_name": "时间", "width": "88px", "data_type": "text", "horizontal_align": "center"},
            {"name": "matchup", "display_name": "对阵", "width": "auto", "data_type": "markdown"},
            {"name": "pick", "display_name": "选择", "width": "auto", "data_type": "markdown"},
            {"name": "status", "display_name": "状态", "width": "88px", "data_type": "options", "horizontal_align": "center"},
        ],
        "rows": rows,
    }


def _removed_markdown(changes: dict) -> str | None:
    """已移除订单的 markdown 文本（表格没有该列，单独展示）。"""
    removed = changes.get("removed", [])
    if not removed:
        return None
    removed_lines = []
    for r in removed:
        match = order_email.tools.lookup_match(r.get("lota_id", "")) or {}
        home = match.get("home_name", "?")
        away = match.get("away_name", "?")
        pick = r.get("pick", "")
        pick_name = home if pick == "H" else away if pick == "A" else pick
        removed_lines.append(f"🗑 {home} vs {away} · ~~{pick_name}~~")
    return f"**已移除 ({len(removed)})**\n" + "\n".join(removed_lines)


def _agent_section(agent_name: str, day_label: str, orders: list[dict], changes: dict, element_id: str) -> list[dict]:
    """单个 agent 的卡片块：标题 markdown + table + 已移除说明。"""
    blocks: list[dict] = [
        {"tag": "markdown", "content": f"**{agent_name}** · 足球日 {day_label} · {_time_hint(orders)} 待结算"},
        _agent_table(agent_name, day_label, orders, changes, element_id),
    ]
    removed_text = _removed_markdown(changes)
    if removed_text:
        blocks.append({"tag": "markdown", "content": removed_text})
    return blocks


def _fallback_section(agent_name: str, day_label: str, orders: list[dict], changes: dict) -> list[dict]:
    """超过 5 个 table 上限时的降级方案：纯 markdown 段落（原样式）。"""
    capital = _capital(agent_name, orders)
    new_ids = {x["lota_id"] for x in changes.get("new", [])}
    changed_map = {c["order"]["lota_id"]: c for c in changes.get("changed", [])}

    lines = [f"**{agent_name}** · 足球日 {day_label} · {_time_hint(orders)} 待结算"]
    for i, o in enumerate(orders, 1):
        if o["lota_id"] in changed_map:
            o = {**o, "_change": changed_map[o["lota_id"]]}
        lines.append(_order_line(i, o, capital, o["lota_id"] in new_ids))
        sub = _change_subline(o, capital)
        if sub:
            lines.append(sub)
    removed_text = _removed_markdown(changes)
    if removed_text:
        lines.append("")
        lines.append(removed_text)

    return [{"tag": "markdown", "content": "\n".join(lines)}]


def build_card(agent_sections: list[tuple[str, list[dict]]], day_label: str, generated_at: str) -> dict:
    elements: list[dict] = []
    for idx, (agent_name, elements_block) in enumerate(agent_sections):
        if idx > 0:
            elements.append({"tag": "hr"})
        elements.extend(elements_block)

    # 飞书卡片 schema V2 已不支持 note 元素（230099），改用 markdown 元素承载辅助信息
    elements.append({"tag": "markdown", "content": "⏰ 除 12 点附近的邮件外，请参考未来 3 小时内的比赛。"})
    elements.append({"tag": "markdown", "content": f"更新于 {generated_at} · 群里说「今日单」刷新"})

    agents_text = " + ".join(a for a, _ in agent_sections)
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"📧 今日订单 · {day_label}"},
        },
        "body": {"elements": elements},
    }


def main() -> int:
    args = sys.argv[1:]
    day_str = None
    agents = list(DEFAULT_AGENTS)
    if "--day" in args:
        i = args.index("--day")
        day_str = args[i + 1]
        agents = [a for j, a in enumerate(args) if j not in (i, i + 1)] or DEFAULT_AGENTS
    else:
        agents = args or DEFAULT_AGENTS

    if day_str:
        try:
            d = date.fromisoformat(day_str)
        except ValueError:
            print(f"日期格式错误: {day_str}，应为 YYYY-MM-DD")
            return 1
        start, end = get_football_day(d)
    else:
        start, end = get_football_day()
    day_label = start[:10]

    sections: list[tuple[str, list[dict]]] = []
    agent_orders: dict[str, tuple[list[dict], dict]] = {}
    for agent in agents:
        orders = order_email.get_pending_orders(agent, start, end)
        if not orders:
            print(f"[card] {agent} 足球日 {day_label} 无待结算订单，跳过")
            continue
        snapshot = order_email.load_snapshot(agent)
        if snapshot and snapshot.get("football_day") != day_label:
            snapshot = None
        changes = order_email.detect_changes(orders, snapshot)
        agent_orders[agent] = (orders, changes)
        if len(sections) < 5:
            sections.append((agent, _agent_section(agent, day_label, orders, changes, f"orders_{len(sections) + 1}")))
        else:
            sections.append((agent, _fallback_section(agent, day_label, orders, changes)))

    if not sections:
        print(f"[card] 足球日 {day_label} 没有任何 agent 有待结算订单")
        return 0

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    CARD_DIR.mkdir(parents=True, exist_ok=True)

    combined = build_card(sections, day_label, generated_at)
    combined_path = CARD_DIR / "orders_card.json"
    combined_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 合并卡片: {combined_path}")

    for agent, _ in sections:
        orders, changes = agent_orders[agent]
        card = build_card([(agent, _agent_section(agent, day_label, orders, changes, "orders_1"))], day_label, generated_at)
        path = CARD_DIR / f"orders_card_{agent}.json"
        path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 单 agent 卡片: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
