/**
 * dsh ↔ python-engine 统一桥接（NDJSON 协议，仿 place_orders 桥）。
 *
 * 设计约束（与 grill 定稿一致）：
 *   - 固定 argv 直接 spawn（pythonBin -m src.bridge），绝不拼 shell、无 bash -c；
 *   - stdin 单行 JSON 请求；stdout 逐行 NDJSON（progress / result / error）；stderr 诊断；
 *   - func 白名单 + 狗名/日期双端校验；每步独立超时；API key 由 JS 直读 ~/.zshrc 注入 env。
 */
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** 桥 func 白名单（与 python-engine/src/bridge.py FUNCS 一一对应）。 */
export const BRIDGE_FUNCS = [
  "prepare",
  "analyze",
  "settle",
  "factor-induction",
  "factor-review",
  "status",
  "refresh",
  "reset",
];

/** 写操作 func（同一只狗串行 + 在途去重；prepare 只读/只拉数据不排队）。 */
export const MUTATING_FUNCS = new Set([
  "analyze",
  "settle",
  "factor-induction",
  "factor-review",
  "refresh",
  "reset",
]);

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** YYYY-MM-DD 且是真实日历日期。 */
export function isValidDateStr(s) {
  if (typeof s !== "string" || !DATE_RE.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** 从 ~/.zshrc 直读密钥（DEEPSEEK_API_KEY / LOTA_API_KEY），不经过 shell。 */
function keysFromZshrc() {
  const env = {};
  try {
    const text = readFileSync(join(homedir(), ".zshrc"), "utf8");
    const deepseek = text.match(/DEEPSEEK_API_KEY=["']?([A-Za-z0-9_-]+)/);
    if (deepseek) env.DEEPSEEK_API_KEY = deepseek[1];
    const lota = text.match(/LOTA_API_KEY=["']?([A-Za-z0-9_-]+)/);
    if (lota) env.LOTA_API_KEY = lota[1];
  } catch {
    // 读不到就算了：调用方进程 env 里可能已有
  }
  return env;
}

/**
 * 跑一次桥调用。
 *
 * @param {object} opts
 *   pythonBin / engineRoot / req({func,dog,day,start,end,opts})
 *   timeoutMs（默认 30 分钟）/ onProgress({phase,done,total,detail})
 * @returns {Promise<{ok:boolean, data?:object, error?:string, code:number, stderr:string, timedOut?:boolean}>}
 */
export function runBridge({
  pythonBin = "python",
  engineRoot = "",
  req = {},
  timeoutMs = 30 * 60 * 1000,
  onProgress,
} = {}) {
  return new Promise((resolve) => {
    // 沙箱写盘白名单：role_root 必须是 replays/sandboxes/<沙箱>/workspace
    const roleRoot = req && req.opts && req.opts.role_root;
    if (roleRoot && !/replays[\\/]sandboxes[\\/][^\\/]+[\\/]workspace$/.test(String(roleRoot))) {
      resolve({ ok: false, code: "role-root", error: `role_root 必须是沙箱 workspace 路径（replays/sandboxes/<沙箱>/workspace）: ${roleRoot}`, stderr: "" });
      return;
    }
    const childEnv = {
      ...process.env,
      ...keysFromZshrc(),
    };
    let child;
    try {
      child = spawn(pythonBin, ["-m", "src.bridge"], {
        cwd: engineRoot || undefined,
        env: childEnv,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (e) {
      resolve({ ok: false, code: "spawn-error", error: String((e && e.message) || e), stderr: "" });
      return;
    }

    let settled = false;
    const stdout = [];
    const stderr = [];
    let result = null;
    let bridgeError = null;
    const progress = typeof onProgress === "function" ? onProgress : () => {};

    const done = (payload) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(payload);
    };

    const timer = setTimeout(() => {
      try { child.kill("SIGKILL"); } catch {}
      done({ ok: false, timedOut: true, code: "timeout", error: `桥调用超时（${Math.round(timeoutMs / 1000)}s）: ${req.func}`, stderr: stderr.join("").slice(-2000) });
    }, timeoutMs);

    let stdoutBuf = "";
    const handleLine = (line) => {
      const t = line.trim();
      if (!t) return;
      let ev;
      try { ev = JSON.parse(t); } catch { return; }
      if (ev.type === "progress") {
        progress({ phase: ev.phase, done: ev.done, total: ev.total, detail: ev.detail });
      } else if (ev.type === "result") {
        result = ev;
      } else if (ev.type === "error") {
        bridgeError = ev;
      }
    };
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      const text = String(chunk);
      stdout.push(text);
      stdoutBuf += text;
      let idx;
      while ((idx = stdoutBuf.indexOf("\n")) >= 0) {
        handleLine(stdoutBuf.slice(0, idx));
        stdoutBuf = stdoutBuf.slice(idx + 1);
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => stderr.push(String(chunk)));
    child.on("error", (err) => {
      done({ ok: false, code: "spawn-error", error: String((err && err.message) || err), stderr: stderr.join("").slice(-2000) });
    });
    child.on("exit", (code) => {
      if (settled) return;
      if (stdoutBuf.trim()) handleLine(stdoutBuf); // 最后一行没有换行符时兜底
      const stderrText = stderr.join("").slice(-4000);
      if (bridgeError) {
        done({ ok: false, code, error: String(bridgeError.message || "桥错误"), stderr: stderrText });
        return;
      }
      if (result) {
        done({ ok: true, code, data: result.data, stderr: stderrText });
        return;
      }
      const outText = stdout.join("").slice(-2000);
      done({
        ok: false,
        code,
        error: `桥未返回结果（exit=${code}）: ${outText || stderrText || "无输出"}`.slice(0, 1000),
        stderr: stderrText,
      });
    });

    try {
      child.stdin.write(JSON.stringify(req) + "\n");
      child.stdin.end();
    } catch (e) {
      done({ ok: false, code: "stdin-error", error: String((e && e.message) || e), stderr: "" });
    }
  });
}

/** 把桥 result 压成任务状态摘要（result_summary / detail 用，≤200 字）。 */
export function bridgeResultSummary(func, data = {}) {
  try {
    switch (func) {
      case "prepare":
        return `竞彩 ${data.jingcai_count ?? data.candidates ?? 0} 场，预取 ${data.prefetched_ok ?? 0}/${data.candidates ?? 0}${data.warnings && data.warnings.length ? `；${data.warnings[0]}` : ""}`;
      case "analyze": {
        const placed = Number(data.placed || 0);
        const skipped = (data.orders || []).filter((o) => o.skip).length;
        return `比赛 ${data.matches_count ?? 0} 场 → 下单 ${placed} 单${skipped ? `（skip ${skipped}）` : ""}，余额 ${data.capital ?? "?"}`;
      }
      case "settle": {
        const s = data.settlement || {};
        return `结算 ${s.settled ?? 0} 单 命中${s.hit ?? 0}/未中${s.miss ?? 0}/走水${s.push ?? 0} PnL ${s.pnl ?? 0}，余额 ${data.capital ?? "?"}`;
      }
      case "factor-induction": {
        const sum = data.summary || {};
        return `合并 ${sum.merged ?? 0}，补定义 ${sum.fac_created ?? 0}，LLM 判重 ${sum.llm_calls ?? 0}（${data.factors?.counts?.active ?? "?"} 活跃）`;
      }
      case "factor-review": {
        const c = data.factor_summary?.counts || {};
        return `退役评估完成（至 ${data.end_date ?? ""}）: 活跃 ${c.active ?? 0}/退役 ${c.retired ?? 0}/休眠 ${c.dormant ?? 0}${data.user_notes ? "｜已应用用户意见" : ""}`;
      }
      case "status": {
        return `余额 ${data.capital ?? "?"}｜待结算 ${data.pending_count ?? 0} 单｜活跃因子 ${data.factors?.counts?.active ?? "?"}｜上次退役 ${data.last_factor_review || "—"}`;
      }
      case "refresh":
        return `退回 ${data.refunded ?? 0} 单 ¥${data.total_refund ?? 0}，保留 ${data.kept ?? 0} 单，余额 ${data.capital ?? "?"}`;
      case "reset":
        return `已${data.mode === "full" ? "完全" : "轻量"}重置 → 余额 ${data.capital ?? "?"}`;
      default:
        return "";
    }
  } catch {
    return "";
  }
}
