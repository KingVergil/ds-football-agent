/**
 * @module tools/deterministic
 *
 * 【只读数据工具组】agent 面板仅保留只读缓存读取 + lota_status（python 桥只读封装）。
 * 固定流（数据准备/分析/结算/因子归纳/因子退役/刷新/重置）不再注册为 LLM 工具：
 * 执行入口只剩 dashboard 表单（POST /ds-run、/ds-replay），由插件侧直接 spawn python 桥。
 */
import { join } from "node:path";

import { runBridge } from "../bridge.js";
import { withTask } from "../taskStatus.js";
// ⚠️ 数据专有：见 odds.js 头部注释。用户自定义数据源时需修改/替换 odds.js。
import { extractOdds } from "../odds.js";
import {
  KIND_DIR, LOOSE_OBJECT, readJson, readMatches, normalizeFeature,
  findMatch, readSections, truncate, jsonRender,
} from "./shared.js";

/**
 * 注册只读数据工具组。
 * @param {object} deps { registerTool, taskReg, cacheDir, engineRoot, pythonBin }
 */
export function registerDeterministicTools(deps) {
  const { registerTool, taskReg, cacheDir, engineRoot, pythonBin } = deps;

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

  // ── lota_status：某狗状态（python 桥只读封装；聊天问答用）──
  registerTool({
    name: "lota_status",
    description:
      "只读：查某只狗的状态（资金/待结算订单/活跃·退役·休眠因子数/资金曲线/上次因子退役日期），数据来自 python-engine（roles/<狗>/）。不触网、不写盘。",
    parameters: {
      dog: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(4000),
    },
    execute: withTask(taskReg, { type: "status", title: "狗状态" }, async (args) => {
      try {
        const r = await runBridge({ pythonBin, engineRoot, req: { func: "status", dog: String(args.dog) } });
        if (r.ok) return r.data;
        return { error: r.error };
      } catch (e) {
        return { error: String((e && e.message) || e) };
      }
    }),
  });
}
