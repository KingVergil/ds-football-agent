#!/bin/bash
# ═══════════════════════════════════════════════
# 批量操作 agent — settle / analyze / dashboard
# ═══════════════════════════════════════════════
#
# 用法:
#   ./batch_agents.sh settle 2026-07-09             # 全部结算（指定日期）
#   ./batch_agents.sh settle live                   # 全部结算（足球当日，自动推算）
#   ./batch_agents.sh analyze 2026-07-09             # 全部分析下单（默认 live 模式）
#   ./batch_agents.sh analyze live                   # 全部分析下单（足球当日）
#   ./batch_agents.sh status                        # 全部状态
#   ./batch_agents.sh pending                       # 全部待结算
#   ./batch_agents.sh dashboard                     # 刷新数据 → 打开 UI
#   ./batch_agents.sh email-orders                 # 发送各 agent 未结算订单邮件
#   ./batch_agents.sh email-orders live            # 发送当前足球日未结算订单邮件

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

AGENTS=(
    "alpha2狗"
    "alpha狗"
    "梭哈2狗"
    "梭哈3狗"
    "平局狗"
    "跟风狗"
    "均注狗"
)

cmd="${1:-}"
[ -z "$cmd" ] && echo "用法: ./batch_agents.sh <settle|analyze|status|pending|dashboard|email-orders> [args...]" && exit 1
shift || true

# ── 足球时间"当日"：12:01 ～ 次日 12:00 ──
# analyze 用窗口起始日，settle 用窗口结束日（差 1 天）
# 当前足球日标签 = 窗口起始日：
#   当前 < 12:00  → 昨天 12:01 ~ 今天 12:00，标签=昨天
#   当前 ≥ 12:00  → 今天 12:01 ~ 明天 12:00，标签=今天
_football_label() {
    if [ "$(date +%H%M)" -lt 1200 ]; then
        date -v-1d +%Y-%m-%d   # 窗口起始日 = 昨天
    else
        date +%Y-%m-%d          # 窗口起始日 = 今天
    fi
}
# 处理参数: "live" → 足球日日期
args=()
for a in "$@"; do
    if [ "$a" = "live" ]; then
        label="$(_football_label)"
        case "$cmd" in
            settle|factor-review)
                # settle 传窗口结束日 = 起始日 + 1
                args+=("$(date -j -v+1d -f %Y-%m-%d "$label" +%Y-%m-%d)")
                ;;
            *)
                # analyze / status / pending 传窗口起始日
                args+=("$label")
                ;;
        esac
    else
        args+=("$a")
    fi
done

    # email-orders 无参数 → 默认足球日窗口
    if [ "$cmd" = "email-orders" ] && [ "${#args[@]}" -eq 0 ]; then
        args+=("$(_football_label)")
    fi

case "$cmd" in
    dashboard)
        echo "🔄 拉取数据..."
        python "$SCRIPT_DIR/dsfootball_cli.py" dashboard
        echo "✅ 数据已刷新 → 打开 UI"
        open "$SCRIPT_DIR/lota_data/dashboard.html"
        ;;
    settle|analyze|status|pending|factor-review)
        for agent in "${AGENTS[@]}"; do
            echo ""
            echo "▸ $agent — $cmd ${args[*]:-}"
            echo ""
            python "$SCRIPT_DIR/dsfootball_cli.py" agent "$agent" "$cmd" ${args[@]+"${args[@]}"} "--jingcai"
        done
        ;;
    email-orders)
        EMAIL_AGENTS=("均注狗")
        for agent in "${EMAIL_AGENTS[@]}"; do
            echo ""
            echo "📧 $agent — $cmd ${args[*]:-}"
            echo ""
            python "$SCRIPT_DIR/dsfootball_cli.py" agent "$agent" "$cmd" ${args[@]+"${args[@]}"}
        done
        ;;
    *)
        echo "未知: $cmd (settle|analyze|status|pending|dashboard|email-orders)"
        exit 1
        ;;
esac
