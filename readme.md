# ds-agents — 足球投注分析 agent（harness 版）

纯 JS 重构的足球投注分析系统。harness agent 当「大脑」独立推理下单，确定性业务（资金 / 下单 / 结算 / 因子）**全部纯 JS**，Python 只保留 Lota 数据获取层（私有，不入库）。

```
LangGraph 状态机  ──重构──►  harness 插件（纯 JS 工具 + storage 域）
Python 业务代码  ──重构──►  JS 确定性层（资金/订单/结算/因子门控）
Python 数据获取  ──保留──►  lota_fetcher.js（私有，抓 Lota → 写缓存）
```

## 原理

### 架构分层

| 层 | 是什么 | 提供什么 | 归属 |
|---|---|---|---|
| **插件** `ds-agents-lota-data` | 能力（工具） | 读数据 + 下单/结算/因子全套工具 | 开源（`harness-plugin/`） |
| **预设** `ds-agents` | 大脑（人设 + 提示） | persona「足球分析 agent」+ 分析框架 section | 开源（预设目录） |
| **数据获取层** `lota_fetcher.js` | 缓存生产者 | 抓 Lota API → 写本地缓存 | 私有（单独分发） |

- **harness agent 是「大脑」**：主循环 LLM 自动驱动轮次，插件只注册 system-prompt section + 工具 + 读写 storage 域，不手拼 prompt、不自己调主循环 LLM。
- **确定性业务纯 JS**：金额折算、去重、已开赛保护、结算数学（亚盘/大小球/赢半输半）、因子门控（14 天休眠 + 低信息退役）都是确定性代码，LLM 只给「方向 + 比例」。
- **LLM 只收敛到两处**：主循环（分析/下单决策）+ 旁路 `ctx.llm.stream`（结算后反思、因子退役评估、因子判重）。

### 数据流

```
Lota API ──(lota_fetcher.js 私有)──► 本地缓存 matches/features/tags
                                        │
                            ┌───────────┴───────────┐
                            ▼                       ▼
                     lota_* 只读工具          /ds-dashboard 仪表盘
                            │
                     harness agent 判断（读人设 + 读因子记忆）
                            │
                     submit_orders（纯 JS 下单）
                            │
                     storage 域（ds_roles/ds_factors/ds_reflections/ds_slugs）
```

### 记忆读写闭环

因子记忆不是静态数据，是「每日 factor-induction」动态生产的：

```
analyze（读 ds_memory_js 注入活跃因子/负例护栏/历史反思）
   │ 下单
   ▼
settle（ds_settle_js 纯 JS 结算）
   │
   ▼
reflect（ds_reflect_js 旁路 LLM 因子发现 → 写回 factor_perf + 反思）
   │
   ▼
factor-induction（ds_factor_induction 去重合并；alpha 狗跨狗 1 次）
   │
   └──► 次日 analyze 再读回 ──► 闭环
```

## 部署使用

### 1. 前置

- DSH harness（Cordis 运行环境）
- Node ≥ 18（仅私有 `lota_fetcher.js` 需要全局 `fetch`）

### 2. 装插件

把 `harness-plugin/` 作为 npm 包（`ds-agents-lota-data`）或本地路径，插件行并入你的 composition（host 层全局挂载，或 preset 的 `agent.cordis.yml`）：

```yaml
- id: lota-data
  name: 'ds-agents-lota-data'
  config:
    cacheDir: ./python-engine/data   # 缓存根目录（matches/features/tags/roles）
```

### 3. 配预设

复制 `ds-agents` 预设（persona「足球分析 agent」+ 分析框架 section）到你的 `.agent-presets/`。预设里的 persona 告诉模型「分析某狗 = refresh → 读数据 → 读人设 → 读记忆 → 查资金 → 判断 → 下单」的完整工作流。

### 4. 准备数据

两种方式，二选一：

- **种子数据包**（给只想先跑起来的人）：解压后 `cacheDir` 指向它，跑一次 `ds_migrate_storage` 把 7 只狗的初始状态迁入 storage 域。
- **真实数据**（给有数据源的人）：拿私有 `lota_fetcher.js` + API key，跑：

  ```bash
  export LOTA_API_KEY=...
  node lota_fetcher.js refresh-range 2026-08-01 2026-08-14   # 拉比赛 + 足球日切分写盘
  node lota_fetcher.js prefetch 2026-08-14                   # 抓 compact-fet + 切 sections
  ```

### 5. 日常使用

在 `ds-agents` 预设下开一个 session，用自然语言下指令：

| 你要干的 | 直接说 |
|---|---|
| 分析下单 | 「分析梭哈2狗」/「分析梭哈2狗 2026-08-14」 |
| 结算 | 「结算梭哈2狗 2026-08-14」 |
| 结算后反思 | 「反思梭哈2狗」 |
| 因子退役 | 「因子审查梭哈2狗」 |
| 因子归纳 | 「因子归纳 梭哈2狗」/「因子归纳 alpha」 |

7 只狗：`alpha2狗` `alpha狗` `均注狗` `梭哈2狗` `梭哈3狗` `平局狗` `跟风狗`。

## 目录结构

```
harness-plugin/            # 开源（入库）
├─ index.js                # 插件本体：工具注册 + 分析框架 section
├─ dashboard.js / client.js  # 「斗狗场」仪表盘（Host /docs + Client tab）
├─ domains.js / storage.js  # storage 域定义 + 数据迁移
├─ memory.js               # 因子记忆 + 反思注入（读）
├─ settle.js / settleEngine.js / placeOrders.js  # 结算数学 + 下单
├─ reflect.js / factorReview.js / factorInduction.js  # 反思 + 退役 + 归纳（写）
├─ odds.js / fundLimits.js # 盘口解析 + 资金约束
├─ docs/
│  ├─ cache_format_spec.md # 缓存文件格式规范（盘上格式）
│  └─ fetcher_protocol.md  # 数据获取层接入协议（网络侧）
└─ SKILL.md / cordis.yml / package.json

python-engine/             # 私有（不入库）
├─ lota_fetcher.js         # Lota 数据获取层（单独分发）
└─ data/                   # 缓存 + 角色数据（本地保留）
```

## 数据协议

开源仓库不绑定数据源。缓存格式 + Fetcher 接入见两份文档：

- [`harness-plugin/docs/cache_format_spec.md`](harness-plugin/docs/cache_format_spec.md) — 缓存 JSON 长什么样
- [`harness-plugin/docs/fetcher_protocol.md`](harness-plugin/docs/fetcher_protocol.md) — 数据怎么进缓存、`lota_fetcher.js` 放哪怎么用

自建数据源只要按 `cache_format_spec.md` 产出缓存文件，插件就能跑；唯一数据专有点是 `odds.js` 的盘口排版解析。

## License

[MIT](LICENSE)
