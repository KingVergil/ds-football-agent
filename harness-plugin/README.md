# ds-agents-lota-data

DSH harness 插件：**本地缓存只读工具 + 「斗狗场」仪表盘 + python 引擎桥**。

## 这是什么

一个 Cordis 插件（`index.js`），薄壳架构：

- **只读数据工具**（agent 面板）：`lota_matches(date)`、`lota_match(lota_id)`、
  `lota_sections(lota_id, slugs)`、`lota_status(dog)` —— 只读本地缓存，不触网、不含密钥。
- **斗狗场仪表盘**（`/ds-dashboard`）：狗列表 / 资金曲线 / 创建狗（`/ds-dogs`）/
  逐狗执行（`POST /ds-run` → python 桥）/ 回放（`POST /ds-replay`）。
- **统一桥**（`bridge.js`）：`spawn python -m src.bridge`（NDJSON 协议），
  固定流（数据准备/分析/结算/因子归纳/因子退役/状态/刷新/重置）全部由本地 Python 引擎执行，
  不再注册为 LLM 工具。

Python 引擎与运行时数据见仓库根 [`../readme.md`](../readme.md)（引擎代码随仓库发布，
`python-engine/data/` 运行时数据不入库）。数据获取也在引擎侧
（`python-engine/src/data_manager.py`，需要 `LOTA_API_KEY`，key 找维护者要）。

## 目录

```
harness-plugin/
├─ index.js                # 插件本体（装配 + 只读工具 + 定时刷新/邮件）
├─ bridge.js               # dsh ↔ python-engine 桥（func 白名单 / 进度 / 超时）
├─ dashboard.js / client.js  # 斗狗场仪表盘（Host 路由 + Client tab）
├─ dogRegistry.js          # 运行时狗注册表（创建/编辑/删除狗，幂等补建 Python 角色）
├─ replay.js / tools/replayTool.js  # 回放（沙箱目录模型，半交互暂停）
├─ tools/deterministic.js  # 只读数据工具
├─ tools/roles.js          # 角色解析（狗列表按本地角色派生）
├─ cordis.yml / package.json / SKILL.md
└─ docs/                   # 桥协议 / 缓存格式 / 回放设计
```

## 挂载

把 `cordis.yml` 里的插件行并入你的 profile / agent preset：

```yaml
- id: lota-data
  name: 'ds-agents-lota-data'
  config:
    cacheDir: ./python-engine/data   # 缓存根目录（matches/features/tags/roles）
    engineRoot: ./python-engine      # Python 引擎根目录（桥 spawn python -m src.bridge）
```

引擎需要 `DEEPSEEK_API_KEY`（LLM 决策）与 `LOTA_API_KEY`（数据获取）；
插件 `bridge.js` 会直读 `~/.zshrc` 注入子进程 env。

## 缓存格式

详见 [`docs/cache_format_spec.md`](docs/cache_format_spec.md)。要点：
`matches/<date>.json`、`features/<id>.json`、`tags/<id>.json`、`predicts/<id>.json`、`orders/<id>.json`。

## 数据协议

- [`docs/fetcher_protocol.md`](docs/fetcher_protocol.md) — 数据怎么进缓存
  （参考实现 = python 引擎 `src/data_manager.py`，需 `LOTA_API_KEY`）
- [`docs/bridge.md`](docs/bridge.md) — dsh ↔ python-engine 桥协议

## 数据专有部分

`lota_match` 的赔率提取（`odds.js`）依赖作者数据源的 compact-fet 文本排版，
**用户自定义数据源时需修改/替换 `odds.js`**（详见文件头部注释与 `SKILL.md`）。

## License

MIT
