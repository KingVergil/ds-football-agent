/**
 * @module tools/deterministic
 *
 * 【固定无 LLM 工作流】确定性工具组：数据获取(data_fetch) + 订单/结算(settle_orders)
 * + 只读缓存 + 存储迁移/回放恢复。全部无 LLM 决策。
 */
import { existsSync } from "node:fs";
import { join } from "node:path";
import { prepareDay } from "../dataflow.js";
import { settleDog, fetchScoresFromCache } from "../settleEngine.js";
import { submitOrders, refreshOrders } from "../placeOrders.js";
import { migrateFromPython, exportToPython } from "../storage.js";
import { settledOrdersForDay } from "../flows.js";
import { restoreDomainSnapshot, listCheckpoints } from "../replay.js";
import { withTask } from "../taskStatus.js";
// ⚠️ 数据专有：见 odds.js 头部注释。用户自定义数据源时需修改/替换 odds.js。
import { extractOdds } from "../odds.js";
import {
  KIND_DIR, LOOSE_OBJECT, readJson, readMatches, normalizeFeature,
  findMatch, readSections, truncate, jsonRender,
} from "./shared.js";

/**
 * 注册固定无 LLM 工作流工具组。
 * @param {object} deps { registerTool, taskReg, domainHandles, cacheDir, engineRoot, pythonBin }
 */
