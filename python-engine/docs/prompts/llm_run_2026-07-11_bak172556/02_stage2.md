# 2026-07-11 stage2 #2（真 LLM）

## SYSTEM

你是北单串关分析 agent（bcl狗）。你只决定一件事：**这一票买哪几条腿、每条腿买哪些方向**。
串长（腿数）由你自己定，不固定。

## 人设（策略唯一来源，按它执行）
## 🎯 个人偏好

你是「奖池错价猎手」（bc狗 · 账本版，沙盒验证中）。你不猜比赛，赚的是奖池定价错误。

## 唯一目标：长期正期望，不靠命中率

北单返奖 = 每注 2 元 × 65% × Π SP，0.65 只乘一次。
- 单腿永远亏：x = 市场p̂ × 赛前赔率 = 1.19 → 单腿期望 0.65×1.19−1 = −23%；
- 只能靠长串摊销：4 关 ≈ +86%、5 关 ≈ +172%、9 关 ≈ +748%（x=1.30 口径）；
- 2~3 关数学上不可能正期望 → 不买短串。

## 账本先验（已经查明，不要试图推翻）

1. 错价目前只在**不让球（goal_line = 0）**被证实（引擎的门只放行 gl=0）。
   **盘口类型只是产品信息，不是纪律**：H/D/A 都是"该盘口下的让球后结果"，
   你不需要、也不应该因为"这是让球盘"去否决一条腿——是否成交由引擎的门决定。
   你的 veto 只用于**信息层面**（伤停/阵容/轮换/盘口异动/奖池快照过期）。
2. 只在**奖池比锐市场便宜**的档位选腿：x ≥ 门限（现约 1.10）。
3. **x 不是越高越好**：门限 ~1.2 附近最稳；极端高 x（>1.4）多半是锐市场参考噪声，实测「按 x 降序挑最高那几条」连一张票都中不了（赢家诅咒）。**不要专门去挑极端高 x。**
4. 低赔档（x < 1.0，奖池比锐市场还贵）一律不买。
5. 你的「我觉得这场会赢」不是边际来源：你报的 p̂ 会被引擎封顶在市场的 1.25 倍，并按因子历史压回去。**边际来自 x 档，不来自你的信心。**

## 引擎每天给你什么

- 今日候选腿：只有 gl=0 且某侧 x ≥ 门限的场次，每条已算好 x 与市场 p̂；
- 门限 θ、各档实测 y 与 95%CI、每注最少关数 m_star（现约 4 关）；
- 资金与成本上限（注数 × 2 元）。

## 你只做三件事

1. **排序**：在候选腿里挑出你最愿意连乘的 N 条（N ≥ m_star，每注最多 9 关）。候选多于 N 时，**不要在 x 上择优**（宁可均匀挑），你只需剔除明确有问题的那几条。
2. **否决**：只允许基于信息层面的否决（伤停/阵容/盘口异动/信息滞后让「奖池便宜」不再成立），并写清是哪条事实；不许用「感觉」或「低赔更稳」当理由。
3. **定串长与票型**：默认 N串1；只有确认某 1~2 条腿「边际真实但胜率不高」且成本放得下时，才用 N过(N−1)（容错只降方差、不提高每元期望）。候选不足 m_star 或全部被否决 → 空仓（空仓是合法且常见的结论）。

## 因子怎么用

- **方向型因子**：只用于候选腿内部排序或否决；归因必须写因子清单里真实存在的名字（引擎要按因子历史校准你的 p̂）。
- **波动型因子**：只当**风险提示**（可能爆冷的场次少押/不押）。实测波动路径没有可证实的边际（不产生 x），不作为选腿依据。
- 不许为了「用上某个因子」去凑腿。

## 纪律

- 一场最多一条腿；同一天同一场不重复买；**只买候选里的腿**。
- **多选（一条腿买 2~3 个方向）是你的正当决策，但有条件**：只有当**每个被选方向各自都过门（x≥θ）**时才可以多选
  —— 引擎按被选方向的**平均 x** 判这条腿过不过门。成本 ×2/×3、该腿边际与 x 都按平均算。
  典型场景：一场里两个方向都便宜（例如 H x=1.19✅ 且 D x=1.13✅），你不确定哪个赢 → **双选**（用成本换方向保险）；
  只有一侧过门 → **必须单选**。
- 出票前写清「为什么这几条腿值得连乘、每条 x 多少、串长多少、估 ROI 多少」；算不出 > 0 就不出票。
- 验证期预期：在 30~44 天窗口里，4 关以上票的期望中票数 < 0.2 张 → **不能用「中没中」判断策略好坏**，要看腿级 y 与置信区间（引擎会给你）。
- 本狗是**沙盒验证版**：回放验证通过、人工转正后才上 live。

## 北单返奖数学（必须按这个算，不许跳过）
- 中奖奖金 = 每注 2 元 × Π(开奖SP) × 0.65；**0.65 只乘一次，与串长无关**。
- 单注毛额 > 10000 元要缴 20% 个税（**全额计税**）：毛额 10000~12500 是净亏区，单注 SP 连乘要么 ≤7692，要么 ≥9615。
- 单腿边际 v̂ = p̂（你估的概率）× 该方向北单赛前赔率；与市场持平 = 1.000。
- 整票期望 ROI = 0.65 × Π v̂ − 1。**打平需要 Π v̂ > 1.538**：

| 腿数 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 10 |
|---|---|---|---|---|---|---|---|---|
| 每腿最低 v̂ | 1.240 | 1.154 | 1.114 | 1.090 | 1.074 | 1.063 | 1.055 | 1.044 |

- ⚠️ 北单是**奖池型**：你看到的赔率只是「下注当时的奖池快照（1/f）」，最终奖金由**赛后开奖 SP**决定
  （1531 场实测：开奖SP/下注时赔率 中位 1.008，p10 0.849、p90 1.208 → 单腿漂移 ±13%）。
  所以引擎按**每注关数 M**（容错票按 M，不按选场数 N）加了漂移安全垫，**实际出票线**是：
  2关 1.741、3关 1.791、5关 1.871、
  8关 1.971、9关 2.001——别卡着 1.538 出票。
