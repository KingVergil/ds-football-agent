/**
 * reflect 旁路 LLM + 确定性写回（harness_js_reconstruction.md §3.4）。
 *
 * 分工：
 *   - buildReflectPrompt  从 storage/缓存组装反思 prompt（人设 + 结算单 + 已有因子 + 任务）
 *   - streamReflectJson   旁路 ctx.llm.stream（温度 0.3）→ 文本 JSON
 *   - applyReflection     确定性写回：因子归因 → ds_factors、新因子 → ds_factor_registry、反思 → ds_reflections
 *
 * 对齐 python-engine/src/agent.py::run_reflect 的写回尾部 + src/memory.py::FactorMemory.record。
 */
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { createUserMessage, BlockAssembler, deepFreeze } from "@deepseek-ai/dsh-llm";

import { beijingNowIso } from "./settle.js";

export const REFLECT_DEFAULT = { provider: "deepseek-official", model: "deepseek-v4-pro" };

/** 数据段 slug 白名单（对齐 agent.py SECTION_SLUG_WHITELIST）。 */
export const SLUG_WHITELIST = [
  "asian-handicap-crown", "asian-handicap-macau", "asian-handicap-pinnacle",
  "away-recent", "betfair-buysell", "betfair-eu", "discrete-odds", "eu-odds-pinnacle",
  "fair-odds", "goal-bonus", "home-recent", "lineup", "match-head", "match-history",
  "over-under-crown", "over-under-macau", "rank-info", "score-bonus",
];

function readText(path) {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return "";
  }
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

export function facIdFor(name) {
  return `fac_${String(name).toLowerCase().replace(/\s+/g, "_").slice(0, 40)}`;
}

