"""
DSFootball Python CLI — 角色对象

命名的博彩角色，捆绑策略、资金、订单、预测。
支持持久化到独立目录，不同角色数据隔离。
"""

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

from .models import Order, Prediction, SystemPrompt, model_to_dict
from .memory import AgentMemory


# ═══════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════

ROLES_DIR = Path(__file__).parent.parent / "lota_data" / "roles"
ROLES_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now().isoformat()


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
# Role
# ═══════════════════════════════════════════════

class Role:
    """
    命名的博彩角色。

    用法:
      r = Role(name='trader-1', capital=10000)
      r.system_prompt = PromptBuilder().ensure_baseline()
      r.save()

      r = Role.load('trader-1')
      r.place_order(order_dict)   # 扣资金
      r.settle_order(order_dict)  # 更新资金
      r.pnl()                     # 盈亏
    """

    def __init__(
        self,
        name: str = "default",
        capital: float = 10000.0,
        system_prompt_name: str = "baseline-v1",
    ):
        self.name = name
        self.initial_capital = capital
        self.capital = capital
        self.system_prompt_name = system_prompt_name
        self.alpha_mode = False  # 跨Agent因子读取开关
        self.cross_factor_exclude: list[str] = []  # 排除某些角色的因子
        self.memory = AgentMemory(role_name=name)
        self.created_at = _now()
        self.orders = []  # 订单统一存 role.json，不再单独落盘

        # 角色专属目录
        self._role_dir = ROLES_DIR / name
        self._predicts_dir = self._role_dir / "predicts"
        for d in [self._role_dir, self._predicts_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # persona.md — 用户自然语言偏好描述，自动注入 prompt
        self._persona_path = self._role_dir / "persona.md"

    # ═══════════════════════════════════════════
    # 持久化
    # ═══════════════════════════════════════════

    def save(self) -> None:
        """保存角色状态到 roles/{name}/{name}.json"""
        path = self._role_dir / f"{self.name}.json"
        _write_json(path, {
            "name": self.name,
            "capital": self.capital,
            "initial_capital": self.initial_capital,
            "system_prompt_name": self.system_prompt_name,
            "alpha_mode": self.alpha_mode,
            "cross_factor_exclude": self.cross_factor_exclude,
            "updated_at": _now(),
            "orders": self.orders,
        })

    @classmethod
    def load(cls, name: str) -> "Role":
        """从磁盘加载角色"""
        path = ROLES_DIR / name / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"角色 '{name}' 不存在")

        data = _read_json(path)
        if not data:
            raise ValueError(f"角色 '{name}' 数据损坏")

        r = cls(
            name=data.get("name", name),
            capital=data.get("capital", 10000),
            system_prompt_name=data.get("system_prompt_name", "baseline-v1"),
        )
        r.initial_capital = data.get("initial_capital", r.capital)
        r.created_at = data.get("created_at", r.created_at)
        r.alpha_mode = data.get("alpha_mode", False)
        r.cross_factor_exclude = data.get("cross_factor_exclude", [])

        # 加载订单：优先从 role.json，兼容旧格式迁移
        r.orders = data.get("orders", [])
        if not r.orders:
            r._migrate_orders_from_disk()

        # 加载订单记忆
        r.memory.refresh_from_role(r)
        return r

    def persona_text(self) -> str:
        """读取 roles/{name}/persona.md，注入 prompt。文件不存在则返回空字符串。"""
        if self._persona_path.exists():
            text = self._persona_path.read_text(encoding="utf-8").strip()
            if text:
                return "## 🎯 个人偏好\n\n" + text
        return ""

    @classmethod
    def list_all(cls) -> list[dict]:
        """列出所有角色"""
        result = []
        for d in sorted(ROLES_DIR.iterdir()):
            if d.is_dir():
                cfg_path = d / f"{d.name}.json"
                if cfg_path.exists():
                    data = _read_json(cfg_path)
                    if data:
                        result.append({
                            "name": data.get("name", d.name),
                            "capital": data.get("capital", 0),
                            "initial_capital": data.get("initial_capital", 0),
                            "pnl": data.get("capital", 0) - data.get("initial_capital", 0),
                            "updated_at": data.get("updated_at", "")[:19],
                        })
        return result

    # ═══════════════════════════════════════════
    # 资金
    # ═══════════════════════════════════════════

    def deposit(self, amount: float) -> None:
        self.capital += amount

    def withdraw(self, amount: float) -> None:
        if amount > self.capital:
            raise ValueError(f"资金不足: {self.capital} < {amount}")
        self.capital -= amount

    def pnl(self) -> float:
        return self.capital - self.initial_capital

    # ── 资金曲线 ──
    def record_capital_snapshot(self, day_date: str) -> None:
        """每日结算后记录资金快照，用于前端展示曲线"""
        import json
        path = self._role_dir / "capital_history.json"
        history = []
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # 同一天覆盖（重跑时更新）
        for h in history:
            if h.get("date") == day_date:
                h["capital"] = self.capital
                h["pnl"] = self.pnl()
                break
        else:
            history.append({
                "date": day_date,
                "capital": self.capital,
                "pnl": self.pnl(),
            })
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_capital_history(self) -> list[dict]:
        import json
        path = self._role_dir / "capital_history.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _migrate_orders_from_disk(self) -> None:
        """一次性迁移：从旧 orders/ 目录读取订单到 self.orders，然后删除目录"""
        import shutil
        old_dir = self._role_dir / "orders"
        if not old_dir.exists():
            return
        for fpath in sorted(old_dir.glob("*.json")):
            data = _read_json(fpath)
            if isinstance(data, list):
                self.orders.extend(data)
        if self.orders:
            self.save()
        shutil.rmtree(old_dir, ignore_errors=True)

    # ═══════════════════════════════════════════
    # 订单
    # ═══════════════════════════════════════════

    def get_orders(self, lota_id: str = None) -> list[dict]:
        """查询角色订单（从内存列表）"""
        if lota_id:
            return [o for o in self.orders if o.get("lota_id") == lota_id]
        return list(self.orders)

    def remove_order(self, order_id: str) -> bool:
        """按 id 删除订单"""
        before = len(self.orders)
        self.orders = [o for o in self.orders if o.get("id") != order_id]
        if len(self.orders) < before:
            self.save()
            return True
        return False

    def save_order(self, order: dict) -> None:
        """保存订单到内存列表 + 持久化到 role.json"""
        oid = order.get("id", "")
        for i, o in enumerate(self.orders):
            if o.get("id") == oid:
                self.orders[i] = order
                self.save()
                return
        self.orders.append(order)
        self.save()

    def place_order(self, order_data: dict) -> dict:
        """
        下注：保存订单并从资金中扣除 bet_size。
        返回更新后的 order（含 id）。
        """
        bet_size = float(order_data.get("bet_size", 100))
        if bet_size > self.capital:
            raise ValueError(f"资金不足: {self.capital} < {bet_size}")

        # 确保有 id
        if not order_data.get("id"):
            from src.models import _uid
            order_data["id"] = _uid("ord_")
        if not order_data.get("created_at"):
            order_data["created_at"] = _now()

        self.withdraw(bet_size)
        self.save_order(order_data)
        return order_data

    def settle_order(self, order_data: dict, score: str) -> dict:
        """
        结算订单：用比分判定、更新资金、写回。
        """
        from src.store import settle_order as _settle

        order_data = _settle(order_data, score)
        return_amount = order_data.get("return_amount", 0)
        self.deposit(return_amount)

        # 写回
        self.save_order(order_data)
        self.save()  # 资金变动同步到磁盘
        return order_data

    # ═══════════════════════════════════════════
    # 预测
    # ═══════════════════════════════════════════

    def get_predictions(self, lota_id: str = None) -> list[dict]:
        """查询角色预测"""
        if lota_id:
            path = self._predicts_dir / f"{lota_id}.json"
            data = _read_json(path)
            return data if isinstance(data, list) else []
        else:
            result = []
            for fpath in sorted(self._predicts_dir.glob("*.json")):
                data = _read_json(fpath)
                if isinstance(data, list):
                    result.extend(data)
            return result

    def save_prediction(self, pred: dict) -> None:
        """保存预测到角色专属目录"""
        lid = pred.get("lota_id", "unknown")
        path = self._predicts_dir / f"{lid}.json"
        data = _read_json(path) or []
        if not isinstance(data, list):
            data = []
        pid = pred.get("id", "")
        found = False
        for i, p in enumerate(data):
            if p.get("id") == pid:
                data[i] = pred
                found = True
                break
        if not found:
            data.append(pred)
        _write_json(path, data)

    def soft_reset(self, capital: float = None) -> None:
        """轻量重置：只清空订单+资金曲线+重置资金，保留因子记忆。

        用于 live 模式快速重来，不丢失已学习的因子。
        """
        import shutil

        self.orders.clear()
        old_orders = self._role_dir / "orders"
        if old_orders.exists():
            shutil.rmtree(old_orders)

        for f in self._predicts_dir.glob("*.json"):
            f.unlink()

        if capital is not None:
            self.initial_capital = float(capital)
            self.capital = float(capital)
        else:
            self.capital = self.initial_capital

        hist = self._role_dir / "capital_history.json"
        if hist.exists():
            hist.unlink()

        self.save()

    # ═══════════════════════════════════════════
    # 重置
    # ═══════════════════════════════════════════

    def reset(self, capital: float = None) -> None:
        """重置角色到初始状态：清空订单/预测/记忆/资金曲线，恢复初始资金。

        如果指定 capital，则同时重置 initial_capital 和当前资金为该值；
        否则恢复到角色创建时的 initial_capital。
        """
        import shutil

        # 1. 清空订单
        self.orders.clear()
        # 清理旧 orders 目录（兼容迁移前残留）
        old_orders = self._role_dir / "orders"
        if old_orders.exists():
            shutil.rmtree(old_orders)

        # 2. 清空预测
        for f in self._predicts_dir.glob("*.json"):
            f.unlink()

        # 3. 重置资金 + 清空资金曲线
        if capital is not None:
            self.initial_capital = float(capital)
            self.capital = float(capital)
        else:
            self.capital = self.initial_capital
        hist = self._role_dir / "capital_history.json"
        if hist.exists():
            hist.unlink()

        # 4. 清空记忆（磁盘 + 内存）
        mem_dir = self._role_dir / "memory"
        if mem_dir.exists():
            shutil.rmtree(mem_dir)
        self.memory = AgentMemory(role_name=self.name)
        self.memory.refresh_from_role(self)

        # 5. 持久化
        self.save()

    # ═══════════════════════════════════════════
    # 统计
    # ═══════════════════════════════════════════

    def stats(self) -> dict:
        """角色订单统计"""
        orders = self.get_orders()
        by_type = defaultdict(lambda: {"total": 0, "hit": 0, "miss": 0, "push": 0, "profit": 0.0, "bet": 0.0})
        total_bet = 0.0
        total_return = 0.0
        settled = 0

        for o in orders:
            if o.get("settled_at") is None:
                continue
            settled += 1
            bt = o.get("bet_type", "其他")
            bs = float(o.get("bet_size", 100))
            by_type[bt]["total"] += 1
            by_type[bt]["bet"] += bs
            by_type[bt]["profit"] += o.get("profit", 0)
            h = o.get("hit")
            if h is True:       by_type[bt]["hit"] += 1
            elif h is False:    by_type[bt]["miss"] += 1
            else:               by_type[bt]["push"] += 1
            total_bet += bs
            total_return += o.get("return_amount", 0)

        result = {
            "total_orders": len(orders),
            "settled": settled,
            "pending": len(orders) - settled,
            "total_bet": total_bet,
            "total_return": total_return,
            "pnl": self.pnl(),
            "roi": round((total_return - total_bet) / total_bet * 100, 1) if total_bet > 0 else 0,
            "by_type": {},
        }

        for bt in ["胜平负", "亚盘", "大小球"]:
            s = by_type.get(bt)
            if s and s["total"] > 0:
                denom = s["total"] - s["push"]
                result["by_type"][bt] = {
                    "total": s["total"],
                    "hit": s["hit"],
                    "miss": s["miss"],
                    "push": s["push"],
                    "hit_rate": round(s["hit"] / denom * 100, 1) if denom > 0 else 0,
                    "profit": round(s["profit"], 2),
                    "roi": round(s["profit"] / s["bet"] * 100, 1) if s["bet"] > 0 else 0,
                }

        return result


