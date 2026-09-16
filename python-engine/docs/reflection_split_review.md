# 改造 review：人设分离 / 结算-反思解耦 / 反思输入补全（2026-09-13）

> 对应工单 4 条：① 人设分离；② 结算与因子产生解耦；③ 反思输入补 比分/goal_line；
> ④ 不得影响单关狗。**本文只列改动与验证方式，便于逐条 review。**

---

## ① 人设分离（下注人设 / 反思人设）

**改动**：`src/role.py::persona_text(mode="bet")`

```python
def persona_text(self, mode: str = "bet") -> str:
    if mode == "reflect":
        rp = self._role_dir / "persona_reflect.md"       # 新增可选文件
        if rp.exists():
            text = rp.read_text(...).strip()
            if text:
                return "## 🪞 反思人设\n\n" + text
        # 没写 → 回落 persona.md（旧行为）
    if self._persona_path.exists():                      # 原逻辑原样保留
        ...
        return "## 🎯 个人偏好\n\n" + text
```

**注入点改动（仅 1 处）**：`src/agent.py:1311`（在 `run_reflect` 内）

```python
- persona_text = role.persona_text() if role else ""
+ persona_text = role.persona_text(mode="reflect") if role else ""
```

**其余调用点全部未动**（仍是默认 `mode="bet"`）：

| 位置 | 用途 |
|---|---|
| `src/agent.py:548` | 单狗分析 prompt（下注） |
| `src/agent.py:1943` | 因子退役评估 prompt |
| `src/beidan_parlay_dog.py:651` | 北单 stage1 分析 prompt（下注） |
| `src/chuan_guan_dog.py:621` | 竞彩串关分析 prompt（下注） |

**单狗安全性**：没写 `persona_reflect.md` 的角色，`reflect` 模式与 `bet` 模式返回**同一字符串**
（实测：`95狗` 395 字==395 字、`梭哈2狗` 365 字==365 字）。

**怎么用**：给狗目录加一个 `persona_reflect.md` 即生效（只需写反思视角，不用重写下注人设）。

---

## ② 结算订单 与 因子产生 解耦

**改动 1**：`src/beidan_parlay_dog.py` 新增两个显式入口（第 3153 行起）

```python
def settle_only(self, day_date=None) -> dict:      # 只对账订单/资金，不产因子
    return self.settle(day_date, reflect=False)

def reflect_only(self, day_date=None, include_skipped=True) -> dict:
    settled = [已结算订单]
    有已结算 → _reflect_settled(真实样本)
    无 + include_skipped → _reflect_skipped(观察样本，带虚拟结算)
    返回 {mode, settled_orders, factors_before/after/delta, reflections}
```

> `settle(reflect=...)` **原语义不变**（既有调用方与单狗不受影响）。

**改动 2**：`src/bridge.py` 暴露为可编排的两步

| 调用 | 行为 |
|---|---|
| `settle` + `opts.stage="settle"` | 只结算（`settle_only`） |
| `settle` + `opts.stage="reflect"` | 只产因子（`reflect_only`，返回 `reflect` 统计） |
| `settle`（缺省 / `stage="both"`） | 结算 + 反思（**改造前行为**） |
| **新增 `func="reflect"`** | 独立"只产因子"入口（非串关狗**显式报错**） |

```bash
# 只结算
{"func":"settle","dog":"95狗","day":"2026-09-14","opts":{"stage":"settle"}}
# 只产因子
{"func":"reflect","dog":"95狗","day":"2026-09-14","opts":{}}
```

**改动 3（2026-09-14 补）**：斗狗场 UI 落地 —— 否则解耦"引擎做到了但点不到"
（`🧾 结算` 固定发 `opts: {}` = both，而反思才烧 LLM）。

| 按钮 | 串关狗 | 单关狗 |
|---|---|---|
| ⚡ 分析 / 🧾 结算（both）/ 🪦 Review / 📊 状态 | ✅ | ✅ |
| 🧾 **只结算**（`opts.stage="settle"`） | ✅ 新增 | ❌ **不加** |
| 🧬 **只产因子**（`func="reflect"`） | ✅ 新增 | ❌ **不加** |

- 纯逻辑 `harness-plugin/lobby.js::stageActionsFor(dog, day)`（可单测），
  `client.js` 按仓库既有做法内联同一口径（bundle 只能 `require("react")`，无法 import 兄弟模块）；
- 顺手把 `client.js` 里重复两遍的串关判定抽成模块级 `isParlayDog(d)`，
  Dashboard 内的 `lobbyOf` 改为调它（逻辑逐字等价，去重）；
- ⚠️ **需重启 DSH 才生效**（插件在会话启动时 import）。本次**未重启**，避免打断会话。

