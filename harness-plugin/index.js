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
 *   - apply(ctx, config) 内用 ctx.tools.register(defineTool({...}))
 */
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { join, resolve } from "node:path";
import { defineTool } from "@deepseek-ai/dsh-tools";
// ⚠️ 数据专有：见 odds.js 头部注释。用户自定义数据源时需修改/替换 odds.js。
import { extractOdds } from "./odds.js";
import { setupDomains, migrateFromPython, capitalQuery, exportToPython } from "./storage.js";
import { setupDashboard } from "./dashboard.js";
import { memoryQuery } from "./memory.js";
import { beijingNowIso } from "./settle.js";
import { settleDog, fetchScoresFromCache } from "./settleEngine.js";
import { submitOrders, refreshOrders } from "./placeOrders.js";
import { analyzeDogsParallel } from "./fanout.js";
import { prepareDay, prepareRange } from "./dataflow.js";
import { runReplay } from "./replay.js";
import {
  buildReflectPrompt, streamReflectJson, parseReflectJson, applyReflection,
  readPersona, getExistingFactorSummary, SLUG_WHITELIST, REFLECT_DEFAULT,
} from "./reflect.js";
import { factorReview } from "./factorReview.js";
import { judgeFactorDedup, inductFactors, inductAlpha, ALPHA_DOGS } from "./factorInduction.js";

const name = "lota-data";
// webServer 不放进 inject：headless(定时分析)没有 web 服务，放进去会导致插件 pending。
// dashboard 用 ctx.webServer 判空优雅跳过；web 下 cordis 的 ctx.get 仍能取到该服务。
const inject = ["tools", "storageDomain", "llm", "systemPrompt", "subagents"];

