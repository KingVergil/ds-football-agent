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
import { spawn } from "node:child_process";
import { join, resolve } from "node:path";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { setupDashboard } from "./dashboard.js";
import { createTaskRegistry } from "./taskStatus.js";
import { runBridge } from "./bridge.js";
import { resolveRoles } from "./tools/roles.js";
import { registerDeterministicTools } from "./tools/deterministic.js";
import { registerReplayTool } from "./tools/replayTool.js";

const name = "lota-data";
// webServer 不放进 inject：headless(定时分析)没有 web 服务，放进去会导致插件 pending。
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

你（harness agent）的工具面板只有只读数据工具（lota_matches / lota_match / lota_sections / lota_status），
用于回答用户关于比赛/缓存/角色状态的查询。**不要尝试用 bash / 文件操作 / 手工编排去执行固定流**——
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
function startMatchRefresher(ctx, engineRoot, cacheDir, pythonBin = "python") {
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

/**
 * 定时任务：每天在 config.scheduledEmails 指定的时间点（HH:MM，北京时间），
 * 逐狗调 python 桥 analyze（live）→ 按 email_recipients.txt 名单发邮件。
 * 全部直接 spawn python（无 bash -c、无 headless agent）。
 */
function startScheduledJobs(ctx, engineRoot, config, dogs = []) {
  const times = Array.isArray(config.scheduledEmails) && config.scheduledEmails.length
    ? config.scheduledEmails : ["15:58", "18:15", "20:15"];
  const agents = Array.isArray(config.scheduledEmailAgents) && config.scheduledEmailAgents.length
    ? config.scheduledEmailAgents : ["梭哈2狗", "均注狗", "跟风狗", "alpha2狗"];
  const pythonBin = config.pythonBin ?? "python";

  const bj = (ts) => new Date(ts + 8 * 3600 * 1000);
  const dayOf = (ts) => bj(ts).toISOString().slice(0, 10);
  const hhmm = (ts) => {
    const d = bj(ts);
    return String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
  };

  const fired = new Set();
  let lastDay = "";
  let running = false;

  const runPythonCmd = (args) => new Promise((resolve2) => {
    const c = spawn(pythonBin, args, { cwd: engineRoot, stdio: "ignore", env: { ...process.env } });
    c.on("exit", () => resolve2());
    c.on("error", () => resolve2());
  });

  const run = async () => {
    if (running) return;
    running = true;
    try {
      // 1) 逐狗 analyze（python 桥，live 语义：内部 refresh + 取数 + LLM 决策 + 下单）
      const dogList = (Array.isArray(dogs) && dogs.length
        ? dogs : ["梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗", "alpha狗", "alpha2狗"]);
      const bj = (ts) => new Date(ts + 8 * 3600 * 1000).toISOString().slice(0, 10);
      const day = bj(Date.now() - (12 * 3600 + 60) * 1000);
      for (const dog of dogList) {
        const r = await runBridge({
          pythonBin,
          engineRoot,
          req: { func: "analyze", dog, day, opts: { live: true, prefetched: false, jingcai_only: true } },
        });
        if (!r.ok) console.warn(`[lota-data] 定时分析失败 ${dog} ${day}: ${r.error}`);
      }
      // 2) 发邮件：Python send_order_email 按 email_recipients.txt 名单（agent 过滤）
      const emailPy = `from src.order_email import send_order_email; [send_order_email(a, None) for a in ${JSON.stringify(agents)}]`;
      await runPythonCmd(["-c", emailPy]);
    } catch (e) {
      console.warn(`[lota-data] 定时任务失败: ${(e && e.message) || e}`);
    } finally {
      running = false;
    }
  };

  const tick = () => {
    const now = Date.now();
    const day = dayOf(now);
    if (day !== lastDay) { fired.clear(); lastDay = day; }
    const t = hhmm(now);
    if (times.includes(t) && !fired.has(t)) {
      fired.add(t);
      run();
    }
  };

  tick(); // 注册即检查一次，避免重启后等 30 秒错过整分钟时间点
  const timer = setInterval(tick, 30 * 1000);
  ctx.effect(() => () => clearInterval(timer));
}

function apply(ctx, config = {}) {
  const cacheDir = resolve(config.cacheDir ?? "data");
  const engineRoot = resolve(config.engineRoot ?? join(cacheDir, ".."));
  const pythonBin = config.pythonBin ?? "python";
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
    taskReg,
  });

  // ── 比赛信息定时刷新（每 30 分钟，dsh 启动期间）──
  if (once(`refresher:${engineRoot}`)) startMatchRefresher(ctx, engineRoot, cacheDir, pythonBin);

  // ── 定时邮件任务（每天固定时间点跑全狗分析 + 发邮件，dsh 启动期间）──
  if (once(`scheduler:${engineRoot}`)) startScheduledJobs(ctx, engineRoot, config, roles.dogs);

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
    cacheDir, engineRoot, pythonBin,
  });

  // ── ds_replay：harness 唯一保留的工作流入口（agent 驱动，插件侧拼装）──
  registerReplayTool({
    ctx, registerTool, taskReg,
    cacheDir, engineRoot, pythonBin,
  });
}

export { apply, inject, name };
