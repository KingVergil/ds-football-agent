# 北单串关因子测试：7 月并行挖掘 → 串行回放计划

> 状态：计划 / 接口契约 v1
> 日期：2026-08-27
> 面向：开发 `python-engine/scripts/run_beidan_factor_test.py` 的北单 agent + 因子公共链路维护者
> 通用原理见 [`factor_parallelism_plan.md`](factor_parallelism_plan.md)，本文只定北单落地方案。

---

## 0. TL;DR

- **7 月可以并行，但并行的是「因子挖掘（训练侧）」，不是「轨迹回放（决策侧）」**。
- 推荐两阶段：
  1. **Phase A（并行）**：7 月按 K 日 batch 挖因子（推荐 `--batch-size 4`）或每天独立
     （`isolated`），产出当日 `factor_memory / reflection / orders` 候选文件，worker 并发执行。
  2. **Phase B（串行）**：用 Phase A 合并出的因子库，从 8 月开始串行 day-loop 回放，
     `reflect=False`（不回写、不重训），只测「7 月学到的因子在 8 月是否有增量」。
- 这样 7 月 = 并行 map，8 月 = 串行 walk-forward 验证；训练/验证窗口完全分离，无前视。

---

## 1. 现状与问题

`scripts/run_beidan_factor_test.py` 当前逻辑：

```text
for D in 2026-07-01 .. 2026-08-24:
    analyze(D, use_llm=True)      # LLM 选腿/组票
    settle(D, reflect=True)       # LLM 因子反思
```

- 串行天数 55 天 × 每天 2 次 LLM ≈ 110 次串行调用，非常慢。
- `DS_ROLES_ROOT` 在模块 import 时用 `tempfile.mkdtemp` 固定，**无法直接给并行 worker 复用**。
- 北单数据缓存已就绪：`data/beidan/` 有 `2026-06-28 ~ 2026-08-27` 共 61 个足球日文件；
  7 月开奖 `result/spvalue` 齐全（未开奖场次会自然跳过）。
- 因子反思目前复用 `agent.py::run_reflect`（7 狗单关口径），
  **北单串关子单是多腿结构，这是并行前必须先修的口径问题**（见 §4 M1）。

---

## 2. 为什么这样切分

```text
训练侧（可并行）:  y_D 已知，miner(D) 只读 D 的数据 → candidates_D
决策侧（必须串行）: h_{D+1} = F(h_D, x_{D+1}, y_{D+1})，资金/挂单/因子状态递推
```

- 7 月每天开奖结果已在 `beidan_info.result/spvalue` 里，因此可以把 7 月当成
  **已标注训练集**，并行跑 miner。
- 8 月回放是评估：角色必须按真实时序逐日推进，否则资金、挂单、因子可见性都会失真。

---

## 3. Phase A：7 月并行因子挖掘（map）

### 3.1 数据流

```text
data/beidan/2026-07-*.json（只读）
        │  worker(D)：独立 DS_ROLES_ROOT/DS_FACTORS_ROOT/DS_SESSIONS_ROOT
        ▼
worker(D):
    BeidanParlayDog(user="bd_miner_<D>", capital=5000).reset()
    analyze(D, use_llm=True)      # 赛果不可见；无历史因子库
    settle(D, reflect=True)       # 产生当日因子候选
        │
        ▼
data/beidan_factor_test/
  mined/<D>/factor_memory.json
  mined/<D>/reflection_memory.json
  mined/<D>/orders.json
  mined/<D>/summary.json
  mined/<D>/done.flag
```

### 3.2 worker 约束（防泄漏）

1. 每个 worker **只允许读 `D` 足球日窗口内**的北单场次；
2. analyze prompt 只能出现 `goal_line + 赛前赔率 + 比赛信息`，
   **禁止出现 `result / spvalue / score / draw_datetime`**（当前 `_select_legs_llm` 已只拼赔率字段，保持并加显式 redact 测试）；
3. worker 起始状态 = `capital=5000` + 空 `factor_memory` + 空 `reflection`；
   —— 这里**故意放弃跨日资金递推**：Phase A 的目标是产生因子候选，不是复现资金曲线；
4. 每个 worker 用独立根目录，禁止写 `data/roles` / `data/factors` 线上目录；
5. 已有 `done.flag` 的日期自动跳过（支持中断续跑）。

### 3.3 并行方式

- 用**子进程**而不是线程：`DS_ROLES_ROOT` 是环境变量级隔离，且 `agent._rt` 是按 user 的
  进程内全局状态，线程并行会互相污染。
- 参考 `scripts/replay_week.py` 的 `ProcessPoolExecutor(max_workers=...)` 模式。
- 默认 `--workers 6`（DeepSeek 并发压力与耗时折中；2 次 LLM/天 × 31 天，6 worker 约 10~11 轮）。

### 3.4 输出文件格式

`summary.json`：

