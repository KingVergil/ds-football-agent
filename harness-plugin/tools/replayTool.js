/**
 * @module tools/replayTool
 *
 * ds_replay：回放唯一 agent 入口（薄壳设计下 harness 仅保留回放模式）。
 * 沙箱模型：replays/sandboxes/<狗>_<MMDD>/，桥经 role_root 写沙箱 workspace，线上零影响。
 * 半交互暂停时返回 direction_suggestion，由 agent 呈现给用户确认后带 induction_notes 续跑。
 */
import { runReplay } from "../replay.js";
import { withTask } from "../taskStatus.js";
import { LOOSE_OBJECT, jsonRender } from "./shared.js";

/**
 * 注册 ds_replay 工具。
 * @param {object} deps { ctx, registerTool, taskReg, cacheDir, engineRoot, pythonBin, envFile }
 */
export function registerReplayTool(deps) {
  const { ctx, registerTool, taskReg, cacheDir, engineRoot, pythonBin, envFile = "" } = deps;

  registerTool({
    name: "ds_replay",
    description:
      "回放模式（沙箱模型，harness 唯一保留的工作流）：创建 replays/sandboxes/<狗>_<MMDD>/ 沙箱（新狗空骨架、老狗复制到起始日结算后/因子归纳前），桥经 role_root 写沙箱 workspace，线上零影响；逐日「分析→结算→因子归纳→周期性因子退役」并产 facts.json（事实订单/因子/资金曲线）。半交互（mode=\"interactive\"）时在周期边界暂停，返回 direction_suggestion——你必须呈现给用户确认/编辑，再带 induction_notes 续跑；也可 to_end 一路到底 / rewind_to 回退。skip_llm=true 为演示模式（秒级跑完看交互）。转正/放弃走 POST /ds-sandbox/<沙箱>/promote|abort。",
    parameters: {
      dog: { type: "string", description: "回放狗名（沙箱单狗模型）" },
      start: { type: "string", description: "起始足球日 YYYY-MM-DD（新回放必填）" },
      end: { type: "string", description: "结束足球日 YYYY-MM-DD（新回放必填，区间 ≤60 天）" },
      sandbox: { type: "string", description: "沙箱名（缺省 <狗>_<MMDD>；已存在 paused 会话则续跑）" },
      mode: { type: "string", description: "interactive=半交互（周期边界暂停）/ auto=一路到底（默认）" },
      interactive: { type: "boolean", description: "true 等价 mode=interactive" },
      factor_review_every: { type: "number", description: "每隔多少天做因子退役（默认 7）；半交互即暂停周期" },
      reset: { type: "string", description: "none=当前状态（默认）/ zero=初始资金+空记忆" },
      restore_after: { type: "boolean", description: "true=模拟跑结束还原起点；默认 false=沙箱保留待转正" },
      skip_llm: { type: "boolean", description: "true=演示模式：分析/退役跳过 LLM（0 订单），秒级跑完看交互卡片" },
      user_notes: { type: "string", description: "首周期用户调整意见（注入退役评估）；后续周期用 induction_notes" },
      induction_notes: { type: "string", description: "续跑时：用户确认/编辑后的下一轮方向，注入下一周期退役评估" },
      to_end: { type: "boolean", description: "续跑时：本次一路到底，不再暂停" },
      rewind_to: { type: "string", description: "续跑时：回退到某天开始状态 YYYY-MM-DD" },
    },
    output: {
      schema: LOOSE_OBJECT,
      render: jsonRender(10000),
    },
    execute: withTask(taskReg, { type: "replay", title: "回放" }, async (args, _exec, progress) => {
      try {
        const onProgress = (p) => progress({
          phase: p.phase || "回放中",
          done: p.done ?? 0,
          total: p.total ?? 0,
          detail: p.detail || "",
        });
        return await runReplay(ctx, cacheDir, engineRoot, {
          dog: args.dog || (Array.isArray(args.dogs) && args.dogs[0]),
          start: args.start,
          end: args.end,
          sandbox: args.sandbox,
          mode: args.mode,
          interactive: args.interactive,
          factor_review_every: args.factor_review_every,
          reset: args.reset,
          restore_after: args.restore_after,
          skip_llm: args.skip_llm === true,
          user_notes: args.user_notes,
          induction_notes: args.induction_notes,
          to_end: args.to_end,
          rewind_to: args.rewind_to,
          pythonBin,
          envFile,
          onProgress,
        });
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) };
      }
    }),
  });
}
