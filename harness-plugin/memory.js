/**
 * 因子记忆 + 历史反思注入（对齐 Python prompt_builder.py + memory.py 的 format_for_prompt）。
 *
 * memoryQuery(handles, dog, {day, getMatchName}) 读 ds_roles / ds_factors / ds_reflections，
 * 复刻 baseline-v1 memory_config 的注入文本：
 *   include_summary    订单统计摘要（summary_text）
 *   include_streaks    连胜/连败（streak_text）
 *   max_recent_orders  最近 20 条已结算订单（recent_text）
 *   include_factor_perf 因子表现：负例护栏 + 活跃因子 + 观察 + 休眠计数（perf_text）
 *   include_reflections 历史反思最近 5 条（ReflectionMemory.format_for_prompt）
 *   include_settlement_review 昨日结算回顾（_format_settlement_review）
 *
 * 因子自适应筛选复刻 factor_select.py::factor_profile（样本窗 + 指数衰减 + 自适应休眠）。
 */

const FACTOR_SAMPLE_WINDOW = 6;
const FACTOR_DECAY_HALF_LIFE_DAYS = 7.0;
const FACTOR_INTERVAL_MULTIPLIER = 3.0;
const FACTOR_MAX_MAIN = 12;
const FACTOR_SMALL_SAMPLE = 5;

/** "YYYY-MM-DD" → UTC 午夜 Date（时区无关）。 */
function parseDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || "");
  if (!m) return null;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
}

function daysBetween(a, b) {
  return Math.round((a - b) / 86400000);
}

function round1(x) {
  return Math.round(x * 10) / 10;
}

/**
 * 因子在「最近 N 单」上的自适应画像（复刻 factor_select.py::factor_profile）。
 * 无历史 → null。
 */
function factorProfile(stats, nowDate) {
  const hist = stats.history || [];
  if (!hist.length) return null;
  const sorted = [...hist].sort((a, b) => (a.date || "").localeCompare(b.date || ""));
  const recent = sorted.slice(-FACTOR_SAMPLE_WINDOW);

  const weights = [];
  for (const h of recent) {
    const day = parseDate(h.date) || nowDate;
    const age = Math.max(daysBetween(nowDate, day), 0);
    weights.push(Math.pow(0.5, age / FACTOR_DECAY_HALF_LIFE_DAYS));
  }
  const wsum = weights.reduce((s, x) => s + x, 0) || 1.0;
  const wReturn =
    recent.reduce((s, h, i) => s + (h.return_ratio || 0) * weights[i], 0) / wsum;
  const hits = recent.reduce(
    (s, h) => s + (h.hit === true ? 1 : h.hit === 0.5 ? 0.5 : 0),
    0,
  );
  const n = recent.length;

  // 触发间隔 → 自适应休眠（按因子自身频率）
  const dates = sorted.map((h) => (h.date || "").slice(0, 10)).filter(Boolean);
  let intervalDays = null;
  if (dates.length >= 2) {
    const diffs = [];
    for (let i = 1; i < dates.length; i++) {
      const a = parseDate(dates[i - 1]);
      const b = parseDate(dates[i]);
      if (a && b) diffs.push(daysBetween(b, a));
    }
    if (diffs.length) {
      const avg = diffs.reduce((s, x) => s + x, 0) / diffs.length;
      intervalDays = avg >= 1 ? avg : null;
    }
  }
  let lastAgeDays = null;
  const lastSeen = stats.last_seen || "";
  if (lastSeen) {
    const d = parseDate(lastSeen);
    if (d) lastAgeDays = daysBetween(nowDate, d);
  }
  let dormant = false;
  if (lastAgeDays !== null) {
    const base = intervalDays || FACTOR_DECAY_HALF_LIFE_DAYS;
    dormant = lastAgeDays > base * FACTOR_INTERVAL_MULTIPLIER;
  }

  return { n, hits, w_return: wReturn, dormant };
}

/** 复刻 FactorMemory.selected_active：返回 { main, aux, dormantCount }。 */
function selectedActive(factorPerf, nowDate) {
  const main = [];
  const aux = [];
  let dormantCount = 0;
  for (const [fid, s] of Object.entries(factorPerf || {})) {
    const status = s.status || "active";
    if (status === "retired") continue;
    if (status === "dormant") {
      dormantCount += 1;
      continue;
    }
    const p = factorProfile(s, nowDate);
    if (!p) continue;
    if (p.dormant) {
      dormantCount += 1;
      continue;
    }
    if (p.n >= 2 && p.w_return > 0) main.push([fid, s, p]);
    else aux.push([fid, s, p]);
  }
  main.sort((a, b) => b[2].w_return - a[2].w_return);
  aux.sort((a, b) => (b[2]?.w_return || 0) - (a[2]?.w_return || 0));
  return { main: main.slice(0, FACTOR_MAX_MAIN), aux: aux.slice(0, 6), dormantCount };
}

