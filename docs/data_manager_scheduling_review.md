# DataManager 数据访问与调度盘点

> 日期：2026-08-29 · 范围：单狗需求 → 历史补全 → 多狗调度 → 北单狗差异

## 1. 背景与结论

`python-engine/src/data_manager.py` 是统一数据访问层，多狗以独立进程运行，
进程内单例管不到跨进程并发。已新增跨进程调度层（文件锁单飞 + 共享状态文件
`python-engine/data/.dm_state.json`），但接入尚不完整。

核心结论：

1. **分析 / 结算 / 反思** 三链路真正访问 DataManager 的比赛类数据；**归纳、review**
   完全不碰 DataManager（只读角色因子/反思记忆文件）。
2. 反思/归纳/review 隐藏了**历史数据补全**请求：它们的输入（归因、历史同信号回顾、
   因子统计窗口）依赖历史 matches/features/tags 齐全，数据缺失时**静默降级**。
3. 多狗视角加入**北单狗**后问题面变化：共享比赛行、live 刷新绕过新鲜窗口、
   开奖 SP 成为新数据面、存在第二套 prefetch、窗口键控口径多一层。

## 2. 单狗数据访问需求

### 2.1 分析 analyze

入口 `Agent.analyze` → 图 `build_analyze_graph`（`src/agent.py`）。

| 阶段 | DataManager 调用 | 数据层 | 读/写 |
|---|---|---|---|
| fetch_matches | `get_cached_matches` → 缺失/live 时 `prepare_matches` | 比赛列表（按日历日，all 类型） | 读 + 写缓存 |
| fetch_features | `prepare_features` | compact-fet（按场） | 读 + 写 features 缓存 |
| check | `check_data_freshness` | 特征缓存时间戳 + 比赛 state | 只读 |
| build_prompt | `get_compact_fet`、`get_match_context`、`get_sections`、`get_odds` | compact-fet、tags/sections、赔率、预测、订单 | 读（tags 缺失时写） |
| parse_orders | `get_blacklist`、`get_odds` | 黑名单、Pinnacle 赔率 | 只读 |
| place_orders | `get_cached_match`、`get_cached_compact_fet` | 比赛信息 + fet 尾点 | 只读 |

需求要点：比赛列表（含 `jc_hhad`/`beidan_info` 按需）、每场 compact-fet、
按因子 slugs 切的 sections、终盘赔率、黑名单。**新鲜度最敏感**：live 模式要求
未开赛场次赔率 15 分钟内（`COMPACT_FET_CACHE_TTL`），比赛缓存 5 分钟内
（`MATCHES_CACHE_MAX_AGE`）。

### 2.2 结算 settle

入口 `Agent.settle` → 图 `build_settle_graph`（`src/agent.py`）：
load_unsettled → fetch_scores → settle_orders → **reflect**（结算图自带反思）。

| 阶段 | DataManager 调用 | 数据层 | 读/写 |
|---|---|---|---|
| load_unsettled | `get_cached_match` | 比赛时间标注 | 只读 |
| fetch_scores | `check_data_freshness`、`get_cached_matches`、`prepare_matches(live=True)`、`refresh_score_match`、`get_cached_compact_fet` | 比分/state、比赛列表、compact-fet | 读 + 写 matches/features 缓存 |
| settle_orders | `get_cached_match` | 比赛名展示 | 只读 |

需求要点：**只关心已完场（state==6）的权威比分**，其余数据透传给反思。
写操作集中在 `refresh_score_match`（比分回写 matches + features 两份缓存，已加
`score:{lota_id}` 锁）。

### 2.3 结算 + 反思（settle 图 reflect 节点）

`node_reflect` → `run_reflect`（`src/agent.py`）：

- `get_match_context(lid)`（每笔已结算订单 + 补充样本）→ 内部再调 `get_odds`→
  `get_compact_fet`、`get_predictions`、`get_orders`、`_tags_summary`→`get_tags`
- `get_sections(lid, reflect_slugs)` → tags 缓存
- `_extra_reflect_matches` → `get_cached_matches`（当天窗口完场未下单样本）
- 历史同信号回顾（`slug_history.py`）→ 扫描历史 matches + features + tags 缓存

需求要点：**纯本地读取型**——完场比赛 compact-fet 永久有效（`_is_match_finished`），
反思基本不打线上；需要 tags/sections、比分、历史缓存齐全。

### 2.4 因子归纳 factor-induction

