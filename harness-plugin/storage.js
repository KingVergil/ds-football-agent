/**
 * ds-agents storage 域打开 + 数据迁移（harness_js_reconstruction.md §7）。
 *
 * setupDomains(ctx)  在插件 apply 时打开全部域（json 后端，host 已挂载），
 *                    返回 name → Promise<Domain>，供工具 await 使用；close 挂在 fiber 上。
 * migrateFromPython  把 python-engine/data 下的 roles/factor_memory/reflection_memory
 *                    一次性迁进 storage 域（幂等：put 全量覆盖）。
 */
import { existsSync, readFileSync, readdirSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { DS_DOMAINS } from "./domains.js";
import { orderLimitsFor } from "./fundLimits.js";

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function writeJson(path, value) {
  writeFileSync(path, JSON.stringify(value, null, 2), "utf8");
}

/** 真实参与分析的 7 只狗（排除 _simwk、_sim0804、_snapshot、__mt_*、test_verify 等快照）。 */
export const DS_REAL_DOGS = ["alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗"];

/** 打开全部 storage 域，返回 name → Promise<Domain>。 */
export function setupDomains(ctx) {
  const handles = {};
  for (const spec of DS_DOMAINS) {
    handles[spec.name] = ctx.storageDomain.open(spec);
  }
  // 吞掉 open 的 unhandled rejection 噪音；工具 await 时仍能拿到真实 rejection。
  Object.values(handles).forEach((p) => p.catch(() => {}));
  ctx.effect(() => () => {
    Object.values(handles).forEach((p) => p.then((d) => d.close()).catch(() => {}));
  }, "ds-domains.close");
  return handles;
}

/** 查资金现状（读 ds_roles 域 + fundLimits，替代 Python capital_query.py 桥）。 */
export async function capitalQuery(handles, dog) {
  const domain = await handles["ds_roles"];
  const role = domain.table("roles").get(dog);
  if (!role) return { error: `角色 ${dog} 不存在（storage 域未迁移？先跑 ds_migrate_storage）` };
  const orders = role.orders || [];
  const unsettled = orders.filter((o) => !o.settled_at);
  const lockedExposure = unsettled.reduce((s, o) => s + Number(o.bet_size || 0), 0);
  const capital = Number(role.capital || 0);
  const limits = orderLimitsFor(dog);
  const round2 = (x) => Math.round(x * 100) / 100;
  return {
    user: dog,
    capital: round2(capital),
    locked_exposure: round2(lockedExposure),
    full_capital: round2(capital + lockedExposure),
    unsettled_count: unsettled.length,
    limits: {
      max_exposure_pct: limits.max_exposure_pct,
      truncate: limits.truncate,
      max_orders: limits.max_orders,
      min_orders: limits.min_orders,
    },
  };
}

/** 把 Python 数据文件迁进 storage 域（roles/factor_memory/reflection_memory）。默认只迁 7 只真实狗。 */
export async function migrateFromPython(handles, cacheDir, { dryRun = false, dogs = DS_REAL_DOGS } = {}) {
  const rolesDir = join(cacheDir, "roles");
  const allDogs = existsSync(rolesDir)
    ? readdirSync(rolesDir).filter((d) => existsSync(join(rolesDir, d, `${d}.json`)))
    : [];
  // dogs 传入 null 表示迁移全部（含快照）
  const dogList = dogs === null ? allDogs : dogs.filter((d) => existsSync(join(rolesDir, d, `${d}.json`)));

  const [rolesDomain, factorsDomain, reflectionsDomain, slugsDomain] = await Promise.all([
    handles["ds_roles"],
    handles["ds_factors"],
    handles["ds_reflections"],
    handles["ds_slugs"],
  ]);
  const rolesTable = rolesDomain.table("roles");
  const factorsTable = factorsDomain.table("factors");
  const reflectionsTable = reflectionsDomain.table("reflections");
  const slugsTable = slugsDomain.table("slugs");

  const result = {
    dogs: [],
    rolesMigrated: 0,
    factorsMigrated: 0,
    reflectionsMigrated: 0,
    slugsMigrated: 0,
    errors: [],
  };

  for (const dog of dogList) {
    try {
      const role = readJson(join(rolesDir, dog, `${dog}.json`));
      if (!role) continue;
      const fm = readJson(join(rolesDir, dog, "memory", "factor_memory.json"));
      const rm = readJson(join(rolesDir, dog, "memory", "reflection_memory.json"));
      const sm = readJson(join(rolesDir, dog, "memory", "slug_memory.json"));

      if (!dryRun) {
        await rolesTable.put(dog, role);
        if (fm) await factorsTable.put(dog, fm);
        if (rm) await reflectionsTable.put(dog, rm);
        if (sm) await slugsTable.put(dog, sm);
      }
      result.dogs.push(dog);
      result.rolesMigrated += 1;
      if (fm) result.factorsMigrated += 1;
      if (rm) result.reflectionsMigrated += 1;
      if (sm) result.slugsMigrated += 1;
    } catch (e) {
      result.errors.push(`${dog}: ${(e && e.message) || e}`);
    }
  }
  return result;
}

/**
 * 反向迁移：把 storage 域（ds_roles/ds_factors/ds_reflections/ds_factor_registry）
 * 还原成 Python 文件（data/roles/<dog>/<dog>.json + memory/*.json + factors/fac_*.json）。
 * 默认只导出 DS_REAL_DOGS（7 只真实狗），跳过临时快照。
 */
export async function exportToPython(handles, cacheDir, { dogs = DS_REAL_DOGS, dryRun = false } = {}) {
  const [rolesDomain, factorsDomain, reflectionsDomain, registryDomain, slugsDomain] = await Promise.all([
    handles["ds_roles"],
    handles["ds_factors"],
    handles["ds_reflections"],
    handles["ds_factor_registry"],
    handles["ds_slugs"],
  ]);
  const rolesTable = rolesDomain.table("roles");
  const factorsTable = factorsDomain.table("factors");
  const reflectionsTable = reflectionsDomain.table("reflections");
  const registryTable = registryDomain.table("factors");
  const slugsTable = slugsDomain.table("slugs");

  const result = {
    dogs: [],
    rolesExported: 0,
    factorsExported: 0,
    reflectionsExported: 0,
    slugsExported: 0,
    registryExported: 0,
    errors: [],
  };

  for (const dog of dogs) {
    try {
      const role = rolesTable.get(dog);
      if (!role) {
        result.errors.push(`${dog}: ds_roles 无记录（先跑 ds_migrate_storage）`);
        continue;
      }
      const fm = factorsTable.get(dog);
      const rm = reflectionsTable.get(dog);
      const sm = slugsTable.get(dog);

      if (!dryRun) {
        const roleDir = join(cacheDir, "roles", dog);
        mkdirSync(roleDir, { recursive: true });
        writeJson(join(roleDir, `${dog}.json`), role);
        if (fm) {
          mkdirSync(join(roleDir, "memory"), { recursive: true });
          writeJson(join(roleDir, "memory", "factor_memory.json"), fm);
        }
        if (rm) {
          mkdirSync(join(roleDir, "memory"), { recursive: true });
          writeJson(join(roleDir, "memory", "reflection_memory.json"), rm);
        }
        if (sm) {
          mkdirSync(join(roleDir, "memory"), { recursive: true });
          writeJson(join(roleDir, "memory", "slug_memory.json"), sm);
        }
      }
      result.dogs.push(dog);
      result.rolesExported += 1;
      if (fm) result.factorsExported += 1;
      if (rm) result.reflectionsExported += 1;
      if (sm) result.slugsExported += 1;
    } catch (e) {
      result.errors.push(`${dog}: ${(e && e.message) || e}`);
    }
  }

  // 跨狗因子注册表 → data/factors/fac_*.json
  const regDir = join(cacheDir, "factors");
  for (const [facId, def] of registryTable.entries()) {
    try {
      if (!dryRun) {
        mkdirSync(regDir, { recursive: true });
        writeJson(join(regDir, `${facId}.json`), def);
      }
      result.registryExported += 1;
    } catch (e) {
      result.errors.push(`registry ${facId}: ${(e && e.message) || e}`);
    }
  }

  return result;
}
