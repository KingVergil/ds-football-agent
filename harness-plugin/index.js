/**
 * @module ds-agents-lota-data
 *
 * 开源 harness 插件：本地缓存读取工具（data 缓存目录）。
 *
 * dsh 薄壳：数据唯一真源 = python-engine/data 文件。固定流全部走 python 桥
 * （POST /ds-run、/ds-replay 直连 python -m src.bridge）；agent 面板只剩只读数据工具。
 *
 * 插件形状对齐 DSH 真实工具插件（参见 @deepseek-ai/dsh-tool-bash）：
 *   - 导出 { name, inject, apply }
 *   - apply(ctx, config) 内装配 dashboard/定时器/systemPrompt，再注册工具：
 *       · 只读数据工具（agent 面板）    → tools/deterministic.js
 *       · 回放（harness 唯一工作流入口）→ tools/replayTool.js
 *       固定流（分析/结算/因子）执行入口 = dashboard 表单 → python 桥（tools/roles.js 只做角色解析）。
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { setupDashboard } from "./dashboard.js";
import { createTaskRegistry } from "./taskStatus.js";
import { runBridge, defaultPythonBin } from "./bridge.js";
import { resolveRoles } from "./tools/roles.js";
import { registerDeterministicTools } from "./tools/deterministic.js";
import { registerReplayTool } from "./tools/replayTool.js";
import { registerTrainingTools } from "./tools/trainingTools.js";

const name = "lota-data";
// webServer 不放进 inject：headless 没有 web 服务，放进去会导致插件 pending。
// dashboard 用 ctx.webServer 判空优雅跳过；web 下 cordis 的 ctx.get 仍能取到该服务。
const inject = ["tools", "storageDomain", "llm", "systemPrompt", "subagents"];

// dsh-scope 顶层加载（避免 agent/created 与动态 import 的竞态）；无 dsh 环境时为 null
let dshScope = null;
try {
  dshScope = await import("@deepseek-ai/dsh-scope");
} catch {
  dshScope = null;
}

/** 单进程单例守卫：同一进程内多个 preset 挂载本插件时，全局副作用（工具/路由/定时器/prompt section）只注册一次。 */
const __singletonKeys = new Set();
function once(key) {
  if (__singletonKeys.has(key)) return false;
  __singletonKeys.add(key);
  return true;
}

/**
 * agent 模式：toolAllowlist 时，监听 agent/created，对加入本 preset 的 agent
 * 用其 scoped ctx 调 tools.restrict({allow})，把宿主平面（bash/fs/skills 等）工具一并遮蔽。
 * 动态 import dsh-scope，避免在无 dsh 环境下加载失败。
 */
function setupAgentModeRestrict(ctx, cacheDir, config) {
  // config.toolAllowlist（单模式）或 config.agentModePresets（preset 名 → 工具列表）
  const presets = config.agentModePresets && typeof config.agentModePresets === "object"
    ? config.agentModePresets
    : null;
  const single = Array.isArray(config.toolAllowlist) && config.toolAllowlist.length
    ? [...config.toolAllowlist]
    : null;
  if (!presets && !single) return;
  if (!dshScope) {
    console.warn(`[lota-data] dsh-scope 不可用，agent 模式无法按 scope 遮蔽工具`);
    return;
  }
  const { scopeOf, scopeParentOf } = dshScope;
  // 本插件实例所在的 scope（预设挂载时 = preset standing scope；profile 挂载时 = 根 scope）
  let myScope = null;
  try { myScope = scopeOf(ctx) ?? null; } catch {}
  const debugLog = [];
  const record = (entry) => {
    debugLog.push(entry);
    try {
      const dir = join(cacheDir, "tasks");
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, "agent-mode-debug.json"), JSON.stringify({ events: debugLog.slice(-50) }, null, 2), "utf8");
    } catch {}
  };
  ctx.on("agent/created", ({ agent }) => {
    try {
      const agentCtx = agent && agent.ctx;
      const agentId = String((agent && agent.id) || agent && agent.ctx && "?");
      const agentScope = agentCtx ? scopeOf(agentCtx) : undefined;
      const presetScope = agentScope ? scopeParentOf(agentScope) : undefined;
      const presetKey = presetScope == null ? "" : String(presetScope);
      let allowlist = null;
      if (single) {
        allowlist = single;
      } else if (presets) {
        for (const [name, tools] of Object.entries(presets)) {
          // 恒等优先（dsh 内部按 scope key 对象比较）；字符串兜底
          const byIdentity = myScope != null && presetScope === myScope;
          if (byIdentity || presetKey === name || presetKey.endsWith(`/${name}`) || presetKey.includes(name)) {
            allowlist = tools;
            break;
          }
        }
      }
      const matched = Boolean(allowlist && allowlist.length);
      record({ agent: agentId, hasCtx: Boolean(agentCtx), agentScope: agentScope == null ? null : String(agentScope), presetKey, byIdentity: myScope != null && presetScope === myScope, matched, allowlist: allowlist || null });
      if (matched && agentCtx && typeof agentCtx.tools?.restrict === "function") {
        agentCtx.tools.restrict({ allow: allowlist });
        console.log(`[lota-data] agent 模式白名单已应用 ${agentId}: ${allowlist.join(", ")}`);
      }
    } catch (e) {
      record({ error: String((e && e.message) || e) });
      console.warn(`[lota-data] agent 模式 restrict 失败: ${(e && e.message) || e}`);
    }
  });
}

