#!/usr/bin/env python3
"""
DSFootball — 赌狗故事会

根据 session 下注记录 + LLM 自动编排故事。
角色有预设命运，无论输赢最终都会走向宿命。

用法:
  python storyteller.py jy                    # jy 的故事（默认东莞仔）
  python storyteller.py jy --role 澳门仔       # 换个角色
  python storyteller.py jy --role 深圳仔 --save # 保存到文件
  python storyteller.py list                  # 列出所有角色
"""

import sys
import json
import os
import re
from pathlib import Path
from datetime import date, timedelta
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent / "src"))
from data_manager import DataManager

# ═══════════════════════════════════════════════
# 角色模板（人物设定 + 宿命）
# ═══════════════════════════════════════════════

CHARACTERS = {
    "东莞仔": {
        "name": "东莞仔",
        "alias": "东莞仔",
        "personality": "嚣张跋扈，赢了吹牛逼输了骂街，爱吹牛而且好色，每次输钱就装死赢钱就吹自己厉害",
        "speech_style": "粤语粗口风，口头禅：'出嚟行，预咗要还''叼你老母''今晚会所我请'",
        "background": "东莞长安镇出身，小学文凭，16岁出来混，现在靠赌球为生。在帝豪会有个相好的技师叫阿美。",
        "fate": "无论世界杯输赢，最终都会在帝豪会所停车场被三个壮汉堵住爆菊花。",
    },
    "澳门仔": {
        "name": "澳门仔",
        "alias": "阿澳",
        "personality": "低调阴湿，精算师转行，从不冲动，只信概率不信命",
        "speech_style": "中葡混搭，口头禅：'概率系唯一嘅真理''呢铺唔系赌，系套利''Poisson唔系鱼蛋，系泊松'",
        "background": "澳门大学统计系毕业，在葡京做了三年精算师。后来发现帮赌场算概率不如自己下场赌。自建泊松模型，坚信数学能战胜足球。",
        "fate": "泊松模型在第14天崩了。准确率42%，比随机还低8个百分点。他撕掉论文，站在葡京门口，终于明白了：不是他拟合了市场，是市场拟合了他。",
    },
    "深圳仔": {
        "name": "深圳仔",
        "alias": "阿深",
        "personality": "程序员转行赌狗，什么都想自动化，写了个AI帮他下注",
        "speech_style": "中英混杂极客风，口头禅：'这个edge我测过了p<0.05''Hard code hard life''ML说买大但我的心说买小……算了听模型的'",
        "background": "深圳科技园某大厂程序员，被裁后转型full-time degen。用PyTorch训练了一个下注模型，在2024年数据上准确率82%。他相信AI能降维打击博彩市场。",
        "fate": "2024年训练集准确率82%，2026年真实世界杯准确率42%。他悟了：不是数据不够，是足球本身就是混沌。把GPU卖了，换了一张去西藏的机票。",
    },
}

# ═══════════════════════════════════════════════
# Session / Order 数据提取
# ═══════════════════════════════════════════════

SESSIONS_DIR = Path(__file__).parent / "lota_data" / "sessions"
_dm = DataManager()


def _match_name(lota_id: str) -> str:
    ctx = _dm.get_match_context(lota_id)
    m = ctx.get("match", {})
    home = m.get("home", "?")
    away = m.get("away", "?")
    if home == "?" and away == "?":
        feat = _dm.get_cached_compact_fet(lota_id)
        if feat and isinstance(feat, dict):
            d = feat.get("data", {}) or {}
            home = d.get("home_name", "?") or "?"
            away = d.get("away_name", "?") or "?"
    return f"{home} vs {away}"


