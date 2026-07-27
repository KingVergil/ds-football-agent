"""
DSFootball Python CLI — Session Markdown Logger

每次 agent 运行生成一个 markdown 日志文件，记录工具调用、token 统计、LLM 响应。
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from .data_manager import DataManager

SESSIONS_DIR = Path(__file__).parent.parent / "lota_data" / "sessions"


class SessionLogger:
    """
    会话日志器 — 生成 markdown 格式的运行记录。

    用法:
      log = SessionLogger(user='jy')
      log.start('analyze', '2026-06-11')
      log.tool_call('fetch_matches', {'date': '2026-06-11'}, '9 matches')
      log.llm_call(system_prompt, response, tokens_in=10800, tokens_out=1200)
      log.orders([{...}, ...])
      log.finish(role)
      # → sessions/jy/2026-06-30T120000_analyze.md
    """

    def __init__(self, user: str = "default"):
        self.user = user
        self._dir = SESSIONS_DIR / user
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lines: list[str] = []
        self._tool_count = 0
        self._total_tokens_in = 0
        self._total_tokens_out = 0

    def start(self, action: str, day_date: str = "", capital: float = 0):
        """开始新会话"""
        ts = datetime.now().strftime("%Y-%m-%dT%H%M%S")
        self._ts = ts
        self._action = action
        self._day = day_date
        self._capital_before = capital
        self._path = self._dir / f"{ts}_{action}_{day_date}.md"

        self._lines = []
        self._tool_count = 0
        self._total_tokens_in = 0
        self._total_tokens_out = 0

        self._w(f"# Session: {action} — {day_date}")
        self._w(f"")
        self._w(f"| 项目 | 值 |")
        self._w(f"|------|-----|")
        self._w(f"| 用户 | `{self.user}` |")
        self._w(f"| 时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
        self._w(f"| 操作 | `{action}` |")
        self._w(f"| 日期 | {day_date} |")
        self._w(f"| 初始资金 | {capital:.0f} |")
        self._w(f"")

    def tool_call(self, name: str, params: dict, result_summary: str):
        """记录工具调用"""
        self._tool_count += 1
        self._w(f"---")
        self._w(f"### 🔧 Tool #{self._tool_count}: `{name}`")
        self._w(f"")
        self._w(f"```json")
        self._w(json.dumps(params, ensure_ascii=False, indent=2))
        self._w(f"```")
        self._w(f"")
        self._w(f"**结果**: {result_summary}")
        self._w(f"")

    def llm_call(self, system_prompt: str, response: str,
                 tokens_in: int, tokens_out: int = 0,
                 model: str = "deepseek-v4-pro",
                 token_breakdown: dict = None):
        """记录 LLM 调用"""
        self._total_tokens_in += tokens_in
        self._total_tokens_out += tokens_out

        self._w(f"---")
        self._w(f"### 🤖 LLM Call")
        self._w(f"")
        self._w(f"| 指标 | 值 |")
        self._w(f"|------|-----|")
        self._w(f"| Model | `{model}` |")
        self._w(f"| Prompt tokens | {tokens_in} |")
        if token_breakdown:
            for label, key in [("  ├─ sys", "sys"), ("  ├─ mem", "mem"),
                               ("  ├─ tools", "tools"), ("  ├─ data", "data"),
                               ("  └─ user", "user")]:
                if key in token_breakdown:
                    self._w(f"| {label} | {token_breakdown[key]} |")
        self._w(f"| Response tokens | {tokens_out} |")
        self._w(f"| Total tokens | {tokens_in + tokens_out} |")
        self._w(f"")

        # 截断 system prompt
        max_prompt = 3000
        sp_display = system_prompt if len(system_prompt) <= max_prompt else system_prompt[:max_prompt] + "\n\n...(truncated)"

        self._w(f"<details><summary>System Prompt ({tokens_in} tokens)</summary>")
        self._w(f"")
        self._w(f"```")
        self._w(sp_display)
        self._w(f"```")
        self._w(f"")
        self._w(f"</details>")
        self._w(f"")

        self._w(f"<details><summary>LLM Response ({len(response)} chars)</summary>")
        self._w(f"")
        self._w(response)
        self._w(f"")
        self._w(f"</details>")
        self._w(f"")

    def reflect_call(self, reflect_prompt: str, response: str, params: dict):
        """记录反思 LLM 调用（完整 prompt + response）"""
        self._tool_count += 1
        self._w(f"---")
        self._w(f"### 🧠 Tool #{self._tool_count}: `reflect`")
        self._w(f"")
        self._w(f"```json")
        self._w(json.dumps(params, ensure_ascii=False, indent=2))
        self._w(f"```")
        self._w(f"")

        self._w(f"<details><summary>Reflect Prompt ({len(reflect_prompt)} chars)</summary>")
        self._w(f"")
        self._w(f"```")
        self._w(reflect_prompt[:8000])
        if len(reflect_prompt) > 8000:
            self._w(f"\n...(truncated, {len(reflect_prompt)} chars total)")
        self._w(f"```")
        self._w(f"")
        self._w(f"</details>")
        self._w(f"")

        self._w(f"<details><summary>Reflect Response ({len(response)} chars)</summary>")
        self._w(f"")
        self._w(response)
        self._w(f"")
        self._w(f"</details>")
        self._w(f"")

    def orders(self, orders: list[dict]):
        """记录下单"""
        self._w(f"---")
        self._w(f"### 📈 Orders ({len(orders)} 单)")
        self._w(f"")

        if not orders:
            self._w(f"(无订单)")
            self._w(f"")
            return

        self._w(f"| # | lota_id | 类型 | pick | 赔率 | 金额 | 理由 |")
        self._w(f"|---|---------|------|------|------|------|------|")
        for i, o in enumerate(orders):
            if o.get("skip"):
                self._w(f"| {i+1} | {o.get('lota_id','?')} | ⏭ skip | - | - | - | {o.get('reason','')[:50]} |")
            else:
                self._w(f"| {i+1} | {o.get('lota_id','?')} | {o.get('bet_type','')} | {o.get('pick','')} | "
                        f"{o.get('odds',0):.2f} | {o.get('bet_size',0):.0f} | {o.get('reason','')[:50]} |")
        self._w(f"")

    def settlement(self, result: dict):
        """记录结算"""
        self._w(f"---")
        self._w(f"### 📊 Settlement")
        self._w(f"")
        self._w(f"| 指标 | 值 |")
        self._w(f"|------|-----|")
        self._w(f"| 结算数 | {result.get('settled',0)} |")
        self._w(f"| 命中 | {result.get('hit',0)} |")
        self._w(f"| 未中 | {result.get('miss',0)} |")
        self._w(f"| 走水 | {result.get('push',0)} |")
        self._w(f"| PnL | {result.get('pnl',0):+.0f} |")
        self._w(f"")

    def finish(self, capital_after: float, stats: dict = None):
        """结束会话，写入文件"""
        pnl = capital_after - self._capital_before

        self._w(f"---")
        self._w(f"### 📋 Summary")
        self._w(f"")
        self._w(f"| 指标 | 值 |")
        self._w(f"|------|-----|")
        self._w(f"| Tool calls | {self._tool_count} |")
        self._w(f"| Total tokens in | {self._total_tokens_in} |")
        self._w(f"| Total tokens out | {self._total_tokens_out} |")
        self._w(f"| 资金变化 | {self._capital_before:.0f} → {capital_after:.0f} (PnL {pnl:+.0f}) |")

        if stats:
            self._w(f"| 总订单 | {stats.get('total_orders',0)} |")
            self._w(f"| 已结算 | {stats.get('settled',0)} |")
            if stats.get('total_bet', 0) > 0:
                self._w(f"| ROI | {stats.get('roi',0):+.1f}% |")

        self._w(f"")

        content = "\n".join(self._lines)
        self._path.write_text(content, encoding="utf-8")
        return str(self._path)

    def _w(self, line: str):
        self._lines.append(line)

    @classmethod
    def list_sessions(cls, user: str = None) -> list[dict]:
        """列出会话文件"""
        result = []
        base = SESSIONS_DIR
        pattern = f"{user}/**/*.md" if user else "**/*.md"
        for fpath in sorted(base.glob(pattern), reverse=True):
            result.append({
                "user": fpath.parent.name,
                "file": fpath.name,
                "size": fpath.stat().st_size,
            })
        return result
