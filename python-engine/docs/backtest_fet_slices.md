# 回测取数：北单 fet_txt 时间切片源

> 模块：`python-engine/src/backtest_fet.py`（新建）· 挂钩：`data_manager.py` / `beidan_parlay_dog.py`
> 切片范围：bc狗 北单 2026-07-01 ~ 2026-09-08（2593 场，可重同步/扩范围）

## 1. 问题：回放读 `live/` = 前视泄漏

回放（沙箱模型）跑历史足球日时，`DataManager` 只走线上 `compact-fet`。
线上接口 `fet_from_disk()` 的目录优先级是：

```
live → pass_2_hours → pass_3_hours → pass_6_hours → pass_12_hours → pass_1_day
```

`live/` 是**开赛前持续更新的终盘快照**（≈赛前 15 分钟）。于是回放里「17:00 那一波分析」
拿到的其实是赛前 15 分钟的数据——A、B 两场都按赛前终盘算，而实盘在 17:00 时：

| 场次 | 开赛 | 距访问时刻 | 实盘实得数据档 |
|---|---|---|---|
| A | 20:30 | 3.5h | `pass_6_hours`（赛前 6h 档） |
| B | 次日 03:00 | 10h | `pass_12_hours`（赛前 12h 档） |

线上生成规则（`online/utils.check_time_diff` + `auto_water_level`）：
每场**每个阶段只生成一次**，`pass_6_hours/<lid>.txt` 的文件时间 ≈ 开赛前 6h、
`pass_12_hours` ≈ 12h、`pass_1_day` ≈ 24h（2026-08 实测中位数 5.96 / 11.96 / 23.9h）。
切片内容与线上 compact-fet 是**同一份文本**（线上就是直接读这些文件），
所以下游 `compact_fet_to_tags` / `extract_odds` 一行不用改。

## 2. 口径：访问时刻 → 档位

```
gap = 开赛时刻 − 访问时刻
gap ≤ 6h   → pass_6_hours
gap ≤ 12h  → pass_12_hours
gap ≤ 24h  → pass_1_day
gap ≤ 0    → 无数据（该波访问时已开赛，实盘也不会拿它选腿）
gap > 24h  → 无数据（那时线上还没生成任何档）
```

**固定习惯波次**（北京时间，按足球日起始日 D 的星期）：

| 足球日起始日 | 波次 |
|---|---|
| 周六 / 周日 | 16:30、20:30（两波，各出一张票） |
| 周一 ~ 周五 | 22:30（一波） |

回放在一个足球日内**逐波跑**：每波只分析「该波时刻尚未开赛」的场次。
同一场晚场在两个波次里可能落到不同档（16:30→`pass_12_hours`，20:30→`pass_6_hours`），
这正是实盘两波各自看到的数据。

覆盖度（2026-07-01~09-08，2593 场北单）：

| 波次 | pass_6_hours | pass_12_hours | pass_1_day | 无数据 |
|---|---|---|---|---|
| 16:30（周末首波） | 1505 | 797 | 223 | 68 |
| 22:30（工作日） | 764 | 231 | 20 | 60 |

「无数据」= 开赛早于当天第一波（工作日 22:30 前的早场、周末 16:30 前），这类场次不进分析。

### 回退规则：只往回旧，绝不往前新

某档切片缺失时，按 `pass_6_hours → pass_12_hours → pass_1_day` **只能回退到更旧的档**
（信息更少，不会前视）；目标档没有更旧的可用档 → 该场判为**无数据**，不进分析，
**绝不回退到线上实时缓存**（那是赛前终盘 = 前视）。
`allow_newer_fallback=True` 只在显式要求时开启，默认关闭。

## 3. 生效范围（严格按索引）

范围写在切片根目录的 `.bc_backtest_index.json`：`{lid: {kickoff, stages}}`（2593 场）。

- **在索引里** → 本模块是该场**唯一**数据源：`get_cached_compact_fet()` / `_load_cached_tags()`
  都改读切片（tags 由切片文本即时切分，且**不落盘**、不污染线上 `data/tags`）。