- 多选腿（一条腿买多个方向）的边际 = 这些方向 v̂ 的**平均值**：只有当你对被选的**每一个**方向都认为 v̂>1 时多选才不亏；三个方向全买 = 买市场本身（≈1.00），只是用成本换命中率。
- ⚠️ 所以「求稳买低串」在这个玩法里是负期望的数学陷阱：2串1 要求每腿 +24% 边际，做不到就是稳定 −35%。正期望只能来自**多条真有边际的腿连乘**。**没有边际就不出票**，空仓是合法结果。

## 引擎护栏（超出会被修剪或拒单，别试探）
- 腿数 2~17；每腿 1~3 个方向；整票注数 ≤ 512；单票成本 ≤ 资金 10%（≈ 500.00 元 → 最多 250 注）
- v̂ < 1 的腿会被引擎剔除；整票 Π v̂ ≤ 打平线则不出票。
- **市场 p̂ 封顶**：每条候选后面都给了「市场 p̂（锐市场同一盘口去水；北单赔率本身是公平赔率，Σ1/赔率≈1.00）」。你报的 p̂ 最多只能是市场 p̂ 的 1.25 倍，**超出的部分引擎直接砍掉**——所以"我特别看好"没有意义，必须说清你为什么比锐市场还准（例如阵容/资金流/盘口背离这类市场没定价的信息）。
- 拿不到市场参考的场次：可以用，但你没有外部依据，默认别给高 p̂。

## ⛔ 奖池门（引擎硬规则，先看这个）
- **候选里所有场次都给你**（含让球盘）；但**只有不让球（goal_line=0）的腿会进票**。
- 让球盘（glN）**只观察、不下注**：x 照算、你可以评论，但它不进票（等该盘口的 CI 下沿 > 打平线再放行）。
- 放行腿还要 **x = 市场 p̂ × 北单赛前赔率 ≥ 1.10**；候选行里已标好每侧的 x（✅ = 过门）。
- 每注关数必须 **≥ 3 关**（打平所需），否则整票不出票（空仓）。
- 门限来源：config_default —— 账本证据不足（gl0 x≥1: n=29, 3 天，未达停投门槛 n≥200 且 ≥8 天）→ 不下「无边际」结论，用配置默认 θ=1.1、最小 3 关



## 候选腿表（引擎定价 + stage1 判断；原始数据段在 stage1，这里不重复）
- Lota4480398 | 光州FC vs 浦项铁人 [韩K联] goal_line=0（平手） 赔率 H/D/A=6.80/4.04/1.65 | 市场p̂(Pinnacle去水) H/D/A=H0.147/D0.247/A0.606 | x(H/D/A)=H1.00/D1.00/A1.00 盘口=gl0｜可下注盘口 | stage1 SKIP(conf=0)
- Lota4480346 | 蔚山HD vs 全北现代 [韩K联] goal_line=0（平手） 赔率 H/D/A=2.65/3.53/2.95 | 市场p̂(Pinnacle去水) H/D/A=H0.378/D0.283/A0.339 | x(H/D/A)=H1.00/D1.00/A1.00 盘口=gl0｜可下注盘口 | stage1 SKIP(conf=0)
- Lota4480338 | 金泉尚武 vs 富川FC [韩K联] goal_line=0（平手） 赔率 H/D/A=2.08/3.60/4.14 | 市场p̂(Pinnacle去水) H/D/A=H0.481/D0.278/A0.242 | x(H/D/A)=H1.00/D1.00/A1.00 盘口=gl0｜可下注盘口 | stage1 SKIP(conf=0)
- Lota4481127 | 龙仁FC vs 忠北清州 [韩K2联] goal_line=0（平手） 赔率 H/D/A=2.42/3.53/3.30 | 市场p̂(Pinnacle去水) H/D/A=H0.414/D0.283/A0.303 | x(H/D/A)=H1.00/D1.00/A1.00 盘口=gl0｜可下注盘口 | stage1 SKIP(conf=0)
- Lota4467565 | 腓特烈斯塔 vs 利勒斯特罗姆 [挪超] goal_line=0（平手） 赔率 H/D/A=2.64/3.56/2.92 | 市场p̂(Pinnacle去水) H/D/A=H0.317/D0.282/A0.402 | x(H/D/A)=H0.84/D1.00/A1.17✅ 盘口=gl0｜可下注盘口 | stage1 A(conf=1)
- Lota4468144 | 拉赫蒂 vs 赫尔辛基 [芬超] goal_line=0（平手） 赔率 H/D/A=3.28/4.18/2.18 | 市场p̂(Pinnacle去水) H/D/A=H0.299/D0.251/A0.450 | x(H/D/A)=H0.98/D1.05/A0.98 盘口=gl0｜可下注盘口 | stage1 D(conf=1)
- Lota4477391 | SJK学院 vs MP米克力 [芬甲] goal_line=0（平手） 赔率 H/D/A=2.57/4.31/2.63 | 市场p̂(Pinnacle去水) H/D/A=H0.324/D0.289/A0.387 | x(H/D/A)=H0.83/D1.25✅/A1.02 盘口=gl0｜可下注盘口 | stage1 D(conf=1)
- Lota4467093 | 米亚尔比 vs AIK索尔纳 [瑞典超] goal_line=0（平手） 赔率 H/D/A=2.10/3.44/4.24 | 市场p̂(Pinnacle去水) H/D/A=H0.496/D0.249/A0.255 | x(H/D/A)=H1.04/D0.86/A1.08 盘口=gl0｜可下注盘口 | stage1 A(conf=1)
- Lota4477390 | 哈卡 vs EIF埃克纳斯 [芬甲] goal_line=-1（主队让1球） 赔率 H/D/A=2.70/3.90/2.67 | 市场p̂(让球欧盘去水) H/D/A=H0.400/D0.236/A0.364 | x(H/D/A)=H1.08/D0.92/A0.97 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 H(conf=1)
- Lota4468146 | TPS图尔库 vs 奥卢 [芬超] goal_line=0（平手） 赔率 H/D/A=2.32/3.79/3.27 | 市场p̂(Pinnacle去水) H/D/A=H0.347/D0.282/A0.370 | x(H/D/A)=H0.81/D1.07/A1.21✅ 盘口=gl0｜可下注盘口 | stage1 A(conf=1)
- Lota4468145 | 格尼斯坦 vs 玛丽港 [芬超] goal_line=-2（主队让2球） 赔率 H/D/A=3.30/4.27/2.15 | 市场p̂(Pinnacle换算(line)去水) H/D/A=H0.283/D0.215/A0.502 | x(H/D/A)=H0.94/D0.92/A1.08 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 A(conf=1)
- Lota4467503 | 奥勒松 vs 莫尔德 [挪超] goal_line=0（平手） 赔率 H/D/A=3.94/4.51/1.90 | 市场p̂(Pinnacle去水) H/D/A=H0.301/D0.251/A0.448 | x(H/D/A)=H1.19✅/D1.13✅/A0.85 盘口=gl0｜可下注盘口 | stage1 H(conf=1)
- Lota4467094 | 奥尔格里特 vs 赫根 [瑞典超] goal_line=+1（主队受让1球） 赔率 H/D/A=3.74/3.72/2.15 | 市场p̂(让球欧盘去水) H/D/A=H0.378/D0.238/A0.384 | x(H/D/A)=H1.41✅/D0.88/A0.83 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 H(conf=1)
- Lota4469072 | 戈尔韦联 vs 斯莱戈流浪者 [爱超] goal_line=-1（主队让1球） 赔率 H/D/A=3.43/3.54/2.34 | 市场p̂(让球欧盘去水) H/D/A=H0.339/D0.232/A0.430 | x(H/D/A)=H1.16✅/D0.82/A1.01 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 H(conf=1)
- Lota4477392 | 克鲁比04 vs 基柏 [芬甲] goal_line=+1（主队受让1球） 赔率 H/D/A=2.24/3.40/3.83 | 市场p̂(让球欧盘去水) H/D/A=H0.421/D0.250/A0.329 | x(H/D/A)=H0.94/D0.85/A1.26✅ 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 A(conf=1)
- Lota4467508 | 特罗姆瑟 vs 瓦勒伦加 [挪超] goal_line=-1（主队让1球） 赔率 H/D/A=2.82/3.28/2.93 | 市场p̂(让球欧盘去水) H/D/A=H0.314/D0.230/A0.456 | x(H/D/A)=H0.89/D0.76/A1.34✅ 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 A(conf=1)
- Lota4459810 | 挪威 vs 英格兰 [世界杯] goal_line=+1（主队受让1球） 赔率 H/D/A=2.15/3.48/3.99 | 市场p̂(让球欧盘去水) H/D/A=H0.517/D0.234/A0.248 | x(H/D/A)=H1.11✅/D0.82/A0.99 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 H(conf=1)
- Lota4459811 | 阿根廷 vs 瑞士 [世界杯] goal_line=-1（主队让1球） 赔率 H/D/A=3.25/3.35/2.53 | 市场p̂(让球欧盘去水) H/D/A=H0.305/D0.272/A0.423 | x(H/D/A)=H0.99/D0.91/A1.07 盘口=glN｜只观察不下注（引擎规则，无需你处理） | stage1 A(conf=1)

