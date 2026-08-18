/**
 * 业务流入口（docs/workflow_tool_groups.md §2）：
 *   - 分析流 → fanout.js analyzeOneDog（已注册 ds_analyze_dog，单狗 headless；并行由父 agent 决定）
 *   - 结算流 → settleEngine.js settleAll（纯 JS，无 LLM，已注册 ds_settle_all）
 *   - 因子流 → 本文件 factorFlow（管理比赛数据 → 因子专员 xN subagent → alpha barrier → 退役）
 *
 * 因子流 LLM 上下文约定（目标3）：
 *   - 父任务（ds_factor_flow）带出多个子任务：每狗一个独立 subagent（dsh-subagent spawn provider，
 *     与并行分析同一套）；每个子任务只带自己的工具组（toolFilter allow）与最小 prompt，
 *     不把大段文本（全量因子表/全量反思/整份人设）重复拼接，避免 token 膨胀。
 */
import { beijingNowIso } from "./settle.js";
import { DS_REAL_DOGS } from "./storage.js";
import { inductAlpha, ALPHA_DOGS } from "./factorInduction.js";
import { prepareDay } from "./dataflow.js";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

export const FLOW_MODEL = "deepseek-v4-flash";

/** 比赛开赛时间 → 足球日标签（[D 12:01, D+1 12:00]，12:00 前归上一足球日）。 */
export function footballDayOf(matchTime) {
  const t = String(matchTime || "").replace("T", " ").slice(0, 16);
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(t);
  if (!m) return "";
  const [y, mo, d, hh, mm] = m.slice(1).map(Number);
  const dt = new Date(Date.UTC(y, mo - 1, d + (hh >= 12 || (hh === 12 && mm >= 1) ? 0 : -1)));
  return dt.toISOString().slice(0, 10);
}

