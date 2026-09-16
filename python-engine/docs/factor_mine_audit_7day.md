# 两轴候选因子 · 逐 worker 引擎复核

共 55 条因子｜**完全复现 26（47%）**｜接近 22｜偏离 7｜不可求值 0

| worker | 因子 | cond | 引擎 n | LLM n | 引擎 d_pp | LLM d_pp | 判定 |
|---|---|---|---|---|---|---|---|
| probe2_20260703_directiona | 最发散侧 | `disp >= disp.h 且 disp >= disp.d 且 disp >` | 10 | 11 | +5.4 | +2.8 | 接近 |
| probe2_20260703_directiona | 最发散弱侧 | `disp >= disp.h 且 disp >= disp.d 且 disp >` | 9 | 10 | +11.0 | +7.6 | 接近 |
| probe2_20260703_directiona | 最发散欧赔稳 | `disp >= disp.h 且 disp >= disp.d 且 disp >` | 7 | 8 | +9.2 | +5.1 | 接近 |
| probe2_20260703_directiona | 最发散弱侧正差 | `disp >= disp.h 且 disp >= disp.d 且 disp >` | 7 | 8 | +8.9 | +5.0 | 接近 |
| probe2_20260703_directiona | 中高稳赔或下沉发散 | `mp >= 0.40 且 eu.move <= 0.05 或 disp >= d` | 6 | 9 | +13.9 | +7.7 | 偏离 |
| probe2_20260703_directiona | 中高稳赔冷门发散 | `mp >= 0.40 且 eu.move <= 0.05 或 mp <= 0.3` | 9 | 12 | +10.3 | +6.6 | 偏离 |
| probe2_20260703_volatility | 高回扣腿缩水 | `x >= 1.2` | 9 | 8 | -23.6 | -23.1 | 接近 |
| probe2_20260703_volatility | 冷门侧赔率缩水 | `mp <= 0.33` | 9 | 10 | -16.0 | -7.6 | 偏离 |
| probe2_20260703_volatility | 高概率侧守住 | `mp >= 0.40` | 8 | 9 | -6.4 | -10.2 | 接近 |
| probe2_20260703_volatility | 冷门分散缩水 | `span >= 0.50 且 mp <= 0.33` | 7 | 8 | -11.5 | -1.6 | 偏离 |
| probe2_20260704_directiona | 主队中档低波动 | `side_is == H 且 mp >= 0.30 且 mp <= 0.40 且` | 6 | 8 | +16.5 | +14.8 | 接近 |
| probe2_20260704_directiona | 主队中档盘口不动 | `side_is == H 且 mp >= 0.30 且 mp <= 0.40 且` | 7 | 8 | +8.4 | +2.3 | 接近 |
| probe2_20260704_directiona | 主队欧赔下沉中档 | `side_is == H 且 mp <= 0.40 且 eu.move <= -` | 7 | 8 | +10.0 | +3.0 | 接近 |
| probe2_20260704_directiona | 主队非绝对热门 | `side_is == H 且 mp <= 0.50` | 17 | 18 | +17.3 | +13.6 | 接近 |
| probe2_20260704_volatility | 三侧赔率集中 | `span <= 0.487` | 16 | 16 | +2.2 | +0.0 | 接近 |
| probe2_20260704_volatility | 三侧结构离散 | `span > 0.695` | 20 | 20 | -5.7 | -0.1 | 接近 |
| probe2_20260704_volatility | 价格未膨胀腿 | `x <= 1.10` | 21 | 22 | -1.2 | -0.0 | 接近 |
| probe2_20260704_volatility | 价格膨胀腿 | `x > 1.20` | 12 | 11 | +0.2 | +0.1 | 接近 |
| probe2_20260704_volatility | 低赔率腿 | `od <= 3.0` | 12 | 12 | +5.4 | +0.1 | 接近 |
| probe2_20260704_volatility | 热门概率腿 | `mp >= 0.40` | 10 | 10 | -5.9 | -0.1 | 接近 |
| probe2_20260704_volatility | 冷门概率腿 | `mp <= 0.28` | 12 | 13 | -7.3 | -0.1 | 接近 |
| w7b_20260628_directional | 冷门侧欧赔下沉 | `rank_mp == 1 且 eu.move <= -0.04` | 13 | 8 | +8.6 | +20.4 | 偏离 |
| w7b_20260628_directional | 低市场概率侧 | `rank_mp == 1` | 15 | 9 | +9.6 | +14.6 | 偏离 |
| w7b_20260628_directional | 主队低离散 | `side_is == H 且 span <= 0.69` | 7 | 8 | +16.2 | +19.9 | 接近 |
| w7b_20260628_directional | 主队欧赔下沉 | `side_is == H 且 eu.move <= -0.04` | 7 | 8 | +12.1 | +18.5 | 接近 |
| w7b_20260628_volatility | 低总进球赔率下修 | `gs <= 2.8` | 9 | 8 | +0.2 | -6.0 | 接近 |
| w7b_20260705_volatility | 欧赔下行腿 | `eu.move <= -0.05` | 10 | 11 | -14.4 | -15.4 | 接近 |
| w7b_20260705_volatility | 浅盘口腿 | `ah.line >= -0.25 且 ah.line <= 0.25` | 11 | 8 | -14.9 | -10.4 | 偏离 |
| w7b_20260705_volatility | 中等概率腿 | `mp >= 0.30 且 mp <= 0.40` | 8 | 9 | -9.6 | -11.9 | 接近 |
