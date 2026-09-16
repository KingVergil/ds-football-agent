# 北单数据通道现状记录（2026-08-31）

> 背景：bc狗结算曾因开奖 SP 数据错乱而失败。经排查，问题不在 ds_agents 结算逻辑本身，而在**上游数据通道**。本文记录当前链路、踩到的具体问题与改进方向，供专职改进任务交接。

## 一、链路现状

```
spider(bjlot 抓取) ──> spdex DB(BeidanDraw) ──> deepseek_lota /beidan/sp ──> ds_agents(北单串关结算)
```

- 北单开奖结果写入 `spdex` 库的 `BeidanDraw`（表管理：`spider_monorepo/spdex/baseinfov2/models.py`）。
  - 新增/回填命令：`spider_monorepo/spdex/baseinfov2/management/commands/backfill_beidan_draw.py`。
- `deepseek_lota` 提供 `/predictions/api/v2/beidan/sp`（读 `BeidanDraw`），ds_agents 通过 `DataManager.fetch_beidan_sp` 查询。
- ds_agents bc狗结算：`_fetch_beidan_results` → `prepare_beidan_sp(sp_date)` → `get_cached_beidan_results`。其中 `sp_date = settle_day - 1`。

## 二、本次踩到的具体问题（均已被验证）

### 1. 开奖 result/spvalue 抓成脏值（最严重）
- **现象**：26089 期 193（桑普多利亚 vs 尤维斯塔比亚，意乙）库里 `result=3（胜）/ spvalue=1.82`；实际应为 `result=0（负）/ spvalue=10.894241`。
- **根因**：`backfill_beidan_draw.py` 的 `_parse_html` 在比赛**尚未"已开奖"**时抓取，把页面上 `tds[7]` 的**赛前赔率**（`value="1.82"`）当成了开奖 SP（`if sp_h>0 → result="3"`）。没有校验 `status=="已开奖"`。比赛结束后页面变成 `0 / 0 / 10.89`，但 DB 未重抓，脏值残留。
- **佐证**：修复后重新抓 `26089_08_29.html`，193 行 `tds[7]=0 / tds[8]=0 / tds[9]=10.894241` → 正确解析为 `result=0 / spvalue=10.894241`。

### 2. 开奖日期映射漂移（导致结算取不到数据）
- **现象**：26089 期(80-29 晚场)开奖数据，修复前挂在 `date=2026-08-30`，修复后归到 `date=2026-08-29`；26090 期(08-30 晚场) SP 则挂 `date=2026-08-30`。
- **正确口径（修复后已自洽）**：
  `/beidan/sp` 的 `date` 参数 = **足球日窗口起始日**（= 该期早场所在日历日）。
  ds_agents 结算的 `settle_day` = **窗口结束日（起始日 + 1）**，`sp_date = settle_day - 1` = 窗口起始日 = 足球日标签。
  即：26089 用 `settle 2026-08-30`(sp_date=08-29)；26090 用 `settle 2026-08-31`(sp_date=08-30)。
- **踩坑**：曾用 `settle 2026-08-30` 去结 26090——`sp_date=08-29` 取不到 26090 的 SP（它们在 08-30），导致只结了 26089。必须用窗口结束日 08-31 才能结 26090。**日期口径必须统一，否则结算静默为 0/漏结。**

### 3. 旧 JS 端点已下线
- `/data/200/draw/<year>/<issue>.js`（旧接口）现已 **404**，只剩 HTML `/ssm/200/html/<期号>_<MM_DD>.html`。回填只能依赖 HTML，而 HTML 列位/结构对 `_parse_html` 的 `tds[i]` 索引敏感。

### 4. HTML 解析对列位/属性敏感
- `_sp_value(td)` 用正则 `value="([\d.]+)"` 取第一个 value；`_parse_html` 硬编码 `tds[7]/[8]/[9]` 为胜/平/负 SP。若页面出现非 SP 的 `value` 属性或列位变化，易错读。

