/**
 * 结算/因子解耦的 UI 阶段按钮测试（2026-09-13 工单②）。
 *
 * 守两件事：
 *   1. 串关狗拿到「结算 / 产因子」两个 action，且 payload 打对了桥的入口
 *      （两个都走 `func="settle"`，靠 `opts.stage="settle"|"reflect"` 分流）；
 *   2. **单关狗（含北单单关狗）一个都不加** —— 单狗红线。
 *
 * ⚠️ 2026-09-16：旧实现的「产因子」发 `func:"reflect"`，而桥的 func 白名单里
 * 没有 `reflect` → 点按钮必报错。现在统一走 `settle` + `stage=reflect`，
 * 断言按现行实现更新。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { stageActionsFor } from "../lobby.js";

const PARLAY_DOG = { name: "95狗", scope: "beidan", isParlayRole: true };
const SINGLE_JC = { name: "梭哈2狗", scope: "jc", isParlayRole: false };
// 关键反例：scope=beidan 但**没有** parlay.json → 走通用单关链路
const SINGLE_BEIDAN = { name: "梭哈北单狗", scope: "beidan", isParlayRole: false };

test("串关狗 → 得到「结算」与「产因子」两个按钮", () => {
  const acts = stageActionsFor(PARLAY_DOG, "2026-09-14");
  assert.equal(acts.length, 2);
  assert.deepEqual(acts.map((a) => a.id), ["settle-only", "reflect-only"]);
  assert.deepEqual(acts.map((a) => a.label), ["🧾 结算", "🧬 产因子"]);
});

test("「只结算」打 opts.stage=settle（不产因子、不烧 LLM）", () => {
  const [settleOnly] = stageActionsFor(PARLAY_DOG, "2026-09-14");
  assert.equal(settleOnly.func, "settle");
  assert.equal(settleOnly.payload.func, "settle");
  assert.equal(settleOnly.payload.opts.stage, "settle");
  assert.equal(settleOnly.payload.dog, "95狗");
});

test("「产因子」走 settle + opts.stage=reflect（不动订单与资金）", () => {
  const reflectOnly = stageActionsFor(PARLAY_DOG, "2026-09-14")[1];
  // func 必须是桥白名单里的 settle（旧实现发 reflect 会被桥拒）
  assert.equal(reflectOnly.func, "settle");
  assert.equal(reflectOnly.payload.func, "settle");
  assert.equal(reflectOnly.payload.opts.stage, "reflect");
  assert.equal(reflectOnly.payload.dog, "95狗");
});

test("足球日透传到两个 payload", () => {
  for (const a of stageActionsFor(PARLAY_DOG, "2026-09-14")) {
    assert.equal(a.payload.day, "2026-09-14");
  }
});

test("单关狗（jc）→ 不加任何阶段按钮", () => {
  assert.deepEqual(stageActionsFor(SINGLE_JC, "2026-09-14"), []);
});

test("北单单关狗（scope=beidan 但无 parlay.json）→ 也不加（单狗红线）", () => {
  assert.deepEqual(stageActionsFor(SINGLE_BEIDAN, "2026-09-14"), []);
});

test("不传 day 时 payload.day 为 null（不伪造日期）", () => {
  assert.equal(stageActionsFor(PARLAY_DOG)[0].payload.day, null);
});

test("线上真实注册表快照：只有串关狗拿到阶段按钮", () => {
  const dogs = [
    { name: "95狗", scope: "beidan", isParlayRole: true },
    { name: "bcl狗", scope: "beidan", isParlayRole: true },
    { name: "bc狗", scope: "beidan", isParlayRole: true },
    { name: "梭哈北单狗", scope: "beidan", isParlayRole: false },
    { name: "跟风北单狗", scope: "beidan", isParlayRole: false },
    { name: "梭哈2狗", scope: "jc", isParlayRole: false },
  ];
  const got = dogs.filter((d) => stageActionsFor(d, "2026-09-14").length > 0)
                  .map((d) => d.name);
  assert.deepEqual(got, ["95狗", "bcl狗", "bc狗"]);
});
