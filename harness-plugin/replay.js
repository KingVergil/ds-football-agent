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
 *   - 起点快照（storage 域全量）→ replayDir/snapshot/，可 restore_after 还原（默认还原）
 *   - reset="zero" 时从初始资金/空记忆开始（"从 0 开始"）
 */
import { mkdirSync, writeFileSync, readFileSync, existsSync, copyFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { beijingNowIso } from "./settle.js";
import { DS_REAL_DOGS } from "./storage.js";
import { prepareRange, addDays, jingcaiWindowMatches, hasValidFeature, hasTags } from "./dataflow.js";
import { analyzeDogsParallel } from "./fanout.js";
import { settleDog } from "./settleEngine.js";
import {
  buildReflectPrompt, streamReflectJson, parseReflectJson, applyReflection,
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

/** 从 srcDir 还原 storage 域（文件直接放 srcDir 下）。 */
export async function restoreDomainSnapshot(handles, srcDir) {
  const restored = {};
  for (const [domainName, tableName] of Object.entries(SNAPSHOT_TABLES)) {
    const rec = readJson(join(srcDir, `${domainName}__${tableName}.json`));
    if (!rec) continue;
    const domain = await handles[domainName];
    const table = domain.table(tableName);
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

/** 读某狗回放人设：persona_overrides[dog] 优先，否则 persona.md。 */
function personaFor(cacheDir, dog, personaOverrides) {
  const over = personaOverrides && personaOverrides[dog];
  if (over) return over;
  return readPersona(cacheDir, dog);
}

/** 反思（等价 ds_reflect_js，模型/人设可覆盖）。 */
export async function reflectDog(handles, ctx, dog, day, settled, { model = REPLAY_MODEL, persona } = {}) {
  if (!settled || !settled.length) return { ok: true, skipped: "无结算单，跳过反思" };
  const existingSummary = await getExistingFactorSummary(handles, dog);
  const prompt = buildReflectPrompt({
    persona: persona || "",
    settled, existingSummary, factorDescText: "", keySlugWhitelist: SLUG_WHITELIST,
  });
  const text = await streamReflectJson(ctx, prompt, { model });
  const data = parseReflectJson(text);
  if (!data) return { ok: false, error: "reflect JSON 解析失败", raw: text.slice(0, 500) };
  return applyReflection(handles, dog, day, data, settled);
}

/**
 * 跑一轮回放。
 * @param {object} opts
 *   start/end/dogs/parallel/model/jingcai_only/user_notes/persona_overrides/
 *   factor_review_every/reset("none"|"zero")/restore_after/run_id/replay_dir
 */
export async function runReplay(ctx, handles, cacheDir, engineRoot, opts = {}) {
  const start = opts.start;
  const end = opts.end;
  if (!start || !end || start > end) {
    return { ok: false, error: `回放范围无效: ${start} ~ ${end}` };
  }
  const dogs = (opts.dogs && opts.dogs.length ? opts.dogs : DS_REAL_DOGS).slice();
  const parallel = Math.max(1, Math.min(Number(opts.parallel) || dogs.length, dogs.length || 1));
  const model = opts.model || REPLAY_MODEL;
  const factorReviewEvery = Math.max(1, Number(opts.factor_review_every) || 7);
  const reset = opts.reset === "zero" ? "zero" : "none";
  const restoreAfter = opts.restore_after !== false;
  const userNotes = String(opts.user_notes || "").trim();
  const personaOverrides = opts.persona_overrides || {};
  const onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};

  const runId = opts.run_id || `replay_${start}_${end}_${Date.now()}`;
  const replayDir = join(cacheDir, "replays", runId);
  mkdirSync(join(replayDir, "snapshot"), { recursive: true });

  const log = [];
  const warn = (msg) => log.push(`⚠️ ${msg}`);

  // 0) 快照起点（供 restore + 轨迹对比）
  const snapshot = await snapshotDomains(handles, replayDir);
  // 快照之后无论成败都必须还原起点：防止 reset / 回放中间态泄漏到线上
  let restored = null;
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
  // 范围数据快照：逐日循环只读快照，避免 web 刷新器并发改写 matches 造成边界抖动
  const snapDates = snapshotMatchesRange(cacheDir, replayDir, rangePrep.dates);
  log.push(`   matches 快照：${snapDates.length ? snapDates.join("、") : "(无)"} → replays/${runId}/cache/matches`);

  // 2) 逐日管线
  const trajectory = []; // { day, dogs: { dog: {capital, placed, pnl, pending, factors} } }
  const reviewLog = [];
  const checkpointLog = [];
  const startCapital = {};
  for (const dog of dogs) {
    const domain = await handles["ds_roles"];
    const role = domain.table("roles").get(dog);
    startCapital[dog] = Number((role && role.capital) || 0);
  }

  let day = start;
  let dayIdx = 0;
  const days = [];
  while (day <= end) {
    days.push(day);
    day = addDays(day, 1);
  }

  for (dayIdx = 0; dayIdx < days.length; dayIdx++) {
    const d = days[dayIdx];
    const startedAt = beijingNowIso();
    log.push(`\n📅 [${d}] 第 ${dayIdx + 1}/${days.length} 天（${startedAt}）`);

    // 2.1 数据边界：只读回放快照（matches），features/tags 完整性查真实缓存
    const data = readReplayDay(cacheDir, replayDir, d);
    log.push(`   竞彩 ${data.jingcai_count}/${data.window_total} 场（排除北单/无号 ${data.excluded_count}）`);
    for (const w of data.warnings) warn(`[${d}] ${w}`);

    // 2.2 并行分析（fan-out；人设已注入）
    onProgress({ phase: `第 ${dayIdx + 1}/${days.length} 天 分析`, done: dayIdx, total: days.length, detail: d });
    const analysis = await analyzeDogsParallel(ctx, {
      day: d,
      dogs,
      parallel,
      parent: opts.parent,
      signal: opts.signal,
      cacheDir,
      personas: personaOverrides,
      onProgress: (p) => onProgress({
        phase: `第 ${dayIdx + 1}/${days.length} 天 ${p.phase || "分析"}`,
        done: p.idx || dayIdx, total: days.length,
        detail: p.dog ? `${p.dog} ${p.status || ""}` : p.detail || d,
      }),
    });
    log.push(`   分析: ok=${analysis.ok_count} fail=${analysis.fail_count} matches=${analysis.matches_count}`);
    const placedByDog = {};
    for (const row of analysis.rows || []) {
      const m = String(row.text || "").match(/\{\s*"dog".*?"placed"\s*:\s*(\d+)/);
      placedByDog[row.dog] = m ? Number(m[1]) : 0;
    }
    for (const row of analysis.rows || []) {
      log.push(`     ${row.ok ? "✅" : "❌"} ${row.dog} [${row.stopReason}] ${String(row.text || "").slice(0, 120).replace(/\n/g, " ")}`);
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
        onProgress({ phase: `第 ${dayIdx + 1}/${days.length} 天 结算 ${dog}`, done: dayIdx, total: days.length });
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
          onProgress({ phase: `第 ${dayIdx + 1}/${days.length} 天 因子流·反思 ${dog}`, done: dayIdx, total: days.length });
          const reflectRes = await reflectDog(handles, ctx, dog, d, settled.orders, {
            model,
            persona: personaFor(cacheDir, dog, personaOverrides),
          });
          if (reflectRes && reflectRes.ok === false) warn(`[${d}] ${dog} 反思失败: ${reflectRes.error}`);
        }
      }

      // 阶段 A：非 alpha 各自归纳（可并行，这里顺序执行；alpha 狗跳过留到阶段 B）
      for (const dog of dogs) {
        if (ALPHA_DOGS.includes(dog)) continue;
        onProgress({ phase: `第 ${dayIdx + 1}/${days.length} 天 因子流·阶段A ${dog}`, done: dayIdx, total: days.length });
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
        onProgress({ phase: `第 ${dayIdx + 1}/${days.length} 天 因子流·阶段B alpha barrier`, done: dayIdx, total: days.length });
        await inductAlpha(ctx, handles, { limit: 30 });
      }
    } catch (e) {
      warn(`[${d}] 因子归纳失败: ${(e && e.message) || e}`);
    }

    // 2.5 周期性因子退役（阶段 C：非 alpha 先行 → alpha 收尾；带用户调整意见）
    if ((dayIdx + 1) % factorReviewEvery === 0) {
      onProgress({ phase: `第 ${dayIdx + 1}/${days.length} 天 因子流·阶段C 退役`, done: dayIdx, total: days.length });
      const ordered = [...dogs.filter((x) => !ALPHA_DOGS.includes(x)), ...dogs.filter((x) => ALPHA_DOGS.includes(x))];
      for (const dog of ordered) {
        try {
          const review = await factorReview(handles, ctx, dog, d, start, cacheDir, {
            userNotes,
            persona: personaFor(cacheDir, dog, personaOverrides),
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
  }

  // 回放结束时补一个"因子流后"检查点（含全部阶段的最终状态）
  const postFactor = `${end}__post-factor`;
  await writeDomainSnapshot(handles, join(replayDir, "checkpoints", postFactor));
  checkpointLog.push({ name: postFactor, day: end, phase: "因子流后（终态）" });

  // 3) 报告
  const finalCapital = {};
  for (const dog of dogs) {
    const domain = await handles["ds_roles"];
    const role = domain.table("roles").get(dog);
    finalCapital[dog] = Number((role && role.capital) || 0);
  }

  const report = {
    run_id: runId,
    created_at: beijingNowIso(),
    range: { start, end, days: days.length },
    dogs,
    model,
    parallel,
    reset,
    restore_after: restoreAfter,
    factor_review_every: factorReviewEvery,
    user_notes: userNotes,
    data_prep: rangePrep,
    start_capital: startCapital,
    end_capital: finalCapital,
    trajectory,
    factor_reviews: reviewLog,
    checkpoints: listCheckpoints(replayDir),
    checkpoint_log: checkpointLog,
    warnings: log.filter((l) => l.startsWith("⚠️")),
  };
  writeJson(join(replayDir, "report.json"), report);
  writeJson(join(replayDir, "replay.log.json"), log);
  writeFileSync(join(replayDir, "report.md"), buildReportMarkdown(report, log), "utf8");

    // 4) 还原起点（默认，成功路径）
    if (restoreAfter) {
      restored = await restoreDomains(handles, replayDir);
    }

    return {
      ok: true,
      run_id: runId,
      replay_dir: replayDir,
      days: days.length,
      dogs,
      model,
      data_prep: rangePrep,
      start_capital: startCapital,
      end_capital: finalCapital,
      restored,
      report_path: join(replayDir, "report.md"),
      log: log.slice(-40),
    };
  } catch (e) {
    // 失败兜底：无论哪一步抛错都必须还原起点，绝不允许把 reset/中间态留在线上
    if (restoreAfter && !restored) {
      try { restored = await restoreDomains(handles, replayDir); } catch {}
    }
    throw e;
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
- 起点：${report.reset === "zero" ? "从 0 重置" : "当前状态"}｜还原：${report.restore_after ? "是" : "否"}
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