# ═══════════════════════════════════════════════
# AgentMemory 扩展 — 从角色加载
# ═══════════════════════════════════════════════

def _agent_memory_refresh_from_role(self, role: "Role") -> None:
    """从角色订单初始化记忆（monkey-patch AgentMemory）"""
    orders = role.get_orders()
    if not orders:
        return

    orders.sort(key=lambda o: o.get("created_at", ""))

    self.orders.recent_orders = [
        {
            "lota_id": o.get("lota_id", ""),
            "bet_type": o.get("bet_type", ""),
            "pick": o.get("pick", ""),
            "odds": o.get("odds", 0),
            "handicap": o.get("handicap"),
            "bet_size": o.get("bet_size", 100),
            "hit": o.get("hit"),
            "profit": o.get("profit", 0),
            "created_at": o.get("created_at", ""),
        }
        for o in orders
    ]

    by_type = defaultdict(lambda: {"total": 0, "hit": 0, "miss": 0, "push": 0, "profit": 0.0})
    for o in orders:
        bt = o.get("bet_type", "其他")
        by_type[bt]["total"] += 1
        h = o.get("hit")
        if h is True:       by_type[bt]["hit"] += 1
        elif h is False:    by_type[bt]["miss"] += 1
        else:               by_type[bt]["push"] += 1
        by_type[bt]["profit"] += o.get("profit", 0)

    self.orders.stats = {
        bt: {
            "total": s["total"],
            "hit": s["hit"], "miss": s["miss"], "push": s["push"],
            "profit": round(s["profit"], 2),
            "hit_rate": round(s["hit"] / (s["total"] - s["push"]) * 100, 1) if (s["total"] - s["push"]) > 0 else 0,
            "roi": round(s["profit"] / (s["total"] * 100) * 100, 1) if s["total"] > 0 else 0,
        }
        for bt, s in by_type.items()
    }
    total_profit = sum(s["profit"] for s in by_type.values())
    total_orders = sum(s["total"] for s in by_type.values())
    self.orders.total_pnl = round(total_profit, 2)
    self.orders.stats["overall"] = {
        "total": total_orders,
        "profit": self.orders.total_pnl,
        "roi": round(total_profit / (total_orders * 100) * 100, 1) if total_orders > 0 else 0,
    }
    self.orders.win_streak, self.orders.lose_streak = self.orders._calc_streaks(orders)
    self.orders._loaded = True

    # Loss memory
    losses = [o for o in orders if o.get("profit", 0) < 0]
    patterns = defaultdict(int)
    for o in losses:
        patterns[f"{o.get('bet_type','')}:{o.get('pick','')}"] += 1
    self.losses.notable_losses = sorted(
        [o for o in losses if o.get("profit", 0) <= -100],
        key=lambda x: x["profit"]
    )[:30]
    self.losses.patterns = dict(sorted(patterns.items(), key=lambda x: -x[1]))
    self.losses.max_single_loss = min((o.get("profit", 0) for o in losses), default=0)
    self.losses._loaded = True

    # Slug memory + reflections — load from disk
    self.slugs.load()
    self.reflections.load()