export function registerDeterministicTools(deps) {
  const { registerTool, taskReg, domainHandles, cacheDir, engineRoot, pythonBin } = deps;

  // ── ds_prepare_day：LLM 前确定性数据边界（竞彩过滤 + 缓存优先/URL 兜底）──
  registerTool({
    name: "ds_prepare_day",
    description:
      "LLM 之前的数据准备（数据获取边界）：一次性获取某足球日的比赛并过滤竞彩（jingcai_number 非空，北单/无号排除），返回 strip_scores 后的候选列表。mode=live 强制刷新（拒绝旧赔率）；mode=replay 缓存优先、缺了才拉 URL。分析/回放前必须先调它拿比赛列表，禁止用 lota_matches 拉全量。",
    parameters: {
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD（窗口 [D 12:01, D+1 12:00]）" },
      mode: { type: "string", description: "live=强制刷新（默认）；replay=历史缓存优先、缺了拉 URL" },
      jingcai_only: { type: "boolean", description: "默认 true：只保留竞彩场次" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(8000),
    },
    execute: withTask(taskReg, { type: "data-prep", title: "数据准备" }, async (args, _exec, progress) => {
      try {
        const res = await prepareDay({
          cacheDir, engineRoot,
          day: args.day,
          mode: args.mode || "live",
          jingcaiOnly: args.jingcai_only !== false,
          pythonBin,
          onProgress: (p) => progress({
            phase: p.phase || "准备中",
            done: p.done ?? 0,
            total: p.total ?? 0,
            detail: p.detail || "",
          }),
        });
        return res;
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_settle_js：纯 JS 结算（settleOrder → 写 ds_roles 域，无 Python 桥）──
  registerTool({
    name: "ds_settle_js",
    description:
      "纯 JS 结算某只狗的未结算订单：取比分(state==6)→settleOrder(亚盘/大小球/胜平负/赢半输半)→写回 ds_roles 域 + 更新 capital。无 LLM、无 Python 桥。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "settle", title: "结算" }, async (args) => {
      try {
        return await settleDog(domainHandles, args.user, args.day, cacheDir);
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── refresh_orders：live 重跑前置，退回未开赛订单金额（对齐 Agent.refresh_orders）──
  registerTool({
    name: "refresh_orders",
    description:
      "刷新当天订单组（live 重跑前置）：把足球日窗口内未开赛的未结算订单退回金额并删除，已开赛的保留。分析当天前先调它，对齐旧 LangGraph 的 analyze(live=True) 行为。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "order", title: "回退订单" }, async (args) => {
      try {
        return await refreshOrders(domainHandles, args.user, args.day, cacheDir);
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── submit_orders：纯 JS 下单（结构化订单，业务规则全部 JS 化，无 Python 桥）──
  registerTool({
    name: "submit_orders",
    description:
      "把结构化订单落库（纯 JS）：跳过 skip → 已开赛保护 → 去重(lota_id,bet_type) → 资金折算(scale=余额/全金额)或硬约束 → 扣资金。订单是结构化数组，无需 order 文本。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
      orders: {
        type: "array",
        required: true,
        items: {
          type: "object",
          additionalProperties: false,
          properties: {
            lota_id: { type: "string", required: true, description: "比赛 ID" },
            bet_type: { type: "string", required: true, description: "亚盘|大小球|胜平负|让球胜平负" },
            pick: { type: "string", required: true, description: "H|A|D|over|under" },
            handicap: { type: "number", description: "亚盘盘口(主队视觉)" },
            odds: { type: "number", description: "赔率" },
            bet_size: { type: "number", description: "金额(=信心比例×全金额)" },
            reason: { type: "string", description: "理由" },
            skip: { type: "boolean", description: "不下注" },
          },
        },
      },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "order", title: "下单" }, async (args) => {
      try {
        return await submitOrders(domainHandles, args.user, args.day, args.orders, cacheDir);
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── fetch_scores：取比分（仅 state==6 完场权威，进行中/未开绝不返回）──
  registerTool({
    name: "fetch_scores",
    description: "从 matches 缓存取比分（仅 state==6 完场权威，进行中/未开的比分绝不返回）。只读。",
    parameters: {
      dates: { type: "array", items: { type: "string" }, required: true, description: "要扫描的日期列表 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "match-read", title: "取比分" }, async (args) => {
      return { scores: fetchScoresFromCache(cacheDir, args.dates) };
    }),
  });

  // ── ds_settled_js：只读取某狗某足球日的已结算订单（因子专员子任务用）──
  registerTool({
    name: "ds_settled_js",
    description:
      "只读：返回某只狗在某足球日窗口（[D 12:01, D+1 12:00]）已结算的订单列表（含 lota_id/类型/pick/比分/hit/profit/金额/理由）。供因子反思用。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(6000),
    },
    execute: withTask(taskReg, { type: "factor-read", title: "读已结算订单" }, async (args) => {
      try {
        const orders = await settledOrdersForDay(domainHandles, cacheDir, args.user, args.day);
        return { user: args.user, day: args.day, count: orders.length, orders };
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_replay_restore：回放检查点恢复（回到结算前 / 因子流前）──
  registerTool({
    name: "ds_replay_restore",
    description:
      "把 storage 域恢复到某次回放的检查点：run_id + checkpoint（start / <day>__pre-settle / <day>__pre-factor / <end>__post-factor，见回放报告检查点列表）。恢复后线上角色回到该阶段状态。",
    parameters: {
      run_id: { type: "string", required: true, description: "回放 run_id（replay_<start>_<end>_<ts>）" },
      checkpoint: { type: "string", required: true, description: "检查点名：start 或 <day>__pre-settle / <day>__pre-factor 等" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "replay-restore", title: "回放恢复" }, async (args) => {
      try {
        const replayDir = join(cacheDir, "replays", args.run_id);
        if (!existsSync(replayDir)) return { error: `回放不存在: ${args.run_id}` };
        const src = args.checkpoint === "start"
          ? join(replayDir, "snapshot")
          : join(replayDir, "checkpoints", args.checkpoint);
        if (!existsSync(src)) {
          return { error: `检查点不存在: ${args.checkpoint}（可用: ${listCheckpoints(replayDir).join(", ")}）` };
        }
        const restored = await restoreDomainSnapshot(domainHandles, src);
        return { ok: true, run_id: args.run_id, checkpoint: args.checkpoint, restored };
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_migrate_storage：Python 数据 → storage 域（一次性迁移）──
  registerTool({
    name: "ds_migrate_storage",
    description:
      "把 python-engine/data 下的 roles/factor_memory/reflection_memory/slug_memory 迁进 storage 域（ds_roles/ds_factors/ds_reflections/ds_slugs）。默认只迁 7 只真实狗（跳过临时快照），幂等（put 全量覆盖）。dry_run 只报告不写。",
    parameters: {
      dry_run: { type: "boolean", description: "只报告，不写 storage 域" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "role", title: "迁移存储" }, async (args) => {
      try {
        return await migrateFromPython(domainHandles, cacheDir, { dryRun: !!args.dry_run });
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_export_to_python：storage 域 → Python 文件（反向迁移，7 只真实狗）──
  registerTool({
    name: "ds_export_to_python",
    description:
      "把 storage 域（ds_roles/ds_factors/ds_reflections/ds_slugs/ds_factor_registry）还原成 Python 文件（data/roles/<狗>/<狗>.json + memory/*.json + factors/fac_*.json）。默认只导出 7 只真实狗（跳过临时快照）。dry_run 只报告不写。",
    parameters: {
      dry_run: { type: "boolean", description: "只报告，不写 Python 文件" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "role", title: "导出到 Python" }, async (args) => {
      try {
        return await exportToPython(domainHandles, cacheDir, { dryRun: !!args.dry_run });
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── lota_matches：某足球日比赛列表 ──
  registerTool({
    name: "lota_matches",
    description:
      "按 lottery_type 限定读取本地缓存中某足球日的比赛列表（matches/<date>.json）。⚠️ 必须带类型边界：jingcai=仅竞彩（默认，防北单/无号混入）、beidan=仅北单、all=全量（谨慎使用）。strip_scores=true 时剥离比分（分析用，防后视）。只读，不触网。",
    parameters: {
      date: { type: "string", required: true, description: "足球日 YYYY-MM-DD（窗口 [D 12:01, D+1 12:00]）" },
      lottery_type: { type: "string", description: "类型边界：jingcai(默认) / beidan / all" },
      strip_scores: { type: "boolean", description: "true=剥离比分（分析用，防后视）；比分只在 settle 工具里出现" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "match-read", title: "比赛列表" }, async (args) => {
      let matches = readMatches(cacheDir, args.date);
      const lotteryType = args.lottery_type || "jingcai"; // 默认竞彩，杜绝无边界全量读取
      if (lotteryType !== "all") {
        // 缓存里没有 lottery_type 字段，按 jingcai_number / beidan_number 过滤
        // （与 fanout.js readMatchesCache、Python 侧 --jingcai 过滤对齐）
        matches = matches.filter((m) =>
          lotteryType === "jingcai" ? Boolean(m && m.jingcai_number)
          : lotteryType === "beidan" ? Boolean(m && m.beidan_number)
          : false);
      }
      if (args.strip_scores) {
        matches = matches.map((m) => {
          const { score, result, ...rest } = m;
          return rest;
        });
      }
      return { date: args.date, lottery_type: lotteryType, count: matches.length, matches };
    }),
  });

  // ── lota_match：单场全貌 ──
  registerTool({
    name: "lota_match",
    description: "读取单场比赛全貌：基础信息 + 比分 + 段落(sections) + 预测/订单计数。只读本地缓存。strip_scores=true 时剥离比分（分析用，防后视）。",
    parameters: {
      lota_id: { type: "string", required: true, description: "比赛 ID，如 Lota4579740" },
      strip_scores: { type: "boolean", description: "true=剥离比分（分析用，防后视）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "match-read", title: "单场全貌" }, async (args) => {
      const feat = normalizeFeature(readJson(join(cacheDir, KIND_DIR.features, `${args.lota_id}.json`)));
      const match = feat?.match ?? findMatch(cacheDir, args.lota_id);
      const sections = readSections(cacheDir, args.lota_id);
      const predictions = readJson(join(cacheDir, KIND_DIR.predicts, `${args.lota_id}.json`)) ?? [];
      const orders = readJson(join(cacheDir, KIND_DIR.orders, `${args.lota_id}.json`)) ?? [];
      // Pinnacle 终盘赔率（数据专有解析，见 odds.js）
      const odds = extractOdds(feat?.compact_fet ?? "");
      const strip = !!args.strip_scores;
      const matchOut = strip && match ? Object.fromEntries(
        Object.entries(match).filter(([k]) => k !== "score" && k !== "result"),
      ) : match;
      return {
        lota_id: args.lota_id,
        match: matchOut,
        score: strip ? "" : (feat?.score ?? match?.score ?? ""),
        odds,
        sections: Object.fromEntries(
          Object.entries(sections).map(([slug, text]) => [slug, truncate(text, 500)])
        ),
        predictions: Array.isArray(predictions) ? predictions.length : 0,
        orders: Array.isArray(orders) ? orders.length : 0,
        cached_at: feat?._cached_at ?? null,
        api_failed: feat?._api_failed === true,
      };
    }),
  });

  // ── lota_sections：按 slug 取 prompt 段落 ──
  registerTool({
    name: "lota_sections",
    description: "读取本地缓存的段落（tags/<id>.json），按 slug 列表取 prompt 片段。只读。",
    parameters: {
      lota_id: { type: "string", required: true, description: "比赛 ID" },
      slugs: {
        type: "array",
        items: { type: "string" },
        required: true,
        description: "需要的段落 slug，如 fair-odds / asian-handicap-crown",
      },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(6000),
    },
    execute: withTask(taskReg, { type: "match-read", title: "比赛段落" }, async (args) => {
      const sections = readSections(cacheDir, args.lota_id);
      const picked = {};
      const parts = [];
      for (const slug of args.slugs) {
        if (sections[slug]) {
          picked[slug] = sections[slug];
          parts.push(`[section:${slug}]\n${sections[slug]}`);
        }
      }
      return { lota_id: args.lota_id, sections: picked, text: parts.join("\n\n") };
    }),
  });
}
