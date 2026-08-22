import re
from typing import Any

from .data_manager import DataManager
from .fund_limits import FundManager
from .prompt_builder import parse_order


def parse_orders(response_text: str, safe_matches: list[dict], dm: DataManager) -> list[dict]:
    """解析 LLM 响应中的 order block，并做合法性校验与补全。"""
    if not response_text:
        return []

    blocks = re.findall(r'```order\n(.*?)(?=```|\Z)', response_text, re.DOTALL)
    if blocks and not response_text.rstrip().endswith('```'):
        print(
            "  ⚠️ LLM 响应可能被截断（末尾无闭合 ```），最后一个 order block 已尽力解析"
        )

    safe_ids = {m['lota_id'] for m in safe_matches if m.get('lota_id')}
    orders: list[dict] = []

    for block in blocks:
        parsed = parse_order("```order\n" + block + "\n```")
        if not parsed:
            continue

        if not parsed.get('lota_id'):
            parsed['lota_id'] = _resolve_lota_id_by_reason(parsed, block, safe_matches)

        if not parsed.get('lota_id') and len(safe_matches) == 1:
            parsed['lota_id'] = safe_matches[0]['lota_id']

        if not parsed.get('lota_id'):
            continue

        lid = parsed['lota_id']

        if lid in dm.get_blacklist():
            print(f"  🚫 黑名单拦截 {lid}，跳过")
            continue

        if lid not in safe_ids:
            matched = _rematch_lota_id(parsed, block, safe_matches)
            if matched:
                print(
                    f"  🔄 重匹配 {lid} → {matched['lota_id']} ({matched.get('home_name','')} vs {matched.get('away_name','')})"
                )
                lid = matched['lota_id']
                parsed['lota_id'] = lid
            else:
                print(
                    f"  🚫 无法匹配合法比赛，跳过订单 (原始 lota_id={lid})"
                )
                continue
        else:
            _cross_validate_order(parsed, block, safe_matches)

        _fill_order_odds_and_handicap(parsed, dm)
        orders.append(parsed)

    return _dedupe_orders(orders)


def _resolve_lota_id_by_reason(parsed: dict, block: str, safe_matches: list[dict]) -> str | None:
    """当 order 没有 lota_id 时，尝试从理由或队名中匹配唯一比赛。"""
    if not safe_matches:
        return None

    reason = (parsed.get('reason', '') or '') + "\n" + block
    for match in safe_matches:
        home = match.get('home_name', '')
        away = match.get('away_name', '')
        if home and len(home) >= 2 and home[:2] in reason:
            return match['lota_id']
        if away and len(away) >= 2 and away[:2] in reason:
            return match['lota_id']

    return None


def _rematch_lota_id(parsed: dict, block: str, safe_matches: list[dict]) -> dict | None:
    """当 LLM 输出的 lota_id 不在当天比赛中时，尝试用队名或数字 ID 重匹配。"""
    reason = (parsed.get('reason', '') or '') + "\n" + block
    for match in safe_matches:
        home = match.get('home_name', '')
        away = match.get('away_name', '')
        if home and len(home) >= 2 and home[:2] in reason:
            return match
        if away and len(away) >= 2 and away[:2] in reason:
            return match

    for match in safe_matches:
        m_lid = match.get('lota_id', '')
        if not m_lid:
            continue
        numeric_id = m_lid.replace('Lota', '').replace('lota', '')
        if m_lid in reason or numeric_id in reason:
            return match

    return None


def _cross_validate_order(parsed: dict, block: str, safe_matches: list[dict]) -> None:
    """对已知 lota_id 的订单做队名/ID 交叉校验，发现可疑输出。"""
    lid = parsed.get('lota_id', '')
    if not lid:
        return

    match_info = next((m for m in safe_matches if m.get('lota_id') == lid), None)
    if not match_info:
        return

    reason = (parsed.get('reason', '') or '') + "\n" + block
    home = match_info.get('home_name', '')
    away = match_info.get('away_name', '')
    if home and away and len(home) >= 2 and len(away) >= 2:
        home_ok = home[:2] in reason
        away_ok = away[:2] in reason
        numeric_id = lid.replace('Lota', '').replace('lota', '')
        id_in_reason = lid in reason or numeric_id in reason
        if not home_ok and not away_ok:
            if id_in_reason:
                print(
                    f"  ✅ 队名交叉校验: {lid} reason中未出现队名但引用了ID，视为通过"
                )
            else:
                print(
                    f"  ⚠️ 队名交叉校验跳过: {lid} ({home} vs {away}) reason中无队名/ID，无法交叉验证但放行（请人工检查）"
                )


