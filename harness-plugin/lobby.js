/**
 * 斗狗场分区归属：单关场 / 串关场（2026-09-13）。
 *
 * 为什么必须"按 scope 判定"而不是写死狗名：
 *   - `scope === "beidan"` → 北单串关狗（组合票型：N串1 / N过M / 包腿），进 **串关场**；
 *   - 其余（`jc` / `all` / 未声明）→ 走单关链路，进 **单关场**；
 *   - ⚠️ 北单**单关**狗（如 梭哈北单狗 / 跟风北单狗）没有 parlay.json，走的是通用
 *     Agent 单关链路，**不算串关**——但它们 scope 也是 beidan，需要按顺序排除。
 *
 * 与 client.js 里 Dashboard 的内联实现保持同一口径；本模块导出纯函数以便单测，
 * 并作为"agent 归属"的唯一事实来源。
 */

/** 有 parlay.json 的角色 = 真串关狗（由调用方传入名单，避免前端读文件）。 */
export const PARLAY_ONLY = "parlay";
export const SINGLE_LOBBY = "single";

/**
 * 判定某只狗属于哪个场子。
 * @param {{scope?: string, name?: string}} dog 注册表条目（至少含 scope）
 * @param {Set<string>|string[]} [parlayDogs] 已知串关狗名单（有 parlay.json 的狗）
 * @returns {"parlay"|"single"}
 */
export function lobbyOf(dog, parlayDogs) {
  // 权威依据：dashboard 下发的 isParlayRole（角色目录有 parlay.json）
  if (dog && typeof dog.isParlayRole === "boolean") return dog.isParlayRole ? PARLAY_ONLY : SINGLE_LOBBY;
  const name = dog && dog.name;
  if (parlayDogs && name) {
    const set = parlayDogs instanceof Set ? parlayDogs : new Set(parlayDogs);
    // 名单非空且命中 → 串关场（名单是权威：只有真有 parlay.json 的狗才是串关）
    if (set.size > 0 && set.has(name)) return PARLAY_ONLY;
    if (set.size > 0 && !set.has(name)) return SINGLE_LOBBY;
  }
  return dog && dog.scope === "beidan" ? PARLAY_ONLY : SINGLE_LOBBY;
}

/** 把狗列表分成两个场子（保持原顺序）。 */
export function splitLobbies(dogs, parlayDogs) {
  const out = { single: [], parlay: [] };
  for (const d of dogs || []) out[lobbyOf(d, parlayDogs)].push(d);
  return out;
}

/**
 * 结算/因子解耦后的两个"单独阶段"按钮（2026-09-13 解耦工单）。
 *
 * 动机：`🧾 结算` 走 `stage="both"`（结算 + 反思产因子），而**反思要调 LLM 烧钱**、
 * 结算本身是纯确定性对账。两者分开后可以先天天结算、攒着批量产因子。
 *
 * ⚠️ **只给串关狗**：单关狗（含北单单关狗）的按钮组保持原样，
 * 这是"改造不得影响单关狗"红线在 UI 上的落地。
 *
 * @param {{name?: string, isParlayRole?: boolean, scope?: string}} dog
 * @param {string} [day] 足球日（YYYY-MM-DD），由调用方按当前北京时间算好传入
 * @returns {Array<object>} 追加到按钮组的 action def（单关狗返回空数组）
 */
export function stageActionsFor(dog, day) {
  if (lobbyOf(dog) !== PARLAY_ONLY) return [];
  const name = (dog && dog.name) || "";
  const d = day || null;
  return [
    {
      id: "settle-only",
      label: "🧾 结算",
      title: "python 桥直启：只对账订单/资金，不产因子（不烧 LLM）",
      func: "settle",
      payload: { dog: name, func: "settle", day: d, opts: { stage: "settle" } },
    },
    {
      id: "reflect-only",
      label: "🧬 产因子",
      title: "python 桥直启：只反思产因子，不动订单与资金（消耗 LLM）",
      func: "settle",
      payload: { dog: name, func: "settle", day: d, opts: { stage: "reflect" } },
    },
  ];
}
