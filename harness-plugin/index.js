/**
 * @module ds-agents-lota-data
 *
 * 开源 harness 插件：本地缓存读取工具（data 缓存目录）。
 *
 * 只做「读」，不做网络。数据来源由用户按 Fetcher 协议自行提供；
 * 盘上格式见 docs/cache_format_spec.md（本插件按该规范直接读 JSON）。
 *
 * 插件形状对齐 DSH 真实工具插件（参见 @deepseek-ai/dsh-tool-bash）：
 *   - 导出 { name, inject, apply }
 *   - apply(ctx, config) 内装配 storage/dashboard/定时器/systemPrompt，再按语义分组注册工具：
 *       · 角色（User Role）           → tools/roles.js
 *       · 固定无 LLM 工作流            → tools/deterministic.js
 *       · 有 LLM 但 headless 工作流    → tools/headless.js
 */
import { writeFileSync, mkdirSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { setupDomains } from "./storage.js";
import { setupDashboard } from "./dashboard.js";
import { createTaskRegistry } from "./taskStatus.js";
import { findMatch } from "./tools/shared.js";
import { resolveRoles, ensureRoleRecords, registerRoleTools } from "./tools/roles.js";
import { registerDeterministicTools } from "./tools/deterministic.js";
import { registerHeadlessTools } from "./tools/headless.js";

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
  text: `## ds-agents 效果 A：足球投注分析工作流

当用户要做足球投注分析/下单时，你（harness agent）是「大脑」，私有 Python 引擎只做确定性基础功能。

0. 分析统一走 ds_analyze_dog(dog, day)：工具内部**确定性**完成 数据准备（prepareDay live 单例、竞彩过滤、strip_scores）→ 回退订单 → 读人设/记忆/资金 → 逐场段落 → 构建一个 prompt → **只调一次 LLM（temperature 0.1）** → 解析结构化订单 → 确定性下单。全程无工具轮次，LLM 只在判断时被调用一次（每次调用起一个最小分析子任务保证 session 可查）。
1. 全狗分析（并行由你决定）：用户说「分析7狗 / 全部分析 / 分析全部狗 / 跑全部狗」时，对每只狗分别调 ds_analyze_dog(dog, day)，并列发起即并行；禁止在主循环里手动逐场读数据/下单（已内建在工具里）。
2. 数据完整性护栏（工具内建）：缺 asian-handicap-* 段禁下亚盘、缺 over-under-* 段禁下大小球；submit_orders 会拒绝缺权威盘口的亚盘单。

## 回放模式

- ds_replay(start, end, ...)：把「获取比赛→分析→结算→因子归纳→周期性因子退役」按日跑一遍（历史数据缓存优先、缺了才拉 URL），记录每狗每日轨迹并出报告。默认写穿：订单/因子直接落库（restore_after=false）；restore_after=true 才是模拟跑、结束还原起点。若 [start,end] 内目标狗已有订单，直接拒绝（diff/diff-report 未设计）。周期性退役支持 user_notes（用户调整意见）与 persona_overrides（人设覆盖）注入评估方向。

## 资金管理（金额语义）

- 金额 = 信心比例 × 全金额（先 ds_capital_js 拿 full_capital）。
- 比例档位：最有信心 30–40% / 次之 15–20% / 试探 5–10%（连输 3 场后最大仓位降到 10%），以 persona.md 为准。
- 确定性层二次处理：去重 / 已开赛跳过 / 资金折算(scale=余额/全金额) / 硬约束 / 破产检查。你只需给方向 + 比例。
- submit_orders 每单字段：lota_id / bet_type(亚盘|大小球|胜平负|让球胜平负) / pick(H|A|D|over|under) / odds / handicap(亚盘盘口主队视觉) / bet_size / reason。不下注则不放该场，或 skip:true。

## 结算 + 反思

- 结算：ds_settle_js(user, day) → 返回 settled orders（含 hit/profit/reason）。
- 反思：ds_reflect_js(user, day, settled) → 旁路 LLM 因子发现 + 写回因子/反思记忆。
- 因子退役：ds_factor_review_js(user, end_date, start_date?) → 门控 + 旁路 LLM 结构性评估。
- 因子判重：ds_factor_dedup(user, factor_id, desc) → create/merge/suppress。`,
};

/**
 * 比赛信息定时刷新：每 30 分钟跑一次私有 lota_fetcher 的 refresh-date，刷新 matches
 * 缓存里的比分/状态（供斗狗场展示）。dsh 启动即生效；无私有 fetcher（开源环境）自动跳过。
 */
function startMatchRefresher(ctx, engineRoot, cacheDir) {
  // 私有 fetcher 优先插件目录（单独分发），缺了回退 engineRoot（老位置兼容）
  const local = join(fileURLToPath(new URL(".", import.meta.url)), "lota_fetcher.js");
  const fetcher = existsSync(local) ? local : join(engineRoot, "lota_fetcher.js");
  if (!existsSync(fetcher)) return;
  const bj = (ts) => new Date(ts + 8 * 3600 * 1000).toISOString().slice(0, 10);
  const footballDay = () => bj(Date.now() - (12 * 3600 + 60) * 1000);
  let running = false;
  const refresh = () => {
    if (running) return;
    running = true;
    const c = spawn(process.execPath, [fetcher, "refresh-date", footballDay()], {
      stdio: "ignore",
      cwd: engineRoot,
      env: { ...process.env, LOTA_DATA_ROOT: cacheDir },
    });
    c.on("exit", () => { running = false; });
    c.on("error", () => { running = false; });
  };
  refresh(); // 启动即刷一次
  const timer = setInterval(refresh, 30 * 60 * 1000);
  ctx.effect(() => () => clearInterval(timer));
}

