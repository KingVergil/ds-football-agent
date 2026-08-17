/**
 * factor_review 纯 JS（harness_js_reconstruction.md §4）。
 * 分两半：
 *   1) 代码门控（无 LLM）：14天零触发→dormant；低信息退役（样本≥5 且 |均回报|<0.15 且命中率 0.35~0.65）→retired
 *   2) 旁路 ctx.llm.stream 结构性评估 → {retire, dormant, rationale} → 清洗因子名 → set_status
 * 对齐 python-engine/src/agent.py::node_factor_review。
 */
import { beijingNowIso } from "./settle.js";
import { streamReflectJson, parseReflectJson, readPersona } from "./reflect.js";

const LOW_INFO_MIN_SAMPLES = 5;
const LOW_INFO_AVG_RETURN = 0.15;
const LOW_INFO_HIT_LO = 0.35;
const LOW_INFO_HIT_HI = 0.65;

function daysSince(dateStr, refStr) {
  try {
    const d = new Date(`${String(dateStr).slice(0, 10)}T00:00:00Z`);
    const r = new Date(`${String(refStr).slice(0, 10)}T00:00:00Z`);
    return Math.round((r - d) / 86400000);
  } catch {
    return 0;
  }
}

function sanitizeReflection(text) {
  let t = String(text || "");
  for (const icon of ["✅", "❌", "➖", "🔴", "⚫", "⬜"]) t = t.split(icon).join("");
  return t.replace(/⇒\s*\S+/g, "");
}

/** 代码门控：14天零触发→dormant + 低信息→retired（原地改 status，返回名单）。 */
export function applyFactorGates(factorPerf, weekEnd) {
  const autoDormant = [];
  const lowInfoRetired = [];
  for (const [fid, s] of Object.entries(factorPerf)) {
    if (s.status === "retired") continue;
    if (daysSince(s.last_seen || "", weekEnd) > 14 && (s.total || 0) > 0) {
      s.status = "dormant";
      autoDormant.push(fid);
      continue;
    }
    const hist = s.history || [];
    if (hist.length < LOW_INFO_MIN_SAMPLES) continue;
    const rets = hist.map((h) => h.return_ratio || 0);
    const avg = rets.reduce((a, b) => a + b, 0) / rets.length;
    const denom = hist.length - (s.push || 0);
    const hitRate = denom > 0 ? (s.hit || 0) / denom : 0;
    if (Math.abs(avg) < LOW_INFO_AVG_RETURN && hitRate >= LOW_INFO_HIT_LO && hitRate <= LOW_INFO_HIT_HI) {
      s.status = "retired";
      lowInfoRetired.push(fid);
    }
  }
  return { autoDormant, lowInfoRetired };
}

/** 构建评估候选（active + dormant，排除 retired）。 */
export function buildCandidates(factorPerf) {
  const out = [];
  for (const [fid, s] of Object.entries(factorPerf)) {
    if (s.status === "retired") continue;
    const total = s.total || 0;
    const denom = total - (s.push || 0);
    const hitRateStr = denom > 0 ? `${Math.round(((s.hit || 0) / denom) * 100)}%` : "无数据";
    out.push({
      fid,
      status: s.status || "active",
      total,
      hit_rate: hitRateStr,
      profit: s.profit || 0,
      desc: s.desc || "",
      first_seen: s.first_seen || "",
      last_seen: s.last_seen || "",
    });
  }
  return out;
}

export function buildReviewPrompt({ persona, candidates, reflectionsText, windowDesc, userNotes = "" }) {
  const candidatesText = candidates
    .sort((a, b) => b.total - a.total)
    .map(
      (c) =>
        `  ${c.fid} [${c.status}]: ${c.total}次 命中${c.hit_rate} 盈亏${c.profit >= 0 ? "+" : ""}${c.profit.toFixed(0)} | 首见=${c.first_seen} 最近=${c.last_seen}\n    定义: ${c.desc.slice(0, 100) || "(无描述)"}`,
    )
    .join("\n");

  return `你是量化足球博彩分析师，负责审查因子库健康度。

## 投注人设
${persona || "(未设)"}

## 反思记录（评估窗口: ${windowDesc}）
${reflectionsText || "(窗口内无反思记录)"}

## 用户调整意见（回放/人工干预时注入，优先参考）
${userNotes || "(无)"}

## 待评估因子列表
${candidatesText}

## 评估原则
你的任务不是评估因子"赢了几次"，而是判断因子的市场假设是否还成立。
对每个因子依次考量：核心假设是什么？近7天反思中是否被反复证伪？定价低效是否已被市场修正？是否已被更精细因子完全替代？
结论三档：retire（假设证伪/市场修正/被替代）、dormant（逻辑可能有效但近期无触发）、active（保留，不需列出）。
⚠️ 保守原则：宁可多保留，不要误删。

输出格式 — 必须输出合法 JSON（不要 markdown、不要多余文字）：
⚠️ 因子名硬约束：retire/dormant 里的每个名字必须与上方待评估列表逐字一致，禁止加图标/后缀，不匹配的名字会被忽略。
{"retire":["因子A"],"dormant":["因子C"],"rationale":{"因子A":"理由≤40字"}}`;
}

