/**
 * 业务流入口（docs/workflow_tool_groups.md §2）：
 *   - 分析流 → fanout.js analyzeDogsParallel（已注册 ds_analyze_all_parallel）
 *   - 结算流 → settleEngine.js settleAll（纯 JS，无 LLM，已注册 ds_settle_all）
 *   - 因子流 → 本文件 factorFlow（阶段A 非alpha归纳 → B alpha barrier → C 退役）
 *
 * 因子流 LLM 上下文约定（目标3）：
 *   - 父任务（ds_factor_flow）带出多个子任务：每阶段/每狗一次自包含 LLM 调用；
 *   - 每个子任务的 prompt 只含它需要的最小上下文（候选对 / 该狗候选+近期反思），
 *     不把大段文本（全量因子表/全量反思/整份人设）重复拼接进每个调用，避免 token 膨胀。
 */
import { beijingNowIso } from "./settle.js";
import { DS_REAL_DOGS } from "./storage.js";
import { inductFactors, inductAlpha, ALPHA_DOGS } from "./factorInduction.js";
import { factorReview } from "./factorReview.js";
import { reflectDog } from "./replay.js";
import { readPersona } from "./reflect.js";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

export const FLOW_MODEL = "deepseek-v4-flash";

/** 比赛开赛时间 → 足球日标签（[D 12:01, D+1 12:00]，12:00 前归上一足球日）。 */
function footballDayOf(matchTime) {
  const t = String(matchTime || "").replace("T", " ").slice(0, 16);
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(t);
  if (!m) return "";
  const [y, mo, d, hh, mm] = m.slice(1).map(Number);
  const dt = new Date(Date.UTC(y, mo - 1, d + (hh >= 12 || (hh === 12 && mm >= 1) ? 0 : -1)));
  return dt.toISOString().slice(0, 10);
}

/** 从 matches 缓存构建 lota_id → match_time（扫描最近 10 个日期文件）。 */
function buildMatchTimeMap(cacheDir) {
  const dir = join(cacheDir, "matches");
  const map = {};
  let files = [];
  try { files = readdirSync(dir).filter((f) => f.endsWith(".json")).sort().slice(-10); } catch { return map; }
  for (const f of files) {
    try {
      const raw = JSON.parse(readFileSync(join(dir, f), "utf8"));
      const ms = Array.isArray(raw) ? raw : (raw && raw.matches) || [];
      for (const m of ms) if (m && m.lota_id && m.match_time && !map[m.lota_id]) map[m.lota_id] = m.match_time;
    } catch {}
  }
  return map;
}

/** 某狗的已结算订单里，属于某足球日的部分（保持订单原字段，供 reflectDog 使用）。 */
async function settledOrdersForDay(handles, cacheDir, dog, day) {
  const rolesDomain = await handles["ds_roles"];
  const role = rolesDomain.table("roles").get(dog);
  if (!role) return [];
  const matchTime = buildMatchTimeMap(cacheDir);
  return (role.orders || []).filter((o) => {
    if (!o.settled_at) return false;
    return footballDayOf(matchTime[o.lota_id]) === day;
  });
}

/** 自动选"最近有已结算订单的足球日"（reflectDay='auto'/空时）。 */
async function latestSettledDay(handles, cacheDir, dogs) {
  const rolesDomain = await handles["ds_roles"];
  const matchTime = buildMatchTimeMap(cacheDir);
  let best = "";
  for (const dog of dogs) {
    const role = rolesDomain.table("roles").get(dog);
    if (!role) continue;
    for (const o of (role.orders || [])) {
      if (!o.settled_at) continue;
      const d = footballDayOf(matchTime[o.lota_id]);
      if (d && d > best) best = d;
    }
  }
  return best;
}

/**
 * 因子流。
 * @param {object} opts
 *   scope: "induct" | "review" | "all"（默认 all = 归纳 + 退役）
 *   endDate / dogs / model / limit / userNotes / cacheDir / onProgress
 */
