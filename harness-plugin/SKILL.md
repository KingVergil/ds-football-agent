---
name: ds-agents-lota-data
description: 使用 ds-agents-lota-data 插件的只读工具回答比赛/缓存/角色状态问题；固定流（分析/结算/因子）执行入口是「斗狗场」仪表盘（POST /ds-run → python 桥），回放由 ds_replay 承担。适用于足球投注分析、查询比赛缓存、跑分析/结算/因子/回放时。
---

# ds-agents-lota-data 插件（薄壳架构）

本插件暴露**只读数据工具 + 斗狗场仪表盘 + 回放入口**。只读工具读本地缓存
（`config.cacheDir`，不触网、不含密钥）；固定流（数据准备/分析/结算/因子归纳/
因子退役/状态/刷新/重置）**没有 LLM 工具**——执行入口只有斗狗场表单
（`POST /ds-run` → 插件直接 spawn python 桥 `-m src.bridge`，本地 Python 引擎执行；
解释器按平台自动选 `python3`/`python`，可用 `config.pythonBin` 覆盖）。

## 你（harness agent）的职责

1. **数据/状态问答**：用只读工具回答比赛、缓存、狗状态类问题。
2. **引导用户执行固定流**：用户要「分析 / 结算 / 因子归纳 / 因子退役」时，
   引导去斗狗场逐狗点对应按钮（或说明会按本地 `status=live` 的狗依次跑）。
3. **回放半交互**：`ds_replay` 是唯一保留的工作流入口；暂停时你的职责是基于
   factor-review 结果起草「下一轮方向建议」供用户编辑（这是唯一 LLM 触达点）。

⚠️ 不要用 bash / 文件操作 / 手工编排去复刻固定流——编排已在引擎（python）里确定性完成。

## 工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `lota_matches` | `date`(必填), `lottery_type`(可选 jingcai/beidan/all), `strip_scores`(可选) | 某足球日的比赛列表 `{date, count, matches}` |
| `lota_match` | `lota_id`(必填), `strip_scores`(可选) | 单场全貌 `{lota_id, match, score, odds, sections, predictions, orders, cached_at, api_failed}` |
| `lota_sections` | `lota_id`, `slugs`(必填, string[]) | 段落 `{lota_id, sections, text}` |
| `lota_status` | `dog`(狗名) | 狗状态（python 桥只读封装）：资金/待结算/因子数/资金曲线/上次退役 |
| `ds_replay` | `start`/`end`/`dogs`/`mode`/`factor_review_every`/`reset`/`restore_after` 等 | 回放会话（沙箱目录模型，逐日逐 func 调桥；暂停卡片交用户编辑方向建议） |
| `ds_list_dogs` / `ds_create_dog` | 选狗 / 语言描述创建新狗（与「➕ 创建狗」表单同一逻辑） | 训练模式入口，纯对话驱动 |
| `ds_sandbox_list` / `ds_promote_sandbox` / `ds_abort_sandbox` | 沙箱列表 / 转正（替换线上+注册表翻 live）/ 放弃（删沙箱） | 训练模式收尾 |

> 训练模式（创建新狗/选狗 → 回放 → 转正/放弃）的对话流程由项目 skill `ds-agents-training` 指导。

## 固定流入口（不是工具）

斗狗场（`/ds-dashboard`）每狗一行的按钮：

| 按钮 | 桥 func | 说明 |
|---|---|---|
| ⚡ 分析 | `analyze` | 数据准备 → 读人设/记忆/资金 → LLM 决策 → 下单 |
| 🧾 结算 | `settle` | 只认完场比分结算并写角色文件 |
| 🧬 归纳 | `factor-induction` | 清洗/合并/补定义（alpha 跨狗逻辑引擎内判定） |
| 🪦 Review | `factor-review` | 退役评估（auto-dormant + 低信息退役 + LLM 结构性判断） |

狗列表由本地角色派生（`python-engine/src/role_registry.py`：`status=live` 才进全量默认），
与仓库提交内容无关；公开仓库零狗时按钮为空，创建狗（`/ds-dogs`）后自动生效。

## 回放（ds_replay / /ds-replay）

- 沙箱身份 `replays/sandboxes/<狗>_<MMDD>/`，幂等创建、可续跑；桥经 `role_root` 写沙箱 workspace，线上零影响；
- 每 `factor_review_every` 天暂停一次：卡片预填「下一轮方向建议」，用户编辑后作为
  `induction_notes` 注入下一周期退役评估；
- 转正/放弃 = `POST /ds-sandbox/<沙箱>/promote|abort`（dsh 文件原语，备份→整目录替换 / 删沙箱）。

## 缓存格式

盘上格式见 `docs/cache_format_spec.md`（canonical cache layout v1）。

## 引擎桥（本地 Python 引擎，随仓库发布）

`bridge.js` spawn python 桥（cwd=`engineRoot`），stdin 单行 JSON 请求、
stdout NDJSON 事件（progress/result/error），8 个 func 白名单双端校验；
API key（`DEEPSEEK_API_KEY` / `LOTA_API_KEY`）按 **环境变量 > `.env`
（`config.envFile` → `<engineRoot>/.env` → `~/.env`）> `~/.zshrc`（仅非 Windows 兜底）**
注入子进程 env。
协议细节见 `docs/bridge.md`。

## 注意

- `lota_match` 的 `odds`（Pinnacle 终盘）解析在 `odds.js`，是**数据专有**（依赖
  compact-fet 文本排版）。自定义数据源需改 `odds.js` 与 `python-engine/src/tools.py::extract_odds`。
- 工具注册用 DSH 的 `defineTool`；挂载后如需收紧 `output.schema`，用 `cordis_inspect` 核对。
