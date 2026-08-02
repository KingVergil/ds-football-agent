"""
DSFootball Python CLI — Prompt 组装 & Agent 策略管理

SystemPrompt: 可固化、可版本迭代的 agent 策略配置
PromptBuilder: 组装 system prompt（记忆 + 数据 + 框架 → LLM 输入）
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from .models import SystemPrompt, model_to_dict, dict_to_model
from .memory import AgentMemory
from .data_manager import DataManager


# ═══════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════

PROMPTS_DIR = Path(__file__).parent.parent / "lota_data" / "agent_prompts"
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

_dm = DataManager()


def _now() -> str:
    return datetime.now().isoformat()


# ═══════════════════════════════════════════════
# Token 工具
# ═══════════════════════════════════════════════

def count_tokens(text: str) -> int:
    """
    粗略估算 token 数。
    中文 ≈1.5 token/char, 英文/数字 ≈0.3 token/char。
    """
    cjk = sum(1 for c in text if '一' <= c <= '鿿' or '　' <= c <= '〿')
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.3)


def truncate_section(text: str, max_tokens: int) -> str:
    """按 token 预算从尾部截断（保留前面的内容更重要）"""
    if count_tokens(text) <= max_tokens:
        return text
    # 二分查找截断点
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            lo = mid + 1
        else:
            hi = mid
    return text[:lo - 1] + "\n...(truncated)"


# ═══════════════════════════════════════════════
# SystemPrompt CRUD
# ═══════════════════════════════════════════════

def create_system_prompt(name: str, **kwargs) -> SystemPrompt:
    """创建新策略。如果同名已存在则报错。"""
    path = PROMPTS_DIR / f"{name}.json"
    if path.exists():
        raise FileExistsError(f"策略 '{name}' 已存在，使用 clone() 或手动删除")

    sp = SystemPrompt(name=name, **kwargs)
    save_system_prompt(sp)
    return sp


def save_system_prompt(sp: SystemPrompt) -> None:
    """持久化策略到 agent_prompts/{name}.json"""
    sp.updated_at = _now()
    path = PROMPTS_DIR / f"{sp.name}.json"
    path.write_text(
        json.dumps(model_to_dict(sp), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_system_prompt(name: str) -> Optional[SystemPrompt]:
    """加载已保存的策略"""
    path = PROMPTS_DIR / f"{name}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict_to_model(data, SystemPrompt)


def list_system_prompts() -> list[dict]:
    """列出所有已保存策略的摘要"""
    result = []
    for fpath in sorted(PROMPTS_DIR.glob("*.json")):
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            result.append({
                "name": data.get("name", fpath.stem),
                "version": data.get("version", 1),
                "updated_at": data.get("updated_at", "")[:19],
                "notes": (data.get("notes", "") or "")[:80],
            })
        except Exception:
            pass
    return result


def clone_system_prompt(from_name: str, to_name: str) -> SystemPrompt:
    """基于已有策略创建变体"""
    sp = load_system_prompt(from_name)
    if not sp:
        raise FileNotFoundError(f"策略 '{from_name}' 不存在")
    sp.name = to_name
    sp.version += 1
    sp.id = ""
    sp.created_at = _now()
    sp.updated_at = _now()
    sp.notes = f"clone from {from_name}"
    save_system_prompt(sp)
    return sp


# ═══════════════════════════════════════════════
# 内置默认策略
# ═══════════════════════════════════════════════

def _default_baseline() -> SystemPrompt:
    """内置 baseline 策略模板"""
    return SystemPrompt(
        name="baseline-v1",
        version=1,
        role="你是职业足球博彩玩家。自主学习，根据比赛数据、赔率、历史表现，独立决策是否下注。",
        memory_config={
            "max_recent_orders": 20,
            "include_summary": True,
            "include_streaks": True,
            "include_loss_patterns": False,
            "include_factor_perf": True,
            "include_slug_perf": True,
        },
        default_slugs=[
            "match-head", "fair-odds", "eu-odds-pinnacle",
            "asian-handicap-pinnacle", "over-under-crown",
            "betfair-buysell", "discrete-odds",
        ],
        framework="""## 决策框架

