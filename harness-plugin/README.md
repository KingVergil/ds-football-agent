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
├─ scripts/install.mjs     # 跨平台一键安装器（macOS / Linux / Windows）
├─ cordis.yml / package.json / SKILL.md
└─ docs/                   # 桥协议 / 缓存格式 / 回放设计
```

## 挂载

### 一键安装（推荐，macOS / Linux / Windows 通用）

```bash
node harness-plugin/scripts/install.mjs \
  --profile-dir <你的 dsh profile 目录，如 ~/.dsh/profiles/web 或 %USERPROFILE%\.dsh\profiles\web> \
  --set-keys DEEPSEEK_API_KEY=... LOTA_API_KEY=...
```

脚本幂等做三件事：把插件装进 profile 的 `node_modules`（POSIX 软链 / Windows
junction，失败自动退化复制）、把挂载条目写进 `cordis.patch.yml`（已有则跳过）、
把密钥写进 `<engineRoot>/.env`（POSIX 上再顺手追加 `~/.zshrc` export 块，命令行 CLI 也能用）。
**全部写入用户目录，不需要管理员/提权**。详见脚本头部注释。

### 手动挂载

把 `cordis.yml` 里的插件行并入你的 profile / agent preset：

```yaml
- id: lota-data
  name: 'ds-agents-lota-data'
  config:
    cacheDir: C:/path/to/python-engine/data   # 缓存根目录（matches/features/tags/roles）
    engineRoot: C:/path/to/python-engine      # Python 引擎根目录
    # pythonBin: C:/Python312/python.exe      # 可选；缺省按平台自动选（Win=python，macOS/Linux=python3）
    # envFile: C:/path/to/.env                # 可选密钥文件；缺省自动读 <engineRoot>/.env 与 ~/.env
```

引擎需要 `DEEPSEEK_API_KEY`（LLM 决策）与 `LOTA_API_KEY`（数据获取）。密钥放哪都行，
`bridge.js` 按 **环境变量 > `.env`（`config.envFile` → `<engineRoot>/.env` → `~/.env`）>
`~/.zshrc` / `~/.bashrc`（仅非 Windows 的历史兜底）** 的顺序注入子进程 env。
`.env` 格式见 [`python-engine/.env.example`](../python-engine/.env.example)。

> Windows 用户不用碰 `~/.zshrc`：把密钥写进 `python-engine/.env` 即可；
> `python3` 在 Windows 上往往是商店别名，插件会自动改用 `python`/`py`（或显式配 `config.pythonBin`）。

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