/** 复刻 FactorMemory.perf_text：负例护栏 + 活跃因子 + 观察 + 休眠计数。 */
function perfText(factorPerf, nowDate) {
  if (!factorPerf || !Object.keys(factorPerf).length) return "";
  const { main, aux, dormantCount } = selectedActive(factorPerf, nowDate);
  const retired = Object.entries(factorPerf)
    .filter(([, s]) => s.status === "retired")
    .sort((a, b) => -(Number(b[1].profit) || 0) + (Number(a[1].profit) || 0))
    .slice(0, 8);

  const lines = [];
  if (retired.length) {
    lines.push("🪦 已证伪模式（负例护栏，勿用）:");
    for (const [fid, s] of retired) {
      lines.push(`  ❌ ${fid} (累计${(Number(s.profit) || 0).toFixed(0)})`);
    }
  }
  if (main.length) {
    lines.push("📐 活跃因子（按自适应得分）:");
    for (const [fid, s, p] of main) {
      const small = p.n < FACTOR_SMALL_SAMPLE ? ` ⚠️样本少(${p.n}单)` : "";
      lines.push(
        `  ${fid} [近${p.n}单 命中${p.hits}/${p.n} 加权回报${p.w_return.toFixed(2)}${small}]`,
      );
      const desc = s.desc || "";
      if (desc) lines.push(`     ${desc.slice(0, 80)}`);
    }
  }
  if (aux.length) {
    lines.push("📉 观察（样本不足/走弱，勿重仓）:");
    for (const [fid, , p] of aux.slice(0, 15)) {
      lines.push(`  ⚠️ ${fid}: 近${p.n}单 加权回报${p.w_return.toFixed(2)}`);
    }
  }
  if (dormantCount) lines.push(`  (另有 ${dormantCount} 个休眠因子)`);
  return lines.join("\n");
}

/** 复刻 ReflectionMemory.format_for_prompt：最近 5 条反思（最新在前，权重最高）。 */
function reflectionsText(reflections) {
  const list = reflections?.reflections || [];
  if (!list.length) return "";
  const lines = ["## 📝 历史反思（Alpha 因子积累）", ""];
  lines.push(
    "⚠️ 优先按最近反思总结的规律选场——最新反思权重最高，判断信号时先套用最近反思的因子，不要各因子均摊。",
    "",
  );
  for (const r of list.slice(-5).reverse()) {
    lines.push(`### ${r.date}`);
    lines.push(r.reflection || "");
    lines.push("");
  }
  return lines.join("\n");
}

/** 复刻 SlugMemory.slug_perf_text：数据段表现（盈利日/使用天）。 */
function slugPerfText(slugMemory) {
  const stats = slugMemory?.slug_stats || {};
  if (!Object.keys(stats).length) return "";
  const items = Object.entries(stats)
    .sort((a, b) => (b[1].appearances || 0) - (a[1].appearances || 0));
  const lines = ["📡 数据段表现（盈利日/使用天）:"];
  for (const [slug, s] of items.slice(0, 8)) {
    lines.push(`  ${slug}: 盈利${s.profitable_days || 0}/${s.appearances || 0}天`);
  }
  return lines.join("\n");
}

/** 复刻 OrderMemory.summary_text：按玩法统计命中率/盈亏/ROI。 */
function ordersSummaryText(settledOrders) {
  if (!settledOrders.length) return "(无订单记忆)";
  const byType = {};
  for (const o of settledOrders) {
    const bt = o.bet_type || "其他";
    const s = byType[bt] || (byType[bt] = { total: 0, hit: 0, miss: 0, push: 0, profit: 0 });
    s.total += 1;
    if (o.hit === true) s.hit += 1;
    else if (o.hit === false) s.miss += 1;
    else s.push += 1;
    s.profit += Number(o.profit) || 0;
  }
  const lines = ["📊 订单统计"];
  for (const bt of ["胜平负", "亚盘", "大小球"]) {
    const s = byType[bt];
    if (!s || s.total === 0) continue;
    const denom = s.total - s.push;
    const hitRate = denom > 0 ? round1((s.hit / denom) * 100) : 0;
    const roi = s.total > 0 ? round1(s.profit / s.total) : 0;
    lines.push(
      `  ${bt}: ${s.total}单 命中${hitRate}% 盈亏${s.profit.toFixed(0)} ROI${roi}%`,
    );
  }
  const totalOrders = settledOrders.length;
  const totalProfit = settledOrders.reduce((s, o) => s + (Number(o.profit) || 0), 0);
  const totalRoi = totalOrders > 0 ? round1(totalProfit / totalOrders) : 0;
  lines.push(`  总计: ${totalOrders}单 总盈亏${totalProfit.toFixed(0)} ROI${totalRoi}%`);
  return lines.join("\n");
}

/** 复刻 OrderMemory.streak_text。 */
function streakText(settledOrders) {
  let win = 0;
  let lose = 0;
  for (let i = settledOrders.length - 1; i >= 0; i--) {
    const h = settledOrders[i].hit;
    if (h === true) {
      if (lose === 0) win += 1;
      else break;
    } else if (h === false) {
      if (win === 0) lose += 1;
      else break;
    }
    // push/走水不打断
  }
  const parts = [];
  if (win >= 2) parts.push(`🔥 连胜 ${win} 场`);
  if (lose >= 2) parts.push(`🔻 连败 ${lose} 场`);
  return parts.join(" | ");
}

