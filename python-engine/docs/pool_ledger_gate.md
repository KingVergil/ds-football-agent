# 奖池账本 + 两道硬门（Pool Ledger & Pool Gate）

> 落地日期：2026-09-11 ｜ 代码：`src/pool_ledger.py`（账本）+ `src/beidan_parlay_dog.py`（门）
> 实验依据：`docs/mispricing_bucket_report.md`、`docs/mispricing_bucket_result.json`

## 1. 一句话

北单是**奖池型**玩法，返奖 `2元 × 65% × ΠSP`（0.65 只乘一次）。账本每天把**全部已开奖的
北单场次**按「盘口类型 × x 区间」数票，得出每一档的**每腿边际 y 与它的置信区间**；引擎用
这个账本落两道硬门：**腿池门**（只买不让球盘里比锐市场便宜的腿）与**关数门**（每注关数
必须够长）。0 次 LLM、纯计数。

## 2. 两个量（口径冻结）

```
x = p_锐市场(去水) × o_北单赛前赔率      ← 赛前可算：奖池比锐市场便宜多少
z = 开奖SP × 1{该侧中了}                  ← 开奖后才有：这条腿每 1 元拿回多少

y = E[z]      单腿每 1 元期望 = 0.65·y − 1
              M 关串每 1 元期望 = 0.65·y^M − 1
              打平关数 m_star = ceil( ln(1/0.65) / ln(y 的 95%CI 下沿) )
```

* `p_锐市场` 用**与北单同一让球盘口**的锐市场：`gl=0` → Pinnacle 1X2；`gl≠0` → 公平盘
  「平均欧盘胜/平/负(<line>)」且 `line == goal_line`。
* 账本的桶 = `gl0 | glN` × `x ∈ {<0.9, 0.9–1.0, 1.0–1.1, 1.1–1.2, 1.2–1.35, 1.35–1.6, ≥1.6}`，
  **预先登记、不许边跑边调**（防 p-hacking）。
* 实验结论（2026-06-10 ~ 09-08 缓存普查）：错价只在 **gl=0** 的 **x≥1.1** 档；
  让球盘（glN）没有可证实的边际。

## 3. 两道门

| 门 | 规则 | 代码位置 |
|---|---|---|
| ① 腿池门 | 只收 `goal_line == 0`、且 `x = 市场p̂×赛前赔率 ≥ θ` 的腿；`x` 拿不到（无锐市场参考）也剔除 | `_pool_leg_ok` / `_apply_flex_guardrails` |
| ② 关数门 | 每**注**关数 `M ≥ m_star`（容错票按 M，不按选场数 N）；否则整票不出（空仓） | 同文件 `pool_gate.block` |

* θ 与 m_star 来自账本：θ 取「CI 下沿最高」的登记阈值，m_star 由该档 `y` 的 **CI 下沿** 反解
  （保守），并夹在 `[1, max_min_legs]`。
* 腿池门改变过腿集时，票型基数按**最终腿数**重算（`12过8` 被拦掉一半腿 → 回落 `6串1`），
  绝不按对不上的 N 算容错额度。
* 不合法/不可证 → 引擎宁可空仓（空仓是合法结果）。

## 4. 工程要点（都是踩过的坑）

### 4.1 开奖 SP 滞后 ⇒ 必须回看窗口，不能只更新前一天
北单多数要等整期结束（约 3 天）才出开奖 SP。因此：

* `update(role, as_of, lookback_days=14)`：每次扫 `[as_of − 14 天, as_of)` 内**所有**缓存日期，
  SP 到了就补记；没到的场次**不写 `ingested`**，下一轮继续扫 → 天然支持迟到。
* 结算（`settle`）里自动调用，**与有没有下注无关**（普查）——空仓日也照样更新。
* 漏网更久的场次可用 `--all` 回填。

### 4.2 幂等
`ingested: {lota_id: 足球日}` 保证同一场只入账一次；重复扫同一天不会重复计数。
已入账的 lid 在扫描时直接跳过（连 tags 都不读），所以每天的开销很小。

