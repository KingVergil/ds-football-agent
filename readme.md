# ds-agents — 足球投注分析 agent（DSH harness + Python 引擎）

多智能体足球投注分析系统：DSH harness 插件负责**只读数据工具 + 仪表盘 + 桥接**，
Python 引擎负责**确定性业务（资金 / 订单 / 结算 / 因子）与分析执行**（随仓库发布）。

```
harness 插件（只读工具 + 斗狗场） ──POST /ds-run──►  python -m src.bridge（引擎）
                                                    │  数据准备 → 读人设 → 查资金
                                                    │  → LLM 决策 → 下单 → 结算 → 因子
                                                    └──► 写回 roles/<狗>/（本地唯一真源）
```

## 架构分层

| 层 | 是什么 | 提供什么 | 归属 |
|---|---|---|---|
| **插件** `ds-agents-lota-data` | 能力（工具 + UI） | 只读数据工具 + 「斗狗场」仪表盘 + 创建狗 + 回放入口 | 开源（`harness-plugin/`） |
| **Python 引擎** | 执行核心 | 数据准备 / 分析下单 / 结算 / 因子归纳 / 因子退役 / 状态 / 重置 | 开源（`python-engine/`） |
| **数据获取层**（python 引擎） | 缓存生产者 | 抓 Lota API → 写本地缓存（`src/data_manager.py` / 桥 `prepare`） | 开源（需 `LOTA_API_KEY`） |
| **运行时数据** `python-engine/data/` | 本地真源 | matches/features/tags 缓存 + `roles/<狗>/`（人设/资金/订单/因子） | 私有（不入库） |

- **固定流不走 LLM 工具面板**：数据准备 / 分析 / 结算 / 因子归纳 / 因子退役 的执行入口只有
  斗狗场按钮（`POST /ds-run` → 插件直接 spawn `python -m src.bridge`）与回放（`/ds-replay`）。
- **harness agent 只剩两个职责**：回答数据/状态类问题（只读工具）；回放暂停时起草
  「下一轮方向建议」供用户编辑（`ds_replay` 半交互）。
- **LLM 只在引擎内两处**：analyze 的一次决策 + factor 相关旁路调用；确定性业务全部在引擎代码里。

## 数据流

```
Lota API ──(python 引擎数据层 + LOTA_API_KEY)──► 本地缓存 matches/features/tags
                                        │
                            ┌───────────┴───────────┐
                            ▼                       ▼
                     lota_* 只读工具          /ds-dashboard 仪表盘
                            │                       │
                            └──── POST /ds-run ─────┘
                                        ▼
                          python -m src.bridge（NDJSON 协议）
                              ├─ analyze：读人设/记忆/资金 → LLM 决策 → 下单
                              ├─ settle / factor-induction / factor-review
                              └─ 写回 roles/<狗>/ + factors/
```

桥协议（stdin 单行 JSON 请求 / stdout NDJSON 事件）见 [`harness-plugin/docs/bridge.md`](harness-plugin/docs/bridge.md)。

## 部署使用

### 1. 前置

- DSH harness（Cordis 运行环境）
- Python 3.10+（引擎，`pip install -r` 依赖见 `python-engine/`）
- Python 引擎自带数据获取（`src/data_manager.py`，需 `LOTA_API_KEY`，见 [`fetcher_protocol.md`](harness-plugin/docs/fetcher_protocol.md)）

### 2. 装插件 + 配引擎

把 `harness-plugin/` 作为 npm 包（`ds-agents-lota-data`）或本地路径，插件行并入 composition：

```yaml
- id: lota-data
  name: 'ds-agents-lota-data'
  config:
    cacheDir: ./python-engine/data   # 缓存根目录（matches/features/tags/roles）
    engineRoot: ./python-engine      # Python 引擎根目录（桥 spawn python -m src.bridge）
```

引擎需要环境变量（可放 `~/.zshrc`，插件 `bridge.js` 会直读注入子进程）：

```bash
export DEEPSEEK_API_KEY=...   # 引擎内 LLM 决策
export LOTA_API_KEY=...       # 数据获取层（python 引擎，key 找维护者要）
# 邮件（可选）：QQ_EMAIL_ADDR / QQ_EMAIL_AUTH_CODE 或 EMAIL_163_ADDR / EMAIL_163_AUTH_CODE
```

### 3. 准备数据

两种方式，二选一：

- **公开数据包**（开箱即玩）：解压 `fixtures/testdata-14d.tar.gz`（或
  `fixtures/testdata-30d.tar.gz`，过去一个月），把 `cacheDir` 指向解压目录。
- **真实数据**：配好 `LOTA_API_KEY` 后由引擎自己拉——桥 `prepare`（live 强制刷新 / replay 缓存优先）
  会抓比赛 + compact-fet + 切 sections；命令行可用 `dsfootball_cli.py dashboard` 触发。

### 4. 初始化一只狗（角色）

