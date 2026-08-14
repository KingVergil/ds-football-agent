/**
 * ⚠️ 数据专有解析（compact-fet 文本格式）—— 用户自定义数据源时需修改/替换本文件。
 *
 * 本文件解析的是 ds_agents 私有数据源产出的 compact-fet 文本排版，
 * 从「欧盘:Pinnacle / 亚盘:Pinnacle / 大小球:Pinnacle」三个段落里
 * 各取最后一条 Δ/OP 更新行，提取 Pinnacle 终盘赔率。
 *
 * 这段逻辑强依赖该文本的排版约定：
 *   - 段落起始标记：'欧盘:Pinnacle' / '亚盘:Pinnacle' / '大小球:Pinnacle'
 *   - 更新行以 'Δ'（最新）或 'OP'（开盘）开头
 *   - 赔率字段用 '/' 分隔（欧盘 h/d/a/r，亚盘 h/盘口/a/r，大小球 o/盘口/u/r）
 *   - 盘口是中文名（如 '受平/半'、'半/一'、'2/2.5'）
 *
 * 若你接入自己的数据源（自定义 Fetcher / 自定义缓存格式），
 * 请修改或替换 extractOdds / parseHandicap / HANDICAP_MAP。
 */

// 盘口中文名 → 数值（与 python-engine/src/tools.py 的 _HANDICAP_MAP 一致）
const HANDICAP_MAP = {
  "受平/半": -0.25, "平/半": 0.25, "半球": 0.5, "受半球": -0.5,
  "半/一": 0.75, "受半/一": -0.75, "一球": 1, "受一球": -1,
  "一/球半": 1.25, "一/半": 1.25, "受一/球半": -1.25, "受一/半": -1.25,
  "球半": 1.5, "受球半": -1.5,
  "半/二": 1.75, "受半/二": -1.75, "二球": 2, "受二球": -2,
  "二/球半": 2.25, "二/半": 2.25, "受二/球半": -2.25, "受二/半": -2.25,
  "二球半": 2.5, "受二球半": -2.5,
  "半/三": 2.75, "受半/三": -2.75, "三球": 3, "受三球": -3,
  "三/球半": 3.25, "三/半": 3.25, "受三/球半": -3.25, "受三/半": -3.25,
  "三球半": 3.5, "受三球半": -3.5,
  "半/四": 3.75, "受半/四": -3.75, "四球": 4, "受四球": -4,
  "四/球半": 4.25, "四/半": 4.25, "受四/球半": -4.25, "受四/半": -4.25,
  "四球半": 4.5, "受四球半": -4.5,
  "平手": 0,
};

/** 盘口文本 → 数值。中文名查表，否则 '2/2.5' 取均值，再否则纯数字，失败返回 0。 */
export function parseHandicap(text) {
  const t = (text ?? "").trim();
  if (t in HANDICAP_MAP) return HANDICAP_MAP[t];
  if (t.includes("/")) {
    const parts = t.split("/");
    if (parts.length === 2) {
      const a = parseFloat(parts[0]);
      const b = parseFloat(parts[1]);
      if (!Number.isNaN(a) && !Number.isNaN(b)) return (a + b) / 2;
    }
  }
  const n = parseFloat(t);
  return Number.isNaN(n) ? 0.0 : n;
}

/**
 * 取某段落最后一条赔率更新行（以 Δ 或 OP 开头）。
 * startMarker 之后的 endMarkers 里最早出现者截断段落。
 */
export function extractSectionLastLine(text, startMarker, endMarkers) {
  const start = text.indexOf(startMarker);
  if (start < 0) return null;
  let end = text.length;
  for (const marker of endMarkers) {
    const p = text.indexOf(marker, start + startMarker.length);
    if (p > start && p < end) end = p;
  }
  const section = text.slice(start, end);
  const lines = section.split("\n").map((l) => l.trim()).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].startsWith("Δ") || lines[i].startsWith("OP")) return lines[i];
  }
  return null;
}

/** OP 行末尾返奖率 "(r93.23%)" → "/93.23"，与 Δ 行四段对齐。 */
const OP_RETURN_SUFFIX = /\(r([\d.]+)%\)$/;

/** 拆赔率行：跳到第一个 "数字/" 处，按 '/' 切分。 */
export function splitOddsLine(line) {
  const m = line.match(/[\d.]+\//);
  if (!m) return [];
  let oddsPart = line.slice(m.index);
  // OP 行（只有开盘价、无 Δ 更新）末尾带 (r%)，归一化成第 4 段
  oddsPart = oddsPart.replace(OP_RETURN_SUFFIX, "/$1");
  return oddsPart.split("/");
}

/**
 * 提取 Pinnacle 终盘赔率（欧盘 / 亚盘 / 大小球）。
 * 返回 { eu?, asian?, ou? }；某段落不存在或最后一行非 Δ 行（如 OP 行只有 3 段）时该键缺省。
 */
export function extractOdds(fetText) {
  const result = {};
  if (!fetText) return result;

  const euLine = extractSectionLastLine(fetText, "欧盘:Pinnacle",
    // "欧盘:" 必须在列：欧盘:Pinnacle 之后可能紧跟「欧盘:澳门」，
    // 否则会把澳门的 OP 行当成 Pinnacle 段最后一行。
    ["欧盘:", "亚盘:", "大小球:", "必发欧盘", "公平盘", "离散指数", "阵容数据", "进球数据", "比分数据"]);
  if (euLine) {
    const parts = splitOddsLine(euLine);
    if (parts.length >= 4) {
      result.eu = { h: parseFloat(parts[0]), d: parseFloat(parts[1]), a: parseFloat(parts[2]) };
    }
  }

  const asLine = extractSectionLastLine(fetText, "亚盘:Pinnacle",
    ["欧盘:", "大小球:", "必发欧盘", "公平盘", "离散指数", "阵容数据", "进球数据", "比分数据"]);
  if (asLine) {
    const parts = splitOddsLine(asLine);
    if (parts.length >= 4) {
      const hcText = parts.slice(1, -2).join("/");
      result.asian = {
        h: parseFloat(parts[0]),
        handicap_text: hcText,
        // ⚠️ 主队视角（正=主受/负=主让），对齐 settle.js 与 Python order.handicap。
        // HANDICAP_MAP 是「受=负/让=正」raw 约定，这里取反成主队视角，
        // 与 python-engine/src/tools.py::extract_odds 之后的 -asian.handicap 一致。
        handicap: -parseHandicap(hcText),
        a: parseFloat(parts[parts.length - 2]),
      };
    }
  }

  const ouLine = extractSectionLastLine(fetText, "大小球:Pinnacle",
    ["进球数据", "比分数据", "阵容数据"]);
  if (ouLine) {
    const parts = splitOddsLine(ouLine);
    if (parts.length >= 4) {
      const thText = parts.slice(1, -2).join("/");
      result.ou = {
        over: parseFloat(parts[0]),
        threshold_text: thText,
        threshold: parseHandicap(thText),
        under: parseFloat(parts[parts.length - 2]),
      };
    }
  }

  return result;
}
