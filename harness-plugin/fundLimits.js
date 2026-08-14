/**
 * 资金管理硬约束（harness_js_reconstruction.md §2.8 step5）。
 * 原样移植 python-engine/src/fund_limits.py。
 */
export const AGENT_LIMITS = {
  alpha2狗: { max_exposure_pct: 40.0, truncate: true, max_orders: null, min_orders: null },
  梭哈2狗: { max_exposure_pct: null, truncate: false, max_orders: null, min_orders: 2 },
  梭哈3狗: { max_exposure_pct: null, truncate: false, max_orders: null, min_orders: 2 },
};

export function orderLimitsFor(agentName) {
  const d = AGENT_LIMITS[agentName] || {};
  const limits = {
    max_exposure_pct: d.max_exposure_pct ?? null,
    truncate: !!d.truncate,
    max_orders: d.max_orders ?? null,
    min_orders: d.min_orders ?? null,
  };
  limits.enabled = [limits.max_exposure_pct, limits.max_orders, limits.min_orders].some(
    (v) => v != null && v !== false && v !== 0,
  );
  return limits;
}

/**
 * 按配置对 LLM 订单依次应用：单数上限 → 总仓上限（截断/缩放）→ 保底注数。
 * 严格按 LLM 输出顺序处理（不重排、不独立打分）。返回 { kept, dropped }。
 */
export function applyFundLimits(limits, orders, capital) {
  if (!limits.enabled) return { kept: orders.slice(), dropped: [] };

  const maxPct = limits.max_exposure_pct;
  const truncate = limits.truncate;
  const maxOrders = limits.max_orders;
  const minOrders = limits.min_orders;

  let kept = orders.slice();
  let dropped = [];

  // 1. 单数上限：按序保留前 max_orders
  if (maxOrders && kept.length > maxOrders) {
    dropped = dropped.concat(kept.slice(maxOrders));
    kept = kept.slice(0, maxOrders);
  }

  // 2. 总仓上限：基数 = 余额（锁定仓位不占额度）
  if (maxPct && kept.length) {
    const cap = (capital * maxPct) / 100;
    const total = kept.reduce((s, o) => s + Number(o.bet_size || 0), 0);
    if (total > cap) {
      if (truncate) {
        let acc = 0;
        let cut = null;
        for (let i = 0; i < kept.length; i++) {
          const s = Number(kept[i].bet_size || 0);
          if (acc + s > cap) {
            cut = i;
            break;
          }
          acc += s;
        }
        if (cut != null) {
          dropped = dropped.concat(kept.slice(cut));
          kept = kept.slice(0, cut);
        }
      } else {
        const scale = cap / total;
        kept = kept.map((o) => ({ ...o, bet_size: Math.trunc(Number(o.bet_size || 0) * scale) }));
      }
    }
  }

  // 3. 保底注数：截断后不足时，从丢弃单按序补回
  if (minOrders && kept.length < minOrders && dropped.length) {
    const need = minOrders - kept.length;
    const restored = dropped.slice(0, need);
    kept = kept.concat(restored);
    dropped = dropped.slice(need);
    if (maxPct) {
      const cap = (capital * maxPct) / 100;
      const total = kept.reduce((s, o) => s + Number(o.bet_size || 0), 0);
      if (total > cap) {
        const scale = cap / total;
        kept = kept.map((o) => ({ ...o, bet_size: Math.trunc(Number(o.bet_size || 0) * scale) }));
      }
    }
  }

  return { kept, dropped };
}
