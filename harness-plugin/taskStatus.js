/**
 * 任务状态注册表（task status registry）—— 见 docs/task_status.md。
 *
 * 每个工具调用登记一条状态记录；长任务运行中通过 progress 回调持续更新
 * phase / done / total / detail；结束标记 completed / failed。
 *
 * 对外契约：
 *   - 持久化文件：<cacheDir>/tasks/status.json（原子写，外部 UI 可轮询）
 *   - HTTP：GET /ds-tasks（web 模式）；/ds-dashboard 附带 tasks 字段
 *
 * 字段：id / type / title / params / status(running|completed|failed|interrupted)
 *      / phase / done / total / detail / result_summary / started_at / updated_at / finished_at
 */
import { readFileSync, writeFileSync, renameSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const DEFAULT_MAX = 50;
const DEFAULT_TTL_MS = 24 * 3600 * 1000;
const STALE_RUNNING_MS = 10 * 60 * 1000;

/** 生成任务 id：<type>_<ts>_<rand>。 */
export function taskId(type) {
  return `${type || "task"}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}

/** 读取任务状态文件（外部 UI / 其他进程用），缺文件返回空列表。 */
export function readTasks(cacheDir) {
  try {
    return JSON.parse(readFileSync(join(cacheDir, "tasks", "status.json"), "utf8"));
  } catch {
    return { tasks: [] };
  }
}

/**
 * 创建任务注册表。
 * @param {string} cacheDir 缓存根目录（tasks/status.json 落在这里）
 * @param {object} opts { max, ttlMs }
 */
export function createTaskRegistry(cacheDir, { max = DEFAULT_MAX, ttlMs = DEFAULT_TTL_MS } = {}) {
  let tasks = (readTasks(cacheDir).tasks || []).map((t) => ({ ...t }));

  const persist = () => {
    try {
      const dir = join(cacheDir, "tasks");
      mkdirSync(dir, { recursive: true });
      const tmp = join(dir, `status.${process.pid}.tmp`);
      writeFileSync(tmp, JSON.stringify({ tasks }, null, 2), "utf8");
      renameSync(tmp, join(dir, "status.json"));
    } catch {
      // 持久化失败不阻断业务（内存态仍有效）
    }
  };

  const prune = () => {
    const now = Date.now();
    tasks = tasks
      .map((t) => {
        // running 且长时间无更新 → interrupted（进程被杀 / 超时遗留）
        if (t.status === "running" && now - new Date(t.updated_at || now).getTime() > STALE_RUNNING_MS) {
          return { ...t, status: "interrupted", phase: "中断", updated_at: new Date().toISOString() };
        }
        return t;
      })
      .filter((t) => t.status === "running" || now - new Date(t.updated_at || now).getTime() < ttlMs)
      .slice(0, max);
  };

  /** 登记一个任务，返回任务 id。 */
  const start = (meta = {}) => {
    const now = new Date().toISOString();
    const t = {
      id: taskId(meta.type || "task"),
      type: meta.type || "task",
      title: meta.title || meta.type || "任务",
      params: meta.params ?? null,
      status: "running",
      phase: meta.phase || "启动",
      done: meta.done ?? 0,
      total: meta.total ?? 0,
      detail: meta.detail ?? "",
      result_summary: "",
      started_at: now,
      updated_at: now,
      finished_at: null,
    };
    tasks.unshift(t);
    prune();
    persist();
    return t.id;
  };

  /** 更新进度（phase / done / total / detail ...）。 */
  const update = (id, patch = {}) => {
    const t = tasks.find((x) => x.id === id);
    if (!t) return null;
    Object.assign(t, patch, { updated_at: new Date().toISOString() });
    persist();
    return t;
  };

  /** 结束任务（ok → completed；否则 failed）。 */
  const finish = (id, { ok = true, detail, result_summary } = {}) => {
    const t = tasks.find((x) => x.id === id);
    if (!t) return null;
    const now = new Date().toISOString();
    t.status = ok ? "completed" : "failed";
    t.phase = ok ? "完成" : "失败";
    if (detail !== undefined) {
      t.detail = typeof detail === "string" ? detail.slice(0, 500) : JSON.stringify(detail).slice(0, 500);
    }
    if (result_summary !== undefined) t.result_summary = String(result_summary).slice(0, 200);
    t.finished_at = now;
    t.updated_at = now;
    persist();
    return t;
  };

  const list = () => tasks.slice();

  return { start, update, finish, list, prune, persist };
}

/**
 * 工具 execute 包装器：start → 业务 → finish；异常标 failed 并返回 { error }。
 * fn 签名：(args, exec, progress) => result
 */
export function withTask(reg, meta, fn) {
  return async (args, exec) => {
    const id = reg.start({ ...meta, params: args });
    const progress = (patch = {}) => reg.update(id, patch);
    try {
      const result = await fn(args, exec, progress);
      const ok = !(result && result.error);
      reg.finish(id, {
        ok,
        detail: result && (result.error || (result.warnings && result.warnings.join(";")) || result.summary || ""),
        result_summary: result && (result.text || result.summary || result.pnl != null ? `PnL ${result.pnl}` : ""),
      });
      return result;
    } catch (e) {
      const msg = String((e && e.message) || e);
      reg.finish(id, { ok: false, detail: msg });
      return { error: msg };
    }
  };
}