## 你的工作（组装，不是重新定价）
1. **方向以 x 为准**：`x(H/D/A)` 是引擎算好的错价倍数，**带 ✅ 的那一侧就是过门的一侧**；
   一行里可以有多个 ✅。你**不能靠自报概率创造边际**（p 被封顶在市场的 1.25 倍 + 按因子历史压低），`p` 只用于排序。
2. **多选是你的正当决策**（一条腿买 2~3 个方向）：当**每个被选方向各自都 ✅ 过门**时可以多选
   （引擎按被选方向的**平均 x** 判这条腿过不过门；成本 ×2/×3）。
   例：某行 `H1.19✅ D1.13✅` → 可以双选 H+D（不确定哪个赢时用成本换方向保险）；只有一侧 ✅ → 必须单选。
3. **只能否决**：stage1 标了 `veto` 的腿默认不选；不同意它的理由可以选，但要在 `why` 写清。
4. **定串长与票型**：从过门腿里挑 N 条组成 `N串1`（N ≥ 每注最少关数）。
   候选多于成本/注数上限时**不要在 x 上择优**（那是在噪声上做选择），按 stage1 的信息剔除即可。

## 票型（结算模式，由你选；不给就用引擎兜底）
- `ticket` 缺省 = `N串1`（N=腿数，**每腿都必须中**，N ≤ 9）。N > 9 时引擎兜底为 `N过9`。
- 容错票 `N过M`：**容忍 t = N − M 条腿错**，注数 = `Σ C(N,M) × Π每腿选项数`，成本按注数放大。
- 硬上限（引擎兜底口径，超了会被改）：**每注最多 9 关**（M ≤ 9）、**容错最多 8 场**（N − M ≤ 8）
  ⇒ 选场数 N 最多 17。所以 N 可以 > 9，只要 M ≤ 9，例如：
  · `12过8` = 12 场单选、容错 4（每注 8 关）——这正是"允许容错"的用法；
  · `9过8` = 9 场单容忍错 1；`9串1` = 9 场全中。
- 怎么选：**先算清楚成本**（注数 × 2 元 ≤ 资金 10% ≈ 500.00 元，最多 250 注）。
  容错买的是"少一条腿报废"的保险，但每多一个容错档，注数按组合数增长：
  容错增加的边际收益抵不过注数爆炸时就别买，宁可减腿或空仓。腿都很有把握 → `N串1`；
  只有一两条不太有把握、且成本放得下 → `N过(N−1)`（例如 9过8）。

## 输出（只输出 JSON）
{"legs":[{"lota_id":"Lota...","picks":["H"],"p":{"H":0.55},"factors":["因子名"],"why":"≤20字"}],"ticket":"N串1 或 N过M","empty":false,"reason":""}
- legs 按信心从强到弱；picks ⊆ H/D/A（1~3 个）；**必须给每个被选方向的概率 p（0~1）**。
  ⚠️ p 只用于排序与信心表达：引擎会把它封顶在市场的 1.25 倍并按因子历史校准，**边际来自 x，不来自你的 p**。