/**
 * 定时邮件任务：每天在 config.scheduledEmails 指定的时间点（HH:MM，北京时间），
 * 自动触发 harness 侧全部分析（headless agent 跑「全部分析」+ ds_export_to_python）
 * + 按 email_recipients.txt 名单发邮件。dsh 启动期间生效。
 */
function startScheduledJobs(ctx, engineRoot, config, dogs = []) {
  // 防递归：本插件拉起的 headless 子进程带 DSH_SCHEDULED_RUN=1，不再注册定时器
  // （否则子进程 tick() 注册即检查，同一分钟又触发拉起孙进程 → 无限递归）
  if (process.env.DSH_SCHEDULED_RUN === "1") return;
  const times = Array.isArray(config.scheduledEmails) && config.scheduledEmails.length
    ? config.scheduledEmails : ["15:58", "18:15", "20:15"];
  const agents = Array.isArray(config.scheduledEmailAgents) && config.scheduledEmailAgents.length
    ? config.scheduledEmailAgents : ["梭哈2狗", "均注狗", "跟风狗", "alpha2狗"];

  const bj = (ts) => new Date(ts + 8 * 3600 * 1000);
  const dayOf = (ts) => bj(ts).toISOString().slice(0, 10);
  const hhmm = (ts) => {
    const d = bj(ts);
    return String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
  };

  const fired = new Set();
  let lastDay = "";
  let running = false;

  const run = () => {
    if (running) return;
    running = true;
    // 1) harness 侧分析：对每只单关狗调 ds_analyze_dog（内部固有取数 + 一次 LLM 决策；可并列并行）→ ds_export_to_python 导出到 data/roles
    const dogList = (Array.isArray(dogs) && dogs.length ? dogs : ["梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗", "alpha狗", "alpha2狗"]);
    const task = `全部分析：对每只单关狗（${dogList.join("、")}）分别调用 ds_analyze_dog(dog=<狗名>)（同一轮里并列发起以并行；工具内部固有完成数据准备/人设/记忆/资金/段落与一次 LLM 决策），全部完成后调用 ds_export_to_python(dry_run=false) 导出到 Python 数据层。`;
    // 2) 发邮件：Python send_order_email 按 email_recipients.txt 名单（agent 过滤）
    const agentsPy = JSON.stringify(agents);
    const emailPy = `from src.order_email import send_order_email; [send_order_email(a, None) for a in ${agentsPy}]`;
    // headless 需要 DEEPSEEK_API_KEY；dsh web 进程环境可能没有（非交互式启动），从 ~/.zshrc 兜底读一次
    const script = `export DEEPSEEK_API_KEY="$(grep -o 'sk-[a-zA-Z0-9]*' ~/.zshrc | head -1)" && export DSH_SCHEDULED_RUN=1 && dsh --profile headless "${task}" && cd "${engineRoot}" && python3 -c "${emailPy}"`;
    const c = spawn("/bin/bash", ["-c", script], { stdio: "ignore" });
    c.on("exit", () => { running = false; });
    c.on("error", () => { running = false; });
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

  // ── storage 域（纯 JS 状态层，见 harness_js_reconstruction.md §7）──
  const domainHandles = setupDomains(ctx);

  // ── config.roles / 运行时注册表（创建狗）新狗初始化：只建缺失角色，已有角色资金/订单不动 ──
  if (roles.configured || roles.hasRegistry) ensureRoleRecords(domainHandles, roles);

  // ── 「斗狗场」仪表盘（/ds-dashboard JSON + /ds-avatars 图片 + /ds-analyze 直启，客户端 tab）──
  if (once("dashboard-routes")) setupDashboard(ctx, domainHandles, cacheDir, roles, config.avatarDir, {
    engineRoot,
    pythonBin,
    taskReg,
  });

  // ── 比赛信息定时刷新（每 30 分钟，dsh 启动期间）──
  if (once(`refresher:${engineRoot}`)) startMatchRefresher(ctx, engineRoot, cacheDir);

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

  // ── 按语义分组注册工具 ──
  const deps = {
    ctx, registerTool, taskReg, domainHandles,
    cacheDir, engineRoot, pythonBin,
    roles,
    helpers: { findMatch },
  };
  registerRoleTools(deps);          // 【User Role】人设/记忆/资金
  registerDeterministicTools(deps); // 【固定无 LLM 工作流】data_fetch / settle_orders / 只读缓存 / 迁移
  registerHeadlessTools(deps);      // 【有 LLM 但 headless 工作流】分析 / settles / induct / review / replay
}

export { apply, inject, name };