/** 从 matches 缓存构建 lota_id → match_time（扫描最近 10 个日期文件）。 */
export function buildMatchTimeMap(cacheDir) {
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
export async function settledOrdersForDay(handles, cacheDir, dog, day) {
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
export async function latestSettledDay(handles, cacheDir, dogs) {
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
/** 因子归纳子任务 prompt（只含该狗 + 目标日，最小上下文）。 */
export function buildFactorInductPrompt(dog, day, limit) {
  return `你是 ds_agents 的「${dog}」因子归纳专员。只处理这一只狗，禁止动其他狗。

目标：从足球日 ${day} 的已结算订单反思生成因子，并归纳该狗因子库。

步骤：
1. ds_settled_js("${dog}", "${day}") 拿该日已结算订单列表（含 lota_id/类型/pick/比分/hit/profit/金额/理由）。
2. 若 settled 非空：ds_reflect_js("${dog}", "${day}", settled) 反思 → 因子归因/新因子写回。
3. ds_factor_induction(user="${dog}", limit=${limit}) 清洗/合并/判重该狗因子。

最后只输出一行 JSON（不要 markdown 代码块，不要多余文字）：
{"dog":"${dog}","day":"${day}","settled":N,"reflected":true|false,"merged":N}

禁止调用其他任何工具（无 bash/文件/分析/结算/回放工具），禁止子代理递归。`;
}

/** 因子退役子任务 prompt（只含该狗 + 评估窗口，最小上下文）。 */
export function buildFactorReviewPrompt(dog, endDate, userNotes) {
  return `你是 ds_agents 的「${dog}」因子退役专员。只处理这一只狗。

调用 ds_factor_review_js(user="${dog}", end_date="${endDate}", start_date="") 做因子结构性退役评估${userNotes ? `（用户意见：${userNotes}）` : ""}。

最后只输出一行 JSON（不要 markdown 代码块，不要多余文字）：
{"dog":"${dog}","candidates":N,"retired":N,"dormant":N}

禁止调用其他任何工具，禁止子代理递归。`;
}

/** 跑一个因子子任务：start → 等待 result → dispose（对齐 fanout.js runOneDog）。 */
async function runFactorSubagent(ctx, parent, signal, label, prompt, allowTools) {
  const run = await ctx.subagents.start("spawn", {
    label,
    prompt: [{ type: "text", text: prompt }],
    parent,
    signal,
    maxDepth: 1,
    toolFilter: {
      allow: allowTools, // 只给本流的业务工具，宿主工具（bash/fs/技能等）一律不可见
      deny: ["subagent", "subagent_fork", "ds_analyze_dog", "ds_factor_flow", "ds_replay", "ds_settle_all"],
    },
  });
  try {
    const result = await run.result;
    const text = Array.isArray(result.output)
      ? result.output.filter((b) => b && b.type === "text" && typeof b.text === "string").map((b) => b.text).join("\n")
      : "";
    return {
      ok: result.stopReason === "completed",
      stopReason: result.stopReason,
      text: text.slice(0, 800),
    };
  } finally {
    try { await run.dispose(); } catch {}
  }
}

/**
 * 因子流（编排）：管理比赛数据刷新 → 因子专员 xN（dsh-subagent spawn，每狗独立子任务）
 * → alpha barrier 收尾 →（可选）退役子任务。父任务只做 fan-out 与汇总。
 *
 * @param {object} opts
 *   scope: "induct" | "review" | "all"
 *   dogs / parallel / model / limit / userNotes / cacheDir / engineRoot / pythonBin /
 *   reflectDay（'auto'=最近已结算日）/ endDate / parent / signal / onProgress
 */
export async function factorFlow(ctx, handles, {
  scope = "all",
  endDate = "",
  dogs = DS_REAL_DOGS,
  model = FLOW_MODEL,
  limit = 30,
  userNotes = "",
  cacheDir = "",
  engineRoot = "",
  pythonBin = "python",
  reflectDay = "",
  parallel = 7,
  parent = null,
  signal = null,
  onProgress,
} = {}) {
  if (!ctx || !ctx.subagents) {
    return { ok: false, error: "ctx.subagents 不可用（host 未挂载 dsh-subagent spawn provider）" };
  }
  if (!parent) return { ok: false, error: "缺少 parent agent，无法启动因子专员子任务" };

  const dogList = (dogs && dogs.length ? dogs : DS_REAL_DOGS).slice();
  const nonAlpha = dogList.filter((d) => !ALPHA_DOGS.includes(d));
  const alpha = dogList.filter((d) => ALPHA_DOGS.includes(d));
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const log = [];
  const end = endDate || beijingNowIso().slice(0, 10);
  const workers = Math.max(1, Math.min(Number(parallel) || dogList.length, dogList.length || 1));

  // ── 1) 管理比赛数据刷新（对齐 分析/结算 流的"获取数据先行"）──
  if (engineRoot && cacheDir) {
    progress({ phase: "数据准备（刷新比赛/比分缓存）", done: 0, total: 1 });
    const prep = await prepareDay({
      cacheDir, engineRoot, day: end, mode: "live", jingcaiOnly: true, pythonBin,
      onProgress: (p) => progress({ phase: `数据准备：${p.phase}`, done: 0, total: 1, detail: p.detail }),
    });
    log.push(`📦 数据准备: 竞彩 ${prep.jingcai_count}/${prep.window_total}` + (prep.warnings && prep.warnings.length ? ` ⚠️ ${prep.warnings.join(";")}` : ""));
  }

  // 归纳目标日（阶段0 反思来源）
  let reflectTarget = String(reflectDay || "").trim();
  if (reflectTarget && reflectTarget !== "auto") {
    // 显式日期
  } else if (cacheDir) {
    reflectTarget = await latestSettledDay(handles, cacheDir, dogList);
  } else {
    reflectTarget = "";
  }

  // ── 2) 阶段0+阶段A：因子归纳专员 xN（每狗：结算单→反思→归纳）──
  if (scope === "induct" || scope === "all") {
    progress({ phase: "因子流·因子专员 x" + dogList.length, done: 0, total: dogList.length });
    const results = new Array(dogList.length);
    let cursor = 0;
    const worker = async () => {
      while (cursor < dogList.length) {
        if (signal && signal.aborted) return;
        const idx = cursor++;
        const dog = dogList[idx];
        const day = reflectTarget || end;
        progress({ phase: `因子专员·${dog}`, done: idx, total: dogList.length, detail: day });
        try {
          const r = await runFactorSubagent(
            ctx, parent, signal, `因子归纳 ${dog}`,
            buildFactorInductPrompt(dog, day, limit),
            ["ds_settled_js", "ds_reflect_js", "ds_factor_induction", "ds_factor_dedup", "ds_memory_js"],
          );
          results[idx] = { dog, ...r };
        } catch (e) {
          results[idx] = { dog, ok: false, stopReason: "start-failed", text: String((e && e.message) || e) };
        }
        progress({ phase: `因子专员·${dog}`, done: idx + 1, total: dogList.length, status: results[idx].ok ? "ok" : "fail" });
      }
    };
    await Promise.all(Array.from({ length: workers }, () => worker()));
    for (const r of results) {
      log.push(`${r.ok ? "✅" : "❌"} 因子专员·${r.dog} [${r.stopReason}] ${String(r.text || "").slice(0, 120).replace(/\n/g, " ")}`);
    }

    // ── 3) 阶段B（barrier）：非 alpha 全部完成后，alpha 跨狗统一归纳一次进全库 ──
    if (alpha.length) {
      progress({ phase: "因子流·阶段B alpha barrier", done: 0, total: 1 });
      const res = await inductAlpha(ctx, handles, { limit });
      progress({ phase: "因子流·阶段B alpha barrier 完成", done: 1, total: 1 });
      log.push(`🔗 阶段B alpha 跨狗归纳: merged=${((res && res.result && res.result.merged) || []).length}`);
    }
  }

  // ── 4) 阶段C：因子退役专员 xN（非 alpha 先行 → alpha 收尾）──
  if (scope === "review" || scope === "all") {
    const ordered = [...nonAlpha, ...alpha];
    progress({ phase: "因子流·退役专员", done: 0, total: Math.max(ordered.length, 1) });
    for (let i = 0; i < ordered.length; i++) {
      const dog = ordered[i];
      progress({ phase: `退役专员·${dog}`, done: i, total: ordered.length });
      const r = await runFactorSubagent(
        ctx, parent, signal, `因子退役 ${dog}`,
        buildFactorReviewPrompt(dog, end, userNotes),
        ["ds_factor_review_js", "ds_memory_js"],
      );
      log.push(`${r.ok ? "✅" : "❌"} 退役专员·${dog} [${r.stopReason}] ${String(r.text || "").slice(0, 120).replace(/\n/g, " ")}`);
      progress({ phase: `退役专员·${dog}`, done: i + 1, total: ordered.length, status: r.ok ? "ok" : "fail" });
    }
  }

  return { ok: true, scope, end_date: end, reflect_day: reflectTarget, dogs: dogList, log, text: log.join("\n") };
}
