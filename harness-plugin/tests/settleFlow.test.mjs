import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { settleAll } from "../settleEngine.js";

function fakeDomain(initial = {}) {
  const data = { ...initial };
  return {
    table: () => ({
      get: (k) => data[k],
      put: async (k, v) => { data[k] = v; },
      entries: function* () { for (const [k, v] of Object.entries(data)) yield [k, v]; },
    }),
    _data: data,
  };
}

function fakeHandles() {
  return { ds_roles: fakeDomain(), ds_factors: fakeDomain(), ds_reflections: fakeDomain() };
}

function makeCache() {
  const dir = mkdtempSync(join(tmpdir(), "ds-settle-"));
  mkdirSync(join(dir, "matches"), { recursive: true });
  return dir;
}

function order(lotaId, betType = "亚盘", pick = "H") {
  return {
    id: `ord_${lotaId}_${betType}`,
    lota_id: lotaId,
    bet_type: betType,
    pick,
    handicap: 0,
    odds: 1.9,
    bet_size: 100,
    created_at: "2026-07-25T12:00:00",
    settled_at: null,
  };
}

test("settleAll：纯 JS 并行结算，进度明确上报，无 LLM", async () => {
  const cacheDir = makeCache();
  try {
    writeFileSync(join(cacheDir, "matches", "2026-07-25.json"), JSON.stringify([
      { lota_id: "L1", home_name: "A", away_name: "B", match_time: "2026-07-25 20:00:00", score: "2:1", state: 6 },
    ]));

    const handles = fakeHandles();
    const roles = await handles.ds_roles;
    await roles.table("roles").put("梭哈2狗", {
      name: "梭哈2狗", capital: 1000, initial_capital: 1000, orders: [order("L1")],
    });
    await roles.table("roles").put("均注狗", {
      name: "均注狗", capital: 1000, initial_capital: 1000, orders: [order("L1")],
    });

    const progressCalls = [];
    const res = await settleAll(handles, cacheDir, {
      day: "2026-07-25",
      dogs: ["梭哈2狗", "均注狗"],
      parallel: 2,
      onProgress: (p) => progressCalls.push(p),
    });

    assert.equal(res.ok, true);
    assert.equal(res.ok_count, 2);
    assert.equal(res.total_pnl, 380); // 每单 profit = 100*1.9 = 190 × 2
    // 进度：每个狗至少一次带 idx/total 的更新，且按 1/2 → 2/2 推进
    const phases = progressCalls.map((p) => p.phase).join("|");
    assert.ok(phases.includes("结算 1/2"));
    assert.ok(phases.includes("结算 2/2"));
    assert.equal(progressCalls.filter((p) => p.status === "ok").length, 2); // 两只狗都有完成态
    // 资金已更新（无 LLM 介入：settleDog 纯函数）
    const dog = roles.table("roles").get("梭哈2狗");
    assert.equal(dog.capital, 1290); // 1000 + 返还(100+190)
    assert.equal(dog.orders[0].settled_at != null, true);
  } finally {
    rmSync(cacheDir, { recursive: true, force: true });
  }
});