def _parse_analyze(content: str, day_date: str) -> dict:
    result = {
        "match_count": 0, "capital_before": 0, "capital_after": 0,
        "prompt_tokens": 0, "response_tokens": 0, "orders": [],
    }
    m = re.search(r'初始资金\s*\|\s*([\d.]+)', content)
    if m: result["capital_before"] = float(m.group(1))
    m = re.search(r'match_count.*?(\d+)', content)
    if m: result["match_count"] = int(m.group(1))
    m = re.search(r'Prompt tokens\s*\|\s*(\d+)', content)
    if m: result["prompt_tokens"] = int(m.group(1))
    m = re.search(r'Response tokens\s*\|\s*(\d+)', content)
    if m: result["response_tokens"] = int(m.group(1))
    m = re.search(r'资金变化\s*\|\s*[\d.]+\s*→\s*([\d.]+)\s*\(PnL\s*([+-][\d.]+)\)', content)
    if m:
        result["capital_after"] = float(m.group(1))

    # 订单表格
    in_orders = False
    for line in content.split("\n"):
        s = line.strip()
        if "### 📈 Orders" in s:
            in_orders = True
            continue
        if in_orders:
            if not s: continue
            if s.startswith("|") and "lota_id" not in s and "---" not in s:
                parts = [p.strip() for p in s.split("|")[1:-1]]
                if len(parts) >= 6:
                    is_skip = "skip" in parts[2].lower() or "⏭" in parts[2]
                    result["orders"].append({
                        "lota_id": parts[1],
                        "bet_type": parts[2] if not is_skip else "skip",
                        "pick": parts[3] if not is_skip else "",
                        "odds": float(parts[4]) if not is_skip and parts[4] not in ("-", "") else 0,
                        "bet_size": float(parts[5]) if not is_skip and parts[5] not in ("-", "") else 0,
                        "reason": parts[6][:100] if len(parts) > 6 else "",
                        "match_name": _match_name(parts[1]),
                        "skip": is_skip,
                    })
            elif not s.startswith("|"):
                in_orders = False
    return result


def _parse_settle(content: str, day_date: str) -> dict:
    result = {"settled": 0, "hit": 0, "miss": 0, "push": 0, "pnl": 0,
              "capital_before": 0, "capital_after": 0}
    for key, pattern in [
        ("settled", r'结算数\s*\|\s*(\d+)'), ("hit", r'命中\s*\|\s*(\d+)'),
        ("miss", r'未中\s*\|\s*(\d+)'), ("push", r'走水\s*\|\s*(\d+)'),
        ("pnl", r'PnL\s*\|\s*([+-][\d.]+)'),
    ]:
        m = re.search(pattern, content)
        if m: result[key] = float(m.group(1)) if key == "pnl" else int(m.group(1))
    m = re.search(r'初始资金\s*\|\s*([\d.]+)', content)
    if m: result["capital_before"] = float(m.group(1))
    m = re.search(r'资金变化\s*\|\s*[\d.]+\s*→\s*([\d.]+)', content)
    if m: result["capital_after"] = float(m.group(1))
    return result


def build_timeline(user: str) -> dict:
    """
    构建完整时间线数据。返回结构化 dict 直接喂给 LLM。
    """
    user_dir = SESSIONS_DIR / user
    if not user_dir.exists():
        return {}

    # 收集 sessions
    analyzes: dict[str, list[Path]] = {}
    settles: dict[str, list[Path]] = {}
    for fpath in sorted(user_dir.glob("*.md")):
        parts = fpath.stem.split("_")
        if len(parts) < 3: continue
        action, day_date = parts[1], parts[2]
        (analyzes if action == "analyze" else settles).setdefault(day_date, []).append(fpath)

    # 每日取最新 session
    days = {}
    for day_date, paths in analyzes.items():
        days.setdefault(day_date, {})["analyze"] = _parse_analyze(
            sorted(paths)[-1].read_text(encoding="utf-8"), day_date)
    for day_date, paths in settles.items():
        days.setdefault(day_date, {})["settle"] = _parse_settle(
            sorted(paths)[-1].read_text(encoding="utf-8"), day_date)

    # 获取角色的已结算订单
    try:
        from role import Role
        r = Role.load(user)
        all_orders = r.get_orders()
        settled_by_lid = {}
        for o in all_orders:
            if o.get("settled_at"):
                settled_by_lid[o.get("lota_id", "")] = {
                    "hit": o.get("hit"), "profit": o.get("profit", 0),
                    "match_name": _match_name(o.get("lota_id", "")),
                    "bet_type": o.get("bet_type", ""), "pick": o.get("pick", ""),
                    "odds": o.get("odds", 0), "bet_size": o.get("bet_size", 0),
                }
    except Exception:
        settled_by_lid = {}

    # 构建时间线：直接用 settle session 的资金变化，不再手动计算
    sorted_dates = sorted(days.keys())
    timeline = []

    for day_date in sorted_dates:
        day_data = days[day_date]
        analyze = day_data.get("analyze", {})
        settle = day_data.get("settle", {})

        # 下注订单
        bet_orders = [o for o in analyze.get("orders", []) if not o.get("skip")]

        # 资金：优先取 settle session 记录的值，fallback 到 analyze
        capital_before = settle.get("capital_before", 0) or analyze.get("capital_before", 0)
        capital_after = settle.get("capital_after", 0) or analyze.get("capital_after", 0)

        # 结算详情
        settled_today = []
        prev_lids = [o["lota_id"] for o in bet_orders]  # 当天分析下的单，第二天会结算
        for lid in prev_lids:
            if lid in settled_by_lid:
                settled_today.append(settled_by_lid[lid])

        day_entry = {
            "day_date": day_date,
            "match_count": analyze.get("match_count", 0),
            "capital": capital_before or 10000,
            "bets": bet_orders,
            "settled": settled_today if settled_today else [],
        }
        if settled_today:
            day_entry["settlement_pnl"] = sum(o.get("profit", 0) for o in settled_today)

        timeline.append(day_entry)

    # 取最后一天的 capital_after
    final_capital = timeline[-1]["capital"] if timeline else 10000

    return {
        "user": user,
        "total_days": len(timeline),
        "initial_capital": 10000,
        "final_capital": final_capital,
        "timeline": timeline,
    }


