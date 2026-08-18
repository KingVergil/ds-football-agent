/**
 * @module tools/headless
 *
 * 【有 LLM 但 headless 工作流】编排 / 旁路 LLM 工具组：
 *   - 分析: ds_analyze_dog（单只狗 = 确定性编排 + 一次 LLM 决策；是否并行由父 agent 决定）
 *   - settles: ds_settle_all（过程纯 JS 无 LLM，启动需 LLM 编排）
 *   - induct: ds_factor_flow / ds_factor_induction
 *   - review: ds_factor_review_js
 *   - replay: ds_replay
 *   - 旁路 LLM: ds_reflect_js / ds_factor_dedup
 *
 * 编排入口默认狗列表走 roles.dogs，人设走 roles.personaFor / roles.personaDir（可外置）。
 */
import { analyzeDogDirect, footballDayLabel } from "../fanout.js";
import { settleAll } from "../settleEngine.js";
import { factorFlow } from "../flows.js";
import { runReplay } from "../replay.js";
import { beijingNowIso } from "../settle.js";
import {
  buildReflectPrompt, streamReflectJson, parseReflectJson, applyReflection,
  getExistingFactorSummary, SLUG_WHITELIST, REFLECT_DEFAULT,
} from "../reflect.js";
import { factorReview } from "../factorReview.js";
import { judgeFactorDedup, inductFactors, inductAlpha, ALPHA_DOGS } from "../factorInduction.js";
import { withTask } from "../taskStatus.js";
import { LOOSE_OBJECT, jsonRender, LLM_TEMPERATURES } from "./shared.js";

/** 编排入口默认狗：显式传 args.dogs 优先，否则用外置角色列表 roles.dogs。 */
function resolveDogs(argDogs, roles) {
  return Array.isArray(argDogs) && argDogs.length ? argDogs : roles.dogs;
}

/** 从角色解析层为一组狗构建人设 map（供 fan-out / 回放注入，覆盖 persona.md）。 */
function personaMap(dogs, roles) {
  const map = {};
  for (const dog of dogs) {
    const p = roles.personaFor(dog);
    if (p) map[dog] = p;
  }
  return map;
}

/**
 * 注册有 LLM 但 headless 的工作流工具组。
 * @param {object} deps { ctx, registerTool, taskReg, domainHandles, cacheDir, engineRoot, pythonBin, roles }
 */
