/**
 * 「斗狗场」仪表盘 Host 半身：读 ds_roles 域 + matches/tags 缓存，注册 HTTP 路由
 *   GET /ds-dashboard         → 仪表盘 JSON（8 只真狗：资金/夏普/六维/资金曲线/订单）
 *   GET /ds-avatars/<name>.jpg → 头像图片（python-engine/data/avatars/）
 *
 * 复用 setupDomains 打开的 ds_roles 域；文件用 node:fs 直接读（与 lota 工具同源）。
 */
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

/** 真实参与分析的 8 只狗（7 只真狗 + 串关2狗；排除 _simwk、_sim0804、_snapshot、__mt_*、test_verify 等临时副本与快照）。 */
const REAL_DOGS = [
  "alpha2狗", "alpha狗", "梭哈2狗", "梭哈3狗", "平局狗", "跟风狗", "均注狗", "串关2狗",
];

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

/** 北京时间日期串（UTC+8，无宿主机时区依赖）。 */
function bjDateStr(ts) {
  return new Date(ts + 8 * 3600 * 1000).toISOString().slice(0, 10);
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

function buildDashboard(rolesTable, matchMap) {
  const dogs = [];
  for (const [name, role] of rolesTable.entries()) {
    if (REAL_DOGS.indexOf(name) < 0) continue;
    const orders = (role && role.orders) || [];
    const capital = Number(role.capital || 0);
    const initial = Number(role.initial_capital || 0);
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
      return {
        lotaId: lota,
        match: m.match || "",
        league: m.league || "",
        time: m.time || "",
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
    }).sort((a, b) => String(b.settledAt || b.time || "").localeCompare(String(a.settledAt || a.time || "")));

    dogs.push({
      name,
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
      curve,
      orders: orderRows,
    });
  }
  dogs.sort((a, b) => b.fullCapital - a.fullCapital);
  return { generatedAt: new Date().toISOString(), dogs };
}

/** 注册 /ds-dashboard 与 /ds-avatars 路由。幂等：由 ctx.effect 挂上并在卸载时移除。 */
export function setupDashboard(ctx, domainHandles, cacheDir) {
  const webServer = ctx.webServer;
  if (!webServer) return;

  ctx.effect(() => {
    return webServer.register({
      kind: "exact",
      path: "/ds-dashboard",
      handler: async (req, res) => {
        try {
          const rolesDomain = await domainHandles["ds_roles"];
          const matchMap = buildMatchMap(cacheDir);
          const needed = [];
          const seen = {};
          for (const [, role] of rolesDomain.table("roles").entries()) {
            for (const o of (role && role.orders) || []) {
              const id = String(o.lota_id || "");
              if (id && !seen[id]) { seen[id] = 1; needed.push(id); }
            }
          }
          fillMissingFromTags(cacheDir, matchMap, needed);
          const result = buildDashboard(rolesDomain.table("roles"), matchMap);
          result.todayMatches = buildTodayMatches(cacheDir);
          res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
          res.end(JSON.stringify(result));
        } catch (e) {
          res.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ error: String((e && e.message) || e) }));
        }
      },
    });
  }, "ds-dashboard.route");

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
          seg = decodeURIComponent(seg);
        } catch { seg = ""; }
        if (!seg || seg.indexOf("..") >= 0 || seg.indexOf("/") >= 0 || seg.indexOf("\\") >= 0 || !/\.(jpg|jpeg|png)$/i.test(seg)) {
          res.writeHead(404, { "Content-Type": "text/plain" });
          res.end("not found");
          return;
        }
        const file = join(cacheDir, "avatars", seg);
        if (!existsSync(file)) {
          res.writeHead(404, { "Content-Type": "text/plain" });
          res.end("not found");
          return;
        }
        const bytes = readFileSync(file);
        const png = /\.png$/i.test(seg);
        res.writeHead(200, { "Content-Type": png ? "image/png" : "image/jpeg", "Cache-Control": "public, max-age=3600" });
        res.end(bytes);
      },
    });
  }, "ds-avatars.route");
}
