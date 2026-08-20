/**
 * @module tools/trainingTools
 *
 * 训练模式工具组（2026-08-20 grill 定稿）：让 agent 纯对话驱动「创建新狗 → 沙箱回放 →
 * 转正/放弃」，与斗狗场表单/按钮走同一批底层函数（dogRegistry.createDog / replay.js 沙箱
 * 生命周期），看板实时反映同一份状态，不存在第二套写入路径。
 *
 * 对话约定（skill ds-agents-training 负责执行）：
 *   - 创建狗：狗名/核心风格缺了才问；比赛范围必问（jc/beidan/all）；其余默认补齐
 *     （资金 10000、alpha 关、观察期 enabled=false、人设默认模板兜底）；
 *   - 回放入口走既有 ds_replay（不在这里重复注册）；
 *   - 转正：替换线上角色 + 注册表翻 live（promoteSandbox 内已同步）；
 *   - 放弃：删沙箱，线上不动。
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { withTask } from "../taskStatus.js";
import { LOOSE_OBJECT, jsonRender, readJson } from "./shared.js";
import { createDog, pythonRoleDirNames } from "../dogRegistry.js";
import { listSandboxes, promoteSandbox, abortSandbox } from "../replay.js";

/** 读某狗的 persona.md 原文（空/缺失返回 ""）。 */
function readPersonaFile(cacheDir, dog) {
  const p = join(cacheDir, "roles", String(dog || "").trim(), "persona.md");
  if (!existsSync(p)) return "";
  try {
    return readFileSync(p, "utf8").trim();
  } catch {
    return "";
  }
}

/**
 * 注册训练模式工具组。
 * @param {object} deps { registerTool, taskReg, cacheDir, roles }
 */
