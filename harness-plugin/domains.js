/**
 * ds-agents 的 storage 域定义（harness_js_reconstruction.md §7 数据分层）。
 *
 * 把 Python 文件状态迁到 DSH storageDomain：
 *   roles/<dog>.json                → ds-roles（按 dog 键控）
 *   roles/<dog>/memory/factor_memory.json  → ds-factors（按 dog 键控）
 *   roles/<dog>/memory/reflection_memory.json → ds-reflections（按 dog 键控）
 *   roles/<dog>/memory/slug_memory.json   → ds-slugs（按 dog 键控）
 *   factors/（跨狗注册表）          → ds-factor-registry（按 fac_id 键控）
 *
 * schema 对齐 python-engine/src/{role,memory,models}.py 的现有数据形状，
 * 用 .passthrough() 容忍 Python 侧新增字段，避免迁移时被 zod 严格校验卡住。
 */
import { defineDomain, domainTable } from "@deepseek-ai/dsh-storage-domain";
import { z } from "zod";

// ── 订单（role.orders[]）──
// 历史数据字段经常缺失（undefined）而非 null，用 nullish(=null+undefined) 容忍；
// 串关订单含 legs 等额外字段，靠 .passthrough() 放行。
const OrderSchema = z.object({
  lota_id: z.string().nullish(),
  bet_type: z.string().nullish(),
  pick: z.string().nullish(),
  odds: z.number().nullish(),
  handicap: z.number().nullish(),
  bet_size: z.number().nullish(),
  reason: z.string().nullish(),
  id: z.string().nullish(),
  created_at: z.string().nullish(),
  hit: z.boolean().nullish(),
  return_amount: z.number().nullish(),
  profit: z.number().nullish(),
  score: z.string().nullish(),
  settled_at: z.string().nullish(),
}).passthrough();

// ── 角色（roles/<dog>.json）──
const RoleSchema = z.object({
  name: z.string(),
  capital: z.number(),
  initial_capital: z.number(),
  system_prompt_name: z.string().default("baseline-v1"),
  alpha_mode: z.boolean().default(false),
  cross_factor_exclude: z.array(z.string()).default([]),
  updated_at: z.string().optional(),
  orders: z.array(OrderSchema).default([]),
}).passthrough();

// ── 因子表现（factor_perf[factor_id]）──
const FactorPerfSchema = z.object({
  total: z.number().default(0),
  hit: z.number().default(0),
  miss: z.number().default(0),
  push: z.number().default(0),
  profit: z.number().default(0),
  total_return: z.number().default(0),
  status: z.string().default("active"),
  desc: z.string().default(""),
  first_seen: z.string().optional(),
  last_seen: z.string().optional(),
  history: z.array(z.unknown()).default([]),
  aliases: z.array(z.string()).default([]),
  fac_id: z.string().optional(),
}).passthrough();

// ── 反思条目（reflection_memory.reflections[]）──
const ReflectionSchema = z.object({
  date: z.string(),
  reflection: z.string(),
  recorded_at: z.string().optional(),
  sample_count: z.number().nullable().optional(),
}).passthrough();

// ── 每日 slug 记忆（slug_memory.json：slug_stats + day_slugs）──
const SlugPerfSchema = z.object({
  appearances: z.number().default(0),
  profitable_days: z.number().default(0),
  loss_days: z.number().default(0),
  flat_days: z.number().default(0),
}).passthrough();
const SlugMemorySchema = z.object({
  updated_at: z.string().optional(),
  slug_stats: z.record(SlugPerfSchema).default({}),
  day_slugs: z.record(z.array(z.string())).default({}),
}).passthrough();

// ── 跨狗因子注册表条目（factors/fac_*.json）──
const FactorDefSchema = z.object({
  id: z.string(),
  slugs: z.array(z.string()).default([]),
  content: z.string().default(""),
}).passthrough();

/** 角色域：orders/capital/alpha_mode 等，按 dog 键控。 */
export const dsRoles = defineDomain({
  name: "ds_roles",
  version: 1,
  tables: {
    roles: domainTable(RoleSchema),
  },
});

/** 因子表现域：factor_perf + status，按 dog 键控。 */
export const dsFactors = defineDomain({
  name: "ds_factors",
  version: 1,
  tables: {
    factors: domainTable(z.object({
      factor_perf: z.record(FactorPerfSchema).default({}),
      updated_at: z.string().optional(),
    }).passthrough()),
  },
});

/** 反思域：reflections + money_lesson + sample_count，按 dog 键控。 */
export const dsReflections = defineDomain({
  name: "ds_reflections",
  version: 1,
  tables: {
    reflections: domainTable(z.object({
      reflections: z.array(ReflectionSchema).default([]),
      updated_at: z.string().optional(),
    }).passthrough()),
  },
});

/** slug 记忆域：slug_stats + day_slugs，按 dog 键控。 */
export const dsSlugs = defineDomain({
  name: "ds_slugs",
  version: 1,
  tables: {
    slugs: domainTable(SlugMemorySchema),
  },
});

/** 跨狗因子注册表域：按 fac_id 键控。 */
export const dsFactorRegistry = defineDomain({
  name: "ds_factor_registry",
  version: 1,
  tables: {
    factors: domainTable(FactorDefSchema),
  },
});

/** 全部域定义，按序打开。 */
export const DS_DOMAINS = [dsRoles, dsFactors, dsReflections, dsSlugs, dsFactorRegistry];
