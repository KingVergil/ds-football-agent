/**
 * 「斗狗场」仪表盘 Host 半身：读 python-engine/data 文件（唯一真源）+ matches/tags 缓存，注册 HTTP 路由
 *   GET  /ds-dashboard           → 仪表盘 JSON（动态狗列表：资金/夏普/六维/资金曲线/订单/回放会话）
 *   GET  /ds-avatars/<name>[.ext] → 头像图片（优先级：config.avatarDir → 仓库根 `头像/` → cacheDir/avatars，png/jpg 自动探测）
 *   GET  /ds-tasks              → 任务状态（外部 UI 轮询）
 *   POST   /ds-dogs             → 创建狗（运行时注册表 cacheDir/dogs.json + Python 角色，幂等补缺）
 *   PATCH  /ds-dogs/<name>      → 编辑狗配置（同步 Python 角色配置字段 + persona.md，资金/订单一律不动）
 *   DELETE /ds-dogs/<name>      → 删除狗（仅移注册表；历史订单/因子/资金保留）
 *   GET  /ds-persona/<name>     → 某只狗的完整人设文本（创建狗「复制自」按需拉取）
 *   POST /ds-run                → 按「功能 + 日期」直连 python-engine 桥（8 func 白名单；进度/结果经 taskReg）
 *   POST /ds-replay             → 直启插件侧回放会话（逐日逐 func 调桥；暂停/续跑/回退由 replay.js 编排）
 *   POST /ds-replay/<run_id>    → 续跑暂停会话（continue/to_end/rewind）
 *
 * 狗列表由 tools/roles.js::resolveRoles 的 roles.dogs 决定：
 *   - config.roles 配置了哪些狗就展示哪些（新狗即使 storage 未初始化也占位展示）
 *   - 未配置 roles 时保持旧行为：7 只真狗 + 串关2狗；「创建狗」新增的注册表狗随时并入。
 *
 * 数据唯一真源 = python-engine/data 文件（roles/<狗>/<狗>.json + memory/factor_memory.json），
 * 不再经过 storage 域（ds_roles 等已退役）。
 */
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { basename, join, resolve } from "node:path";

import { DS_REAL_DOGS } from "./tools/roles.js";
import { readTasks } from "./taskStatus.js";
import {
  runBridge, BRIDGE_FUNCS, MUTATING_FUNCS, isValidDateStr, bridgeResultSummary, defaultPythonBin,
} from "./bridge.js";
import {
  runReplay, validateReplayRange, listSandboxes, promoteSandbox, abortSandbox, sandboxNameFor,
} from "./replay.js";
import {
  createDog,
  updateDogEntry,
  deleteDogEntry,
  setDogStatus,
  pythonRoleDirNames,
} from "./dogRegistry.js";

/** 未配置 config.roles 时的斗狗场展示名单：7 只真狗 + Python 侧串关2狗。 */
const DEFAULT_DASHBOARD_DOGS = [...DS_REAL_DOGS, "串关2狗"];
const AVATAR_EXTS = ["png", "jpg", "jpeg"];
/** 创建新狗时的默认人设来源（用户指定：跟风狗）。 */
const DEFAULT_PERSONA_DOG = "跟风狗";

