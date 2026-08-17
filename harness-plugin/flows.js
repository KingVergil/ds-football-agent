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

export const FLOW_MODEL = "deepseek-v4-flash";

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
  onProgress,
} = {}) {
  const dogList = (dogs && dogs.length ? dogs : DS_REAL_DOGS).slice();
  const nonAlpha = dogList.filter((d) => !ALPHA_DOGS.includes(d));
  const alpha = dogList.filter((d) => ALPHA_DOGS.includes(d));
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const log = [];
  const end = endDate || beijingNowIso().slice(0, 10);

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
