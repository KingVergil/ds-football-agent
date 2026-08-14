/**
 * 纯 JS place_orders（harness_js_reconstruction.md §2.7–2.8）。
 * 业务规则全部保留（对齐 python-engine/src/place_orders.py + node_place_orders）：
 *   1) 跳过 skip 单 → 2) 已开赛保护 → 3) 重复市场去重 (lota_id,bet_type)
 *   → 4) 资金折算(scale=余额/全金额) 或 FundManager 硬约束 → 5) 落库 + 更新 capital（资金不足 break）
 * 无 LLM、无 Python 桥；订单是结构化数组（工具 schema 校验），不再 parse_orders 正则。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beijingNowIso } from "./settle.js";
import { orderLimitsFor, applyFundLimits } from "./fundLimits.js";
import { extractOdds } from "./odds.js";

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function readMatches(cacheDir, date) {
  const raw = readJson(join(cacheDir, "matches", `${date}.json`));
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.matches)) return raw.matches;
  return [];
}

/**
 * 从 features 缓存的 compact_fet 解析权威亚盘盘口（主队视角：负=主让/正=主受）。
 * 落库前用它纠正 agent 传入的 handicap 符号，对齐 python-engine/src/order_utils.py
 * 的 _fill_order_odds_and_handicap（那里用 -float(asian.handicap) 强制纠正）。
 * 返回 null 表示无权威盘口可参考（缓存缺失/数据不全），此时保留 agent 原值。
 */
function readAuthoritativeHandicap(cacheDir, lotaId) {
  try {
    const raw = readJson(join(cacheDir, "features", `${lotaId}.json`));
    if (!raw || typeof raw !== "object") return null;
    const inner = raw.data && typeof raw.data === "object" ? raw.data : {};
    const compactFet = raw.compact_fet || inner.compact_fet || "";
    if (!compactFet) return null;
    const odds = extractOdds(compactFet);
    const hc = odds.asian && odds.asian.handicap;
    return typeof hc === "number" && !Number.isNaN(hc) ? hc : null;
  } catch {
    return null;
  }
}

