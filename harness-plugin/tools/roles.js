/**
 * @module tools/roles
 *
 * 【User Role】角色数据层：人设 / 记忆 / 资金工具 + 可外置修改的角色解析层。
 *
 * 外置入口（都可选，缺省=旧行为）：
 *   - config.roles: [{ name, persona?, scope?, initial_capital? }] —— 覆盖默认狗列表 + 人设文本 + 日常比赛范围
 *   - config.personaDir: 人设根目录（默认 cacheDir/roles），persona.md 从此目录读
 */
import { resolve, join } from "node:path";
import { DS_REAL_DOGS } from "../storage.js";
import { readPersona } from "../reflect.js";
import { capitalQuery } from "../storage.js";
import { memoryQuery } from "../memory.js";
import { withTask } from "../taskStatus.js";
import { LOOSE_OBJECT, jsonRender } from "./shared.js";

/**
 * 解析角色配置，返回统一的角色访问对象。
 * @param {object} config 插件 config
 * @param {string} cacheDir 缓存根目录
 * @returns {{ dogs: string[], personaDir: string, personaFor: (dog:string)=>string, scopeFor: (dog:string)=>string }}
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
  const dogs = rolesCfg ? [...byName.keys()] : [...DS_REAL_DOGS];

  const personaFor = (dog) => {
    const r = byName.get(dog);
    if (r && typeof r.persona === "string" && r.persona.trim()) {
      return `## 🎯 个人偏好\n\n${r.persona.trim()}`;
    }
    return readPersona(cacheDir, dog, personaDir);
  };

  const scopeFor = (dog) => {
    const r = byName.get(dog);
    return r && r.scope ? r.scope : "jc";
  };

  return { dogs, personaDir, personaFor, scopeFor };
}

/**
 * 注册角色数据工具组。
 * @param {object} deps { registerTool, taskReg, domainHandles, cacheDir, roles, helpers:{ findMatch } }
 */
export function registerRoleTools(deps) {
  const { registerTool, taskReg, domainHandles, cacheDir, roles, helpers } = deps;

  // ── ds_persona_js：角色数据助手（人设 + 日常比赛范围 + 资金现状，一次注入）──
  registerTool({
    name: "ds_persona_js",
    description:
      "读某只狗的角色数据并注入上下文：人设（persona.md，投注风格/仓位档位/行为准则）+ 日常比赛范围（默认 jc 竞彩，可 beidan/all）+ 资金现状（余额/锁定敞口/全金额/约束）。分析下单前必读，金额 = 信心比例 × full_capital。只读。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: (_args, value) => [
        { type: "text", text: value && typeof value === "object" && !value.error ? value.text : ((value && value.error) || JSON.stringify(value)) },
      ],
    },
    execute: withTask(taskReg, { type: "role", title: "读角色数据" }, async (args) => {
      try {
        const text = roles.personaFor(args.user);
        const capital = await capitalQuery(domainHandles, args.user);
        return {
          user: args.user,
          text: text || "(无 persona.md，按通用框架执行)",
          scope: roles.scopeFor(args.user), // 日常比赛范围：默认竞彩（人设里可约定 beidan/all）
          capital,
        };
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_memory_js：纯 JS 读因子记忆 + 历史反思（对齐 Python format_for_prompt）──
  registerTool({
    name: "ds_memory_js",
    description:
      "读某只狗的历史记忆（读 ds_roles/ds_factors/ds_reflections/ds_slugs）：订单统计、连胜连败、最近订单、活跃因子/已证伪模式（factor_perf）、数据段表现（slug_stats）、历史反思、昨日结算回顾。分析下单前必读，判断信号时结合活跃因子与已证伪模式（例如「离散极低」可能是诱杀而非看好）。只读。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", description: "足球日 YYYY-MM-DD（因子衰减/休眠基准日 + 昨日结算回顾）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: (_args, value) => {
        const text = value && typeof value === "object" && !value.error
          ? value.text
          : (value && value.error) || JSON.stringify(value);
        return [{ type: "text", text: String(text || "") }];
      },
    },
    execute: withTask(taskReg, { type: "role", title: "读记忆" }, async (args) => {
      try {
        const getMatchName = (lid) => {
          const m = helpers.findMatch(cacheDir, lid);
          return m ? `${m.home || "?"} vs ${m.away || "?"}` : lid;
        };
        return await memoryQuery(domainHandles, args.user, { day: args.day, getMatchName });
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_capital_js：纯 JS 查资金（读 ds_roles 域，无 Python 桥）──
  registerTool({
    name: "ds_capital_js",
    description:
      "查询某只狗的资金现状（读 ds_roles 域）：余额/锁定敞口/全金额/未结算数/约束。产出 order 前先查，金额=信心比例×full_capital。只读。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "role", title: "查资金" }, async (args) => {
      try {
        return await capitalQuery(domainHandles, args.user);
      } catch (error) {
        return { error: error.message };
      }
    }),
  });
}