1. 先判断有没有明确信号——没有信号就 skip，不要为下注而下注
2. 从三种可投注类型中选择最有信心的：
   - 胜平负（欧赔）：看欧盘概率 vs 公平盘偏差
   - 亚盘：看让球方向 + 水位 + quarter-ball 拆解。盘口值始终取主队视角（正=主受让，负=主让球）
   - 大小球：看总进球预期 + 盘口位置
3. 历史战绩连续亏损时要收紧，连续赢可以保持或加注""",
        bet_sizing="""## 资金管理
- 当前资金: {capital}
- 根据信心强度自主决定下注金额
- 单日总下注不可超过当前资金
- 不下注时输出 skip""",
        output_format="""## 输出格式

每场比赛输出一个独立 order 区块，**必须包含 lota_id**：

```order
lota_id: Lota4459820
类型: 胜平负
pick: H
赔率: 1.45
金额: 200
理由: xxx
```

不下注的比赛也要输出，金额填 0：
```order
lota_id: Lota4460938
类型: skip
理由: 信号不明确
```

⚠️ 关键规则:
1. 每场比赛一个 ```order``` 区块
2. lota_id 必须从比赛数据中抄过来，不能省略
3. pick 只能是 H/D/A(胜平负) H/A(亚盘) over/under(大小球)，不要加队名
4. 金额: 200(高置信度) 100(中) 50(低) 0(skip)

### 亚盘盘口规则 ⚠️ 最重要

盘口值在每场比赛数据里以 `盘口值(主队): +X.XX` 格式给出。
**直接把那个数字原样抄到 order 的盘口字段，不准改符号、不准自己算。**

示例：
  比赛数据显示: 盘口值(主队): +1.00  ← 下单写 盘口: +1.00
  比赛数据显示: 盘口值(主队): -0.75  ← 下单写 盘口: -0.75
  比赛数据显示: 盘口值(主队): 0      ← 下单写 盘口: 0