桥级 round-trip 实测（沙箱 95狗，打桩 LLM 以防烧钱）：

```
起点：因子 131｜资金 5000｜订单 1
stage=settle  → stage=settle 结算单=0｜因子 131→131（不变）｜资金 5000｜LLM调用=0
func=reflect  → mode=settled 已结算单=1｜因子 131→131｜资金 5000（不变）｜LLM调用=1
人设分离      → 反思prompt含「反思人设标记REFLECT_ONLY」✅｜下注人设(395字)不含 ✅
```

---

## ③ 反思输入补 比分 / goal_line（真 bug）——**全 opt-in，单狗路径不变**

**现象（用户报障）**：反思输入里比赛片段显示 `比分:?`，且**没有让球线**。

**根因**：
- `_extract_match_info()` 从 **compact-fet 文本**解析 —— 回放用的是 **fet_txt 赛前切片**
  （`pass_6_hours` 等），里面**没有比分**；
- 兜底只查 features / 当前日期桶的 matches，而回放没写 features；
- **`goal_line` 此前根本没被提取**；
- 真实赛果其实就在 `data/beidan/<日>.json` 与 `matches/<日>.json` 的 `beidan_info` 里。

### ⚠️ 口径变更（2026-09-14 用户追加约束）

> 「单狗先不涉及这次的更改，当前更改应当是**沙盒内**，不干扰主要项目。」

原因：`run_reflect` 与 `get_match_context` 是**单狗线上同一条路**，直接铺开会改到单狗
反思 prompt。故全部收敛为**显式 opt-in，默认关闭**：

| 开关 | 默认 | 由谁打开 |
|---|---|---|
| `DataManager.get_match_context(rich=)` | `False` | `run_reflect(rich_match_info=)` |
| `run_reflect(rich_match_info=)` | `False` | `reflect_extra.rich_match_info` |
| `run_reflect(extra_only_beidan=)` | `False` | `reflect_extra.extra_only_beidan` |
| `_extra_reflect_matches(dedup=, only_beidan=)` | `False` | 同上 |

`src/beidan_parlay_dog.py` 的 **4 处** `reflect_extra`（方向/波动 × 两处调用）显式传
`"rich_match_info": True, "extra_only_beidan": True`。**其余调用方一律不传 → 行为同改造前。**

**改动 1**：`src/data_manager.py::_extract_match_info(rich=False)` —— 仅在 `rich=True` 时回填

```python
goal_line = None
if rich:                       # ← 默认 False，单狗/分析 prompt 走不到这里
    if not score:              # 比分优先用 beidan/matches 缓存补（只补缺，不覆盖）
        bi = get_cached_beidan_results({lota_id})[lota_id]
        score = bi["score"]; goal_line = bi["goal_line"]
    if goal_line is None:      # 让球线回退 get_cached_match 的 beidan_info
        ...
return {..., "score": score, "goal_line": goal_line}
```

> **无后视风险**：`prompt_builder.py`（下单/分析 prompt）只读 `home/away/league/match_time`，
> **从不读 `score`** —— 已加测试 `test_analysis_prompt_builder_never_reads_score` 钉死。

**改动 2**：`src/agent.py::run_reflect` 把让球线写进两处 header（已结算订单 + 补充样本），
`gl_txt` / `_gl_txt` 的**计算整段包在 `if rich_match_info:` 里**。

```
默认（单狗/改造前）: ### lota_id=Lota4469357 | 兰斯科罗纳 vs IFK瓦纳默 | 比分=?
串关（rich=True）  : ### lota_id=Lota4469357 | 兰斯科罗纳 vs IFK瓦纳默 | 比分=1:0 | 让球让1
```

### 顺带修掉的 3 个反思输入缺陷（同样只在 opt-in 分支生效）

排查时实测暴露的问题（都发生在**共享路径**上，故一律只修在 opt-in 分支，单狗保持原样）：

| # | 缺陷 | 证据 | 修法 |
|---|---|---|---|
| 1 | **同一场被反思两遍** | 足球日窗口跨两个日历日，`candidates` 里同一 `lota_id` 出现 2 次；e2e 实测 `Lota4459720` 重复 | `dedup=True` 按 `lota_id` 去重 |
| 2 | **`比分=` 空白**（比 `?` 更糟） | `state==6` 但赛果缓存 `score=None`（脏数据，已知 07-01/07/08/14 等） | rich 模式下回落 `?` |
| 3 | **口径混入**：北单狗的补充样本按 `lottery_type="all"` 抽 | 08-16 候选 263 场里**非北单 117 场**（45%）；07-12 65 场里非北单 44 场（68%） | `only_beidan=True` 只留 `beidan_number` 非空（与 `agent.py:232` 同一判别口径） |