AgentMemory.refresh_from_role = _agent_memory_refresh_from_role


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  python role.py list")
        print("  python role.py create <name> [capital]")
        print("  python role.py stats <name>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        roles = Role.list_all()
        if not roles:
            print("(无角色)")
        for r in roles:
            print(f"  {r['name']:<16} 资金 {r['capital']:.0f}  PnL {r['pnl']:+.0f}  {r['updated_at']}")

    elif cmd == "create":
        name = sys.argv[2] if len(sys.argv) > 2 else "default"
        capital = float(sys.argv[3]) if len(sys.argv) > 3 else 10000
        r = Role(name=name, capital=capital)
        r.save()
        print(f"已创建角色 '{name}' (资金 {capital})")

    elif cmd == "stats":
        name = sys.argv[2] if len(sys.argv) > 2 else "default"
        r = Role.load(name)
        s = r.stats()
        print(f"角色: {name}")
        print(f"资金: {r.capital:.0f} (初始 {r.initial_capital:.0f}, PnL {r.pnl():+.0f})")
        print(f"订单: {s['total_orders']} 总 / {s['settled']} 已结算 / {s['pending']} 待定")
        print(f"ROI: {s['roi']:+.1f}%")
        for bt, st in s.get("by_type", {}).items():
            print(f"  {bt}: {st['total']}单 命中{st['hit_rate']}% 盈亏{st['profit']:+.0f} ROI{st['roi']:+.1f}%")

    else:
        print(f"未知命令: {cmd}")
