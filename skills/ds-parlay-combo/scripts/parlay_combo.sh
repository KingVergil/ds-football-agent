#!/bin/bash
# 串关狗连招：预取 → 串关狗分析(3串1) → dashboard → 发送串关狗邮件
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$PROJECT_DIR"

day="${1:-}"
if [ -z "$day" ]; then
    if [ "$(date +%H%M)" -lt 1200 ]; then
        day="$(date -v-1d +%Y-%m-%d)"
    else
        day="$(date +%Y-%m-%d)"
    fi
fi

echo "🧠 串关狗连招: $day"
echo "1/4 预取比赛数据..."
python dsfootball_cli.py prefetch "$day" --jingcai

echo "2/4 串关狗分析（3串1 专注模式）..."
python -m src.chuan_guan_dog analyze "$day" --tickets 3串1

echo "3/4 刷新 dashboard..."
./batch_agents.sh dashboard

echo "4/4 发送串关狗邮件..."
./batch_agents.sh email-orders "$day" 串关狗

echo "✅ 串关狗连招完成"
