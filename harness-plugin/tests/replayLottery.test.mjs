/**
 * 回放彩票类型分派：单狗（竞彩）路径不得改变。
 *
 * 背景（2026-09-11）：ds_replay 原先写死 `jingcai_only: true`，北单串关狗（bc狗/bcl狗）
 * 被当成竞彩跑——7.11 实测返回「13 场竞彩」而非「21 场北单」，还按竞彩波次拆了 4 个窗口。
 * 修法：按 dogs.json 的 scope 分派（beidan → beidan_only + 不走 prepare/不拆窗）。
 *
 * 这个测试钉住的是**单狗原则**：只有 scope="beidan" 才进北单分支，其余（含未知狗、
 * 注册表读不到）必须精确回落竞彩语义——与改造前逐字节一致。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { resolveLottery } from "../replay.js";

test("scope=beidan → 北单口径（beidan_only，不走 prepare）", () => {
  const r = resolveLottery("beidan");
  assert.equal(r.type, "beidan");
  assert.deepEqual(r.opts, { beidan_only: true });
  assert.equal(r.usesPrepare, false, "北单没有 prepare 阶段");
  assert.equal(r.splitWindows, false, "北单波次由引擎内部启动，harness 不拆窗");
});

test("scope=jc / all / 未知 / 空 → 竞彩口径（与改造前一致）", () => {
  for (const scope of ["jc", "all", undefined, null, "", "JC", "beidan2"]) {
    const r = resolveLottery(scope);
    assert.equal(r.type, "jingcai", `scope=${String(scope)} 必须回落竞彩`);
    assert.deepEqual(r.opts, { jingcai_only: true });
    assert.equal(r.usesPrepare, true, `scope=${String(scope)} 必须保留 prepare`);
    assert.equal(r.splitWindows, true, `scope=${String(scope)} 必须保留拆窗`);
  }
});

test("竞彩 opts 与改造前的字面量逐字段相同", () => {
  const r = resolveLottery("jc");
  assert.equal(r.opts.jingcai_only, true);
  assert.equal("beidan_only" in r.opts, false, "竞彩 opts 不得混入 beidan_only");
});

test("北单 opts 不得混入 jingcai_only（两口径互斥）", () => {
  const r = resolveLottery("beidan");
  assert.equal("jingcai_only" in r.opts, false);
});