export function cleanName(name) {
  let n = String(name || "").trim();
  n = n.replace(/["'“”`]/g, "");
  n = n.replace(/[（(][^）)]*[）)]/g, "").trim();
  n = n.replace(/[\u{1F000}-\u{1FAFF}\u2600-\u27BF\u2B00-\u2BFF\uFE0F]/gu, "").trim();
  return n;
}

export function isValidName(name) {
  const n = cleanName(name);
  if (!n || ["无", "none", "null", "n/a", "-", "无新因子"].includes(n.toLowerCase())) return false;
  if (n.includes(":") || n.toLowerCase().startsWith("key_")) return false;
  return true;
}

/** FactorMemory.record 的 JS 移植（原地更新 factorPerf）。 */
export function recordFactor(factorPerf, factorId, hit, profit, { desc = "", date = "", lotaId = "", betSize = 0 } = {}) {
  const returnRatio = betSize > 0 ? profit / betSize : 0;
  let effHit;
  if (hit == null && profit > 0) effHit = 0.5;
  else if (hit == null && profit < 0) effHit = -0.5;
  else effHit = hit;

  let p = factorPerf[factorId];
  if (!p) {
    p = factorPerf[factorId] = {
      total: 0, hit: 0, miss: 0, push: 0, profit: 0, total_return: 0,
      status: "active", desc, first_seen: date, last_seen: date,
      history: [], aliases: [], fac_id: facIdFor(factorId),
    };
  }
  p.total += 1;
  if (effHit === true) p.hit += 1;
  else if (effHit === false) p.miss += 1;
  else if (effHit === 0.5) p.hit += 0.5;
  else if (effHit === -0.5) p.miss += 0.5;
  else p.push += 1;
  p.profit = (p.profit || 0) + profit;
  p.total_return = (p.total_return || 0) + returnRatio;
  if (desc) p.desc = desc;
  if (date) {
    if (!p.first_seen) p.first_seen = date;
    p.last_seen = date;
    p.history = p.history || [];
    p.history.push({ date, hit: effHit, profit, return_ratio: returnRatio, lota_id: lotaId });
  }
}

/** 组装反思 prompt（简化版 run_reflect：人设 + 结算单 + 已有因子 + 任务）。 */
export function buildReflectPrompt({ persona, settled, existingSummary, factorDescText, keySlugWhitelist }) {
  const settledText = settled
    .map((o, i) => {
      const icon = o.profit > 0 ? "✅" : o.profit < 0 ? "❌" : "➖";
      return `### lota_id=${o.lota_id} | 比分=${o.score}\n  order_${i}: ${icon} ${o.bet_type || ""} ${o.pick || ""} @${o.odds ?? 0} bet${o.bet_size ?? 0} → ${o.profit ?? 0}\n    理由: ${(o.reason || "").slice(0, 200)}`;
    })
    .join("\n\n---\n\n");

  return `你是量化足球博彩分析师。你的任务是从已结算比赛中**发现可复用的投注因子**。

## 🎯 投注人设（所有下单基于此人设）
${persona || "(未设)"}

## 已结算投注
${settledText || "(无)"}

## 当前已有因子（仅名字，统计不重要）
${existingSummary || "(空)"}
${factorDescText || ""}

## 任务 — 案例驱动的因子发现
**Step 1 — 跨场对比**：赢的场次之间有什么共同信号模式？输的是否触发同样信号（=失效）还是不同信号（=没覆盖）？单场孤例不构成因子，必须能解释至少 2-3 场共性。
**Step 2 — 模式抽象**：把能解释多个结果的信号提炼为因子；孤立事件只在 per_match 标注。
**Step 3 — 命名**：因子名 ≤ 12 字、禁写数值、用方向词（碾压/极端低位/大幅收紧）。
**Step 4 — 资金管理反思**：哪些注该大哪些该小？仓位和置信度匹配吗？写 money_lesson ≤80字。
**Step 5 — slugs**：key_slugs 从白名单选：${(keySlugWhitelist || []).join(", ")}。

输出格式 — 必须输出合法 JSON（不要 markdown、不要多余文字）：
{"per_match":{"order_0":"..."},"alpha_factors":["因子名≤12字"],"key_slugs":["discrete-odds"],"noise_slugs":["match-head"],"factor_desc":{"因子名":"一句话描述"},"factor_attribution":{"order_0":["因子名"]},"money_lesson":"≤80字","reflection":"≤200字"}`;
}

/** 旁路 ctx.llm.stream → 文本 JSON。 */
export async function streamReflectJson(ctx, prompt, { provider, model } = {}) {
  const p = provider || REFLECT_DEFAULT.provider;
  const m = model || REFLECT_DEFAULT.model;
  const messages = [createUserMessage({
    content: [{ type: "text", text: "按 JSON 格式输出。" }],
    source: { kind: "plugin", plugin: "ds-agents-lota-data" },
  })];
  const options = deepFreeze({
    provider: p,
    model: m,
    messages,
    system: prompt,
    temperature: 0.3,
    maxTokens: 20000,
  });
  const assembler = new BlockAssembler();
  for await (const chunk of ctx.llm.stream(options)) {
    assembler.push(chunk);
  }
  const finish = assembler.finish;
  if (finish && (finish.kind === "error" || finish.kind === "aborted")) {
    throw new Error(`reflect stream ${finish.kind}`);
  }
  return assembler.blocks()
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
}

/** 旁路 ctx.llm.stream → 纯文本（自由文本，不强制 JSON；用于因子方向建议等）。 */
export async function streamText(ctx, prompt, { provider, model, maxTokens = 1200, temperature = 0.4 } = {}) {
  const p = provider || REFLECT_DEFAULT.provider;
  const m = model || REFLECT_DEFAULT.model;
  const messages = [createUserMessage({
    content: [{ type: "text", text: "请按要求输出。" }],
    source: { kind: "plugin", plugin: "ds-agents-lota-data" },
  })];
  const options = deepFreeze({
    provider: p,
    model: m,
    messages,
    system: prompt,
    temperature,
    maxTokens,
  });
  const assembler = new BlockAssembler();
  for await (const chunk of ctx.llm.stream(options)) {
    assembler.push(chunk);
  }
  const finish = assembler.finish;
  if (finish && (finish.kind === "error" || finish.kind === "aborted")) {
    throw new Error(`text stream ${finish.kind}`);
  }
  return assembler.blocks()
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
}

/** 解析 JSON（容错：剥 thinking / 提取花括号块）。 */
export function parseReflectJson(text) {
  const clean = text.replace(/\[thinking\][\s\S]*?\[\/thinking\]/g, "");
  try {
    return JSON.parse(clean);
  } catch {
    const m = clean.match(/\{[\s\S]*\}/);
    if (m) return JSON.parse(m[0]);
    return null;
  }
}

/** 确定性写回：因子归因 → ds_factors、新因子 → ds_factor_registry、反思 → ds_reflections。 */
export async function applyReflection(handles, dog, day, data, settled) {
  const [factorsDomain, registryDomain, reflectionsDomain] = await Promise.all([
    handles["ds_factors"], handles["ds_factor_registry"], handles["ds_reflections"],
  ]);
  const factorsTable = factorsDomain.table("factors");
  const registryTable = registryDomain.table("factors");
  const reflectionsTable = reflectionsDomain.table("reflections");

  const factorsRecord = factorsTable.get(dog) || { factor_perf: {}, updated_at: "" };
  const factorPerf = factorsRecord.factor_perf || {};
  const existingNames = new Set(Object.keys(factorPerf).map((n) => n.toLowerCase()));

  const descMap = data.factor_desc || {};
  const summary = data.reflection || "";
  const moneyLesson = data.money_lesson || "";
  const keySlugs = data.key_slugs || [];
  const noiseSlugs = data.noise_slugs || [];

  // 归因映射 order_N → factors
  const attrMap = new Map();
  for (const [key, fs] of Object.entries(data.factor_attribution || {})) {
    const m = key.match(/^order_(\d+)$/);
    if (!m) continue;
    const idx = Number(m[1]);
    if (idx >= 0 && idx < settled.length) {
      attrMap.set(idx, (Array.isArray(fs) ? fs : [fs]).map(cleanName).filter(isValidName));
    }
  }

  // 1. 归因驱动 record
  for (const [idx, fns] of attrMap) {
    const o = settled[idx];
    for (const fn of fns) {
      recordFactor(factorPerf, fn, o.hit, o.profit || 0, {
        desc: descMap[fn] || "", date: day, lotaId: o.lota_id, betSize: o.bet_size || 0,
      });
    }
  }

  // 2. 新因子（LLM 提到但未归因/未去重）
  const newFactors = [];
  const allAttributed = new Set([...attrMap.values()].flat().map((f) => f.toLowerCase()));
  for (const raw of (data.alpha_factors || [])) {
    const fn = cleanName(raw);
    if (!isValidName(fn)) continue;
    const lower = fn.toLowerCase();
    let dup = false;
    for (const en of existingNames) {
      if (lower === en || lower.includes(en) || en.includes(lower)) { dup = true; break; }
    }
    if (!dup) {
      newFactors.push(fn);
      existingNames.add(lower);
    }
  }
  for (const fn of newFactors) {
    if (!allAttributed.has(fn.toLowerCase())) {
      recordFactor(factorPerf, fn, null, 0, { desc: descMap[fn] || "", date: day });
    }
    // 2.5 保存 Factor 模型 → ds_factor_registry
    await registryTable.put(facIdFor(fn), {
      id: facIdFor(fn),
      slugs: keySlugs.filter((s) => typeof s === "string").slice(0, 12),
      content: descMap[fn] || summary.slice(0, 300),
    });
  }

  // 3. 反思记忆（含低样本标）
  const reflectionsRecord = reflectionsTable.get(dog) || { reflections: [], updated_at: "" };
  const reflections = reflectionsRecord.reflections || [];
  const sampleCount = new Set(settled.map((o) => o.lota_id).filter(Boolean)).size;
  let reflectionText = summary;
  if (sampleCount < 3) reflectionText += `\n⚠️ 低样本（仅 ${sampleCount} 场），结论待验证，不得作为重注/铁律依据`;
  if (keySlugs.length || noiseSlugs.length) {
    reflectionText += `\n📡 有效slug: ${keySlugs.join(", ") || "无"}`;
    if (noiseSlugs.length) reflectionText += `\n🔇 噪声slug: ${noiseSlugs.join(", ")}`;
    if (attrMap.size) reflectionText += `\n📐 因子归因: ${attrMap.size} 笔订单已关联到因子`;
  }
  if (moneyLesson) reflectionText += `\n💰 资金教训: ${moneyLesson}`;
  reflections.push({ date: day, reflection: reflectionText, recorded_at: beijingNowIso(), sample_count: sampleCount });

  // 写回
  await factorsTable.put(dog, { ...factorsRecord, factor_perf: factorPerf, updated_at: beijingNowIso() });
  await reflectionsTable.put(dog, { ...reflectionsRecord, reflections: reflections.slice(-20), updated_at: beijingNowIso() });

  return {
    ok: true, user: dog, day,
    attributed: [...attrMap.values()].reduce((s, v) => s + v.length, 0),
    new_factors: newFactors,
    summary: summary.slice(0, 200),
  };
}

/**
 * 读 persona 文本（对齐 role.persona_text）。
 * personaDir 可外置人设根目录（默认 cacheDir/roles），供 config.personaDir 覆盖。
 */
export function readPersona(cacheDir, dog, personaDir) {
  const root = personaDir || join(cacheDir, "roles");
  const p = join(root, dog, "persona.md");
  if (!existsSync(p)) return "";
  const text = readText(p).trim();
  return text ? `## 🎯 个人偏好\n\n${text}` : "";
}

/** 已有因子摘要（对齐 run_reflect 的 existing_summary）。 */
export async function getExistingFactorSummary(handles, dog) {
  const domain = await handles["ds_factors"];
  const rec = domain.table("factors").get(dog);
  const fp = (rec && rec.factor_perf) || {};
  return Object.entries(fp)
    .sort((a, b) => (b[1].total || 0) - (a[1].total || 0))
    .slice(0, 20)
    .map(([fid, s]) => `  ${fid}: ${s.total || 0}次 状态=${s.status || "active"}`)
    .join("\n");
}

/** 从 tags 缓存读段落（对齐 lota_sections）。 */
export function readSections(cacheDir, lotaId, slugs) {
  const raw = readJson(join(cacheDir, "tags", `${lotaId}.json`));
  const sections = (raw && raw.sections) || {};
  const picked = {};
  for (const slug of slugs) {
    if (sections[slug]) picked[slug] = sections[slug];
  }
  return picked;
}
