# 因子生产并行化设计（day-loop → 并行挖掘 / 串行回放）

> 状态：设计稿 v1
> 日期：2026-08-27
> 范围：`python-engine` 因子产生链路；北单串关体系的具体落地契约见
> [`beidan_factor_parallel_plan.md`](beidan_factor_parallel_plan.md)。

---

## 0. 一句话结论

当前 day-loop 里**必须串行的是「下单决策」**（资金 / 挂单 / 记忆随交易日递推，本质是自回归推理）；
**「因子产生」属于训练侧**——赛果已确定后可以像 Transformer 训练一样并行 map，
最后在 barrier 后按日期顺序 reduce 写回。这样既不改变 walk-forward 口径，也不引入数据泄漏。

---

## 1. 现状：day-loop 就是一个 RNN

代码入口：

- `python-engine/dsfootball_cli.py::cmd_runall`：`while d <= end` 逐日 `agent.run_day`
- `harness-plugin/replay.js::runReplayDay`：逐日 `prepare → analyze → settle → factor-induction → review`
- `scripts/run_beidan_factor_test.py`：逐日 `analyze(LLM) → settle(reflect)` 的实验循环

隐状态（RNN 的 `h_t`）包括：

```text
h_D = {
  role.capital,
  role.orders（未结算/已结算）,
  memory/factor_memory.json（因子统计 + history）,
  memory/reflection_memory.json（反思）,
  factors/fac_*.json（因子 slugs/定义）
}

h_{D+1} = F(h_D, x_{D+1}, y_{D+1})
```

其中 `x_D` 是赛前数据，`y_D` 是赛后结果。`F` 里有 LLM 决策、资金扣减、因子合并，不是可结合算子，
所以**同一条轨迹沿时间轴做 exact parallel scan 不可行**。

但 `F` 可以拆成两段：

```text
analyze(D)   ：用 h_D + x_D 做决策        → 必须因果、必须串行
reflect(D)   ：用 h_D + (x_D, y_D) 挖因子 → y_D 已知，可以并行
```

---

## 2. 数据泄漏红线（任何并行方案的前置条件）

1. **赛果永远不进 analyze prompt**：`agent.py::node_strip_scores` 是竞彩侧的护栏；
   北单侧必须同样保证 `result / spvalue / score / draw_datetime` 不进入选腿 prompt。
2. **因子按日期可见**：
   - 跨狗注册表 `factor_registry.py::get_all_factors(before_date=...)` 已按 `first_seen` 和
     `history.date <= before_date` 过滤，这个模式要保留。
   - ⚠️ 自有因子 `factor_select.py::factor_profile(stats, now=as_of)` **目前没有按 as_of 过滤
     history**，只影响休眠判断；并行预生成/预加载因子库时必须先补上
     `hist = [h for h in hist if h.date <= as_of]`。
   - ⚠️ `ReflectionMemory.format_for_prompt()` 也没有 as_of 过滤，同样要补。
3. **发现窗口 ≠ 评估窗口**：在 `[a,b]` 上并行挖出的因子，只能进入 `b` 之后日期的
   analyze prompt；如果要在同一段回测里评估，必须走 walk-forward（每天只用 `<D` 发现的因子）。
4. **共享文件只允许单写者**：`factors/fac_*.json`、`factor_memory.json` 是全局状态。
   并行 worker 一律写自己的临时根（`DS_ROLES_ROOT` / `DS_FACTORS_ROOT`），
   reducer 串行合并，落盘用「备份 + 原子替换」。

---

## 3. 项目里已有的并行点（2026-08-27 核对）

| 位置 | 现状 |
|---|---|
| `batch_agents.sh analyze` | 多狗分析并发（`PARALLEL`） |
| `python-engine/run_base_dogs.py` | 基础 2 狗按 7 天 chunk 并行 |
| `harness-plugin/dashboard.js` `/ds-induct-all` | **非 alpha 因子归纳 `Promise.all` 并行 → alpha barrier 串行**（正确骨架） |
| `python-engine/src/factor_induction.py` | 阶段 A 注释「可并行，这里顺序执行」；阶段 B alpha barrier 已实现 |
| `scripts/replay_week.py` | `ProcessPoolExecutor` 对 (狗, 日) 并行，且先按日期截断上下文——防泄漏样板 |
| `prepareRange` / `windows.js` | 数据准备整段预取 + 当日智能分窗 |

结论：**跨狗/跨场景的数据并行已经在用；还差「按天并行挖掘 → 串行 reduce」这套训练侧流水线。**

---

## 4. 可落地的并行方案（P0–P4）

### P0 跨狗并行（最安全）

非 alpha 狗状态互相隔离，`analyze/settle/reflect/induction` 全部可并行；
所有非 alpha 完成后，alpha 跨狗归纳串行一次（barrier）。

- 落地：把 `factor_induction.py::main` 阶段 A 的 `for` 改成线程/进程池；
  或继续维持 dashboard 层 `Promise.all`，CLI 只跑单 role。
- 注意：并行 reflect 时多狗会写全局 `data/factors/fac_*.json`，需隔离根目录或原子写。

