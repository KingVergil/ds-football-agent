/**
 * 回放模式（replay）：沙箱复制目录模型（2026-08-19 设计 v2）。
 *
 * 核心：回放 = 创建一只狗的角色复制目录（沙箱），新狗为空骨架、老狗复制「开始→D」到
 * D 结算后/因子归纳前（`D__pre-factor`）；全部桥调用经 `role_root` 写沙箱 `workspace/`，
 * 线上零影响；转正 = dsh 文件原语（备份线上 → 整目录替换）；放弃 = 删沙箱。
 *
 * 沙箱身份：`replays/sandboxes/<狗>_<MMDD>/`（幂等创建，可续跑）。
 * diff：沙箱只产事实（facts.json：订单/因子变化/资金曲线），总结交给 dsh。
 */
import {
  existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync, copyFileSync, rmSync, statSync, renameSync,
} from "node:fs";
import { join } from "node:path";

import { LLM_TEMPERATURES } from "./tools/shared.js";
import { beijingNowIso } from "./tools/shared.js";
import { streamText } from "./tools/llmText.js";
import { runBridge, defaultPythonBin } from "./bridge.js";
import { readDogRegistry, writeDogRegistry } from "./dogRegistry.js";

/** 回放旁路 LLM 默认模型（deepseek-flash 省 token）。 */
export const REPLAY_MODEL = "deepseek-v4-flash";

/** 单段回放天数上限。 */
export const REPLAY_MAX_DAYS = 60;

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

/** 起止足球日 → 逐日列表。 */
function dayListOf(start, end) {
  const days = [];
  let day = start;
  while (day <= end) {
    days.push(day);
    const [y, m, d] = day.split("-").map(Number);
    day = new Date(Date.UTC(y, m - 1, d) + 86400000).toISOString().slice(0, 10);
  }
  return days;
}