/** 去掉 personaFor 的「## 🎯 个人偏好」包装，注册表里存纯人设文本。 */
function unwrapPersona(text) {
  return String(text || "").replace(/^## 🎯 个人偏好\s*\n+/, "").trim();
}

/** 桥调用在途去重：key = `<狗>|<func>|<day|end>`（写操作每狗串行，409 提示）。 */
const __inflightBridge = new Set();

function dashboardDogsFor(roles) {
  if (roles && Array.isArray(roles.dogs) && roles.dogs.length) return [...roles.dogs];
  return [...DEFAULT_DASHBOARD_DOGS];
}

function round2(x) { return Math.round(x * 100) / 100; }

function calcMdd(points) {
  if (!points || points.length < 2) return 0;
  let peak = points[0].capital, mdd = 0;
  for (let i = 1; i < points.length; i++) {
    const c = points[i].capital;
    if (c > peak) peak = c;
    const dd = peak > 0 ? ((peak - c) / peak) * 100 : 0;
    if (dd > mdd) mdd = dd;
  }
  return mdd;
}

function calcSharpe(settled) {
  const returns = [];
  for (const o of settled) {
    const bet = Number(o.bet_size || 0);
    const profit = Number(o.profit || 0);
    if (bet > 0) returns.push(profit / bet);
  }
  const n = returns.length;
  if (n < 2) return null;
  let mean = 0;
  for (let i = 0; i < n; i++) mean += returns[i];
  mean /= n;
  let vs = 0;
  for (let i = 0; i < n; i++) vs += (returns[i] - mean) * (returns[i] - mean);
  const std = Math.sqrt(vs / n);
  if (std === 0) return null;
  return mean / std;
}

function readJson(path) {
  try { return JSON.parse(readFileSync(path, "utf8")); } catch { return null; }
}

/** 头像搜索目录（先到先用）：config.avatarDir → 仓库根 `头像/` → cacheDir/avatars。 */
function avatarDirsFor(cacheDir, configuredAvatarDir) {
  const dirs = [];
  if (configuredAvatarDir) dirs.push(resolve(String(configuredAvatarDir)));
  dirs.push(join(cacheDir, "..", "..", "头像"));
  dirs.push(join(cacheDir, "avatars"));
  return dirs;
}

/** 在头像目录列表里探测文件：<name>.png / <name>.jpg / <name>.jpeg。 */
function findAvatar(avatarDirs, name) {
  for (const dir of avatarDirs) {
    for (const ext of AVATAR_EXTS) {
      const file = join(dir, `${name}.${ext}`);
      if (existsSync(file)) return { file, ext, dir };
    }
  }
  return null;
}

/** 头像 URL（无扩展名；/ds-avatars 路由会自动探测扩展名）。 */
function avatarUrlFor(avatarDirs, name) {
  return findAvatar(avatarDirs, name) ? `/ds-avatars/${encodeURIComponent(name)}` : "";
}

/** 北京时间日期串（UTC+8，无宿主机时区依赖）。 */
function bjDateStr(ts) {
  return new Date(ts + 8 * 3600 * 1000).toISOString().slice(0, 10);
}

/** 比赛开赛时间（北京墙钟 "YYYY-MM-DD HH:MM"）→ 足球日（窗口 [D 12:01, D+1 12:00]，12:00 前归前一天）。 */
function footballDayOf(matchTime) {
  const t = String(matchTime || "");
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/.exec(t);
  if (!m) return "";
  const d = new Date(`${m[1]}T${m[2]}+08:00`);
  if (Number.isNaN(d.getTime())) return "";
  // 北京墙钟 − 12:01 后的北京日历日：bjMs = 真UTC + 8h − 12h01m = 真UTC − 4h01m
  return new Date(d.getTime() - (4 * 3600 + 60) * 1000).toISOString().slice(0, 10);
}

/** 当前足球日（窗口 [D 12:01, D+1 12:00]）：北京时间 now 减 12:01 后的日期。 */
function footballDayToday() {
  return bjDateStr(Date.now() - (12 * 3600 + 60) * 1000);
}

/** 读当日足球日的竞彩列表（jingcai_number 非空）。 */
function buildTodayMatches(cacheDir) {
  const day = footballDayToday();
  const list = readJson(join(cacheDir, "matches", `${day}.json`));
  const matches = Array.isArray(list) ? list : (list && list.matches) || [];
  const jc = matches.filter((m) => m && m.jingcai_number);
  return {
    day,
    count: jc.length,
    matches: jc
      .map((m) => ({
        lotaId: m.lota_id,
        home: m.home_name || "",
        away: m.away_name || "",
        league: m.league_name || "",
        time: m.match_time || "",
        number: m.jingcai_number || "",
        state: m.state == null ? null : m.state,
        stateName: m.state_name || "",
        score: m.score || "",
      }))
      .sort((a, b) => {
        const da = a.state === 6 ? 1 : 0;
        const db = b.state === 6 ? 1 : 0;
        if (da !== db) return da - db;
        return String(a.time).localeCompare(String(b.time));
      }),
  };
}

function parseMatchHead(text) {
  const out = { match: "", league: "", time: "" };
  if (!text) return out;
  const lines = String(text).split("\n");
  for (const line of lines) {
    const vsIdx = line.indexOf("对战:");
    if (vsIdx >= 0 && !out.match) {
      const v = line.slice(vsIdx + 3).trim();
      const parts = v.split("🆚");
      if (parts.length === 2) {
        const away = parts[1].split("｜")[0].split("|")[0].trim();
        out.match = parts[0].trim() + " vs " + away;
      }
    }
    const lgIdx = line.indexOf("联赛类型:");
    if (lgIdx >= 0 && !out.league) {
      out.league = line.slice(lgIdx + 5).split("｜")[0].split("|")[0].trim();
    }
    const tmIdx = line.indexOf("时间:");
    if (tmIdx >= 0 && !out.time) {
      out.time = line.slice(tmIdx + 3).split("｜")[0].split("|")[0].trim();
    }
  }
  return out;
}

/** 读取 matches/*.json + tags/*.json，构建 lota_id → {match,league,time}（60s TTL 缓存）。 */
let _matchMap = null;
let _matchMapAt = 0;
function buildMatchMap(cacheDir) {
  const now = Date.now();
  if (_matchMap !== null && now - _matchMapAt < 60000) return _matchMap;
  const map = {};
  const matchesDir = join(cacheDir, "matches");
  if (existsSync(matchesDir)) {
    for (const f of readdirSync(matchesDir)) {
      if (!f.endsWith(".json")) continue;
      const list = readJson(join(matchesDir, f));
      if (Array.isArray(list)) {
        for (const m of list) {
          if (m && m.lota_id) {
            map[m.lota_id] = {
              match: [m.home_name, m.away_name].filter(Boolean).join(" vs "),
              league: m.league_name || "",
              time: m.match_time || "",
            };
          }
        }
      }
    }
  }
  _matchMap = map;
  _matchMapAt = now;
  return map;
}

/** 用 tags/<id>.json 的 match-head 兜底补全缺失的 lota_id。 */
function fillMissingFromTags(cacheDir, matchMap, neededIds) {
  const tagsDir = join(cacheDir, "tags");
  if (!existsSync(tagsDir)) return;
  for (const id of neededIds) {
    if (matchMap[id]) continue;
    const tag = readJson(join(tagsDir, `${id}.json`));
    if (!tag || !tag.sections) continue;
    const info = parseMatchHead(tag.sections["match-head"] || "");
    if (info.match) matchMap[id] = info;
  }
}

function pickLabel(betType, pick, match) {
  if (pick === "over") return "大球";
  if (pick === "under") return "小球";
  if (betType === "亚盘") {
    const parts = match ? match.split(" vs ") : [];
    if (pick === "H") return parts[0] || "主队";
    if (pick === "A") return parts[1] || "客队";
  }
  if (pick === "H") return "主胜";
  if (pick === "A") return "客胜";
  if (pick === "D") return "平局";
  return pick || "";
}

/** 某狗「正在应用」的活跃因子（status=active），按样本数降序。 */
function activeFactorsFor(rec) {
  const fp = (rec && rec.factor_perf) || {};
  return Object.entries(fp)
    .filter(([, s]) => s && s.status === "active")
    .sort((a, b) => (Number((b[1] && b[1].total) || 0)) - (Number((a[1] && a[1].total) || 0)))
    .map(([factor, s]) => ({
      factor,
      desc: (s && s.desc) || "",
      total: Number((s && s.total) || 0),
      hit: Number((s && s.hit) || 0),
      profit: Number((s && s.profit) || 0),
      lastSeen: (s && s.last_seen) || "",
    }));
}

function buildDashboard(readRole, readFactors, matchMap, avatarDirs, activeDogs, roles) {
  const dogs = [];
  for (const name of activeDogs) {
    const role = readRole(name);
    const inStorage = Boolean(role);
    const safeRole = role || { name, capital: 0, initial_capital: 0, orders: [] };
    const orders = safeRole.orders || [];
    const capital = Number(safeRole.capital || 0);
    const configuredInitial = roles && typeof roles.initialCapitalFor === "function"
      ? roles.initialCapitalFor(name)
      : null;
    const initial = Number(configuredInitial != null ? configuredInitial : safeRole.initial_capital || 0);
    const display = roles && typeof roles.displayFor === "function" ? roles.displayFor(name) : {};
    const scope = roles && typeof roles.scopeFor === "function" ? roles.scopeFor(name) : "jc";
    const enabled = roles && typeof roles.enabledFor === "function" ? roles.enabledFor(name) : true;
    const limits = roles && typeof roles.limitsFor === "function" ? roles.limitsFor(name) : null;
    const settled = orders.filter((o) => o.settled_at);
    const pending = orders.filter((o) => !o.settled_at);
    const locked = pending.reduce((s, o) => s + Number(o.bet_size || 0), 0);
    const pnl = settled.reduce((s, o) => s + Number(o.profit || 0), 0);
    const wins = settled.filter((o) => (o.profit || 0) > 0).length;
    const losses = settled.filter((o) => (o.profit || 0) < 0).length;
    const pushes = settled.length - wins - losses;
    const decided = wins + losses;
    const turnover = settled.reduce((s, o) => s + Number(o.bet_size || 0), 0);
    const hitRate = decided > 0 ? wins / decided : null;
    const roi = turnover > 0 ? pnl / turnover : null;

    const dayMap = {};
    for (const o of settled) {
      const d = String(o.settled_at || "").slice(0, 10);
      if (d) dayMap[d] = (dayMap[d] || 0) + Number(o.profit || 0);
    }
    const dates = Object.keys(dayMap).sort();
    let run = initial;
    const curve = dates.map((d) => { run += dayMap[d]; return { date: d, capital: round2(run) }; });

    const orderRows = orders.map((o) => {
      const lota = String(o.lota_id || "");
      const m = matchMap[lota] || {};
      const matchDay = m.time ? footballDayOf(m.time) : "";
      return {
        lotaId: lota,
        match: m.match || "",
        league: m.league || "",
        time: m.time || "",
        matchDay,
        betType: o.bet_type || "",
        pick: o.pick || "",
        pickLabel: pickLabel(o.bet_type, o.pick, m.match || ""),
        handicap: o.handicap == null ? null : o.handicap,
        odds: o.odds == null ? null : o.odds,
        betSize: o.bet_size == null ? null : o.bet_size,
        score: o.score || "",
        hit: o.hit == null ? null : !!o.hit,
        profit: o.profit == null ? null : o.profit,
        settled: !!o.settled_at,
        settledAt: o.settled_at ? String(o.settled_at).slice(0, 10) : "",
        reason: o.reason || "",
      };
    }).sort((a, b) => String(b.matchDay || b.settledAt || b.time || "").localeCompare(String(a.matchDay || a.settledAt || a.time || "")));

    dogs.push({
      name,
      scope,
      enabled: Boolean(enabled),
      observation: !enabled,
      limits,
      alphaMode: Boolean(safeRole.alpha_mode),
      inStorage,
      configuredInitial: configuredInitial != null ? round2(configuredInitial) : null,
      emoji: display.emoji || "",
      c1: display.c1 || "",
      c2: display.c2 || "",
      avatarUrl: avatarUrlFor(avatarDirs, name),
      capital: round2(capital),
      fullCapital: round2(capital + locked),
      lockedExposure: round2(locked),
      initialCapital: round2(initial),
      pnl: round2(pnl),
      hitRate,
      roi,
      sharpe: calcSharpe(settled),
      mdd: round2(calcMdd(curve)),
      totalCount: settled.length + pending.length,
      wins, losses, pushes, decided,
      settledCount: settled.length,
      pendingCount: pending.length,
      factors: activeFactorsFor(readFactors(name)),
      curve,
      orders: orderRows,
    });
  }
  dogs.sort((a, b) => b.fullCapital - a.fullCapital);
  return { generatedAt: new Date().toISOString(), dogs };
}

/** 读 POST body（上限 1MB，超限截断）。 */
function readRequestBody(req, limit = 1024 * 1024) {
  return new Promise((resolve) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size <= limit) chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", () => resolve(""));
  });
}

