/**
 * @module tools/shared
 *
 * 工具分组模块共享的本地缓存读取辅助 + 渲染器。
 * 从旧 index.js 迁出，供 roles / deterministic / headless 三组复用。
 */
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

/** 缓存格式里的 kind → 子目录名（与 cache_format_spec.md 一致）。 */
export const KIND_DIR = {
  matches: "matches",
  features: "features",
  tags: "tags",
  predicts: "predicts",
  orders: "orders",
};

/** 宽容的对象 schema（返回结构随字段动态变化；挂载后可按需收紧）。 */
export const LOOSE_OBJECT = { type: "object", additionalProperties: true };

/** 自主 LLM stream 温度（旁路子程序式调用；主循环/分析 subagent 温度由会话配置决定）。 */
export const LLM_TEMPERATURES = {
  analyze: 0.1,   // 分析（预留：当前走 subagent 主循环，无 per-call 温度旋钮）
  reflect: 0.6,   // 因子生成（反思）
  induction: 0.1, // 因子归纳（判重）
  review: 0.1,    // 因子退役（结构性评估）
};

/** 北京时间 ISO（与 Python datetime.now() 同口径，不依赖宿主机时区）。 */
export function beijingNowIso() {
  const d = new Date(Date.now() + 8 * 3600 * 1000);
  return d.toISOString().slice(0, 19); // "YYYY-MM-DDTHH:mm:ss"
}

export function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

/** 读 matches/<date>.json，兼容 list 与 {matches:[...]} 两种顶层。 */
export function readMatches(cacheDir, date) {
  const raw = readJson(join(cacheDir, KIND_DIR.matches, `${date}.json`));
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.matches)) return raw.matches;
  return [];
}

/** 列举某 kind 下的全部 key（文件名去后缀）。 */
export function listKeys(cacheDir, kind) {
  const dir = join(cacheDir, KIND_DIR[kind]);
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.slice(0, -".json".length))
    .sort();
}

/**
 * 归一化 features 的三种历史形状（对齐 cache_format_spec.md §2）：
 *   A 顶层字段 / B data 子对象 / C 负缓存桩。
 */
export function normalizeFeature(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw._api_failed) {
    return {
      lota_id: raw.lota_id ?? null,
      compact_fet: "",
      score: "",
      _cached_at: raw._cached_at ?? null,
      _api_failed: true,
    };
  }
  const inner = raw.data && typeof raw.data === "object" ? raw.data : {};
  const match = raw.match || inner.match || null;
  const compact_fet = raw.compact_fet || inner.compact_fet || "";
  const score = raw.score || inner.score || "";
  const out = {
    lota_id: raw.lota_id ?? null,
    compact_fet,
    score,
    _cached_at: raw._cached_at ?? null,
  };
  if (match) out.match = match;
  for (const k of ["success", "lang", "metadata", "api_info"]) {
    if (raw[k] !== undefined) out[k] = raw[k];
  }
  return out;
}

/** 按 lota_id 找比赛：features 优先，再扫最近 30 个 matches 日期文件。 */
export function findMatch(cacheDir, lotaId) {
  const feat = normalizeFeature(readJson(join(cacheDir, KIND_DIR.features, `${lotaId}.json`)));
  if (feat && feat.match) return feat.match;
  const dates = listKeys(cacheDir, "matches").slice(-30);
  for (const date of dates) {
    for (const m of readMatches(cacheDir, date)) {
      if (m.lota_id === lotaId) return m;
    }
  }
  return null;
}

/** 读 tags/<id>.json → {sections}（无则空）。 */
export function readSections(cacheDir, lotaId) {
  const raw = readJson(join(cacheDir, KIND_DIR.tags, `${lotaId}.json`));
  return (raw && raw.sections) || {};
}

export function truncate(text, max) {
  return text.length > max ? `${text.slice(0, max)}\n…[truncated]` : text;
}

/** 通用 JSON 渲染：把返回值 pretty 打印成模型可见文本。 */
export function jsonRender(max = 4000) {
  return (_args, value) => [
    { type: "text", text: truncate(JSON.stringify(value, null, 2), max) },
  ];
}
