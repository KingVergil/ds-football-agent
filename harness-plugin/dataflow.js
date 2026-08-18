/**
 * 数据获取边界（dataflow）：LLM 之前的确定性数据准备层。
 *
 * 设计（对齐用户要求的"获取数据先行"）：
 *   - 分析/结算/因子/回放全部先走 prepareDay / prepareRange，角色只消费结果；
 *   - live 模式：强制刷新（Python prefetch 的 live-strict 语义，拒绝旧赔率进 prompt）；
 *   - 历史/回放模式：缓存优先 —— matches 有缓存直接读，缺了才调私有 fetcher 从 URL 拉一次；
 *     features/tags 同理，缺的按竞彩场次补齐（fetcher prefetch --jingcai，cache-first 跳过已有）；
 *   - 竞彩边界：所有返回的比赛列表都按足球日窗口 [D 12:01, D+1 12:00] + jingcai_number 过滤，
 *     北单/无号场次在 LLM 之前就被排除（对齐 Python --jingcai 与 fanout readMatchesCache）。
 *
 * 网络只发生在私有 fetcher（harness-plugin/lota_fetcher.js，单独分发）或私有 Python prefetch，
 * 插件本身不写死 URL/key。
 */
import { existsSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { join } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";

/** day±n 的日期串（UTC 计算，避免宿主机时区漂移）。 */
export function addDays(dayStr, n) {
  const [y, m, d] = dayStr.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

/** 足球日窗口 [D 12:01, D+1 12:00]（分钟精度，与 match_time[:16] 字符串比较对齐）。 */
export function footballDayRange(day) {
  return {
    start: `${day} 12:01`,
    end: `${addDays(day, 1)} 12:00`,
  };
}

/** 某足球日涉及的两个日历日期（跨天窗口）。 */
export function calendarDatesForDay(day) {
  return [day, addDays(day, 1)];
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

/** 读 matches/<date>.json，兼容 list 与 {matches:[...]}。 */
export function readMatchesFile(cacheDir, date) {
  const raw = readJson(join(cacheDir, "matches", `${date}.json`));
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.matches)) return raw.matches;
  return [];
}

/** 剥离比分（分析防后视；result 一并剥离）。 */
export function stripScores(match) {
  if (!match || typeof match !== "object") return match;
  const { score, result, ...rest } = match;
  return rest;
}

/** 足球日窗口内 + 竞彩号非空的比赛（strip_scores 可选），按开赛时间排序。 */
export function jingcaiWindowMatches(cacheDir, day, { strip = false, jingcaiOnly = true } = {}) {
  const { start, end } = footballDayRange(day);
  const out = [];
  for (const date of calendarDatesForDay(day)) {
    for (const m of readMatchesFile(cacheDir, date)) {
      if (!m || !m.lota_id) continue;
      const mt = String(m.match_time || "").replace("T", " ").slice(0, 16);
      if (mt < start || mt > end) continue;
      if (jingcaiOnly && !m.jingcai_number) continue;
      out.push(strip ? stripScores(m) : m);
    }
  }
  return out.sort((a, b) => String(a.match_time || "").localeCompare(String(b.match_time || "")));
}

/** 有效 features 缓存：存在、非负缓存桩、有 compact_fet 文本。 */
export function hasValidFeature(cacheDir, lotaId) {
  const raw = readJson(join(cacheDir, "features", `${lotaId}.json`));
  if (!raw || raw._api_failed) return false;
  const inner = raw.data && typeof raw.data === "object" ? raw.data : {};
  return Boolean(raw.compact_fet || inner.compact_fet);
}

/** 有效 tags 缓存：存在且至少一个段落。 */
export function hasTags(cacheDir, lotaId) {
  const raw = readJson(join(cacheDir, "tags", `${lotaId}.json`));
  return Boolean(raw && raw.sections && Object.keys(raw.sections).length);
}

/** 某个日历日期的 matches 缓存是否存在（非空数组）。 */
export function hasMatchesFile(cacheDir, date) {
  return readMatchesFile(cacheDir, date).length > 0;
}

/** 逐行读 ~/.zshrc 里的 LOTA_API_KEY 兜底（dsh 非交互进程可能没继承 shell env）。 */
function lotaKeyFromZshrc() {
  try {
    const text = readFileSync(join(homedir(), ".zshrc"), "utf8");
    const m = text.match(/LOTA_API_KEY=["']?([A-Za-z0-9_-]+)/);
    return m ? m[1] : "";
  } catch {
    return "";
  }
}

/** spawn 一个私有命令（fetcher / python prefetch），继承 env + LOTA_API_KEY 兜底。 */
export function runPrivateCmd(cmd, args, cwd, { timeoutMs = 300000, env = {} } = {}) {
  return new Promise((resolve) => {
    const childEnv = {
      ...process.env,
      LOTA_API_KEY: process.env.LOTA_API_KEY || lotaKeyFromZshrc(),
      ...env,
    };
    const child = spawn(cmd, args, { cwd, env: childEnv, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      child.kill("SIGKILL");
      resolve({ code: "timeout", stdout, stderr });
    }, timeoutMs);
    child.stdout.on("data", (b) => { stdout += String(b); });
    child.stderr.on("data", (b) => { stderr += String(b); });
    child.on("error", (err) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ code: "spawn-error", error: err.message, stdout, stderr });
    });
    child.on("exit", (code) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

/**
 * 私有 fetcher 路径：优先插件目录（harness-plugin/lota_fetcher.js，单独分发），
 * 缺了回退 engineRoot（老位置兼容）。返回 node 命令 + fetcher 路径。
 */
function fetcherCmd(engineRoot) {
  const local = join(fileURLToPath(new URL(".", import.meta.url)), "lota_fetcher.js");
  const fetcher = existsSync(local) ? local : join(engineRoot, "lota_fetcher.js");
  return {
    node: process.execPath,
    fetcher,
  };
}

/** 拉取缺失的 matches 缓存文件（历史：URL 一次拉取，之后永远走缓存）。 */
export async function fetchMissingMatches(cacheDir, engineRoot, dates) {
  const { node, fetcher } = fetcherCmd(engineRoot);
  const fetched = [];
  const failed = [];
  for (const date of dates) {
    if (hasMatchesFile(cacheDir, date)) continue; // 缓存优先
    const r = await runPrivateCmd(node, [fetcher, "refresh-date", date], engineRoot, {
      timeoutMs: 180000,
      env: { LOTA_DATA_ROOT: cacheDir },
    });
    if (r.code === 0 && hasMatchesFile(cacheDir, date)) fetched.push(date);
    else failed.push({ date, code: r.code, stderr: String(r.stderr || "").slice(0, 300) });
  }
  return { fetched, failed };
}

/** 补齐缺失的 features/tags（只补竞彩场次，cache-first：已有有效缓存跳过）。 */
export async function fillMissingFeatures(cacheDir, engineRoot, dates) {
  const { node, fetcher } = fetcherCmd(engineRoot);
  const done = [];
  const failed = [];
  for (const date of dates) {
    // 只处理该日历日里竞彩号非空且缺数据的场次；无竞彩场次直接跳过，避免抓北单/无号。
    const jcMatches = readMatchesFile(cacheDir, date).filter((m) => m && m.jingcai_number);
    const missing = jcMatches.filter((m) => !hasValidFeature(cacheDir, m.lota_id) || !hasTags(cacheDir, m.lota_id));
    if (!missing.length) {
      done.push(date);
      continue;
    }
    const r = await runPrivateCmd(node, [fetcher, "prefetch", date, "--jingcai"], engineRoot, {
      timeoutMs: 600000,
      env: { LOTA_DATA_ROOT: cacheDir },
    });
    if (r.code === 0) done.push(date);
    else failed.push({ date, code: r.code, stderr: String(r.stderr || "").slice(0, 300) });
  }
  return { done, failed };
}

// ── data_fetch 单例：同 day+mode 幂等复用 + in-flight 去重 ──
// live 幂等窗口：2 分钟内复用同一次准备结果（对齐未开赛 TTL；超窗重新强制刷新拒绝旧赔率）。
const LIVE_TTL_MS = 120000;
const __prepareCache = new Map();    // key -> { result, at }
const __prepareInflight = new Map(); // key -> Promise<result>

function prepareKey({ cacheDir, mode, day, jingcaiOnly }) {
  return `${cacheDir}|${mode}|${day}|${jingcaiOnly ? 1 : 0}`;
}

function prepareTtl(mode) {
  // 历史/回放缓存不可变 → 永久复用；live → 短 TTL
  return mode === "live" ? LIVE_TTL_MS : Infinity;
}

/** 手动失效某足球日的准备缓存（供测试/强制重取）。 */
export function invalidatePreparedDay({ cacheDir, day, mode = "replay", jingcaiOnly = true } = {}) {
  __prepareCache.delete(prepareKey({ cacheDir, mode, day, jingcaiOnly }));
}

/**
 * 准备某足球日数据（LLM 前的确定性数据边界）——单例入口。
 *
 * 语义：
 *   - 幂等缓存：同 cacheDir+day+mode+jingcaiOnly 在 TTL 内直接复用上次结果（live 2 分钟 / 历史永久），
 *     返回值带 reused:true；
 *   - in-flight 去重：同 key 正在准备时并发调用复用同一 Promise，不重复拉取；
 *   - force:true 跳过缓存与去重，强制重取。
 *
 * @param {object} opts cacheDir / engineRoot / day / mode("live"|"replay") / jingcaiOnly / pythonBin / force / onProgress
 */
export async function prepareDay(opts = {}) {
  const { cacheDir, day, mode = "replay", jingcaiOnly = true, force = false } = opts;
  const progress = typeof opts.onProgress === "function" ? opts.onProgress : () => {};
  const key = prepareKey({ cacheDir, mode, day, jingcaiOnly });

  if (!force) {
    const hit = __prepareCache.get(key);
    if (hit && (Date.now() - hit.at) < prepareTtl(mode)) {
      progress({ phase: "复用已准备数据（单例）", detail: `${day} ${mode}` });
      return { ...hit.result, reused: true };
    }
    const inflight = __prepareInflight.get(key);
    if (inflight) {
      progress({ phase: "等待进行中的数据准备（单例）", detail: `${day} ${mode}` });
      return inflight;
    }
  }

  const run = (async () => {
    const result = await prepareDayCore({ ...opts, mode, jingcaiOnly });
    __prepareCache.set(key, { result, at: Date.now() });
    return result;
  })();
  if (!force) __prepareInflight.set(key, run);
  try {
    return await run;
  } finally {
    __prepareInflight.delete(key);
  }
}

/** 准备某足球日数据（核心实现，无单例语义）。 */
async function prepareDayCore({ cacheDir, engineRoot, day, mode = "replay", jingcaiOnly = true, pythonBin = "python", onProgress } = {}) {
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const calDates = calendarDatesForDay(day);
  const warnings = [];
  const fetches = { matches: [], features: [] };

  if (mode === "live") {
    // live：强制刷新 + 竞彩预取（Python prefetch 的 live-strict / TTL 语义，拒绝旧赔率）
    progress({ phase: "live 预取", detail: `${day}（强制刷新）` });
    const r = await runPrivateCmd(pythonBin, ["dsfootball_cli.py", "prefetch", day, "--jingcai"], engineRoot, { timeoutMs: 900000 });
    if (r.code !== 0) {
      warnings.push(`live prefetch 失败(code=${r.code}): ${String(r.stderr || "").slice(0, 300)}`);
    }
  } else {
    // replay / 历史：matches 缓存优先，缺了才从 URL 拉一次
    progress({ phase: "拉比赛缓存", detail: calDates.join("、") });
    const m = await fetchMissingMatches(cacheDir, engineRoot, calDates);
    fetches.matches = m.fetched;
    for (const f of m.failed) warnings.push(`refresh-date 失败 ${f.date}: ${f.stderr}`);
    // features/tags 缓存优先，缺的按竞彩场次补齐
    progress({ phase: "补 features/tags", detail: calDates.join("、") });
    const f = await fillMissingFeatures(cacheDir, engineRoot, calDates);
    fetches.features = f.done;
    for (const ff of f.failed) warnings.push(`prefetch 失败 ${ff.date}: ${ff.stderr}`);
  }

  progress({ phase: "过滤竞彩", detail: day });
  const windowTotal = jingcaiWindowMatches(cacheDir, day, { jingcaiOnly: false }).length;
  const matches = jingcaiOnly
    ? jingcaiWindowMatches(cacheDir, day, { strip: true, jingcaiOnly: true })
    : jingcaiWindowMatches(cacheDir, day, { strip: true, jingcaiOnly: false });
  const jingcaiCount = matches.filter((m) => m && m.jingcai_number).length;

  // 数据完整性自检：LLM 之前就暴露缺数据，而不是让模型读到空列表再猜
  const missingFeat = matches.filter((m) => !hasValidFeature(cacheDir, m.lota_id)).length;
  const missingTags = matches.filter((m) => !hasTags(cacheDir, m.lota_id)).length;
  if (matches.length === 0) warnings.push("窗口内无竞彩比赛（可能缓存缺失或当天无竞彩场次）");
  if (missingFeat > 0) warnings.push(`${missingFeat} 场缺 features（LLM 将看不到赔率段）`);
  if (missingTags > 0) warnings.push(`${missingTags} 场缺 tags 段落`);

  return {
    day,
    mode,
    calendar_dates: calDates,
    window_total: windowTotal,
    jingcai_count: jingcaiCount,
    excluded_count: windowTotal - jingcaiCount,
    matches,
    fresh: mode === "live",
    warnings,
    fetches,
  };
}

/**
 * 准备回放范围数据：一次性把 [start, end] 涉及的日历日期比赛 + 竞彩 features/tags 拉齐，
 * 之后逐日循环只读缓存，不触网。
 */
export async function prepareRange({ cacheDir, engineRoot, start, end, pythonBin = "python", onProgress } = {}) {
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const dates = new Set();
  let d = start;
  while (d <= end) {
    for (const cd of calendarDatesForDay(d)) dates.add(cd);
    d = addDays(d, 1);
  }
  const dateList = [...dates].sort();
  progress({ phase: "拉比赛缓存（范围）", done: 0, total: dateList.length, detail: `${start} ~ ${end}` });
  const m = await fetchMissingMatches(cacheDir, engineRoot, dateList);
  progress({ phase: "补 features/tags（范围）", done: m.fetched.length, total: dateList.length });
  const f = await fillMissingFeatures(cacheDir, engineRoot, dateList);
  progress({ phase: "范围数据就绪", done: dateList.length, total: dateList.length });
  return {
    dates: dateList,
    matches_fetched: m.fetched,
    matches_failed: m.failed,
    features_fetched: f.done,
    features_failed: f.failed,
  };
}
