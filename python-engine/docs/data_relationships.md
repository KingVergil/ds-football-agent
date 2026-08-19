# lota_data 数据关系整理 + 命名方案

> 结论：`lota_data/` 名不副实——它不只是 Lota 数据，而是「比赛数据缓存 + 系统状态 + 衍生输出」三类的混合工作目录，**应改名为 `data/`**。

## 1. 现状：三类数据混在一个目录

| 类别 | 目录/文件 | 内容 | 谁读/写 | 性质 |
|---|---|---|---|---|
| **比赛数据**（数据源缓存） | `matches/{date}.json` | 比赛列表（按足球日） | data_manager | 只读消费 + 刷新写 |
| | `features/{id}.json` | compact-fet 原始缓存 | data_manager | 同上 |
| | `tags/{id}.json` | 切分段落 `{slug:text}` | data_manager | 同上 |
| | `predicts/{id}.json` | 预测（当前空） | data_manager | 同上 |
| | `orders/{id}.json` | 订单（当前空，实际在 roles/） | data_manager | 同上 |
| | `blacklist.json` | 禁下注黑名单 | data_manager | 只读 |
| **系统状态**（source of truth） | `roles/{name}/{name}.json` | 每个"狗"的资金/订单/配置 | role.py | 读写 |
| | `roles/{name}/persona.md` | 人设 | role.py | 读写 |
| | `roles/{name}/memory/` | 因子记忆 + 反思 | memory.py | 读写 |
| | `roles.backup.*/` | 角色备份 | — | 备份 |
| | `factors/fac_*.json` | 因子库（定义+性能） | factor_registry / memory | 读写 |
| | `agent_prompts/*.json` | 策略/系统提示词 | prompt_builder | 读写 |
| | `sessions/{user}/...` | 会话日志 | session_logger | 追加写 |
| | `agent_memory/` | 旧兼容（空，已迁 roles/） | memory.py | 兼容 |
| **衍生/输出**（生成物） | `reports/` | 报告 | — | 生成 |
| | `email_snapshots/` | 邮件快照 | order_email | 生成 |
| | `exports/` | 导出 | — | 生成 |
| | `labels/` | 标注 | agent.py | 生成 |
| | `notes/` | 备注 | — | 生成 |
| | `backtests/` | 回测 | store.py | 生成 |
| | 根级 `dashboard.html/json`、`orders_card*.json`、`email_recipients.txt` | 看板/飞书卡片/收件人 | CLI / 卡片渲染 | 生成 |

## 2. 关系（读写依赖）

```
比赛数据（matches/features/tags/predicts/orders/blacklist）
   ↑ 读（消费）                    ↑ 写（刷新）
   ├─ agent.py 的 fetch_matches / fetch_features / fetch_scores / build_prompt
   ├─ data_manager（DataSource seam）
   └─ chuan_guan_dog / order_email / eval / label 等

系统状态（roles/ + factors/ + agent_prompts/ + sessions/）
   ↑ 读（载入）                    ↑ 写（保存）
   ├─ role.py: Role.load / save → roles/{name}/
   ├─ memory.py: 因子/反思 → roles/{name}/memory/ + factors/
   ├─ factor_registry / factor_induction: 跨狗因子 → factors/ + roles/
   └─ session_logger: 会话 → sessions/{user}/

衍生输出（reports/email_snapshots/exports/labels/notes/backtests/dashboard）
   ↑ 由 CLI / 卡片渲染 / 邮件 / 报告 生成，纯输出
```

**关键区分**：只有「比赛数据」是**数据源相关**（可刷新、有缓存策略）；「系统状态」是**系统的 source of truth**（资金、订单、人设、因子、会话）；「衍生输出」是**生成物**（可随时重建）。

## 3. 命名方案：`lota_data` → `data`

改名理由：目录名暗示"全是 Lota 数据"，但实际含人设、会话、因子等系统状态。

建议保留现有子目录名（改动最小），只改根名：

```
data/                     # 原 lota_data/
├── matches/ features/ tags/ predicts/ orders/ blacklist.json   # 比赛数据
├── roles/ roles.backup.*/ factors/ agent_prompts/ sessions/     # 系统状态
└── reports/ email_snapshots/ exports/ labels/ notes/ backtests/ # 衍生输出
```

改动面（机械替换）：`src/*.py` 里的 `"lota_data"` 字符串、`.gitignore`、`harness-plugin/cordis.yml` 的 `cacheDir`、`docs/cache_format_spec.md` 的路径描述。根名变量建议统一到一个常量（如 `DATA_ROOT`），避免散落。

## 4. 开源边界（呼应"不暴露 URL"）

- **公开**（cache_format_spec 覆盖的）：只有「比赛数据」的**盘上格式**（matches/features/tags/predicts/orders 的 JSON schema）。
- **私有**：`roles/`（人设+资金+订单+因子记忆）、`sessions/`、`factors/`、`agent_prompts/`、所有 `*.backup.*/`、`email_recipients.txt`、`dashboard.html` 等——这些永不上传。
- **数据源 URL**（`http://deepdata.lota.tv/...`）只存在于私有 `src/data_manager.py` / `src/tools.py` 的 `BASE_URL`，开源面靠 Fetcher seam 挡住，不泄露。
- `.gitignore` 已整目录忽略 `python-engine/`（含 data/），无需逐项排。

## 5. 待办

- [x] 根名 `lota_data` → `data`（机械替换 + 统一 `DATA_ROOT` 常量）
- [ ] 新建 SDK 对比目录 `harness-sdk/`（效果 A：harness agent 当大脑）
- [ ] 把「比赛数据」的公开读逻辑做成 Python MCP server（复用 cache_format + extract_odds）
