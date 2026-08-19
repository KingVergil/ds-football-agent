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

/**
 * 平台默认 Python 解释器：
 *   - Windows：`python`（`python3` 通常是 MS Store 别名，不可靠）
 *   - macOS / Linux：`python3`（Homebrew / apt 一般没有裸 `python`）
 * 挂载时可用 `config.pythonBin` 显式覆盖（如 conda 的绝对路径）。
 */
export function defaultPythonBin() {
  return process.platform === "win32" ? "python" : "python3";
}

/**
 * 解析 KEY=VALUE 文本（兼容 `export ` 前缀与单/双引号；不执行 shell）。
 * 用于 .env 文件与 ~/.zshrc / ~/.bashrc 兜底读取。
 */
export function parseEnvText(text) {
  const env = {};
  for (const rawLine of String(text || "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let val = m[2].trim();
    if (!val || val.includes("$") || val.includes("`")) continue; // 不解析 shell 展开
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    if (val) env[m[1]] = val;
  }
  return env;
}

function readEnvFile(file) {
  try {
    return parseEnvText(readFileSync(file, "utf8"));
  } catch {
    return {};
  }
}

/** 引擎会用到的密钥/配置键（.env / shell rc 兜底只挑这些，避免误读无关行）。 */
const KNOWN_KEYS = new Set([
  "DEEPSEEK_API_KEY",
  "LOTA_API_KEY",
  "QQ_EMAIL_ADDR",
  "QQ_EMAIL_AUTH_CODE",
  "EMAIL_163_ADDR",
  "EMAIL_163_AUTH_CODE",
  "QQ_EMAIL_RECIPIENTS_FILE",
  "QQ_EMAIL_TO",
  "DS_ROLES_ROOT",
  "DS_SESSIONS_ROOT",
  "DS_FACTORS_ROOT",
  "DSF_DATA_DIR",
]);

/**
 * 从 ~/.zshrc / ~/.bashrc / ~/.profile 兜底读密钥（仅非 Windows；老用户迁移用）。
 * 新装一律写 .env，bridge 优先读 .env，这里只是兼容历史配置。
 */
function keysFromShellRc() {
  if (process.platform === "win32") return {};
  const env = {};
  for (const rc of [".zshrc", ".bashrc", ".profile"]) {
    for (const [k, v] of Object.entries(readEnvFile(join(homedir(), rc)))) {
      if (KNOWN_KEYS.has(k) && !(k in env)) env[k] = v;
    }
  }
  return env;
}

/**
 * 组装桥子进程 env，优先级（高 → 低）：
 *   1. 宿主进程已有环境变量（process.env）
 *   2. 显式 envFile（config.envFile）→ <engineRoot>/.env → ~/.env（Windows 友好）
 *   3. ~/.zshrc / ~/.bashrc（仅非 Windows 的历史兜底）
 * 低优先级只填空缺，不覆盖已有值（修复旧实现里 zshrc 覆盖进程 env 的问题）。
 */
export function resolveChildEnv({ envFile = "", engineRoot = "" } = {}) {
  const merged = { ...process.env };
  const files = [];
  if (envFile) files.push(envFile);
  if (engineRoot) files.push(join(engineRoot, ".env"));
  files.push(join(homedir(), ".env"));
  for (const file of files) {
    for (const [k, v] of Object.entries(readEnvFile(file))) {
      if (!(k in merged)) merged[k] = v;
    }
  }
  for (const [k, v] of Object.entries(keysFromShellRc())) {
    if (!(k in merged)) merged[k] = v;
  }
  return merged;
}

/**
 * 跑一次桥调用。
 *
 * @param {object} opts
 *   pythonBin / engineRoot / req({func,dog,day,start,end,opts})
 *   envFile（可选，密钥文件；缺省自动找 <engineRoot>/.env 与 ~/.env）
 *   timeoutMs（默认 30 分钟）/ onProgress({phase,done,total,detail})
 * @returns {Promise<{ok:boolean, data?:object, error?:string, code:number, stderr:string, timedOut?:boolean}>}
 */
export function runBridge({
  pythonBin = defaultPythonBin(),
  engineRoot = "",
  req = {},
  envFile = "",
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
    const childEnv = resolveChildEnv({ envFile, engineRoot });
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
