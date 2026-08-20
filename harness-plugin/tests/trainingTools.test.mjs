import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { createTaskRegistry } from "../taskStatus.js";
import { registerTrainingTools } from "../tools/trainingTools.js";
import { resolveRoles } from "../tools/roles.js";

/** 造一个临时数据根：1 只 live 老狗 + 注册表，返回 { cacheDir, rolesDir, tools, cleanup }。 */
function fixture() {
  const cacheDir = mkdtempSync(join(tmpdir(), "train-test-"));
  const rolesDir = join(cacheDir, "roles");
  mkdirSync(join(rolesDir, "稳狗"), { recursive: true });
  writeFileSync(join(rolesDir, "稳狗", "稳狗.json"), JSON.stringify({
    name: "稳狗", capital: 12000, initial_capital: 10000, enabled: true, status: "live", orders: [],
  }));
  writeFileSync(join(rolesDir, "稳狗", "persona.md"), "稳健流\n");
  writeFileSync(join(cacheDir, "dogs.json"), JSON.stringify([
    { name: "稳狗", enabled: true, status: "live", scope: "jc" },
  ]));

  const taskReg = createTaskRegistry(cacheDir);
  const captured = {};
  registerTrainingTools({
    registerTool: (d) => { captured[d.name] = d; },
    taskReg,
    cacheDir,
    roles: resolveRoles({}, cacheDir),
  });
  return {
    cacheDir,
    rolesDir,
    tools: captured,
    cleanup: () => rmSync(cacheDir, { recursive: true, force: true }),
  };
}

test("训练工具：列表 / 创建 / 撞名 / 默认值", async (t) => {
  const fx = fixture();
  t.after(fx.cleanup);
  const { cacheDir, rolesDir, tools } = fx;

  const list = await tools.ds_list_dogs.execute({});
  assert.equal(list.count, 1);
  assert.equal(list.dogs[0].name, "稳狗");
  assert.equal(list.dogs[0].status, "live");
  assert.equal(list.dogs[0].observation, false);

  const dup = await tools.ds_create_dog.execute({ name: "稳狗", scope: "all", persona: "x" });
  assert.equal(dup.ok, false);
  assert.match(dup.error, /已存在/);

  const created = await tools.ds_create_dog.execute({
    name: "新狗", scope: "all", persona: "激进流",
  });
  assert.equal(created.ok, true);
  const role = JSON.parse(readFileSync(join(rolesDir, "新狗", "新狗.json"), "utf8"));
  assert.equal(role.status, "sandbox");
  assert.equal(role.enabled, false);
  assert.equal(role.capital, 10000);
  assert.equal(role.scope, "all");
  assert.equal(role.limits.max_exposure_pct, 40);
  assert.ok(readFileSync(join(rolesDir, "新狗", "persona.md"), "utf8").includes("激进流"));
});

test("训练工具：沙箱列表 / 转正（解析狗名 + 注册表翻 live）/ 放弃", async (t) => {
  const fx = fixture();
  t.after(fx.cleanup);
  const { cacheDir, rolesDir, tools } = fx;

  // 先创建新狗（注册表有条目，转正后应翻 live）
  const created = await tools.ds_create_dog.execute({
    name: "新狗", scope: "all", persona: "激进流",
  });
  assert.equal(created.ok, true);

  // 造一个 finished 沙箱（session.json 不带 dog 解析不了的场景由 promote 报错覆盖）
  const sbDir = join(cacheDir, "replays", "sandboxes", "新狗_0801");
  mkdirSync(join(sbDir, "workspace"), { recursive: true });
  writeFileSync(join(sbDir, "session.json"), JSON.stringify({
    dog: "新狗", status: "finished", start: "2026-08-01", end: "2026-08-07", next_idx: 7, days: ["2026-08-01"],
  }));
  writeFileSync(join(sbDir, "workspace", "新狗.json"), JSON.stringify({
    name: "新狗", capital: 13500, initial_capital: 10000, status: "sandbox", enabled: false, orders: [],
  }));
  writeFileSync(join(sbDir, "workspace", "persona.md"), "激进流 v2\n");

  const sbList = await tools.ds_sandbox_list.execute({ dog: "新狗" });
  assert.equal(sbList.count, 1);
  assert.equal(sbList.sandboxes[0].status, "finished");

  // 不传 dog：从 session.json 解析
  const promoted = await tools.ds_promote_sandbox.execute({ sandbox: "新狗_0801" });
  assert.equal(promoted.ok, true);
  assert.equal(promoted.dog, "新狗");
  assert.ok(promoted.backup);

  const liveRole = JSON.parse(readFileSync(join(rolesDir, "新狗", "新狗.json"), "utf8"));
  assert.equal(liveRole.status, "live");
  assert.equal(liveRole.enabled, true);
  assert.equal(liveRole.capital, 13500);
  const reg = JSON.parse(readFileSync(join(cacheDir, "dogs.json"), "utf8"));
  const regNew = reg.find((d) => d.name === "新狗");
  assert.equal(regNew.status, "live");
  assert.equal(regNew.enabled, true);

  const aborted = await tools.ds_abort_sandbox.execute({ sandbox: "新狗_0801" });
  assert.equal(aborted.ok, true);
  assert.equal(aborted.removed, true);
  assert.equal(existsSync(join(rolesDir, "新狗", "新狗.json")), true, "放弃后线上仍在");
});

test("训练工具：边界（缺沙箱 / 不存在的沙箱）", async (t) => {
  const fx = fixture();
  t.after(fx.cleanup);
  const { tools } = fx;

  const noSb = await tools.ds_promote_sandbox.execute({ sandbox: "" });
  assert.equal(noSb.ok, false);
  const missing = await tools.ds_promote_sandbox.execute({ sandbox: "不存在_0801" });
  assert.equal(missing.ok, false);
});
