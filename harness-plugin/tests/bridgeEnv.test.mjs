import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseEnvText, resolveChildEnv, defaultPythonBin } from "../bridge.js";

test("parseEnvText 兼容 export 前缀 / 引号 / 注释 / 跳过 shell 展开", () => {
  const env = parseEnvText(
    `# 注释\nexport DEEPSEEK_API_KEY="sk-a b"\nLOTA_API_KEY='x-y'\nBAD=$HOME/x\nEMPTY=\n`,
  );
  assert.equal(env.DEEPSEEK_API_KEY, "sk-a b");
  assert.equal(env.LOTA_API_KEY, "x-y");
  assert.equal(env.BAD, undefined);
  assert.equal(env.EMPTY, undefined);
});

test("resolveChildEnv 优先 envFile，且只填空缺", () => {
  const dir = mkdtempSync(join(tmpdir(), "ds-bridge-env-"));
  try {
    const envFile = join(dir, ".env");
    writeFileSync(envFile, "DS_TEST_KEY=from-file\nDS_ONLY_FILE=only-file\n");
    const env = resolveChildEnv({ envFile, engineRoot: dir });
    assert.equal(env.DS_TEST_KEY, "from-file");
    assert.equal(env.DS_ONLY_FILE, "only-file");
    // process.env 已有值不被 .env 覆盖
    const existing = Object.keys(process.env).find((k) => k.startsWith("PATH"));
    if (existing) {
      writeFileSync(envFile, `${existing}=hijack\n`);
      const env2 = resolveChildEnv({ envFile, engineRoot: dir });
      assert.notEqual(env2[existing], "hijack");
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("defaultPythonBin 按平台返回 python / python3", () => {
  const bin = defaultPythonBin();
  assert.ok(bin === "python" || bin === "python3");
});
