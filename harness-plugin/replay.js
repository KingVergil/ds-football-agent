/**
 * 回放模式（replay）：把 Python runall 的日维度流程迁到 harness。
 *
 * 每日管线（对齐用户要求的"获取比赛 → 分析 → 结算 → 因子归纳 → 周期性因子退役"）：
 *   0. 范围数据一次性准备（prepareRange，历史缓存优先、缺了才拉 URL）——逐日循环只读缓存
 *   1. 并行分析（fan-out 每狗独立 subagent，比赛列表已注入、人设已注入上下文）
 *   2. 结算（settleDog，纯 JS）
 *   3. 反思（ds_reflect 等价，旁路 LLM，模型可覆盖，默认 deepseek-v4-flash）
 *   4. 因子归纳（alpha 跨狗 1 次 + 非 alpha 各自，flash 判重）
 *   5. 每 factor_review_every 天：因子退役评估（可注入用户调整意见 user_notes / 人设覆盖）
 *
 * 轨迹对比：
 *   - 每狗每日快照（资金/待定/下单/结算 PnL/活跃因子数）→ replayDir/report.json + report.md
 *   - 起点快照（storage 域全量）→ replayDir/snapshot/，restore_after=true 时才在结束后还原
 *   - 默认写穿：回放期间的订单/因子直接留在 storage 域（restore_after 默认 false）
 *   - 范围预检：目标狗在 [start,end] 内已有订单时直接拒绝（diff/diff-report 未设计，不支持合并）
 *   - reset="zero" 时从初始资金/空记忆开始（"从 0 开始"）
 */
import { mkdirSync, writeFileSync, readFileSync, existsSync, copyFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { beijingNowIso } from "./settle.js";
import { DS_REAL_DOGS } from "./storage.js";
import { prepareRange, addDays, jingcaiWindowMatches, hasValidFeature, hasTags } from "./dataflow.js";
import { LLM_TEMPERATURES } from "./tools/shared.js";
import { analyzeDogDirect, footballDayLabel } from "./fanout.js";
import { settleDog } from "./settleEngine.js";
import {
  buildReflectPrompt, streamReflectJson, streamText, parseReflectJson, applyReflection,
  readPersona, getExistingFactorSummary, SLUG_WHITELIST, REFLECT_DEFAULT,
} from "./reflect.js";
import { factorReview } from "./factorReview.js";
import { inductAlpha, inductFactors, ALPHA_DOGS } from "./factorInduction.js";

/** 回放旁路 LLM 默认模型（用户指定：deepseek-flash 省 token）。 */
export const REPLAY_MODEL = "deepseek-v4-flash";

/** 要快照的 storage 域：{ 域名: 表名 }。 */
const SNAPSHOT_TABLES = {
  ds_roles: "roles",
  ds_factors: "factors",
  ds_reflections: "reflections",
  ds_slugs: "slugs",
  ds_factor_registry: "factors",
};

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function writeJson(path, value) {
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2), "utf8");
}

/**
 * 回放启动前的 subagent 能力检查。ds_replay 的逐日分析必须 fan-out 到子 agent；
 * 这些条件不满足时在快照/reset/取数/任何写操作之前直接失败，避免「跑到一半才提示 subagent 不能启动」。
 */
export function validateReplaySpawn(ctx, parent) {
  if (!ctx || !ctx.subagents) {
    return { error: "ctx.subagents 不可用：host 未挂载 @deepseek-ai/dsh-subagent（或插件未注入 subagents），无法启动子分析代理" };
  }
  if (!parent) {
    return { error: "缺少 parent agent，无法启动 subagent：回放必须在会话 agent 下触发（斗狗场按钮会写入会话输入框执行）" };
  }
  if (ctx.subagents.getProvider("spawn") === undefined) {
    return { error: "subagent provider 'spawn' 未注册：host 需挂载 @deepseek-ai/dsh-subagent-spawn-in-process" };
  }
  return null;
}

/** 把订单 created_at（北京时间 ISO，可能不带时区）解析成 UTC epoch；解析失败返回 null。 */
function orderCreatedEpoch(createdAt) {
  if (!createdAt) return null;
  let s = String(createdAt).trim();
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?$/.exec(s);
  if (m) s = `${m[1]}T${m[2]}+08:00`; // 落库时写的是北京时间墙钟，不带时区
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
}

/** 订单属于哪个足球日（按 created_at 北京时间，12:00 前归前一天）。 */
export function orderFootballDay(order) {
  const t = orderCreatedEpoch(order && order.created_at);
  return t == null ? "" : footballDayLabel(new Date(t));
}

/**
 * 预检：回放范围 [start,end] 内目标狗是否已有订单。
 * 现有回放是「整段写穿」，没有 diff/diff-report 能力；范围里已有订单时直接返回冲突列表，
 * 由调用方提示「暂不支持」，不做任何快照/重置/取数。
 */
export async function findRangeOrderConflicts(handles, dogs, start, end) {
  const days = new Set(dayListOf(start, end));
  const rolesDomain = await handles["ds_roles"];
  const rolesTable = rolesDomain.table("roles");
  const conflicts = [];
  for (const dog of dogs) {
    const role = rolesTable.get(dog);
    if (!role) continue;
    for (const o of role.orders || []) {
      const day = orderFootballDay(o);
      if (day && days.has(day)) {
        conflicts.push({
          dog,
          day,
          lota_id: o.lota_id || "",
          bet_type: o.bet_type || "",
          created_at: o.created_at || "",
          settled: Boolean(o.settled_at),
        });
      }
    }
  }
  return { conflicts, days: [...days].sort() };
}

function round2(x) {
  return Math.round(x * 100) / 100;
}

