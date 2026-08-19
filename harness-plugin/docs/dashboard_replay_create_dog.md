# 斗狗场：创建狗 + 详情页回放 + 分析直启（2026-08-18 第二轮）

## 功能

0. **单狗分析直启（不经对话，且子代理实时可见）**：行内「⚡ 分析」按钮直接 `POST /ds-analyze`——
   ① 进程内确定性 prefetch（`prepareDay(day, mode="live")` 单例：同 day 并发/重复点击共享同一次拉取）；
   ② 分析子代理**优先挂在当前会话 agent**（body `sessionId`（视图 slot 的 `inject` 注入）→ host
   `ctx.agents.get(sessionId)`）——零对话往返、对话区 subagent 面板**实时可见**（旧「挂在对话的子进程」体验）；
   会话不可用时**回退 headless** `dsh --profile headless` 子进程（与定时邮件任务同一通道，
   `DSH_SCHEDULED_RUN=1` 防递归）。全程进度经 taskReg → `/ds-tasks` → 斗狗场轮询可见；
   `dog|day` 在途去重（409），坏狗名/坏日期 400。其余按钮（结算/归纳/判重/review）暂仍走会话输入框，可同样改造。
1. **回放入详情**：详情页新增「▶️ 回放」区——日期范围 / 模式（半交互/一路到底）/
   退役周期 / 起点（当前/从 0）/ 结束还原，一键把 `ds_replay(...)` 命令写入会话输入框执行；
   本狗的回放会话（`replays/<run_id>/session.json`）逐条展示：
   - `paused`：方向建议**可编辑**（textarea）→「继续（应用方向）」「一路到底」「回退到该日」；
   - `running`：任务进度（phase/done/total，来自 /ds-tasks）+ 运行日志尾部；
   - `finished`：报告路径（`report.md`）。
2. **回放刷新详情下单**：dashboard 客户端常驻 10s 静默轮询；有运行中任务/回放会话时
   提速到 4s —— 回放逐日下单、结算、暂停状态都会实时反映在详情页的订单列表与会话面板。
3. **主页创建狗**：头部「➕ 创建狗」→ 表单覆盖当前狗的全部设置：
   狗名 / 复制自现有狗（拉 /ds-persona/<name> 套用人设）/ 人设 persona / 日常比赛范围
   （jc/beidan/all）/ 初始资金 / α 模式 / 表情 / 头像配色（c1/c2）。
   提交 POST /ds-dogs → 写入运行时注册表 + 初始化 ds_roles 记录 → 列表即时出现、可直接进详情。

## 实现落点

- Host 路由（`dashboard.js`）：
  - `POST /ds-analyze`：标准单狗分析直启（prefetch 进程内 + 会话挂载子代理 + headless 回退），
    `__inflightAnalyze` 在途去重、`taskReg` 记进度、`spawnHeadless` 复用定时任务通道
    （DEEPSEEK_API_KEY 从环境变量 / .env 兜底）；会话挂载失败自动回退 headless，保证分析仍能跑；
  - `POST /ds-dogs`：校验（狗名合法性/重名）→ `createDog` → 200/400 JSON；
  - `GET /ds-persona/<name>`：按需返回完整人设文本（创建表单「复制自」用）；
  - `/ds-dashboard` 的 `replays[]` 增补 `interactive/reset/restoreAfter/logTail/reportExists`。
- `index.js`：`setupDashboard` 增传 `{ engineRoot, pythonBin, taskReg }`。
- 运行时狗注册表（`dogRegistry.js`）：`cacheDir/dogs.json`（mtime 缓存读）；
  `createDog` 写注册表 + 幂等初始化 ds_roles（资金=initial_capital、空订单）。
- 角色层（`tools/roles.js`）：`resolveRoles` 合并注册表——`dogs` 是惰性 getter，
  每次访问重新读注册表，新狗无需重启即可被 dashboard / 工具默认列表（`roles.dogs`）看到；
  `ensureRoleRecords` 只初始化 config 狗 + 注册表狗（绝不替 7 只真狗造空记录）。