/** 复刻 OrderMemory.recent_text。 */
function recentOrdersText(settledOrders, n = 20) {
  const recent = settledOrders.slice(-n);
  if (!recent.length) return "(无最近订单)";
  const lines = ["📋 最近订单:"];
  for (const o of recent) {
    const h = o.hit === true ? "✅" : o.hit === false ? "❌" : "➖";
    lines.push(
      `  ${h} ${o.bet_type} ${o.pick} @${(Number(o.odds) || 0).toFixed(2)} ` +
        `bet ${(Number(o.bet_size) || 0).toFixed(0)} → ${(Number(o.profit) || 0).toFixed(0)}`,
    );
  }
  return lines.join("\n");
}

/** 复刻 prompt_builder._format_settlement_review：昨日结算回顾。 */
function settlementReviewText(settledOrders, day, getMatchName) {
  if (!settledOrders.length || !day) return "";
  const prev = parseDate(day);
  if (!prev) return "";
  const prevDate = new Date(prev.getTime() - 86400000).toISOString().slice(0, 10);
  const settled = settledOrders.filter((o) => (o.settled_at || "").startsWith(prevDate));
  if (!settled.length) return "";

  const lines = [`## 昨日结算回顾 (${prevDate})`, ""];
  lines.push("| # | 比赛 | 类型 | pick | 赔率 | 金额 | 结果 | 盈亏 |");
  lines.push("|---|------|------|------|------|------|------|------|");
  let hit = 0;
  let miss = 0;
  let push = 0;
  let totalPnl = 0;
  settled.forEach((o, i) => {
    const name = (getMatchName && getMatchName(o.lota_id)) || o.lota_id || "?";
    let icon;
    if (o.hit === true) {
      icon = "✅ 命中";
      hit += 1;
    } else if (o.hit === false) {
      icon = "❌ 未中";
      miss += 1;
    } else {
      icon = "➖ 走水";
      push += 1;
    }
    const profit = Number(o.profit) || 0;
    totalPnl += profit;
    lines.push(
      `| ${i + 1} | ${String(name).slice(0, 22)} | ${o.bet_type || ""} | ${o.pick || ""} | ` +
        `${(Number(o.odds) || 0).toFixed(2)} | ${(Number(o.bet_size) || 0).toFixed(0)} | ` +
        `${icon} | ${profit.toFixed(0)} |`,
    );
  });
  const total = settled.length;
  const denom = total - push;
  const hitRate = denom > 0 ? `${hit}/${denom}` : `${hit}/${total}`;
  lines.push("");
  lines.push(
    `昨日PnL: ${totalPnl.toFixed(0)} | 命中: ${hitRate} | 未中: ${miss}` +
      (push > 0 ? ` | 走水: ${push}` : ""),
  );
  return lines.join("\n");
}

/**
 * 主入口：读 storage 域，返回该狗的记忆注入文本（对齐 baseline-v1 memory_config）。
 * @param {object} handles  setupDomains 返回的 name → Promise<Domain>
 * @param {string} dog      狗名
 * @param {object} opts     { day?: "YYYY-MM-DD"（分析日，作为因子衰减/休眠的 now；空=今天）,
 *                            getMatchName?: (lota_id) => string }
 */
export async function memoryQuery(handles, dog, opts = {}) {
  const { day, getMatchName } = opts;
  const [rolesDomain, factorsDomain, reflectionsDomain, slugsDomain] = await Promise.all([
    handles["ds_roles"],
    handles["ds_factors"],
    handles["ds_reflections"],
    handles["ds_slugs"],
  ]);

  const role = rolesDomain.table("roles").get(dog);
  const fm = factorsDomain.table("factors").get(dog);
  const rm = reflectionsDomain.table("reflections").get(dog);
  const sm = slugsDomain.table("slugs").get(dog);

  if (!role) {
    return { error: `角色 ${dog} 不存在（storage 域未迁移？先跑 ds_migrate_storage）` };
  }

  const settled = (role.orders || [])
    .filter((o) => o.settled_at)
    .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
  const nowDate = parseDate(day) || new Date();

  const blocks = [];
  blocks.push(ordersSummaryText(settled));
  const st = streakText(settled);
  if (st) blocks.push(st);
  blocks.push(recentOrdersText(settled, 20));
  const pt = perfText(fm?.factor_perf || {}, nowDate);
  if (pt) blocks.push(pt);
  const spt = slugPerfText(sm);
  if (spt) blocks.push(spt);
  const rt = reflectionsText(rm);
  if (rt) blocks.push(rt);
  const srv = settlementReviewText(settled, day, getMatchName);
  if (srv) blocks.push(srv);

  return {
    user: dog,
    day: day || null,
    settled_count: settled.length,
    factor_count: Object.keys(fm?.factor_perf || {}).length,
    reflection_count: (rm?.reflections || []).length,
    slug_count: Object.keys(sm?.slug_stats || {}).length,
    text: blocks.filter(Boolean).join("\n\n"),
  };
}