> 缺陷 3 与你的边界要求直接相关：北单是**彩池**玩法，混入竞彩固定赔率场次做因子归纳
> 等于两套口径搅在一起。

**端到端实测**（沙箱 95狗，假 provider 抓真实 prompt）：

```
### lota_id=Lota4469357 | 兰斯科罗纳 vs IFK瓦纳默 | 比分=1:0 | 让球让1
### lota_id=Lota4459718 | 德国 vs 巴拉圭            | 比分=1:1（未下单，仅供参考）
### lota_id=Lota4459719 | 荷兰 vs 摩洛哥            | 比分=? | 让球0(平手)（未下单，仅供参考）
### lota_id=Lota4459720 | 巴西 vs 日本              | 比分=? | 让球让1（未下单，仅供参考）
```

---

## ④ 单狗红线

### 4.1 资产零改动（文件系统实证）

`python-engine/data/` 是 **gitignore 的**（`.gitignore:2`），所以 `git status` 不能作证据；
改用 mtime 实证：

```
$ find python-engine/data/roles -type f -newermt '2026-09-14 11:30'   # 本轮跑测试的窗口
  → 0 个
梭哈2狗 0 ｜ 平局狗 0 ｜ 跟风狗 0 ｜ alpha狗 0 ｜ 95狗 0 ｜ bc狗 0 ｜ bcl狗 0
```

（单狗最后写入 09-14 10:57–10:58，是斗狗场自己的「归纳/退役」周期：四狗同时写 +
`history/2026-09-13__pre-factor/` 快照，与本轮改动无关。）

### 4.2 `tests/test_persona_reflect_split.py`（**13 条**）

| 测试 | 守什么 |
|---|---|
| `test_default_persona_unchanged_without_reflect_file` | 无反思人设时 reflect == bet（行为不变） |
| `test_existing_persona_untouched_by_reflect_mode` | reflect 模式**不修改** `persona.md` |
| `test_reflect_persona_only_when_file_exists` | 写了才生效，且只影响 reflect |
| `test_single_dog_has_no_reflect_persona_by_default` | 线上单狗无 `persona_reflect.md` |
| `test_reflect_func_rejects_non_parlay_dog` | 独立 reflect 入口拒绝单狗 |
| `test_settle_stage_default_is_both` | 桥 stage 缺省 = both（旧语义） |
| `test_reflection_header_includes_goal_line` | 两处 header 都带让球的代码路径存在 |
| **`test_single_dog_reflect_prompt_untouched_by_default`** | **真跑 run_reflect**：默认片段仍 `比分=?` 且**无让球线** |
| **`test_parlay_rich_reflect_prompt_includes_score_and_goal_line`** | 串关链路带真实比分 + 让球线 |
| **`test_match_info_backfill_is_opt_in`** | `rich=False` 时 `score` 不变、`goal_line is None` |
| **`test_analysis_prompt_builder_never_reads_score`** | 分析 prompt 不读赛后比分（后视红线） |
| **`test_extra_reflect_dedup_is_opt_in`** | 默认允许重复（旧行为）；dedup 后互不相同 |
| **`test_extra_reflect_only_beidan_is_opt_in`** | 北单狗补充样本不混入竞彩 |

前 6 条为行为/子进程隔离测试；后 4 条**真跑 `run_reflect`**（子进程 + 假 provider 抓 prompt），
而非源码字符串 grep。

---

## 回归

| 侧 | 结果 | 命令 |
|---|---|---|
| 引擎 | **183 passed**（整包全绿；此前 1 条 import 顺序问题已用子进程夹具修掉） | `python3 -m pytest tests/ -q` |
| 插件 | **41 passed / 0 fail**（9 个测试文件逐个跑，含新增 `stageActions.test.mjs` 8 条） | `node --test harness-plugin/tests/<f>.test.mjs` |
| 引擎桥 | `stage="settle"` 零 LLM 零因子；`func="reflect"` 只产因子不动资金（round-trip 实测） | `python3 /tmp/bridge_roundtrip.py` |
| 客户端 bundle | `node --check` + 真实求值冒烟（`__ModuleLoader__.load` 已注册、factory 无异常） | `node /tmp/smoke_client.mjs` |

**未决（等你拍板）**：
1. `## 🪞 反思人设` 这个 header 文案 / 放置位置 OK 吗？
2. `stage="reflect"` 现在返回零值 `settlement` 壳；要不要改成 `settlement: null`？
3. 缺陷 1（去重）与 3（口径）本质是**共享路径的 bug**，目前只修在 opt-in 分支。
   要不要提升为所有狗都修（会改变单狗反思样本，但不改结算/资金）？