### 4.3 防未来（回放不偷看）
只入账 **足球日 < as_of** 的场次。回放里 `as_of = 回放当天`，即使本地缓存里已经有后续日期的
开奖结果也不会被吃进来；查询统计同样按 `by_day` 只取 `day < as_of`。
（沙箱会复制一份线上账本，因此账本里可能已有"未来"日期的条目，但 `as_of` 切片保证它们不参与决策。）

### 4.4 滚动窗口
统计默认取「`as_of` 前 60 天」；窗口内样本不足再回落全历史。用于发现错价漂移。

### 4.5 采样口径
账本吃**当天全部北单场次**，不是"我们下过的腿"（否则又是选择偏差）。
每场 H/D/A 三侧各算一条腿（输的腿 `z=0`），所以样本量是每天几十~上百条。

## 5. 配置（`data/roles/bc狗/parlay.json`）

```json
"pool_gate": {
  "mode": "enforce",          // off | shadow（只记录不拦，用于前向观察）| enforce
  "gl_classes": ["gl0"],      // 只做不让球
  "min_n": 30,                // 样本太少 → 回落 default_*
  "window_days": 60,          // 滚动窗口
  "default_theta": 1.1,       // 账本不足时的 x 门限
  "default_min_legs": 3,      // 账本不足时的最小关数
  "max_min_legs": 9,          // m_star 上限
  "lookback_days": 14,        // 每次结算回看天数（SP 滞后）
  "block_min_n": 200,         // 「判无边际 → 空仓」需要的最少腿数
  "block_min_days": 8         // 同上，最少天数
}
```

**判定表**（`PoolLedger.policy`）：

| 账本状态 | 结果 |
|---|---|
| 无数据 / `n < min_n` | `source=config_default`：用 `default_theta`、`default_min_legs`，**不停投** |
| 有登记阈值满足 `n ≥ min_n` 且 CI 下沿 > 1 | `source=ledger`：用该 θ，`m_star` 由 CI 下沿反解 |
| `n ≥ block_min_n` 且 `≥ block_min_days`，但没有任何阈值 CI 下沿 > 1 | `source=ledger`、`m_star=None` → **空仓**（自动降级） |
| `min_n ≤ n < block_min_n`（证据不足以下结论） | `source=config_default`：不下"无边际"结论，继续用默认门 |

## 6. 当前实测（回填 93 天缓存，as_of=2026-09-11）

```
📒 账本：已入账 1341 场，30 天有数据（其余因 SP 未出待补记）
   gl0  x≥1.1   n= 737  y=1.286 [1.138,1.434]  → 每注至少 4 关才打平（按 CI 下沿）
   gl0  x0.9–1  n= 629  y=0.967  ← 没有边际（x≥1.0 累计会被它稀释，所以 θ 取 1.1）
   glN  x≥1.1   n= 384  y=0.985  ← 让球盘无边际，因此 gl_classes=["gl0"]
```

引擎日志：

```
🧾 flex 票 6串1 | 注数 1 | 成本 2（资金 0.0%） | Πv̂ 2.986 | 估 ROI +94.1%
   | 池门 enforce θ=1.1 剩余腿 6(x_min=1.3) 每注6关/需≥3 [config_default]
```

## 7. 与旧机制的边界（隔离）

* 只有 `path == "beidan"` 的北单串关走这两道门；其它狗不读账本、行为不变。
* 默认 `mode="off"`（`FLEX_DEFAULTS`），只有 bc狗 在 `parlay.json` 里开启。
* 关掉（`"mode": "off"`）时：`_pool_gate_policy` 返回 `None`，护栏与 prompt 与从前**逐字节一致**。

## 8. 运维命令

```bash
# 日常（结算里自动跑；也可手动）
python3 -m src.pool_ledger update --role bc狗 --days 14
# 首次/补漏：回填全部缓存日期
python3 -m src.pool_ledger update --role bc狗 --all
# 看各桶 y / CI / 打平关数
python3 -m src.pool_ledger report --role bc狗
```

## 9. 测试

`tests/test_pool_ledger.py`（16）与 `tests/test_pool_gate.py`（8）覆盖：幂等、开奖 SP 滞后补记、
`as_of` 防未来、滚动窗口、y/SE/CI/m_star 数学、阈值优选、证据不足不停投、无边际自动空仓、
腿池门/关数门/shadow/off 回归、账本接线、结算钩子在关闭时的空操作。