/** YYYY-MM-DD 且是真实日历日期。 */
export function isValidFootballDay(str) {
  if (typeof str !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(str)) return false;
  const [y, m, d] = str.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** 回放范围正确性校验。 */
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

// ── 文件原语（沙箱）────────────────────────────────

function roleDir(cacheDir, dog) {
  return join(cacheDir, "roles", dog);
}

function readRole(cacheDir, dog) {
  return readJson(join(roleDir(cacheDir, dog), `${dog}.json`)) || {};
}

function readFactors(cacheDir, dog) {
  return readJson(join(roleDir(cacheDir, dog), "memory", "factor_memory.json")) || {};
}

/** 沙箱根：replays/sandboxes/<狗>_<MMDD>/。 */
export function sandboxDirOf(cacheDir, sandboxName) {
  return join(cacheDir, "replays", "sandboxes", String(sandboxName).trim());
}

/** 沙箱名：<狗>_<MMDD>（用户定稿命名，如 梭哈2狗_0718）。 */
export function sandboxNameFor(dog, startDay) {
  const mmdd = String(startDay).replace(/^\d{4}-(\d{2})-(\d{2})$/, "$1$2");
  return `${dog}_${mmdd}`;
}

/** 递归复制目录。 */
function copyDir(src, dest) {
  mkdirSync(dest, { recursive: true });
  for (const name of readdirSync(src)) {
    const s = join(src, name);
    const d = join(dest, name);
    if (statSync(s).isDirectory()) copyDir(s, d);
    else copyFileSync(s, d);
  }
}

/** 把每只狗的角色目录复制到 destDir/<狗>/（起点快照 / 检查点用）。 */
export function snapshotRoleFiles(cacheDir, destDir, dogs) {
  const copied = [];
  for (const dog of dogs) {
    const src = roleDir(cacheDir, dog);
    if (!existsSync(src)) continue;
    copyDir(src, join(destDir, dog));
    copied.push(dog);
  }
  return copied;
}

/** 从 srcDir/<狗>/ 还原角色目录（先删再拷 = 真替换）。 */
export function restoreRoleFiles(cacheDir, srcDir, dogs) {
  const restored = [];
  for (const dog of dogs) {
    const src = join(srcDir, dog);
    if (!existsSync(src)) continue;
    const dest = roleDir(cacheDir, dog);
    rmSync(dest, { recursive: true, force: true });
    copyDir(src, dest);
    restored.push(dog);
  }
  return restored;
}

/** 列出沙箱检查点（start + 各阶段，跨狗去重）。 */
export function listCheckpoints(replayDir) {
  const cpDir = join(replayDir, "checkpoints");
  const names = new Set();
  if (existsSync(cpDir)) {
    for (const f of readdirSync(cpDir)) {
      if (f.endsWith(".json")) continue;
      if (existsSync(join(cpDir, f))) names.add(f);
    }
  }
  return ["start", ...[...names].sort()];
}

/** 订单 created_at（北京时间）→ 足球日（窗口 [D 12:01, D+1 12:00]）。 */
export function orderFootballDay(order) {
  const t = String((order && order.created_at) || "");
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/.exec(t);
  if (!m) return "";
  const d = new Date(`${m[1]}T${m[2]}+08:00`);
  if (Number.isNaN(d.getTime())) return "";
  // 北京墙钟 − 12h 后的北京日历日：bjMs = 真UTC − 4h，toISOString 即墙钟日期
  return new Date(d.getTime() - 4 * 3600 * 1000).toISOString().slice(0, 10);
}

/** 桥调用封装（带 role_root）。 */
async function bridgeCall(cacheDir, engineRoot, pythonBin, req, onProgress, roleRoot, envFile = "") {
  const full = {
    ...req,
    opts: { ...(req.opts || {}), ...(roleRoot ? { role_root: roleRoot } : {}) },
  };
  try {
    const r = await runBridge({ pythonBin, engineRoot, envFile, req: full, onProgress });
    if (r.ok) return { ok: true, data: r.data, func: req.func, dog: req.dog };
    const stderrTail = String(r.stderr || "").trim().slice(-600);
    return {
      ok: false,
      func: req.func,
      error: (r.error || "桥调用失败") + (stderrTail ? `｜${stderrTail}` : ""),
      dog: req.dog,
    };
  } catch (e) {
    return { ok: false, func: req.func, error: String((e && e.message) || e), dog: req.dog };
  }
}

/** 桥调用 + 心跳（LLM 决策期间任务进度不死）。 */
async function bridgeCallTick(cacheDir, engineRoot, pythonBin, req, onProgress, label, roleRoot, envFile = "") {
  const t0 = Date.now();
  const hb = setInterval(() => {
    onProgress({ phase: `${label}（${Math.round((Date.now() - t0) / 1000)}s）` });
  }, 10000);
  try {
    return await bridgeCall(cacheDir, engineRoot, pythonBin, req, onProgress, roleRoot, envFile);
  } finally {
    clearInterval(hb);
  }
}

// ── 沙箱生命周期（dsh 文件原语）────────────────────

/**
 * 创建沙箱（幂等）：snapshot/ = 线上角色复制；workspace/ 初始化 =
 *   - 线上有 `<start>__pre-factor` 检查点（结算后/因子归纳前）→ 复制它（老狗，从 D 因子归纳继续）
 *   - 否则 → 复制线上当前角色（新狗=空骨架，从 D 完整日管线开始）
 * @returns {{ok:boolean, sandboxDir:string, workspace:string, partialFirstDay:boolean, error?:string}}
 */
export function createSandbox(cacheDir, dog, startDay, sandboxName) {
  const sbDir = sandboxDirOf(cacheDir, sandboxName);
  if (existsSync(join(sbDir, "session.json"))) {
    return { ok: true, sandboxDir: sbDir, workspace: join(sbDir, "workspace"), reused: true, partialFirstDay: false };
  }
  const live = roleDir(cacheDir, dog);
  if (!existsSync(live)) {
    return { ok: false, error: `角色不存在: ${dog}（roles/${dog} 缺失）` };
  }
  const snapshot = join(sbDir, "snapshot");
  const workspace = join(sbDir, "workspace");
  mkdirSync(join(sbDir, "checkpoints"), { recursive: true });
  copyDir(live, snapshot);
  // 因子注册表（fac_*.json）复制进 workspace，保证沙箱内因子归纳读写完全隔离
  const factorsSrc = join(cacheDir, "factors");
  mkdirSync(join(workspace, "factors"), { recursive: true });
  if (existsSync(factorsSrc)) {
    for (const f of readdirSync(factorsSrc)) {
      if (f.endsWith(".json")) copyFileSync(join(factorsSrc, f), join(workspace, "factors", f));
    }
  }

  // 老狗：线上每日「结算后/因子前」检查点 → 沙箱起点
  const preFactor = join(live, "history", `${startDay}__pre-factor`);
  let partialFirstDay = false;
  if (existsSync(preFactor)) {
    copyDir(preFactor, workspace);
    partialFirstDay = true;
  } else {
    copyDir(live, workspace);
  }
  return { ok: true, sandboxDir: sbDir, workspace, partialFirstDay, snapshot };
}

/** 转正：备份线上 → workspace 整目录替换线上（决策①：替换）。 */
export function promoteSandbox(cacheDir, sandboxName, dog) {
  const sbDir = sandboxDirOf(cacheDir, sandboxName);
  const workspace = join(sbDir, "workspace");
  if (!existsSync(join(workspace, `${dog}.json`))) {
    return { ok: false, error: `沙箱 ${sandboxName} 的 workspace 缺少 ${dog}.json，无法转正` };
  }
  const live = roleDir(cacheDir, dog);
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const backup = join(cacheDir, "backups", `promote_${dog}_${ts}`);
  if (existsSync(live)) {
    mkdirSync(backup, { recursive: true });
    copyDir(live, backup);
  }
  rmSync(live, { recursive: true, force: true });
  copyDir(workspace, live);
  // 状态置 live（角色文件 + 注册表）
  const rolePath = join(live, `${dog}.json`);
  const role = readJson(rolePath) || {};
  role.status = "live";
  role.enabled = true;
  writeJson(rolePath, role);
  // 注册表同步：看板 enabledFor/默认列表读注册表优先，转正后立即翻 live
  syncRegistryLive(cacheDir, dog);
  return { ok: true, sandbox: sandboxName, dog, backup };
}

/** 转正后把注册表条目翻 live（enabled=true, status=live）；无注册表条目（如默认 7 狗）则跳过。 */
function syncRegistryLive(cacheDir, dog) {
  const dogs = readDogRegistry(cacheDir);
  const idx = dogs.findIndex((d) => d && d.name === dog);
  if (idx < 0) return;
  const next = [...dogs];
  next[idx] = { ...next[idx], enabled: true, status: "live" };
  writeDogRegistry(cacheDir, next);
}

/** 放弃：删沙箱，线上不动。 */
export function abortSandbox(cacheDir, sandboxName) {
  const sbDir = sandboxDirOf(cacheDir, sandboxName);
  if (!existsSync(sbDir)) return { ok: true, removed: false };
  rmSync(sbDir, { recursive: true, force: true });
  return { ok: true, removed: true, sandbox: sandboxName };
}

/** 沙箱列表（dashboard 用）。 */
export function listSandboxes(cacheDir) {
  const root = join(cacheDir, "replays", "sandboxes");
  if (!existsSync(root)) return [];
  try {
    return readdirSync(root)
      .map((name) => {
        const s = readJson(join(root, name, "session.json"));
        return {
          name,
          status: (s && s.status) || "created",
          dog: (s && s.dog) || "",
          start: (s && s.start) || "",
          end: (s && s.end) || "",
          daysDone: (s && Number(s.next_idx)) || 0,
          daysTotal: Array.isArray(s && s.days) ? s.days.length : 0,
          nextDay: Array.isArray(s && s.days) ? (s.days[Number(s.next_idx) || 0] ?? "") : "",
          interactive: Boolean(s && s.interactive),
          factorReviewEvery: (s && s.factor_review_every) ?? null,
          skipLlm: Boolean(s && s.skip_llm),
          directionSuggestion: (s && s.pending_direction) || "",
          lastError: (s && s.last_error) || "",
          updatedAt: (s && s.updated_at) || "",
          logTail: Array.isArray(s && s.log) ? s.log.slice(-14) : [],
          reportExists: existsSync(join(root, name, "report.md")),
          factsExists: existsSync(join(root, name, "facts.json")),
          promoted: Boolean(s && s.promoted),
        };
      })
      .filter(Boolean)
      .sort((a, b) => String(b.updatedAt || b.name).localeCompare(String(a.updatedAt || a.name)))
      .slice(0, 20);
  } catch {
    return [];
  }
}

/** 会话持久化（session.json 落在沙箱根）。 */
function sessionPath(replayDir) { return join(replayDir, "session.json"); }
function loadSession(replayDir) { return readJson(sessionPath(replayDir)); }
function saveSession(replayDir, s) {
  const { onProgress, ...rest } = s;
  writeJson(sessionPath(replayDir), { ...rest, updated_at: beijingNowIso() });
}

/** 从 workspace 角色文件 + 记录，写事实文件 facts.json（纯事实，不产结论）。 */
function writeFacts(cacheDir, sandboxDir, s) {
  const role = readJson(join(sandboxDir, "workspace", `${s.dog}.json`)) || {};
  const orders = (role.orders || []).map((o) => ({
    day: orderFootballDay(o),
    lota_id: o.lota_id || "",
    bet_type: o.bet_type || "",
    pick: o.pick || "",
    handicap: o.handicap ?? null,
    odds: o.odds ?? null,
    bet_size: o.bet_size ?? null,
    profit: o.profit ?? null,
    settled_at: o.settled_at || null,
  }));
  const factorChanges = (s.reviewLog || []).flatMap((r) =>
    (r.cycle_changes || []).map((c) => ({ day: r.day, action: c.to, factor: c.id, from: c.from })));
  const capitalCurve = readJson(join(sandboxDir, "workspace", "capital_history.json")) || [];
  const facts = {
    dog: s.dog,
    sandbox: s.sandbox,
    range: { start: s.start, end: s.end },
    orders,
    factor_changes: factorChanges,
    capital_curve: capitalCurve,
    trajectory: s.trajectory || [],
    generated_at: beijingNowIso(),
  };
  writeJson(join(sandboxDir, "facts.json"), facts);
  return facts;
}

// ── 每日管线（沙箱内）────────────────────────────

/** 跑沙箱的一天。partialFirstDay=true 且 dayIdx===0：D 的分析/结算已随复制带过来，只从因子归纳继续。 */
async function runReplayDay(ctx, cacheDir, engineRoot, pythonBin, envFile, sandboxDir, d, dayIdx, cfg, acc) {
  const { dog, factorReviewEvery, userNotes, onProgress, days, skipLlm, roleRoot } = cfg;
  const { trajectory, reviewLog, checkpointLog, log, prepLog } = acc;
  const warn = (msg) => log.push(`⚠️ ${msg}`);
  const total = days.length;
  const firstPartial = cfg.partialFirstDay && dayIdx === 0;
  let placed = 0;
  let settled = 0;
  let pnl = 0;
  log.push(`\n📅 [${d}] 第 ${dayIdx + 1}/${total} 天（${beijingNowIso()}）${firstPartial ? "（沙箱起点日：分析/结算沿用复制状态，从因子归纳继续）" : ""}`);

  if (!firstPartial) {
    // 1) 数据准备（replay：缓存优先）
    onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 数据准备`, done: dayIdx, total, detail: d });
    const prep = await bridgeCall(cacheDir, engineRoot, pythonBin, {
      func: "prepare", day: d, opts: { mode: "replay", jingcai_only: true },
    }, (p) => onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 数据准备·${p.phase || ""}`, done: dayIdx, total, detail: p.detail || d }), roleRoot, envFile);
    const prepMeta = prep.ok ? {
      day: d, candidates: prep.data.candidates, prefetched_ok: prep.data.prefetched_ok,
      warnings: prep.data.warnings || [],
    } : { day: d, error: prep.error };
    prepLog.push(prepMeta);
    if (prep.ok) {
      log.push(`   数据准备: 竞彩 ${prep.data.candidates ?? 0} 场，预取 ${prep.data.prefetched_ok ?? 0}`);
      for (const w of prepMeta.warnings) warn(`[${d}] ${w}`);
    } else {
      warn(`[${d}] 数据准备失败: ${prep.error}`);
    }

    // 2) 分析（沙箱内 LLM 决策）
    onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 分析`, done: dayIdx, total, detail: d });
    const r = await bridgeCallTick(cacheDir, engineRoot, pythonBin, {
      func: "analyze", dog, day: d,
      opts: { prefetched: true, live: false, jingcai_only: true, ...(skipLlm ? { skip_llm: true } : {}) },
    }, (p) => onProgress({
      phase: `第 ${dayIdx + 1}/${total} 天 分析 ${dog}·${p.phase || ""}`, done: dayIdx, total, detail: p.detail || d,
    }), `第 ${dayIdx + 1}/${total} 天 分析 ${dog}`, roleRoot, envFile);
    placed = r.ok ? (r.data.placed || 0) : 0;
    log.push(`     ${r.ok ? "✅" : "❌"} ${dog} 下单 ${placed} 单${r.error ? " " + r.error : ""}`);

    // 3) 结算（写沙箱；live 侧由桥自动落 pre-factor 检查点，沙箱内由本层管）
    onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 结算 ${dog}`, done: dayIdx, total, detail: d });
    const settle = await bridgeCall(cacheDir, engineRoot, pythonBin, { func: "settle", dog, day: d, opts: {} }, undefined, roleRoot, envFile);
    const settleRes = settle.ok ? (settle.data.settlement || {}) : { settled: 0, pnl: 0 };
    settled = settleRes.settled || 0;
    pnl = settleRes.pnl || 0;
    log.push(`   💰 ${dog}: 结算${settled}单 PnL${pnl}${settle.error ? `（${settle.error}）` : ""}`);
  }

  // 检查点：结算后/因子前（沙箱内，供「重跑因子流」/回退）
  const preFactorCp = `${d}__pre-factor`;
  copyDir(roleRoot, join(sandboxDir, "checkpoints", preFactorCp));
  checkpointLog.push({ name: preFactorCp, day: d, phase: "结算后/因子前" });

  // 4) 因子归纳（沙箱内）
  onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子归纳 ${dog}`, done: dayIdx, total, detail: d });
  const ind = await bridgeCallTick(cacheDir, engineRoot, pythonBin, {
    func: "factor-induction", dog, day: d, opts: {},
  }, (p) => onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子归纳 ${dog}·${p.phase || ""}`, done: dayIdx, total, detail: p.detail || d }), `第 ${dayIdx + 1}/${total} 天 因子归纳 ${dog}`, roleRoot, envFile);
  const sum = ind.ok ? (ind.data.summary || {}) : {};
  if (ind.ok) log.push(`   🧬 因子归纳 ${dog}: 合并 ${sum.merged || 0}，补定义 ${sum.fac_created || 0}`);
  else warn(`[${d}] 因子归纳失败: ${ind.error}`);

  // 5) 周期性因子退役（周期边界；可带用户意见）
  const reviewDone = (dayIdx + 1) % factorReviewEvery === 0;
  if (reviewDone) {
    onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子退役`, done: dayIdx, total, detail: d });
    const cycleStartIdx = Math.max(0, dayIdx - (dayIdx % factorReviewEvery));
    const startDate = days[cycleStartIdx];
    const rev = await bridgeCallTick(cacheDir, engineRoot, pythonBin, {
      func: "factor-review", dog, end: d, start: startDate,
      opts: { user_notes: userNotes, ...(skipLlm ? { skip_llm: true } : {}) },
    }, (p) => onProgress({ phase: `第 ${dayIdx + 1}/${total} 天 因子退役 ${dog}·${p.phase || ""}`, done: dayIdx, total, detail: p.detail || d }), `第 ${dayIdx + 1}/${total} 天 因子退役 ${dog}`, roleRoot, envFile);
    const entry = { day: d, dog, ...(rev.ok ? rev.data : { error: rev.error }) };
    reviewLog.push(entry);
    if (rev.ok) {
      const changes = (entry.cycle_changes || []).filter((c) => c.to === "retired" || c.to === "dormant");
      const retired = changes.filter((c) => c.to === "retired").map((c) => c.id);
      const dormant = changes.filter((c) => c.to === "dormant").map((c) => c.id);
      const bits = [];
      if (retired.length) bits.push(`结构性退役: ${retired.join("、")}`);
      if (dormant.length) bits.push(`休眠 ${dormant.length} 个`);
      log.push(`   🔬 因子退役 ${dog}: ${bits.length ? bits.join(" | ") : "无调整"}${userNotes ? "（已应用用户意见）" : ""}`);
    } else {
      warn(`[${d}] 因子退役失败: ${rev.error}`);
    }
  }

  // 轨迹 + 当日终态检查点
  const role = readJson(join(roleRoot, `${dog}.json`)) || {};
  const fp = readJson(join(roleRoot, "memory", "factor_memory.json")) || {};
  trajectory.push({
    day: d,
    dogs: {
      [dog]: {
        capital: round2(Number(role.capital || 0)),
        pending: (role.orders || []).filter((o) => !o.settled_at).length,
        settled,
        pnl: round2(pnl),
        placed,
        active_factors: Object.values(fp.factor_perf || {}).filter((x) => x && x.status !== "retired").length,
      },
    },
  });
  const postDay = `${d}__post-day`;
  copyDir(roleRoot, join(sandboxDir, "checkpoints", postDay));
  checkpointLog.push({ name: postDay, day: d, phase: "当日终态" });
  log.push(`   📍 检查点: ${postDay}`);

  return { reviewDone };
}

/** 生成「下一轮方向建议」：skipLlm=true 只启发式；否则启发式 + LLM 润色（失败回退）。 */
async function buildDirection(ctx, { dogs, cycleReviews, cycleTraj, model, skipLlm }) {
  const dog = dogs[0];
  const lines = [];
  for (const d of dogs) {
    const revs = cycleReviews.filter((r) => r.dog === d && !r.error);
    const changes = revs.flatMap((r) => (r.cycle_changes || []).filter((c) => c.to === "retired" || c.to === "dormant"));
    const retired = changes.filter((c) => c.to === "retired").map((c) => c.id);
    const dormant = changes.filter((c) => c.to === "dormant").map((c) => c.id);
    const pnl = cycleTraj.reduce((s, t) => s + Number(((t.dogs || {})[d] || {}).pnl || 0), 0);
    const bits = [];
    if (retired.length) bits.push(`退役 ${retired.join("、")}`);
    if (dormant.length) bits.push(`休眠 ${dormant.length} 个`);
    const trend = pnl > 0 ? `PnL +${round2(pnl)}（盈）` : pnl < 0 ? `PnL ${round2(pnl)}（亏）` : "PnL 0（平）";
    const advice = pnl < 0
      ? "下轮建议收紧退役标准、复核连亏因子，新因子取样更保守"
      : "下轮建议维持现有因子，重点观察样本不足的新因子";
    lines.push(`- ${d}：${trend}${bits.length ? "；" + bits.join("；") : "；本周期无退役"}。${advice}`);
  }
  let text = `下一轮因子归纳/退役方向建议（可编辑后作为 induction_notes 回传，将注入下一周期退役评估）：\n${lines.join("\n")}`;

  if (!skipLlm && ctx && ctx.llm && typeof ctx.llm.stream === "function") {
    try {
      const prompt = `你是足球投注因子教练。基于下面本周期的因子退役与盈亏摘要，给出「下一轮因子归纳/退役方向」的简洁建议（中文，≤200字，聚焦保留/收紧/观察方向，不要复述数字）：\n\n${text}`;
      const refined = (await streamText(ctx, prompt, { model, maxTokens: 800, temperature: LLM_TEMPERATURES.induction })).trim();
      if (refined) text = refined;
    } catch { /* 回退启发式 */ }
  }
  return text;
}

/** 从 s.next_idx 起逐日跑；interactive 在周期边界（做了退役且非最后一天）暂停。 */
async function replaySegment(ctx, cacheDir, engineRoot, pythonBin, envFile, sandboxDir, s) {
  const cfg = {
    dog: s.dog, factorReviewEvery: s.factor_review_every, userNotes: s.user_notes,
    onProgress: s.onProgress || (() => {}), days: s.days,
    skipLlm: s.skip_llm === true, roleRoot: join(sandboxDir, "workspace"),
    partialFirstDay: s.partial_first_day === true,
  };
  const acc = {
    trajectory: s.trajectory, reviewLog: s.reviewLog, checkpointLog: s.checkpointLog,
    log: s.log, prepLog: s.prepLog,
  };
  while (s.next_idx < s.days.length) {
    const dayIdx = s.next_idx;
    const d = s.days[dayIdx];
    const { reviewDone } = await runReplayDay(ctx, cacheDir, engineRoot, pythonBin, envFile, sandboxDir, d, dayIdx, cfg, acc);
    s.next_idx = dayIdx + 1;
    saveSession(sandboxDir, s); // 每日即时落盘：dashboard 逐日刷新
    if (s.interactive && reviewDone && s.next_idx < s.days.length) {
      return { paused: true, lastDay: d, cycleEndIdx: dayIdx };
    }
  }
  return { paused: false };
}

/** 暂停：生成方向建议、写事实、落 session、返回 paused。 */
async function pauseReplay(ctx, sandboxDir, s, seg) {
  const d = seg.lastDay;
  const cycleReviews = s.reviewLog.filter((r) => r.day === d);
  const cycleStartIdx = Math.max(0, seg.cycleEndIdx - s.factor_review_every + 1);
  const cycleTraj = s.trajectory.slice(cycleStartIdx, seg.cycleEndIdx + 1);
  const suggestion = await buildDirection(ctx, {
    dogs: [s.dog], cycleReviews, cycleTraj, model: s.model, skipLlm: s.skip_llm === true,
  });
  s.status = "paused";
  s.pending_direction = suggestion;
  writeFacts(cacheDir, sandboxDir, s);
  saveSession(sandboxDir, s);
  const nextDay = s.days[s.next_idx];
  return {
    ok: true,
    status: "paused",
    sandbox: s.sandbox,
    run_id: s.run_id,
    dog: s.dog,
    replay_dir: sandboxDir,
    cycle: { end_day: d, days_done: s.next_idx, days_total: s.days.length },
    next_day: nextDay,
    remaining_days: s.days.length - s.next_idx,
    direction_suggestion: suggestion,
    factor_reviews: cycleReviews,
    trajectory_tail: s.trajectory.slice(-s.factor_review_every),
    checkpoints: listCheckpoints(sandboxDir),
    log: s.log.slice(-20),
  };
}

/** 回退：把沙箱 workspace 恢复到「某天开始」状态（前一天终态/起点），截断轨迹。 */
async function rewindSession(cacheDir, sandboxDir, s, toDay) {
  const idx = s.days.indexOf(toDay);
  if (idx < 0) return { error: `rewind_to 不在回放范围: ${toDay}（范围 ${s.start}~${s.end}）` };
  const src = idx === 0
    ? join(sandboxDir, "snapshot")
    : join(sandboxDir, "checkpoints", `${s.days[idx - 1]}__post-day`);
  if (!existsSync(src)) {
    return { error: `缺少可回退快照：${idx === 0 ? "snapshot" : s.days[idx - 1] + "__post-day"}` };
  }
  const workspace = join(sandboxDir, "workspace");
  rmSync(workspace, { recursive: true, force: true });
  copyDir(src, workspace);
  s.trajectory = s.trajectory.filter((t) => t.day < toDay);
  s.reviewLog = s.reviewLog.filter((r) => r.day < toDay);
  s.checkpointLog = s.checkpointLog.filter((c) => c.day < toDay);
  s.prepLog = s.prepLog.filter((p) => p.day < toDay);
  s.next_idx = idx;
  s.log.push(`⏪ 回退到 ${toDay} 开始状态（恢复自 ${idx === 0 ? "起点快照" : s.days[idx - 1] + " 终态"}）`);
  return { idx };
}

/** 收尾：终态检查点 + facts + 报告 + 可选还原起点，标记完成。 */
async function finalizeReplay(cacheDir, sandboxDir, s, restoreAfter) {
  const postFactor = `${s.end}__post-factor`;
  copyDir(join(sandboxDir, "workspace"), join(sandboxDir, "checkpoints", postFactor));
  s.checkpointLog.push({ name: postFactor, day: s.end, phase: "因子流后（终态）" });
  writeFacts(cacheDir, sandboxDir, s);

  const finalCapital = Number((readJson(join(sandboxDir, "workspace", `${s.dog}.json`)) || {}).capital || 0);
  const report = {
    run_id: s.run_id,
    sandbox: s.sandbox,
    created_at: beijingNowIso(),
    range: { start: s.start, end: s.end, days: s.days.length },
    dog: s.dog,
    model: s.model,
    reset: s.reset,
    restore_after: restoreAfter,
    factor_review_every: s.factor_review_every,
    user_notes: s.user_notes,
    skip_llm: s.skip_llm === true,
    data_prep: s.prepLog,
    start_capital: { [s.dog]: s.start_capital },
    end_capital: { [s.dog]: finalCapital },
    trajectory: s.trajectory,
    factor_reviews: s.reviewLog,
    checkpoints: listCheckpoints(sandboxDir),
    checkpoint_log: s.checkpointLog,
    warnings: s.log.filter((l) => l.startsWith("⚠️")),
  };
  writeJson(join(sandboxDir, "report.json"), report);
  writeJson(join(sandboxDir, "replay.log.json"), s.log);
  writeFileSync(join(sandboxDir, "report.md"), buildReportMarkdown(report, s.log), "utf8");

  let restored = null;
  if (restoreAfter) {
    rmSync(roleDir(cacheDir, s.dog), { recursive: true, force: true });
    copyDir(join(sandboxDir, "snapshot"), roleDir(cacheDir, s.dog));
    restored = [s.dog];
  }
  s.status = "finished";
  saveSession(sandboxDir, s);
  return {
    ok: true,
    status: "finished",
    sandbox: s.sandbox,
    run_id: s.run_id,
    dog: s.dog,
    replay_dir: sandboxDir,
    days: s.days.length,
    start_capital: s.start_capital,
    end_capital: finalCapital,
    restored,
    facts_path: join(sandboxDir, "facts.json"),
    report_path: join(sandboxDir, "report.md"),
    log: s.log.slice(-40),
  };
}

/**
 * 跑一轮回放（沙箱模型）。
 *
 * @param {object} opts
 *   全新：dog（或 dogs[0]）/ start / end / sandbox（缺省 <狗>_<MMDD>）/
 *         mode("interactive"|"auto") / factor_review_every / reset("none"|"zero") /
 *         restore_after / skip_llm（演示）/ user_notes / run_id / pythonBin
 *   续跑：sandbox 已存在 paused 会话 → resume（induction_notes / to_end / rewind_to）
 */
export async function runReplay(ctx, cacheDir, engineRoot, opts = {}) {
  const start = opts.start;
  const end = opts.end;
  const dog = String((opts.dog || (Array.isArray(opts.dogs) && opts.dogs[0]) || "")).trim();
  const sandbox = String(opts.sandbox || (dog && start ? sandboxNameFor(dog, start) : "")).trim();
  if (!sandbox) return { ok: false, error: "回放需要 sandbox（<狗>_<MMDD>）或 dog+start" };
  const sandboxDir = sandboxDirOf(cacheDir, sandbox);

  // 已存在 paused 会话 → 续跑
  const existing = loadSession(sandboxDir);
  if (existing && existing.status === "paused") {
    return resumeReplay(ctx, cacheDir, engineRoot, { ...opts, sandbox });
  }
  if (!dog) return { ok: false, error: "新回放需要 dog（沙箱单狗模型）" };
  const rangeCheck = validateReplayRange(start, end);
  if (!rangeCheck.ok) return rangeCheck;

  const created = createSandbox(cacheDir, dog, start, sandbox);
  if (!created.ok) return created;
  const interactive = opts.interactive === true || opts.mode === "interactive";
  const model = opts.model || REPLAY_MODEL;
  const factorReviewEvery = Math.max(1, Number(opts.factor_review_every) || 7);
  const reset = opts.reset === "zero" ? "zero" : "none";
  const restoreAfter = opts.restore_after === true;
  const userNotes = String(opts.user_notes || "").trim();
  const skipLlm = opts.skip_llm === true;
  const pythonBin = opts.pythonBin || defaultPythonBin();
  const envFile = opts.envFile || "";
  const onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};
  const runId = opts.run_id || `replay_${sandbox}_${Date.now()}`;

  const log = [];
  const warn = (msg) => log.push(`⚠️ ${msg}`);
  const roleRoot = created.workspace;

  try {
    // 可选：从 0 开始（沙箱 workspace 内重置）
    if (reset === "zero") {
      const r = await bridgeCall(cacheDir, engineRoot, pythonBin, {
        func: "reset", dog, opts: { reset_mode: "full" },
      }, undefined, roleRoot, envFile);
      if (r.ok) log.push(`🧹 reset=zero：${dog} 已重置为初始资金 + 空记忆`);
      else warn(`${dog} reset 失败: ${r.error}`);
    }

    const startCapital = Number(readJson(join(roleRoot, `${dog}.json`)).capital || 0);
    const s = {
      run_id: runId, sandbox, dog, start, end, model,
      factor_review_every: factorReviewEvery, reset, restore_after: restoreAfter,
      interactive, days: dayListOf(start, end), next_idx: 0, status: "running",
      user_notes: userNotes, skip_llm: skipLlm,
      partial_first_day: created.partialFirstDay === true,
      start_capital: startCapital,
      trajectory: [], reviewLog: [], checkpointLog: [], prepLog: [], log,
      created_at: beijingNowIso(),
      onProgress,
    };
    saveSession(sandboxDir, s);

    const seg = await replaySegment(ctx, cacheDir, engineRoot, pythonBin, envFile, sandboxDir, s);
    if (seg.paused) return await pauseReplay(ctx, sandboxDir, s, seg);
    return await finalizeReplay(cacheDir, sandboxDir, s, restoreAfter);
  } catch (e) {
    if (restoreAfter) {
      try {
        rmSync(roleRoot, { recursive: true, force: true });
        copyDir(join(sandboxDir, "snapshot"), roleRoot);
      } catch {}
    }
    throw e;
  }
}

/** 续跑暂停会话（沙箱内）。 */
async function resumeReplay(ctx, cacheDir, engineRoot, opts) {
  const sandbox = String(opts.sandbox || "").trim();
  const sandboxDir = sandboxDirOf(cacheDir, sandbox);
  const s = loadSession(sandboxDir);
  if (!s) return { ok: false, error: `找不到沙箱会话: ${sandbox}（session.json 缺失）` };
  if (s.status === "finished") {
    return { ok: false, error: `沙箱 ${sandbox} 已完成；可转正（promote）或放弃（abort）` };
  }
  s.onProgress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};

  if (opts.rewind_to) {
    const r = await rewindSession(cacheDir, sandboxDir, s, String(opts.rewind_to));
    if (r.error) return { ok: false, error: r.error };
    saveSession(sandboxDir, s);
    if (!opts.to_end && !(typeof opts.induction_notes === "string" && opts.induction_notes.trim())) {
      s.status = "paused";
      saveSession(sandboxDir, s);
      return {
        ok: true, status: "rewound", sandbox, run_id: s.run_id, replay_dir: sandboxDir,
        next_day: s.days[s.next_idx], remaining_days: s.days.length - s.next_idx,
        checkpoints: listCheckpoints(sandboxDir), log: s.log.slice(-20),
      };
    }
  }

  if (typeof opts.induction_notes === "string" && opts.induction_notes.trim()) {
    s.user_notes = opts.induction_notes.trim();
    s.log.push(`📝 应用下一轮方向（induction_notes）：${s.user_notes.slice(0, 120)}`);
  }
  if (opts.to_end === true) s.interactive = false;
  s.status = "running";

  try {
    const seg = await replaySegment(ctx, cacheDir, engineRoot, opts.pythonBin || defaultPythonBin(), opts.envFile || "", sandboxDir, s);
    if (seg.paused) return await pauseReplay(ctx, sandboxDir, s, seg);
    return await finalizeReplay(cacheDir, sandboxDir, s, s.restore_after);
  } catch (e) {
    s.status = "paused";
    s.last_error = String((e && e.message) || e);
    saveSession(sandboxDir, s);
    return { ok: false, status: "paused", sandbox, error: `续跑失败: ${s.last_error}` };
  }
}

/** 生成人类可读的回放报告。 */
export function buildReportMarkdown(report, logLines = []) {
  const rows = report.trajectory.map((t) => {
    const d = t.dogs && t.dogs[report.dog] || {};
    return `| ${t.day} | ${d.capital ?? "-"}(${d.pnl >= 0 ? "+" : ""}${d.pnl ?? 0}) |`;
  }).join("\n");
  const reviewLines = report.factor_reviews.length
    ? report.factor_reviews.map((r) => {
        const changes = (r.cycle_changes || []).filter((c) => c.to === "retired" || c.to === "dormant");
        const retired = changes.filter((c) => c.to === "retired").map((c) => c.id);
        const dormant = changes.filter((c) => c.to === "dormant").map((c) => c.id);
        const bits = [];
        if (retired.length) bits.push(`结构性退役: ${retired.join("、")}`);
        if (dormant.length) bits.push(`休眠 ${dormant.length} 个`);
        return `- [${r.day}] ${r.dog}: ${r.error ? r.error : (bits.length ? bits.join("；") : "无调整")}`;
      }).join("\n")
    : "(未触发或窗口内无因子)";
  const s = report.start_capital || {};
  const e = report.end_capital || {};
  const cmp = `- ${report.dog}: ${s[report.dog] ?? 0} → ${e[report.dog] ?? 0}（${((e[report.dog] ?? 0) - (s[report.dog] ?? 0)) >= 0 ? "+" : ""}${Math.round((e[report.dog] ?? 0) - (s[report.dog] ?? 0))}）`;
  const warnings = report.warnings && report.warnings.length ? report.warnings.map((w) => `- ${w}`).join("\n") : "(无)";
  const prepDays = Array.isArray(report.data_prep)
    ? report.data_prep.map((p) => `${p.day}: ${p.candidates ?? "?"} 场${p.error ? `（${p.error}）` : ""}`).join("、")
    : "";
  return `# 回放报告 ${report.run_id}（沙箱 ${report.sandbox}）

- 范围：${report.range.start} ~ ${report.range.end}（${report.range.days} 天）｜狗：${report.dog}
- 模型：${report.model}（旁路 LLM，仅建议草稿）${report.skip_llm ? "｜演示模式（跳过 LLM）" : ""}
- 起点：${report.reset === "zero" ? "从 0 重置" : "复制到起点日"}｜结束处理：${report.restore_after ? "还原起点（模拟）" : "沙箱保留，待转正"}
- 因子退役周期：每 ${report.factor_review_every} 天${report.user_notes ? `｜用户意见：${report.user_notes}` : ""}

## 数据准备（逐日）

- ${prepDays || "(无)"}
- 警告：${warnings}

## 轨迹（余额与当日 PnL）

| 日期 | 余额(PnL) |
|---|---|
${rows || "(无轨迹)"}

## 起点 vs 终点

${cmp}

## 检查点

${(report.checkpoints || ["start"]).map((c) => `- \`${c}\``).join("\n")}

## 因子退役记录

${reviewLines}

## 运行日志（节选）

\`\`\`text
${(logLines || []).slice(-30).join("\n")}
\`\`\`
`;
}