export function registerTrainingTools(deps) {
  const { registerTool, taskReg, cacheDir, roles } = deps;

  // ── ds_list_dogs：选狗入口（只读）──
  registerTool({
    name: "ds_list_dogs",
    description:
      "列出当前全部可选狗：live（默认列表）/ 观察期 / 沙箱在跑，含范围、alpha、资金、初始资金与沙箱数。选狗进回放时先调它。只读，不触网。",
    parameters: {},
    output: { schema: LOOSE_OBJECT, render: jsonRender(8000) },
    execute: withTask(taskReg, { type: "training", title: "狗列表" }, async () => {
      const names = (roles && Array.isArray(roles.dogs) && roles.dogs.length) ? [...roles.dogs] : [];
      const sandboxes = listSandboxes(cacheDir);
      const dogs = names.map((name) => {
        const role = readRoleFile(cacheDir, name);
        const status = roleStatusOf(role);
        const sb = sandboxes.filter((s) => s.dog === name);
        return {
          name,
          status,
          observation: status !== "live",
          scope: (roles && typeof roles.scopeFor === "function" ? roles.scopeFor(name) : "jc") || "jc",
          alpha_mode: Boolean(roles && typeof roles.alphaModeFor === "function" ? roles.alphaModeFor(name) : false),
          capital: Number((role && role.capital) || 0),
          initial_capital: Number((role && role.initial_capital) || 0),
          sandboxes: sb.map((s) => ({ name: s.name, status: s.status, start: s.start, end: s.end })),
        };
      });
      return { count: dogs.length, dogs };
    }),
  });

  // ── ds_create_dog：语言描述创建新狗 ──
  registerTool({
    name: "ds_create_dog",
    description:
      "创建新狗（与斗狗场「➕ 创建狗」表单同一逻辑）：写运行时注册表 dogs.json + 幂等补建 Python 角色（persona.md 为人设唯一源，<狗>.json 存限额/资金）。创建后默认观察期（enabled=false），转正后翻 live。",
    parameters: {
      name: { type: "string", required: true, description: "狗名（中英文/数字/下划线/短横线，1-24 字符）" },
      persona: { type: "string", description: "人设描述（写入 persona.md；缺省用默认模板）" },
      copy_from: { type: "string", description: "复制现有狗的人设（persona 未给时从其 persona.md 拷贝）" },
      scope: { type: "string", description: "比赛范围 jc/beidan/all（skill 约定必问，默认 jc）" },
      initial_capital: { type: "number", description: "初始资金（默认 10000）" },
      alpha_mode: { type: "boolean", description: "是否 alpha 模式（默认 false）" },
      enabled: { type: "boolean", description: "true=直接进全量默认列表；默认 false=观察期" },
      limits: { type: "object", description: "限额（max_exposure_pct 等，默认 max_exposure_pct=40）" },
      emoji: { type: "string", description: "展示表情（可选）" },
      c1: { type: "string", description: "头像渐变起始色（可选）" },
      c2: { type: "string", description: "头像渐变结束色（可选）" },
    },
    output: { schema: LOOSE_OBJECT, render: jsonRender(6000) },
    execute: withTask(taskReg, { type: "training", title: "创建狗" }, async (args) => {
      const name = String((args && args.name) || "").trim();
      if (!name) return { ok: false, error: "狗名必填" };
      let persona = typeof (args && args.persona) === "string" ? args.persona.trim() : "";
      const copyFrom = String((args && args.copy_from) || "").trim();
      if (copyFrom) {
        const src = readPersonaFile(cacheDir, copyFrom);
        if (!src) return { ok: false, error: `复制来源狗「${copyFrom}」的 persona.md 不存在` };
        if (!persona) persona = src;
      }
      const spec = {
        name,
        persona,
        scope: (args && args.scope) || "jc",
        initial_capital: args && args.initial_capital,
        alpha_mode: Boolean(args && args.alpha_mode),
        enabled: Boolean(args && args.enabled),
        // 与斗狗场表单默认一致：max_exposure_pct=40%（否则 limits 全 null = 不启用约束）
        limits: (args && args.limits) || { max_exposure_pct: 40 },
        emoji: (args && args.emoji) || "",
        c1: (args && args.c1) || "",
        c2: (args && args.c2) || "",
      };
      const existingNames = [
        ...((roles && Array.isArray(roles.dogs)) ? roles.dogs : []),
        ...pythonRoleDirNames(cacheDir),
      ];
      return await createDog(cacheDir, null, spec, { existingNames });
    }),
  });

  // ── ds_sandbox_list：沙箱列表（续跑 / 汇总 / 转正前确认）──
  registerTool({
    name: "ds_sandbox_list",
    description:
      "列出回放沙箱（replays/sandboxes/<狗>_<MMDD>/）：paused/running/finished/created，含方向建议（paused 可编辑后带 induction_notes 续跑）、facts/report 是否存在。续跑或汇报训练进度时先调它。只读。",
    parameters: {
      dog: { type: "string", description: "按狗过滤（可选）" },
    },
    output: { schema: LOOSE_OBJECT, render: jsonRender(10000) },
    execute: withTask(taskReg, { type: "training", title: "沙箱列表" }, async (args) => {
      const dog = String((args && args.dog) || "").trim();
      const all = listSandboxes(cacheDir);
      const list = dog ? all.filter((s) => s.dog === dog) : all;
      return { count: list.length, sandboxes: list };
    }),
  });

  // ── ds_promote_sandbox：转正 ──
  registerTool({
    name: "ds_promote_sandbox",
    description:
      "沙箱转正：备份线上角色到 backups/promote_* → workspace 整目录替换线上 → 注册表翻 live（enabled=true, status=live，进全量默认列表）。**必须用户明确表态才调用**（可提前授权）。",
    parameters: {
      sandbox: { type: "string", required: true, description: "沙箱名，如 梭哈2狗_0718" },
      dog: { type: "string", description: "狗名（缺省从沙箱 session.json 解析）" },
    },
    output: { schema: LOOSE_OBJECT, render: jsonRender(4000) },
    execute: withTask(taskReg, { type: "training", title: "转正" }, async (args) => {
      const sandbox = String((args && args.sandbox) || "").trim();
      if (!sandbox) return { ok: false, error: "sandbox 必填（如 梭哈2狗_0718）" };
      let dog = String((args && args.dog) || "").trim();
      if (!dog) {
        const s = readJson(join(cacheDir, "replays", "sandboxes", sandbox, "session.json"));
        dog = String((s && s.dog) || "").trim();
      }
      if (!dog) return { ok: false, error: `无法从沙箱 ${sandbox} 解析狗名，请显式传 dog` };
      return promoteSandbox(cacheDir, sandbox, dog);
    }),
  });

  // ── ds_abort_sandbox：放弃 ──
  registerTool({
    name: "ds_abort_sandbox",
    description:
      "放弃沙箱：删除 replays/sandboxes/<沙箱>，线上角色不动。**必须用户明确表态才调用**。",
    parameters: {
      sandbox: { type: "string", required: true, description: "沙箱名，如 梭哈2狗_0718" },
    },
    output: { schema: LOOSE_OBJECT, render: jsonRender(2000) },
    execute: withTask(taskReg, { type: "training", title: "放弃沙箱" }, async (args) => {
      const sandbox = String((args && args.sandbox) || "").trim();
      if (!sandbox) return { ok: false, error: "sandbox 必填（如 梭哈2狗_0718）" };
      return abortSandbox(cacheDir, sandbox);
    }),
  });
}

/** 读角色文件 JSON（不存在/损坏返回 null）。 */
function readRoleFile(cacheDir, dog) {
  return readJson(join(cacheDir, "roles", String(dog || "").trim(), `${String(dog || "").trim()}.json`));
}

/** 由角色文件派生状态：status 优先，缺省由 enabled 推导；无文件返回 sandbox（观察）。 */
function roleStatusOf(role) {
  if (role && ["live", "sandbox", "archived"].includes(role.status)) return role.status;
  if (role && typeof role.enabled === "boolean") return role.enabled ? "live" : "sandbox";
  return "sandbox";
}
