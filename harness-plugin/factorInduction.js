/**
 * factor_induction 纯 JS（harness_js_reconstruction.md §5 旁路 LLM 判重）。
 * 核心：因子判重（create/merge/suppress）——LLM 用 fast 模型(deepseek-v4-flash, temp 0) +
 * 确定性兜底（retired 因子名称≥0.8 且描述相似≥0.25 → suppress）。
 * 对齐 python-engine/src/memory.py::_consolidate_candidate。
 */
import { createUserMessage, BlockAssembler, deepFreeze } from "@deepseek-ai/dsh-llm";

import { LLM_TEMPERATURES } from "./tools/shared.js";

const FAST_MODEL = "deepseek-v4-flash";

/** 名称/描述相似度（LCS 版，≈ Python difflib.SequenceMatcher.ratio = 2*M/(len_a+len_b)）。
 *  滚动两行 DP，内存 O(min(m,n)) —— 原实现为 (m+1)×(n+1) 全矩阵，
 *  对几百字中文描述×数百因子判重时会撑爆堆（OOM）。 */
export function similarityRatio(a, b) {
  const s = String(a || "");
  const t = String(b || "");
  const m = s.length;
  const n = t.length;
  if (!m || !n) return 0;
  // 让 x 为较短串，缩小行宽
  const [x, y] = m <= n ? [s, t] : [t, s];
  let prev = new Array(y.length + 1).fill(0);
  let cur = new Array(y.length + 1).fill(0);
  for (let i = 1; i <= x.length; i++) {
    for (let j = 1; j <= y.length; j++) {
      cur[j] = x[i - 1] === y[j - 1]
        ? prev[j - 1] + 1
        : Math.max(prev[j], cur[j - 1]);
    }
    [prev, cur] = [cur, prev];
    cur.fill(0);
  }
  return (2 * prev[y.length]) / (m + n);
}

/** 字符二元组 Jaccard（对齐 memory.py::_desc_bigram_jaccard）。 */
export function bigramJaccard(a, b) {
  const bigrams = (t) => {
    const s = String(t || "").replace(/\s+/g, "");
    const set = new Set();
    for (let k = 0; k < s.length - 1; k++) set.add(s.slice(k, k + 2));
    return set;
  };
  const A = bigrams(a);
  const B = bigrams(b);
  const inter = [...A].filter((x) => B.has(x)).length;
  const union = A.size + B.size - inter;
  return union ? inter / union : 0;
}

/** 确定性兜底：retired 近亲（名称≥0.8 且 desc 相似≥0.25）→ suppress。 */
function deterministicSuppress(factorId, desc, factorPerf) {
  let best = null;
  let bestScore = 0;
  for (const [n, v] of Object.entries(factorPerf)) {
    if (v.status !== "retired") continue;
    const nr = similarityRatio(factorId, n);
    if (nr < 0.8) continue;
    const dv = Math.max(bigramJaccard(desc, v.desc || ""), similarityRatio(desc, v.desc || ""));
    if (dv < 0.25) continue;
    const score = 0.6 * nr + 0.4 * dv;
    if (score > bestScore) {
      best = n;
      bestScore = score;
    }
  }
  return best;
}

/**
 * LLM 判重 + 确定性兜底。
 * @returns { action: 'create'|'merge'|'suppress', target: string|null, reason: string }
 */