/** 效果 A analyze 框架 section（主循环 LLM 的 prompt 段，见 harness_js_reconstruction.md §2.5）。 */
const ANALYZE_FRAMEWORK_SECTION = {
  name: "ds-agents-analyze",
  order: 150,
  text: `## ds-agents 薄壳说明

dsh 只做薄壳：数据准备/分析/结算/因子归纳/因子退役/状态/刷新/重置全部由 python-engine
执行（入口是斗狗场表单 → POST /ds-run 直连 python 桥；回放由 /ds-replay 插件侧编排，逐日逐 func 调桥）。

你（harness agent）的工具面板：只读数据工具（lota_matches / lota_match / lota_sections / lota_status，
回答比赛/缓存/角色状态查询）+ 回放（ds_replay）+ 训练模式工具组（ds_list_dogs / ds_create_dog /
ds_sandbox_list / ds_promote_sandbox / ds_abort_sandbox，用于训练：创建新狗/选狗→回放→转正/放弃，
流程见 skill ds-agents-training）。**不要尝试用 bash / 文件操作 / 手工编排去执行固定流**——
执行入口只有 dashboard，固定流工具已从面板移除。

## 回放模式的唯一 LLM 职责

- 回放（/ds-replay）由插件侧编排：每周期因子退役结束会暂停，卡片预填「下一轮方向建议」。
- 你的职责：暂停后基于 factor-review 结果（退役/休眠因子、周期 PnL、用户意见）起草方向建议；
  用户编辑确认后，插件把它作为下一周期 user_notes 注入退役评估。
- 只做这一件事：不要主动编排回放、不要调用固定流。`,
};

/**
 * 比赛信息定时刷新：每 30 分钟调一次 python 桥 prepare（live 模式），刷新
 * matches/features/tags 缓存（比分/状态/赔率，供斗狗场展示）。dsh 启动即生效；
 * 无 LOTA_API_KEY 的离线环境会失败并自动跳过。
 */
function startMatchRefresher(ctx, engineRoot, cacheDir, pythonBin = defaultPythonBin(), envFile = "") {
  const footballDay = () => {
    const ts = Date.now() - (12 * 3600 + 60) * 1000;
    return new Date(ts + 8 * 3600 * 1000).toISOString().slice(0, 10);
  };
  let running = false;
  const refresh = async () => {
    if (running) return;
    running = true;
    try {
      const r = await runBridge({
        pythonBin,
        engineRoot,
        envFile,
        req: { func: "prepare", day: footballDay(), opts: { mode: "live", jingcai_only: true } },
        timeoutMs: 10 * 60 * 1000,
      });
      if (!r.ok) console.warn(`[lota-data] 定时刷新失败: ${r.error}`);
    } catch (e) {
      console.warn(`[lota-data] 定时刷新失败: ${(e && e.message) || e}`);
    } finally {
      running = false;
    }
  };
  refresh(); // 启动即刷一次
  const timer = setInterval(refresh, 30 * 60 * 1000);
  ctx.effect(() => () => clearInterval(timer));
}

function apply(ctx, config = {}) {
  const cacheDir = resolve(config.cacheDir ?? "data");
  const engineRoot = resolve(config.engineRoot ?? join(cacheDir, ".."));
  const pythonBin = config.pythonBin ?? defaultPythonBin();
  const envFile = config.envFile ?? "";
  const toolAllowlist = Array.isArray(config.toolAllowlist) && config.toolAllowlist.length
    ? [...config.toolAllowlist]
    : null;
  const taskReg = createTaskRegistry(cacheDir);

  // ── 角色解析层（User Role）：config.roles / config.personaDir 可外置修改，缺省=旧行为 ──
  const roles = resolveRoles(config, cacheDir);

  // ── 「斗狗场」仪表盘（/ds-dashboard + /ds-run + /ds-replay，客户端 tab）──
  if (once("dashboard-routes")) setupDashboard(ctx, cacheDir, roles, config.avatarDir, {
    engineRoot,
    pythonBin,
    envFile,
    taskReg,
  });

  // ── 比赛信息定时刷新（每 30 分钟，dsh 启动期间）──
  if (once(`refresher:${engineRoot}`)) startMatchRefresher(ctx, engineRoot, cacheDir, pythonBin, envFile);

  // ── 主循环 LLM 的 prompt section（analyze 框架 + 资金管理 + 结构化下单）──
  if (once("section:ds-agents-analyze")) ctx.systemPrompt.section(ANALYZE_FRAMEWORK_SECTION);

  // ── 工具注册（按本 preset 的 scope 注册；toolAllowlist 时只注册白名单内工具）──
  const registerTool = (definition) => {
    if (toolAllowlist && !toolAllowlist.includes(definition.name)) return;
    ctx.tools.register(defineTool(definition));
  };

  // ── agent 模式：toolAllowlist / agentModePresets 时，对加入对应 preset 的 agent
  //    用 agent scoped ctx 遮蔽宿主平面工具（bash/fs/skills 等）──
  if (toolAllowlist || config.agentModePresets) {
    setupAgentModeRestrict(ctx, cacheDir, config);
  }

  // ── 只读数据工具组（agent 面板唯一工具集；固定流执行入口= dashboard 表单）──
  registerDeterministicTools({
    registerTool, taskReg,
    cacheDir, engineRoot, pythonBin, envFile,
  });

  // ── ds_replay：harness 唯一保留的工作流入口（agent 驱动，插件侧拼装）──
  registerReplayTool({
    ctx, registerTool, taskReg,
    cacheDir, engineRoot, pythonBin, envFile,
  });

  // ── 训练模式工具组（创建狗/选狗/沙箱列表/转正/放弃；回放入口仍走 ds_replay）──
  registerTrainingTools({
    registerTool, taskReg, cacheDir, roles,
  });
}

export { apply, inject, name };
