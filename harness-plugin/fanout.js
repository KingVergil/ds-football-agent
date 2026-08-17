/**
 * subagent fan-out（计划 D3）：并行分析 7 只单关狗。
 *
 * 每只狗 = 一个 spawn subagent（独立 session；记忆仍在共享 ds_* storage 域，按狗名键控）。
 * 父 agent 只做 fan-out 与汇总，不亲自逐狗分析。
 *
 * 依赖 host 已挂载的 ctx.subagents + spawn provider（dsh-base cordis.patch 提供，
 * providerName=spawn，capabilities 全开）。插件不注册自己的 provider。
 */
import { beijingNowIso } from "./settle.js";
import { DS_REAL_DOGS } from "./storage.js";
import { readPersona } from "./reflect.js";
import { jingcaiWindowMatches } from "./dataflow.js";

/** 北京时间足球日标签：HHMM < 12:00 用昨天，>= 12:00 用今天（与 batch_agents.sh 一致）。 */
export function footballDayLabel(now = new Date()) {
  const bj = new Date(now.getTime() + 8 * 3600 * 1000);
  const hhmm = bj.getUTCHours() * 100 + bj.getUTCMinutes();
  const d = new Date(Date.UTC(bj.getUTCFullYear(), bj.getUTCMonth(), bj.getUTCDate()));
  if (hhmm < 1200) d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
}

/**
 * 从本地缓存读某足球日比赛列表（strip scores，防后视），只读一次供 7 个子 agent 共用。
 * 统一走 dataflow 的足球日窗口 [D 12:01, D+1 12:00] + 竞彩过滤：
 *   排除 12:01 前的早场（上一足球日），纳入次日 12:00 前的跨天场次。
 */
export function readMatchesCache(cacheDir, day) {
  return jingcaiWindowMatches(cacheDir, day, { strip: true, jingcaiOnly: true });
}

/** 比赛列表 → 注入每个子 agent prompt 的紧凑文本。 */
export function matchesToPrompt(matches) {
  if (!matches.length) return "(当日无比赛)";
  return matches
    .map((m, i) => `${i + 1}. ${m.lota_id || "?"} | ${m.league_name || ""} | ${m.home_name || ""} vs ${m.away_name || ""} | ${m.match_time || ""} | 竞彩=${m.jingcai_number || "-"}`)
    .join("\n");
}

/** 子 agent 最终输出的文本（只取 text block）。 */
function outputText(output) {
  if (!Array.isArray(output)) return "";
  return output
    .filter((b) => b && typeof b === "object" && b.type === "text" && typeof b.text === "string")
    .map((b) => b.text)
    .join("\n");
}

/**
 * 组装一只狗的自包含分析 prompt（spawn 子 agent 看不到父对话；比赛列表已由工具预取注入）。
 * 人设直接拼进上下文（readPersona），不再要求子 agent 自己 read 文件。
 */
export function buildDogFanoutPrompt(dog, day, matchesText, { persona = "" } = {}) {
  return `你是 ds_agents 的「${dog}」专属分析员。现在只分析这一只狗，禁止分析或下单其他狗。

足球日 = ${day}（北京时间窗口 [${day} 12:01, 次日 12:00]）。

## 当日比赛列表（父任务已从缓存获取并 strip_scores 防后视，无需再调 lota_matches）
${matchesText || "(当日无比赛)"}

## 人设（已注入上下文，禁止再 read 文件）
${persona || "(无 persona.md，按通用框架执行)"}

严格按 ds-agents-analyze 工作流执行：
1. refresh_orders("${dog}", "${day}")
2. 从上面的比赛列表逐场读关键段落再选场：当日候选 <= 50 场时必须逐场读全，禁止只读少数几场；只有 > 50 场才允许先按联赛/时间粗筛。逐场 lota_sections(id, slugs=["fair-odds","asian-handicap-pinnacle","over-under-crown","betfair-buysell","discrete-odds"])。
3. 读记忆：ds_memory_js("${dog}", "${day}")；判断时结合活跃因子与已证伪模式（例如「离散极低」这类信号历史上可能是诱杀而非看好）。
4. 读角色数据：ds_persona_js("${dog}")（返回人设 + 日常比赛范围 + 资金现状 capital/full_capital/约束）；金额 = 信心比例 x full_capital。
5. 独立判断后 submit_orders("${dog}", "${day}", orders) 结构化下单。该狗行为准则：平局狗无干净信号就 0 注；均注狗每场必下；梭哈2/3狗必下 2-4 注；alpha 系凯利负期望就 skip；以上方人设为准。

禁止调用 ds_analyze_all_parallel / ds_prepare_day / 结算 / 因子 / 回放 / ds_capital_js 或 subagent 类工具（比赛列表已注入、资金在人设工具里；递归委派被系统拒绝）。lota_matches 仅在确需按类型复核时可用（默认竞彩）。

最后只输出一行 JSON（不要 markdown 代码块，不要多余文字）：
{"dog":"${dog}","day":"${day}","placed":0,"skipped":0,"capital":0,"orders":0,"summary":"一句话"}`;
}