`src/factor_induction.py` **完全不 import DataManager**。直接读 `ROLES_DIR`
（角色 JSON）、factors JSON、reflection_memory.json，写因子去重/合并结果。
对比赛类数据零需求，但依赖反思记录质量（见 §3）。

### 2.5 因子 review

图 `build_factor_review_graph`（`src/agent.py`）：load_role → factor_review。
`node_factor_review` 只读角色的 `memory.factors`（表现统计）、
`_get_recent_reflections`（反思文件）、persona；写因子 status（dormant/retired）。
**对 DataManager 零访问**。

### 2.6 数据层汇总

| 数据层 | 分析 | 结算 | 反思 | 归纳 | review |
|---|---|---|---|---|---|
| 比赛列表（matches/date） | ✅ 读写 | ✅ 读+补缺写 | ✅ 读 | — | — |
| compact-fet | ✅ 读写 | ✅ 读 | ✅ 读 | — | — |
| tags/sections | ✅ 读（缺失写） | — | ✅ 读 | — | — |
| 赔率 odds | ✅ 读 | — | ✅（经 context） | — | — |
| 比分/state | 剥离 | ✅ 读写 | ✅ 读 | — | — |
| 黑名单 | ✅ 读 | — | — | — | — |
| 预测/订单 | ✅（context） | 读 role 订单 | ✅（context） | — | — |
| 角色因子/反思记忆 | 读人设 | — | 读+写 | 读+写 | 读+写 |

## 3. 隐藏的历史数据补全需求

### 3.1 依赖链

```
历史 matches/features/tags 齐全
        ↓ (缺失则静默降级)
反思 run_reflect（当日归因 + slug_history 90天回顾）
        ↓
reflection_memory.json（归因因子 + 样本量）
        ↓
factor_perf（total/hit/profit/last_seen/history）
        ↓
归纳（去重合并） / review（退役判断）
```

### 3.2 各链路缺口

- **反思**：已结算订单的 compact-fet/tags 若未缓存过，`get_sections` 返回空但
  header 照常进 prompt，无报错；`slug_history.py` 的 `SlugHistoryIndex` 扫描最近
  90 天 `tags/*.json` + `matches/*.json` + `features/*.json`，哪天缺缓存就少返回
  几场——**均静默降级**。
- **归纳**：读 `reflection_memory.json`（`find_slugs_in_reflections`、样本量、
  history）。历史反思若因缺数据写成空/低样本，归纳是垃圾进垃圾出。
- **review**：`factor_perf` 统计由反思归因累积；`_get_recent_reflections` 扫评估
  窗口的反思记录。窗口内缺反思日 = 统计盲区，候选列表照常生成，不提示缺口。

### 3.3 两层补全（补数据 ≠ 补反思）

- **Tier 1 数据层**（DataManager 的活）：保证窗口内 matches/features/tags 齐全。
  现有 `prepare_range` 已能做逐日单飞 + 就绪报告，但反思/归纳/review 入口均未调用。
- **Tier 2 反思覆盖层**（引擎流程的活）：窗口内「有结算订单但没有反思记录」的
  足球日需重跑 reflect。DataManager 只能提供缺口报告，不能代跑。

提议：新增 `ensure_history(start, end)`（复用 `prepare_range`，返回逐日完整性报告，
complete 阈值如 features/tags 覆盖率 ≥ 90%）与 `reflection_coverage(start, end)`
（对比各狗 reflection_memory 与结算日，暴露 Tier 2 缺口）。

## 4. 多狗调度视角

现状编排（斗狗场 / `python-engine`，`batch_agents.sh` 已停用）：

- `analyze`：先 `prefetch <day> --jingcai`（已走 `prepare_day` 调度器）→ 并行
  跑注册表 live 狗（`--prefetched --jingcai`）→ 串关2狗。
- `settle`：串行逐狗结算 + 串关2狗；结算图自带反思。
- 注册表 live 列表实际包含：7 只竞彩狗 + 深度足球狗 + 梭哈北单狗 + 跟风北单狗 +
  bc狗（另有串关2狗走独立入口）。

调度器已覆盖：分析取比赛（`prepare_matches` 日期锁 + 5 分钟新鲜窗口）、
compact-fet 预取（`prepare_features` 按场锁 + TTL）、结算日期补缺与逐场比分
（`score:{lota_id}` 锁）。

残余盲区：

- prompt/反思阶段 compact-fet 恰好 TTL 过期时，`get_compact_fet`/`get_odds`
  会无锁打线上（prefetch 后基本缓存命中，概率低但存在）。