- **单选 / 多选的条件**：只有一侧 ✅ → 单选；**多个方向各自都 ✅** → 可以多选（成本 ×k、边际按平均，
  是用成本换方向保险）。不要选没有 ✅ 的方向。
- `factors`：这条腿靠上面因子清单里的哪些因子成立（**只能填清单里出现过的名字**，没有就空数组）。
  引擎会按因子历史**校准你报的概率**：某个因子过去声称 60% 实际只中 40%，下一轮它支持的概率就会被压低。
- 空仓就输出 {"legs":[],"empty":true,"reason":"为什么今天没有正边际的腿"}。

## USER

按人设与上面的门/腿表组装这一票的腿集 JSON：方向以 x 为准，只做过门（✅+🟢）的腿，尊重 stage1 的 veto；没有可下注的腿就空仓。

## RESPONSE

[thinking]
We need answer JSON only. Need follow persona, assemble ticket. Need decide legs. Need analyze candidate table.

We need only gl=0 can bet; glN observe only no bet. Need x >= θ=1.10 and ✅. Let's list gl0 candidates with ✅:

- Lota4480398 光州 vs 浦项 x all 1.00 ✅ none? x(H/D/A)=H1.00/D1.00/A1.00 no ✅. stage1 SKIP.
- Lota4480346 蔚山 vs 全北 x all 1.00 no.
- Lota4480338 金泉 vs 富川 x all 1.00 no.
- Lota4481127 龙仁 vs 清州 x all 1.00 no.
- Lota4467565 腓特烈斯塔 vs 利勒斯特罗姆 gl0 x H0.84/D1.00/A1.17✅ stage1 A conf1.
- Lota4468144 拉赫蒂 vs 赫尔辛基 gl0 x H0.98/D1.05/A0.98 no ✅? D 1.05 below 1.10, no ✅. stage1 D but x not ✅. So not gate.
- Lota4477391 SJK学院 vs MP米克力 gl0 x H0.83/D1.25✅/A1.02 stage1 D. Only D ✅.
- Lota4467093 米亚尔比 vs AIK索尔纳 gl0 x H1.04/D0.86/A1.08 no ✅ (threshold 1.10).
- Lota4468146 TPS图尔库 vs 奥卢 gl0 x H0.81/D1.07/A1.21✅ stage1 A. Only A ✅.
- Lota4467503 奥勒松 vs 莫尔德 gl0 x H1.19✅/D1.13✅/A0.85 stage1 H. H and D both ✅.

Other gl0? Lota4467094 is gl+1 only observe; Lota4469072 gl-1 observe; etc. Need ignore.

So only three gl0 with ✅:
1 Lota4467565: A x1.17 stage1 A
2 Lota4477391: D x1.25 stage1 D
3 Lota4468146: A x1.21 stage1 A
4 Lota4467503: H x1.19, D x1.13 (two ✅) stage1 H

Need respect stage1 veto? None say veto. But stage1 has SKIP conf=0 for early matches, but those x all 1.00 so not relevant. Stage1 for relevant maybe conf=1. Need use.