/** 跑一只狗：start -> 等待 result -> dispose（对齐 dsh-tool-subagent 的前台收束）。 */
async function runOneDog(ctx, parent, signal, dog, day, matchesText, persona) {
  const prompt = buildDogFanoutPrompt(dog, day, matchesText, { persona });
  const run = await ctx.subagents.start("spawn", {
    label: `analyze ${dog}`,
    prompt: [{ type: "text", text: prompt }],
    parent,
    signal,
    maxDepth: 1, // 只允许这一层子 agent，禁止孙级递归
    toolFilter: { deny: [
      // 工具组可见性（docs/workflow_tool_groups.md §2.1）：分析子流只留 分析组+角色组+只读数据
      "subagent", "subagent_fork", "ds_analyze_all_parallel",
      "ds_prepare_day", "ds_migrate_storage", "ds_export_to_python",
      "ds_settle_js", "ds_reflect_js", "fetch_scores",
      "ds_factor_induction", "ds_factor_dedup", "ds_factor_review_js",
      "ds_replay",
      "ds_capital_js", // 资金并入角色数据（ds_persona_js 返回）
    ] },
  });
  try {
    const result = await run.result;
    return {
      dog,
      runId: String(run.id ?? ""),
      ok: result.stopReason === "completed",
      stopReason: result.stopReason,
      text: outputText(result.output).slice(0, 2000),
    };
  } finally {
    try { await run.dispose(); } catch {}
  }
}

/**
 * 并行分析多只狗（默认 7 只真狗）。返回汇总报告。
 * @param {object} ctx Cordis 上下文（已注入 subagents）
 * @param {object} opts { day, dogs, parallel, parent, signal }
 */
export async function analyzeDogsParallel(ctx, opts = {}) {
  if (!ctx || !ctx.subagents) {
    return { error: "ctx.subagents 不可用：host 未挂载 @deepseek-ai/dsh-subagent（或插件未注入 subagents）" };
  }
  if (!opts.parent) {
    return { error: "缺少 parent agent，无法启动 subagent" };
  }
  if (ctx.subagents.getProvider("spawn") === undefined) {
    return { error: "subagent provider 'spawn' 未注册：host 需挂载 @deepseek-ai/dsh-subagent-spawn-in-process" };
  }

  const signal = opts.signal ?? new AbortController().signal;
  const day = opts.day || footballDayLabel();
  const dogs = (opts.dogs && opts.dogs.length ? opts.dogs : DS_REAL_DOGS).slice();
  const parallel = Math.max(1, Math.min(Number(opts.parallel) || dogs.length, dogs.length || 1));
  const personas = opts.personas || {}; // dog → 人设文本覆盖（默认读 persona.md）
  const onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};

  // 1) 先获取比赛：只读一次本地缓存，strip_scores 后注入每个子 agent prompt（子 agent 不再各自 lota_matches）
  const matches = opts.cacheDir ? readMatchesCache(opts.cacheDir, day) : [];
  onProgress({ phase: "读取比赛缓存", idx: 0, total: dogs.length, detail: `竞彩 ${matches.length} 场` });
  const matchesText = matchesToPrompt(matches);
  if (opts.cacheDir && matches.length === 0) {
    return { ok: true, skipped: `当日(${day})无比赛缓存，不启动子 agent`, day, matches_count: 0 };
  }

  const results = new Array(dogs.length);
  let cursor = 0;

  const worker = async () => {
    while (cursor < dogs.length) {
      if (signal.aborted) return;
      const idx = cursor++;
      const dog = dogs[idx];
      try {
        const persona = personas[dog] || (opts.cacheDir ? readPersona(opts.cacheDir, dog) : "");
        results[idx] = await runOneDog(ctx, opts.parent, signal, dog, day, matchesText, persona);
        onProgress({
          idx: idx + 1,
          total: dogs.length,
          dog,
          status: results[idx] && results[idx].ok ? "ok" : "fail",
          phase: `并行分析 ${idx + 1}/${dogs.length}`,
        });
      } catch (error) {
        results[idx] = { dog, ok: false, stopReason: "start-failed", text: String(error && error.message || error) };
        onProgress({ idx: idx + 1, total: dogs.length, dog, status: "fail", phase: `并行分析 ${idx + 1}/${dogs.length}` });
      }
    }
  };

  const startedAt = beijingNowIso();
  await Promise.all(Array.from({ length: parallel }, () => worker()));

  const rows = results.map((r) => r || { dog: "?", ok: false, stopReason: "no-result", text: "" });
  const okCount = rows.filter((r) => r.ok).length;
  const failCount = rows.length - okCount;

  return {
    ok: failCount === 0,
    started_at: startedAt,
    finished_at: beijingNowIso(),
    day,
    matches_count: matches.length,
    parallel,
    dogs: rows.map((r) => r.dog),
    ok_count: okCount,
    fail_count: failCount,
    rows,
    text: rows.map((r) => `${r.ok ? "OK " : "FAIL "} ${r.dog} [${r.stopReason}] ${r.text.slice(0, 300).replace(/\n/g, " ")}`).join("\n"),
  };
}
