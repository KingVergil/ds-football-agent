/**
 * @module tools/roles
 *
 * 【User Role】角色解析层（薄壳架构）：只负责从本地文件解析狗列表 / 人设 / 配置，
 * 供 dashboard 与回放入口使用。固定流（分析/结算/因子）不再注册 LLM 角色工具——
 * 执行入口只有 dashboard 表单（POST /ds-run → python 桥）。
 *
 * 外置入口（都可选，缺省=旧行为）：
 *   - config.roles: [{ name, scope?, initial_capital?, alpha_mode?, limits?, enabled?, emoji?, c1?, c2? }]
 *     —— 覆盖默认狗列表 + 日常比赛范围 + 展示配色
 *   - config.personaDir: 人设根目录（默认 cacheDir/roles），persona.md 从此目录读
 */
import { readFileSync, existsSync } from "node:fs";
import { resolve, join } from "node:path";
import { readDogRegistry, normalizeLimits, DEFAULT_LIMITS } from "../dogRegistry.js";

/** 内置默认狗名单（历史上 7 只单关狗）。公开仓库不携带任何狗数据：
 *  本地 roles/<狗>/<狗>.json 不存在时，默认狗不会进入任何列表（见 defaultDogExists）。 */
export const DS_REAL_DOGS = ["alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗"];

/** 内置默认狗是否在本地真实存在（roles/<狗>/<狗>.json）。 */
function defaultDogExists(cacheDir, name) {
  try {
    return existsSync(join(cacheDir, "roles", name, `${name}.json`));
  } catch {
    return false;
  }
}

/** 读 persona 文本（对齐 python role.persona_text）；personaDir 可外置（默认 cacheDir/roles）。 */
function readPersona(cacheDir, dog, personaDir) {
  const root = personaDir || join(cacheDir, "roles");
  const p = join(root, dog, "persona.md");
  if (!existsSync(p)) return "";
  const text = readFileSync(p, "utf8").trim();
  return text ? `## 🎯 个人偏好\n\n${text}` : "";
}

/**
 * 解析角色配置，返回统一的角色访问对象。
 * @param {object} config 插件 config
 * @param {string} cacheDir 缓存根目录
 * @returns {{
 *   dogs: string[], configured: boolean, hasRegistry: boolean,
 *   configDogs: string[], registryDogs: string[],
 *   personaDir: string,
 *   personaFor: (dog:string)=>string, scopeFor: (dog:string)=>string,
 *   initialCapitalFor: (dog:string)=>number|null, displayFor: (dog:string)=>object,
 *   alphaModeFor: (dog:string)=>boolean, enabledFor: (dog:string)=>boolean,
 *   limitsFor: (dog:string)=>object|null,
 * }}
 *
 * 合并来源（优先级：config.roles 内联 > 运行时注册表 cacheDir/dogs.json）：
 *   - dogs：config.roles 配置了哪些就展示哪些（+ 注册表新增狗）；未配置 = 7 只真狗 + 注册表新增狗。
 *   - 注册表由「创建狗」功能写入，dogs 是惰性 getter —— 每次访问重新读注册表，
 *     新建的狗无需重启即可被 dashboard / 工具默认列表看到。
 *   - persona 唯一源 = roles/<狗>/persona.md（注册表/config 内联 persona 不再生效）。
 */
