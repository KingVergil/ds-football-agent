/**
 * 运行时狗注册表（「创建狗」功能的持久层）：cacheDir/dogs.json。
 *
 * 条目形状（与 config.roles 对齐，覆盖当前狗的全部可配置项）：
 *   { name, scope, initial_capital, alpha_mode, limits, enabled, emoji, c1, c2, created_at }
 *
 * 设计定稿（2026-08-18 grill 定稿）：
 *   - persona 唯一源 = Python roles/<狗>/persona.md，注册表不再存 persona；
 *   - limits 结构化存 roles/<狗>/<狗>.json，LangGraph 优先读文件、缺省用代码默认；
 *   - enabled 控制是否进全量默认列表（分析/结算/回放），新建狗默认观察期（enabled=false）；
 *   - 创建即同步：注册表 dogs.json + ds_roles + Python 角色（幂等补缺）。
 *
 * 读有 mtime 缓存（dashboard 4s 轮询 + 回放逐狗 personaFor 都走这里，避免每访读盘）；
 * 写是原子写（tmp + rename），防止 dashboard 轮询读到半截文件。
 */
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

import { beijingNowIso } from "./tools/shared.js";

/** 合法比赛范围取值（与 config.roles.scope 一致）。 */
export const DOG_SCOPES = ["jc", "beidan", "all"];

/** 新建狗默认资金约束（表单默认 max_exposure_pct=40%）。 */
export const DEFAULT_LIMITS = { max_exposure_pct: 40 };

/** 人设不允许为空：后端兜底模板（观察期保守默认，可在创建后改 persona.md）。 */
export const DEFAULT_PERSONA_TEMPLATE =
  "偏好稳健：只在信号明确（≥2 因子共振）时下注，单注不超过可用资金的 10%；" +
  "观察期（默认前 30 天）只下小注试探，单注不超过 5%，每天最多 2 注；" +
  "不追单、不报复性下注，连续亏损 3 天后主动减仓。";

let _cache = { path: "", mtimeMs: -1, dogs: [] };

export function registryPath(cacheDir) {
  return join(cacheDir, "dogs.json");
}

/** Python 角色目录（cacheDir 即 python-engine/data，角色根在其 roles/ 下）。 */
export function pythonRoleDir(cacheDir, name) {
  return join(cacheDir, "roles", name);
}

/** 读注册表（mtime 缓存；文件缺失/损坏返回 []）。 */
export function readDogRegistry(cacheDir) {
  const path = registryPath(cacheDir);
  let mtimeMs = -1;
  try { mtimeMs = statSync(path).mtimeMs; } catch { /* 缺失 */ }
  if (_cache.path === path && _cache.mtimeMs === mtimeMs) return _cache.dogs;
  let dogs = [];
  try {
    const raw = JSON.parse(readFileSync(path, "utf8"));
    if (Array.isArray(raw)) dogs = raw.filter((d) => d && typeof d.name === "string" && d.name.trim());
  } catch { dogs = []; }
  _cache = { path, mtimeMs, dogs };
  return dogs;
}