export async function judgeFactorDedup(ctx, factorId, desc, factorPerf, { provider = "deepseek-official" } = {}) {
  const names = Object.keys(factorPerf);
  if (!names.length) return { action: "create", target: null, reason: "因子库为空" };

  const scored = names
    .map((n) => ({ n, s: similarityRatio(factorId, n) }))
    .filter((x) => x.s >= 0.45)
    .sort((a, b) => b.s - a.s)
    .slice(0, 15);
  if (!scored.length) return { action: "create", target: null, reason: "无相似因子" };

  const libLines = scored
    .map((x, i) => {
      const v = factorPerf[x.n];
      return `${i + 1}. ${x.n} [状态:${v.status || "active"}] | ${(v.desc || "").slice(0, 200)} (样本${v.total || 0} 盈亏${v.profit ?? 0})`;
    })
    .join("\n");

  const system = "你是足球因子库管理员。判断候选因子是否与现有因子重复。规则：\n1. 同模式不同表述→merge，target 填最匹配的现有因子名；\n2. 方向相反（上盘vs下盘/让球方vs受让方/阻上vs诱上/追强vs防冷）→绝不合并，create；\n3. 与 retired 因子高度一致→suppress；\n4. 与现有因子样本都充足且盈亏方向相反→create；\n5. 全新模式→create。\n用语义判断，不要只看字面。只输出严格 JSON，不要多余文字。";
  const user = `候选因子: ${factorId} | ${desc.slice(0, 200)}\n\n现有因子(共${scored.length}个):\n${libLines}\n\n输出严格 JSON: {"action":"merge|create|suppress","target":"现有因子名或null","reason":"一句话"}`;

  let verdict = null;
  try {
    const messages = [createUserMessage({
      content: [{ type: "text", text: user }],
      source: { kind: "plugin", plugin: "ds-agents-lota-data" },
    })];
    const options = deepFreeze({
      provider, model: FAST_MODEL, messages, system, temperature: LLM_TEMPERATURES.induction, maxTokens: 500,
    });
    const assembler = new BlockAssembler();
    for await (const chunk of ctx.llm.stream(options)) {
      assembler.push(chunk);
    }
    const text = assembler.blocks().filter((b) => b.type === "text").map((b) => b.text).join("");
    const clean = text.replace(/\[thinking\][\s\S]*?\[\/thinking\]/g, "").trim();
    const start = clean.indexOf("{");
    const end = clean.lastIndexOf("}");
    if (start !== -1 && end !== -1) verdict = JSON.parse(clean.slice(start, end + 1));
  } catch {
    verdict = null;
  }

  let action = verdict && verdict.action ? verdict.action : "create";
  let target = verdict && verdict.target ? verdict.target : null;
  let reason = verdict && verdict.reason ? verdict.reason : "LLM调用失败";

  // 确定性兜底：判 create 或失败时，若命中 retired 近亲 → suppress
  if (action === "create") {
    const det = deterministicSuppress(factorId, desc, factorPerf);
    if (det) {
      action = "suppress";
      target = det;
      reason = `确定性兜底(desc一致+retired): ${reason}`;
    }
  }

  if (action === "suppress" && !(target in factorPerf)) {
    action = "create";
    target = null;
  }
  if (action === "merge" && !(target in factorPerf)) {
    action = "create";
    target = null;
  }

  return { action, target, reason };
}

// ═══════════════════════════════════════════════════════════════
// factor_induction（阶段3 批量归纳去重）—— 对齐 python-engine/src/factor_induction.py
// 每日 settle 后跑：候选对发现（同清洗名 / bit 距离 / 孤儿名字）→ 确定性合并 + LLM 判重 → 写回。
// ═══════════════════════════════════════════════════════════════

const BIT_DIST_MAX = 2;
const NAME_RATIO_MIN = 0.60;
const BIT_NAME_FLOOR = 0.35;
const PER_FACTOR_PAIR_CAP = 3;

const INDUCTION_JUDGE_SYSTEM = `你是足球因子库管理员。判断两个因子是否为同一模式。
规则：
1. 语义重复（同一模式的不同表述/同义改写）→ merge=true，keep 填样本更多、描述更全的一方
2. 方向相反（上盘vs下盘、让球方vs受让方、追强vs防冷、诱上vs阻上）→ merge=false
3. 两者样本都充足且盈亏方向相反 → 视为经验上不同模式，merge=false
4. 仅名称/描述部分相似但模式不同 → merge=false
只输出严格 JSON，不要多余文字。`;