export function resolveRoles(config = {}, cacheDir = "") {
  const rolesCfg = Array.isArray(config.roles) && config.roles.length ? config.roles : null;
  const byName = new Map();
  if (rolesCfg) {
    for (const r of rolesCfg) {
      if (r && typeof r.name === "string" && r.name) byName.set(r.name, r);
    }
  }
  const personaDir = config.personaDir ? resolve(config.personaDir) : join(cacheDir, "roles");
  const configured = Boolean(rolesCfg);

  const registryDogs = () => readDogRegistry(cacheDir);
  const registryByName = () => {
    const m = new Map();
    for (const r of registryDogs()) m.set(r.name, r);
    return m;
  };

  /** 完整狗列表（惰性：每次访问合并最新注册表）。 */
  const dogList = () => {
    const reg = registryDogs().map((r) => r.name);
    if (rolesCfg) {
      return [...byName.keys(), ...reg.filter((n) => !byName.has(n))];
    }
    return [...DS_REAL_DOGS.filter((n) => defaultDogExists(cacheDir, n)), ...reg.filter((n) => !DS_REAL_DOGS.includes(n))];
  };

  /** 全量默认列表（分析/结算/回放）：生产狗 + enabled 注册表狗；观察狗不默认进。 */
  const enabledDogList = () => {
    const reg = registryDogs();
    const enabledReg = reg.filter((r) => r.enabled !== false).map((r) => r.name);
    if (rolesCfg) {
      const cfgEnabled = [...byName.entries()]
        .filter(([, r]) => r.enabled !== false)
        .map(([n]) => n);
      return [...cfgEnabled, ...enabledReg.filter((n) => !byName.has(n))];
    }
    return [...DS_REAL_DOGS.filter((n) => defaultDogExists(cacheDir, n)), ...enabledReg.filter((n) => !DS_REAL_DOGS.includes(n))];
  };

  const initialCapitalFor = (dog) => {
    const reg = registryByName().get(dog);
    if (reg) {
      const n = Number(reg.initial_capital);
      if (Number.isFinite(n)) return n;
    }
    const r = byName.get(dog);
    const n = Number(r && r.initial_capital);
    return Number.isFinite(n) ? n : null;
  };

  const displayFor = (dog) => {
    const reg = registryByName().get(dog);
    const src = reg || byName.get(dog) || {};
    const out = {};
    if (typeof src.emoji === "string" && src.emoji) out.emoji = src.emoji;
    if (typeof src.c1 === "string" && src.c1) out.c1 = src.c1;
    if (typeof src.c2 === "string" && src.c2) out.c2 = src.c2;
    return out;
  };

  const personaFor = (dog) => {
    return readPersona(cacheDir, dog, personaDir);
  };

  const scopeFor = (dog) => {
    const reg = registryByName().get(dog);
    if (reg && reg.scope) return reg.scope;
    const r = byName.get(dog);
    return r && r.scope ? r.scope : "jc";
  };

  const alphaModeFor = (dog) => {
    const reg = registryByName().get(dog);
    if (reg) return Boolean(reg.alpha_mode);
    const r = byName.get(dog);
    return Boolean(r && r.alpha_mode);
  };

  const enabledFor = (dog) => {
    const st = roleStatusOf(cacheDir, dog);
    if (st) return st === "live";
    const reg = registryByName().get(dog);
    if (reg) return Boolean(reg.enabled);
    const r = byName.get(dog);
    if (r && typeof r.enabled === "boolean") return r.enabled;
    return true;
  };

  const limitsFor = (dog) => {
    const reg = registryByName().get(dog);
    if (reg && reg.limits) return normalizeLimits(reg.limits);
    const r = byName.get(dog);
    if (r && r.limits) return normalizeLimits(r.limits);
    return { ...DEFAULT_LIMITS };
  };

  return {
    get dogs() { return dogList(); },
    get enabledDogs() { return enabledDogList(); },
    configured,
    get hasRegistry() { return registryDogs().length > 0; },
    get configDogs() { return [...byName.keys()]; },
    get registryDogs() { return registryDogs().map((r) => r.name); },
    personaDir,
    personaFor,
    scopeFor,
    initialCapitalFor,
    displayFor,
    alphaModeFor,
    enabledFor,
    limitsFor,
  };
}

/** 读角色文件 status（live/sandbox/archived）；缺 status 时由 enabled 派生。 */
function roleStatusOf(cacheDir, dog) {
  try {
    const p = join(cacheDir, "roles", dog, `${dog}.json`);
    if (!existsSync(p)) return null;
    const r = JSON.parse(readFileSync(p, "utf8"));
    if (r && ["live", "sandbox", "archived"].includes(r.status)) return r.status;
    if (r && typeof r.enabled === "boolean") return r.enabled ? "live" : "sandbox";
    return null;
  } catch {
    return null;
  }
}
