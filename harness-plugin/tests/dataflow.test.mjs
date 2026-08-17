import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import {
  addDays, footballDayRange, calendarDatesForDay,
  jingcaiWindowMatches, hasValidFeature, hasTags,
} from "../dataflow.js";
import { readMatchesCache } from "../fanout.js";
import {
  snapshotDomains, restoreDomains, resetRolesToZero,
  snapshotMatchesRange, readReplayDay,
  writeDomainSnapshot, restoreDomainSnapshot, listCheckpoints,
} from "../replay.js";

function makeCache() {
  const dir = mkdtempSync(join(tmpdir(), "ds-dataflow-"));
  mkdirSync(join(dir, "matches"), { recursive: true });
  mkdirSync(join(dir, "features"), { recursive: true });
  mkdirSync(join(dir, "tags"), { recursive: true });
  return dir;
}

function match(id, mt, extra = {}) {
  return { lota_id: id, home_name: "A", away_name: "B", league_name: "L", match_time: mt, score: "2:1", ...extra };
}

test("addDays / footballDayRange / calendarDatesForDay", () => {
  assert.equal(addDays("2026-08-16", 1), "2026-08-17");
  assert.deepEqual(footballDayRange("2026-08-16"), { start: "2026-08-16 12:01", end: "2026-08-17 12:00" });
  assert.deepEqual(calendarDatesForDay("2026-08-16"), ["2026-08-16", "2026-08-17"]);
});