export function registerHeadlessTools(deps) {
  const { ctx, registerTool, taskReg, domainHandles, cacheDir, engineRoot, pythonBin, roles } = deps;

  // ── ds_analyze_dog：分析单只狗（父入口）——起一个最小执行子任务保证 session 可查，
  //    子任务唯一动作是调用 ds_analyze_dog_run 一次；重型 LLM 决策在引擎内只调一次。
  registerTool({
    name: "ds_analyze_dog",
    description:
      "分析单只狗（父入口）：起一个最小分析子任务（每个狗一个独立 session 可查），子任务唯一操作是调用 ds_analyze_dog_run(dog, day) 一次并原样汇报。数据准备/人设/记忆/资金/段落全部在 ds_analyze_dog_run 内部固有获取，LLM 只在判断时被调用一次（temperature 0.1），无工具轮次。父 agent 对每只狗调一次本工具；并列发起即并行。",
    parameters: {
      dog: { type: "string", required: true, description: "要分析的狗名（角色），如 梭哈2狗" },
      day: { type: "string", description: "足球日 YYYY-MM-DD（空=按北京时间自动推当日）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "analyze", title: "分析单狗" }, async (args, exec, progress) => {
      try {
        if (!ctx.subagents) return { error: "ctx.subagents 不可用，无法创建分析 session" };
        if (!exec || !exec.agent) return { error: "缺少 parent agent" };
        const day = args.day || footballDayLabel();
        progress({ phase: "分析（执行子任务）", done: 0, total: 1, detail: `${args.dog} ${day}` });
        const prompt = `你是 ds_agents 的分析执行子任务，处理狗「${args.dog}」，足球日 ${day}。

唯一操作：调用 ds_analyze_dog_run(dog="${args.dog}", day="${day}") 一次。
然后把它的返回内容原样汇报（JSON 即可，不要修改、不要总结重写、不要调用其他任何工具）。
禁止调用其他工具（没有 bash/文件/结算/因子工具）。`;
        const run = await ctx.subagents.start("spawn", {
          label: `analyze ${args.dog}`,
          prompt: [{ type: "text", text: prompt }],
          parent: exec.agent,
          signal: exec.signal,
          maxDepth: 1,
          toolFilter: {
            allow: ["ds_analyze_dog_run"],
            deny: ["subagent", "subagent_fork", "ds_analyze_dog"],
          },
        });
        const result = await run.result;
        const text = Array.isArray(result.output)
          ? result.output.filter((b) => b && b.type === "text" && typeof b.text === "string").map((b) => b.text).join("\n")
          : "";
        try { await run.dispose(); } catch {}
        progress({ phase: "分析完成", done: 1, total: 1, detail: `${args.dog} ${result.stopReason}` });
        return {
          ok: result.stopReason === "completed",
          dog: args.dog,
          day,
          runId: String(run.id ?? ""),
          text: text.slice(0, 2000),
        };
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_analyze_dog_run：分析单狗确定性引擎（供分析子任务调用；数据获取全固有，一次 LLM 决策）──
  registerTool({
    name: "ds_analyze_dog_run",
    description:
      "分析单狗确定性引擎（由 ds_analyze_dog 的分析子任务调用，也可直接调用）：固有完成 数据准备(prepareDay live 单例)→回退订单→读人设/记忆/资金→逐场段落→构建一个 prompt→调一次 LLM（temperature 0.1）→解析结构化订单→确定性下单。",
    parameters: {
      dog: { type: "string", required: true, description: "要分析的狗名（角色），如 梭哈2狗" },
      day: { type: "string", description: "足球日 YYYY-MM-DD（空=按北京时间自动推当日）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "analyze", title: "分析执行" }, async (args, _exec, progress) => {
      try {
        const day = args.day || footballDayLabel();
        return await analyzeDogDirect(ctx, domainHandles, {
          dog: args.dog,
          day,
          cacheDir,
          engineRoot,
          pythonBin,
          persona: roles.personaFor(args.dog),
          personaDir: roles.personaDir,
          onProgress: (p) => progress({ phase: p.phase, done: p.done, total: p.total, detail: p.detail }),
        });
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_settle_all：结算流（纯 JS，无 LLM，每狗进度入任务状态；启动由 LLM 编排触发）──
  registerTool({
    name: "ds_settle_all",
    description:
      "结算流（纯 JS，无 LLM）：并行结算指定狗的未结算订单（只认 state==6 比分），每狗进度写入任务状态。适合「全部结算 / 结算7狗」。",
    parameters: {
      day: { type: "string", description: "足球日 YYYY-MM-DD（空=按北京时间自动推当日）" },
      dogs: { type: "array", items: { type: "string" }, description: "要结算的狗名列表（空=默认 7 只真狗）" },
      parallel: { type: "number", description: "最大并发数（默认 4）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(8000),
    },
    execute: withTask(taskReg, { type: "settle-flow", title: "结算全部" }, async (args, _exec, progress) => {
      try {
        const day = args.day || footballDayLabel();
        return await settleAll(domainHandles, cacheDir, {
          day,
          dogs: resolveDogs(args.dogs, roles),
          parallel: args.parallel,
          engineRoot,
          pythonBin,
          onProgress: (p) => progress({ phase: p.phase, done: p.idx, total: p.total, detail: p.detail }),
        });
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_factor_flow：因子流（阶段A/B/C；父任务带出多个子任务，各自精简 prompt）──
  registerTool({
    name: "ds_factor_flow",
    description:
      "因子流：scope=induct 跑 阶段A 非alpha归纳→阶段B alpha barrier；scope=review 跑 阶段C 退役（非alpha先行/alpha收尾，支持 user_notes）；scope=all 全跑。每阶段/每狗一个独立子任务（各自精简 prompt），进度写入任务状态。适合「全部因子归纳 / 全部因子退役」。",
    parameters: {
      scope: { type: "string", description: "induct / review / all（默认 all）" },
      end_date: { type: "string", description: "评估窗口结束日 YYYY-MM-DD（空=今天）" },
      dogs: { type: "array", items: { type: "string" }, description: "狗名列表（空=默认 7 只真狗）" },
      model: { type: "string", description: "旁路 LLM 模型（默认 deepseek-v4-flash 省 token）" },
      limit: { type: "number", description: "每 scope 最多 LLM 判重次数（默认 30）" },
      user_notes: { type: "string", description: "用户调整意见（阶段C 退役评估注入）" },
      reflect_day: { type: "string", description: "阶段0 反思目标足球日（YYYY-MM-DD）；'auto' 或空 = 自动选最近有已结算订单的足球日" },
      parallel: { type: "number", description: "因子专员子任务并发数（默认 7）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(8000),
    },
    execute: withTask(taskReg, { type: "factor-flow", title: "因子流" }, async (args, exec, progress) => {
      try {
        if (!exec || !exec.agent) return { error: "exec.agent 不存在，无法启动因子专员子任务" };
        return await factorFlow(ctx, domainHandles, {
          scope: args.scope || "all",
          endDate: args.end_date,
          dogs: resolveDogs(args.dogs, roles),
          model: args.model,
          limit: args.limit,
          userNotes: args.user_notes,
          cacheDir,
          engineRoot,
          pythonBin,
          reflectDay: args.reflect_day,
          parallel: args.parallel,
          parent: exec.agent,
          signal: exec.signal,
          onProgress: (p) => progress({ phase: p.phase, done: p.done, total: p.total, detail: p.detail }),
        });
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_replay：回放模式（runall 日维度流程迁移）──
  registerTool({
    name: "ds_replay",
    description:
      "回放模式：把「获取比赛→分析→结算→因子归纳→周期性因子退役」按日维度跑 [start, end]。范围数据一次性准备（历史缓存优先、缺了拉 URL），逐日并行分析（fan-out subagent）+ 结算 + 反思 + 因子归纳，每 factor_review_every 天做因子退役评估。记录每狗每日轨迹到 cacheDir/replays/<run_id>/report.md。⚠️ 默认写穿：订单/因子直接落库保留（restore_after 默认 false）；restore_after=true 才是模拟跑、结束还原起点。⚠️ 预检：回放范围 [start,end] 内目标狗已有订单会直接拒绝（diff/diff-report 工具未设计）。reset=zero 从初始资金+空记忆开始。旁路 LLM 默认 deepseek-v4-flash 省 token。\n" +
      "两种运行方式：\n" +
      "  • 一路到底（默认）：不传 mode/interactive，直接跑完整段并出报告。\n" +
      "  • 半交互：mode=\"interactive\"（或 interactive=true）→ 每个因子退役周期结束就暂停，返回 status=\"paused\"，附带「下一轮因子归纳/退役方向建议」(direction_suggestion) 与可回退检查点。之后用 resume_run_id 续跑：\n" +
      "      - 采纳/编辑方向 → resume_run_id + induction_notes（注入下一周期退役评估）；\n" +
      "      - 回到某天状态 → resume_run_id + rewind_to=YYYY-MM-DD（恢复该天开始状态，截断其后轨迹）；\n" +
      "      - 一路到底 → resume_run_id + to_end=true（本次不再暂停）。",
    parameters: {
      start: { type: "string", description: "起始足球日 YYYY-MM-DD（全新回放必填；resume_run_id 续跑时忽略）" },
      end: { type: "string", description: "结束足球日 YYYY-MM-DD（含）（全新回放必填；resume_run_id 续跑时忽略）" },
      mode: { type: "string", description: "auto=一路到底（默认）；interactive=每个因子周期暂停交给用户" },
      interactive: { type: "boolean", description: "等价 mode=interactive（true=半交互，每周期暂停）" },
      resume_run_id: { type: "string", description: "续跑一个已暂停的会话（给此参数即续跑，忽略 start/end 等全新参数）" },
      induction_notes: { type: "string", description: "续跑时：用户编辑后的下一轮因子归纳/退役方向，注入下一周期退役评估" },
      rewind_to: { type: "string", description: "续跑时：回退到某天开始状态 YYYY-MM-DD（恢复该天前的线上状态并截断其后轨迹）" },
      to_end: { type: "boolean", description: "续跑时：本次一路跑到底，不再周期性暂停" },
      dogs: { type: "array", items: { type: "string" }, description: "回放的狗名列表（空=默认 7 只真狗）" },
      parallel: { type: "number", description: "分析并发数（默认=狗数）" },
      model: { type: "string", description: "旁路 LLM（反思/退役）模型，默认 deepseek-v4-flash" },
      user_notes: { type: "string", description: "首周期用户调整意见：注入因子退役评估（后续周期用 induction_notes 续传）" },
      persona_overrides: { type: "object", additionalProperties: true, description: "狗名→人设文本覆盖（分析/反思/退役用），值是字符串" },
      factor_review_every: { type: "number", description: "每隔多少天做一次因子退役（默认 7）；半交互模式下即暂停周期" },
      reset: { type: "string", description: "none=用当前状态（默认）；zero=从初始资金+空记忆开始" },
      restore_after: { type: "boolean", description: "true=模拟跑，跑完后还原起点；默认 false=写穿，订单/因子保留在线上" },
      run_id: { type: "string", description: "自定义运行标识（默认 replay_<start>_<end>_<ts>）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(10000),
    },
    execute: withTask(taskReg, { type: "replay", title: "回放" }, async (args, exec, progress) => {
      try {
        // 续跑：只透传续跑相关参数（狗/范围等已存于 session）
        if (args.resume_run_id) {
          return await runReplay(ctx, domainHandles, cacheDir, engineRoot, {
            resume_run_id: args.resume_run_id,
            induction_notes: args.induction_notes,
            rewind_to: args.rewind_to,
            to_end: args.to_end,
            parent: exec && exec.agent,
            signal: exec && exec.signal,
            onProgress: (p) => progress({
              phase: p.phase || "续跑中",
              done: p.done ?? 0,
              total: p.total ?? 0,
              detail: p.detail || "",
            }),
          });
        }
        const dogs = resolveDogs(args.dogs, roles);
        // 外置角色人设（config.roles 内联人设）作为覆盖基底，运行时 persona_overrides 仍优先
        const personaOverrides = { ...personaMap(dogs, roles), ...(args.persona_overrides || {}) };
        return await runReplay(ctx, domainHandles, cacheDir, engineRoot, {
          start: args.start,
          end: args.end,
          mode: args.mode,
          interactive: args.interactive,
          dogs,
          parallel: args.parallel,
          model: args.model,
          user_notes: args.user_notes,
          persona_overrides: personaOverrides,
          personaDir: roles.personaDir,
          factor_review_every: args.factor_review_every,
          reset: args.reset,
          restore_after: args.restore_after,
          run_id: args.run_id,
          parent: exec && exec.agent,
          signal: exec && exec.signal,
          pythonBin,
          onProgress: (p) => progress({
            phase: p.phase || "回放中",
            done: p.done ?? 0,
            total: p.total ?? 0,
            detail: p.detail || "",
          }),
        });
      } catch (error) {
        return { ok: false, error: error.message };
      }
    }),
  });

  // ── ds_reflect_js：纯 JS 结算后反思（旁路 LLM + 写回 storage，无 Python 桥）──
  registerTool({
    name: "ds_reflect_js",
    description:
      "纯 JS 结算后反思：读结算单+人设+已有因子 → 旁路 ctx.llm.stream 因子发现(JSON) → 写回 ds_factors/ds_reflections/ds_factor_registry。无 Python 桥。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      day: { type: "string", required: true, description: "足球日 YYYY-MM-DD" },
      settled: { type: "array", required: true, description: "ds_settle_js 返回的 orders 列表" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "reflect", title: "反思" }, async (args) => {
      try {
        const settled = args.settled || [];
        if (!settled.length) return { ok: true, skipped: "无结算单，跳过反思" };
        const persona = roles.personaFor(args.user);
        const existingSummary = await getExistingFactorSummary(domainHandles, args.user);
        const prompt = buildReflectPrompt({
          persona, settled, existingSummary, factorDescText: "", keySlugWhitelist: SLUG_WHITELIST,
        });
        const text = await streamReflectJson(ctx, prompt, {
          ...REFLECT_DEFAULT,
          temperature: LLM_TEMPERATURES.reflect,
        });
        const data = parseReflectJson(text);
        if (!data) return { ok: false, error: "reflect JSON 解析失败", raw: text.slice(0, 500) };
        return await applyReflection(domainHandles, args.user, args.day, data, settled);
      } catch (error) {
        return { ok: false, error: error.message };
      }
    }),
  });

  // ── ds_factor_review_js：纯 JS 因子退役评估（门控 + 旁路 LLM，无 Python 桥）──
  registerTool({
    name: "ds_factor_review_js",
    description:
      "纯 JS 因子退役评估：14天零触发休眠 + 低信息退役（确定性门控）+ 旁路 ctx.llm.stream 结构性评估(retire/dormant/active)。写回 ds_factors。无 Python 桥。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      end_date: { type: "string", required: true, description: "评估窗口结束日 YYYY-MM-DD" },
      start_date: { type: "string", description: "评估窗口起始日（空=近7天）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "factor-review", title: "因子退役评估" }, async (args) => {
      try {
        return await factorReview(domainHandles, ctx, args.user, args.end_date, args.start_date || "", cacheDir, {
          persona: roles.personaFor(args.user),
          personaDir: roles.personaDir,
        });
      } catch (error) {
        return { ok: false, error: error.message };
      }
    }),
  });

  // ── ds_factor_dedup：纯 JS 因子判重（旁路 LLM fast 模型 + 确定性兜底）──
  registerTool({
    name: "ds_factor_dedup",
    description:
      "纯 JS 因子判重：判断候选因子与某狗已有因子是否重复（create/merge/suppress）。旁路 ctx.llm.stream(fast 模型)+ 确定性兜底（retired 近亲 suppress）。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色），如 梭哈2狗" },
      factor_id: { type: "string", required: true, description: "候选因子名" },
      desc: { type: "string", description: "候选因子描述" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(),
    },
    execute: withTask(taskReg, { type: "factor-dedup", title: "因子判重" }, async (args) => {
      try {
        const domain = await domainHandles["ds_factors"];
        const rec = domain.table("factors").get(args.user);
        const fp = (rec && rec.factor_perf) || {};
        return await judgeFactorDedup(ctx, args.factor_id, args.desc || "", fp);
      } catch (error) {
        return { error: error.message };
      }
    }),
  });

  // ── ds_factor_induction：因子归纳去重（对齐 factor_induction.py，每日 settle 后跑）──
  registerTool({
    name: "ds_factor_induction",
    description:
      "因子归纳去重（对齐 python factor_induction.py）：同清洗名确定性合并 + slugs bit距离/孤儿名字相似 LLM 判重合并，合并后重算统计、累计 aliases，写回 ds_factors。每日 settle 后跑。user='alpha' 或 alpha 狗名（alpha2狗/alpha狗/均注狗）→ alpha 跨狗统一归纳（1 次进全库）；其他狗名 → 单狗各自归纳。dry_run 只报告候选不写回。",
    parameters: {
      user: { type: "string", required: true, description: "狗名（角色）；'alpha' 触发跨狗归纳" },
      dry_run: { type: "boolean", description: "只报告候选，不调 LLM、不写回" },
      limit: { type: "number", description: "最多 LLM 判重次数（默认 30）" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(8000),
    },
    execute: withTask(taskReg, { type: "factor-induction", title: "因子归纳" }, async (args) => {
      try {
        if (args.user === "alpha" || ALPHA_DOGS.includes(args.user)) {
          const res = await inductAlpha(ctx, domainHandles, {
            dryRun: !!args.dry_run,
            limit: args.limit ?? 30,
          });
          return { user: args.user, dry_run: !!args.dry_run, ...res };
        }
        const domain = await domainHandles["ds_factors"];
        const rec = domain.table("factors").get(args.user);
        const fp = { ...((rec && rec.factor_perf) || {}) };
        const { result, factorPerf } = await inductFactors(ctx, fp, {
          dryRun: !!args.dry_run,
          limit: args.limit ?? 30,
          scope: args.user,
        });
        if (!args.dry_run) {
          await domain.table("factors").put(args.user, {
            ...(rec || { factor_perf: {} }),
            factor_perf: factorPerf,
            updated_at: beijingNowIso(),
          });
        }
        return {
          user: args.user,
          dry_run: !!args.dry_run,
          factor_count_before: Object.keys((rec && rec.factor_perf) || {}).length,
          factor_count_after: Object.keys(factorPerf).length,
          ...result,
        };
      } catch (error) {
        return { error: error.message };
      }
    }),
  });
}
