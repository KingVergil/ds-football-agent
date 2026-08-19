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
#   ./batch_agents.sh factor-induction              # 因子归纳（alpha 跨狗 1 次 + 非 alpha 各自）
#   ./batch_agents.sh email-orders                 # 发送所有默认 agent 未结算订单邮件
#   ./batch_agents.sh email-orders live            # 发送当前足球日（默认 agent）
#   ./batch_agents.sh email-orders live 均注狗      # 只发均注狗
#   ./batch_agents.sh email-orders 2026-07-29 均注狗 梭哈2狗  # 指定日期 + 指定 agent

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 全量默认狗列表：7 只生产狗 + 注册表 enabled=true 的狗（观察狗默认不进，显式指定不受限）
AGENTS=($(cd "$SCRIPT_DIR" && python -m src.role_registry live))

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
# email-orders 模式下，第一个非 live 参数为日期，后续参数为 agent 名
args=()
was_live=0
for a in "$@"; do
    if [ "$a" = "live" ]; then
        was_live=1
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

# ── 并发跑多个 agent（各自输出到独立日志，结束后按顺序回放）──
# 用法: _run_agents_parallel [额外参数...]（自动追加 --jingcai）
_run_agents_parallel() {
    local parallel="${PARALLEL:-7}"
    local tmpdir pids=() agent fail=0
    tmpdir="$(mktemp -d)"
    for agent in "${AGENTS[@]}"; do
        (
            echo ""
            echo "▸ $agent — $cmd ${args[*]:-}"
            echo ""
            python "$SCRIPT_DIR/dsfootball_cli.py" agent "$agent" "$cmd" ${args[@]+"${args[@]}"} "--jingcai" "$@"
        ) > "$tmpdir/$agent.log" 2>&1 &
        pids+=("$!")
        while [ "${#pids[@]}" -ge "$parallel" ]; do
            wait "${pids[0]}" || true
            pids=("${pids[@]:1}")
        done
    done
    for pid in "${pids[@]}"; do
        wait "$pid" || fail=1
    done
    for agent in "${AGENTS[@]}"; do
        cat "$tmpdir/$agent.log"
    done
    rm -rf "$tmpdir"
    return "$fail"
}

# ── 自动刷新 dashboard（数据变化后调用，免手动 dashboard）──
# 用法: _refresh_dashboard [open|noopen]
_refresh_dashboard() {
    local open_mode="${1:-open}"
    echo ""
    echo "📊 自动刷新 dashboard..."
    python "$SCRIPT_DIR/dsfootball_cli.py" dashboard 2>&1 | tail -1
    if [ "$open_mode" = "open" ]; then
        open "$SCRIPT_DIR/data/dashboard.html"
    fi
}

case "$cmd" in
    dashboard)
        echo "🔄 拉取数据..."
        if [[ "${args[*]:-}" == *"--watch"* ]]; then
            # watch 模式：CLI 负责首刷 + 定时刷新 + 打开浏览器，前台阻塞
            python "$SCRIPT_DIR/dsfootball_cli.py" dashboard ${args[@]+"${args[@]}"}
        else
            python "$SCRIPT_DIR/dsfootball_cli.py" dashboard
            echo "✅ 数据已刷新 → 打开 UI"
            open "$SCRIPT_DIR/data/dashboard.html"
        fi
        ;;
    analyze)
        echo "🔄 预取比赛数据 (compact-fet + tags)..."
        python "$SCRIPT_DIR/dsfootball_cli.py" prefetch ${args[0]:-} --jingcai
        echo "✅ 预取完成 → 并发分析 7 狗 (PARALLEL=${PARALLEL:-7})"
        _run_agents_parallel --prefetched
        echo ""
        echo "▸ 串关2狗 — analyze ${args[0]:-}（3串1 专注模式，用自己的因子）"
        live_flag=""
        [ "$was_live" = "1" ] && live_flag="--live"
        (cd "$SCRIPT_DIR" && python -m src.chuan_guan_dog analyze ${args[0]:-} --tickets 3串1 --user 串关2狗 $live_flag) 2>&1 | tail -4
        _refresh_dashboard open
        ;;
    status|pending)
        _run_agents_parallel
        echo ""
        echo "▸ 串关2狗 — $cmd"
        (cd "$SCRIPT_DIR" && python -m src.chuan_guan_dog "$cmd" --user 串关2狗) 2>&1 | tail -3
        ;;
    settle|factor-review)
        for agent in "${AGENTS[@]}"; do
            echo ""
            echo "▸ $agent — $cmd ${args[*]:-}"
            echo ""
            python "$SCRIPT_DIR/dsfootball_cli.py" agent "$agent" "$cmd" ${args[@]+"${args[@]}"} "--jingcai"
        done
        if [ "$cmd" = "settle" ]; then
            echo ""
            echo "▸ 串关2狗 — settle ${args[*]:-}（3串1 独立角色，用自己的因子）"
            (cd "$SCRIPT_DIR" && python -m src.chuan_guan_dog settle ${args[0]:-} --user 串关2狗) 2>&1 | tail -3
            echo ""
            echo "🧠 因子归纳（结算后自动：alpha 跨狗 1 次 + 非 alpha 各自，--limit 30）..."
            python "$SCRIPT_DIR/dsfootball_cli.py" factor-induction --limit 30
        fi
        _refresh_dashboard noopen
        ;;
    factor-induction)
        echo "🧠 因子归纳（alpha 跨狗 1 次 + 非 alpha 各自）..."
        python "$SCRIPT_DIR/dsfootball_cli.py" factor-induction ${args[@]+"${args[@]}"}
        ;;
    email-orders)
        # 默认发送列表（命令行未指定 agent 时使用）
        DEFAULT_EMAIL_AGENTS=("梭哈2狗" "跟风狗")
        date_arg="${args[0]:-}"
        # args[1:] 如果有值 → 当作 agent 列表；否则用默认
        if [ "${#args[@]}" -gt 1 ]; then
            EMAIL_AGENTS=("${args[@]:1}")
        else
            EMAIL_AGENTS=("${DEFAULT_EMAIL_AGENTS[@]}")
        fi
        for agent in "${EMAIL_AGENTS[@]}"; do
            echo ""
            echo "📧 $agent — $cmd $date_arg"
            echo ""
            python "$SCRIPT_DIR/dsfootball_cli.py" agent "$agent" "$cmd" "$date_arg"
        done
        ;;
    *)
        echo "未知: $cmd (settle|analyze|status|pending|dashboard|email-orders)"
        exit 1
        ;;
esac