# ═══════════════════════════════════════════════
# LLM 故事生成
# ═══════════════════════════════════════════════

def generate_story(user: str, char_key: str = "东莞仔", api_key: str = None) -> str:
    char = CHARACTERS.get(char_key, CHARACTERS["东莞仔"])
    timeline = build_timeline(user)

    if not timeline.get("timeline"):
        return f"(用户 {user} 没有 session 记录)"

    # 构建写作 prompt
    prompt = f"""你是一个港式黑帮小说作家，风格类似《古惑仔》+《赌神》。根据以下真实博彩数据写一篇故事。

## 人物设定
- 名字：{char['name']}（{char['alias']}）
- 性格：{char['personality']}
- 说话风格：{char['speech_style']}
- 背景：{char['background']}

## 宿命（必须融入故事）
{char['fate']}

## 下注时间线
```json
{json.dumps(timeline, ensure_ascii=False, indent=2)}
```

## 写作要求
1. 标题："{char['name']}的世界杯"
2. 每天分两段：⏰清晨结算 + 🧠午后看盘
3. 用人物的口吻和性格写，对话用粤语/方言
4. 第2天起每隔2天插入一句伏笔，暗示宿命
5. 每单下注都要写出比赛队名、方向、赔率、金额
6. 赢了写人物的得意反应，输了写沮丧反应
7. 结局必须走向宿命，不管资金盈亏
8. 结尾写出完整资金曲线

直接输出markdown故事，不要任何前言或后记。"""

    # 调用 LLM
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return "❌ 请设置 DEEPSEEK_API_KEY 环境变量"

    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 65536,
            "temperature": 0.8,
        },
        timeout=180,
    )
    data = resp.json()
    if "choices" in data:
        msg = data["choices"][0]["message"]
        thinking = msg.get("reasoning_content", "")
        content = msg.get("content", "")
        thinking_block = f"[thinking]\n{thinking}\n[/thinking]\n\n" if thinking else ""
        return thinking_block + content
    return f"❌ LLM 错误: {data}"


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════

def list_characters():
    for key, c in CHARACTERS.items():
        print(f"  {key}: {c['name']} — {c['personality'][:50]}...")
        print(f"    宿命: {c['fate'][:60]}...")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "list":
        list_characters()
        sys.exit(0)

    user = cmd
    char_key = "东莞仔"
    do_save = False
    rest = sys.argv[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--role" and i + 1 < len(rest):
            char_key = rest[i + 1]; i += 2
        elif rest[i] == "--save":
            do_save = True; i += 1
        else:
            i += 1

    if char_key not in CHARACTERS:
        print(f"未知角色 '{char_key}'，可用: {', '.join(CHARACTERS.keys())}")
        sys.exit(1)

    print(f"📝 正在为 {user} 生成 {char_key} 的故事...")
    story = generate_story(user, char_key)

    if do_save:
        out_path = Path(f"{user}_{char_key}_story.md")
        out_path.write_text(story, encoding="utf-8")
        print(f"✅ 已保存到 {out_path}")
    else:
        print(story)