/** 注册 /ds-dashboard 与 /ds-avatars 路由。幂等：由 ctx.effect 挂上并在卸载时移除。 */
export function setupDashboard(ctx, cacheDir, roles = null, avatarDir = null, extras = {}) {
  const avatarDirs = avatarDirsFor(cacheDir, avatarDir);
  // 用 ctx.get(strict=false) 而非 ctx.webServer 属性访问：headless 模式没有 webServer，
  // 属性访问会抛 "cannot get property webServer without inject"。
  const webServer = typeof ctx.get === "function" ? ctx.get("webServer", false) : undefined;
  if (!webServer) return;

  ctx.effect(() => {
    return webServer.register({
      kind: "exact",
      path: "/ds-dashboard",
      handler: async (req, res) => {
        try {
          // 每次请求重新解析狗名单：创建狗写入 dogs.json 后无需重启即可出现
          const activeDogs = dashboardDogsFor(roles);
          const readRole = (name) => readJson(join(cacheDir, "roles", name, `${name}.json`)) || {};
          const readFactors = (name) => readJson(join(cacheDir, "roles", name, "memory", "factor_memory.json")) || {};
          const matchMap = buildMatchMap(cacheDir);
          const needed = [];
          const seen = {};
          for (const name of activeDogs) {
            const role = readRole(name);
            for (const o of (role && role.orders) || []) {
              const id = String(o.lota_id || "");
              if (id && !seen[id]) { seen[id] = 1; needed.push(id); }
            }
          }
          fillMissingFromTags(cacheDir, matchMap, needed);
          const result = buildDashboard(
            readRole,
            readFactors,
            matchMap,
            avatarDirs,
            activeDogs,
            roles,
          );
          result.todayMatches = buildTodayMatches(cacheDir);
          result.tasks = readTasks(cacheDir).tasks || [];
          result.replays = listSandboxes(cacheDir);
          res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          res.end(JSON.stringify(result));
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-dashboard.route");

  // /ds-tasks：任务状态（运行中/最近完成），供外部 UI 轮询
  ctx.effect(() => {
    return webServer.register({
      kind: "exact",
      path: "/ds-tasks",
      handler: (req, res) => {
        res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
        res.end(JSON.stringify(readTasks(cacheDir)));
      },
    });
  }, "ds-tasks.route");

  // POST /ds-dogs：创建狗（主页「创建狗」表单）→ 写运行时注册表 + ds_roles + Python 角色（幂等补缺）
  ctx.effect(() => {
    return webServer.register({
      kind: "exact",
      path: "/ds-dogs",
      handler: async (req, res) => {
        try {
          if (String(req.method || "GET").toUpperCase() !== "POST") {
            res.writeHead(405, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "仅支持 POST" }));
            return;
          }
          let spec = {};
          try {
            spec = JSON.parse(await readRequestBody(req) || "{}");
          } catch { spec = {}; }
          // 默认人设：跟风狗 persona.md（用户未填/清空时后端兜底，仍不允许空）
          let defaultPersona = "";
          if (roles && typeof roles.personaFor === "function") {
            defaultPersona = unwrapPersona(roles.personaFor(DEFAULT_PERSONA_DOG));
          }
          const existing = [...dashboardDogsFor(roles), ...pythonRoleDirNames(cacheDir)];
          const result = await createDog(cacheDir, null, spec, {
            existingNames: existing,
            defaultPersona,
          });
          res.writeHead(result.ok ? 200 : 400, {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
          });
          res.end(JSON.stringify(result));
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-dogs.route");

  // PATCH /ds-dogs/<name>：编辑狗配置（注册表 + Python 角色配置字段 + persona.md）
  ctx.effect(() => {
    return webServer.register({
      kind: "prefix",
      path: "/ds-dogs/",
      handler: async (req, res) => {
        try {
          const method = String(req.method || "GET").toUpperCase();
          const pathPart = String((req && req.url) || "").split("?")[0];
          const marker = "/ds-dogs/";
          const idx = pathPart.indexOf(marker);
          let name = idx >= 0 ? pathPart.slice(idx + marker.length) : "";
          try { name = decodeURIComponent(name); } catch { name = ""; }
          if (!name || name.indexOf("/") >= 0 || name.indexOf("\\") >= 0 || name.indexOf("..") >= 0) {
            res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "狗名不合法" }));
            return;
          }
          if (method === "PATCH") {
            let patch = {};
            try { patch = JSON.parse(await readRequestBody(req) || "{}"); } catch { patch = {}; }
            const result = updateDogEntry(cacheDir, name, patch);
            res.writeHead(result.ok ? 200 : 400, {
              "Content-Type": "application/json; charset=utf-8",
              "Cache-Control": "no-store",
            });
            res.end(JSON.stringify(result));
            return;
          }
          if (method === "DELETE") {
            const result = deleteDogEntry(cacheDir, name);
            res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
            res.end(JSON.stringify(result));
            return;
          }
          res.writeHead(405, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: "仅支持 PATCH/DELETE" }));
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-dogs.patch.route");

  // GET /ds-persona/<name>：某只狗的完整人设文本（创建狗「复制自」用，按需拉取）
  ctx.effect(() => {
    return webServer.register({
      kind: "prefix",
      path: "/ds-persona",
      handler: (req, res) => {
        let seg = "";
        try {
          const pathPart = String((req && req.url) || "").split("?")[0];
          const marker = "/ds-persona/";
          const idx = pathPart.indexOf(marker);
          seg = idx >= 0 ? pathPart.slice(idx + marker.length) : "";
          seg = decodeURIComponent(seg);
        } catch { seg = ""; }
        if (!seg || seg.indexOf("/") >= 0 || seg.indexOf("\\") >= 0 || seg.indexOf("..") >= 0) {
          res.writeHead(404, { "Content-Type": "text/plain" });
          res.end("not found");
          return;
        }
        const persona = unwrapPersona(roles && typeof roles.personaFor === "function" ? roles.personaFor(seg) : "");
        res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
        res.end(JSON.stringify({ name: seg, persona: persona || "" }));
      },
    });
  }, "ds-persona.route");

  // POST /ds-run：按「功能 + 日期」直连 python-engine 桥（不经 LLM、不经 bash）。
  //   校验（func 白名单 / 狗名存在 / 日期格式与区间）→ 写操作在途去重（409）→
  //   后台 spawn python -m src.bridge，NDJSON progress → taskReg → /ds-tasks 轮询，
  //   结果摘要进任务记录（detail），前端渲染卡片。
  const {
    engineRoot = join(cacheDir, ".."),
    pythonBin = defaultPythonBin(),
    envFile = "",
    taskReg = null,
  } = extras;
  const allowedDog = (name) =>
    dashboardDogsFor(roles).includes(name) || pythonRoleDirNames(cacheDir).includes(name);
  const FUNC_LABEL = {
    prepare: "数据准备", analyze: "分析", settle: "结算",
    "factor-induction": "因子归纳", "factor-review": "因子退役", status: "状态",
    refresh: "刷新订单", reset: "重置",
  };

  // 校验桥请求（返回错误串或 null）
  function validateRun(spec) {
    const func = String(spec.func || "").trim();
    if (!BRIDGE_FUNCS.includes(func)) {
      return `未知功能: ${func || "(空)"}（可用: ${BRIDGE_FUNCS.join("/")}）`;
    }
    const dog = String(spec.dog || "").trim();
    if (func !== "prepare" && !dog) return "缺 dog（狗名）";
    if (dog && !allowedDog(dog)) return `狗「${dog}」不存在（roles/ 下无此角色）`;
    const day = String(spec.day || "").trim();
    const start = String(spec.start || "").trim();
    const end = String(spec.end || "").trim();
    if (["prepare", "analyze", "settle", "refresh"].includes(func)) {
      if (!isValidDateStr(day)) return `需要有效的 day 日期（YYYY-MM-DD）: ${day || "(空)"}`;
    }
    if (func === "factor-review") {
      const endDate = end || day;
      if (!isValidDateStr(endDate)) return `factor-review 需要有效的 end 日期: ${endDate || "(空)"}`;
      if (start && !isValidDateStr(start)) return `start 日期格式错误: ${start}`;
      if (start && start > endDate) return `start(${start}) 不能晚于 end(${endDate})`;
    }
    return null;
  }

  ctx.effect(() => {
    return webServer.register({
      kind: "exact",
      path: "/ds-run",
      handler: async (req, res) => {
        try {
          if (String(req.method || "GET").toUpperCase() !== "POST") {
            res.writeHead(405, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "仅支持 POST" }));
            return;
          }
          let spec = {};
          try { spec = JSON.parse(await readRequestBody(req) || "{}"); } catch { spec = {}; }
          const err = validateRun(spec);
          if (err) {
            res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: err }));
            return;
          }
          const func = String(spec.func).trim();
          const dog = String(spec.dog || "").trim();
          const day = String(spec.day || "").trim();
          const start = String(spec.start || "").trim();
          const end = String(spec.end || "").trim();
          const key = `${dog}|${func}|${day || end}`;
          if (MUTATING_FUNCS.has(func) && __inflightBridge.has(key)) {
            res.writeHead(409, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: `${dog || ""} ${FUNC_LABEL[func] || func} ${day || end || ""} 已在运行` }));
            return;
          }
          if (MUTATING_FUNCS.has(func)) __inflightBridge.add(key);

          res.writeHead(202, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          res.end(JSON.stringify({
            ok: true, dog, func, day, start, end,
            message: `${FUNC_LABEL[func] || func} 已启动（python-engine 桥，后台运行）`,
          }));

          // ── 后台管线（响应已发出，不阻塞请求）──
          const title = `${FUNC_LABEL[func] || func} ${dog} ${day || end || ""}`.trim();
          const taskId = taskReg
            ? taskReg.start({ type: `bridge-${func}`, title, params: { dog, func, day, start, end, opts: spec.opts } })
            : null;
          const prog = (p = {}) => {
            if (!taskReg || !taskId) return;
            taskReg.update(taskId, {
              phase: String(p.phase || ""),
              done: p.done ?? 0,
              total: p.total ?? 0,
              detail: String(p.detail || ""),
            });
          };
          try {
            const r = await runBridge({
              pythonBin,
              engineRoot,
              envFile,
              req: {
                func,
                ...(dog ? { dog } : {}),
                ...(day ? { day } : {}),
                ...(start ? { start } : {}),
                ...(end ? { end } : {}),
                opts: spec.opts || {},
              },
              onProgress: prog,
            });
            if (r.ok) {
              if (taskReg && taskId) {
                taskReg.finish(taskId, {
                  ok: true,
                  detail: bridgeResultSummary(func, r.data) || "完成",
                });
              }
            } else {
              const stderrTail = String(r.stderr || "").trim().slice(-300);
              if (taskReg && taskId) {
                taskReg.finish(taskId, { ok: false, detail: (r.error || "桥调用失败") + (stderrTail ? `｜${stderrTail}` : "") });
              }
            }
          } catch (e) {
            if (taskReg && taskId) taskReg.finish(taskId, { ok: false, detail: String((e && e.message) || e) });
          } finally {
            if (MUTATING_FUNCS.has(func)) __inflightBridge.delete(key);
          }
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-run.route");

  // POST /ds-induct-all：归纳全部（batch 模式）——非 alpha 并行，全部结束后 alpha barrier 串行一次。
  ctx.effect(() => {
    return webServer.register({
      kind: "exact",
      path: "/ds-induct-all",
      handler: async (req, res) => {
        try {
          if (String(req.method || "GET").toUpperCase() !== "POST") {
            res.writeHead(405, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "仅支持 POST" }));
            return;
          }
          if (__inflightBridge.has("induct-all")) {
            res.writeHead(409, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "归纳全部已在运行" }));
            return;
          }
          __inflightBridge.add("induct-all");

          const liveDogs = dashboardDogsFor(roles).filter((d) => roles && typeof roles.enabledFor === "function" ? roles.enabledFor(d) : true);
          const nonAlpha = liveDogs.filter((d) => !(roles && typeof roles.alphaModeFor === "function" ? roles.alphaModeFor(d) : false));
          const alpha = liveDogs.filter((d) => roles && typeof roles.alphaModeFor === "function" ? roles.alphaModeFor(d) : false);

          res.writeHead(202, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          res.end(JSON.stringify({
            ok: true,
            non_alpha: nonAlpha,
            alpha,
            message: `归纳全部已启动：非 alpha ${nonAlpha.length} 只并行 → alpha barrier（${alpha.join("、") || "无"}）串行`,
          }));

          const taskId = taskReg
            ? taskReg.start({ type: "induct-all", title: "归纳全部", params: { non_alpha: nonAlpha, alpha } })
            : null;
          const prog = (p = {}) => {
            if (!taskReg || !taskId) return;
            taskReg.update(taskId, {
              phase: String(p.phase || ""),
              done: p.done ?? 0,
              total: p.total ?? 0,
              detail: String(p.detail || ""),
            });
          };
          try {
            const summaryByDog = {};
            // 阶段 A：非 alpha 并行
            await Promise.all(nonAlpha.map(async (dog) => {
              const r = await runBridge({
                pythonBin, engineRoot, envFile,
                req: { func: "factor-induction", dog, opts: {} },
                onProgress: (p) => prog({ phase: `非 alpha 归纳 ${dog}·${p.phase || ""}`, done: 0, total: 1, detail: p.detail || dog }),
              });
              const sum = r.ok ? (r.data && r.data.summary) || {} : {};
              summaryByDog[dog] = { ok: r.ok, error: r.error, ...sum };
            }));
            // 阶段 B（barrier）：非 alpha 全部结束后，alpha 跨狗统一归纳一次（串行）
            let barrier = { merged: 0, llm_calls: 0, fac_created: 0 };
            if (alpha.length) {
              prog({ phase: `alpha barrier（${alpha.join("、")}）跨狗统一归纳`, done: 0, total: 1 });
              const r = await runBridge({
                pythonBin, engineRoot, envFile,
                req: { func: "factor-induction", opts: { roles: alpha } },
                onProgress: (p) => prog({ phase: `alpha barrier·${p.phase || ""}`, done: 0, total: 1, detail: p.detail || "" }),
              });
              barrier = r.ok ? (r.data && r.data.summary) || {} : { error: r.error };
            }
            const totalMerged = nonAlpha.reduce((s, d) => s + (summaryByDog[d].merged || 0), 0) + (barrier.merged || 0);
            const totalCreated = nonAlpha.reduce((s, d) => s + (summaryByDog[d].fac_created || 0), 0) + (barrier.fac_created || 0);
            const totalCalls = nonAlpha.reduce((s, d) => s + (summaryByDog[d].llm_calls || 0), 0) + (barrier.llm_calls || 0);
            if (taskReg && taskId) {
              taskReg.finish(taskId, {
                ok: true,
                detail: `非 alpha ${nonAlpha.length} 只完成；alpha barrier ${alpha.length ? "已执行" : "（无）"}｜合计 合并 ${totalMerged}，补定义 ${totalCreated}，LLM 判重 ${totalCalls}`,
              });
            }
          } catch (e) {
            if (taskReg && taskId) taskReg.finish(taskId, { ok: false, detail: String((e && e.message) || e) });
          } finally {
            __inflightBridge.delete("induct-all");
          }
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-induct-all.route");

  // POST /ds-replay：直启沙箱回放（表单交给会话 agent 后通常走 ds_replay 工具；本路由为直接触发/调试）。
  //   body { dog, start, end, mode("auto"|"interactive"), factor_review_every, reset("none"|"zero"), restore_after, skip_llm }
  ctx.effect(() => {
    return webServer.register({
      kind: "exact",
      path: "/ds-replay",
      handler: async (req, res) => {
        try {
          if (String(req.method || "GET").toUpperCase() !== "POST") {
            res.writeHead(405, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "仅支持 POST" }));
            return;
          }
          let spec = {};
          try { spec = JSON.parse(await readRequestBody(req) || "{}"); } catch { spec = {}; }
          const start = String(spec.start || "").trim();
          const end = String(spec.end || "").trim();
          const rangeCheck = validateReplayRange(start, end);
          if (!rangeCheck.ok) {
            res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: rangeCheck.error }));
            return;
          }
          const dog = String(spec.dog || (Array.isArray(spec.dogs) && spec.dogs[0]) || "").trim();
          if (!allowedDog(dog)) {
            res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: `狗「${dog}」不存在（roles/ 下无此角色）` }));
            return;
          }
          const sandbox = String(spec.sandbox || sandboxNameFor(dog, start)).trim();
          const interactive = spec.mode === "interactive";
          const reset = spec.reset === "zero" ? "zero" : "none";
          const skipLlm = spec.skip_llm === true;

          res.writeHead(202, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          res.end(JSON.stringify({
            ok: true,
            sandbox,
            dog,
            start,
            end,
            interactive,
            skip_llm: skipLlm,
            message: `沙箱 ${sandbox} 回放已启动（${dog} ${start}~${end}${interactive ? "，半交互" : ""}${skipLlm ? "，演示模式·跳过 LLM" : ""}）`,
          }));

          const taskId = taskReg
            ? taskReg.start({ type: "replay", title: `回放 ${sandbox}`, params: { sandbox, dog, start, end, interactive, reset, skip_llm: skipLlm } })
            : null;
          const prog = (p = {}) => {
            if (!taskReg || !taskId) return;
            taskReg.update(taskId, {
              phase: String(p.phase || ""),
              done: p.done ?? 0,
              total: p.total ?? 0,
              detail: String(p.detail || ""),
            });
          };
          try {
            const r = await runReplay(ctx, cacheDir, engineRoot, {
              dog,
              start,
              end,
              sandbox,
              mode: interactive ? "interactive" : "auto",
              factor_review_every: Math.max(1, Number(spec.factor_review_every) || 7),
              reset,
              restore_after: spec.restore_after === true,
              skip_llm: skipLlm,
              pythonBin,
              envFile,
              onProgress: prog,
            });
            if (r && r.status === "paused") {
              if (taskReg && taskId) {
                taskReg.finish(taskId, {
                  ok: true,
                  detail: `已暂停（第 ${r.days_done}/${r.days_total} 天）等待方向意见 → 详情页回放区编辑`,
                });
              }
            } else if (r && r.ok) {
              if (taskReg && taskId) {
                taskReg.finish(taskId, { ok: true, detail: `回放完成（${r.days} 天），报告: ${r.report_path || ""}` });
              }
            } else {
              if (taskReg && taskId) taskReg.finish(taskId, { ok: false, detail: (r && r.error) || "回放失败" });
            }
          } catch (e) {
            if (taskReg && taskId) taskReg.finish(taskId, { ok: false, detail: String((e && e.message) || e) });
          }
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-replay.route");

  // POST /ds-replay/<sandbox>：续跑暂停沙箱会话。
  //   body { action: "continue"|"to_end"|"rewind", induction_notes?, rewind_to? }
  ctx.effect(() => {
    return webServer.register({
      kind: "prefix",
      path: "/ds-replay/",
      handler: async (req, res) => {
        try {
          if (String(req.method || "GET").toUpperCase() !== "POST") {
            res.writeHead(405, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "仅支持 POST" }));
            return;
          }
          const pathPart = String((req && req.url) || "").split("?")[0];
          const marker = "/ds-replay/";
          const idx = pathPart.indexOf(marker);
          let sandbox = idx >= 0 ? pathPart.slice(idx + marker.length) : "";
          try { sandbox = decodeURIComponent(sandbox); } catch { sandbox = ""; }
          if (!sandbox || sandbox.indexOf("/") >= 0 || sandbox.indexOf("\\") >= 0 || sandbox.indexOf("..") >= 0) {
            res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "sandbox 名不合法" }));
            return;
          }
          let body = {};
          try { body = JSON.parse(await readRequestBody(req) || "{}"); } catch { body = {}; }
          const action = String(body.action || "continue").trim();
          if (!["continue", "to_end", "rewind"].includes(action)) {
            res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: `action 必须是 continue/to_end/rewind: ${action}` }));
            return;
          }
          res.writeHead(202, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          res.end(JSON.stringify({ ok: true, sandbox, action, message: "沙箱回放续跑已启动" }));

          const taskId = taskReg
            ? taskReg.start({ type: "replay-resume", title: `续跑回放 ${sandbox}`, params: { sandbox, action } })
            : null;
          const prog = (p = {}) => {
            if (!taskReg || !taskId) return;
            taskReg.update(taskId, {
              phase: String(p.phase || ""),
              done: p.done ?? 0,
              total: p.total ?? 0,
              detail: String(p.detail || ""),
            });
          };
          try {
            const opts = { sandbox, onProgress: prog, pythonBin, envFile };
            if (action === "to_end") opts.to_end = true;
            if (action === "rewind") opts.rewind_to = String(body.rewind_to || "").trim();
            if (typeof body.induction_notes === "string" && body.induction_notes.trim()) {
              opts.induction_notes = body.induction_notes.trim();
            }
            const r = await runReplay(ctx, cacheDir, engineRoot, opts);
            if (r && r.status === "paused") {
              if (taskReg && taskId) taskReg.finish(taskId, { ok: true, detail: `已暂停（第 ${r.days_done}/${r.days_total} 天）等待方向意见` });
            } else if (r && r.ok) {
              if (taskReg && taskId) taskReg.finish(taskId, { ok: true, detail: `回放完成（${r.days} 天），报告: ${r.report_path || ""}` });
            } else {
              if (taskReg && taskId) taskReg.finish(taskId, { ok: false, detail: (r && r.error) || "续跑失败" });
            }
          } catch (e) {
            if (taskReg && taskId) taskReg.finish(taskId, { ok: false, detail: String((e && e.message) || e) });
          }
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-replay.resume.route");

  // POST /ds-sandbox/<sandbox>/promote | /abort：转正（替换线上）/ 放弃（删沙箱）。
  ctx.effect(() => {
    return webServer.register({
      kind: "prefix",
      path: "/ds-sandbox/",
      handler: async (req, res) => {
        try {
          if (String(req.method || "GET").toUpperCase() !== "POST") {
            res.writeHead(405, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "仅支持 POST" }));
            return;
          }
          const pathPart = String((req && req.url) || "").split("?")[0];
          const marker = "/ds-sandbox/";
          const idx = pathPart.indexOf(marker);
          let seg = idx >= 0 ? pathPart.slice(idx + marker.length) : "";
          try { seg = decodeURIComponent(seg); } catch { seg = ""; }
          const m = /^([^/]+)\/(promote|abort)$/.exec(seg);
          if (!m) {
            res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify({ ok: false, error: "路径应为 /ds-sandbox/<沙箱>/promote|abort" }));
            return;
          }
          const sandbox = m[1];
          const action = m[2];
          const s = readJson(join(cacheDir, "replays", "sandboxes", sandbox, "session.json")) || {};
          const dog = String(s.dog || "").trim();
          if (action === "promote") {
            if (!dog) {
              res.writeHead(400, { "Content-Type": "application/json; charset=utf-8" });
              res.end(JSON.stringify({ ok: false, error: "沙箱会话缺 dog，无法转正" }));
              return;
            }
            const p = promoteSandbox(cacheDir, sandbox, dog);
            if (p.ok) setDogStatus(cacheDir, dog, "live");
            res.writeHead(p.ok ? 200 : 400, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
            res.end(JSON.stringify(p));
            return;
          }
          const a = abortSandbox(cacheDir, sandbox);
          res.writeHead(a.ok ? 200 : 400, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          res.end(JSON.stringify(a));
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-sandbox.route");

  ctx.effect(() => {
    return webServer.register({
      kind: "prefix",
      path: "/ds-avatars",
      handler: (req, res) => {
        let seg = "";
        try {
          const pathPart = String((req && req.url) || "").split("?")[0];
          const marker = "/ds-avatars/";
          const idx = pathPart.indexOf(marker);
          seg = idx >= 0 ? pathPart.slice(idx + marker.length) : "";
          seg = decodeURIComponent(seg).replace(/\.(jpg|jpeg|png)$/i, "");
        } catch { seg = ""; }
        if (!seg || seg !== basename(seg) || seg.indexOf("..") >= 0 || seg.indexOf("/") >= 0 || seg.indexOf("\\") >= 0) {
          res.writeHead(404, { "Content-Type": "text/plain" });
          res.end("not found");
          return;
        }
        const avatar = findAvatar(avatarDirs, seg);
        if (!avatar) {
          res.writeHead(404, { "Content-Type": "text/plain" });
          res.end("not found");
          return;
        }
        const bytes = readFileSync(avatar.file);
        const contentType = avatar.ext === "png" ? "image/png" : "image/jpeg";
        res.writeHead(200, { "Content-Type": contentType, "Cache-Control": "public, max-age=3600" });
        res.end(bytes);
      },
    });
  }, "ds-avatars.route");
}