function addDays(dayStr, n) {
  const [y, m, d] = dayStr.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

/** 足球日窗口 [D 12:01, D+1 12:00]（与 environment.get_football_day 一致）。 */
function getFootballDay(day) {
  return [`${day} 12:01:00`, `${addDays(day, 1)} 12:00:00`];
}

/** 当前北京时间 "YYYY-MM-DD HH:MM:SS"。 */
function nowBj() {
  return beijingNowIso().replace("T", " ");
}

/** 已开赛比赛的 lota_id 集合（仅当 now 仍落在足球日窗口内时生效）。 */
function computeStartedLids(cacheDir, day) {
  const [start, end] = getFootballDay(day);
  const now = nowBj();
  if (!(start <= now && now <= end)) return new Set();
  const started = new Set();
  for (const date of [day, addDays(day, 1)]) {
    for (const m of readMatches(cacheDir, date)) {
      const mt = m && m.match_time;
      if (mt && start <= mt && mt <= end && mt <= now && m.lota_id) {
        started.add(m.lota_id);
      }
    }
  }
  return started;
}

function randHex(n) {
  const bytes = new Uint8Array(n);
  for (let i = 0; i < n; i++) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").slice(0, n * 2);
}

/**
 * 刷新当天订单组（live 重跑前置）：退回未开赛订单金额，保留已开赛订单。
 * 对齐 agent.py::Agent.refresh_orders：
 *   - 足球日窗口 [D 12:01, D+1 12:00] 内的未结算订单
 *   - match_time > now（未开赛）→ 退回 bet_size 到余额、删除订单
 *   - match_time <= now（已开赛）→ 保留
 */
export async function refreshOrders(handles, dog, day, cacheDir) {
  const rolesDomain = await handles["ds_roles"];
  const rolesTable = rolesDomain.table("roles");
  const role = rolesTable.get(dog);
  if (!role) return { error: `角色 ${dog} 不存在` };

  const orders = role.orders || [];
  const now = nowBj().slice(0, 16); // "YYYY-MM-DD HH:MM"
  const windowStart = `${day} 12:01`;
  const windowEnd = `${addDays(day, 1)} 12:00`;

  // lota_id → match_time（当天窗口涉及的日期）
  const matchTimes = {};
  for (const date of [day, addDays(day, 1)]) {
    for (const m of readMatches(cacheDir, date)) {
      if (m && m.lota_id && m.match_time) {
        matchTimes[m.lota_id] = String(m.match_time).replace("T", " ").slice(0, 16);
      }
    }
  }

  let refunded = 0;
  let kept = 0;
  let totalRefund = 0;
  let capital = Number(role.capital || 0);
  const newOrders = [];

  for (const o of orders) {
    if (o.settled_at) {
      newOrders.push(o);
      continue;
    }
    const mt = matchTimes[o.lota_id] || "";
    const inWindow = mt && windowStart <= mt && mt <= windowEnd;
    if (!inWindow) {
      newOrders.push(o); // 不在当天窗口，保留
      continue;
    }
    const betSize = Number(o.bet_size || 0);
    if (mt <= now) {
      kept += 1; // 已开赛 → 保留
      newOrders.push(o);
    } else {
      capital += betSize; // 未开赛 → 退回金额，删除订单
      refunded += 1;
      totalRefund += betSize;
    }
  }

  await rolesTable.put(dog, {
    ...role,
    orders: newOrders,
    capital: Math.round(capital * 100) / 100,
    updated_at: beijingNowIso(),
  });

  return {
    user: dog, day,
    window: `${windowStart} ~ ${windowEnd}`,
    refunded, kept,
    total_refund: Math.round(totalRefund * 100) / 100,
    capital: Math.round(capital * 100) / 100,
  };
}

/**
 * 把结构化订单落库（写 ds_roles 域 + 扣 capital）。
 * @param orders 结构化订单数组（每单含 lota_id/bet_type/pick/odds/handicap/bet_size/reason/skip）。
 */
export async function submitOrders(handles, dog, day, orders, cacheDir) {
  const rolesDomain = await handles["ds_roles"];
  const rolesTable = rolesDomain.table("roles");
  const role = rolesTable.get(dog);
  if (!role) return { error: `角色 ${dog} 不存在` };

  const existing = role.orders || [];
  const capital = Number(role.capital || 0);

  // 1. 去重市场 (lota_id, bet_type)
  const pendingMarkets = new Set(
    existing.filter((o) => !o.settled_at && o.lota_id).map((o) => `${o.lota_id}|${o.bet_type}`),
  );
  // 2. 已开赛保护
  const startedLids = computeStartedLids(cacheDir, day);

  // 3. 过滤 skip / started / duplicate
  const candidates = [];
  for (const o of orders || []) {
    if (o.skip) continue;
    const lid = o.lota_id;
    if (lid && startedLids.has(lid)) continue;
    if (lid && pendingMarkets.has(`${lid}|${o.bet_type}`)) continue;
    candidates.push(o);
  }

  // 3b. 对齐 Python _fill_order_odds_and_handicap：亚盘 handicap 用权威盘口纠正符号
  for (const o of candidates) {
    if (o.bet_type === "亚盘" && o.lota_id) {
      const hc = readAuthoritativeHandicap(cacheDir, o.lota_id);
      if (hc != null) o.handicap = hc;
    }
  }

  // 4. 资金折算 或 FundManager
  const lockedExposure = existing
    .filter((o) => !o.settled_at)
    .reduce((s, o) => s + Number(o.bet_size || 0), 0);
  const limits = orderLimitsFor(dog);
  let toPlace;
  if (limits.enabled) {
    toPlace = applyFundLimits(limits, candidates, capital).kept;
  } else {
    const fullAmount = capital + lockedExposure;
    const newTotal = candidates.reduce((s, o) => s + Number(o.bet_size || 0), 0);
    if (newTotal > 0 && fullAmount > 0) {
      const scale = capital / fullAmount;
      toPlace = candidates.map((o) => ({
        ...o,
        bet_size: Math.trunc(Number(o.bet_size || 0) * scale),
      }));
    } else {
      toPlace = candidates;
    }
  }

  // 5. 落库 + 扣 capital（资金不足 break）
  let newCapital = capital;
  const newOrders = existing.slice();
  const placed = [];
  for (const o of toPlace) {
    const betSize = Number(o.bet_size || 0);
    if (betSize > newCapital) break;
    const full = {
      ...o,
      id: o.id || `ord_${Date.now()}_${randHex(3)}`,
      created_at: o.created_at || beijingNowIso(),
      hit: o.hit ?? null,
      return_amount: o.return_amount ?? 0,
      profit: o.profit ?? 0,
      score: o.score ?? "",
      settled_at: o.settled_at ?? null,
    };
    newCapital -= betSize;
    newOrders.push(full);
    placed.push(full);
    pendingMarkets.add(`${full.lota_id}|${full.bet_type}`);
  }

  await rolesTable.put(dog, {
    ...role,
    orders: newOrders,
    capital: Math.round(newCapital * 100) / 100,
    updated_at: beijingNowIso(),
  });

  return {
    user: dog,
    day,
    parsed: (orders || []).length,
    placed: placed.length,
    skipped: (orders || []).length - placed.length,
    capital: Math.round(newCapital * 100) / 100,
    orders: placed.map((o) => ({
      lota_id: o.lota_id,
      bet_type: o.bet_type,
      pick: o.pick,
      odds: o.odds,
      bet_size: o.bet_size,
      reason: (o.reason || "").slice(0, 40),
    })),
  };
}
