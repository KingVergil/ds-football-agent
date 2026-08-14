/**
 * 纯 JS 结算数学（harness_js_reconstruction.md §3.3）。
 * 原样移植 python-engine/src/store.py::settle_order / _settle_hk_quarter。
 *
 * 语义：
 *   - score = "主:客"（hg:ag）
 *   - handicap 是「主队视觉」：正=主受、负=主让（与 order.handicap 存储一致）
 *   - 亚盘 adj = diff + handicap；H 赢盘 = adj>0，A 赢盘 = adj<0，adj==0 走水
 *   - 大小球 total == handicap 走水；over = total>handicap，under = total<handicap
 *   - quarter-ball（hc % 0.5 != 0）拆 hc±0.25 两半各自结算
 *   - hit: true 全赢 / false 全输 / null 走水或半赢半输
 */

/** 北京时间 ISO（与 Python datetime.now() 同口径，不依赖宿主机时区）。 */
export function beijingNowIso() {
  const d = new Date(Date.now() + 8 * 3600 * 1000);
  return d.toISOString().slice(0, 19); // "YYYY-MM-DDTHH:mm:ss"
}

function round2(x) {
  return Math.round(x * 100) / 100;
}

/** 港赔 quarter-ball 结算（亚盘 & 大小球通用）。 */
export function settleHkQuarter(betType, pick, handicap, odds, betSize, diff, total) {
  const isQuarter = Math.abs(handicap % 0.5) > 0.001;

  if (!isQuarter) {
    let win;
    if (betType === "亚盘") {
      const adj = diff + handicap;
      if (adj === 0) return { hit: null, returnAmount: betSize, profit: 0 };
      win = pick === "H" ? adj > 0 : adj < 0;
    } else {
      // 大小球
      if (total === handicap) return { hit: null, returnAmount: betSize, profit: 0 };
      win = pick === "over" ? total > handicap : total < handicap;
    }
    if (win) {
      return { hit: true, returnAmount: betSize * (1 + odds), profit: betSize * odds };
    }
    return { hit: false, returnAmount: 0, profit: -betSize };
  }

  // quarter-ball：拆两半
  const hc1 = handicap - 0.25;
  const hc2 = handicap + 0.25;
  const halfResult = (hc) => {
    if (betType === "亚盘") {
      const adj = diff + hc;
      if (adj === 0) return "push";
      if (pick === "H") return adj > 0 ? "win" : "lose";
      return adj < 0 ? "win" : "lose";
    }
    if (total === hc) return "push";
    if (pick === "over") return total > hc ? "win" : "lose";
    return total < hc ? "win" : "lose";
  };

  const r1 = halfResult(hc1);
  const r2 = halfResult(hc2);
  const halfBet = betSize / 2;
  let ret = 0;
  for (const r of [r1, r2]) {
    if (r === "win") ret += halfBet * (1 + odds);
    else if (r === "push") ret += halfBet;
  }
  const profit = ret - betSize;
  const wins = (r1 === "win") + (r2 === "win");
  const losses = (r1 === "lose") + (r2 === "lose");
  const hit = wins === 2 ? true : losses === 2 ? false : null;
  return { hit, returnAmount: ret, profit };
}

/**
 * 结算一单：用比分判定，返回更新后的 order（含 hit/return_amount/profit/score/settled_at）。
 * 不落库、不动资金——纯函数，副作用由调用方（storage 域 + capital 更新）负责。
 */
export function settleOrder(order, score, settledAt = beijingNowIso()) {
  if (!/^\d+:\d+$/.test(score)) throw new Error(`比分格式错误: ${score}`);
  const [hg, ag] = score.split(":").map(Number);
  const diff = hg - ag;
  const total = hg + ag;

  const betType = order.bet_type || "";
  const pick = order.pick || "";
  const handicap = Number(order.handicap || 0);
  const odds = Number(order.odds || 0);
  const betSize = Number(order.bet_size || 100);

  let hit;
  let returnAmount;
  let profit;

  if (betType === "胜平负" || betType === "让球胜平负") {
    let actual;
    if (betType === "让球胜平负") {
      const glRaw = order.goal_line;
      const gl = Number(glRaw != null ? glRaw : (order.handicap || 0));
      const adj = diff + gl;
      actual = adj > 0 ? "H" : adj < 0 ? "A" : "D";
    } else {
      actual = hg > ag ? "H" : hg < ag ? "A" : "D";
    }
    hit = pick === "H" || pick === "D" || pick === "A" ? pick === actual : null;
    if (hit == null) {
      returnAmount = betSize;
      profit = 0;
    } else if (hit === true) {
      returnAmount = betSize * odds;
      profit = returnAmount - betSize;
    } else {
      returnAmount = 0;
      profit = -betSize;
    }
  } else {
    const r = settleHkQuarter(betType, pick, handicap, odds, betSize, diff, total);
    hit = r.hit;
    returnAmount = r.returnAmount;
    profit = r.profit;
  }

  return {
    ...order,
    hit,
    return_amount: round2(returnAmount),
    profit: round2(profit),
    score,
    settled_at: settledAt,
  };
}