❌ 错误: 自己推断符号方向
✅ 正确: 比赛数据里写什么数字就抄什么数字""",
        notes="初始基线策略，保守型",
    )


# ═══════════════════════════════════════════════
# PromptBuilder
# ═══════════════════════════════════════════════

class PromptBuilder:
    """
    Prompt 组装器。支持单场/多场 + 各自 factor 组合。

    用法:
      builder = PromptBuilder()
      sp = builder.ensure_baseline()

      # 单场
      result = builder.build(sp, matches=['Lota4459820'])

      # 多场 + 各自 factor
      result = builder.build(sp, matches=[
          {'lota_id': 'Lota4459820', 'factors': [factor_a]},
          {'lota_id': 'Lota4459821', 'factors': [factor_b]},
      ])

      # 20 场批量（充分利用 1M context）
      result = builder.build(sp, matches=lid_list, max_matches=20)
    """

    TOKEN_BUDGET = 800000       # 默认 800K（接近 1M，留 buffer）
    MAX_MATCHES_DEFAULT = 50    # 单次最多比赛数
    TOKENS_PER_MATCH = 3000     # 每场数据段预算（超出则截断）

    def ensure_baseline(self) -> SystemPrompt:
        """确保 baseline 策略存在（首次运行时自动创建）"""
        sp = load_system_prompt("baseline-v1")
        if not sp:
            sp = _default_baseline()
            save_system_prompt(sp)
        return sp

    def build(
        self,
        system_prompt: SystemPrompt,
        memory: Optional[AgentMemory] = None,
        matches: list = None,
        extra_context: Optional[str] = None,
        token_budget: int = None,
        max_matches: int = None,
        settled_orders: list = None,
        day_date: str = None,
        persona_text: str = "",
        **kwargs,
    ) -> dict:
        """
        组装完整 system prompt。

        Args:
            system_prompt: 策略配置
            memory:        agent 记忆
            matches:       比赛列表，每项可以是:
                           - str: lota_id（用 default_slugs）
                           - dict: {lota_id, factors?}
                           None = 无比赛数据
            settled_orders: 刚结算的订单列表，用于生成昨日回顾
            day_date:      当前分析日期，用于推算"昨日"

        Returns:
            {
                "system": str,
                "token_count": int,
                "budget": int,
                "match_count": int,
                "breakdown": {role, memory, settlement_review, matches, frameworks, output, extra},
                "per_match_tokens": [int, ...],
                "review_text": str,
                "settled_count": int,
            }
        """
        if token_budget is None:
            token_budget = self.TOKEN_BUDGET
        if max_matches is None:
            max_matches = self.MAX_MATCHES_DEFAULT

        breakdown = {}

        # ── 1. 角色 ──
        role_text = system_prompt.role

        # 注入用户偏好
        if persona_text:
            role_text = role_text + "\n\n" + persona_text

        breakdown["role"] = count_tokens(role_text)

        # ── 2. 记忆 ──
        memory_text = ""
        if memory:
            memory_text = memory.format_for_prompt(system_prompt.memory_config)
        breakdown["memory"] = count_tokens(memory_text)

        # ── 2.5 昨日结算回顾 ──
        review_text = ""
        if settled_orders and system_prompt.memory_config.get("include_settlement_review", True):
            review_text = self._format_settlement_review(settled_orders, day_date or "")
        breakdown["settlement_review"] = count_tokens(review_text)

        # ── 3. 框架 + 输出格式（固定开销）──
        framework_text = (system_prompt.framework or "") + "\n\n" + (
            system_prompt.bet_sizing or "").format(capital=int(kwargs.get("capital", 10000)))
        output_text = system_prompt.output_format or ""
        fixed_overhead = (
            breakdown["role"]
            + count_tokens(framework_text)
            + count_tokens(output_text)
            + breakdown.get("settlement_review", 0)
        )

        # ── 4. 比赛数据（支持多场）──
        match_blocks = []
        per_match_tokens = []
        match_count = 0

        # 标准化 matches
        match_tasks = self._normalize_matches(matches or [], system_prompt, max_matches)

        # 每场比赛的 token 预算
        remaining_for_data = token_budget - fixed_overhead - breakdown["memory"]
        per_match_budget = min(
            self.TOKENS_PER_MATCH,
            remaining_for_data // max(len(match_tasks), 1)
        )

        for task in match_tasks:
            lid = task["lota_id"]
            factors = task.get("factors", [])

            # 确定 slugs
            slugs = list(system_prompt.default_slugs)
            for f in factors:
                for s in (f.slugs if hasattr(f, 'slugs') else []):
                    if s not in slugs:
                        slugs.append(s)

            # 取数据
            ctx = _dm.get_match_context(lid)
            match_info = ctx.get("match", {})
            data_text = _dm.get_sections(lid, slugs) if slugs else ""

            # 赔率
            odds_text = self._format_odds(ctx.get("odds", {}))
            if odds_text:
                data_text = odds_text + "\n\n" + data_text

            # 截断
            if count_tokens(data_text) > per_match_budget:
                data_text = truncate_section(data_text, per_match_budget)

            tokens = count_tokens(data_text)
            per_match_tokens.append(tokens)

            # 比赛头部标注
            header = (
                f"## 比赛 {match_count + 1}: {match_info.get('home','?')} vs {match_info.get('away','?')}\n"
                f"  联赛: {match_info.get('league','?')} | 时间: {match_info.get('match_time','?')}\n"
                f"  lota_id: {lid}"
            )
            header += "\n\n" + data_text

            match_blocks.append(header)
            match_count += 1

        matches_text = "\n\n---\n\n".join(match_blocks) if match_blocks else "(无比赛数据)"
        breakdown["matches"] = count_tokens(matches_text)

        # ── 4.5 跨Agent因子注册表（仅 alpha 模式开启）──
        alpha_mode = kwargs.get("alpha_mode", False)
        cross_factor_text = ""
        if memory and alpha_mode:
            from .factor_registry import FactorRegistry
            exclude = set(kwargs.get("cross_factor_exclude", []) or [])
            fr = FactorRegistry(exclude_roles=exclude)
            # 自适应选择：最近 N 单 + 衰减加权 + 休眠过滤，避免固定时间窗口
            cross_factor_text = fr.format_for_prompt(current_date=day_date, adaptive=True)
        breakdown["cross_agent_factors"] = count_tokens(cross_factor_text)

        # ── 5. 拼接 ──
        blocks = [role_text]
        if memory_text:
            blocks.append(memory_text)
        if review_text:
            blocks.append(review_text)
        if cross_factor_text:
            blocks.append(cross_factor_text)
            # 桥接指令：alpha 用跨狗因子生成候选方向，原始数据做最终裁判
            blocks.append(
                "## 🔗 跨Agent因子匹配任务\n\n"
                "以上因子来自其他Agent的真实交易验证，作为**候选假设**——"
                "不是下单指令。\n\n"
                "对每场比赛：\n"
                "1. 从注册表中筛选与当前比赛数据相关的因子\n"
                "2. 逐个检查这些因子在当前数据中是否触发\n"
                "3. 触发后，按以下两步验证：\n"
                "   a. 判断因子类型：方向型（给出明确上下盘方向）还是预警型（提示风险）\n"
                "   b. 对照原始数据做三步验证：离散是否凝聚？赔率是否同向？资金是否同向？\n"
                "      - 方向型因子 + 三步中≥2同向 → 按因子方向下单\n"
                "      - 方向型因子 + 数据背离 → 放弃该因子\n"
                "      - 预警型因子触发 → 降低仓位/收紧条件，但不反向下注\n"
                "4. 没有因子触发或验证不通过 → 按你自己的数据分析决策\n\n"
                "⚠️ 跨狗因子是参考，原始数据是最终裁判。不盲从。\n\n"
                "输出时，每场比赛的 order 理由中必须注明："
                "\"跨狗检查: [因子名]-[触发/不触发]\""
            )
        if match_blocks:
            blocks.append(f"## 比赛数据（共 {match_count} 场）\n\n{matches_text}")
        blocks.append(framework_text)
        blocks.append(output_text)
        if extra_context:
            blocks.append(extra_context)
            breakdown["extra"] = count_tokens(extra_context)
        else:
            breakdown["extra"] = 0
        breakdown["frameworks"] = count_tokens(framework_text)
        breakdown["output"] = count_tokens(output_text)

        system = "\n\n".join(b for b in blocks if b)
        total = count_tokens(system)

        return {
            "system": system,
            "token_count": total,
            "budget": token_budget,
            "match_count": match_count,
            "breakdown": breakdown,
            "per_match_tokens": per_match_tokens,
            "review_text": review_text,
            "settled_count": len(settled_orders or []),
        }

    def _normalize_matches(
        self, matches: list, sp: SystemPrompt, max_matches: int
    ) -> list[dict]:
        """标准化比赛输入 → [{'lota_id': str, 'factors': []}]"""
        tasks = []
        for m in matches[:max_matches]:
            if isinstance(m, str):
                tasks.append({"lota_id": m, "factors": []})
            elif isinstance(m, dict):
                tasks.append({
                    "lota_id": m.get("lota_id", ""),
                    "factors": m.get("factors", []),
                })
        return tasks

    def _format_odds(self, odds: dict) -> str:
        """赔率 → 文本"""
        if not odds:
            return ""
        lines = ["📈 Pinnacle 终盘赔率:"]
        eu = odds.get("eu")
        if eu:
            lines.append(f"  欧赔: 主{eu['h']} / 平{eu['d']} / 客{eu['a']}")
        asian = odds.get("asian")
        if asian:
            # 数据层约定：受=负/让=正；主队视角(结算/展示)约定：正=主受/负=主让 → 取反对齐
            hc = -float(asian['handicap'])
            lines.append(
                f"  亚盘: 主{asian['h']} / 客{asian['a']}  |  "
                f"盘口值(主队): {hc:+.2f}  ← 下单时原样抄这个数字"
            )
        ou = odds.get("ou")
        if ou:
            lines.append(f"  大小球: 大{ou['over']} / {ou['threshold_text']}({ou['threshold']}) / 小{ou['under']}")
        return "\n".join(lines)

    def _format_settlement_review(
        self, settled_orders: list[dict], day_date: str
    ) -> str:
        """
        生成 '昨日结算回顾' 段落。

        Args:
            settled_orders: 刚结算完成的订单列表 (每个含 hit/profit/lota_id)
            day_date: 当前分析日期 "2026-06-12" — 推算前一日

        Returns:
            格式化后的 markdown 文本; 若无数据返回空字符串。
        """
        if not settled_orders:
            return ""

        from datetime import timedelta, date as _date
        prev_date = (
            _date.fromisoformat(day_date) - timedelta(days=1)
        ).isoformat() if day_date else "?"

        lines = [f"## 昨日结算回顾 ({prev_date})", ""]
        lines.append(
            "| # | 比赛 | 类型 | pick | 赔率 | 金额 | 结果 | 盈亏 |"
        )
        lines.append(
            "|---|------|------|------|------|------|------|------|"
        )

        hit = miss = push = 0
        total_pnl = 0.0

        for i, o in enumerate(settled_orders):
            lid = o.get("lota_id", "")
            ctx = _dm.get_match_context(lid)
            match_info = ctx.get("match", {})
            home = match_info.get("home", "?")[:10]
            away = match_info.get("away", "?")[:10]
            match_name = f"{home} vs {away}"

            h = o.get("hit")
            if h is True:
                result_icon = "✅ 命中"
                hit += 1
            elif h is False:
                result_icon = "❌ 未中"
                miss += 1
            else:
                result_icon = "➖ 走水"
                push += 1

            profit = o.get("profit", 0)
            total_pnl += profit

            lines.append(
                f"| {i+1} | {match_name[:22]}"
                f" | {o.get('bet_type','')} | {o.get('pick','')}"
                f" | {o.get('odds',0):.2f} | {o.get('bet_size',0):.0f}"
                f" | {result_icon} | {profit:+.0f} |"
            )

        total = len(settled_orders)
        denom = total - push
        hit_rate = f"{hit}/{denom}" if denom > 0 else f"{hit}/{total}"

        lines.append("")
        lines.append(
            f"昨日PnL: {total_pnl:+.0f} | 命中: {hit_rate}"
            f" | 未中: {miss}" + (f" | 走水: {push}" if push > 0 else "")
        )
        lines.append("")
        lines.append(
            "⚠️ 回顾以上结算结果，思考哪些判断正确、哪些需要调整，"
            "将经验应用到今天的下注决策中。"
        )

        return "\n".join(lines)

    # ═══════════════════════════════════════════
    # Freeze — 策略固化
    # ═══════════════════════════════════════════

    def freeze(
        self,
        name: str,
        base_name: str = "baseline-v1",
        role: str = None,
        memory_config: dict = None,
        default_slugs: list[str] = None,
        framework: str = None,
        bet_sizing: str = None,
        output_format: str = None,
        notes: str = "",
    ) -> SystemPrompt:
        """
        固化当前配置为新策略。

        基于 base_name 策略，覆盖指定字段，保存为新策略 name。
        """
        base = load_system_prompt(base_name)
        if not base:
            base = _default_baseline()

        version = 1
        existing = load_system_prompt(name)
        if existing:
            version = existing.version + 1

        sp = SystemPrompt(
            name=name,
            version=version,
            role=role or base.role,
            memory_config=memory_config or base.memory_config,
            default_slugs=default_slugs or base.default_slugs,
            framework=framework or base.framework,
            bet_sizing=bet_sizing or base.bet_sizing,
            output_format=output_format or base.output_format,
            notes=notes or f"frozen from {base_name}",
        )

        save_system_prompt(sp)
        return sp


# ═══════════════════════════════════════════════
# 输出解析
# ═══════════════════════════════════════════════

def parse_order(response_text: str) -> dict | None:
    """
    解析 LLM 响应中的 order 代码块。

    Returns:
        {bet_type, pick, odds, bet_size, reason}  或
        {skip: True, reason: str}                 或
        None（解析失败）
    """
    import re

    # 提取 order 块
    m = re.search(r'```order\n(.*?)```', response_text, re.DOTALL)
    if not m:
        # 尝试不严格的匹配
        m = re.search(r'(类型|bet_type)[：:]\s*(.+?)(?:\n|$)', response_text)
        if not m:
            return None
        block = response_text
    else:
        block = m.group(1)

    result = {}

    # lota_id
    id_m = re.search(r'lota_id[：:]\s*(\w+)', block)
    if id_m:
        result["lota_id"] = id_m.group(1)

    # 类型 (skip 表示不下注)
    type_m = re.search(r'类型[：:]\s*(胜平负|亚盘|大小球|skip)', block)
    if type_m:
        if type_m.group(1) == "skip":
            result["skip"] = True
            result["bet_type"] = ""
        else:
            result["bet_type"] = type_m.group(1)

    # pick
    pick_m = re.search(r'pick[：:]\s*(H|D|A|over|under)', block)
    if pick_m:
        result["pick"] = pick_m.group(1)

    # 赔率
    odds_m = re.search(r'赔率[：:]\s*(\d+(?:\.\d+)?)', block)
    if odds_m:
        try:
            result["odds"] = float(odds_m.group(1))
        except ValueError:
            pass  # LLM 输出异常值（如"..."），跳过赔率字段

    # 盘口 (亚盘/大小球)
    hc_m = re.search(r'盘口[：:]\s*([+-]?\d+(?:\.\d+)?)', block)
    if hc_m:
        try:
            result["handicap"] = float(hc_m.group(1))
        except ValueError:
            pass

    # 金额
    size_m = re.search(r'金额[：:]\s*(skip|[\d.]+)', block)
    if size_m:
        val = size_m.group(1)
        if val.lower() == "skip":
            result["skip"] = True
        else:
            try:
                bet_size = float(val)
            except ValueError:
                result["skip"] = True  # 金额无法解析→跳过
            if bet_size <= 0:
                result["skip"] = True  # 金额0=skip
            else:
                result["bet_size"] = bet_size
    elif not result.get("skip"):
        result["bet_size"] = 100

    # 理由
    reason_m = re.search(r'理由[：:]\s*(.+?)(?:\n|$)', block)
    if reason_m:
        result["reason"] = reason_m.group(1).strip()

    return result


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Prompt Builder")
    sub = p.add_subparsers(dest="cmd")

    # list
    sub.add_parser("list", help="列出所有策略")

    # show
    sp_show = sub.add_parser("show", help="显示策略详情")
    sp_show.add_argument("name", help="策略名")

    # create
    sp_create = sub.add_parser("create", help="创建新策略")
    sp_create.add_argument("name", help="策略名")
    sp_create.add_argument("--base", default="baseline-v1", help="基于哪个策略")

    # build
    sp_build = sub.add_parser("build", help="组装 prompt")
    sp_build.add_argument("lota_id", help="比赛 ID（逗号分隔多个）")
    sp_build.add_argument("--prompt", default="baseline-v1", help="策略名")
    sp_build.add_argument("--memory", action="store_true", help="注入记忆")
    sp_build.add_argument("--no-truncate", action="store_true", help="不截断")

    # freeze
    sp_freeze = sub.add_parser("freeze", help="固化策略")
    sp_freeze.add_argument("name", help="新策略名")
    sp_freeze.add_argument("--base", default="baseline-v1", help="基于哪个策略")
    sp_freeze.add_argument("--notes", default="", help="迭代说明")

    # parse
    sp_parse = sub.add_parser("parse", help="解析 order 输出")
    sp_parse.add_argument("text", help="LLM 响应文本")

    args = p.parse_args()

    builder = PromptBuilder()

    if args.cmd == "list":
        prompts = list_system_prompts()
        if not prompts:
            print("(无已保存策略，首次运行自动创建 baseline-v1)")
            builder.ensure_baseline()
            prompts = list_system_prompts()
        for pr in prompts:
            print(f"  {pr['name']:<20} v{pr['version']}  {pr['updated_at']}  {pr['notes']}")

    elif args.cmd == "show":
        sp = load_system_prompt(args.name)
        if not sp:
            print(f"策略 '{args.name}' 不存在")
            sys.exit(1)
        d = model_to_dict(sp)
        print(json.dumps(d, ensure_ascii=False, indent=2))

    elif args.cmd == "create":
        sp = builder.ensure_baseline()
        clone_system_prompt(args.base, args.name)
        print(f"已创建策略 '{args.name}' (基于 {args.base})")

    elif args.cmd == "build":
        sp = load_system_prompt(args.prompt)
        if not sp:
            print(f"策略 '{args.prompt}' 不存在，使用 baseline")
            sp = builder.ensure_baseline()

        mem = None
        if args.memory:
            mem = AgentMemory()
            mem.refresh()

        # lota_id 可以是逗号分隔的多个 ID
        lids = [x.strip() for x in args.lota_id.split(",") if x.strip()]
        matches = lids  # 直接传 lota_id 列表（或 dict 列表）

        result = builder.build(
            system_prompt=sp,
            memory=mem,
            matches=matches,
            token_budget=None if args.no_truncate else None,  # None = 用默认 800K
        )

        print(f"=== SYSTEM PROMPT ===")
        print(f"matches: {result['match_count']} | tokens: {result['token_count']} | budget: {result['budget']}")
        print(f"breakdown: {result['breakdown']}")
        print(f"per_match: {result.get('per_match_tokens', [])}")
        print()
        print(result["system"])

    elif args.cmd == "parse":
        parsed = parse_order(args.text)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))

    else:
        p.print_help()