/** 清洗 LLM 输出的因子名，返回匹配的真实 key 或 null。 */
export function cleanFactorName(raw, fpKeys) {
  let s = String(raw || "").trim();
  if (["", "无", "none", "-"].includes(s)) return null;
  if (fpKeys.has(s)) return s;
  let c = s;
  for (const icon of ["✅", "❌", "➖", "🔴", "⚫", "⬜"]) c = c.split(icon).join("");
  c = c.replace(/⇒\s*\S+/g, "").trim();
  if (c && fpKeys.has(c)) return c;
  const c2 = c.replace(/[\s（(][^)）]*$/, "").trim();
  if (c2 && fpKeys.has(c2)) return c2;
  return null;
}

/**
 * 完整 factor_review：门控 + 旁路 LLM 评估 + set_status 写回。
 * @param {object} opts { userNotes, persona } — 用户调整意见注入评估 prompt；persona 覆盖默认读取。
 */
export async function factorReview(handles, ctx, dog, weekEnd, startDate, cacheDir, opts = {}) {
  const [factorsDomain, reflectionsDomain] = await Promise.all([
    handles["ds_factors"], handles["ds_reflections"],
  ]);
  const factorsTable = factorsDomain.table("factors");
  const reflectionsTable = reflectionsDomain.table("reflections");

  const rec = factorsTable.get(dog);
  const fp = (rec && rec.factor_perf) || {};
  if (!Object.keys(fp).length) return { ok: true, skipped: "无因子数据" };

  // 1. 代码门控
  const { autoDormant, lowInfoRetired } = applyFactorGates(fp, weekEnd);

  // 2. 候选
  const candidates = buildCandidates(fp);
  if (!candidates.length) {
    await factorsTable.put(dog, { ...rec, factor_perf: fp, updated_at: beijingNowIso() });
    return { ok: true, auto_dormant: autoDormant, low_info_retired: lowInfoRetired, candidates: 0 };
  }

  // 3. 近期反思
  const refRec = reflectionsTable.get(dog);
  const reflections = (refRec && refRec.reflections) || [];
  const recentRefs = reflections.filter((r) => daysSince(r.date || "", weekEnd) <= 7).slice(-10);
  const windowDesc = startDate ? `${startDate} ~ ${weekEnd}` : `近7天 (~${weekEnd})`;
  const reflectionsText = recentRefs.length
    ? recentRefs.map((r) => `  [${r.date || "?"}] ${sanitizeReflection((r.reflection || "").slice(0, 300))}`).join("\n")
    : "";

  const persona = opts.persona || readPersona(cacheDir, dog);
  const prompt = buildReviewPrompt({
    persona, candidates, reflectionsText, windowDesc,
    userNotes: opts.userNotes || "",
  });

  // 4. 旁路 LLM 评估
  let data = null;
  try {
    data = parseReflectJson(await streamReflectJson(ctx, prompt));
  } catch (e) {
    return { ok: false, error: `factor_review LLM 失败: ${e.message}` };
  }

  // 5. set_status
  const fpKeys = new Set(Object.keys(fp));
  const actuallyRetired = [];
  const llmDormant = [];
  if (data) {
    for (const fn of data.retire || []) {
      const matched = cleanFactorName(fn, fpKeys);
      if (matched) {
        fp[matched].status = "retired";
        actuallyRetired.push(matched);
      }
    }
    for (const fn of data.dormant || []) {
      const matched = cleanFactorName(fn, fpKeys);
      if (matched && !autoDormant.includes(matched)) {
        fp[matched].status = "dormant";
        llmDormant.push(matched);
      }
    }
  }

  await factorsTable.put(dog, { ...rec, factor_perf: fp, updated_at: beijingNowIso() });

  return {
    ok: true, user: dog, week_end: weekEnd,
    candidates: candidates.length,
    auto_dormant: autoDormant,
    low_info_retired: lowInfoRetired,
    retired: actuallyRetired,
    dormant: [...autoDormant, ...llmDormant],
    llm_dormant: llmDormant,
  };
}