```json
{
  "day": "2026-07-01",
  "placed": 243,
  "settled": 243,
  "hit": 0,
  "miss": 243,
  "push": 0,
  "pnl": -486.0,
  "capital": 4514.0,
  "factor_count": 3,
  "llm_used": true,
  "seconds": 41.2
}
```

`factor_memory.json` 直接复用 `FactorMemory._save()` 结构（`factor_perf` + `updated_at`），
这样 reducer 可以直接按现有结构合并。

### 3.5 可选模式：单狗 K 日 batch（先试 `--batch-size 4`）

上面 3.1 是 `batch-size=31` 的极端（每天完全独立，7 月一次性并行）。
如果想让单狗在**同一段训练里保留跨日因子累积**，用 K 日 batch：

```text
S_0 = chunk 起点（资金/订单/因子库冻结）
① 并行 analyze: worker(d, S_0) → orders_d        # chunk 内都不看赛果、都看同一 S_0
② 串行 apply:   d 顺序 place orders_d（资金不足按现有规则 skip）
③ 并行 reflect: worker(d) = settle(d) + reflect(d, S_0) → candidates_d
④ 串行 reduce:  d 顺序应用 candidates_d + 结算资金 → S_{0+K}
下一 chunk 用 S_{0+K}
```

- K=4 时，原来 4 天 × 2 次串行 LLM ≈ 8 次调用，变成 2 轮并行调用（analyze 一轮 + reflect 一轮）。
- **代价**：chunk 内后几天的 analyze 看不到前几天新学到的因子，因子更新滞后 ≤3 天。
  这是 frozen-state 近似，不是严格 walk-forward；训练/挖候选可用，评估回放不可用。
- **泄漏安全性不变**：chunk 内共用 `S_0`、赛果只在 ③ 结算/反思侧出现、④ 按日期单写者归并。
- 对 7 月训练建议：先跑 `mine --mode batch --batch-size 4`；再跑 `mine --mode isolated`
  作为「无累积」对照，最后看哪个产出的因子库在 8 月验证更好。

---

## 4. Phase A'：barrier 归并（reduce）

### 4.1 归并算法

```text
输入: mined/<D>/factor_memory.json（31 个）
1. 按 D 排序，同 clean_name 的条目确定性合并（历史按 date 排序、统计重算）
2. 对候选对走现有 factor_induction：
   - same_name 确定性合并
   - bit 距离 ≤2 + 名字相似度 ≥0.35 的候选对 LLM 判重
   - 孤儿按 reflection 里的 key_slugs 补 fac 定义
3. 输出 library/factor_memory.json + library/factors/fac_*.json
4. 全量历史都是 7 月样本，mining_window_end = "2026-07-31"
```

实现方式：构造一个临时归并角色根，把 31 个 `factor_memory` 合并成一个
`factor_perf`，设置 `DS_ROLES_ROOT` / `DS_FACTORS_ROOT` 指向
`data/beidan_factor_test/library/`，直接调 `src.factor_induction.main`。
不写线上 `data/factors`。

### 4.2 归并验收

- 31/31 天都有 `done.flag`；
- 每个有结算单的日期都有 `factor_memory.json`；
- 归并后因子数不膨胀：同义因子已合并，方向相反因子保持两个；
- `factor_perf[*].first_seen/last_seen` 全部落在 `2026-07-01 ~ 2026-07-31`；
- 允许 0 因子，但不能有 `first_seen > 2026-07-31` 的脏数据。

---

## 5. Phase B：8 月串行回放（walk-forward 验证）

### 5.1 跑法

```text
起点: capital=5000，订单/反思为空，factor_memory = library/factor_memory.json
for D in 2026-08-01 .. 2026-08-24:
    analyze(D, use_llm=True)     # 消费 7 月因子库
    settle(D, reflect=False)     # 验证模式：只结算，不回写因子
    record trajectory
```

- `reflect=False` 是硬约束：8 月是评估集，不能让 8 月结果回流到因子库。
- 对照组：同样从 8-01 开始跑一次**空因子库**的串行回放（代码、人设、参数完全一致）。
- 每天只需 1 次 LLM（analyze），24 天串行约 24 次调用。

### 5.2 对比指标

| 指标 | 口径 |
|---|---|
| 资金曲线 / PnL | 期末资金、日 PnL、最大回撤 |
| 下单质量 | 子单数、命中率、走水率、空仓日 |
| 因子使用 | 每天 prompt 里实际注入的因子数（从 `_self_factor_text` 日志/会话记录取） |
| 腿级命中 | 单选腿命中率、全包腿占比（全包腿本身必中，单看单选腿） |
| 投入 | LLM 调用次数、耗时、token |

### 5.3 判定标准（先跑通再调优）

1. 因子组资金曲线与空因子组有明显差异（方向不做预设，可能正也可能负——负向因子也是信息）；
2. 因子组不是「7 月过拟合」：若 7 月 mined 因子在 8 月全部失效，优先检查
   `factor_profile` 的 as_of 过滤是否生效、reflect 是否按多腿归因；