/** 效果 A analyze 框架 section（主循环 LLM 的 prompt 段，见 harness_js_reconstruction.md §2.5）。 */
const ANALYZE_FRAMEWORK_SECTION = {
  name: "ds-agents-analyze",
  order: 150,
  text: `## ds-agents 效果 A：足球投注分析工作流

当用户要做足球投注分析/下单时，你（harness agent）是「大脑」，私有 Python 引擎只做确定性基础功能。

0. 数据获取（LLM 之前确定性完成，禁止模型自己拉全量）：先调 ds_prepare_day(day, mode="live")，一次性获取并过滤竞彩比赛，直接用返回的比赛列表。⚠️ 禁止再调 lota_matches 拉全量（会混入北单/无号场次）；lota_sections/lota_match 只用于按需读单场段落。
1. 重跑前置（live 重跑当天才做）：refresh_orders(<狗名>, day) 退回窗口内未开赛订单金额，保留已开赛订单——对齐旧 LangGraph analyze(live=True) 的行为。
2. 读人设：ds_persona_js(<狗名>) 确定性注入人设（禁止自己 read 文件），拿该狗的投注风格与仓位档位。
3. 读记忆：ds_memory_js(<狗名>, day) 拿该狗的历史因子记忆——活跃因子/已证伪模式/历史反思/最近订单/昨日结算回顾。⚠️ 判断时必须结合活跃因子与已证伪模式（例如「离散极低」这类信号历史上可能是诱杀而非看好），不要只按直觉解读离散凝聚。
4. 查资金：ds_capital_js(<狗名>) 拿 full_capital（全金额 = 余额 + 锁定敞口）与 limits 约束。
5. 判断：基于赛前数据 + 因子记忆独立推理（⚠️ ds_prepare_day 已 strip_scores 防后视），按信心给「比例」。
6. 下单：submit_orders(user, day, orders) 结构化下单（去重/已开赛保护/资金折算/硬约束/扣资金）。
6a. 逐场选场约束：当日候选比赛 ≤ 50 场时，必须逐场读全所有比赛的关键段落再选场，禁止只读少数几场（漏读=丢机会）；只有 >50 场才允许先按联赛/时间粗筛候选。逐场 lota_sections(id, slugs=["fair-odds","asian-handicap-pinnacle","over-under-crown","betfair-buysell","discrete-odds"]) 取关键段落。
6b. 并行全狗：当用户说「分析7狗 / 全部分析 / 分析全部狗 / 跑全部狗」时，必须调 ds_analyze_all_parallel(day, parallel=7) fan-out 7 个独立 subagent（每狗一个会话并行跑），禁止自己顺序逐狗分析；等它返回汇总即可。

## 回放模式

- ds_replay(start, end, ...)：把「获取比赛→分析→结算→因子归纳→周期性因子退役」按日跑一遍（历史数据缓存优先、缺了才拉 URL），记录每狗每日轨迹并出报告。周期性退役支持 user_notes（用户调整意见）与 persona_overrides（人设覆盖）注入评估方向。

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

/** 缓存格式里的 kind → 子目录名（与 cache_format_spec.md 一致）。 */
const KIND_DIR = {
  matches: "matches",
  features: "features",
  tags: "tags",
  predicts: "predicts",
  orders: "orders",
};

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

/** 读 matches/<date>.json，兼容 list 与 {matches:[...]} 两种顶层。 */
function readMatches(cacheDir, date) {
  const raw = readJson(join(cacheDir, KIND_DIR.matches, `${date}.json`));
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.matches)) return raw.matches;
  return [];
}

/** 列举某 kind 下的全部 key（文件名去后缀）。 */
function listKeys(cacheDir, kind) {
  const dir = join(cacheDir, KIND_DIR[kind]);
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.slice(0, -".json".length))
    .sort();
}

/**
 * 归一化 features 的三种历史形状（对齐 cache_format_spec.md §2）：
 *   A 顶层字段 / B data 子对象 / C 负缓存桩。
 */
function normalizeFeature(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw._api_failed) {
    return {
      lota_id: raw.lota_id ?? null,
      compact_fet: "",
      score: "",
      _cached_at: raw._cached_at ?? null,
      _api_failed: true,
    };
  }
  const inner = raw.data && typeof raw.data === "object" ? raw.data : {};
  const match = raw.match || inner.match || null;
  const compact_fet = raw.compact_fet || inner.compact_fet || "";
  const score = raw.score || inner.score || "";
  const out = {
    lota_id: raw.lota_id ?? null,
    compact_fet,
    score,
    _cached_at: raw._cached_at ?? null,
  };
  if (match) out.match = match;
  for (const k of ["success", "lang", "metadata", "api_info"]) {
    if (raw[k] !== undefined) out[k] = raw[k];
  }
  return out;
}

/** 按 lota_id 找比赛：features 优先，再扫最近 30 个 matches 日期文件。 */
function findMatch(cacheDir, lotaId) {
  const feat = normalizeFeature(readJson(join(cacheDir, KIND_DIR.features, `${lotaId}.json`)));
  if (feat && feat.match) return feat.match;
  const dates = listKeys(cacheDir, "matches").slice(-30);
  for (const date of dates) {
    for (const m of readMatches(cacheDir, date)) {
      if (m.lota_id === lotaId) return m;
    }
  }
  return null;
}

/** 读 tags/<id>.json → {sections}（无则空）。 */
function readSections(cacheDir, lotaId) {
  const raw = readJson(join(cacheDir, KIND_DIR.tags, `${lotaId}.json`));
  return (raw && raw.sections) || {};
}

function truncate(text, max) {
  return text.length > max ? `${text.slice(0, max)}\n…[truncated]` : text;
}

/** 通用 JSON 渲染：把返回值 pretty 打印成模型可见文本。 */
function jsonRender(max = 4000) {
  return (_args, value) => [
    { type: "text", text: truncate(JSON.stringify(value, null, 2), max) },
  ];
}

/** 宽容的对象 schema（返回结构随字段动态变化；挂载后可按需收紧）。 */
const LOOSE_OBJECT = { type: "object", additionalProperties: true };

/**
 * 比赛信息定时刷新：每 30 分钟跑一次私有 lota_fetcher 的 refresh-date，刷新 matches
 * 缓存里的比分/状态（供斗狗场展示）。dsh 启动即生效；无私有 fetcher（开源环境）自动跳过。
 */
function startMatchRefresher(ctx, engineRoot) {
  const fetcher = join(engineRoot, "lota_fetcher.js");
  if (!existsSync(fetcher)) return;
  const bj = (ts) => new Date(ts + 8 * 3600 * 1000).toISOString().slice(0, 10);
  const footballDay = () => bj(Date.now() - (12 * 3600 + 60) * 1000);
  let running = false;
  const refresh = () => {
    if (running) return;
    running = true;
    const c = spawn(process.execPath, [fetcher, "refresh-date", footballDay()], { stdio: "ignore", cwd: engineRoot });
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
function startScheduledJobs(ctx, engineRoot, config) {
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
    // 1) harness 侧分析：headless agent 调 ds_analyze_all_parallel(parallel=7) 并行跑 7 狗 → ds_export_to_python 导出到 data/roles
    const task = "全部分析：调用 ds_analyze_all_parallel(parallel=7) 并行分析 7 只单关狗（每狗一个独立 subagent），完成后调用 ds_export_to_python(dry_run=false) 导出到 Python 数据层。";
    // 2) 发邮件：Python send_order_email 按 email_recipients.txt 名单（agent 过滤）
    const agentsPy = JSON.stringify(agents);
    const emailPy = `from src.order_email import send_order_email; [send_order_email(a, None) for a in ${agentsPy}]`;
    // headless 需要 DEEPSEEK_API_KEY；dsh web 进程环境可能没有（非交互式启动），从 ~/.zshrc 兜底读一次
    const script = `export DEEPSEEK_API_KEY="$(grep -o 'sk-[a-zA-Z0-9]*' ~/.zshrc | head -1)" && dsh --profile headless "${task}" && cd "${engineRoot}" && python3 -c "${emailPy}"`;
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

  // ── storage 域（纯 JS 状态层，见 harness_js_reconstruction.md §7）──
  const domainHandles = setupDomains(ctx);

  // ── 「斗狗场」仪表盘（/ds-dashboard JSON + /ds-avatars 图片，客户端 tab）──
  setupDashboard(ctx, domainHandles, cacheDir);

  // ── 比赛信息定时刷新（每 30 分钟，dsh 启动期间）──
  startMatchRefresher(ctx, engineRoot);

  // ── 定时邮件任务（每天固定时间点跑全狗分析 + 发邮件，dsh 启动期间）──
  startScheduledJobs(ctx, engineRoot, config);

  // ── 主循环 LLM 的 prompt section（analyze 框架 + 资金管理 + 结构化下单）──
  ctx.systemPrompt.section(ANALYZE_FRAMEWORK_SECTION);

  // ── ds_capital_js：纯 JS 查资金（读 ds_roles 域，无 Python 桥）──
  ctx.tools.register(defineTool({
    name: "ds_capital_js",
    description:
      "查询某只狗的资金现状（读 ds_roles 域）：余额/锁定敞口/全金额/未结算数/约束。产出 order 前先查，金额=信心比例×full_capital。只读。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        return await capitalQuery(domainHandles, args.user);
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── ds_memory_js：纯 JS 读因子记忆 + 历史反思（对齐 Python format_for_prompt）──
  ctx.tools.register(defineTool({
    name: "ds_memory_js",
    description:
      "读某只狗的历史记忆（读 ds_roles/ds_factors/ds_reflections/ds_slugs）：订单统计、连胜连败、最近订单、活跃因子/已证伪模式（factor_perf）、数据段表现（slug_stats）、历史反思、昨日结算回顾。分析下单前必读，判断信号时结合活跃因子与已证伪模式（例如「离散极低」可能是诱杀而非看好）。只读。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", description: "足球日 YYYY-MM-DD（因子衰减/休眠基准日 + 昨日结算回顾）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: (_args, value) => {
        const text = value && typeof value === "object" && !value.error
          ? value.text
          : (value && value.error) || JSON.stringify(value);
        return [{ type: "text", text: String(text || "") }];
      },
    },
    async execute(args) {
      try {
        const getMatchName = (lid) => {
          const m = findMatch(cacheDir, lid);
          return m ? `${m.home || "?"} vs ${m.away || "?"}` : lid;
        };
        return await memoryQuery(domainHandles, args.user, { day: args.day, getMatchName });
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── ds_migrate_storage：Python 数据 → storage 域（一次性迁移）──
  ctx.tools.register(defineTool({
    name: "ds_migrate_storage",
    description:
      "把 python-engine/data 下的 roles/factor_memory/reflection_memory/slug_memory 迁进 storage 域（ds_roles/ds_factors/ds_reflections/ds_slugs）。默认只迁 7 只真实狗（跳过临时快照），幂等（put 全量覆盖）。dry_run 只报告不写。",
    parameters: {
      dry_run: { type: "boolean", description: "只报告，不写 storage 域" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        return await migrateFromPython(domainHandles, cacheDir, { dryRun: !!args.dry_run });
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── ds_export_to_python：storage 域 → Python 文件（反向迁移，7 只真实狗）──
  ctx.tools.register(defineTool({
    name: "ds_export_to_python",
    description:
      "把 storage 域（ds_roles/ds_factors/ds_reflections/ds_slugs/ds_factor_registry）还原成 Python 文件（data/roles/<狗>/<狗>.json + memory/*.json + factors/fac_*.json）。默认只导出 7 只真实狗（跳过临时快照）。dry_run 只报告不写。",
    parameters: {
      dry_run: { type: "boolean", description: "只报告，不写 Python 文件" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        return await exportToPython(domainHandles, cacheDir, { dryRun: !!args.dry_run });
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── fetch_scores：取比分（仅 state==6 完场权威，进行中/未开绝不返回）──
  ctx.tools.register(defineTool({
    name: "fetch_scores",
    description: "从 matches 缓存取比分（仅 state==6 完场权威，进行中/未开的比分绝不返回）。只读。",
    parameters: {
      dates: { type: "array", items: { type: "string" }, required: true, description: "要扫描的日期列表 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      return { scores: fetchScoresFromCache(cacheDir, args.dates) };
    },
  }));

  // ── ds_settle_js：纯 JS 结算（settleOrder → 写 ds_roles 域，无 Python 桥）──
  ctx.tools.register(defineTool({
    name: "ds_settle_js",
    description:
      "纯 JS 结算某只狗的未结算订单：取比分(state==6)→settleOrder(亚盘/大小球/胜平负/赢半输半)→写回 ds_roles 域 + 更新 capital。无 LLM、无 Python 桥。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        return await settleDog(domainHandles, args.user, args.day, cacheDir);
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── refresh_orders：live 重跑前置，退回未开赛订单金额（对齐 Agent.refresh_orders）──
  ctx.tools.register(defineTool({
    name: "refresh_orders",
    description:
      "刷新当天订单组（live 重跑前置）：把足球日窗口内未开赛的未结算订单退回金额并删除，已开赛的保留。分析当天前先调它，对齐旧 LangGraph 的 analyze(live=True) 行为。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        return await refreshOrders(domainHandles, args.user, args.day, cacheDir);
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── submit_orders：纯 JS 下单（结构化订单，业务规则全部 JS 化，无 Python 桥）──
  ctx.tools.register(defineTool({
    name: "submit_orders",
    description:
      "把结构化订单落库（纯 JS）：跳过 skip → 已开赛保护 → 去重(lota_id,bet_type) → 资金折算(scale=余额/全金额)或硬约束 → 扣资金。订单是结构化数组，无需 order 文本。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
      orders: {
        type: "array",
        required: true,
        items: {
          type: "object",
          additionalProperties: false,
          properties: {
            lota_id: { type: "string", required: true, description: "比赛 ID" },
            bet_type: { type: "string", required: true, description: "亚盘|大小球|胜平负|让球胜平负" },
            pick: { type: "string", required: true, description: "H|A|D|over|under" },
            handicap: { type: "number", description: "亚盘盘口(主队视觉)" },
            odds: { type: "number", description: "赔率" },
            bet_size: { type: "number", description: "金额(=信心比例×全金额)" },
            reason: { type: "string", description: "理由" },
            skip: { type: "boolean", description: "不下注" },
          },
        },
      },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        return await submitOrders(domainHandles, args.user, args.day, args.orders, cacheDir);
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── ds_analyze_all_parallel：subagent fan-out 并行分析（计划 D3，每狗独立 session）──
  ctx.tools.register(defineTool({
      name: "ds_analyze_all_parallel",
      description:
        "并行分析全部（默认 7 只单关狗）：每狗启动一个独立 subagent 会话，并发执行 refresh_orders→读数据→读人设/记忆/资金→独立判断→submit_orders。父 agent 只做 fan-out 与汇总，不亲自逐狗分析。适合「全部分析 / 分析全部狗 / 跑全部狗」。",
      parameters: {
        day: { type: "string", description: "足球日 YYYY-MM-DD（空=按北京时间自动推当日）" },
        dogs: { type: "array", items: { type: "string" }, description: "要分析的狗名列表（空=默认 7 只真狗）" },
        parallel: { type: "number", description: "最大并发数（默认 7）" },
      },
      output: {
        schema: LOOSE_OBJECT,
        render: jsonRender(),
      },
      async execute(args, exec) {
        try {
          if (!exec || !exec.agent) return { error: "exec.agent 不存在，无法启动 subagent" };
          return await analyzeDogsParallel(ctx, {
            day: args.day,
            dogs: args.dogs,
            parallel: args.parallel,
            parent: exec.agent,
            signal: exec.signal,
            cacheDir,
          });
        } catch (error) {
          return { error: error.message };
        }
      },
    }));


  // ── ds_prepare_day：LLM 前确定性数据边界（竞彩过滤 + 缓存优先/URL 兜底）──
  ctx.tools.register(defineTool({
    name: "ds_prepare_day",
    description:
      "LLM 之前的数据准备（数据获取边界）：一次性获取某足球日的比赛并过滤竞彩（jingcai_number 非空，北单/无号排除），返回 strip_scores 后的候选列表。mode=live 强制刷新（拒绝旧赔率）；mode=replay 缓存优先、缺了才拉 URL。分析/回放前必须先调它拿比赛列表，禁止用 lota_matches 拉全量。",
    parameters: {
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD（窗口 [D 12:01, D+1 12:00]）" },
      mode: { type: "string", description: "live=强制刷新（默认）；replay=历史缓存优先、缺了拉 URL" },
      jingcai_only: { type: "boolean", description: "默认 true：只保留竞彩场次" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(8000),
    },
    async execute(args) {
      try {
        const res = await prepareDay({
          cacheDir, engineRoot,
          day: args.day,
          mode: args.mode || "live",
          jingcaiOnly: args.jingcai_only !== false,
          pythonBin,
        });
        return res;
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── ds_persona_js：确定性人设注入（不再让模型 read 文件）──
  ctx.tools.register(defineTool({
    name: "ds_persona_js",
    description:
      "读某只狗的人设（roles/<狗名>/persona.md）并注入上下文：投注风格、仓位档位、行为准则。分析下单前必读。只读。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: (_args, value) => [
        { type: "text", text: value && typeof value === "object" && !value.error ? value.text : ((value && value.error) || JSON.stringify(value)) },
      ],
    },
    async execute(args) {
      try {
        const text = readPersona(cacheDir, args.user);
        return { user: args.user, text: text || "(无 persona.md，按通用框架执行)" };
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── ds_replay：回放模式（runall 日维度流程迁移）──
  ctx.tools.register(defineTool({
    name: "ds_replay",
    description:
      "回放模式：把「获取比赛→分析→结算→因子归纳→周期性因子退役」按日维度跑 [start, end]。范围数据一次性准备（历史缓存优先、缺了拉 URL），逐日并行分析（fan-out subagent）+ 结算 + 反思 + 因子归纳，每 factor_review_every 天做因子退役评估（可注入 user_notes 用户调整意见 / persona_overrides 人设覆盖）。记录每狗每日轨迹到 cacheDir/replays/<run_id>/report.md。reset=zero 从初始资金+空记忆开始；默认 restore_after 还原起点状态。旁路 LLM 默认 deepseek-v4-flash 省 token。",
    parameters: {
      start: { type: "string", required: true, description: "起始足球日 YYYY-MM-DD" },
      end: { type: "string", required: true, description: "结束足球日 YYYY-MM-DD（含）" },
      dogs: { type: "array", items: { type: "string" }, description: "回放的狗名列表（空=默认 7 只真狗）" },
      parallel: { type: "number", description: "分析并发数（默认=狗数）" },
      model: { type: "string", description: "旁路 LLM（反思/退役）模型，默认 deepseek-v4-flash" },
      user_notes: { type: "string", description: "用户调整意见：注入周期性因子退役评估（可调人设/因子思考方向）" },
      persona_overrides: { type: "object", additionalProperties: true, description: "狗名→人设文本覆盖（分析/反思/退役用），值是字符串" },
      factor_review_every: { type: "number", description: "每隔多少天做一次因子退役（默认 7）" },
      reset: { type: "string", description: "none=用当前状态（默认）；zero=从初始资金+空记忆开始" },
      restore_after: { type: "boolean", description: "跑完后是否还原起点状态（默认 true）" },
      run_id: { type: "string", description: "自定义运行标识（默认 replay_<start>_<end>_<ts>）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(10000),
    },
    async execute(args, exec) {
      try {
        return await runReplay(ctx, domainHandles, cacheDir, engineRoot, {
          start: args.start,
          end: args.end,
          dogs: args.dogs,
          parallel: args.parallel,
          model: args.model,
          user_notes: args.user_notes,
          persona_overrides: args.persona_overrides,
          factor_review_every: args.factor_review_every,
          reset: args.reset,
          restore_after: args.restore_after,
          run_id: args.run_id,
          parent: exec && exec.agent,
          signal: exec && exec.signal,
          pythonBin,
        });
      } catch (error) {
        return { ok: false, error: error.message };
      }
    },
  }));


  // ── ds_reflect_js：纯 JS 结算后反思（旁路 LLM + 写回 storage，无 Python 桥）──
  ctx.tools.register(defineTool({
    name: "ds_reflect_js",
    description:
      "纯 JS 结算后反思：读结算单+人设+已有因子 → 旁路 ctx.llm.stream 因子发现(JSON) → 写回 ds_factors/ds_reflections/ds_factor_registry。无 Python 桥。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
      settled: { type: "array", required: true, description: "ds_settle_js 返回的 orders 列表" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        const settled = args.settled || [];
        if (!settled.length) return { ok: true, skipped: "无结算单，跳过反思" };
        const persona = readPersona(cacheDir, args.user);
        const existingSummary = await getExistingFactorSummary(domainHandles, args.user);
        const prompt = buildReflectPrompt({
          persona, settled, existingSummary, factorDescText: "", keySlugWhitelist: SLUG_WHITELIST,
        });
        const text = await streamReflectJson(ctx, prompt, REFLECT_DEFAULT);
        const data = parseReflectJson(text);
        if (!data) return { ok: false, error: "reflect JSON 解析失败", raw: text.slice(0, 500) };
        return await applyReflection(domainHandles, args.user, args.day, data, settled);
      } catch (error) {
        return { ok: false, error: error.message };
      }
    },
  }));

  // ── ds_factor_review_js：纯 JS 因子退役评估（门控 + 旁路 LLM，无 Python 桥）──
  ctx.tools.register(defineTool({
    name: "ds_factor_review_js",
    description:
      "纯 JS 因子退役评估：14天零触发休眠 + 低信息退役（确定性门控）+ 旁路 ctx.llm.stream 结构性评估(retire/dormant/active)。写回 ds_factors。无 Python 桥。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      end_date: { type: "string", required: true, description: "评估窗口结束日 YYYY-MM-DD" },
      start_date: { type: "string", description: "评估窗口起始日（空=近7天）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        return await factorReview(domainHandles, ctx, args.user, args.end_date, args.start_date || "", cacheDir);
      } catch (error) {
        return { ok: false, error: error.message };
      }
    },
  }));

  // ── ds_factor_dedup：纯 JS 因子判重（旁路 LLM fast 模型 + 确定性兜底）──
  ctx.tools.register(defineTool({
    name: "ds_factor_dedup",
    description:
      "纯 JS 因子判重：判断候选因子与某狗已有因子是否重复（create/merge/suppress）。旁路 ctx.llm.stream(fast 模型)+ 确定性兜底（retired 近亲 suppress）。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      factor_id: { type: "string", required: true, description: "候选因子名" },
      desc: { type: "string", description: "候选因子描述" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      try {
        const domain = await domainHandles["ds_factors"];
        const rec = domain.table("factors").get(args.user);
        const fp = (rec && rec.factor_perf) || {};
        return await judgeFactorDedup(ctx, args.factor_id, args.desc || "", fp);
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── ds_factor_induction：因子归纳去重（对齐 factor_induction.py，每日 settle 后跑）──
  ctx.tools.register(defineTool({
    name: "ds_factor_induction",
    description:
      "因子归纳去重（对齐 python factor_induction.py）：同清洗名确定性合并 + slugs bit距离/孤儿名字相似 LLM 判重合并，合并后重算统计、累计 aliases，写回 ds_factors。每日 settle 后跑。user='alpha' 或 alpha 狗名（alpha2狗/alpha狗/均注狗）→ alpha 跨狗统一归纳（1 次进全库）；其他狗名 → 单狗各自归纳。dry_run 只报告候选不写回。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色）；'alpha' 触发跨狗归纳" },
      dry_run: { type: "boolean", description: "只报告候选，不调 LLM、不写回" },
      limit: { type: "number", description: "最多 LLM 判重次数（默认 30）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(8000),
    },
    async execute(args) {
      try {
        if (args.user === "alpha" || ALPHA_DOGS.includes(args.user)) {
          const res = await inductAlpha(ctx, domainHandles, {
            dryRun: !!args.dry_run,
            limit: args.limit ?? 30,
          });
          return { user: args.user, dry_run: !!args.dry_run, ...res };
        }
        const domain = await domainHandles["ds_factors"];
        const rec = domain.table("factors").get(args.user);
        const fp = { ...((rec && rec.factor_perf) || {}) };
        const { result, factorPerf } = await inductFactors(ctx, fp, {
          dryRun: !!args.dry_run,
          limit: args.limit ?? 30,
          scope: args.user,
        });
        if (!args.dry_run) {
          await domain.table("factors").put(args.user, {
            ...(rec || { factor_perf: {} }),
            factor_perf: factorPerf,
            updated_at: beijingNowIso(),
          });
        }
        return {
          user: args.user,
          dry_run: !!args.dry_run,
          factor_count_before: Object.keys((rec && rec.factor_perf) || {}).length,
          factor_count_after: Object.keys(factorPerf).length,
          ...result,
        };
      } catch (error) {
        return { error: error.message };
      }
    },
  }));

  // ── lota_matches：某足球日比赛列表 ──
  ctx.tools.register(defineTool({
    name: "lota_matches",
    description: "读取本地缓存中某足球日的比赛列表（matches/<date>.json）。只读，不触网。strip_scores=true 时剥离比分（分析用，防后视）。",
    parameters: {
      date: { type: "string", required: true, description: "足球日 YYYY-MM-DD（窗口 [D 12:01, D+1 12:00]）" },
      lottery_type: { type: "string", description: "可选过滤，如 jingcai / beidan / all" },
      strip_scores: { type: "boolean", description: "true=剥离比分（分析用，防后视）；比分只在 settle 工具里出现" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      let matches = readMatches(cacheDir, args.date);
      if (args.lottery_type && args.lottery_type !== "all") {
        // 缓存里没有 lottery_type 字段，按 jingcai_number / beidan_number 过滤
        // （与 fanout.js readMatchesCache、Python 侧 --jingcai 过滤对齐）
        matches = matches.filter((m) =>
          args.lottery_type === "jingcai" ? Boolean(m && m.jingcai_number)
          : args.lottery_type === "beidan" ? Boolean(m && m.beidan_number)
          : false);
      }
      if (args.strip_scores) {
        matches = matches.map((m) => {
          const { score, result, ...rest } = m;
          return rest;
        });
      }
      return { date: args.date, count: matches.length, matches };
    },
  }));

  // ── lota_match：单场全貌 ──
  ctx.tools.register(defineTool({
    name: "lota_match",
    description: "读取单场比赛全貌：基础信息 + 比分 + 段落(sections) + 预测/订单计数。只读本地缓存。strip_scores=true 时剥离比分（分析用，防后视）。",
    parameters: {
      lota_id: { type: "string", required: true, description: "比赛 ID，如 Lota4579740" },
      strip_scores: { type: "boolean", description: "true=剥离比分（分析用，防后视）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    async execute(args) {
      const feat = normalizeFeature(readJson(join(cacheDir, KIND_DIR.features, `${args.lota_id}.json`)));
      const match = feat?.match ?? findMatch(cacheDir, args.lota_id);
      const sections = readSections(cacheDir, args.lota_id);
      const predictions = readJson(join(cacheDir, KIND_DIR.predicts, `${args.lota_id}.json`)) ?? [];
      const orders = readJson(join(cacheDir, KIND_DIR.orders, `${args.lota_id}.json`)) ?? [];
      // Pinnacle 终盘赔率（数据专有解析，见 odds.js）
      const odds = extractOdds(feat?.compact_fet ?? "");
      const strip = !!args.strip_scores;
      const matchOut = strip && match ? Object.fromEntries(
        Object.entries(match).filter(([k]) => k !== "score" && k !== "result"),
      ) : match;
      return {
        lota_id: args.lota_id,
        match: matchOut,
        score: strip ? "" : (feat?.score ?? match?.score ?? ""),
        odds,
        sections: Object.fromEntries(
          Object.entries(sections).map(([slug, text]) => [slug, truncate(text, 500)])
        ),
        predictions: Array.isArray(predictions) ? predictions.length : 0,
        orders: Array.isArray(orders) ? orders.length : 0,
        cached_at: feat?._cached_at ?? null,
        api_failed: feat?._api_failed === true,
      };
    },
  }));

  // ── lota_sections：按 slug 取 prompt 段落 ──
  ctx.tools.register(defineTool({
    name: "lota_sections",
    description: "读取本地缓存的段落（tags/<id>.json），按 slug 列表取 prompt 片段。只读。",
    parameters: {
      lota_id: { type: "string", required: true, description: "比赛 ID" },
      slugs: {
        type: "array",
        items: { type: "string" },
        required: true,
        description: "需要的段落 slug，如 fair-odds / asian-handicap-crown",
      },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(6000),
    },
    async execute(args) {
      const sections = readSections(cacheDir, args.lota_id);
      const picked = {};
      const parts = [];
      for (const slug of args.slugs) {
        if (sections[slug]) {
          picked[slug] = sections[slug];
          parts.push(`[section:${slug}]\n${sections[slug]}`);
        }
      }
      return { lota_id: args.lota_id, sections: picked, text: parts.join("\n\n") };
    },
  }));


}

export { apply, inject, name };