/** 把范围涉及的 matches 文件快照进回放目录（隔离运行中的 web 刷新器并发改写）。 */
export function snapshotMatchesRange(cacheDir, replayDir, dates) {
  const dir = join(replayDir, "cache", "matches");
  mkdirSync(dir, { recursive: true });
  const copied = [];
  for (const d of dates) {
    const src = join(cacheDir, "matches", `${d}.json`);
    if (!existsSync(src)) continue;
    copyFileSync(src, join(dir, `${d}.json`));
    copied.push(d);
  }
  return copied;
}

/** 逐日只读回放快照（matches 从快照读；features/tags 完整性用真实缓存检查）。 */
export function readReplayDay(cacheDir, replayDir, day) {
  const windowTotal = jingcaiWindowMatches(join(replayDir, "cache"), day, { jingcaiOnly: false }).length;
  const matches = jingcaiWindowMatches(join(replayDir, "cache"), day, { strip: true, jingcaiOnly: true });
  const jingcaiCount = matches.filter((m) => m && m.jingcai_number).length;
  const missingFeat = matches.filter((m) => !hasValidFeature(cacheDir, m.lota_id)).length;
  const missingTags = matches.filter((m) => !hasTags(cacheDir, m.lota_id)).length;
  const warnings = [];
  if (matches.length === 0) warnings.push("窗口内无竞彩比赛（可能缓存缺失或当天无竞彩场次）");
  if (missingFeat > 0) warnings.push(`${missingFeat} 场缺 features（LLM 将看不到赔率段）`);
  if (missingTags > 0) warnings.push(`${missingTags} 场缺 tags 段落`);
  return {
    day,
    window_total: windowTotal,
    jingcai_count: jingcaiCount,
    excluded_count: windowTotal - jingcaiCount,
    matches,
    warnings,
  };
}

/** 快照全部 storage 域到 destDir（文件直接放 destDir 下）。 */
export async function writeDomainSnapshot(handles, destDir) {
  const out = {};
  for (const [domainName, tableName] of Object.entries(SNAPSHOT_TABLES)) {
    const domain = await handles[domainName];
    const table = domain.table(tableName);
    const rec = {};
    for (const [key, value] of table.entries()) rec[key] = value;
    out[domainName] = rec;
    writeJson(join(destDir, `${domainName}__${tableName}.json`), rec);
  }
  return out;
}

/** 从 srcDir 还原 storage 域（文件直接放 srcDir 下）——真替换：先删快照里没有的 key，再全量 put。 */
export async function restoreDomainSnapshot(handles, srcDir) {
  const restored = {};
  for (const [domainName, tableName] of Object.entries(SNAPSHOT_TABLES)) {
    const rec = readJson(join(srcDir, `${domainName}__${tableName}.json`));
    if (!rec) continue;
    const domain = await handles[domainName];
    const table = domain.table(tableName);
    // 回放期间新写入、但快照里不存在的 key（如新增因子注册表条目）要删除，保证「还原=替换」
    for (const key of table.keys()) {
      if (!(key in rec)) await table.delete(key);
    }
    for (const [key, value] of Object.entries(rec)) await table.put(key, value);
    restored[domainName] = Object.keys(rec).length;
  }
  return restored;
}

/** 快照全部 storage 域到 replayDir/snapshot/（兼容旧 API）。 */
export async function snapshotDomains(handles, dir) {
  return writeDomainSnapshot(handles, join(dir, "snapshot"));
}

/** 从 replayDir/snapshot/ 还原 storage 域（兼容旧 API）。 */
export async function restoreDomains(handles, dir) {
  return restoreDomainSnapshot(handles, join(dir, "snapshot"));
}

/** 列出回放目录下的全部检查点（start + 各阶段）。 */
export function listCheckpoints(replayDir) {
  const cpDir = join(replayDir, "checkpoints");
  if (!existsSync(cpDir)) return ["start"];
  const names = [];
  for (const f of readdirSync(cpDir)) {
    if (f.endsWith(".json")) continue;
    if (existsSync(join(cpDir, f))) names.push(f);
  }
  return ["start", ...names.sort()];
}

/** reset="zero"：把指定狗重置为初始资金 + 空订单/因子/反思（从 0 开始）。 */
export async function resetRolesToZero(handles, dogs) {
  const rolesDomain = await handles["ds_roles"];
  const rolesTable = rolesDomain.table("roles");
  const [factorsDomain, reflectionsDomain, slugsDomain] = await Promise.all([
    handles["ds_factors"], handles["ds_reflections"], handles["ds_slugs"],
  ]);
  const factorsTable = factorsDomain.table("factors");
  const reflectionsTable = reflectionsDomain.table("reflections");
  const slugsTable = slugsDomain.table("slugs");
  const reset = [];
  for (const dog of dogs) {
    const role = rolesTable.get(dog) || {};
    const initial = Number(role.initial_capital || role.capital || 10000);
    await rolesTable.put(dog, {
      ...role,
      name: role.name || dog,
      initial_capital: role.initial_capital ?? initial,
      capital: initial,
      orders: [],
      updated_at: beijingNowIso(),
    });
    await factorsTable.put(dog, { factor_perf: {}, updated_at: beijingNowIso() });
    await reflectionsTable.put(dog, { reflections: [], updated_at: beijingNowIso() });
    await slugsTable.put(dog, { slug_stats: {}, day_slugs: {}, updated_at: beijingNowIso() });
    reset.push(dog);
  }
  return reset;
}

/** 读某狗回放人设：persona_overrides[dog] 优先，否则从 personaDir(默认 cacheDir/roles) 读 persona.md。 */
function personaFor(cacheDir, dog, personaOverrides, personaDir) {
  const over = personaOverrides && personaOverrides[dog];
  if (over) return over;
  return readPersona(cacheDir, dog, personaDir);
}

