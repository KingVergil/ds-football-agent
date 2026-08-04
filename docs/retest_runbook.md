# 全狗隔离 Retest 操作手册

## 目的

对比**分层注入 vs 当前代码**在相同起始状态、相同窗口（07-11 之后）下的决策质量。
对照组 = 当前代码（main+aux 注入、无去重）。

| 版本 | 环境 | 说明 |
|---|---|---|
| **当前代码（对照组）** | `/private/tmp/ds_retest`（去掉 dedup 钩子） | 08-02 修改后，main+aux |
| **分层注入+去重（实验组）** | `/private/tmp/vt_tier_base` | 负例护栏+正例预算+观察摘要 |

> 窗口起点说明：起始状态只有 **07-12 备份**（含 07-11 结果），干净回放起点是 **07-13**。
> 要"07-11 之后"就从 07-13 开始（07-11/12 已在起始状态里）；若必须从 07-11 起，
> 需要先从备份剔除 07-11/12 的订单与资金增量（重建 07-10 状态），不推荐。

## 标准步骤（每版本每狗）

```bash
# 1. 从 07-12 备份重置起始状态（7 狗）
for a in alpha2狗 alpha狗 均注狗 平局狗 梭哈2狗 梭哈3狗 跟风狗; do
  rm -rf <ENV>/lota_data/roles/$a
  cp -R "/Users/cjy/Desktop/code/ds_agents/lota_data/roles.backup.20260712_010652/$a" <ENV>/lota_data/roles/
done

# 2. 逐狗回放（07-15~07-25）
cd <ENV> && DEEPSEEK_API_KEY="$(sed -n 's/.*experimental_bearer_token = "\(.*\)"/\1/p' /Users/cjy/.codex/config.toml)" \
  /Users/cjy/miniconda3/bin/python dsfootball_cli.py agent <狗名> runall 2026-07-15 2026-07-25 --live --jingcai

# 3. 并行全狗：7 个后台作业 + wait（注意：每个作业自带 cd，别用子shell后台）
```

## 对比指标

```bash
python3 scripts/replay_compare.py   # 07-15~07-25 三路 ROI 对比
```

窗口内订单按 `lota_id → match_time` 归窗；看 ROI（注额加权会失真，梭哈2狗注额爆炸）。

## 关键已知项

- **单次回放噪声 ±13~22pp**：结论只看多次均值/方向，单次不可靠。至少 3 次/版本。
- `parse_orders` 有 `bet_size` UnboundLocalError bug（当前代码），遇到即崩——需先修（临时初始化 `bet_size=0`）。
- 梭哈2狗注额会爆炸（当前/分层注入下最大 8 万），对比时以 ROI 为准。
- 历史实际 ≠ 回放：回放系统性低于历史（缺多波分析/人工干预），只能做"回放 vs 回放"。

## 已有数据（截止 2026-08-03）

- 梭哈2狗 ×3：旧 -5.2% / 当前 -12.9% / 全量注入 -1.1%
- alpha2狗 分层注入 ×3：+21.2% / +25.2% / -14.8%（均值 +10.5%）