- 北单狗整个数据链路未接入调度器（见 §5）。

## 5. 北单狗差异

北单狗（`src/beidan_parlay_dog.py`，`python -m src.beidan_parlay_dog`）有独立
入口与数据流程，不经过 batch 的 generic CLI。

### 5.1 共享比赛行（两池共存）

同一场比赛可能同时带 `jingcai_number` 与 `beidan_number`（实测 8 月下旬缓存中
150 行共存）。竞彩狗维护 `jc_hhad`、北单狗维护 `beidan_info`，写入同一份
`matches/<date>.json`，必须靠 `_merge_preserved_odds` 互相保留。

现状：竞彩狗走 `prepare_matches`（有锁），北单狗走自己的 `_beidan_matches` /
`_prepare_beidan_data`（**直接调原始 `refresh_matches_cache`，无锁**）。同日期并发
时是「有锁 vs 无锁」抢写同一文件，last-writer-wins 存在丢字段窗口。

### 5.2 live 刷新绕过新鲜窗口

`_beidan_matches` 在 live 模式无条件 `refresh_matches_cache(cd,
with_beidan_odds=True)`，不管缓存是否刚刷过。与 `MATCHES_CACHE_MAX_AGE`（5 分钟
本地窗口）理念冲突，多狗并发时重复打线上。

### 5.3 开奖 SP：新数据面

北单结算不靠 `state==6 + score`，而是 `_fetch_beidan_results` →
`fetch_beidan_sp` + `merge_beidan_sp`，把开奖 result/spvalue 合并进本地缓存再判
串关。这三个方法（含 `refresh_beidan_cache`）**均无锁**。多只北单狗并发结算时，
同一足球日 `/beidan/sp` 重复拉取，`merge_beidan_sp` 并发写 `matches/` 与
`beidan/` 两份缓存。

提议：新增 `prepare_beidan_sp(sp_date)`，按 `beidan-sp:{sp_date}` 单飞 + 状态记录。

### 5.4 第二套 prefetch

竞彩狗有 `prefetch --jingcai`（走 `prepare_day`）；北单狗没有 prefetch 阶段，
只有 `_prepare_beidan_data` 在 live 拉不到比赛时兜底触发，且逐场 `sleep(0.3)`
串行拉 compact-fet + tags。两套流程互不知道对方；两池共存的场次可能被重复拉取
同一场 compact-fet（features 锁只保护走 `prepare_features` 的路径）。

### 5.5 窗口键控口径

- 竞彩狗 analyze：足球日起始日，比赛按日历日缓存。
- 北单狗 analyze：足球日，只认 `beidan_info` 齐全的场次。
- 北单狗 settle：day_date 是窗口**结束日**，`_fetch_beidan_results` 内部
  `-1 天` 换算成 SP 的足球日起始日。

调度状态需三类键：`matches:{cd}`、`features:{lota_id}`、`beidan-sp:{sp_date}`；
「北单数据准备完成」判定 ≠ 竞彩判定，需校验窗口内北单场次 100% 带 `beidan_info`
（含开奖字段）。

### 5.6 北单接入方案

1. `_beidan_matches` / `_prepare_beidan_data` 改走 `prepare_matches(cd,
   with_beidan_odds=True)` + `prepare_features`，吃掉日期锁、新鲜窗口与按场单飞。
2. `fetch_beidan_sp` + `merge_beidan_sp` 包进 `prepare_beidan_sp(sp_date)` 单飞。
3. `prepare_day` / `prepare_range` 增加北单完整性判定（beidan 模式要求候选场次
   100% 带 `beidan_info`）。
4. 多狗编排把北单狗纳入统一 prefetch 阶段（`prepare_day --beidan`），两池共存场次
   compact-fet 只拉一次，避免分析 0 场兜底触发慢速串行准备。

## 6. 待办清单

- [ ] 北单狗接入 `prepare_matches` / `prepare_features`（§5.6-1）
- [ ] `prepare_beidan_sp` 单飞 + 状态记录（§5.6-2）
- [ ] `prepare_day` / `prepare_range` 北单完整性判定（§5.6-3）
- [ ] batch 编排统一北单 prefetch（§5.6-4）
- [ ] `ensure_history` + `reflection_coverage`（§3.3），接入反思/归纳/review 入口
- [ ] 封堵 prompt/反思阶段 `get_compact_fet` 的无锁线上出口（§4 残余盲区）