export async function factorFlow(handles, ctx, {
  scope = "all",
  endDate = "",
  dogs = DS_REAL_DOGS,
  model = FLOW_MODEL,
  limit = 30,
  userNotes = "",
  cacheDir = "",
  reflectDay = "",
  onProgress,
} = {}) {
  const dogList = (dogs && dogs.length ? dogs : DS_REAL_DOGS).slice();
  const nonAlpha = dogList.filter((d) => !ALPHA_DOGS.includes(d));
  const alpha = dogList.filter((d) => ALPHA_DOGS.includes(d));
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const log = [];
  const end = endDate || beijingNowIso().slice(0, 10);

  // ── 阶段 0：反思（可选）。reflectDay='auto'/空 = 最近有已结算订单的足球日；
  //    已有当天反思的狗跳过（幂等）。目标是"从某批结算订单生成新因子"（如 0816 订单）。──
  let reflectTarget = String(reflectDay || "").trim();
  if (reflectTarget && reflectTarget !== "auto") {
    // 显式日期，原样使用
  } else if (cacheDir) {
    reflectTarget = await latestSettledDay(handles, cacheDir, dogList);
  } else {
    reflectTarget = "";
  }
  if (reflectTarget) {
    progress({ phase: `因子流·阶段0 反思 ${reflectTarget}`, done: 0, total: dogList.length });
    let doneCount = 0;
    for (const dog of dogList) {
      const settled = await settledOrdersForDay(handles, cacheDir, dog, reflectTarget);
      let skipped = "无该日结算单";
      if (settled.length) {
        const refsRec = (await handles["ds_reflections"]).table("reflections").get(dog) || {};
        const already = (refsRec.reflections || []).some((r) => r.date === reflectTarget);
        if (already) {
          skipped = "已有当天反思（幂等跳过）";
        } else {
          progress({ phase: `因子流·阶段0·反思 ${dog}`, done: doneCount, total: dogList.length, detail: `${settled.length} 单` });
          const persona = cacheDir ? readPersona(cacheDir, dog) : "";
          const r = await reflectDog(handles, ctx, dog, reflectTarget, settled, { model, persona });
          if (r && r.ok === false) log.push(`⚠️ 反思 ${dog} 失败: ${r.error}`);
          else log.push(`🧠 阶段0 ${dog}: 反思 ${settled.length} 单`);
          doneCount += 1;
          progress({ phase: `因子流·阶段0·反思 ${dog}`, done: doneCount, total: dogList.length, status: "ok" });
          continue;
        }
      }
      doneCount += 1;
      progress({ phase: `因子流·阶段0·反思 ${dog}`, done: doneCount, total: dogList.length, detail: skipped });
    }
  }

  const inductOne = async (dog) => {
    const factorsDomain = await handles["ds_factors"];
    const rec = factorsDomain.table("factors").get(dog) || { factor_perf: {} };
    const { result, factorPerf } = await inductFactors(ctx, rec.factor_perf || {}, { limit, scope: dog });
    await factorsDomain.table("factors").put(dog, {
      ...rec,
      factor_perf: factorPerf,
      updated_at: beijingNowIso(),
    });
    return { dog, merged: ((result && result.merged) || []).length };
  };

  const reviewOne = async (dog) => {
    // 子任务 prompt：只含该狗 persona + 近7天反思 + 候选因子（factorReview 内部精简组装）
    return factorReview(handles, ctx, dog, end, "", cacheDir, {
      userNotes: userNotes || "",
      model,
    });
  };

  // ── 阶段 A：非 alpha 归纳（各自子任务，互不依赖）──
  if (scope === "induct" || scope === "all") {
    progress({ phase: "因子流·阶段A 非alpha归纳", done: 0, total: Math.max(nonAlpha.length, 1) });
    const resA = [];
    await Promise.all(nonAlpha.map(async (dog, i) => {
      progress({ phase: `因子流·阶段A·归纳 ${dog}`, done: i, total: nonAlpha.length });
      const r = await inductOne(dog);
      resA.push(r);
      progress({ phase: `因子流·阶段A·归纳 ${dog}`, done: i + 1, total: nonAlpha.length, status: "ok" });
    }));
    for (const r of resA) log.push(`🧬 阶段A ${r.dog}: 合并 ${r.merged} 个`);
  }

  // ── 阶段 B（barrier）：非 alpha 全部完成后，alpha 跨狗统一归纳一次进全库 ──
  if ((scope === "induct" || scope === "all") && alpha.length) {
    progress({ phase: "因子流·阶段B alpha barrier", done: 0, total: 1 });
    const res = await inductAlpha(ctx, handles, { limit });
    progress({ phase: "因子流·阶段B alpha barrier 完成", done: 1, total: 1 });
    log.push(`🔗 阶段B alpha 跨狗归纳: merged=${((res && res.result && res.result.merged) || []).length}`);
  }

  // ── 阶段 C：退役（非 alpha 先行 → alpha 收尾；userNotes 注入评估方向）──
  if (scope === "review" || scope === "all") {
    const ordered = [...nonAlpha, ...alpha];
    progress({ phase: "因子流·阶段C 退役", done: 0, total: Math.max(ordered.length, 1) });
    const resC = [];
    for (let i = 0; i < ordered.length; i++) {
      const dog = ordered[i];
      progress({ phase: `因子流·阶段C·退役 ${dog}`, done: i, total: ordered.length });
      const r = await reviewOne(dog);
      resC.push(r);
      progress({
        phase: `因子流·阶段C·退役 ${dog}`,
        done: i + 1,
        total: ordered.length,
        status: r && r.ok !== false ? "ok" : "fail",
      });
    }
    for (const r of resC) {
      log.push(`🔬 阶段C ${r.dog}: 候选=${r.candidates || 0} 退役=${(r.retired || []).length} 休眠=${(r.dormant || []).length}`);
    }
  }

  return { ok: true, scope, end_date: end, dogs: dogList, log, text: log.join("\n") };
}