test("jingcaiWindowMatches：窗口 + 竞彩边界过滤", () => {
  const dir = makeCache();
  try {
    writeFileSync(join(dir, "matches", "2026-08-16.json"), JSON.stringify([
      match("L1", "2026-08-16 20:00", { jingcai_number: "001" }),          // 窗口内竞彩
      match("L2", "2026-08-16 10:00", { jingcai_number: "002" }),          // 12:01 前 → 窗口外
      match("L3", "2026-08-16 20:30", { beidan_number: "B1" }),            // 北单仅 → 排除
      match("L4", "2026-08-16 21:00", {}),                                 // 无号 → 排除
    ]));
    writeFileSync(join(dir, "matches", "2026-08-17.json"), JSON.stringify([
      match("L5", "2026-08-17 10:00", { jingcai_number: "003" }),          // 窗口内竞彩（跨天）
      match("L6", "2026-08-17 13:00", { jingcai_number: "004" }),          // 12:00 后 → 窗口外
    ]));

    const jc = jingcaiWindowMatches(dir, "2026-08-16");
    assert.deepEqual(jc.map((m) => m.lota_id), ["L1", "L5"]); // 排序按时间
    assert.equal(jc[0].score, "2:1"); // 未 strip

    const stripped = jingcaiWindowMatches(dir, "2026-08-16", { strip: true });
    assert.ok(!("score" in stripped[0]));

    const all = jingcaiWindowMatches(dir, "2026-08-16", { jingcaiOnly: false });
    assert.deepEqual(all.map((m) => m.lota_id), ["L1", "L3", "L4", "L5"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readMatchesCache：与 dataflow 窗口过滤一致（fanout 注入用）", () => {
  const dir = makeCache();
  try {
    writeFileSync(join(dir, "matches", "2026-08-16.json"), JSON.stringify([
      match("M1", "2026-08-16 10:00", { jingcai_number: "早场" }),   // 窗口外（上一足球日）
      match("M2", "2026-08-16 20:00", { jingcai_number: "001" }),
    ]));
    writeFileSync(join(dir, "matches", "2026-08-17.json"), JSON.stringify([
      match("M3", "2026-08-17 10:00", { jingcai_number: "003" }),    // 窗口内（跨天）
    ]));
    const list = readMatchesCache(dir, "2026-08-16");
    assert.deepEqual(list.map((m) => m.lota_id), ["M2", "M3"]);
    assert.ok(!("score" in list[0]));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("hasValidFeature / hasTags", () => {
  const dir = makeCache();
  try {
    writeFileSync(join(dir, "features", "L1.json"), JSON.stringify({ compact_fet: "▋联赛类型: 英超" }));
    writeFileSync(join(dir, "features", "L2.json"), JSON.stringify({ _api_failed: true }));
    writeFileSync(join(dir, "tags", "L1.json"), JSON.stringify({ sections: { "fair-odds": "x" } }));
    assert.equal(hasValidFeature(dir, "L1"), true);
    assert.equal(hasValidFeature(dir, "L2"), false);
    assert.equal(hasValidFeature(dir, "L3"), false);
    assert.equal(hasTags(dir, "L1"), true);
    assert.equal(hasTags(dir, "L3"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** 内存版 storage 域（domain.table().get/put/entries）。 */
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
  return {
    ds_roles: fakeDomain(),
    ds_factors: fakeDomain(),
    ds_reflections: fakeDomain(),
    ds_slugs: fakeDomain(),
    ds_factor_registry: fakeDomain(),
  };
}

test("snapshotDomains / restoreDomains 往返", async () => {
  const handles = fakeHandles();
  const roles = await handles.ds_roles;
  await roles.table("roles").put("梭哈2狗", { name: "梭哈2狗", capital: 1234, initial_capital: 10000, orders: [{ lota_id: "L1" }] });
  const dir = mkdtempSync(join(tmpdir(), "ds-replay-snap-"));
  try {
    await snapshotDomains(handles, dir);
    // 改动现场
    await roles.table("roles").put("梭哈2狗", { name: "梭哈2狗", capital: 999, initial_capital: 10000, orders: [] });
    await restoreDomains(handles, dir);
    const after = roles.table("roles").get("梭哈2狗");
    assert.equal(after.capital, 1234);
    assert.equal(after.orders.length, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("阶段检查点：writeDomainSnapshot / restoreDomainSnapshot / listCheckpoints", async () => {
  const handles = fakeHandles();
  const roles = await handles.ds_roles;
  await roles.table("roles").put("梭哈2狗", {
    name: "梭哈2狗", capital: 1000, initial_capital: 1000, orders: [],
  });
  const dir = mkdtempSync(join(tmpdir(), "ds-replay-cp-"));
  try {
    const cp = join(dir, "checkpoints", "2026-07-25__pre-settle");
    await writeDomainSnapshot(handles, cp);
    // 模拟结算后状态变化
    await roles.table("roles").put("梭哈2狗", {
      name: "梭哈2狗", capital: 500, initial_capital: 1000, orders: [{ lota_id: "L1" }],
    });
    assert.equal(roles.table("roles").get("梭哈2狗").capital, 500);
    // 恢复到结算前
    await restoreDomainSnapshot(handles, cp);
    const back = roles.table("roles").get("梭哈2狗");
    assert.equal(back.capital, 1000);
    assert.deepEqual(back.orders, []);
    assert.deepEqual(listCheckpoints(dir), ["start", "2026-07-25__pre-settle"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("resetRolesToZero：初始资金 + 空记忆", async () => {
  const handles = fakeHandles();
  const roles = await handles.ds_roles;
  await roles.table("roles").put("梭哈2狗", { name: "梭哈2狗", capital: 500, initial_capital: 10000, orders: [{ lota_id: "L1", bet_size: 100 }] });
  const reset = await resetRolesToZero(handles, ["梭哈2狗"]);
  assert.deepEqual(reset, ["梭哈2狗"]);
  const role = roles.table("roles").get("梭哈2狗");
  assert.equal(role.capital, 10000);
  assert.deepEqual(role.orders, []);
  const fp = (await handles.ds_factors).table("factors").get("梭哈2狗");
  assert.deepEqual(fp.factor_perf, {});
});

test("snapshotMatchesRange / readReplayDay：回放快照隔离并发改写", () => {
  const dir = makeCache();
  const replayDir = join(dir, "replay_run");
  mkdirSync(replayDir, { recursive: true });
  try {
    writeFileSync(join(dir, "matches", "2026-08-16.json"), JSON.stringify([
      match("S1", "2026-08-16 20:00", { jingcai_number: "001" }),
    ]));
    writeFileSync(join(dir, "matches", "2026-08-17.json"), JSON.stringify([]));
    snapshotMatchesRange(dir, replayDir, ["2026-08-16", "2026-08-17"]);
    // 模拟外部刷新器改写真实缓存（回放不受影响）
    writeFileSync(join(dir, "matches", "2026-08-16.json"), JSON.stringify([]));
    const day = readReplayDay(dir, replayDir, "2026-08-16");
    assert.equal(day.window_total, 1);
    assert.equal(day.jingcai_count, 1);
    assert.deepEqual(day.matches.map((m) => m.lota_id), ["S1"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