一只狗 = `roles/<狗>/persona.md`（人设，唯一源）+ `roles/<狗>/<狗>.json`
（资金/订单/limits/status）+ 可选注册表条目（`data/dogs.json`，结构化配置），
外加引擎按需生成的 `memory/`、`factors/`。初始化方式：

1. **斗狗场「创建狗」**（推荐）：仪表盘表单 → `POST /ds-dogs`，插件幂等补建
   `persona.md`（默认保守模板）与 `<狗>.json`（初始资金默认 10000），并写入 `dogs.json` 注册表。
   新建狗默认 `status=sandbox`（观察期，不进全量默认列表）。
2. **插件 config.roles 内联**：`config.roles: [{ name, scope, initial_capital, alpha_mode, limits, enabled, emoji, c1, c2 }]`。
   只影响展示/默认列表，角色文件仍需在本地存在。
3. **手动建目录**：`mkdir -p roles/<狗>/`，写 `persona.md` 与 `<狗>.json`
   （字段见 `python-engine/src/role_registry.py::sync_from_registry`），
   然后 `python -m src.role_registry sync` 幂等补缺/迁移。

初始化完成后：

- `status=live` 的狗进入全量默认列表（`python -m src.role_registry live`），
  斗狗场逐狗按钮与 `./batch_agents.sh` 群体操作都会带上它；
- 人设/limits/scope 后续可在斗狗场编辑狗（`PATCH /ds-dogs/<name>`），资金与订单不受影响；
- 删除狗只移注册表（`DELETE /ds-dogs/<name>`），历史订单/因子/资金保留；
- 公开仓库**不携带任何狗数据**：本地没有 `roles/<狗>/` 就不会出现在任何列表/群体操作里。

> 狗列表完全由本地角色派生（`python-engine/src/role_registry.py`），与仓库里提交了什么无关。

### 5. 日常使用

在挂载插件的 preset 下开 session：

| 你要干的 | 直接说 |
|---|---|
| 分析下单 | 斗狗场逐狗点「⚡ 分析」（或「分析 <狗>」） |
| 结算 | 斗狗场「🧾 结算」 |
| 因子归纳 / 退役 | 斗狗场「🧬 归纳」「🪦 Review」 |
| 回放 | 「跑回放」/ `ds_replay` 工具 |
| 数据/状态问答 | 「<狗> 余额多少」「今天有哪些竞彩比赛」 |

命令行批量（`python-engine/`）：

```bash
./batch_agents.sh analyze live        # 全部 live 狗分析（足球当日）
./batch_agents.sh settle live         # 全部 live 狗结算
./batch_agents.sh status              # 全部状态
./batch_agents.sh dashboard           # 刷新数据并打开看板
./batch_agents.sh factor-induction    # 因子归纳
```

> 群体操作（analyze/settle/factor）只作用于本地 `status=live` 的角色；
> 公开仓库零狗时它们自然为空操作，创建狗后自动生效。

## 目录结构

```
harness-plugin/            # 开源插件（入库）
├─ index.js                # 插件本体：只读工具注册 + 斗狗场 + 定时器 + prompt section
├─ bridge.js               # dsh ↔ python-engine 统一桥（NDJSON，func 白名单）
├─ dashboard.js / client.js  # 「斗狗场」仪表盘（/ds-dashboard + /ds-dogs + /ds-run + /ds-replay）
├─ dogRegistry.js          # 运行时狗注册表（创建/编辑/删除狗，幂等补建 Python 角色）
├─ replay.js / tools/replayTool.js  # 回放（沙箱目录模型，半交互）
├─ tools/deterministic.js  # 只读数据工具（lota_matches / lota_match / lota_sections / lota_status）
├─ tools/roles.js          # 角色解析（狗列表按本地角色派生）
├─ docs/                   # 桥协议 / 缓存格式 / 回放设计
└─ SKILL.md / cordis.yml / package.json

python-engine/             # Python 引擎（入库）
├─ src/bridge.py           # 统一桥入口（8 个 func 白名单）
├─ src/agent.py            # 角色 Agent（人设/资金/订单/记忆）
├─ src/data_manager.py     # 数据获取层（Lota API，需 LOTA_API_KEY）
├─ src/place_orders.py / settle.py / factor_*.py  # 下单/结算/因子
├─ src/role_registry.py    # 角色注册表（live/all/alpha/sync）
├─ dsfootball_cli.py / batch_agents.sh   # CLI 批量操作
├─ data/                   # 运行时数据（私有，不入库）
└─ docs/                   # 架构/格式文档

fixtures/                  # 公开测试数据包（matches + features）
```

## 数据协议

开源仓库不绑定数据源。缓存格式 + Fetcher 接入见：

- [`harness-plugin/docs/cache_format_spec.md`](harness-plugin/docs/cache_format_spec.md) — 缓存 JSON 格式
- [`harness-plugin/docs/fetcher_protocol.md`](harness-plugin/docs/fetcher_protocol.md) — 数据怎么进缓存

自建数据源按 `cache_format_spec.md` 产出缓存即可；唯一数据专有点是 `odds.js` 的盘口排版解析。

## License

[MIT](LICENSE)