Need decide N. Minimum m_star? User says per persona "每注最少关数 m_star（现约 4 关）" but later hard rules: "每注关数必须 ≥ 3 关" in 奖池门. There's conflict. Persona says engine gives m_star ~4; 护栏 says 每注最少关数 m_star present. 奖池门 says ≥3. Later says "腿数 2~17". And ticket default N串1 N≤9. Need pick N ≥ m_star. Need probably 4? Let's check we have only 4 unique match legs? Actually four matches:
- 腓特烈斯塔 A
- SJK学院 D
- TPS图尔库 A
- 奥勒松 vs 莫尔德 maybe H and/or D (one match can be multi-direction, but "一场最多一条腿; 同一天同一场不重复买". Multi-select on one leg counts as one leg but picks multiple directions. So we can have one leg for 奥勒松 with picks maybe H,D? That's allowed if both ✅. So we can have at most 4 legs if single/multi. Unique matches = 4. Need N≥4. Good. Could form 4串1 with these 4 legs. But the 奥勒松 leg multi-select H+D? Need decide. Each direction must be ✅: H x1.19, D x1.13 both ✅. Could multi-select to cover uncertain. But cost increases by 2; marginal average x = (1.19+1.13)/2 = 1.16, still >=1.10. That's okay. However, if multi-select a leg with two directions, ticket options multiply; if 4 legs all single except one double, total combinations = 2, cost = 2注? Actually for N串1 with one leg double = 2 bets? In Chinese parlay, each leg can have multiple choices; total number of combinations = product of number of picks per leg. So if one leg has 2 choices and other 3 singles, total = 2 x 1 x 1 x 1 = 2 notes. Cost = 4元. Within budget. So ticket 4串1 with 4 legs, one multi-select H+D. Need provide p for each pick. Need perhaps include probabilities. Need obey market cap: p cannot exceed market p̂ *1.25. Need set p for each selected direction. Since p only for sorting and confidence, but engine will cap. We need give p plausible ≤ 1.25 * market p̂ maybe. For leg 奥勒松 pick H and D: market p̂ H=0.301,D=0.251. 1.25*market = H0.376, D0.314. But if we give p H maybe 0.36, D maybe 0.29. Need not exceed. But p doesn't need be accurate, just for sorting; however engine will cap. We should provide maybe conservative based on market? Actually p should be your estimated probability; but we need avoid exceeding cap. Could set p equal to market p̂ perhaps? Since we're not adding info. But factors maybe? Need factor list. Need identify factors allowed. The user says factors must be from "因子清单" which is not explicitly listed in prompt? We have no enumerated factor names? We need infer? The prompt mentions "方向型因子" but no list. It says "归因必须写因子清单里真实存在的名字（引擎要按因子历史校准你的 p̂）" but no factor list provided in visible prompt. Could be latent? We need maybe use empty array if no factors. The instructions: factors can be empty if no factor. So maybe use empty arrays to avoid invalid factor names. Or use known factors? Need decide. Since no list, safest empty factors. But maybe we need provide some reasoning in why ≤20字. What can we say: "x过门，gl0错价" etc. That's not a factor list. Could use empty.

Need check stage1 conf: stage1 A/D etc. Not veto. Need perhaps respect stage1 picks? Stage1 "A" or "D" maybe recommended. For Lota4467503 stage1 H but we multi-select H+D maybe okay if both ✅; but stage1 had H only? It says stage1 H(conf=1) and x H1.19✅ D1.13✅. Could multi-select because both ✅. Good.

Need consider potential info-level vetoes: no veto stated. Need maybe veto some due to data? But no specific facts. Could include only legs with x≥1.10 and gl0. Need maybe all four. But need verify if "候选多于 N 时不要在 x 上择优". We have exactly 4? Actually unique matches = 4, so all. Need not drop any. But maybe one leg is 奥勒松 vs 莫尔德 with H and D both ✅. That is okay.

Need decide ticket: 4串1? Since N=4, m_star ~4, fine. But ticket default N串1 ≤9. Could set "4串1". Need compute expected ROI? Not required in JSON but maybe in reason? Reason field maybe brief. Need include legs sorted by confidence strong to weak. Need assign p.

Let's compute p values cautiously.

Candidate legs:

1 Lota4467503 奥勒松 vs 莫尔德: picks H,D. p H and D. Market p̂ H=0.301,D=0.251,A=0.448. Since we are picking H and D, not A. Need p H >0, D >0. Must be less than or equal to 1.25 market? H max 0.376, D max 0.314. But p should reflect our probability estimates; if we give H=0.34,D=0.27, H+D=0.61 leaving A=0.39. That may be okay? Market had A=0.448 favorite. But we are selecting H/D because x too high; probability perhaps lower than market? Actually x = market p̂ * odds; if x > 1, odds are higher than fair. The p̂ is market probability, not our p; our p could be same as market? Engine will cap at 1.25* market p̂. So any p within. Since H and D both underdogs according to market? Market H 0.301, D 0.251, A 0.448. If we give H=0.30,D=0.25, that's sum 0.55, A 0.45. Fine. But if we give p equal market, that's acceptable. Why would we choose H/D then? The marginal is from x not our p; we don't need p to exceed. So set p near market maybe. Need sort by confidence: Which leg strongest? Could rank by x or by stage1? Instructions: legs sorted by confidence strong to weak. Could use x as proxy? But x is not confidence; p maybe? But p is capped. We need sort. We can use maybe higher x first? But they said don't use x to optimize; for ordering within selected legs, we can use p or other info. Need perhaps sort by p? But p for each single direction vs multi? Hard.

Need maybe include all four legs with single picks except 奥勒松 double. Let's assign p:
- 奥勒松 vs 莫尔德 picks H,D: p maybe H=0.30,D=0.25 (same as market). Could be strongest? Not sure.
- TPS图尔库 vs 奥卢 pick A: market p̂ A=0.370, x=1.21. p A maybe 0.37.
- SJK学院 vs MP米克力 pick D: market p̂ D=0.289, x=1.25. p D maybe 0.29.
- 腓特烈斯塔 vs 利勒斯特罗姆 pick A: market p̂ A=0.402, x=1.17. p A maybe 0.40.

Need sort by confidence: maybe higher p? Then Fredrikstad A 0.40, TPS A 0.37, Olesund H/D combined maybe 0.55? But single p H 0.30 D 0.25 not high. Leg with multi-direction has higher chance at least one hits (0.55) but ticket requires all legs all selected directions? Wait N串1 means each leg's selected direction must win. If leg has double, the leg is considered "hit" if any of the picked directions wins? Actually in accumulator, each "leg" with multiple choices creates multiple possible combinations; the ticket wins only if one of those combinations occurs such that every leg wins according to the combination. Since leg has two choices, there are two possible outcomes for that leg in the combination. So the leg is not "any" but each combination includes one choice. The ticket can win if at least one combination wins. So overall probability is product of single probabilities times sum of multi? For calculating, not necessary.

But confidence of leg with double could be considered okay because we cover both underdogs; but that's not exactly p sum. We can sort by maybe the probability of the selected pick? The double leg has two picks, not comparable.

Maybe sort by x? But user says don't select based on x; but sorting within candidates is okay? Persona says sort among candidates you want to multiply. Could use p from factors. Need perhaps rank by "confidence" as p values in single leg? For multi, maybe use sum p of selected picks? That would be 0.55 highest, then Fredrikstad 0.40, TPS 0.37, SJK 0.29. That would put 奥勒松 first. But is that "confidence"? Could be. But we need not be too precise.

Alternatively maybe we should avoid multi-select because it increases cost and we only have exactly 4 legs; if we multi-select, ticket number of bets =2, okay. But if we choose all single, 4串1 with four singles =1注. Which is simpler? The multi-select on 奥勒松 both H/D covers two directions, but if we select both, the ticket has 2 combos, cost 4. Could still be positive expected if average x 1.16. But is it necessary? We could select only H (since stage1 H) because D x 1.13 also ✅; but the instructions say multi-select is allowed when both ✅; not mandatory. Which is better? We need decide based on "uncertainty". Since I as model have no additional info, maybe multi-select is justified because both directions are cheap. But is it "as a ticket" better? The expected value of a double-leg is sum of p_i * odds_i for each pick? Actually the leg has two possible winning outcomes; the marginal for the leg if multi-selected = average v̂ of selected picks, where v̂ = p * odds? Wait need define v. The user text says "多选腿（一条腿买多个方向）的边际 = 这些方向 v̂ 的**平均值**：只有当你对被选的**每一个**方向都认为 v̂>1 时多选才不亏；三个方向全买 = 买市场本身（≈1.00）". It uses v̂ = p̂ * odds? Actually earlier单腿边际 v̂ = p̂（你估的概率）× 该方向北单赛前赔率. But later says "x = 市场 p̂ × 北单赛前赔率". They use x for market p? Need consistent. In candidate, x is given as market p * odds. So v̂ likely our estimated p * odds? But p is capped. However, for multi-select, they say average x? "引擎按被选方向的平均 x 判这条腿过不过门" (in ticket instruction). And "多选腿 ... 边际 = 这些方向 v̂ 的平均值". So we can consider average x? We must ensure average x ≥1.10, which is true: (1.19+1.13)/2=1.16. So okay.

But if we multi-select, the leg's effective x maybe 1.16. The overall product of x perhaps slightly lower than if we used only H x1.19, but reduces variance? Actually multi-select increases chance of that leg hitting but adds cost. Expected ROI is not simply product of average x? Let's check. For a single leg with two picks, the expected return per note? Suppose you bet two notes each 2 yuan: one on H, one on D. If H wins, you get payout on H note; if D wins, payout on D note; if A wins, lose both. Expected total payout across the two notes = 2*(p_H*SP_H + p_D*SP_D)*0.65? But note SP depends odds. The average v of the two picks is (p_H*odds_H + p_D*odds_D)/2? Actually each note has different odds. The expected return per note if considering total outlay 4: E = 0.65 * (p_H*odds_H*2 + p_D*odds_D*2) /4? Wait per note formula: single note cost 2, expected payout = 2 * p_i * odds_i * 0.65. For two notes, total cost 4, expected payout = 2 * 0.65*(p_H*odds_H + p_D*odds_D). Expected ROI = (2*0.65*(sum v_i))/4 - 1 = 0.65*(sum v_i)/2 - 1 = 0.65 * average(v_i) - 1? That matches average v^ of selected picks? Actually v_i = p_i*odds_i, not market x? But they earlier define v^ = p * odds. So yes average of v^ selected. So expected ROI using average v. Thus if both v >1, average >1, okay. But the market x given is market p * odds, not our p. They treat x as market p * odds, which is fair probability times odds. Our p may be capped to market*1.25. So likely v maybe similar to x if p close to market. For multi-select, average of x maybe is the effective x if p=market. Engine will use selected directions average x. So okay.

Thus multi-select is okay and possibly lowers variance (covers two outcomes) at cost. But if we have only 4 legs and all have x around 1.13–1.25, product of single x maybe? Let's compute product if we choose singles:
- 奥勒松 H x1.19
- SJK D x1.25
- TPS A x1.21
- 腓特烈斯塔 A x1.17
Product = 1.19*1.25*1.21*1.17 = let's compute: 1.19*1.25=1.4875; *1.21=1.800; *1.17=2.106. Expected ROI = 0.65*2.106 -1 = 0.369 (36.9%) if using x. But actual safety margins etc maybe require product of v^? The table in persona says actual出票线 for 4关 maybe? The table lists 每腿最低 v̂ for 4 legs =1.114, so average individual x need be >1.114. Our x avg about 1.205? Actually each leg above 1.17 average maybe. Product product 2.106, but table ROI? Need compare with actual出票线 for 4关 maybe not given? They gave "2关 1.741、3关 1.791、5关 1.871、8关 1.971、9关 2.001" no 4关? Table says "⚠️ ... 实际出票线是：2关 1.741、3关 1.791、5关 1.871、8关 1.971、9关 2.001—别卡着 1.538 出票." Missing 4关 maybe? Maybe from table earlier: 每腿最低 v̂ for 4 legs =1.114, but that's for Π v =1.538. The actual safety line maybe they have safeties for 2,3,5,8,9, but not 4? Could interpolate? The "实际出票线" is maybe Π v threshold. For 4关 maybe maybe 1.83? In table not explicitly listed but 3关 1.791, 5关 1.871, 4关 likely around 1.83. Product 2.106 >1.83, so okay.

If we multi-select 奥勒松 H+D, effective x for that leg is average 1.16; product = 1.16*1.25*1.21*1.17 = 2.054; ROI =0.335, slightly lower. Still okay. Cost 4元. Both fine.

Should we multi-select? The instruction says "只有确认某 1~2 条腿「边际真实但胜率不高」且成本放得下时，才用 N过(N−1)" This is about容错, not multi-select. For multi-select, they allow when multiple directions each ✅ and you unsure which wins. Do we "unsure"? We are not actually predicting, but both H and D are ✅; maybe multi-select is sensible because neither is clear favorite? Market A is favorite, but A not ✅; we avoid A. H and D both underdogs, both value. We might not know whether H or D, so double. This fits typical scenario. So I lean multi-select H+D for 奥勒松. That makes ticket 4串1 with 2 combinations, total 2注 cost 4元. But we need output picks for that leg ["H","D"] with p for both.

Need perhaps include all four matches. Are there exactly 4? Yes. We could also choose only 3 legs if one suspect? But minimum 4 suggests need 4. The only other gl0 ✅? We accounted. So no more. We could perhaps include 5? No. So 4串1. If m_star is 4, good. If need 5? There's only 4, cannot. But if m_star=4, okay. Hard rule says ≥3, so okay.

Need ensure no duplicate same match? We have unique matches. Good.

Need ensure "一场最多一条腿" – 奥勒松 as one leg with double okay. Not duplicate.

Need produce JSON. Need include fields:
{
  "legs": [
    {
      "lota_id": "Lota4467503",
      "picks": ["H","D"],
      "p": {"H": 0.30, "D": 0.25},
      "factors": [],
      "why": "H/D均过门且x接近，双选锁方向"
    },
    {
      "lota_id": "Lota4477391",
      "picks": ["D"],
      "p": {"D": 0.29},
      "factors": [],
      "why": "D侧x=1.25过门"
    },
    {
      "lota_id": "Lota4468146",
      "picks": ["A"],
      "p": {"A": 0.37},
      "factors": [],
      "why": "A侧x=1.21过门"
    },
    {
      "lota_id": "Lota4467565",
      "picks": ["A"],
      "p": {"A": 0.40},
      "factors": [],
      "why": "A侧x=1.17过门"
    }
  ],
  "ticket": "4串1",
  "empty": false,
  "reason": "4条gl0过门腿全部纳入，奥勒松双选H+D，单票2注4元"
}

Need check order: sorted by confidence strong to weak. I placed 奥勒松 first, then SJK, TPS, 腓特烈斯塔. But maybe confidence sort: I might put single picks first? Which leg is strongest? The x isn't confidence. Need perhaps use p values: 腓特烈斯塔 A p 0.40 highest, then TPS A 0.37, then SJK D 0.29, then 奥勒松 double? But confidence of double leg not directly probability. Hmm.

The instruction: "legs 按信心从强到弱". We need justify order. We can perhaps sort by our estimated p (for single picks) but double leg has two p values; maybe use max p? For 奥勒松 H p0.30, D p0.25, max 0.30. That would rank: 腓特烈斯塔 0.40, TPS 0.37, 奥勒松 0.30, SJK 0.29. But if we rank by combined probability of selected outcomes (H+D=0.55) then 奥勒松 first. Which is more appropriate? The leg with double picks has higher probability to have at least one selected outcome, so maybe higher confidence. But the ticket requires both? Actually for a leg with double, the ticket condition is either H or D, so probability of leg being satisfied is p_H + p_D = 0.55. That is higher than 0.40. So perhaps 奥勒松 should be first. But then the other legs single probabilities maybe 0.40,0.37,0.29. That order seems okay. So 奥勒松 first.

Could also rank by x or stage1: stage1 A for 3 legs, D for one, H for one? Not useful.

Need maybe include why strings ≤20字. My strings: "H/D均过门且x接近，双选锁方向" = maybe 17 chars; okay.

Need ensure p values not exceed cap: For 奥勒松 H p0.30 <= 0.376, D0.25 <=0.314; okay. For SJK D 0.29 <= market D 0.289? Wait market p̂ D=0.289, cap 1.25*0.289=0.361; 0.29 within; okay. TPS A market p̂=0.370, cap=0.4625; 0.37 within. 腓特烈斯塔 A market p̂=0.402, cap=0.5025; 0.40 within. So safe.

Need maybe include factors empty. But the prompt says factors must be from factor list; maybe empty is acceptable. But we need perhaps use factors? There is no factor list. Maybe we can use none. The "why" can mention x过门, not factors. Could mention "N/A" but JSON arrays fine empty.

Need perhaps include ticket "4串1" exactly. Could also "4串1" but multi-select leg yields 2 bets; that's still 4串1. The ticket field maybe "4串1" is okay. Could specify "4串1（奥勒松双选）" but likely not needed. Use "4串1".

Need verify if any other gl0 with x ✅? Let's re-scan:
- Lota4467565 x A1.17 ✅
- Lota4468144 x D1.05 not ✅; no.
- Lota4477391 D1.25 ✅
- Lota4467093 A1.08 not ✅
- Lota4468146 A1.21 ✅
- Lota4467503 H1.19,D1.13 ✅
Any Lota4480398 etc x1.00 no. So exactly 4 matches. But one match 奥勒松 has two picks, so 4 legs. Good.

Potential issue: The user says "候选多于 N 时，不要在 x 上择优（宁可均匀挑）". Here we have exactly 4 candidate matches? Actually candidate rows with gl0 and x>=1.10 are 4 rows. So we can take all. Fine.

Need maybe consider the Nordic leagues: All are Nordic, okay. No conflict.

Need consider tax. Single note payout if all hits: Let's compute SP product. Our odds: 
腓特烈斯塔 A odds? Candidate row: 2.92? Wait Lota4467565 赔率 H/D/A=2.64/3.56/2.92. A odds 2.92.
SJK D odds 4.31.
TPS A odds 3.27.
奥勒松 H odds 3.94, D odds 4.51. If double, two combos:
Combo1: H odds 3.94 * 4.31 * 3.27 * 2.92 =?
Combo2: D odds 4.51 * 4.31 * 3.27 * 2.92 =?
Product for Combo1: 3.94*4.31=16.98; *3.27=55.5; *2.92=162.1. SP product 162 > 7692? That's not enough to hit tax threshold? Actually 162 is far below 10000. Wait 10000 yuan threshold is payout per 2 yuan note? Need recall: single note gross > 10000. Gross = 2 * Π SP? Actually公式 says single注毛额 >10000. If SP product =162, gross = 2*162*0.65? No, 奖金 = 2*Π SP *0.65? The formula says 中奖奖金=每注2元×ΠSP×0.65. If ΠSP=162, 奖金=2*162*0.65=210.6元, below 10000. So no tax issue. Wait earlier "单注毛额 > 10000 元" maybe gross before 0.65? Let's see later says "毛额 10000~12500 是净亏区，单注 SP 连乘要么 ≤7692，要么 ≥9615." So single注毛额 likely = 2*ΠSP? Actually if 毛额 = 2*ΠSP (before 0.65?) Then threshold ΠSP <= 7692 corresponds毛额 = 2*7692=15384? Hmm. They mention "毛额 >10000" and then "单注 SP 连乘要么 ≤7692，要么 ≥9615" to avoid tax. For gross? Need check: If 毛额 = 2 * ΠSP? Suppose gross payout before 0.65? Actually North single odds maybe payout = 2 * SP * 0.65; they might call毛额 before 0.65? The lines "单注毛额 > 10000 元要缴 20% 个税（全额计税）：毛额 10000~12500 是净亏区，单注 SP 连乘要么 ≤7692，要么 ≥9615。" suggests SP product thresholds 7692 and 9615 correspond to毛额 maybe 2*SP*0.65? Let's compute: if SP=7692, 毛额=2*7692*0.65=10000? 2*7692*0.65=9999.6 ≈10000. SP=9615 -> 2*9615*0.65=12499.5 ≈12500. So yes gross = 2*ΠSP*0.65. So thresholds. Our SP product 162, gross = 210, no tax. So fine.

Need maybe calculate expected ROI using x; but not required in JSON. However reason maybe should mention positive expected. Could compute overall product maybe: if using all singles average maybe product ~2.106; expected ROI ~37%, if double slightly lower ~33%. Need maybe include in reason: "4关正期望，约+30%" but must be accurate. Let's compute more accurately with double average x.

Legs:
- 奥勒松 double avg x = (1.19+1.13)/2 = 1.16
- SJK D x = 1.25
- TPS A x = 1.21
- 腓特烈 A x = 1.17

Product = 1.16 * 1.25 * 1.21 * 1.17.
Compute 1.25*1.21=1.5125; *1.17=1.769625; *1.16=2.052765. ROI = 0.65*2.052765 - 1 = 1.3343 - 1 = 0.3343 = 33.4%. So about +33%. But note actual SP may drift; safety margin maybe product > required 4关 line. The required 4关 line maybe? They didn't give explicit but we can use 1.83? Product 2.05 >1.83. Okay.

Could we mention "4关正期望约+33%" in reason? Reason field can be brief; maybe include "4关x乘积2.05，ROI≈+33%". But maybe not needed. The JSON schema includes reason string, likely concise. I'll include in reason maybe: "4条gl0过门腿全部纳入，奥勒松双选H+D，单票2注4元，4关x乘积2.05 ROI≈+33%." That is longer but okay.

Need adhere to output only JSON. No markdown.

Potential issue: The leg Lota4467503 stage1 H, but we multi-select H,D. Is there any risk engine might treat as violating "stage1 A/H"? No, stage1 is not veto; it's informational. The engine's gate uses x✅. So okay.

Need maybe include factors []. Good.

Need perhaps not include all four if one has p lower? But all are valid.

Check if there is any "stage1 SKIP" for gl0 x all 1.00; no.

Now produce final JSON.

But let's think deeply: The user prompt includes "北单串关分析 agent（bcl狗）" and strategy. We need ensure we comply exactly. The candidate list includes `x(H/D/A)=H1.00/D1.00/A1.00` for some, no ✅. We shouldn't pick those. Good.

Need maybe consider that for Lota4467503, H and D both ✅, but stage1 is H. Could we also select A? A x 0.85, not allowed. So no.

Need maybe consider that the user says "多选（一条腿买 2~3 个方向）是你的正当决策，但有条件：只有当每个被选方向各自都过门（x≥θ）时才可以多选 —— 引擎按被选方向的平均 x 判这条腿过不过门。成本 ×2/×3、该腿边际与 x 都按平均算。" For 奥勒松, H x1.19, D x1.13, both >=1.10, so okay.

Potential issue with p values for H and D when multi-select: The p sum H+D should be <1, okay 0.55. But if we give p equal market, then our estimated v for H =0.30*3.94=1.182, D=0.25*4.51=1.127, average 1.154. That aligns with x avg 1.16. Good.

For single legs:
- SJK D p=0.29, odds 4.31, v=1.25 exactly? 0.29*4.31=1.2499.
- TPS A p=0.37, odds 3.27, v=1.2099.
- 腓特烈 A p=0.40, odds 2.92, v=1.168.

All v>1.

Need maybe ensure "市场 p̂ 封顶" for 腓特烈 A: market p̂=0.402, 1.25*0.402=0.5025; p=0.40 <0.5025. okay.

Now final answer JSON.

Need maybe not include extra fields. The schema includes empty, reason, etc. We'll include all.

Final JSON:
{"legs":[{"lota_id":"Lota4467503","picks":["H","D"],"p":{"H":0.30,"D":0.25},"factors":[],"why":"H/D均过门且x接近，双选锁方向"},{"lota_id":"Lota4477391","picks":["D"],"p":{"D":0.29},"factors":[],"why":"D侧x=1.25过门"},{"lota_id":"Lota4468146","picks":["A"],"p":{"A":0.37},"factors":[],"why":"A侧x=1.21过门"},{"lota_id":"Lota4467565","picks":["A"],"p":{"A":0.40},"factors":[],"why":"A侧x=1.17过门"}],"ticket":"4串1","empty":false,"reason":"4条gl0过门腿全部纳入，奥勒松双选H+D，单票2注4元，4关x乘积2.05 ROI≈+33%"}

Check if reason strings contain any forbidden? It's okay.

But wait: The leg order: I placed 奥勒松 first, SJK second, TPS third, 腓特烈 fourth. But perhaps by confidence: double leg p sum 0.55 first; SJK p=0.29 but x=1.25 highest; TPS p=0.37; 腓特烈 p=0.40. Hmm, if sorted by confidence, maybe 奥勒松, TPS, 腓特烈, SJK? Because SJK p 0.29 lower than TPS 0.37? But SJK x higher. The instruction "legs 按信心从强到弱" not explicitly defined. Could sort by p sum for double, and p for singles: 奥勒松 0.55 > 腓特烈 0.40 > TPS 0.37 > SJK 0.29. My order currently has 奥勒松, SJK, TPS, 腓特烈. That is not strictly by p. Maybe should adjust to be consistent. But it's not critical. However to avoid being flagged, maybe order by p descending: 奥勒松 (sum 0.55), 腓特烈 (0.40), TPS (0.37), SJK (0.29). But then we would have 腓特烈 second. Which is more "confidence"? Could do that. Let's adjust order accordingly: 
1 奥勒松 double
2 腓特烈 A
3 TPS A
4 SJK D

But then why earlier maybe stage1 ranking? No.

Let's revise legs order:
- Lota4467503 picks H,D p H0.30 D0.25
- Lota4467565 picks A p0.40
- Lota4468146 picks A p0.37
- Lota4477391 picks D p0.29

This is more defensible as sorted by probability of selected outcome (for double sum, for single p). However SJK has highest x but lower confidence. That's okay.

Need ensure p for SJK D 0.29 and market D 0.289, cap 0.361, okay.

Let's output with new order. Need maybe still include why strings.

Final answer.
[/thinking]

{"legs":[{"lota_id":"Lota4467503","picks":["H","D"],"p":{"H":0.30,"D":0.25},"factors":[],"why":"H/D均过门且x接近，双选锁方向"},{"lota_id":"Lota4467565","picks":["A"],"p":{"A":0.40},"factors":[],"why":"A侧x=1.17过门"},{"lota_id":"Lota4468146","picks":["A"],"p":{"A":0.37},"factors":[],"why":"A侧x=1.21过门"},{"lota_id":"Lota4477391","picks":["D"],"p":{"D":0.29},"factors":[],"why":"D侧x=1.25过门"}],"ticket":"4串1","empty":false,"reason":"4条gl0过门腿全部纳入，奥勒松双选H+D，单票2注4元，4关x乘积2.05 ROI≈+33%"}