/** 反思（等价 ds_reflect_js，模型/人设可覆盖）。 */
export async function reflectDog(handles, ctx, dog, day, settled, { model = REPLAY_MODEL, persona } = {}) {
  if (!settled || !settled.length) return { ok: true, skipped: "无结算单，跳过反思" };
  const existingSummary = await getExistingFactorSummary(handles, dog);
  const prompt = buildReflectPrompt({
    persona: persona || "",
    settled, existingSummary, factorDescText: "", keySlugWhitelist: SLUG_WHITELIST,
  });
  const text = await streamReflectJson(ctx, prompt, { model, temperature: LLM_TEMPERATURES.reflect });
  const data = parseReflectJson(text);
  if (!data) return { ok: false, error: "reflect JSON 解析失败", raw: text.slice(0, 500) };
  return applyReflection(handles, dog, day, data, settled);
}

/** 起止足球日 → 逐日列表。 */
function dayListOf(start, end) {
  const days = [];
  let day = start;
  while (day <= end) { days.push(day); day = addDays(day, 1); }
  return days;
}

/** 单段回放天数上限（防止误填超长范围把进程/资金链拖垮）。 */
export const REPLAY_MAX_DAYS = 60;

/** YYYY-MM-DD 且是真实日历日期。 */
export function isValidFootballDay(str) {
  if (typeof str !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(str)) return false;
  const [y, m, d] = str.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/**
 * 回放范围正确性校验（先于快照/取数/任何变更执行，失败即返回 error 不落任何状态）：
 *   1. start/end 必填且格式 YYYY-MM-DD；
 *   2. start ≤ end；
 *   3. 天数 ≤ REPLAY_MAX_DAYS。
 */
export function validateReplayRange(start, end) {
  if (!start || !end) return { ok: false, error: `回放范围无效: start/end 必填（${start} ~ ${end}）` };
  if (!isValidFootballDay(start) || !isValidFootballDay(end)) {
    return { ok: false, error: `回放范围无效: 日期必须是 YYYY-MM-DD（${start} ~ ${end}）` };
  }
  if (start > end) return { ok: false, error: `回放范围无效: start 不能晚于 end（${start} ~ ${end}）` };
  const days = dayListOf(start, end).length;
  if (days > REPLAY_MAX_DAYS) {
    return { ok: false, error: `回放范围过大: ${days} 天，单段上限 ${REPLAY_MAX_DAYS} 天` };
  }
  return { ok: true, days };
}

/** 交互式回放会话持久化（session.json 落在 replayDir 下，供跨工具调用续跑/回退）。 */
function sessionPath(replayDir) { return join(replayDir, "session.json"); }
function loadSession(replayDir) { return readJson(sessionPath(replayDir)); }
function saveSession(replayDir, s) {
  // parent/signal 不落盘（不可序列化）
  const { parent, signal, onProgress, ...rest } = s;
  writeJson(sessionPath(replayDir), { ...rest, updated_at: beijingNowIso() });
}

/**
 * 跑回放的一天（2.1 数据 → 2.2 分析 → 结算前检查点 → 2.3 结算 → 因子前检查点
 * → 2.4 因子流(反思/归纳/alpha barrier) → 2.5 周期退役 → 当日终态快照）。
 * 结果 push 进 acc 的 trajectory/reviewLog/checkpointLog/log；返回 { reviewDone }。
 */
async function runReplayDay(ctx, handles, cacheDir, replayDir, d, dayIdx, cfg, acc) {
  const { dogs, parallel, model, personaOverrides, personaDir, factorReviewEvery, userNotes, start, parent, signal, onProgress, days } = cfg;
  const { trajectory, reviewLog, checkpointLog, log } = acc;
  const warn = (msg) => log.push(`⚠️ ${msg}`);
  const total = days.length;

  const startedAt = beijingNowIso();
  log.push(`\n📅 [${d}] 第 ${dayIdx + 1}/${total} 天（${startedAt}）`);

  // 2.1 数据边界：只读回放快照（matches），features/tags 完整性查真实缓存
  const data = readReplayDay(cacheDir, replayDir, d);
  log.push(`   竞彩 ${data.jingcai_count}/${data.window_total} 场（排除北单/无号 ${data.excluded_count}）`);
  for (const w of data.warnings) warn(`[${d}] ${w}`);

  // 2.2 逐狗确定性分析（每狗一次 LLM 决策；数据/人设/记忆/资金/段落全部固有获取，无工具轮次）
  onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 分析`, done: dayIdx, total, detail: d });
  const analysisRows = new Array(dogs.length);
  let cursor = 0;
  const analysisWorker = async () => {
    while (cursor < dogs.length) {
      if (signal && signal.aborted) return;
      const idx = cursor++;
      const dog = dogs[idx];
      onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 分析 ${dog}`, done: dayIdx, total, detail: d });
      try {
        analysisRows[idx] = await analyzeDogDirect(ctx, handles, {
          dog,
          day: d,
          cacheDir,
          persona: (personaOverrides || {})[dog] || "",
          personaDir,
          onProgress: (p) => onProgress({
            phase: `第 ${dayIdx + 1}/${total} 天 ${p.phase || "分析"}`,
            done: dayIdx, total,
            detail: p.detail || d,
          }),
        });
      } catch (e) {
        analysisRows[idx] = { ok: false, error: String((e && e.message) || e), dog };
      }
    }
  };
  const analysisWorkers = Math.max(1, Math.min(Number(parallel) || dogs.length, dogs.length || 1));
  await Promise.all(Array.from({ length: analysisWorkers }, () => analysisWorker()));
  const analysis = {
    rows: analysisRows.map((r) => r || { ok: false, dog: "?" }),
    ok_count: analysisRows.filter((r) => r && r.ok).length,
    fail_count: analysisRows.filter((r) => r && !r.ok).length,
    matches_count: analysisRows.reduce((s, r) => s + (r && r.matches_count || 0), 0),
  };
  log.push(`   分析: ok=${analysis.ok_count} fail=${analysis.fail_count} matches=${analysis.matches_count}`);
  const placedByDog = {};
  for (const row of analysis.rows || []) {
    placedByDog[row.dog] = row.placed || 0;
    log.push(`     ${row.ok ? "✅" : "❌"} ${row.dog} ${row.skipped ? `skip: ${row.skipped}` : (row.text || row.error || "")}`);
  }

  // 检查点：结算前（分析+下单后的状态）
  const preSettle = `${d}__pre-settle`;
  await writeDomainSnapshot(handles, join(replayDir, "checkpoints", preSettle));
  checkpointLog.push({ name: preSettle, day: d, phase: "结算前" });
  log.push(`   📍 检查点: ${preSettle}`);

  // 2.3 结算（纯结算；反思移入因子流阶段 0）
  const dayTraj = { day: d, dogs: {} };
  const settledByDog = {};
  for (const dog of dogs) {
    try {
      onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 结算 ${dog}`, done: dayIdx, total });
      settledByDog[dog] = await settleDog(handles, dog, d, cacheDir);
    } catch (e) {
      settledByDog[dog] = { error: String((e && e.message) || e), settled: 0, pnl: 0 };
    }
    const settledRes = settledByDog[dog];
    const rolesDomain = await handles["ds_roles"];
    const role = rolesDomain.table("roles").get(dog) || {};
    const orders = role.orders || [];
    const factorsDomain = await handles["ds_factors"];
    const fp = ((factorsDomain.table("factors").get(dog) || {}).factor_perf) || {};
    dayTraj.dogs[dog] = {
      capital: round2(Number(role.capital || 0)),
      pending: orders.filter((o) => !o.settled_at).length,
      settled: (settledRes && settledRes.settled) || 0,
      pnl: (settledRes && round2(settledRes.pnl)) || 0,
      placed: placedByDog[dog] ?? 0,
      active_factors: Object.values(fp).filter((s) => s.status !== "retired").length,
    };
    log.push(`   💰 ${dog}: 结算${dayTraj.dogs[dog].settled}单 PnL${dayTraj.dogs[dog].pnl} → 余额${dayTraj.dogs[dog].capital}`);
  }

  // 检查点：因子流前（结算后的状态）
  const preFactor = `${d}__pre-factor`;
  await writeDomainSnapshot(handles, join(replayDir, "checkpoints", preFactor));
  checkpointLog.push({ name: preFactor, day: d, phase: "因子流前" });
  log.push(`   📍 检查点: ${preFactor}`);

  // 2.4 因子流（docs/workflow_tool_groups.md §2.3：阶段0 反思 → A 非alpha → B alpha barrier → C 退役）
  try {
    // 阶段 0：反思（每狗，输入=当日已结算订单，产出新因子落库）
    for (const dog of dogs) {
      const settled = settledByDog[dog];
      if (settled && settled.settled > 0 && !settled.error) {
        onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子流·反思 ${dog}`, done: dayIdx, total });
        const reflectRes = await reflectDog(handles, ctx, dog, d, settled.orders, {
          model,
          persona: personaFor(cacheDir, dog, personaOverrides, personaDir),
        });
        if (reflectRes && reflectRes.ok === false) warn(`[${d}] ${dog} 反思失败: ${reflectRes.error}`);
      }
    }

    // 阶段 A：非 alpha 各自归纳（可并行，这里顺序执行；alpha 狗跳过留到阶段 B）
    for (const dog of dogs) {
      if (ALPHA_DOGS.includes(dog)) continue;
      onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子流·阶段A ${dog}`, done: dayIdx, total });
      const factorsDomain = await handles["ds_factors"];
      const rec = factorsDomain.table("factors").get(dog) || { factor_perf: {} };
      const { result, factorPerf } = await inductFactors(ctx, rec.factor_perf || {}, { limit: 30, scope: dog });
      await factorsDomain.table("factors").put(dog, {
        ...rec,
        factor_perf: factorPerf,
        updated_at: beijingNowIso(),
      });
      if (result && result.merged && result.merged.length) {
        log.push(`   🧬 因子归纳 ${dog}: 合并 ${result.merged.length} 个`);
      }
    }

    // 阶段 B（barrier）：非 alpha 完成后，alpha 跨狗统一归纳一次进全库
    if (dogs.some((x) => ALPHA_DOGS.includes(x))) {
      onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子流·阶段B alpha barrier`, done: dayIdx, total });
      await inductAlpha(ctx, handles, { limit: 30 });
    }
  } catch (e) {
    warn(`[${d}] 因子归纳失败: ${(e && e.message) || e}`);
  }

  // 2.5 周期性因子退役（阶段 C：非 alpha 先行 → alpha 收尾；带用户调整意见/下一轮方向）
  const reviewDone = ((dayIdx + 1) % factorReviewEvery === 0);
  if (reviewDone) {
    onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子流·阶段C 退役`, done: dayIdx, total });
    const ordered = [...dogs.filter((x) => !ALPHA_DOGS.includes(x)), ...dogs.filter((x) => ALPHA_DOGS.includes(x))];
    for (const dog of ordered) {
      try {
        const review = await factorReview(handles, ctx, dog, d, start, cacheDir, {
          userNotes,
          persona: personaFor(cacheDir, dog, personaOverrides, personaDir),
        });
        reviewLog.push({ day: d, dog, ...review });
        const bits = [];
        if (review.auto_dormant && review.auto_dormant.length) bits.push(`休眠${review.auto_dormant.length}`);
        if (review.low_info_retired && review.low_info_retired.length) bits.push(`低信息退役${review.low_info_retired.length}`);
        if (review.retired && review.retired.length) bits.push(`结构性退役: ${review.retired.join("、")}`);
        log.push(`   🔬 因子退役 ${dog}: ${bits.length ? bits.join(" | ") : "无调整"}${userNotes ? "（已应用用户意见）" : ""}`);
      } catch (e) {
        warn(`[${d}] ${dog} 因子退役失败: ${(e && e.message) || e}`);
      }
    }
  }

  trajectory.push(dayTraj);

  // 当日终态快照：供交互式 rewind（回到"某天开始"= 前一天终态）
  const postDay = `${d}__post-day`;
  await writeDomainSnapshot(handles, join(replayDir, "checkpoints", postDay));
  checkpointLog.push({ name: postDay, day: d, phase: "当日终态" });

  return { reviewDone };
}

/**
 * 生成「下一轮因子归纳/退役方向建议」：先启发式（基于本周期退役/休眠/PnL），
 * 可选旁路 LLM 润色（失败回退启发式）。产物交给用户编辑后作为 induction_notes 回传。
 */
async function buildDirectionSuggestion(ctx, { dogs, cycleReviews, cycleTraj, model }) {
  const lines = [];
  for (const dog of dogs) {
    const revs = cycleReviews.filter((r) => r.dog === dog);
    const retired = revs.flatMap((r) => r.retired || []);
    const dormant = revs.flatMap((r) => r.auto_dormant || []);
    const lowInfo = revs.flatMap((r) => r.low_info_retired || []);
    const pnl = cycleTraj.reduce((s, t) => s + Number(((t.dogs || {})[dog] || {}).pnl || 0), 0);
    const bits = [];
    if (retired.length) bits.push(`退役 ${retired.join("、")}`);
    if (dormant.length) bits.push(`休眠 ${dormant.length} 个`);
    if (lowInfo.length) bits.push(`低信息退役 ${lowInfo.length} 个`);
    const trend = pnl > 0 ? `PnL +${round2(pnl)}（盈）` : pnl < 0 ? `PnL ${round2(pnl)}（亏）` : "PnL 0（平）";
    const advice = pnl < 0
      ? "下轮建议收紧退役标准、复核连亏因子，新因子取样更保守"
      : "下轮建议维持现有因子，重点观察样本不足的新因子";
    lines.push(`- ${dog}：${trend}${bits.length ? "；" + bits.join("；") : "；本周期无退役"}。${advice}`);
  }
  let text = `下一轮因子归纳/退役方向建议（可编辑后作为 induction_notes 回传，将注入下一周期退役评估）：\n${lines.join("\n")}`;

  if (ctx && ctx.llm && typeof ctx.llm.stream === "function") {
    try {
      const prompt = `你是足球投注因子教练。基于下面本周期的因子退役与盈亏摘要，给出「下一轮因子归纳/退役方向」的简洁建议（中文，≤200字，每只狗一句，聚焦保留/收紧/观察方向，不要复述数字）：\n\n${text}`;
      const refined = (await streamText(ctx, prompt, { model, maxTokens: 800 })).trim();
      if (refined) text = refined;
    } catch { /* 回退启发式 */ }
  }
  return text;
}

/** 从 s.next_idx 起逐日跑；interactive 模式在周期边界（做了退役且非最后一天）暂停。 */
async function replaySegment(ctx, handles, cacheDir, replayDir, s) {
  const cfg = {
    dogs: s.dogs, parallel: s.parallel, model: s.model,
    personaOverrides: s.persona_overrides, personaDir: s.personaDir,
    factorReviewEvery: s.factor_review_every, userNotes: s.user_notes,
    start: s.start, parent: s.parent, signal: s.signal,
    onProgress: s.onProgress || (() => {}), days: s.days,
  };
  const acc = { trajectory: s.trajectory, reviewLog: s.reviewLog, checkpointLog: s.checkpointLog, log: s.log };
  while (s.next_idx < s.days.length) {
    const dayIdx = s.next_idx;
    const d = s.days[dayIdx];
    const { reviewDone } = await runReplayDay(ctx, handles, cacheDir, replayDir, d, dayIdx, cfg, acc);
    s.next_idx = dayIdx + 1;
    if (s.interactive && reviewDone && s.next_idx < s.days.length) {
      return { paused: true, lastDay: d, cycleEndIdx: dayIdx };
    }
  }
  return { paused: false };
}

/** 暂停：生成方向建议、落 session、返回 paused 结果（不还原状态，供续跑）。 */
async function pauseReplay(ctx, handles, cacheDir, replayDir, s, seg) {
  const d = seg.lastDay;
  const cycleReviews = s.reviewLog.filter((r) => r.day === d);
  const cycleStartIdx = Math.max(0, seg.cycleEndIdx - s.factor_review_every + 1);
  const cycleTraj = s.trajectory.slice(cycleStartIdx, seg.cycleEndIdx + 1);
  const suggestion = await buildDirectionSuggestion(ctx, { dogs: s.dogs, cycleReviews, cycleTraj, model: s.model });
  s.status = "paused";
  s.pending_direction = suggestion;
  saveSession(replayDir, s);
  const nextDay = s.days[s.next_idx];
  return {
    ok: true,
    status: "paused",
    run_id: s.run_id,
    replay_dir: replayDir,
    cycle: { end_day: d, days_done: s.next_idx, days_total: s.days.length },
    next_day: nextDay,
    remaining_days: s.days.length - s.next_idx,
    direction_suggestion: suggestion,
    factor_reviews: cycleReviews,
    trajectory_tail: s.trajectory.slice(-s.factor_review_every),
    checkpoints: listCheckpoints(replayDir),
    how_to: {
      continue: `ds_replay(resume_run_id="${s.run_id}", induction_notes="<编辑后的方向>")`,
      rewind: `ds_replay(resume_run_id="${s.run_id}", rewind_to="${nextDay || d}")`,
      run_to_end: `ds_replay(resume_run_id="${s.run_id}", to_end=true)`,
    },
    log: s.log.slice(-20),
  };
}

/** 回退：把线上域恢复到"某天开始"状态（前一天终态/起点），截断该天及之后的轨迹与检查点。 */
async function rewindSession(handles, replayDir, s, toDay) {
  const idx = s.days.indexOf(toDay);
  if (idx < 0) return { error: `rewind_to 不在回放范围: ${toDay}（范围 ${s.start}~${s.end}）` };
  const src = idx === 0
    ? join(replayDir, "snapshot")
    : join(replayDir, "checkpoints", `${s.days[idx - 1]}__post-day`);
  if (!existsSync(src)) {
    return { error: `缺少可回退快照：${idx === 0 ? "snapshot" : s.days[idx - 1] + "__post-day"}` };
  }
  const restored = await restoreDomainSnapshot(handles, src);
  s.trajectory = s.trajectory.filter((t) => t.day < toDay);
  s.reviewLog = s.reviewLog.filter((r) => r.day < toDay);
  s.checkpointLog = s.checkpointLog.filter((c) => c.day < toDay);
  s.next_idx = idx;
  s.log.push(`⏪ 回退到 ${toDay} 开始状态（恢复自 ${idx === 0 ? "起点快照" : s.days[idx - 1] + " 终态"}）`);
  return { idx, restored };
}

/** 收尾：终态检查点 + 报告 + 可选还原起点，标记 session 完成。 */
async function finalizeReplay(handles, cacheDir, replayDir, s, restoreAfter) {
  const postFactor = `${s.end}__post-factor`;
  await writeDomainSnapshot(handles, join(replayDir, "checkpoints", postFactor));
  s.checkpointLog.push({ name: postFactor, day: s.end, phase: "因子流后（终态）" });

  const finalCapital = {};
  for (const dog of s.dogs) {
    const domain = await handles["ds_roles"];
    const role = domain.table("roles").get(dog);
    finalCapital[dog] = Number((role && role.capital) || 0);
  }

  const report = {
    run_id: s.run_id,
    created_at: beijingNowIso(),
    range: { start: s.start, end: s.end, days: s.days.length },
    dogs: s.dogs,
    model: s.model,
    parallel: s.parallel,
    reset: s.reset,
    restore_after: restoreAfter,
    factor_review_every: s.factor_review_every,
    user_notes: s.user_notes,
    data_prep: s.data_prep,
    start_capital: s.start_capital,
    end_capital: finalCapital,
    trajectory: s.trajectory,
    factor_reviews: s.reviewLog,
    checkpoints: listCheckpoints(replayDir),
    checkpoint_log: s.checkpointLog,
    warnings: s.log.filter((l) => l.startsWith("⚠️")),
  };
  writeJson(join(replayDir, "report.json"), report);
  writeJson(join(replayDir, "replay.log.json"), s.log);
  writeFileSync(join(replayDir, "report.md"), buildReportMarkdown(report, s.log), "utf8");

  let restored = null;
  if (restoreAfter) restored = await restoreDomains(handles, replayDir);
  s.status = "finished";
  saveSession(replayDir, s);

  return {
    ok: true,
    status: "finished",
    run_id: s.run_id,
    replay_dir: replayDir,
    days: s.days.length,
    dogs: s.dogs,
    model: s.model,
    data_prep: s.data_prep,
    start_capital: s.start_capital,
    end_capital: finalCapital,
    restored,
    report_path: join(replayDir, "report.md"),
    log: s.log.slice(-40),
  };
}

/**
 * 跑一轮回放（支持 auto 一路到底 / interactive 半交互 / resume 续跑 / rewind 回退）。
 *
 * @param {object} opts
 *   全新：start/end/dogs/parallel/model/user_notes/persona_overrides/personaDir/
 *         factor_review_every/reset("none"|"zero")/restore_after/run_id/pythonBin/
 *         interactive(=true 或 mode="interactive" 每周期暂停)
 *   续跑：resume_run_id（续上次暂停会话）+ induction_notes（用户编辑的下一轮方向，注入退役评估）
 *         + to_end（本次一路跑到底，不再暂停）+ rewind_to（回退到某天开始状态）
 */
export async function runReplay(ctx, handles, cacheDir, engineRoot, opts = {}) {
  if (opts.resume_run_id) return resumeReplay(ctx, handles, cacheDir, opts);

  const start = opts.start;
  const end = opts.end;
  // 范围正确性最先校验：任何快照/重置/取数都不允许在无效范围上发生
  const rangeCheck = validateReplayRange(start, end);
  if (!rangeCheck.ok) return rangeCheck;
  const interactive = opts.interactive === true || opts.mode === "interactive";
  const dogs = (opts.dogs && opts.dogs.length ? opts.dogs : DS_REAL_DOGS).slice();
  const parallel = Math.max(1, Math.min(Number(opts.parallel) || dogs.length, dogs.length || 1));
  const model = opts.model || REPLAY_MODEL;
  const factorReviewEvery = Math.max(1, Number(opts.factor_review_every) || 7);
  const reset = opts.reset === "zero" ? "zero" : "none";
  // 默认写穿：订单/因子留在 storage 域；只有显式 restore_after=true 才在结束后还原（模拟跑）
  const restoreAfter = opts.restore_after === true;
  const userNotes = String(opts.user_notes || "").trim();
  const personaOverrides = opts.persona_overrides || {};
  const personaDir = opts.personaDir || undefined; // 外置人设根目录（config.personaDir）
  const onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};

  // ── 启动前预检（全部在快照/重置/取数/任何写操作之前，失败不落任何状态）──
  // 1) subagent 能力：缺 parent/provider 时立刻给出明确错误，不要跑到一半才提示
  const spawnCheck = validateReplaySpawn(ctx, opts.parent);
  if (spawnCheck) return { ok: false, ...spawnCheck };
  // 2) 范围订单冲突：回放会写穿订单/因子；范围里已有订单属于 diff 场景，暂不支持
  const rangeOrders = await findRangeOrderConflicts(handles, dogs, start, end);
  if (rangeOrders.conflicts.length) {
    const dogsHit = [...new Set(rangeOrders.conflicts.map((c) => c.dog))].join("、");
    const sample = rangeOrders.conflicts.slice(0, 3)
      .map((c) => `${c.dog} ${c.day} ${c.lota_id || "?"}/${c.bet_type || "?"}`)
      .join("；");
    return {
      ok: false,
      error: `回放范围 ${start} ~ ${end} 内已有 ${rangeOrders.conflicts.length} 笔订单（涉及 ${dogsHit}），暂不支持：该场景需要 diff/diff-report 工具，尚未设计。请清空对应时段订单或更换回放范围。示例：${sample}`,
      conflicts: rangeOrders.conflicts.slice(0, 20),
    };
  }

  const runId = opts.run_id || `replay_${start}_${end}_${Date.now()}`;
  const replayDir = join(cacheDir, "replays", runId);
  mkdirSync(join(replayDir, "snapshot"), { recursive: true });

  const log = [];
  const warn = (msg) => log.push(`⚠️ ${msg}`);

  // 0) 快照起点（供轨迹对比 + restore_after=true 时还原）
  await snapshotDomains(handles, replayDir);
  try {
    // 0b) 可选：从 0 开始（初始资金 + 空记忆）
    if (reset === "zero") {
      const resetDogs = await resetRolesToZero(handles, dogs);
      log.push(`🧹 reset=zero：${resetDogs.join("、")} 已重置为初始资金 + 空记忆`);
    }

    // 1) 范围数据一次性准备（历史缓存优先，缺了才拉 URL）
    log.push(`📦 准备回放数据 ${start} ~ ${end} ...`);
    const rangePrep = await prepareRange({
      cacheDir, engineRoot, start, end, pythonBin: opts.pythonBin,
      onProgress: (p) => onProgress({ ...p, phase: `数据准备：${p.phase}` }),
    });
    log.push(
      `   matches 文件：新拉 ${rangePrep.matches_fetched.length ? rangePrep.matches_fetched.join(",") : "无（全缓存命中）"}` +
      (rangePrep.matches_failed.length ? ` | 失败 ${rangePrep.matches_failed.map((f) => f.date).join(",")}` : "") +
      `；features/tags 补齐：${rangePrep.features_fetched.length ? rangePrep.features_fetched.join(",") : "无（全缓存命中）"}` +
      (rangePrep.features_failed.length ? ` | 失败 ${rangePrep.features_failed.map((f) => f.date).join(",")}` : ""),
    );
    for (const f of rangePrep.matches_failed) warn(`refresh-date ${f.date}: ${f.stderr}`);
    for (const f of rangePrep.features_failed) warn(`prefetch ${f.date}: ${f.stderr}`);
    const snapDates = snapshotMatchesRange(cacheDir, replayDir, rangePrep.dates);
    log.push(`   matches 快照：${snapDates.length ? snapDates.join("、") : "(无)"} → replays/${runId}/cache/matches`);

    // 起点资金
    const startCapital = {};
    for (const dog of dogs) {
      const role = (await handles["ds_roles"]).table("roles").get(dog);
      startCapital[dog] = Number((role && role.capital) || 0);
    }

    // 会话状态（供 interactive 续跑/回退）
    const s = {
      run_id: runId, start, end, dogs, parallel, model,
      factor_review_every: factorReviewEvery, reset, restore_after: restoreAfter,
      persona_overrides: personaOverrides, personaDir,
      interactive, days: dayListOf(start, end), next_idx: 0, status: "running",
      user_notes: userNotes, start_capital: startCapital,
      trajectory: [], reviewLog: [], checkpointLog: [], log,
      data_prep: rangePrep, created_at: beijingNowIso(),
      // 运行态（不落盘）
      parent: opts.parent, signal: opts.signal, onProgress,
    };
    saveSession(replayDir, s);

    const seg = await replaySegment(ctx, handles, cacheDir, replayDir, s);
    if (seg.paused) return await pauseReplay(ctx, handles, cacheDir, replayDir, s, seg);
    return await finalizeReplay(handles, cacheDir, replayDir, s, restoreAfter);
  } catch (e) {
    // 失败兜底：只有 restore_after=true（模拟跑）才还原起点；默认写穿时保留中间态供 rewind/手动恢复
    if (restoreAfter) { try { await restoreDomains(handles, replayDir); } catch {} }
    throw e;
  }
}

/** 续跑一个已暂停的交互式回放会话（可选先 rewind、注入 induction_notes、或 to_end 一路到底）。 */
async function resumeReplay(ctx, handles, cacheDir, opts) {
  const runId = String(opts.resume_run_id);
  const replayDir = join(cacheDir, "replays", runId);
  const s = loadSession(replayDir);
  if (!s) return { ok: false, error: `找不到回放会话: ${runId}（session.json 缺失）` };
  if (s.status === "finished") {
    return { ok: false, error: `回放 ${runId} 已结束；如需分叉可先 ds_replay_restore(run_id, checkpoint) 再另起新 run` };
  }
  // 恢复运行态（不落盘的字段）
  s.parent = opts.parent;
  s.signal = opts.signal;
  s.onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};

  // 可选：回退到某天开始状态
  let rewound = null;
  if (opts.rewind_to) {
    const r = await rewindSession(handles, replayDir, s, String(opts.rewind_to));
    if (r.error) return { ok: false, error: r.error };
    rewound = { to: String(opts.rewind_to), restored: r.restored };
    saveSession(replayDir, s);
    // 纯回退（未同时给方向/续跑指令）：把控制权交回用户，不自动续跑
    if (!opts.to_end && !(typeof opts.induction_notes === "string" && opts.induction_notes.trim())) {
      s.status = "paused";
      saveSession(replayDir, s);
      return {
        ok: true, status: "rewound", run_id: runId, replay_dir: replayDir,
        rewind: rewound, next_day: s.days[s.next_idx],
        remaining_days: s.days.length - s.next_idx,
        checkpoints: listCheckpoints(replayDir),
        how_to: {
          continue: `ds_replay(resume_run_id="${runId}", induction_notes="<方向>")`,
          run_to_end: `ds_replay(resume_run_id="${runId}", to_end=true)`,
        },
        log: s.log.slice(-20),
      };
    }
  }

  // 本次要继续跑（不是纯 rewind）：先确认 subagent 能力，失败不进入 running
  const spawnCheck = validateReplaySpawn(ctx, s.parent);
  if (spawnCheck) return { ok: false, ...spawnCheck };

  // 用户方向：作为下一周期退役评估的 user_notes
  if (typeof opts.induction_notes === "string" && opts.induction_notes.trim()) {
    s.user_notes = opts.induction_notes.trim();
    s.log.push(`📝 应用下一轮方向（induction_notes）：${s.user_notes.slice(0, 120)}`);
  }
  // to_end：本次一路跑到底（关闭暂停）
  if (opts.to_end === true) s.interactive = false;
  s.status = "running";

  try {
    const seg = await replaySegment(ctx, handles, cacheDir, replayDir, s);
    if (seg.paused) return await pauseReplay(ctx, handles, cacheDir, replayDir, s, seg);
    return await finalizeReplay(handles, cacheDir, replayDir, s, s.restore_after);
  } catch (e) {
    // 续跑失败：不还原（保留中间态，供用户 rewind / ds_replay_restore 手动处理）
    s.status = "paused";
    s.last_error = String((e && e.message) || e);
    saveSession(replayDir, s);
    return { ok: false, status: "paused", run_id: runId, error: `续跑失败: ${s.last_error}` };
  }
}

/** 生成人类可读的回放报告（轨迹表 + 对比 + 因子退役）。 */
export function buildReportMarkdown(report, logLines = []) {
  const rows = report.trajectory.map((t) => {
    const cells = report.dogs.map((dog) => {
      const d = t.dogs[dog] || {};
      return `${d.capital ?? "-"}(${d.pnl >= 0 ? "+" : ""}${d.pnl ?? 0})`;
    });
    return `| ${t.day} | ${cells.join(" | ")} |`;
  }).join("\n");

  const header = `| 日期 | ${report.dogs.map((d) => `余额(PnL)`).join(" | ")} |`;
  const sep = `|---|${report.dogs.map(() => "---|").join("")}`;

  const reviewLines = report.factor_reviews.length
    ? report.factor_reviews.map((r) => {
        const bits = [];
        if (r.auto_dormant && r.auto_dormant.length) bits.push(`休眠: ${r.auto_dormant.join("、")}`);
        if (r.low_info_retired && r.low_info_retired.length) bits.push(`低信息退役: ${r.low_info_retired.join("、")}`);
        if (r.retired && r.retired.length) bits.push(`结构性退役: ${r.retired.join("、")}`);
        return `- [${r.day}] ${r.dog}: ${bits.length ? bits.join("；") : "无调整"}`;
      }).join("\n")
    : "(未触发或窗口内无因子)";

  const cmp = report.dogs.map((dog) => {
    const s = report.start_capital[dog] ?? 0;
    const e = report.end_capital[dog] ?? 0;
    return `- ${dog}: ${s} → ${e}（${e - s >= 0 ? "+" : ""}${Math.round(e - s)}）`;
  }).join("\n");

  const warnings = report.warnings.length ? report.warnings.map((w) => `- ${w}`).join("\n") : "(无)";

  return `# 回放报告 ${report.run_id}

- 范围：${report.range.start} ~ ${report.range.end}（${report.range.days} 天）
- 狗：${report.dogs.join("、")}
- 模型：${report.model}（旁路 LLM）｜并行：${report.parallel}
- 起点：${report.reset === "zero" ? "从 0 重置" : "当前状态"}｜结束处理：${report.restore_after ? "还原起点（模拟）" : "写穿保留（订单/因子已落库）"}
- 因子退役周期：每 ${report.factor_review_every} 天${report.user_notes ? `｜用户意见：${report.user_notes}` : ""}

## 数据准备（竞彩边界）

- 日历日期：${(report.data_prep.dates || []).join("、")}
- matches 新拉：${report.data_prep.matches_fetched.length ? report.data_prep.matches_fetched.join("、") : "无（缓存命中）"}
- features/tags 补齐：${report.data_prep.features_fetched.length ? report.data_prep.features_fetched.join("、") : "无（缓存命中）"}
- 警告：${warnings}

## 轨迹（每狗余额与当日 PnL）

${header}
${sep}
${rows || "(无轨迹)"}

## 起点 vs 终点

${cmp}

## 检查点（可恢复到某阶段前）

${(report.checkpoints || ["start"]).map((c) => `- \`${c}\``).join("\n")}

## 因子退役记录（含用户意见）

${reviewLines}

## 运行日志（节选）

\`\`\`text
${(logLines || []).slice(-30).join("\n")}
\`\`\`
`;
}
