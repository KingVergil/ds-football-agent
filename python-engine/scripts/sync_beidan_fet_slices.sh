#!/usr/bin/env bash
#
# 同步 bc狗 北单 fet_txt 时间切片到本地（回测取数用）。
#
# 线上 fet_txt 是 deepseek_lota 的数据核，本脚本只读线上、只写本地：
#   1) 本地从 python-engine/data/matches/*.json 收集范围内的北单场次 lid
#   2) 把 lid 清单传线上 /tmp，线上按 3 个阶段目录 tar（在 /tmp，不碰线上服务）
#   3) 拉回本地并解包到 <fet_txt 根>/{pass_6_hours,pass_12_hours,pass_1_day}
#   4) 重建切片索引 .bc_backtest_index.json（src/backtest_fet.py 读它判定范围）
#
# 用法:
#   scripts/sync_beidan_fet_slices.sh [起始足球日] [结束足球日]
#   REMOTE=my-server REMOTE_FET_ROOT=/path/to/fet_txt \
#       scripts/sync_beidan_fet_slices.sh 2026-07-01 2026-09-08
#
# 默认远端: lota（~/.ssh/config）; 本地根: DS_FET_TXT_ROOT 或同级 deepseek_lota/data/runtime/fet_txt
set -euo pipefail

REMOTE="${REMOTE:-my-server}"
REMOTE_FET_ROOT="${REMOTE_FET_ROOT:-/path/to/fet_txt}"
START="${1:-2026-07-01}"
END="${2:-2026-09-08}"
STAGES="pass_6_hours pass_12_hours pass_1_day"

ENGINE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_ROOT="${DS_FET_TXT_ROOT:-$ENGINE_ROOT/../../deepseek_lota/data/runtime/fet_txt}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "▶ 范围 $START ~ $END | 远端 $REMOTE:$REMOTE_FET_ROOT | 本地 $LOCAL_ROOT"

echo "① 收集北单 lid（本地 matches 缓存）"
( cd "$ENGINE_ROOT" && python3 -m src.backtest_fet lids --start "$START" --end "$END" ) \
  > "$TMP/lids.txt" 2> "$TMP/lids.log"
sort -u "$TMP/lids.txt" -o "$TMP/lids.txt"
cat "$TMP/lids.log"
echo "  待同步 $(wc -l < "$TMP/lids.txt") 场"

echo "② 上传清单 + 线上打包（只读线上，产物在 /tmp）"
scp -q "$TMP/lids.txt" "$REMOTE:/tmp/bc_fet_lids.txt"
ssh "$REMOTE" "cd '$REMOTE_FET_ROOT' && : > /tmp/bc_fet_list.txt && \
  for s in $STAGES; do while read -r l; do [ -f \"\$s/\$l.txt\" ] && echo \"\$s/\$l.txt\" >> /tmp/bc_fet_list.txt; done < /tmp/bc_fet_lids.txt; done; \
  echo \"  线上命中 \$(wc -l < /tmp/bc_fet_list.txt) 个切片\"; \
  for s in $STAGES; do printf '  %s: ' \"\$s\"; grep -c \"^\$s/\" /tmp/bc_fet_list.txt || true; done"
ssh "$REMOTE" "cd '$REMOTE_FET_ROOT' && tar -czf - -T /tmp/bc_fet_list.txt" > "$TMP/slices.tar.gz"

echo "③ 解包到本地"
mkdir -p "$LOCAL_ROOT"
tar -xzf "$TMP/slices.tar.gz" -C "$LOCAL_ROOT"
for s in $STAGES; do printf '  %s: %s 个文件\n' "$s" "$(ls "$LOCAL_ROOT/$s" 2>/dev/null | wc -l | tr -d ' ')"; done

echo "④ 重建切片索引"
( cd "$ENGINE_ROOT" && python3 -m src.backtest_fet index --root "$LOCAL_ROOT" --start "$START" --end "$END" )

echo "✅ 完成。回测（沙箱回放）会自动启用切片源；可用下面命令抽查某日各波取数档位："
echo "   python3 -m src.backtest_fet check --root '$LOCAL_ROOT' --day 2026-08-22"