- 客户端（`client.js`）：`CreateDogPanel` / `ReplaySection` / `isDayStr`；行内「回放」按钮移除
  （入口移入详情）；主列表暂停会话面板可点击进入对应狗详情。

## 回放流程加固（本轮）

- `dataflow.js prepareRange`：加单例（幂等缓存 + in-flight 去重，TTL 5 分钟），与 `prepareDay` 同构。
- `replay.js`：`validateReplayRange`（格式/顺序/≤60 天）在任何副作用前校验；
  `restoreDomainSnapshot` 改为**真替换**（先删快照外新增 key，再全量 put）。
- 见 `docs/replay_mode.md`「启动次序（单例取数 → 范围正确性 → 替换/还原）」。

## 验证

```bash
cd harness-plugin
node --check dataflow.js && node --check replay.js && node --check dogRegistry.js \
  && node --check tools/roles.js && node --check dashboard.js && node --check client.js
node --test tests/dataflow.test.mjs && node --test tests/settleFlow.test.mjs \
  && node --test tests/taskStatus.test.mjs
```

## 注意

## 创建狗设计定稿（2026-08-18 第三轮，已实现）

- **创建即同步**：`POST /ds-dogs` 一次写 注册表 `dogs.json`（原子写 tmp+rename，
  防 dashboard 4s 轮询读到半截）+ ds_roles 记录 + Python 角色（幂等补缺）。
- **persona 唯一源 = Python `roles/<狗>/persona.md`**：注册表/config 内联 persona 不再生效；
  创建时未填/清空 → 默认模板兜底（不允许空）；旧注册表 `persona` 字段由
  `python dsfootball_cli.py role-sync` 迁移到 persona.md 后剥掉。
- **limits 结构化存 `roles/<狗>/<狗>.json`**：LangGraph（`fund_limits.order_limits_for`）
  优先读文件、缺省走 `AGENT_LIMITS` 代码默认；表单默认 `max_exposure_pct=40%`。
- **enabled 控制全量默认列表**（分析/结算/回放）：`roles.enabledDogs` = 7 只生产狗 +
  注册表 enabled=true；观察狗（👀）默认不进，但显式指定狗名不受限；看板显示全部注册表狗。
- **alpha 结构化存角色** `alpha_mode`：`factor_induction` 的 ALPHA_ROLES/ALL_ROLES
  从角色文件 + 注册表派生（`src/role_registry.py`），不再硬编码。
- **生命周期**：`PATCH /ds-dogs/<name>` 编辑配置（同步 Python 角色配置字段 + persona.md，
  资金/订单一律不动）；`DELETE /ds-dogs/<name>` 仅移注册表，历史订单/因子/资金保留。
- **撞名防护**：existingNames = config 狗 + 默认狗 + 注册表 + Python `roles/` 全部目录名。
- **迁移入口**：`python dsfootball_cli.py role-sync [--dry-run]`（补建缺失角色，
  幂等；已跑：智疯狗 → persona.md + 智疯狗.json）。
- **batch_agents.sh**：`AGENTS=($(python -m src.role_registry enabled))`，跟随注册表。

- **Host 半身（dashboard.js/dogRegistry.js/tools/roles.js/replay.js/dataflow.js）在 dsh 进程内存里跑，
  改动后需重启 dsh web 服务才生效**（`POST /ds-dogs` 等新路由在重启前返回 405/SPA 兜底）。
- 客户端 bundle（`client.js`）由 `/plugins/ds-agents-lota-data/client.js` 从磁盘实时下发，
  刷新页面即生效（manifest rev 只是缓存戳）。
- 回放启动走会话输入框（firePrompt → 队列）而非直接 HTTP 触发：`ds_replay` 需要 parent agent
  起子分析代理，与既有按钮（分析/结算/归纳）同一条路径。
