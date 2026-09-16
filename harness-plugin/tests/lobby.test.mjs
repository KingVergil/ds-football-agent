/**
 * 斗狗场分区（单关场 / 串关场）归属测试。
 *
 * 规则：真串关狗（有 parlay.json）→ 串关场；其余 → 单关场。
 * 关键反例：北单**单关**狗 scope 也是 `beidan`，但走通用 Agent 单关链路，
 * 必须落在单关场（靠"串关狗名单"区分，而不是只看 scope）。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { lobbyOf, splitLobbies } from "../lobby.js";

test("scope=beidan 且在有 parlay.json 的名单里 → 串关场", () => {
  const parlay = ["bc狗", "bcl狗"];
  assert.equal(lobbyOf({ name: "bc狗", scope: "beidan" }, parlay), "parlay");
  assert.equal(lobbyOf({ name: "bcl狗", scope: "beidan" }, parlay), "parlay");
});

test("北单单关狗（scope=beidan 但无 parlay.json）→ 单关场", () => {
  const parlay = ["bc狗", "bcl狗"];
  assert.equal(lobbyOf({ name: "梭哈北单狗", scope: "beidan" }, parlay), "single");
  assert.equal(lobbyOf({ name: "跟风北单狗", scope: "beidan" }, parlay), "single");
});

test("scope=jc / all / 未声明 → 单关场", () => {
  const parlay = ["bc狗"];
  for (const scope of ["jc", "all", undefined, null, ""]) {
    assert.equal(lobbyOf({ name: "梭哈2狗", scope }, parlay), "single", `scope=${scope}`);
  }
});

test("不传名单时退回 scope 判定（串关场的兜底口径）", () => {
  assert.equal(lobbyOf({ name: "任意", scope: "beidan" }), "parlay");
  assert.equal(lobbyOf({ name: "任意", scope: "jc" }), "single");
});

test("isParlayRole 权威优先（修掉北单单关狗被错分进串关场）", () => {
  // dashboard 下发的真实字段：scope=beidan 但 isParlayRole=false → 必须落单关场
  assert.equal(lobbyOf({ name: "梭哈北单狗", scope: "beidan", isParlayRole: false }), "single");
  assert.equal(lobbyOf({ name: "跟风北单狗", scope: "beidan", isParlayRole: false }), "single");
  assert.equal(lobbyOf({ name: "bc狗", scope: "beidan", isParlayRole: true }), "parlay");
  assert.equal(lobbyOf({ name: "95狗", scope: "beidan", isParlayRole: true }), "parlay");
  // 即使名单给错，isParlayRole 也优先
  assert.equal(lobbyOf({ name: "梭哈北单狗", scope: "beidan", isParlayRole: false }, ["梭哈北单狗"]), "single");
});

test("splitLobbies 保持原顺序并分桶", () => {
  const dogs = [
    { name: "梭哈2狗", scope: "jc" },
    { name: "bcl狗", scope: "beidan" },
    { name: "梭哈北单狗", scope: "beidan" },
    { name: "95狗", scope: "beidan", isParlayRole: true },
  ];
  const { single, parlay } = splitLobbies(dogs, ["95狗", "bcl狗"]);
  assert.deepEqual(single.map((d) => d.name), ["梭哈2狗", "梭哈北单狗"]);
  assert.deepEqual(parlay.map((d) => d.name), ["bcl狗", "95狗"]);
});

test("线上真实注册表快照：串关场只有串关狗", () => {
  // 快照式断言：如将来新增串关狗，需同时更新这里与 lobby.js 的口径说明
  const dogs = [
    { name: "alpha2狗", scope: "jc" }, { name: "alpha狗", scope: "jc" },
    { name: "梭哈2狗", scope: "jc" }, { name: "梭哈3狗", scope: "jc" },
    { name: "平局狗", scope: "jc" }, { name: "跟风狗", scope: "jc" },
    { name: "均注狗", scope: "jc" }, { name: "深度足球狗", scope: "jc" },
    { name: "梭哈北单狗", scope: "beidan" }, { name: "跟风北单狗", scope: "beidan" },
    { name: "bc狗", scope: "beidan" }, { name: "bcl狗", scope: "beidan" },
  ];
  const { single, parlay } = splitLobbies(dogs, ["bc狗", "bcl狗"]);
  assert.equal(parlay.length, 2);
  // 保持输入顺序（bcl狗 在 bc狗 之前）
  assert.deepEqual(parlay.map((d) => d.name), ["bc狗", "bcl狗"]);
  assert.equal(single.length, 10);
  assert.ok(single.some((d) => d.name === "梭哈北单狗"), "北单单关狗应在单关场");
});
