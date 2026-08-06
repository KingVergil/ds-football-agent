---
name: ds-parlay-combo
description: 串关狗（3串1 竞彩串关）独立连招：预取数据 → 串关狗分析下单 → 刷新 dashboard → 发送串关狗邮件。因为串关狗受竞彩销售截止时间限制、需要比其他 7 狗更早启动，当用户说"跑串关狗连招 / 串关启动 / 串关+看板+邮件 / 单独跑串关狗"或要求提前为串关狗执行 分析+dashboard+发邮件 一套流程时使用。
---

# 串关狗连招

独立于 7 狗的串关狗日常流程。竞彩截止时间早，串关狗需提前单独跑完整套：**预取 → 分析(3串1) → dashboard → 发邮件**。

## 一键执行

```bash
bash skills/ds-parlay-combo/scripts/parlay_combo.sh [YYYY-MM-DD]   # 不传日期 = live 语义
```

脚本依次执行：prefetch → `python -m src.chuan_guan_dog analyze <day> --tickets 3串1` → `./batch_agents.sh dashboard` → `./batch_agents.sh email-orders <day> 串关狗`。

## 分步执行（调试时）

1. **预取**：`python dsfootball_cli.py prefetch <day> --jingcai`（串关狗需要 jc_hhad 让球盘 + features 数据段）
2. **串关狗分析**：`python -m src.chuan_guan_dog analyze <day> --tickets 3串1`
   - 正式角色 `串关狗`，人设已锁定"只玩 3串1，没有条件就跳过"
   - `--tickets 3串1` 硬锁：prompt 同步收窄为恰好 3 场
3. **刷新看板**：`./batch_agents.sh dashboard`（打开 UI）
4. **发送串关狗邮件**：`./batch_agents.sh email-orders <day> 串关狗`

## 关键规则

- **日期口径**：analyze/email 用足球日窗口起始日；12:00 前 = 昨天（live 语义由脚本处理）
- **独立于 7 狗**：串关狗有自己的 CLI（`python -m src.chuan_guan_dog`）和角色/资金，不进 batch 的 7 狗列表；7 狗流程里 analyze/settle 会带串关狗，但"提前连招"场景单独跑本技能
- **不用 alpha 模式**：串关狗当前不读跨狗因子注册表（人设 + 竞彩赔率 + 数据段选腿）
- **邮件收件人**：`email-orders` 的 agent 参数必须传 `串关狗`，否则默认发梭哈2狗/均注狗
- **结算**：`python -m src.chuan_guan_dog settle <day>` 或 `./batch_agents.sh settle <day>`（已内置串关狗 + 因子归纳）
- 结果落盘：会话 `lota_data/sessions/串关狗/`、订单/资金 `lota_data/roles/串关狗/`
