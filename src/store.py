"""
DSFootball Python CLI — 模型存储 & 业务逻辑

每个写操作包含检查逻辑，确保数据一致性。
不包含回测（backtest 单独一个文件）。
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from .models import (
    Match, Prediction, Factor, BacktestResult,
    PredictType, model_to_dict, dict_to_model,
)

# ═══════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════

DATA_ROOT = Path(__file__).parent.parent / "lota_data"
MATCHES_DIR = DATA_ROOT / "matches"
PREDICTS_DIR = DATA_ROOT / "predicts"
FACTORS_DIR = DATA_ROOT / "factors"
BACKTESTS_DIR = DATA_ROOT / "backtests"

for d in [MATCHES_DIR, PREDICTS_DIR, FACTORS_DIR, BACKTESTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Optional[dict | list]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _write_json(path: Path, data: dict | list) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════
# Match
# ═══════════════════════════════════════════════

def save_match(match: Match) -> Match:
    """写入单场比赛缓存。按日期分文件。"""
    date_str = match.match_time[:10] if match.match_time else "unknown"
    path = MATCHES_DIR / f"{date_str}.json"
    data = _read_json(path) or []
    if not isinstance(data, list):
        data = []

    # upsert by lota_id
    exists = False
    d = model_to_dict(match)
    for i, m in enumerate(data):
        if m.get("lota_id") == match.lota_id:
            data[i] = d
            exists = True
            break
    if not exists:
        data.append(d)

    _write_json(path, data)
    return match


def get_match(lota_id: str) -> Optional[dict]:
    """从缓存查找单场比赛（扫描 matches 目录）"""
    from src.tools import get_cached_match
    return get_cached_match(lota_id)


# ═══════════════════════════════════════════════
# Prediction
# ═══════════════════════════════════════════════

def save_prediction(pred: Prediction) -> Prediction:
    """
    写入预测。检查逻辑:
      1. type 必须是有效的 PredictType
      2. value 必须与该类型的 schema 匹配
      3. upsert: 同 (lota_id, type) 则更新
    """
    # ── 检查 1: type 合法性 ──
    valid_types = {t.value for t in PredictType}
    if pred.type.value not in valid_types:
        raise ValueError(f"无效预测类型: {pred.type}，合法值: {valid_types}")

    # ── 检查 2: value schema ──
    _check_prediction_value(pred.type, pred.value)

    # ── 检查 3: lota_id 非空 ──
    if not pred.lota_id:
        raise ValueError("lota_id 不能为空")

    # ── upsert ──
    path = PREDICTS_DIR / f"{pred.lota_id}.json"
    data = _read_json(path) or []
    if not isinstance(data, list):
        data = []

    d = model_to_dict(pred)
    key = f"{pred.lota_id}::{pred.type.value}"
    found = False
    for i, p in enumerate(data):
        ek = f"{p.get('lota_id','')}::{p.get('type','')}"
        if ek == key:
            data[i] = d
            found = True
            break
    if not found:
        data.append(d)

    _write_json(path, data)
    return pred


def _check_prediction_value(ptype: PredictType, value: dict) -> None:
    """验证 value 与预测类型的 schema 是否匹配"""
    t = ptype.value
    if t == "胜平负":
        if "result" not in value or value["result"] not in ("H", "D", "A"):
            raise ValueError(f"胜平负 value 需含 result: H|D|A, got {value}")
    elif t == "亚盘":
        if "handicap" not in value or "direction" not in value:
            raise ValueError(f"亚盘 value 需含 handicap + direction, got {value}")
        if value["direction"] not in ("H", "A"):
            raise ValueError(f"亚盘 direction 需为 H|A, got {value['direction']}")
    elif t == "大小球":
        if "threshold" not in value or "direction" not in value:
            raise ValueError(f"大小球 value 需含 threshold + direction, got {value}")
        if value["direction"] not in ("over", "under"):
            raise ValueError(f"大小球 direction 需为 over|under, got {value['direction']}")
    elif t == "比分":
        has_scores = "scores" in value and isinstance(value["scores"], list) and len(value["scores"]) > 0
        has_single = "home" in value and "away" in value
        if not has_scores and not has_single:
            raise ValueError(f"比分 value 需含 scores[] 或 home+away, got {value}")
    elif t == "进球数":
        if "goals" not in value:
            raise ValueError(f"进球数 value 需含 goals, got {value}")


def set_prediction_result(pred_id: str, score: str) -> Prediction | None:
    """
    用比分回填一条预测的 hit/result/checked_at。

    检查逻辑:
      1. 通过 id 找到预测
      2. score 格式必须为 "N:N"
      3. 根据 type + value 判定命中/未中/走水
      4. 写回磁盘
    """
    import re
    if not re.match(r'^\d+:\d+$', score):
        raise ValueError(f"比分格式错误: {score}，需为 N:N")

    from src.tools import score2goal_diff, score2goal_sum, score2_1x2

    # ── 查找预测 ──
    pred_data, path = _find_prediction_by_id(pred_id)
    if not pred_data:
        return None

    ptype = pred_data.get("type", "")
    value = pred_data.get("value", {})
    hg, ag = map(int, score.split(":"))
    diff = hg - ag
    total = hg + ag

    outcome = None
    if ptype == "胜平负":
        want = value.get("result", "")
        actual = score2_1x2(score)
        outcome = (actual == want) if want in ("H", "D", "A") else None

    elif ptype == "亚盘":
        hc = float(value.get("handicap", 0))
        d = (value.get("direction", "")).strip().upper()
        if d == "H":
            outcome = None if diff + hc == 0 else (diff + hc > 0)
        elif d == "A":
            outcome = None if diff == hc else (diff < hc)

    elif ptype == "大小球":
        th = float(value.get("threshold", 0))
        d = (value.get("direction", "")).strip().lower()
        if total == th:
            outcome = None
        elif d in ("over", "大", "大球"):
            outcome = total > th
        elif d in ("under", "小", "小球"):
            outcome = total < th

    elif ptype == "比分":
        scores = value.get("scores") or []
        if not scores:
            h = int(value.get("home", -1))
            a = int(value.get("away", -1))
            scores = [{"home": h, "away": a}] if h >= 0 and a >= 0 else []
        outcome = any(s.get("home") == hg and s.get("away") == ag for s in scores)

    elif ptype == "进球数":
        g = int(value.get("goals", -1))
        outcome = (total == g) if g >= 0 else None

    # ── 写回 ──
    pred_data["hit"] = outcome
    pred_data["result"] = score
    pred_data["checked_at"] = datetime.now().isoformat()

    data = _read_json(path) or []
    for i, p in enumerate(data):
        if p.get("id") == pred_id:
            data[i] = pred_data
            break
    _write_json(path, data)

    return dict_to_model(pred_data, Prediction)


def _find_prediction_by_id(pred_id: str) -> tuple[dict | None, Path | None]:
    """在所有 predicts 文件中查找指定 id 的预测"""
    for fpath in sorted(PREDICTS_DIR.glob("*.json")):
        data = _read_json(fpath)
        if isinstance(data, list):
            for p in data:
                if p.get("id") == pred_id:
                    return p, fpath
    return None, None


def get_predictions(lota_id: str = None, ptype: str = None) -> list[dict]:
    """查询预测，可按 lota_id / type 过滤"""
    result = []
    files = [PREDICTS_DIR / f"{lota_id}.json"] if lota_id else sorted(PREDICTS_DIR.glob("*.json"))
    for fpath in files:
        data = _read_json(fpath)
        if isinstance(data, list):
            for p in data:
                if ptype and p.get("type") != ptype:
                    continue
                result.append(p)
    return result


def remove_prediction(pred_id: str) -> bool:
    """按 id 删除预测"""
    _, path = _find_prediction_by_id(pred_id)
    if not path:
        return False
    data = _read_json(path) or []
    before = len(data)
    data = [p for p in data if p.get("id") != pred_id]
    if len(data) < before:
        _write_json(path, data)
        return True
    return False


# ═══════════════════════════════════════════════
# Factor
# ═══════════════════════════════════════════════

def save_factor(factor: Factor) -> Factor:
    """
    写入决策因子。检查逻辑:
      1. slugs 非空
      2. slugs 必须在已知 section slug 白名单内
      3. content 非空
    """
    if not factor.slugs:
        raise ValueError("Factor.slugs 不能为空")
    if not factor.content.strip():
        raise ValueError("Factor.content 不能为空")

    # ── 白名单: tools._SECTION_RULES 中的 slug ──
    valid_slugs = _get_valid_section_slugs()
    invalid = [s for s in factor.slugs if s not in valid_slugs]
    if invalid:
        raise ValueError(f"无效 slug: {invalid}，合法值: {valid_slugs}")

    path = FACTORS_DIR / f"{factor.id}.json"
    d = model_to_dict(factor)
    _write_json(path, d)
    return factor


def _get_valid_section_slugs() -> set[str]:
    """从 tools._SECTION_RULES 获取合法 slug 列表"""
    try:
        from src.tools import _SECTION_RULES
        return {slug for slug, _ in _SECTION_RULES}
    except Exception:
        return set()


def get_factor(factor_id: str) -> Optional[Factor]:
    d = _read_json(FACTORS_DIR / f"{factor_id}.json")
    return dict_to_model(d, Factor) if d else None


def list_factors() -> list[dict]:
    result = []
    for fpath in sorted(FACTORS_DIR.glob("*.json")):
        d = _read_json(fpath)
        if d:
            result.append(d)
    return result


def remove_factor(factor_id: str) -> bool:
    path = FACTORS_DIR / f"{factor_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
# Order — 虚拟投注 CRUD + 结算
# ═══════════════════════════════════════════════

ORDERS_DIR = DATA_ROOT / "orders"

for d in [ORDERS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


def save_order(order: "Order | dict") -> "Order":
    """写入订单。按 lota_id 分文件。接受 Order 对象或 dict。"""
    from src.models import Order, model_to_dict
    if isinstance(order, dict):
        lota_id = order.get("lota_id", "")
        d = order
    else:
        lota_id = order.lota_id
        d = model_to_dict(order)
    path = ORDERS_DIR / f"{lota_id}.json"
    data = _read_json(path) or []
    if not isinstance(data, list):
        data = []

    found = False
    for i, o in enumerate(data):
        if o.get("id") == d.get("id"):
            data[i] = d
            found = True
            break
    if not found:
        data.append(d)

    _write_json(path, data)
    return order


def get_orders(lota_id: str = None) -> list[dict]:
    """查询订单，可按 lota_id 过滤"""
    result = []
    files = [ORDERS_DIR / f"{lota_id}.json"] if lota_id else sorted(ORDERS_DIR.glob("*.json"))
    for fpath in files:
        data = _read_json(fpath)
        if isinstance(data, list):
            result.extend(data)
    return result


def get_order(order_id: str) -> Optional[dict]:
    """按 id 查找订单"""
    for fpath in sorted(ORDERS_DIR.glob("*.json")):
        data = _read_json(fpath)
        if isinstance(data, list):
            for o in data:
                if o.get("id") == order_id:
                    return o
    return None


def remove_order(order_id: str) -> bool:
    """按 id 删除订单"""
    for fpath in sorted(ORDERS_DIR.glob("*.json")):
        data = _read_json(fpath) or []
        if not isinstance(data, list):
            continue
        before = len(data)
        data = [o for o in data if o.get("id") != order_id]
        if len(data) < before:
            _write_json(fpath, data)
            return True
    return False


# ═══════════════════════════════════════════════
# Odds → Prediction 富化
# ═══════════════════════════════════════════════

def populate_prediction_odds(lota_id: str) -> int:
    """
    从 features 缓存提取 Pinnacle 赔率，写入该场比赛所有预测的 odds 字段。

    Returns: 富化的预测数量
    """
    from src.tools import extract_odds

    odds = extract_odds(lota_id)
    if not odds:
        return 0

    path = PREDICTS_DIR / f"{lota_id}.json"
    data = _read_json(path) or []
    if not isinstance(data, list):
        return 0

    updated = 0
    for p in data:
        ptype = p.get("type", "")
        if ptype == "胜平负" and odds.get("eu"):
            p["odds"] = odds["eu"]
            updated += 1
        elif ptype == "亚盘" and odds.get("asian"):
            p["odds"] = odds["asian"]
            updated += 1
        elif ptype == "大小球" and odds.get("ou"):
            p["odds"] = odds["ou"]
            updated += 1
        # 比分、进球数 不关联赔率

    if updated > 0:
        _write_json(path, data)

    return updated


# ═══════════════════════════════════════════════
# Order 创建（从 Prediction）
# ═══════════════════════════════════════════════

def create_order_from_prediction(pred: dict) -> Optional[dict]:
    """
    从一条预测创建投注订单。

    规则:
      - 胜平负: pick = value.result (H/D/A), odds = odds.h/d/a 中对应项
      - 亚盘:   pick = value.direction, odds = odds.h/a 中对应项, handicap = value.handicap
      - 大小球: pick = value.direction, odds = odds.over/under 中对应项, threshold = value.threshold
      - 比分/进球数: 不创建 (返回 None)

    返回: order dict 或 None
    """
    from src.models import Order, model_to_dict

    ptype = pred.get("type", "")
    value = pred.get("value", {})
    odds_data = pred.get("odds", {})

    if ptype not in ("胜平负", "亚盘", "大小球"):
        return None

    if not odds_data:
        return None  # 没有赔率数据，无法创建订单

    order = Order(
        predict_id=pred.get("id", ""),
        lota_id=pred.get("lota_id", ""),
        bet_type=ptype,
    )

    if ptype == "胜平负":
        pick = value.get("result", "")
        order.pick = pick
        if pick == "H":
            order.odds = float(odds_data.get("h", 0))
        elif pick == "D":
            order.odds = float(odds_data.get("d", 0))
        elif pick == "A":
            order.odds = float(odds_data.get("a", 0))
        else:
            return None

    elif ptype == "亚盘":
        pick = value.get("direction", "")
        order.pick = pick
        order.handicap = float(value.get("handicap", 0))
        if pick == "H":
            order.odds = float(odds_data.get("h", 0))
        elif pick == "A":
            order.odds = float(odds_data.get("a", 0))
        else:
            return None

    elif ptype == "大小球":
        pick = value.get("direction", "")
        order.pick = pick
        order.handicap = float(value.get("threshold", 0))
        if pick == "over":
            order.odds = float(odds_data.get("over", 0))
        elif pick == "under":
            order.odds = float(odds_data.get("under", 0))
        else:
            return None

    if order.odds <= 0:
        return None

    return model_to_dict(order)


# ═══════════════════════════════════════════════
# Order 结算
# ═══════════════════════════════════════════════

def settle_order(order_data: dict, score: str) -> dict:
    """
    用比分结算订单。支持赢半/输半（quarter-ball 盘口拆两半结算）。

    盘口类型:
      - 整数 (0, 1, 2...): 可走水
      - 半球 (0.5, 1.5...): 无走水
      - quarter (0.25, 0.75, 1.25...): 拆成 hc±0.25 两半各自结算

    Returns: 更新后的 order dict
    """
    import re
    from src.tools import score2goal_diff, score2goal_sum, score2_1x2

    if not re.match(r'^\d+:\d+$', score):
        raise ValueError(f"比分格式错误: {score}")

    hg, ag = map(int, score.split(":"))
    diff = hg - ag
    total = hg + ag

    bet_type = order_data.get("bet_type", "")
    pick = order_data.get("pick", "")
    handicap = float(order_data.get("handicap") or 0)  # 负=主让 正=主受(主队视觉), adj=diff+handicap直接判定
    odds = float(order_data.get("odds") or 0)
    bet_size = float(order_data.get("bet_size") or 100)

    # ── 胜平负（无 quarter-ball）──
    if bet_type == "胜平负":
        actual = score2_1x2(score)
        hit = (pick == actual) if pick in ("H", "D", "A") else None

        if hit is None:
            return_amount, profit = bet_size, 0.0
        elif hit is True:
            return_amount = bet_size * odds
            profit = return_amount - bet_size
        else:
            return_amount, profit = 0.0, -bet_size

    # ── 亚盘 / 大小球（港赔水位，支持 quarter-ball）──
    else:
        hit, return_amount, profit = _settle_hk_quarter(
            bet_type=bet_type,
            pick=pick,
            handicap=handicap,
            odds=odds,
            bet_size=bet_size,
            diff=diff,
            total=total,
        )

    order_data["hit"] = hit
    order_data["return_amount"] = round(return_amount, 2)
    order_data["profit"] = round(profit, 2)
    order_data["score"] = score
    order_data["settled_at"] = datetime.now().isoformat()

    return order_data


def _settle_hk_quarter(bet_type: str, pick: str, handicap: float,
                        odds: float, bet_size: float,
                        diff: int, total: int) -> tuple:
    """
    港赔 quarter-ball 结算（亚盘 & 大小球通用）。

    quarter-ball (hc % 0.5 != 0): 拆成 hc-0.25 和 hc+0.25 两半，
    各自独立结算后合并返还。

    Returns: (hit, return_amount, profit)
      hit: True=全赢, False=全输, None=走水/半赢半输
    """
    is_quarter = abs(handicap % 0.5) > 0.001

    if not is_quarter:
        # ── 整数 / 半球：简单判定 ──
        if bet_type == "亚盘":
            adj = diff + handicap  # hc<0=主让(主队视觉), adj>0=主队赢盘
            if adj == 0:
                return None, bet_size, 0.0  # push
            if pick == "H":
                win = adj > 0
            else:
                win = adj < 0
        else:  # 大小球
            if total == handicap:
                return None, bet_size, 0.0  # push
            if pick == "over":
                win = total > handicap
            else:
                win = total < handicap

        if win:
            return True, bet_size * (1 + odds), bet_size * odds
        else:
            return False, 0.0, -bet_size

    # ── quarter-ball: 拆两半 ──
    hc1 = handicap - 0.25
    hc2 = handicap + 0.25

    def _half_result(hc: float) -> str:
        """单一半的结算结果: 'win' | 'push' | 'lose'"""
        if bet_type == "亚盘":
            adj = diff + hc
            if adj == 0:
                return "push"
            if pick == "H":
                return "win" if adj > 0 else "lose"
            else:
                return "win" if adj < 0 else "lose"
        else:  # 大小球
            if total == hc:
                return "push"
            if pick == "over":
                return "win" if total > hc else "lose"
            else:
                return "win" if total < hc else "lose"

    r1 = _half_result(hc1)
    r2 = _half_result(hc2)

    half_bet = bet_size / 2
    ret = 0.0
    for r in [r1, r2]:
        if r == "win":
            ret += half_bet * (1 + odds)
        elif r == "push":
            ret += half_bet
        # lose: +0

    profit = ret - bet_size

    # hit 判定
    wins = (r1 == "win") + (r2 == "win")
    losses = (r1 == "lose") + (r2 == "lose")
    if wins == 2:
        hit = True
    elif losses == 2:
        hit = False
    else:
        hit = None  # mixed: 赢半/输半/双走水

    return hit, ret, profit


# ═══════════════════════════════════════════════
# 一键流水线
# ═══════════════════════════════════════════════

def run_order_pipeline(lota_id: str = None, dry_run: bool = False) -> dict:
    """
    一键执行: 提取赔率 → 填充 Prediction.odds → 创建 Order → 结算。

    Args:
        lota_id: 单场比赛 lota_id，None=所有
        dry_run: True=只统计不写盘

    Returns:
        {
          "odds_populated": int,    # 富化的预测数
          "orders_created": int,    # 创建的订单数
          "orders_settled": int,    # 结算的订单数
          "summary": {              # 汇总
            "total": int,
            "hit": int,
            "miss": int,
            "push": int,
            "total_bet": float,
            "total_return": float,
            "roi": float,
          }
        }
    """
    # ── 确定处理范围 ──
    if lota_id:
        lid_list = [lota_id]
    else:
        lid_list = sorted(
            f.stem for f in PREDICTS_DIR.glob("*.json") if f.stem
        )

    # ── 读取比分 ──
    from src.tools import get_cached_compact_fet
    scores: dict[str, str] = {}
    for lid in lid_list:
        feat = get_cached_compact_fet(lid)
        if feat:
            sc = (feat.get("data") or {}).get("score", "")
            if sc:
                scores[lid] = sc

    # ── 处理 ──
    odds_populated = 0
    orders_created = 0
    orders_settled = 0
    summary = {"total": 0, "hit": 0, "miss": 0, "push": 0,
               "total_bet": 0.0, "total_return": 0.0, "roi": 0.0}

    for lid in lid_list:
        # 1. 填充赔率
        n = populate_prediction_odds(lid)
        odds_populated += n

        # 2. 读取预测（已含 odds）
        pred_path = PREDICTS_DIR / f"{lid}.json"
        preds = _read_json(pred_path) or []
        if not isinstance(preds, list):
            continue

        for p in preds:
            # 创建订单
            order = create_order_from_prediction(p)
            if not order:
                continue
            orders_created += 1

            # 结算
            sc = scores.get(lid, "")
            if sc:
                order = settle_order(order, sc)
                orders_settled += 1

                # 统计
                summary["total"] += 1
                if order["hit"] is True:
                    summary["hit"] += 1
                elif order["hit"] is False:
                    summary["miss"] += 1
                else:
                    summary["push"] += 1
                summary["total_bet"] += order.get("bet_size", 100)
                summary["total_return"] += order.get("return_amount", 0)

            # 写盘
            if not dry_run:
                save_order(order)

    # ── ROI ──
    if summary["total_bet"] > 0:
        summary["roi"] = round(
            (summary["total_return"] - summary["total_bet"]) / summary["total_bet"] * 100, 2
        )

    return {
        "odds_populated": odds_populated,
        "orders_created": orders_created,
        "orders_settled": orders_settled,
        "summary": summary,
    }


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv
    lota_id = None
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            lota_id = a
            break

    print(f"[store] 执行 order pipeline...")
    if dry_run:
        print("[store] dry-run 模式")
    if lota_id:
        print(f"[store] 单场: {lota_id}")

    result = run_order_pipeline(lota_id=lota_id, dry_run=dry_run)

    print(f"\n赔率富化: {result['odds_populated']} 条")
    print(f"订单创建: {result['orders_created']} 条")
    print(f"订单结算: {result['orders_settled']} 条")

    s = result["summary"]
    denom = s["total"] - s["push"]
    hit_rate = f"{s['hit']/denom*100:.1f}%" if denom > 0 else "-"
    print(f"\n{'='*50}")
    print(f"订单总数: {s['total']}  |  命中: {s['hit']}  |  未中: {s['miss']}  |  走水: {s['push']}  |  命中率: {hit_rate}")
    print(f"总投注: {s['total_bet']:.0f}  |  总返还: {s['total_return']:.0f}  |  盈亏: {s['total_return']-s['total_bet']:+.0f}  |  ROI: {s['roi']:+.1f}%")
    print(f"{'='*50}")
