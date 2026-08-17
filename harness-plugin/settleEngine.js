/**
 * 纯 JS settle 引擎（harness_js_reconstruction.md §3.1–3.3）。
 * 从 ds_roles 域读未结算订单 → 取比分（matches 缓存，仅 state==6）→ settleOrder → 写回 + 更新 capital。
 * 无 LLM、无 Python 桥。
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { settleOrder, beijingNowIso } from "./settle.js";
import { DS_REAL_DOGS } from "./storage.js";

function round2(x) {
  return Math.round(x * 100) / 100;
}

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

/** day±n 的日期串（UTC，避免宿主机时区）。 */
function addDays(dayStr, n) {
  const [y, m, d] = dayStr.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

/** 从 matches 缓存取比分（仅 state==6 完场权威）。 */
export function fetchScoresFromCache(cacheDir, dates) {
  const scores = {};
  for (const date of dates) {
    for (const m of readMatches(cacheDir, date)) {
      if (m && m.lota_id && m.score && m.state === 6) {
        scores[m.lota_id] = m.score;
      }
    }
  }
  return scores;
}

/** 结算一只狗的未结算订单（纯 JS，写回 ds_roles 域 + 更新 capital）。 */
export async function settleDog(handles, dog, day, cacheDir) {
  const rolesDomain = await handles["ds_roles"];
  const rolesTable = rolesDomain.table("roles");
  const role = rolesTable.get(dog);
  if (!role) return { error: `角色 ${dog} 不存在` };

  const orders = role.orders || [];
  const unsettled = orders.filter((o) => !o.settled_at);
  if (!unsettled.length) {
    return {
      user: dog, day, unsettled: 0, settled: 0, hit: 0, miss: 0, push: 0,
      pnl: 0, capital: role.capital, orders: [],
    };
  }

  const dates = [day, addDays(day, 1), addDays(day, -1)];
  const scores = fetchScoresFromCache(cacheDir, dates);

  // 逐单结算（命中一次，写回复用同一结果）
  const settledById = new Map();
  for (const o of unsettled) {
    const sc = scores[o.lota_id];
    if (!sc) continue;
    settledById.set(o.id, settleOrder(o, sc));
  }

  let hit = 0;
  let miss = 0;
  let push = 0;
  let pnl = 0;
  let capital = Number(role.capital || 0);
  const settledList = [];
  for (const s of settledById.values()) {
    capital += s.return_amount;
    const h = s.hit;
    const profit = s.profit;
    if (h === true || (h == null && profit > 0)) hit += 1;
    else if (h === false || (h == null && profit < 0)) miss += 1;
    else push += 1;
    pnl += profit;
    settledList.push({
      lota_id: s.lota_id,
      bet_type: s.bet_type,
      pick: s.pick,
      score: s.score,
      hit: s.hit,
      profit: s.profit,
      bet_size: s.bet_size,
      reason: s.reason,
    });
  }

  const newOrders = orders.map((o) => settledById.get(o.id) || o);
  await rolesTable.put(dog, {
    ...role,
    orders: newOrders,
    capital: Math.round(capital * 100) / 100,
    updated_at: beijingNowIso(),
  });

  return {
    user: dog, day,
    unsettled: unsettled.length,
    settled: settledById.size,
    hit, miss, push,
    pnl: Math.round(pnl * 100) / 100,
    capital: Math.round(capital * 100) / 100,
    orders: settledList,
  };
}

/**
 * 结算流（纯 JS，无 LLM）：并行结算多只狗的未结算订单（只认 state==6 比分）。
 * 每狗进度通过 onProgress 上报（idx/total/dog/status），供任务状态展示。
 */
export async function settleAll(handles, cacheDir, { day, dogs = DS_REAL_DOGS, parallel = 4, onProgress } = {}) {
  const dogList = (dogs && dogs.length ? dogs : DS_REAL_DOGS).slice();
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const rows = new Array(dogList.length);
  let cursor = 0;

  const worker = async () => {
    while (cursor < dogList.length) {
      const idx = cursor++;
      const dog = dogList[idx];
      progress({ idx, total: dogList.length, dog, phase: `结算 ${dog}` });
      try {
        const res = await settleDog(handles, dog, day, cacheDir);
        rows[idx] = { dog, ok: !res.error, ...res };
      } catch (e) {
        rows[idx] = { dog, ok: false, error: String((e && e.message) || e) };
      }
      progress({
        idx: idx + 1,
        total: dogList.length,
        dog,
        status: rows[idx].ok ? "ok" : "fail",
        phase: `结算 ${idx + 1}/${dogList.length}`,
        detail: `${rows[idx].settled || 0}单 PnL${rows[idx].pnl != null ? rows[idx].pnl : 0}`,
      });
    }
  };

  const workers = Math.max(1, Math.min(Number(parallel) || dogList.length, dogList.length || 1));
  await Promise.all(Array.from({ length: workers }, () => worker()));

  const okCount = rows.filter((r) => r && r.ok).length;
  const totalPnl = rows.reduce((s, r) => s + (r && r.pnl || 0), 0);
  return {
    ok: okCount === rows.length,
    day,
    dogs: dogList,
    ok_count: okCount,
    fail_count: rows.length - okCount,
    total_pnl: round2(totalPnl),
    rows,
    text: rows
      .map((r) => `${r.ok ? "OK " : "FAIL "}${r.dog} 结算${r.settled || 0}单 PnL${r.pnl != null ? r.pnl : "?"}`)
      .join("\n"),
  };
}
