#!/usr/bin/env python3
"""
对其他狗的因子库执行全时间范围的结构性退役审查。
每个狗只看自己的因子，互不干扰。

用法:
  python review_all_factors.py              # 审查所有非alpha狗
  python review_all_factors.py 梭哈2狗       # 只审查某个狗
"""
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent))

from src.agent import Agent, _rt
from src.providers.deepseek import DeepSeekProvider


def review_one(role_name: str, end_date: str = None, start_date: str = None):
    """对单个角色执行因子审查"""
    agent = Agent(role_name)
    agent.set_provider(DeepSeekProvider())

    if end_date is None:
        end_date = date.today().isoformat()
    if start_date is None:
        start_date = ""   # 空 = 自动 7 天

    print(f"\n{'='*60}")
    print(f"  {role_name} — 因子审查: {start_date or '(近7天)'} ~ {end_date}")
    print(f"{'='*60}")

    r = agent.factor_review(end_date=end_date, start_date=start_date)
    print(f"  结果: {r}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        targets = [sys.argv[1]]
    else:
        targets = ["梭哈2狗", "梭哈3狗", "跟风狗", "平局狗","均注狗","alpha狗","alpha2狗"]

    today = date.today().isoformat()

    for name in targets:
        try:
            review_one(name, end_date=today, start_date="")
        except Exception as e:
            print(f"  ❌ {name} 审查失败: {e}")

    print(f"\n✅ 全部完成")
    