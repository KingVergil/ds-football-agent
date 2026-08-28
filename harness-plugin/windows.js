/**
 * 智能窗口（外部编排专用，不进引擎）。
 *
 * 目标：每次分析决策尽量装满 maxPerWindow（默认 10）场——
 * 按开赛顺序贪心装窗，不按波次锚点硬切（锚点只作窗口标签参考）。
 * 只有「跨大时段」才断窗：相邻开赛间隙 ≥ gapBreak（默认 180min，如傍晚 vs 早场），
 * 避免把早场/次日场混进同一决策。
 *
 * 波次启动时机（几点跑哪一波）由调用方决定，本模块只负责把比赛分到窗口。
 */

export const DEFAULT_WAVE_ANCHORS = ["17:30", "19:30", "21:30", "00:30"];
export const DEFAULT_MAX_PER_WINDOW = 10;
export const DEFAULT_GAP_BREAK_MIN = 180;
/** 末波（00:30）覆盖到 ~04:30；04:30 后开赛的早场强制独立 session。 */
export const DEFAULT_LAST_WAVE_END_OFFSET = 990; // 04:30 相对足球日起点（12:00）分钟

/** 足球日起点 = 当日 12:00（北京时间），窗口 [D 12:01, D+1 12:00]。 */
function toUtcMin(ts) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(String(ts || ""));
  if (!m) return null;
  const [, y, mo, d, h, mi] = m.map(Number);
  return Date.UTC(y, mo - 1, d, h, mi) / 60000;
}

/** 比赛开赛时间相对足球日起点（D 12:00）的分钟偏移；不在窗口内返回 null。 */
export function matchOffsetMinutes(day, matchTime) {
  const base = toUtcMin(`${day} 12:00`);
  const mt = toUtcMin(matchTime);
  if (base == null || mt == null) return null;
  const off = mt - base;
  return off >= 0 && off < 1440 ? off : null;
}

/** 锚点 "17:30" → 相对足球日起点分钟（330）；"00:30" → 750（次日凌晨）。 */
export function anchorOffsetMinutes(anchor) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(anchor || ""));
  if (!m) return null;
  const abs = Number(m[1]) * 60 + Number(m[2]);
  return abs >= 720 ? abs - 720 : abs + 1440 - 720;
}

function assignAnchor(off, anchors) {
  let idx = 0;
  for (let i = 0; i < anchors.length; i++) {
    const a = anchorOffsetMinutes(anchors[i]);
    if (a == null) continue;
    if (off >= a) idx = i;
  }
  return idx;
}

/** 窗口标签：首场开赛时间对应的波次锚点；04:30 后归早场。 */
function anchorLabel(off) {
  const idx = assignAnchor(off, DEFAULT_WAVE_ANCHORS);
  const a = DEFAULT_WAVE_ANCHORS[idx];
  return off >= DEFAULT_LAST_WAVE_END_OFFSET ? `${a}后·早场` : a;
}

function stripMeta(matches) {
  return matches.map(({ _off, ...rest }) => rest);
}

function makeWindow(anchor, group) {
  const sorted = [...group].sort((a, b) => a._off - b._off);
  return {
    anchor,
    start: sorted[0].match_time,
    end: sorted[sorted.length - 1].match_time,
    match_ids: sorted.map((m) => m.lota_id),
    matches: stripMeta(sorted),
  };
}

/**
 * 把足球日 D 的竞彩比赛切成 1..N 个窗口，每窗尽量装满 maxPerWindow（默认 10）场。
 * @param {string} day "YYYY-MM-DD"
 * @param {Array<{lota_id:string, match_time:string}>} matches 已去重、仅竞彩
 * @param {object} [opts] { maxPerWindow, gapBreak }
 * @returns {Array<{anchor:string, start:string, end:string, match_ids:string[], matches:object[]}>}
 */
export function splitDayWindows(day, matches, opts = {}) {
  const cfg = {
    maxPerWindow: opts.maxPerWindow || DEFAULT_MAX_PER_WINDOW,
    gapBreak: opts.gapBreak || DEFAULT_GAP_BREAK_MIN,
  };
  const withOff = [];
  const seen = new Set();
  for (const m of matches || []) {
    if (!m || !m.lota_id || seen.has(m.lota_id)) continue;
    const off = matchOffsetMinutes(day, m.match_time);
    if (off == null) continue;
    seen.add(m.lota_id);
    withOff.push({ ...m, _off: off });
  }
  withOff.sort((a, b) => a._off - b._off);
  if (withOff.length === 0) return [];

  // 1) 按大时段（≥gapBreak）切成 session（同 session 内开赛连续）
  const sessions = [];
  let cur = [];
  for (const m of withOff) {
    const crossesEarly = m._off >= DEFAULT_LAST_WAVE_END_OFFSET
      && cur.length > 0 && cur[0]._off < DEFAULT_LAST_WAVE_END_OFFSET;
    if (cur.length > 0 && (m._off - cur[cur.length - 1]._off >= cfg.gapBreak || crossesEarly)) {
      sessions.push(cur);
      cur = [];
    }
    cur.push(m);
  }
  if (cur.length) sessions.push(cur);

  // 2) 每个 session 均衡切块：目标每窗尽量接近 maxPerWindow，且不出现 1 场尾窗
  const windows = [];
  for (const sess of sessions) {
    const n = sess.length;
    if (n <= cfg.maxPerWindow) {
      windows.push(makeWindow(anchorLabel(sess[0]._off), sess));
      continue;
    }
    const k = Math.ceil(n / cfg.maxPerWindow);
    const size = Math.ceil(n / k);
    for (let i = 0; i < n; i += size) {
      windows.push(makeWindow(anchorLabel(sess[i]._off), sess.slice(i, i + size)));
    }
  }
  windows.sort((a, b) => String(a.start).localeCompare(String(b.start)));
  return windows;
}