3. 任何结论都要附 walk-forward 口径说明：**7 月只训练，8 月只测试**。

---

## 6. 严格 walk-forward 模式（后续可选）

如果要在 **7+8 月整段**上同时「训练 + 回放」，不能用 Phase A 的整库注入，而要：

```text
for D in 2026-07-01 .. 2026-08-24:
    先注入 mined 候选里 first_seen < D 的因子（reducer 按日期预生成每个 D 的因子快照）
    analyze(D, use_llm=True)
    settle(D, reflect=False 或按需 reflect)
```

这是 P1「按天并行挖掘 → 按日期串行 reduce」的完整形态，等 Phase A/B 跑通后再做。

---

## 7. 脚本接口约定（给北单 agent）

保留现有兼容入口，新增子命令：

```bash
# 串行全量（现有行为，不破坏）
python scripts/run_beidan_factor_test.py 2026-07-01 2026-08-24

# Phase A：并行挖掘 7 月（K 日 batch：推荐先试 4 天一次）
python scripts/run_beidan_factor_test.py mine \
  --mode batch --batch-size 4 \
  --start 2026-07-01 --end 2026-07-31 --workers 6 --resume

# Phase A 对照：每天完全独立（无跨日因子累积，最大并行度）
python scripts/run_beidan_factor_test.py mine \
  --mode isolated \
  --start 2026-07-01 --end 2026-07-31 --workers 6 --resume

# Phase A'：归并
python scripts/run_beidan_factor_test.py reduce \
  --mined-root data/beidan_factor_test/mined \
  --out-root data/beidan_factor_test/library

# Phase B：8 月串行回放（因子组 / 对照组）
python scripts/run_beidan_factor_test.py backtest \
  --start 2026-08-01 --end 2026-08-24 \
  --factor-memory data/beidan_factor_test/library/factor_memory.json \
  --factors-root data/beidan_factor_test/library/factors \
  --run-root data/beidan_factor_test/runs/<run_id> \
  --reflect false
python scripts/run_beidan_factor_test.py backtest \
  --start 2026-08-01 --end 2026-08-24 \
  --run-root data/beidan_factor_test/runs/<baseline_id> \
  --baseline
```

要求：

- 模块 import 不得再无条件设置 `tempfile.mkdtemp`；改为在 `main()` 内按 worker/run 初始化；
- worker 根目录统一放在 `data/beidan_factor_test/workers/<D>/`，不用系统临时目录（可审计、可续跑）；
- 所有写盘用「临时文件 + `os.replace`」，避免半文件被 reducer 读到；
- LLM 调用失败重试 1 次；连续失败写 `error.json` 而不是 `done.flag`。

---

## 8. 里程碑与分工

| 里程碑 | 内容 | 负责 |
|---|---|---|
| M0 | 数据/结算已通：`verify_beidan_dog_e2e.py` 通过；61 天缓存齐 | ✅ 已完成 |
| M1 | **并行前必修**：`factor_profile`/`ReflectionMemory` 按 `as_of` 过滤 history；北单串关 reflect 改为多腿归因（或 flatten 成腿级样本） | 公共因子侧 + 北单 agent |
| M2 | `run_beidan_factor_test.py` 重构为子命令 + 子进程 worker；增加 analyze redact 测试（result/spvalue 不出现） | 北单 agent |
| M3 | Phase A 跑通 7 月 31 天，输出 `mined/`；Phase A' 归并产出 `library/` | 北单 agent |
| M4 | Phase B 串行回放：因子组 vs 对照组，出 `report.md` / `trajectory.jsonl` | 北单 agent |
| M5 | 对比结论：8 月增量是否成立；决定是否进入严格 walk-forward 模式 | 一起 review |

---

## 9. 风险与注意事项

1. **串关 reflect 口径**：现有 `run_reflect` 以 `order.lota_id` 为单场、整单归因；
   北单子单是 8 腿结构，直接复用会只看到首腿。M1 必须解决，否则 Phase A 挖出的因子不可信。
2. **as_of 过滤缺失**：当前 `factor_profile` 会拿全部 history 算最近 N 单。
   只要库是 7 月预先灌入、8 月回放，风险小；一旦进入严格 walk-forward 必须已修复。
3. **开奖数据未齐的场次**：settle 会跳过 `result` 缺失腿，summary 里 `settled < placed`
   是正常现象，不要当成 bug；但要在 summary 里记录 `skipped_no_result`。
4. **DeepSeek 并发/限流**：worker 数不要一上来拉满，先 `--workers 4` 跑 3 天验证。
5. **样本量**：7 月只有 31 个足球日，因子最多几百条触发样本；结论只能作为方向性筛选，
   不要直接转正上线。
6. **输出目录不进 git**：`data/beidan_factor_test/` 属于运行时数据，加入 `.gitignore`
   （`data/` 本身已私有，确认即可）。