### 5. 本地缓存比赛状态不刷新
- 与北单无关的另一个通道问题：已开赛的比赛在 ds_agents 缓存里常停在校  `state=-1（未开）` / `score=None`（如 08-31 凌晨的那不勒斯 00:30），开赛/滚球/完场状态不更新。导致分析/邮件用了过期状态、甚至对已开赛场次下单。
- **补充**：存在**两条读路径不一致**——`data/matches/*.json` 里的场次实际已是 `state=6 完场`（有比分），但 `tools.lookup_match(lid)` 读出来却是 `state=0 / score=None`。排查/修复时要注意"看哪个文件"结论可能不同。

## 三、改进方向（供专职任务）

1. **只在"已开奖"后回填**：`_parse_html` 增加 `status`（tds[1]）门控，非"已开奖"的行不产出 result/spvalue；对已开奖场次定期重抓覆盖，避免把未开奖页面的赔率当 SP。
2. **统一开奖日期口径**：明确北单开奖数据归属日期（按开奖日/期号），并在 ds_agents 结算的 `sp_date` 推导与上游 `/beidan/sp` 的 `date` 语义之间建立显式映射，避免错位。
3. **HTML 解析健壮化**：弃用"取第一个 value"的启发式，改为按稳定 CSS/列标题定位胜/平/负 SP；或恢复/新增稳定的结构化接口。
4. **监控脏值**：加一道校验，检测 `BeidanDraw.result` 与 `score + handicap` 推导方向不一致的场次并告警（本次 193 即此类）。
5. **比赛状态时效**：ds_agents 侧对已开赛/临近开赛的场次，开赛后可续刷 state/score，避免停留"未开"。

## 四、本次影响与处理

- **两段已闭环**：
  - 足球日 29（26089）：`settle 2026-08-30` 结 3 张票（slip_87c07 -486、slip_a1c24 -486、slip_2f704 +2588），已归纳（合并 6 组）。
  - 足球日 30（26090）：`settle 2026-08-31` 结 2 张票（slip_69a1c、slip_a318b，均 -486，合计 -972），已归纳（合并 3 组）。
- bc狗 capital：**→ 5644.31**。
- 上游修复由 spider 端负责；ds_agents 只依赖 `/beidan/sp` 结果，上游修正后直接生效。

## 五、2026-09-16 复核：`date=` 口径漂移（已修）+ 脏值误判（已修）

94狗 转正后第一次真实结算（足球日 09-15，live）暴露两个引擎侧 bug：

1. **`/beidan/sp?date=D` 的窗口口径依赖服务端当前时间**（见 `deepseek_lota/predictions/views/api_v2/beidan_draw_api.py`）：
   `hour>=12 → [D 12:01, D+1 12:00]`，`hour<12 → [D-1 12:01, D 12:00]`。
   引擎传的是**足球日标签**，于是 2026-09-16 10:55 用 `date=2026-09-15` 拉到的是
   **足球日 09-14** 的 40 条开奖（实测 `draw_datetime` 全是 09-15）。
   - 修复：`fetch_beidan_sp(sp_date)` 改用**显式时间窗** `start_date/end_date`（`beidan_day_window`），
     中午前后跑结算都锁定同一个足球日；`merge_beidan_sp(sp_map, day=D)` 增加窗口门，
     越界场次不写入任何缓存文件。
   - 数据修：错落成 `beidan_sp/2026-09-15.json` 的那 40 条已改名为 `2026-09-14.json` 并重新合并。

2. **脏值校验把「让球线缺失」当让球 0**：`_beidan_result_suspect` 对缓存的空
   `beidan_info`（无 goal_line）也做推导比对，导致 9 场正常开奖（如中国香港女足 1:5 中国女足，
   北单主队受让 4 → 官方「平」）被标 `result_suspect`，直接卡住结算。
   - 修复：`goal_line` 缺失时直接放行；成功合并后清掉历史 `result_suspect` 标记。

3. **足球日 09-15（期 26095）的开奖 SP 上游尚未入库**：窗口内最后一场
   （普埃布拉 vs 托卢卡，09-16 09:00 开球）刚结束，`/beidan/sp` 用 `lota_id` 单场查也是空。
   接口本身正常（同一时刻用已知老场次 `lota_id=Lota4594092` 能查到 result/spvalue）。
   比分已可从 `/matches` 取到，9 条腿推导命中 5/9（C(5,4)=5 注中），与用户实际兑奖一致；
   但派彩必须用开奖 SP，所以结算只能等上游发布。