/** 写注册表（原子写：tmp + rename；mtime 缓存立即失效，下次读重新落盘读）。 */
export function writeDogRegistry(cacheDir, dogs) {
  const path = registryPath(cacheDir);
  mkdirSync(join(path, ".."), { recursive: true });
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`;
  writeFileSync(tmp, JSON.stringify(dogs, null, 2), "utf8");
  try {
    renameSync(tmp, path);
  } catch (e) {
    try { rmSync(tmp, { force: true }); } catch { /* 忽略清理失败 */ }
    throw e;
  }
  _cache = { path, mtimeMs: -1, dogs };
  return dogs;
}

/** 狗名合法性：中英文/数字/下划线/短横线，1-24 字符（防路径穿越/表 key 污染）。 */
export function isValidDogName(name) {
  return typeof name === "string" && /^[\u4e00-\u9fa5A-Za-z0-9_-]{1,24}$/.test(name.trim());
}

/** limits 白名单归一化：只保留已支持的键，数值非法/缺省置 null（=不启用该约束）。 */
export function normalizeLimits(raw) {
  const src = (raw && typeof raw === "object") ? raw : {};
  const out = {};
  const num = (v) => {
    if (v === null || v === undefined || v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  out.max_exposure_pct = num(src.max_exposure_pct);
  out.truncate = Boolean(src.truncate);
  out.max_orders = num(src.max_orders);
  out.min_orders = num(src.min_orders);
  return out;
}

/**
 * 幂等补建 Python 角色（roles/<狗>/）：persona.md + <狗>.json。
 * 已存在的文件不动（资金/订单/人设以 Python 侧为准）。
 *
 * @param {string} cacheDir 数据根（python-engine/data）
 * @param {object} entry 注册表条目（含 name/scope/initial_capital/alpha_mode/limits/enabled）
 * @param {string} personaText 人设原文（空则用默认模板；仅 persona.md 缺失/为空时写入）
 * @returns {{ created: boolean, error?: string }}
 */
export function ensurePythonRole(cacheDir, entry, personaText = "") {
  try {
    const name = String(entry.name || "").trim();
    if (!isValidDogName(name)) return { created: false, error: "狗名不合法，无法建角色" };
    const dir = pythonRoleDir(cacheDir, name);
    mkdirSync(dir, { recursive: true });

    // persona.md：唯一人设源；缺失/为空才补（默认模板兜底，不允许空）
    const personaPath = join(dir, "persona.md");
    if (!existsSync(personaPath) || !readFileSync(personaPath, "utf8").trim()) {
      writeFileSync(personaPath, (personaText || DEFAULT_PERSONA_TEMPLATE).trim() + "\n", "utf8");
    }

    // <狗>.json：已存在则不动（保留资金/订单），缺失则按注册表初始化
    const rolePath = join(dir, `${name}.json`);
    if (!existsSync(rolePath)) {
      const initialCapital = Number(entry.initial_capital) > 0
        ? Math.round(Number(entry.initial_capital) * 100) / 100
        : 10000;
      const limits = normalizeLimits(entry.limits);
      writeFileSync(rolePath, JSON.stringify({
        name,
        capital: initialCapital,
        initial_capital: initialCapital,
        system_prompt_name: "baseline-v1",
        alpha_mode: Boolean(entry.alpha_mode),
        cross_factor_exclude: [],
        limits,
        scope: DOG_SCOPES.includes(entry.scope) ? entry.scope : "jc",
        enabled: Boolean(entry.enabled),
        status: entry.status || (entry.enabled ? "live" : "sandbox"),
        orders: [],
        updated_at: beijingNowIso(),
      }, null, 2), "utf8");
    }
    return { created: true };
  } catch (e) {
    return { created: false, error: String((e && e.message) || e) };
  }
}

/**
 * 同步 Python 角色配置字段（编辑狗时用）：把 limits/enabled/scope/alpha_mode
 * 写进已存在的 roles/<狗>/<狗>.json；资金/订单/人设不动。原子写。
 */
export function syncPythonRoleConfig(cacheDir, entry) {
  const name = String((entry && entry.name) || "").trim();
  const rolePath = join(pythonRoleDir(cacheDir, name), `${name}.json`);
  if (!existsSync(rolePath)) return ensurePythonRole(cacheDir, entry, "");
  try {
    const data = JSON.parse(readFileSync(rolePath, "utf8"));
    data.alpha_mode = Boolean(entry.alpha_mode);
    data.status = entry.status || (entry.enabled ? "live" : "sandbox");
    data.limits = normalizeLimits(entry.limits);
    if (DOG_SCOPES.includes(entry.scope)) data.scope = entry.scope;
    data.enabled = Boolean(entry.enabled);
    data.updated_at = beijingNowIso();
    const tmp = `${rolePath}.tmp-${process.pid}-${Date.now()}`;
    writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
    renameSync(tmp, rolePath);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

/** Python roles/ 下全部目录名（撞名防护用，含 __mt_* / _sim* / test_verify 等）。 */
export function pythonRoleDirNames(cacheDir) {
  try {
    const root = join(cacheDir, "roles");
    if (!existsSync(root)) return [];
    return readdirSync(root, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name);
  } catch {
    return [];
  }
}

/**
 * 创建新狗：校验 → 写注册表（原子）→ 幂等补建 Python 角色（roles/<狗>/persona.md + <狗>.json）。
 * storage 域（ds_roles）已退役：数据唯一真源 = Python 文件，不再双写。
 *
 * @param {object} spec { name, persona?, scope?, initial_capital?, alpha_mode?, limits?, enabled?, emoji?, c1?, c2? }
 * @param {object} opts { existingNames: string[], defaultPersona?: string }
 * @returns { ok, dog?, error? }
 */
export async function createDog(cacheDir, _domainHandles, spec = {}, { existingNames = [], defaultPersona = "" } = {}) {
  const name = String((spec && spec.name) || "").trim();
  if (!isValidDogName(name)) {
    return { ok: false, error: "狗名无效：仅限中英文/数字/下划线/短横线，1-24 字符" };
  }
  if (existingNames.includes(name)) {
    return { ok: false, error: `狗「${name}」已存在，换一个名字` };
  }

  const scope = DOG_SCOPES.includes(spec.scope) ? spec.scope : "jc";
  const initialRaw = Number(spec.initial_capital);
  const initialCapital = Number.isFinite(initialRaw) && initialRaw > 0
    ? Math.round(initialRaw * 100) / 100
    : 10000;
  const personaText = (typeof spec.persona === "string" && spec.persona.trim())
    ? spec.persona.trim()
    : ((typeof defaultPersona === "string" && defaultPersona.trim()) ? defaultPersona.trim() : DEFAULT_PERSONA_TEMPLATE);

  const entry = {
    name,
    scope,
    initial_capital: initialCapital,
    alpha_mode: Boolean(spec.alpha_mode),
    limits: normalizeLimits(spec.limits),
    enabled: Boolean(spec.enabled),
    status: spec.status || (spec.enabled ? "live" : "sandbox"),
    emoji: typeof spec.emoji === "string" ? spec.emoji.slice(0, 8) : "",
    c1: typeof spec.c1 === "string" ? spec.c1 : "",
    c2: typeof spec.c2 === "string" ? spec.c2 : "",
    created_at: beijingNowIso(),
  };

  const dogs = readDogRegistry(cacheDir);
  if (dogs.some((d) => d.name === name)) {
    return { ok: false, error: `狗「${name}」已在注册表，换一个名字` };
  }
  writeDogRegistry(cacheDir, [...dogs, entry]);

  // Python 角色幂等补建（persona.md + <狗>.json）
  const py = ensurePythonRole(cacheDir, entry, personaText);
  if (py.error) {
    return { ok: false, error: `Python 角色补建失败: ${py.error}`, dog: entry };
  }
  return { ok: true, dog: { ...entry, persona: personaText } };
}

/**
 * 编辑狗配置：合并注册表条目并写盘（原子），同步 Python 角色配置字段
 * （limits/enabled/scope/alpha_mode 写进 <狗>.json；persona 写 persona.md）。
 * 资金/订单一律不动。
 *
 * @returns { ok, dog?, error? }
 */
export function updateDogEntry(cacheDir, name, patch = {}) {
  const dogs = readDogRegistry(cacheDir);
  const idx = dogs.findIndex((d) => d.name === name);
  if (idx < 0) return { ok: false, error: `狗「${name}」不在注册表` };
  const prev = dogs[idx];
  const next = {
    ...prev,
    scope: DOG_SCOPES.includes(patch.scope) ? patch.scope : prev.scope,
    alpha_mode: patch.alpha_mode === undefined ? prev.alpha_mode : Boolean(patch.alpha_mode),
    limits: patch.limits !== undefined ? normalizeLimits(patch.limits) : prev.limits,
    enabled: patch.enabled === undefined ? prev.enabled : Boolean(patch.enabled),
    status: patch.status || prev.status,
    emoji: typeof patch.emoji === "string" ? patch.emoji.slice(0, 8) : prev.emoji,
    c1: typeof patch.c1 === "string" ? patch.c1 : prev.c1,
    c2: typeof patch.c2 === "string" ? patch.c2 : prev.c2,
  };
  const updated = [...dogs];
  updated[idx] = next;
  writeDogRegistry(cacheDir, updated);

  const py = syncPythonRoleConfig(cacheDir, next);
  if (py.error) return { ok: false, error: `Python 角色同步失败: ${py.error}`, dog: next };

  // persona 单独写 persona.md（更新覆盖；删除时还原默认模板，不允许空）
  if (typeof patch.persona === "string") {
    try {
      const dir = pythonRoleDir(cacheDir, name);
      mkdirSync(dir, { recursive: true });
      const text = patch.persona.trim() || DEFAULT_PERSONA_TEMPLATE;
      writeFileSync(join(dir, "persona.md"), text + "\n", "utf8");
    } catch (e) {
      return { ok: false, error: `persona.md 写入失败: ${(e && e.message) || e}`, dog: next };
    }
  }
  return { ok: true, dog: next };
}

/**
 * 删除狗：仅移注册表。历史订单/因子/资金（ds_roles + Python roles/）一律保留。
 * 已不在注册表的狗视为成功（幂等）。
 */
export function deleteDogEntry(cacheDir, name) {
  const dogs = readDogRegistry(cacheDir);
  const next = dogs.filter((d) => d.name !== name);
  if (next.length === dogs.length) return { ok: true, removed: false };
  writeDogRegistry(cacheDir, next);
  return { ok: true, removed: true };
}

/**
 * 设置狗状态（转正/归档）：同步注册表条目 + 角色文件 status 字段。
 * @returns {{ ok:boolean, error?:string }}
 */
export function setDogStatus(cacheDir, name, status) {
  if (!["live", "sandbox", "archived"].includes(status)) {
    return { ok: false, error: `status 必须是 live/sandbox/archived: ${status}` };
  }
  const dogs = readDogRegistry(cacheDir);
  const idx = dogs.findIndex((d) => d.name === name);
  if (idx >= 0) {
    const next = [...dogs];
    next[idx] = { ...next[idx], status };
    writeDogRegistry(cacheDir, next);
  }
  const rolePath = join(pythonRoleDir(cacheDir, name), `${name}.json`);
  try {
    if (existsSync(rolePath)) {
      const data = JSON.parse(readFileSync(rolePath, "utf8"));
      data.status = status;
      if (status === "live") data.enabled = true;
      const tmp = `${rolePath}.tmp-${process.pid}-${Date.now()}`;
      writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
      renameSync(tmp, rolePath);
    }
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
  return { ok: true, name, status };
}
