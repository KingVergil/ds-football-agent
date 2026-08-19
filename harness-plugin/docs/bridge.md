# dsh ↔ python-engine 桥（薄壳架构，2026-08-19 定稿）

## 定位

dsh 是薄壳：数据准备 / 分析 / 结算 / 因子归纳 / 因子退役 / 状态 / 刷新 / 重置
全部由 python-engine 执行。harness 的 LLM 职责只剩回放模式的中途
「起草下一轮方向建议卡片」，供用户编辑后注入下一周期退役评估。

数据唯一真源 = `python-engine/data` 文件（`roles/<狗>/<狗>.json` + `memory/` +
`predicts/` + `capital_history.json` + `persona.md`）。storage 域（`ds_roles` 等）、
migrate/export、JS 镜像实现（settleEngine / placeOrders / factorInduction /
factorReview / fanout / flows / reflect / memory 的 domain 部分）退役，不再装配。

## 协议（仿 place_orders 桥）

入口：`python -m src.bridge`（`python-engine/src/bridge.py`；解释器按平台自动选
`python3`/`python`，挂载时可用 `config.pythonBin` 覆盖）。

- stdin：单行 JSON 请求
  `{func, dog?, day?, start?, end?, opts?}`
- stdout：NDJSON 事件流（逐行 JSON）
  - `{"type":"progress","phase":"...","done":n,"total":n,"detail":"..."}`
  - `{"type":"result","func":"...","data":{...}}`
  - `{"type":"error","func":"...","message":"..."}`
- stderr：诊断/内部 print（一律重定向，stdout 只出 NDJSON）

dsh 侧固定 argv 直接 spawn（`spawn(pythonBin, ["-m","src.bridge"], {cwd: engineRoot})`），
不拼 shell、无 bash -c。API key（`DEEPSEEK_API_KEY` / `LOTA_API_KEY`）由
`harness-plugin/bridge.js` 按 **环境变量 > `.env`（`config.envFile` →
`<engineRoot>/.env` → `~/.env`）> `~/.zshrc` / `~/.bashrc`（仅非 Windows 兜底）**
注入子进程 env（低优先级只填空缺，不覆盖已有环境变量）。

## func 白名单（8 个）

| func | 必填入参 | 说明 |
|---|---|---|
| `prepare` | day, opts.mode(live/replay), opts.jingcai_only | 数据准备（live 强制刷新 / replay 缓存优先） |
| `analyze` | dog, day, opts.prefetched/live/jingcai_only | 分析（python 内 LLM 决策 + 下单） |
| `settle` | dog, day | 结算未结算订单 |
| `factor-induction` | dog | 因子归纳/去重（alpha 跨狗逻辑引擎内判定） |
| `factor-review` | dog, end(+start), opts.user_notes | 因子退役评估；结果含 factor_summary / pnl_trend / cycle_changes / review_md_tail |
| `status` | dog | 资金/待结算/因子状态/资金曲线/上次退役 |
| `refresh` | dog, day | 刷新订单组（退未开赛、保留已开赛） |
| `reset` | dog, opts.reset_mode(soft/full), opts.capital | 重置（soft 保留记忆 / full 清空） |

双端校验：dsh 侧 spawn 前校验 func 白名单 / 狗存在（roles 目录）/ 日期格式与区间 ≤60 天；
python 侧入口再校验一遍。写操作（analyze/settle/induction/review/refresh/reset）
同狗串行 + 在途去重（HTTP 409）。

## dsh web 入口

- `POST /ds-run`：`{dog, func, day|start|end, opts}` → 后台 spawn 桥，
  NDJSON progress → taskReg → `/ds-tasks` 轮询；结果摘要进任务 detail（结果卡片）。
- `POST /ds-replay`：`{dogs, start, end, mode, factor_review_every, reset, restore_after}`
  → 插件侧回放会话（`replay.js` 逐日逐 func 调桥）。
- `POST /ds-replay/<run_id>`：`{action: continue|to_end|rewind, induction_notes?, rewind_to?}`
  → 续跑暂停会话。

agent 工具面板只剩只读：`lota_matches` / `lota_match` / `lota_sections` / `lota_status`。

## 回放（replay.js）

> 2026-08-19 起改为**沙箱复制目录模型**，完整设计见
> [`replay_sandbox_design.md`](./replay_sandbox_design.md)（v2 定稿）。

- 沙箱身份：`replays/sandboxes/<狗>_<MMDD>/`（如 `梭哈2狗_0718`），幂等创建、可续跑；
- 目录：`snapshot/`（起点复制）→ `workspace/`（回放运行写盘根，桥经 `role_root` 写它）→
  `checkpoints/<day>__pre-factor|post-day/` + `trajectory.json` / `factor_reviews.json` /
  `facts.json`（事实订单/因子/资金曲线，总结交给 dsh）+ `report.*`；
- 老狗沙箱起点 = 线上「开始→D」复制到 **D 结算后/因子归纳前**（`roles/<狗>/history/<D>__pre-factor/`，
  live 结算后自动落盘）；D 当天从因子归纳继续，D+1 起完整日管线；新狗 = 空骨架完整日管线；
- 转正/放弃 = dsh 文件原语（`promoteSandbox` 备份线上→整目录替换；`abortSandbox` 删沙箱），
  入口 `POST /ds-sandbox/<沙箱>/promote|abort`；
- 角色根覆盖：桥 `opts.role_root`（沙箱 workspace 白名单）→ python 引擎
  `DS_ROLES_ROOT` / `DS_SESSIONS_ROOT` / `DS_FACTORS_ROOT` 重定向，线上零影响；
- `enable` 已重构为 `status ∈ {live, sandbox, archived}`（`role_registry sync` 迁移；
  `batch_agents.sh` 默认列表改 `role_registry live`）；
- 半交互暂停：harness LLM（`tools/llmText.js::streamText`，失败回退启发式）起草方向建议 →
  卡片预填 → 用户编辑 → `induction_notes` 注入下一周期；agent 面板 `ds_replay` 工具为唯一回放入口。

## 验证

```bash
cd harness-plugin
node --check bridge.js && node --check dashboard.js && node --check replay.js \
  && node --check index.js && node --check client.js
node --test tests/taskStatus.test.mjs   # 无宿主依赖测试

# python 桥冒烟（离线可测：status / prepare replay / 校验错误）
cd ../python-engine
printf '%s' '{"func":"status","dog":"梭哈2狗"}' | /Users/cjy/miniconda3/bin/python -m src.bridge
printf '%s' '{"func":"prepare","day":"2026-06-11","opts":{"mode":"replay"}}' | /Users/cjy/miniconda3/bin/python -m src.bridge
```

> ⚠️ `analyze` / `factor-review` 会触发 python 引擎内的 LLM 与写盘；冒烟测试只用
> `status` / `prepare(replay)` / 校验错误分支，避免污染真实角色数据。

## LLM 失败语义（2026-08-19 修正，发布用）

- `DeepSeekProvider._call_api` 不再静默返回空串：缺 `DEEPSEEK_API_KEY`、网络失败、
  API 返回异常一律抛 `RuntimeError`（含明确原因）。
- 影响：analyze 失败会以桥 error 事件响亮暴露（不再出现「ok + 0 订单」或
  `(dry-run: 无 LLM 响应)` 占位）；`run_reflect` / `factor_review` 已有 try/except 兜底，
  失败时正常降级（无反思输出 / 自动休眠），session 文案已改为诚实描述。