/** 复刻 factor_induction.py::clean_name（去引号/括号/emoji + 空白归一化）。 */
export function cleanNameInduct(name) {
  let n = String(name || "").trim();
  n = n.replace(/["'“”`]/g, "");
  n = n.replace(/[（(][^）)]*[）)]/g, "").trim();
  n = n.replace(/[\u{1F000}-\u{1FAFF}\u2600-\u27BF\u2B00-\u2BFF\uFE0F]/gu, "").trim();
  n = n.replace(/\s+/g, " ");
  return n;
}

function entrySlugs(entry) {
  const s = entry?.slugs || [];
  return Array.isArray(s) ? s : [];
}

function bitDistance(sa, sb) {
  const a = new Set(sa);
  const b = new Set(sb);
  let d = 0;
  for (const x of a) if (!b.has(x)) d += 1;
  for (const x of b) if (!a.has(x)) d += 1;
  return d;
}

/** 复刻 factor_induction.py::recompute_stats（从 history 重算）。 */
function recomputeStats(entry) {
  const hist = entry.history || [];
  const total = hist.length;
  const hit = hist.reduce((s, h) => s + (h.hit === true ? 1 : h.hit === 0.5 ? 0.5 : 0), 0);
  const miss = hist.reduce((s, h) => s + (h.hit === false ? 1 : h.hit === -0.5 ? 0.5 : 0), 0);
  const push = total - hit - miss;
  const profit = hist.reduce((s, h) => s + (Number(h.profit) || 0), 0);
  const ret = hist.reduce((s, h) => s + (Number(h.return_ratio) || 0), 0);
  const dates = hist.map((h) => h.date).filter(Boolean).sort();
  entry.total = total;
  entry.hit = hit;
  entry.miss = miss;
  entry.push = push;
  entry.profit = Math.round(profit * 100) / 100;
  entry.total_return = Math.round(ret * 10000) / 10000;
  if (dates.length) {
    entry.first_seen = dates[0];
    entry.last_seen = dates[dates.length - 1];
  }
  return entry;
}

/** 复刻 factor_induction.py::merge_entries。 */
function mergeEntries(target, source, sourceName) {
  const th = (target.history || []).slice();
  th.push(...(source.history || []));
  th.sort((a, b) => (a.date || "").localeCompare(b.date || ""));
  target.history = th;
  recomputeStats(target);
  const targetName = target._name || "";
  const aliases = [...new Set([
    ...(target.aliases || []),
    ...(sourceName !== targetName ? [sourceName] : []),
  ])];
  target.aliases = aliases;
  if (!target.fac_id) target.fac_id = source.fac_id;
  if (!entrySlugs(target).length && entrySlugs(source).length) target.slugs = source.slugs;
  return target;
}

/** 复刻 factor_induction.py::find_candidates → [(a,b,kind)]。 */
function findCandidates(entries) {
  // 1) 同清洗名（确定性合并，不调 LLM）
  const byClean = {};
  for (const name of Object.keys(entries)) {
    const c = cleanNameInduct(name);
    (byClean[c] ||= []).push(name);
  }
  const same = [];
  for (const names of Object.values(byClean)) {
    for (let i = 0; i < names.length; i++) {
      for (let j = i + 1; j < names.length; j++) {
        same.push([names[i], names[j], "same_name"]);
      }
    }
  }

  // 2) bit 距离 ≤2 + 名字相似度 ≥ 下限，每因子 top 3
  const names = Object.keys(entries);
  const bitScores = [];
  for (let i = 0; i < names.length; i++) {
    const sa = entrySlugs(entries[names[i]]);
    if (!sa.length) continue;
    for (let j = i + 1; j < names.length; j++) {
      const sb = entrySlugs(entries[names[j]]);
      if (!sb.length) continue;
      const dist = bitDistance(sa, sb);
      if (dist <= BIT_DIST_MAX) {
        const ratio = similarityRatio(names[i], names[j]);
        if (ratio >= BIT_NAME_FLOOR) bitScores.push([names[i], names[j], "bit", ratio]);
      }
    }
  }
  bitScores.sort((a, b) => b[3] - a[3]);
  const perFactor = {};
  const bitPairs = [];
  for (const [a, b, kind] of bitScores) {
    if ((perFactor[a] || 0) >= PER_FACTOR_PAIR_CAP || (perFactor[b] || 0) >= PER_FACTOR_PAIR_CAP) continue;
    bitPairs.push([a, b, kind]);
    perFactor[a] = (perFactor[a] || 0) + 1;
    perFactor[b] = (perFactor[b] || 0) + 1;
  }

  // 3) 孤儿（无 slugs）名字相似度
  const namePairs = [];
  for (let i = 0; i < names.length; i++) {
    if (entrySlugs(entries[names[i]]).length) continue;
    for (let j = i + 1; j < names.length; j++) {
      if (entrySlugs(entries[names[j]]).length) continue;
      if (similarityRatio(names[i], names[j]) >= NAME_RATIO_MIN) {
        namePairs.push([names[i], names[j], "name"]);
      }
    }
  }

  return [...same, ...bitPairs, ...namePairs];
}

/** 复刻 factor_induction.py::llm_judge_pair（fast 模型两两判重）。 */
async function llmJudgePair(ctx, aName, aEntry, bName, bEntry) {
  const line = (name, e) => {
    const hitRate = ((e.hit || 0) / Math.max((e.total || 0) - (e.push || 0), 1)) * 100;
    return `${name} | 描述: ${(e.desc || "").slice(0, 120)} | ` +
      `slugs: ${entrySlugs(e).slice(0, 5).join(", ")} | ` +
      `样本${e.total || 0} 盈亏${(e.profit || 0).toFixed(0)} 命中率${hitRate.toFixed(0)}%`;
  };
  const user = `因子A:\n${line(aName, aEntry)}\n\n因子B:\n${line(bName, bEntry)}\n\n` +
    `输出 JSON: {"merge": true|false, "keep": "A|B或null", "reason": "一句话"}`;

  try {
    const messages = [createUserMessage({
      content: [{ type: "text", text: user }],
      source: { kind: "plugin", plugin: "ds-agents-lota-data" },
    })];
    const options = deepFreeze({
      provider: "deepseek-official", model: FAST_MODEL, messages,
      system: INDUCTION_JUDGE_SYSTEM, temperature: LLM_TEMPERATURES.induction, maxTokens: 500,
    });
    const assembler = new BlockAssembler();
    for await (const chunk of ctx.llm.stream(options)) {
      assembler.push(chunk);
    }
    const text = assembler.blocks().filter((b) => b.type === "text").map((b) => b.text).join("");
    const clean = text.replace(/\[thinking\][\s\S]*?\[\/thinking\]/g, "").trim();
    const start = clean.indexOf("{");
    const end = clean.lastIndexOf("}");
    if (start === -1 || end === -1) return { merge: false, keep: null, reason: "LLM 输出无法解析" };
    return JSON.parse(clean.slice(start, end + 1));
  } catch (e) {
    return { merge: false, keep: null, reason: `LLM 调用失败: ${(e && e.message) || e}` };
  }
}

/** 复刻 factor_induction.py::induct_scope 的核心（对 entries 原地归纳，entries 值带 _name）。 */
async function runInduction(ctx, entries, { dryRun = false, limit = 30, scope = "" } = {}) {
  const result = { merged: 0, llm_calls: 0, skipped: [], kept: [] };
  const candidates = findCandidates(entries);

  // 1) 同清洗名确定性合并（不调 LLM）
  for (const [a, b, kind] of candidates) {
    if (kind !== "same_name") continue;
    if (!(a in entries) || !(b in entries)) continue;
    const ea = entries[a];
    const eb = entries[b];
    const keep = (ea.total || 0) >= (eb.total || 0) ? a : b;
    const drop = keep === a ? b : a;
    if (!dryRun) {
      mergeEntries(entries[keep], entries[drop], drop);
      delete entries[drop];
    }
    result.merged += 1;
    result.kept.push({ source: drop, target: keep, kind: "same_name", reason: "同清洗名" });
  }

  // 2) bit / name LLM 判重
  let judged = 0;
  for (const [a, b, kind] of candidates) {
    if (kind === "same_name") continue;
    if (!(a in entries) || !(b in entries)) continue;
    if (judged >= limit) {
      result.skipped.push({ a, b, kind, reason: "超出 limit" });
      continue;
    }
    const ea = entries[a];
    const eb = entries[b];
    if (dryRun) {
      judged += 1;
      result.skipped.push({ a, b, kind });
      continue;
    }
    const verdict = await llmJudgePair(ctx, a, ea, b, eb);
    judged += 1;
    result.llm_calls += 1;
    if (!verdict.merge) {
      result.skipped.push({ a, b, kind, reason: verdict.reason });
      continue;
    }
    const keep = verdict.keep === "A" ? a
      : verdict.keep === "B" ? b
      : (ea.total || 0) >= (eb.total || 0) ? a : b;
    const drop = keep === a ? b : a;
    mergeEntries(entries[keep], entries[drop], drop);
    delete entries[drop];
    result.merged += 1;
    result.kept.push({ source: drop, target: keep, kind, reason: verdict.reason });
  }

  return { result, entries };
}

function stripUnderscore(entries) {
  const cleaned = {};
  for (const [name, e] of Object.entries(entries)) {
    const { _name, ...rest } = e;
    cleaned[name] = rest;
  }
  return cleaned;
}

/**
 * 复刻 factor_induction.py::induct_scope（单狗各自归纳）。
 * @param {object} factorPerf  该狗的 factor_perf
 * @returns { result, factorPerf, scope }
 */
export async function inductFactors(ctx, factorPerf, { dryRun = false, limit = 30, scope = "" } = {}) {
  const entries = {};
  for (const [name, e] of Object.entries(factorPerf || {})) {
    entries[name] = { ...e, _name: name };
  }
  const { result, entries: merged } = await runInduction(ctx, entries, { dryRun, limit, scope });
  return { result, factorPerf: stripUnderscore(merged), scope };
}

/** alpha 池：跨狗统一归纳（factor_induction.py::main 的 alpha scope）。 */
export const ALPHA_DOGS = ["alpha2狗", "alpha狗", "均注狗"];

/**
 * alpha 跨狗归纳：3 只 alpha 狗的同清洗名因子先确定性跨角色合并（保留样本最多者），
 * 统一池后再跑 runInduction（bit/name LLM 判重合并），最后按归属角色写回各狗。
 * @param {object} handles  name → Promise<Domain>
 * @returns { result, perDog: {狗名: factorPerf}, crossMerged: [...] }
 */
export async function inductAlpha(ctx, handles, { dryRun = false, limit = 30 } = {}) {
  const domain = await handles["ds_factors"];
  const table = domain.table("factors");

  // 读 3 只 alpha 狗快照
  const perDog = {};
  const recs = {};
  for (const dog of ALPHA_DOGS) {
    recs[dog] = table.get(dog);
    perDog[dog] = { ...((recs[dog] && recs[dog].factor_perf) || {}) };
  }

  // 1) 跨狗同清洗名：确定性合并（保留样本最多者），构造统一池 + 归属
  const entries = {};   // name → {..., _name}
  const roleOf = {};    // name → dog
  const crossMerged = [];
  const groups = {};    // clean_name → [[dog, name, entry], ...]
  for (const dog of ALPHA_DOGS) {
    for (const [name, entry] of Object.entries(perDog[dog])) {
      const c = cleanNameInduct(name);
      (groups[c] ||= []).push([dog, name, entry]);
    }
  }
  for (const [cname, items] of Object.entries(groups)) {
    if (items.length === 1) {
      const [dog, name, entry] = items[0];
      entries[name] = { ...entry, _name: name };
      roleOf[name] = dog;
      continue;
    }
    let bdog = items[0][0];
    let bname = items[0][1];
    let bentry = items[0][2];
    for (const [dog, name, entry] of items) {
      if ((entry.total || 0) > (bentry.total || 0)) {
        bdog = dog;
        bname = name;
        bentry = entry;
      }
    }
    const keeper = { ...bentry, _name: bname };
    entries[bname] = keeper;
    roleOf[bname] = bdog;
    for (const [dog, name, entry] of items) {
      if (dog === bdog && name === bname) continue;
      if (!dryRun) mergeEntries(keeper, entry, name);
      crossMerged.push({ source: name, source_role: dog, target: bname, target_role: bdog });
    }
  }

  // 2) 统一池归纳
  const { result, entries: merged } = await runInduction(ctx, entries, { dryRun, limit, scope: "alpha" });

  // 3) 按归属角色拆分写回
  const out = {};
  for (const dog of ALPHA_DOGS) out[dog] = {};
  for (const [name, e] of Object.entries(merged)) {
    const dog = roleOf[name];
    if (dog && out[dog] !== undefined) out[dog][name] = e;
  }

  if (!dryRun) {
    for (const dog of ALPHA_DOGS) {
      await table.put(dog, {
        ...(recs[dog] || { factor_perf: {} }),
        factor_perf: stripUnderscore(out[dog] ? { ...out[dog] } : {}),
        updated_at: new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 19),
      });
    }
  }

  return {
    scope: "alpha",
    dogs: ALPHA_DOGS,
    cross_merged: crossMerged,
    factor_count_before: ALPHA_DOGS.reduce((s, d) => s + Object.keys(perDog[d]).length, 0),
    factor_count_after: Object.keys(merged).length,
    ...result,
  };
}
