/**
 * 回放「桥调用计划」契约测试：ds_replay（薄壳）必须与引擎脚本 run_beidan_loop.py 同口径。
 *
 * 为什么需要它
 * ────────────
 * 2026-09-11 实测事故：ds_replay 写死 `jingcai_only: true`，北单串关狗被当竞彩跑
 * （7.11 返回「13 场竞彩」而不是「21 场北单」，还按竞彩波次拆了 4 个窗口）。
 * 薄壳原则要求：斗狗场只是**忠实转发**，引擎收到什么 = 引擎脚本亲自跑收到什么。
 *
 * 这个测试用替身桥（opts._bridge）捕获 runReplay 每天的**完整调用计划**并逐条断言，
 * 不改生产路径（不传 _bridge 时行为与改造前逐字节一致）。
 *
 * 北单应收到（对齐 run_beidan_loop.py）：
 *   ✗ 不调 prepare-range（北单没有 prepare）
 *   ✗ 不调 prepare
 *   ✓ analyze {dog, day, opts:{live:false, beidan_only:true, prefetched:false}}
 *   ✓ settle  {dog, day}                      ← 引擎 settle(reflect=True) 默认开反思
 *   ✓ factor-induction {dog, day}
 * 竞彩（单狗）应收到（与改造前一致）：
 *   ✓ prepare-range → prepare → analyze(jingcai_only + 切窗) → settle → factor-induction
 */
import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync, cpSync, rmSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { runReplay } from "../replay.js";

// 角色源在测试里自建（不依赖仓库里的私有 roles/，公开克隆也能跑）
const ROLE_SRC = join(tmpdir(), `dsp_role_src_${process.pid}`);
// 引擎根目录只做「传进去的路径」用（真桥被替身接管），指向测试自建目录即可
const ENGINE_ROOT = join(tmpdir(), `dsp_engine_root_${process.pid}`);
mkdirSync(ROLE_SRC, { recursive: true });
mkdirSync(ENGINE_ROOT, { recursive: true });
writeFileSync(join(ROLE_SRC, "bcl狗.json"),
  JSON.stringify({ name: "bcl狗", capital: 5000, initial_capital: 5000, orders: [] }));
writeFileSync(join(ROLE_SRC, "persona.md"), "# 测试人设\n");

/** 造一个沙箱会话 + 替身桥，跑一天，返回按顺序捕获的桥请求。 */
async function capturePlan(scope, sandboxName) {
  const cacheDir = join(tmpdir(), `dsp_${process.pid}_${sandboxName}`);
  rmSync(cacheDir, { recursive: true, force: true });
  mkdirSync(join(cacheDir, "roles"), { recursive: true });
  mkdirSync(join(cacheDir, "factors"), { recursive: true });
  writeFileSync(join(cacheDir, "dogs.json"), JSON.stringify([
    { name: "bcl狗", scope, enabled: true, status: "sandbox", initial_capital: 5000 },
    { name: "单狗", scope: "jc", enabled: true, status: "live", initial_capital: 1000 },
  ]));
  cpSync(ROLE_SRC, join(cacheDir, "roles/bcl狗"), { recursive: true });
  const sandboxDir = join(cacheDir, "replays/sandboxes", sandboxName);
  cpSync(join(cacheDir, "roles/bcl狗"), join(sandboxDir, "workspace"), { recursive: true });
  writeFileSync(join(sandboxDir, "session.json"), JSON.stringify({
    run_id: "test", sandbox: sandboxName, dog: "bcl狗", start: "2026-07-11", end: "2026-07-11",
    days: ["2026-07-11"], next_idx: 0, status: "running", factor_review_every: 7,
  }));

  const calls = [];
  const fakeBridge = async ({ req }) => {
    calls.push(JSON.parse(JSON.stringify(req)));
    // 关键：每天 settle 之后必须有可归纳的因子（真实引擎由反思产出），这里造一个
    if (req.func === "factor-induction") {
      const fm = join(sandboxDir, "workspace/memory/factor_memory.json");
      if (!existsSync(fm)) {
        mkdirSync(join(sandboxDir, "workspace/memory"), { recursive: true });
        writeFileSync(fm, JSON.stringify({ factor_perf: { 测试因子: { total: 1, hit: 1, profit: 1.0 } } }));
      }
    }
    return { ok: true, data: { placed: 0, settlement: { settled: 0, pnl: 0 }, summary: {} }, stdout: "", stderr: "" };
  };

  const res = await runReplay({}, cacheDir, ENGINE_ROOT, {
    dog: "bcl狗", start: "2026-07-11", end: "2026-07-11", sandbox: sandboxName,
    pythonBin: "python3", _bridge: fakeBridge,
  });
  assert.equal(res.ok, true, `回放应跑完: ${JSON.stringify(res).slice(0, 300)}`);
  const plan = calls.map((c) => c.func);
  rmSync(cacheDir, { recursive: true, force: true });
  return { calls, plan };
}

test("北单（scope=beidan）：不调 prepare/prepare-range，analyze 传 beidan_only", async () => {
  const { calls, plan } = await capturePlan("beidan", "bcl狗_0711_plan_bd");
  assert.deepEqual(plan, ["analyze", "settle", "factor-induction"],
    `北单桥调用计划应为 analyze→settle→factor-induction，实际 ${plan.join("→")}`);

  const analyze = calls.find((c) => c.func === "analyze");
  assert.equal(analyze.opts.beidan_only, true, "analyze 必须带 beidan_only");
  assert.equal("jingcai_only" in analyze.opts, false, "北单不得混入 jingcai_only");
  assert.equal(analyze.opts.prefetched, false, "北单没有 prepare，prefetched 必须 false");
  assert.equal(analyze.opts.live, false);
  assert.equal(analyze.opts.role_root.endsWith("workspace"), true, "必须带沙箱 role_root");
  assert.equal(analyze.opts.window, undefined, "北单不拆窗（波次由引擎内部启动）");

  const settle = calls.find((c) => c.func === "settle");
  assert.equal("skip_llm" in settle.opts, false, "settle 默认开反思（reflect=True）");

  assert.equal(plan.includes("prepare"), false, "北单不得调 prepare");
  assert.equal(plan.includes("prepare-range"), false, "北单不得调 prepare-range");
  for (const c of calls) {
    assert.equal("_bridge" in c, false, "测试注入键不得进入引擎请求");
    assert.equal("_engineRoot" in c, false, "测试注入键不得进入引擎请求");
  }
});

test("竞彩（scope=jc）：保留 prepare/prepare-range 与切窗语义", async () => {
  const { calls, plan } = await capturePlan("jc", "单狗_0711_plan_jc");
  assert.equal(plan[0], "prepare-range", `竞彩应先范围预取，实际 ${plan.join("→")}`);
  assert.equal(plan[1], "prepare");

  const prep = calls.find((c) => c.func === "prepare");
  assert.equal(prep.opts.mode, "replay");
  assert.equal(prep.opts.jingcai_only, true, "竞彩 prepare 必须 jingcai_only");

  const analyze = calls.find((c) => c.func === "analyze");
  assert.equal(analyze.opts.jingcai_only, true, "竞彩 analyze 必须 jingcai_only");
  assert.equal(analyze.opts.prefetched, true, "竞彩走 prepare → prefetched");
  assert.equal("beidan_only" in analyze.opts, false);

  const prepRange = calls.find((c) => c.func === "prepare-range");
  assert.equal(prepRange.opts.jingcai_only, true, "竞彩范围预取必须 jingcai_only");
});