def _fill_order_odds_and_handicap(parsed: dict, dm: DataManager) -> None:
    """为订单补全赔率和权威亚盘盘口。"""
    if not parsed.get('odds'):
        lid = parsed.get('lota_id', '')
        if not lid:
            return

        odds = dm.get_odds(lid)
        bt = parsed.get('bet_type', '')
        pk = parsed.get('pick', '')
        if bt == '胜平负' and odds.get('eu'):
            parsed['odds'] = odds['eu'].get(pk, 0)
        elif bt == '亚盘' and odds.get('asian'):
            parsed['odds'] = odds['asian'].get('h' if pk == 'H' else 'a', 0)
        elif bt == '大小球' and odds.get('ou'):
            parsed['odds'] = odds['ou'].get('over' if pk == 'over' else 'under', 0)

    if parsed.get('bet_type') == '亚盘':
        lid = parsed.get('lota_id', '')
        asian = (dm.get_odds(lid) or {}).get('asian', {})
        if asian.get('handicap') is not None:
            parsed['handicap'] = -float(asian['handicap'])


def _dedupe_orders(orders: list[dict]) -> list[dict]:
    """同批去重：保留同一场同一类型的最后一个订单。"""
    seen: dict[tuple[str, str], dict] = {}
    for order in orders:
        key = (order.get('lota_id'), order.get('bet_type'))
        if key in seen:
            prev = seen[key]
            print(
                f"  ⚠️ 同批重复 {key[0]} {key[1]}: "
                f"¥{prev.get('bet_size', 0)} → ¥{order.get('bet_size', 0)}（以后者为准）"
            )
        seen[key] = order
    return list(seen.values())


def partition_placement_orders(
    orders: list[dict],
    started_lids: set[str],
    pending_markets: set[tuple[str, str]],
) -> tuple[list[dict], float]:
    """把 LLM 订单拆成「可下单」与「已开赛预算占用」两组（确定性，供两条调用路径复用）。

    规则（按序）：
      - skip → 直接丢弃；
      - (lota_id, bet_type) 已有未结算订单 → 跳过（重复单/维持原仓），不计入占用——
        既有订单的金额已在锁定敞口里，再计入会二次扣减预算；
      - 已开赛且无既有订单 → 不下注，金额计入当日预算占用（**只影响预算口径，不真实扣款**）；
      - 其余 → 进入下单列表。

    返回 (new_orders, started_total)。started 订单会打 `_started_deduct` 标记供 session 日志展示。
    """
    new_orders: list[dict] = []
    started_total = 0.0
    for o in orders:
        if o.get("skip"):
            continue
        lid = o.get("lota_id", "")
        market = (lid, o.get("bet_type"))
        if market in pending_markets:
            print(f"  ⏭ 跳过重复单: {market[0]} {market[1]}（已有未结算订单）")
            continue
        if lid in started_lids:
            started_total += float(o.get("bet_size", 0) or 0)
            o["_started_deduct"] = True
            print(f"  🔒 已开赛不下注（计入当日占用）: {lid} ¥{float(o.get('bet_size', 0) or 0):,.0f}")
            continue
        new_orders.append(o)
    return new_orders, started_total


def scale_orders_to_budget(
    new_orders: list[dict],
    capital_before: float,
    locked_exposure: float,
    started_total: float,
    limits,
) -> list[dict]:
    """按可用预算折算下单金额（已开赛占用是预算口径，不真实扣款）。

    可用预算 = 扣减前余额 − 已开赛占用（LLM 下单时的余额口径）。
    limits 启用 → FundManager 硬约束；否则按「全金额 = 余额 + 锁定敞口」比例缩放。
    返回折算后可下单的订单列表（不会真实扣掉 started_total）。
    """
    effective_capital = capital_before - started_total
    if effective_capital <= 0:
        print("  📐 可用预算 ≤ 0（已开赛占用吃满），本轮不下单")
        return []
    if limits.enabled:
        placed, _dropped = FundManager(limits).apply(new_orders, effective_capital)
        return placed
    full_amount = capital_before + locked_exposure
    new_total = sum(o.get("bet_size", 0) for o in new_orders)
    if new_total > 0 and full_amount > 0:
        scale = effective_capital / full_amount
        print(f"  📐 资金折算: 锁定¥{locked_exposure:,.0f} + 余额¥{capital_before:,.0f}"
              f"（已开赛占用¥{started_total:,.0f}） = 全金额¥{full_amount:,.0f}")
        print(f"  📐 LLM分配¥{new_total:,.0f} → 折算×{scale:.2f} → 实下¥{int(new_total * scale):,.0f}")
        for o in new_orders:
            o["bet_size"] = int(o["bet_size"] * scale)
    return new_orders
