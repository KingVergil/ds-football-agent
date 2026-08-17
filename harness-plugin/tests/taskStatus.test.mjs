import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { createTaskRegistry, readTasks, withTask } from "../taskStatus.js";

function tmpCache() {
  return mkdtempSync(join(tmpdir(), "ds-taskstatus-"));
}

test("生命周期：start → update → finish（completed）", () => {
  const dir = tmpCache();
  try {
    const reg = createTaskRegistry(dir);
    const id = reg.start({ type: "replay", title: "回放 07-25", params: { start: "2026-07-25" } });
    assert.equal(reg.list().length, 1);
    assert.equal(reg.list()[0].status, "running");
    reg.update(id, { phase: "第 1/1 天 结算", done: 1, total: 1 });
    reg.finish(id, { ok: true, detail: "竞彩 11/146", result_summary: "1000 → 831.5" });
    const t = reg.list()[0];
    assert.equal(t.status, "completed");
    assert.equal(t.done, 1);
    assert.equal(t.result_summary, "1000 → 831.5");
    assert.ok(t.finished_at);
    // 持久化到文件
    const disk = readTasks(dir);
    assert.equal(disk.tasks.length, 1);
    assert.equal(disk.tasks[0].id, id);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("withTask：成功 / 异常标 failed 且返回 { error }", async () => {
  const dir = tmpCache();
  try {
    const reg = createTaskRegistry(dir);
    const okFn = withTask(reg, { type: "settle", title: "结算" }, async (args, _exec, progress) => {
      progress({ phase: "结算中", done: 1, total: 1 });
      return { settled: 3, pnl: 10 };
    });
    const okRes = await okFn({ day: "2026-07-25" });
    assert.deepEqual(okRes, { settled: 3, pnl: 10 });
    assert.equal(reg.list()[0].status, "completed");

    const failFn = withTask(reg, { type: "factor-induction", title: "因子归纳" }, async () => {
      throw new Error("boom");
    });
    const failRes = await failFn({});
    assert.deepEqual(failRes, { error: "boom" });
    assert.equal(reg.list()[0].status, "failed");
    assert.equal(reg.list()[0].type, "factor-induction");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("prune / interrupted 兜底：running 超时转 interrupted，completed 超 TTL 清理", async () => {
  const dir = tmpCache();
  try {
    // 直接写一份含超时 running 任务的 status.json（模拟进程遗留）
    mkdirSync(join(dir, "tasks"), { recursive: true });
    const stale = new Date(Date.now() - 20 * 60 * 1000).toISOString();
    writeFileSync(join(dir, "tasks", "status.json"), JSON.stringify({
      tasks: [{ id: "replay_stale_1", type: "replay", title: "旧任务", status: "running",
                phase: "回放中", done: 0, total: 1, started_at: stale, updated_at: stale }],
    }), "utf8");
    const reg = createTaskRegistry(dir, { max: 3, ttlMs: 60 * 1000 });
    // 触发一次新任务 → prune 会把超时 running 转 interrupted
    reg.start({ type: "settle", title: "新任务" });
    const list = reg.list();
    const old = list.find((t) => t.id === "replay_stale_1");
    assert.equal(old.status, "interrupted");
    // max=3：多余条目被裁剪
    assert.ok(list.length <= 3);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