### P1 按天并行「因子挖掘」+ 按日期串行 reduce（核心方案）

项目每天有 `roles/<狗>/history/<D>__pre-factor/`（结算后、因子流前）检查点，
它本身就是当天的因果状态 `S_D`。因此：

```text
map（并行）:
  worker(D) = run_reflect(settled_orders(D), S_D) → candidates_D.json
barrier:
reduce（串行）:
  for D in sorted(days):
      apply candidates_D（date=D）
      factor_induction(D)
```

- 每个 worker 只读 `S_D`，只使用 `<= D` 的订单/因子/反思，不产生前视。
- reducer 按日期推进，等价于原 day-loop 的因子侧结果。
- 下单决策侧（analyze）仍保持串行，不受影响。

### P1b 单狗 K 日 batch（例如 4 天一次）

P1 是「按天有因果快照」的理想形态；没有每日快照时，可以退化为**K 日 batch**：

```text
S_0 = chunk 起点状态（资金/订单/因子库，冻结）

① analyze（并行）:
   for d in [D0 .. D0+K-1]:
       worker(d, S_0) → orders_d        # 都只看 S_0，不看任何赛果
② apply（串行，很快）:
   for d in [D0 .. D0+K-1]:
       按顺序 place orders_d，资金不足按原策略 skip/缩放
③ settle + reflect（并行）:
   for d in [D0 .. D0+K-1]:
       worker(d) = settle(orders_d) + reflect(orders_d, S_0) → candidates_d
④ barrier reduce（串行）:
   for d in [D0 .. D0+K-1]:
       按日期 apply candidates_d + 结算资金
   → S_K，作为下一 chunk 的起点
```

- **速度**：K 天内每天 2 次 LLM（analyze + reflect）变成「并行 1 批 analyze + 并行 1 批 reflect」，
  wall-clock 近似从 `2K` 次串行调用降到 `2` 轮调用（实际受 worker 数/限流约束）。
- **代价**：chunk 内 `D+1..D+K-1` 的决策看不到 `D..D+K-2` 当天新学到的因子，
  因子更新最多滞后 `K-1` 天。这不是严格 walk-forward，而是 **frozen-state 近似**。
- **泄漏安全性**：只要 ① 里全部决策共用 `S_0`、chunk 内任何 worker 都看不到本 chunk 赛果、
  ④ 按日期顺序单写者归并，就没有前视；有损的是时效性（factor lag），不是因果性。
- **适用边界**：K 日 batch 用于**训练/候选因子挖掘**没问题；**评估回放仍应逐日串行**，
  否则资金与因子滞后都会让回测偏离线上行为。

### P2 单日内 reflect 分片（MapReduce）

`run_reflect` 当前把当天所有已结算订单塞进一个 prompt，受 token 预算限制。
可以按 `lota_id` 切 k 个不相交 shard，每个 shard 一次 LLM，输出局部
`alpha_factors / per_match / factor_attribution / key_slugs`，reducer 清洗、去重后统一
`FactorMemory.record`。

安全前提：reflect 本来就在结算后执行，赛果可见是允许的；补充比赛仍只能取
当前足球日窗口内 `state==6` 的场次。

### P3 `factor_induction` 内部 LLM 判重并发

`induct_scope` 的候选对两两独立，当前边判边 merge。改为：

1. 对不可变快照并行调 `llm_judge_pair`（受 `--limit` 控制）；
2. 收集全部 verdict；
3. 单线程按确定性顺序应用合并（样本量降序；冲突对保守不合并）。

写阶段必须单写者。

### P4 场景 / 集成并行（rollout）

用不同 `user_notes` / 人设 / 退役参数开 N 个沙箱并行跑。每条轨迹自身因果，
跑完比较再决定转正；跨沙箱因子合并在所有轨迹结束后进行，并给因子加
`valid_from`（不能早于其发现窗口）。

---

## 5. 通用架构：并行 map → barrier → 串行 reduce

```text
                      ┌─ worker(D0) ─→ candidates_D0 ─┐
S_D0..S_Dn（因果快照）─┼─ worker(D1) ─→ candidates_D1 ─┼─→ barrier ─→ 串行 reduce ─→ 因子库
                      └─ worker(Dn) ─→ candidates_Dn ─┘                （按 D 顺序 apply/induction）

                                                                    ↓
                                         analyze(D') 只消费 valid_from <= D' 的因子
```

**不推荐并行**：

- 同一条轨迹的 analyze 跨天并行（资金/挂单/因子状态严格递推）；
- 当天多窗口共用资金并行下单（lost update + 预算重复占用）；
- alpha 归纳与非 alpha 归纳并行（依赖方向决定）。

---

## 6. 建议实施顺序

1. 补 as_of 过滤：`factor_profile` + `ReflectionMemory.format_for_prompt`。
2. 把 `factor_induction` 判重改成「快照读 + 并发判 + 串行写」。
3. 把阶段 A 非 alpha 归纳改成真并行。
4. 基于每日 `__pre-factor` 检查点做 P1 的按天并行挖掘 runner。
5. 北单侧按 `beidan_factor_parallel_plan.md` 走「7 月并行挖因子 → 8 月串行回放」验证。
