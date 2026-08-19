/**
 * 旁路 LLM 自由文本（不强制 JSON）——回放方向建议草稿用。
 * 从 reflect.js 的 streamText 抽出，避免活动代码依赖已退役的 domain 模块。
 */
import { createUserMessage, BlockAssembler, deepFreeze } from "@deepseek-ai/dsh-llm";

export const LLM_TEXT_DEFAULT = { provider: "deepseek-official", model: "deepseek-v4-flash" };

/** 调 ctx.llm.stream 拿纯文本（error/aborted 抛错，由调用方回退启发式）。 */
export async function streamText(ctx, prompt, { provider, model, maxTokens = 1200, temperature = 0.4 } = {}) {
  const p = provider || LLM_TEXT_DEFAULT.provider;
  const m = model || LLM_TEXT_DEFAULT.model;
  const messages = [createUserMessage({
    content: [{ type: "text", text: "请按要求输出。" }],
    source: { kind: "plugin", plugin: "ds-agents-lota-data" },
  })];
  const options = deepFreeze({
    provider: p,
    model: m,
    messages,
    system: prompt,
    temperature,
    maxTokens,
  });
  const assembler = new BlockAssembler();
  for await (const chunk of ctx.llm.stream(options)) {
    assembler.push(chunk);
  }
  const finish = assembler.finish;
  if (finish && (finish.kind === "error" || finish.kind === "aborted")) {
    throw new Error(`text stream ${finish.kind}`);
  }
  return assembler.blocks()
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
}