- **不在索引里**（竞彩、其它日期）→ 返回 None，`DataManager` 按原逻辑读缓存/线上，行为完全不变。

启用条件（`src/backtest_fet.py::_maybe_auto_enable`）：

| 环境 | 行为 |
|---|---|
| 沙箱回放（桥设置 `DS_ROLES_ROOT`） | **自动启用**（线上分析/prefetch 不受影响） |
| `DS_BACKTEST_FET=0/off/false/no` | 强制关闭 |
| `DS_BACKTEST_FET=1/on/true/yes` | 强制开启（手动离线脚本用） |
| 线上（无 `DS_ROLES_ROOT`） | 不启用 |

其它环境变量：`DS_FET_TXT_ROOT`（切片根，默认同级 `deepseek_lota/data/runtime/fet_txt`）、
`DS_BACKTEST_FET_ROOT`、`DS_BACKTEST_WAVES` / `DS_BACKTEST_WAVES_WEEKDAY` / `_WEEKEND`（覆盖波次）。

## 4. 顺手修掉的一个坑：回测不联网刷比赛缓存

`_beidan_matches()` 在回测/离线时**只读本地比赛缓存**，不再走 `prepare_matches()` 的联网刷新。
原因：线上 `/matches?date=D` 的 `date` 是「足球日结束日」口径（返回 D−1 的窗），
回测中对 D 刷新会把这个足球日的比赛列表整窗刷错位（2026-09-11 实测踩到，
`data/matches/2026-08-22.json` 被刷成 08-21 的窗，已用 range 口径恢复）。
`src/beidan_parlay_dog.py::_beidan_matches` 里保留了 `is_offline() or backtest_fet.active()` 判断。

## 5. 重同步 / 扩范围

```bash
# 1) 拉切片（只读线上：清单经 /tmp，打包到 /tmp，不碰线上服务）
python-engine/scripts/sync_beidan_fet_slices.sh 2026-07-01 2026-09-08
#    内部：收集北单 lid → scp 清单 → 线上 tar → 拉回解包 → 重建索引

# 2) 抽查某日各波取数档位
python3 -m src.backtest_fet check  --day 2026-08-22
python3 -m src.backtest_fet resolve --lid Lota4555190 --at 2026-08-22T16:30

# 3) 只重建索引（切片已就位时）
python3 -m src.backtest_fet index --start 2026-07-01 --end 2026-09-08
```

切片清单（本次已同步，位于 `deepseek_lota/data/runtime/fet_txt/`）：

| 阶段 | 文件数（北单范围） |
|---|---|
| `pass_6_hours` | 2547 / 2593 |
| `pass_12_hours` | 2547 / 2593 |
| `pass_1_day` | 2541 / 2593 |

46 场线上就没有切片（`fet` 未生成），它们在索引里 `stages=[]` → 回测判无数据，不进分析。

## 6. 已知限制

1. **反思链路的历史同信号回顾**仍读线上 `data/tags/*.json`（`slug_history.py` 直接扫目录），
   那批 tags 是 live 终盘切出来的 → 反思的历史样本统计仍带前视。本次只接管了
   分析链路的取数（compact-fet / sections / odds）。
2. `data/matches/*.json`（比赛列表、北单盘口）仍是共享缓存，回测只读不写；
   但别的狗/prefetch 若在回测期间联网刷新，会改动这批文件（不属于本次范围）。
3. 波次是**固定习惯表**，与实盘手工点击的随机时刻不完全一致（实盘 9/2 16:42、9/9 18:01 等）；
   回测口径以固定表为准。
4. `live` / `last`（赛前 0~15 分钟）两个档没有同步：回测永远拿不到"最后一刻"的盘口，
   这是刻意的（那正是实盘 17:00 拿不到的东西）。

## 7. 测试

```bash
python3 -m pytest tests/test_backtest_fet.py -q     # 切片源单测 + 逐波 prompt 端到端
```

覆盖：gap→档位边界、波次表（工作日/周末）、只回退更旧档、索引外不受影响、
DataManager 接管与不落盘、周末两波 prompt 分别来自 `pass_12_hours` / `pass_6_hours`
且不含线上缓存文本。
