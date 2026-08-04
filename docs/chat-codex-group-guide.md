# Chat-Codex 飞书群聊接入与权限说明

## 已完成配置

1. 飞书群聊接收开关已打开（Chat-Codex 0.1.5 默认关闭，已本地启用）。
2. 群消息会带上 `名字(open_id)` 前缀进入 Codex 上下文，身份不可伪造。
3. 项目根目录 `AGENTS.md` 已写入权限规则：
   - 只有所有者可以触发分析 / 结算 / 因子退役 / 发邮件等写操作；
   - 其他群成员只能查询（状态、看板、待结算、赛程等）。
4. 新增飞书卡片渲染：`render_feishu_orders_card.py` 把订单邮件同款内容渲染成飞书卡片；
   Chat-Codex 飞书 adapter 支持 `BRIDGE_SEND_FEISHU_CARD: <路径>` 协议行，直接发送 interactive 卡片。

## 把机器人拉进群聊

1. 在 `ds_agents` 目录启动 Chat-Codex：

   ```bash
   cd /Users/cjy/Desktop/code/ds_agents
   chat-codex
   ```

2. 在已配对的飞书私聊里给机器人发送 `/group on`，确认群聊接收开启（回复"已开启飞书群聊接收"）。
3. 在飞书群里把机器人「因子狗」加进群（群设置 → 添加机器人）。
4. 在群里 `@因子狗` 发任意消息，Chat-Codex 终端/TUI 会出现配对码；**用你自己的账号**在群里发送 `/pair <配对码>`。
5. 配对成功后你就是该群的超级管理员，也只有你能批准审批类操作。

## 群成员使用方式

- 成员第一次发言前需登记展示名：`@因子狗 /name 你的名字`（只影响展示，不影响权限）。
- 之后 `@因子狗 今天有哪些比赛？` 即可查询；必须 @ 机器人才会响应。
- 请求"跑分析 / 结算 / 退役因子"等操作会被拒绝，提示只有所有者能执行。

## 今日单（飞书卡片）

在群里（或私聊）发送「今日单 / 待结算订单 / 看板」，机器人会：

1. 指定单个 agent（如"梭哈2狗的单"）→ 运行 `python3 render_feishu_orders_card.py <agent名>`，发送对应单卡 `lota_data/orders_card_<agent>.json`；
2. 未指定 agent / 说"全部"或"看板" → 运行 `python3 render_feishu_orders_card.py`（默认 梭哈2狗、均注狗、alpha2狗），发送合并卡 `lota_data/orders_card.json`。

卡片内容与订单邮件一致（时间、联赛、对阵、选择、让球、赔率、仓位，NEW/变化/已移除标记齐全）。单 agent 查询绝不会用合并卡代替。

今日无待结算订单时机器人会直接文字回复。

## 常用命令

- `/whoami`：查看当前群内身份与角色
- `/status`：查看 session、权限、队列状态
- `/permission`：查看/切换审批模式（群内审批默认仅超级管理员）
- `/help`：查看当前渠道可用命令

## 注意

- 群聊功能在 0.1.5 里属于预发布能力（官方默认关闭），本地启用后如遇异常，可在私聊发 `/group off` 回退。
- 升级或重装 `chat-codex`（`npm i -g chat-codex`）会覆盖本地补丁，需重新执行：
  - `src/channels/feishu/feishu-feature-flags.js` 中 `FEISHU_GROUP_RECEIVE_PUBLICLY_ENABLED = true`
  - `src/bridge/formatters.js` 中群消息前缀带上 open_id
