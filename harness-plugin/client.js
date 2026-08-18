// 「斗狗场」仪表盘 Client 半身：注册 conversation.view tab，fetch /ds-dashboard 取数渲染。
// 静态 client bundle（dsh.client 双面包），格式与 shipped 的 trajectory 插件一致。
window.__ModuleLoader__.load({
  id: "ds-agents-lota-data",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    var ReactModule = require("react");
    var React = (ReactModule && ReactModule.default) ? ReactModule.default : ReactModule;
    var h = React.createElement;

    var css = `
.dsd-root{padding:8px 4px 24px;color:var(--dsw-alias-label-primary);font-family:inherit;overflow-y:auto;height:100%;box-sizing:border-box}
.dsd-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.dsd-h1{font-size:18px;font-weight:800;letter-spacing:.5px}
.dsd-h2{font-size:11px;color:var(--dsw-alias-label-secondary);margin-top:2px}
.dsd-actions{display:flex;gap:8px;flex-shrink:0}
.dsd-btn{font-size:12px;padding:6px 12px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary);cursor:pointer}
.dsd-btn:hover{border-color:var(--dsw-alias-brand-primary)}
.dsd-btn.on{background:var(--dsw-alias-brand-primary);color:#fff;border-color:var(--dsw-alias-brand-primary)}
.dsd-btn:disabled{opacity:.6;cursor:default}
.dsd-back{margin-bottom:12px}
.dsd-error{border:1px solid var(--dsw-alias-state-error-primary);color:var(--dsw-alias-state-error-primary);border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:13px;background:var(--dsw-alias-bg-layer-1)}
.dsd-empty{color:var(--dsw-alias-label-secondary);text-align:center;padding:24px;font-size:13px}
.dsd-layout{display:grid;grid-template-columns:250px 1fr;gap:14px;align-items:start}
@media(max-width:900px){.dsd-layout{grid-template-columns:1fr}}
.dsd-board{background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:14px;padding:12px;position:sticky;top:0}
.dsd-board-title{font-size:13px;font-weight:800;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.dsd-board-row{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:10px;cursor:pointer}
.dsd-board-row:hover{background:var(--dsw-alias-bg-layer-2)}
.dsd-board-row.top{background:var(--dsw-alias-bg-layer-2)}
.dsd-board-rank{width:22px;text-align:center;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums;flex-shrink:0}
.dsd-board-name{flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dsd-board-power{font-size:12px;font-weight:800;color:var(--dsw-alias-brand-primary);font-variant-numeric:tabular-nums}
.dsd-avatar{border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 2px 6px rgba(0,0,0,.18);overflow:hidden;background:var(--dsw-alias-bg-layer-2)}
.dsd-avatar-emoji{line-height:1}
.dsd-avatar-img{width:100%;height:100%;object-fit:cover;display:block}
.dsd-avatar-ring{border-radius:50%;padding:3px;background:linear-gradient(135deg,var(--dsw-alias-brand-primary),var(--dsw-alias-state-warn-primary));display:inline-flex;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.25)}
.dsd-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.dsd-card{position:relative;overflow:hidden;background:radial-gradient(circle at 18% 0%,rgba(147,51,234,0.12),transparent 55%),radial-gradient(circle at 100% 100%,rgba(219,39,119,0.10),transparent 50%),var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:16px;padding:14px 16px}
.dsd-card::before{content:'';position:absolute;inset:0;z-index:0;pointer-events:none;background-image:radial-gradient(circle,var(--dsw-alias-border-l2) 1px,transparent 1.6px);background-size:14px 14px;opacity:.4}
.dsd-card::after{content:'ゴゴゴ';position:absolute;right:10px;bottom:6px;z-index:0;pointer-events:none;font-weight:900;font-size:30px;color:var(--dsw-alias-label-secondary);opacity:.10;transform:rotate(-6deg);letter-spacing:-2px}
.dsd-card>*{position:relative;z-index:1}
.dsd-card-btn{cursor:pointer;transition:transform .12s,border-color .12s,box-shadow .12s}
.dsd-card-btn:hover{border-color:var(--dsw-alias-brand-primary);transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.10)}
.dsd-card-head{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.dsd-card-name{flex:1;min-width:0}
.dsd-name{font-weight:800;font-size:14px}
.dsd-sub{font-size:11px;color:var(--dsw-alias-label-secondary)}
.dsd-cap{text-align:right}
.dsd-money{font-variant-numeric:tabular-nums}
.dsd-strong{font-weight:800;font-size:14px}
.dsd-radar-wrap{display:flex;justify-content:center;margin:4px 0 2px}
.dsd-radar-label{font-size:9px;fill:var(--dsw-alias-label-secondary)}
.dsd-card-foot{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--dsw-alias-label-secondary);border-top:1px solid var(--dsw-alias-border-l1);padding-top:8px;margin-top:4px}
.dsd-pos{color:var(--dsw-alias-state-success-primary)}
.dsd-neg{color:var(--dsw-alias-state-error-primary)}
.dsd-mut{color:var(--dsw-alias-label-secondary)}
.dsd-pend{color:var(--dsw-alias-state-warn-primary)}
.dsd-detail-head{display:flex;align-items:center;gap:14px;margin:4px 0 14px}
.dsd-detail-info{flex:1;min-width:0}
.dsd-detail-meta{font-size:12px;color:var(--dsw-alias-label-secondary);margin-top:3px}
.dsd-stats{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:14px}
@media(max-width:900px){.dsd-stats{grid-template-columns:repeat(3,1fr)}}
.dsd-stat{background:var(--dsw-alias-bg-layer-2);border-radius:10px;padding:8px;text-align:center}
.dsd-stat-label{font-size:10px;color:var(--dsw-alias-label-secondary)}
.dsd-stat-val{font-size:14px;font-weight:800;font-variant-numeric:tabular-nums}
.dsd-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:760px){.dsd-detail-grid{grid-template-columns:1fr}}
.dsd-panel{background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:14px;padding:12px}
.dsd-panel-title{font-size:12px;font-weight:700;margin-bottom:6px;color:var(--dsw-alias-label-secondary)}
.dsd-spark-meta{display:flex;justify-content:space-between;font-size:10px;color:var(--dsw-alias-label-secondary);margin-top:-2px;padding:0 2px}
.dsd-spark-val{font-variant-numeric:tabular-nums}
.dsd-sec{font-size:11px;font-weight:700;color:var(--dsw-alias-label-secondary);margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid var(--dsw-alias-border-l1)}
.dsd-orders{margin-top:10px;max-height:320px;overflow:auto;border:1px solid var(--dsw-alias-border-l1);border-radius:10px;background:var(--dsw-alias-bg-layer-1)}
.dsd-table{width:100%;border-collapse:collapse;font-size:11px}
.dsd-table th{text-align:left;padding:5px 8px;background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-secondary);font-weight:600;position:sticky;top:0;white-space:nowrap}
.dsd-table td{padding:5px 8px;border-top:1px solid var(--dsw-alias-border-l1);white-space:nowrap}
.dsd-table tbody tr:hover td{background:var(--dsw-alias-bg-layer-2)}
.dsd-num{text-align:right;font-variant-numeric:tabular-nums}
.dsd-hcp{text-align:right;font-variant-numeric:tabular-nums;font-size:10px;color:var(--dsw-alias-label-secondary);padding:5px 3px}
.dsd-td-match{max-width:200px;overflow:hidden;text-overflow:ellipsis}
.dsd-podium{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:760px){.dsd-podium{grid-template-columns:1fr}}
.dsd-podium-card{position:relative;border-radius:16px;padding:18px 12px;text-align:center;cursor:pointer;transition:transform .15s,box-shadow .15s;border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1);overflow:hidden}
.dsd-podium-card:hover{transform:translateY(-3px);box-shadow:0 8px 24px rgba(0,0,0,.16)}
.dsd-podium-card::before{content:'';position:absolute;inset:0;z-index:0;opacity:.55}
.dsd-podium-1::before{background:linear-gradient(160deg,rgba(255,215,0,.20),rgba(255,180,0,.05))}
.dsd-podium-2::before{background:linear-gradient(160deg,rgba(192,192,192,.20),rgba(160,160,160,.05))}
.dsd-podium-3::before{background:linear-gradient(160deg,rgba(205,127,50,.20),rgba(180,100,40,.05))}
.dsd-podium-card>*{position:relative;z-index:1}
.dsd-podium-medal{font-size:32px;line-height:1}
.dsd-podium-name{font-weight:800;font-size:15px;margin:8px 0 2px}
.dsd-podium-sharpe{font-size:11px;color:var(--dsw-alias-label-secondary)}
.dsd-podium-cap{font-size:13px;font-weight:800;margin-top:6px;font-variant-numeric:tabular-nums;color:var(--dsw-alias-brand-primary)}
.dsd-matches-panel{background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:16px;padding:16px}
.dsd-matches-panel .dsd-panel-title{font-size:14px;font-weight:800;margin-bottom:10px;color:var(--dsw-alias-label-primary)}
.dsd-matches-panel .dsd-orders{margin-top:0;max-height:150px;border-radius:12px}
.dsd-matches-panel .dsd-table{font-size:11px}
.dsd-matches-panel .dsd-table td{padding:2px 8px}
.dsd-done td{color:var(--dsw-alias-state-error-primary)}
.dsd-main{display:grid;grid-template-columns:minmax(280px,1fr) minmax(380px,1.7fr);gap:14px;align-items:start}
@media(max-width:900px){.dsd-main{grid-template-columns:1fr}}
.dsts-badge-root{position:relative;display:inline-flex;align-items:center}
.dsts-badge-btn{appearance:none;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary);font-size:12px;line-height:1;padding:6px 10px;border-radius:999px;cursor:pointer;white-space:nowrap;display:inline-flex;align-items:center;gap:4px}
.dsts-badge-btn:hover{border-color:var(--dsw-alias-brand-primary);color:var(--dsw-alias-brand-primary)}
.dsts-panel{position:absolute;right:0;top:calc(100% + 8px);z-index:9999;width:340px;max-height:60vh;overflow:auto;display:flex;flex-direction:column;gap:3px;padding:6px;background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.22);font-size:11px}
.dsts-row{display:flex;align-items:flex-start;gap:6px;padding:4px 6px;border-radius:8px;background:var(--dsw-alias-bg-layer-2)}
.dsts-row.dsts-running{border-left:2px solid var(--dsw-alias-brand-primary)}
.dsts-row.dsts-completed{opacity:.72}
.dsts-row.dsts-failed{border-left:2px solid var(--dsw-alias-state-error-primary)}
.dsts-badge{flex:none;width:12px;text-align:center;font-size:10px;line-height:14px}
.dsts-running .dsts-badge{color:var(--dsw-alias-brand-primary);animation:dsts-pulse 1s infinite}
@keyframes dsts-pulse{0%,100%{opacity:1}50%{opacity:.35}}
.dsts-title{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dsts-phase{color:var(--dsw-alias-label-secondary);word-break:break-all;white-space:normal}
`;

    function fmt(v) {
      if (v == null) return "—";
      var n = Number(v);
      if (isNaN(n)) return String(v);
      return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    }
    function signed(v) {
      if (v == null) return "—";
      var n = Number(v);
      return (n >= 0 ? "+" : "") + fmt(n);
    }
    function pct(x) {
      if (x == null) return "—";
      var n = Number(x);
      return (n >= 0 ? "+" : "") + (n * 100).toFixed(1) + "%";
    }
    function money(v, hide) { return hide ? "•••" : fmt(v); }

    var DOG_META = {
      "alpha2狗": { emoji: "🐕‍🦺", c1: "#ff9a8b", c2: "#ff6a88" },
      "alpha狗": { emoji: "🐺", c1: "#a18cd1", c2: "#fbc2eb" },
      "梭哈2狗": { emoji: "🦁", c1: "#ffd86f", c2: "#fc6262" },
      "梭哈3狗": { emoji: "🐯", c1: "#fbc2eb", c2: "#a6c1ee" },
      "平局狗": { emoji: "🐢", c1: "#43e97b", c2: "#38f9d7" },
      "跟风狗": { emoji: "🦊", c1: "#fa709a", c2: "#fee140" },
      "均注狗": { emoji: "🐿️", c1: "#30cfd0", c2: "#330867" },
      "串关2狗": { emoji: "🎯", c1: "#f6d365", c2: "#fda085" },
    };
    var HAS_AVATAR = { "alpha2狗": 1, "alpha狗": 1, "梭哈2狗": 1, "梭哈3狗": 1, "平局狗": 1, "跟风狗": 1, "均注狗": 1 };
    function metaFor(name) {
      var base = DOG_META[name] || { emoji: "🐶", c1: "#a1c4fd", c2: "#c2e9fb" };
      return { emoji: base.emoji, c1: base.c1, c2: base.c2, img: HAS_AVATAR[name] ? ("/ds-avatars/" + encodeURIComponent(name) + ".jpg") : "" };
    }

    var DIMS = [
      { key: "hitRate", label: "咬中", higher: true },
      { key: "mdd", label: "抗回撤", higher: false },
      { key: "totalCount", label: "单数", higher: true },
      { key: "roi", label: "ROI", higher: true },
      { key: "pnl", label: "净粮", higher: true },
      { key: "fullCapital", label: "满仓", higher: true },
    ];
    var DIM_LABELS = DIMS.map(function (d) { return d.label; });

    function normalize(vals, higher) {
      var min = vals[0], max = vals[0];
      for (var i = 1; i < vals.length; i++) { if (vals[i] < min) min = vals[i]; if (vals[i] > max) max = vals[i]; }
      var range = max - min;
      return vals.map(function (v) {
        if (range === 0) return 50;
        var s = higher ? ((v - min) / range) * 100 : ((max - v) / range) * 100;
        return Math.max(0, Math.min(100, s));
      });
    }
    function buildRadars(dogs) {
      var byName = {};
      dogs.forEach(function (d) { byName[d.name] = []; });
      DIMS.forEach(function (dim) {
        var vals = dogs.map(function (d) { var v = d[dim.key]; return v == null ? 0 : Number(v); });
        var scores = normalize(vals, dim.higher);
        dogs.forEach(function (d, i) { byName[d.name].push(scores[i]); });
      });
      return byName;
    }

    function powerColor(radar) {
      var sum = 0, n = 0;
      for (var i = 0; i < radar.length; i++) { sum += Number(radar[i] || 0); n++; }
      var avg = n ? sum / n : 0;
      return avg >= 50 ? "var(--dsw-alias-state-error-primary)" : "var(--dsw-alias-state-success-primary)";
    }

    function Avatar(props) {
      var meta = props.meta, size = props.size || 40;
      var bg = { width: size + "px", height: size + "px", background: "linear-gradient(135deg," + meta.c1 + "," + meta.c2 + ")" };
      return h("div", { className: "dsd-avatar", style: bg },
        meta.img ? h("img", { className: "dsd-avatar-img", src: meta.img, alt: "" })
          : h("span", { className: "dsd-avatar-emoji", style: { fontSize: Math.round(size * 0.52) + "px" } }, meta.emoji));
    }

    function Radar(props) {
      var vals = props.values || [];
      var labels = props.labels || [];
      var color = props.color || "var(--dsw-alias-brand-primary)";
      var size = props.size || 170;
      var cx = size / 2, cy = size / 2;
      var r = size / 2 - 26;
      var n = 6;
      function ang(i) { return (Math.PI * 2 * i) / n - Math.PI / 2; }
      function px(i, rr) { return cx + rr * Math.cos(ang(i)); }
      function py(i, rr) { return cy + rr * Math.sin(ang(i)); }
      var rings = [0.25, 0.5, 0.75, 1].map(function (f) {
        var pts = "";
        for (var i = 0; i < n; i++) pts += px(i, r * f).toFixed(1) + "," + py(i, r * f).toFixed(1) + " ";
        return pts.trim();
      });
      var spokes = [];
      for (var i = 0; i < n; i++) spokes.push(h("line", { key: "sp" + i, x1: cx, y1: cy, x2: px(i, r), y2: py(i, r), stroke: "var(--dsw-alias-border-l2)", strokeWidth: 1 }));
      var poly = "";
      var dots = [];
      for (var i = 0; i < n; i++) {
        var raw = vals[i] == null ? 0 : Number(vals[i]);
        var v = Math.max(0, Math.min(100, raw));
        poly += px(i, r * (v / 100)).toFixed(1) + "," + py(i, r * (v / 100)).toFixed(1) + " ";
        dots.push(h("circle", { key: "dot" + i, cx: px(i, r * (v / 100)), cy: py(i, r * (v / 100)), r: 2.5, fill: color }));
      }
      poly = poly.trim();
      var labelEls = labels.map(function (lb, i) {
        var x = px(i, r + 13), y = py(i, r + 13);
        var anchor = x < cx - 5 ? "end" : (x > cx + 5 ? "start" : "middle");
        return h("text", { key: "lb" + i, x: x, y: y, textAnchor: anchor, dominantBaseline: "middle", className: "dsd-radar-label" }, lb);
      });
      var gid = "rad" + Math.random().toString(36).slice(2, 8);
      return h("svg", { width: size, height: size, viewBox: "0 0 " + size + " " + size },
        h("defs", null, h("linearGradient", { id: gid, x1: "0", y1: "0", x2: "1", y2: "1" },
          h("stop", { offset: "0%", stopColor: color, stopOpacity: 0.45 }),
          h("stop", { offset: "100%", stopColor: color, stopOpacity: 0.15 }))),
        rings.map(function (pts, i) { return h("polygon", { key: "ring" + i, points: pts, fill: "none", stroke: "var(--dsw-alias-border-l2)", strokeWidth: 1 }); }),
        spokes,
        h("polygon", { points: poly, fill: "url(#" + gid + ")", stroke: color, strokeWidth: 2, strokeLinejoin: "round" }),
        dots,
        labelEls);
    }

    function Sparkline(props) {
      var points = props.points || [];
      var color = props.color || "var(--dsw-alias-brand-primary)";
      var height = props.height || 56;
      if (points.length === 0) return h("div", { className: "dsd-empty" }, "暂无曲线");
      var w = 260, pad = 6;
      var vals = points.map(function (p) { return Number(p.capital); });
      var min = vals[0], max = vals[0];
      for (var i = 1; i < vals.length; i++) { if (vals[i] < min) min = vals[i]; if (vals[i] > max) max = vals[i]; }
      var range = (max - min) || 1;
      var iw = w - pad * 2, ih = height - pad * 2;
      var xs = points.map(function (_, i) { return pad + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw); });
      var ys = vals.map(function (v) { return pad + (1 - (v - min) / range) * ih; });
      var line = "M";
      for (var i = 0; i < points.length; i++) line += xs[i].toFixed(1) + "," + ys[i].toFixed(1) + " ";
      line = line.trim();
      var area = line + " L" + xs[xs.length - 1].toFixed(1) + "," + (height - pad) + " L" + xs[0].toFixed(1) + "," + (height - pad) + " Z";
      var gid = "sp" + Math.random().toString(36).slice(2, 9);
      var last = points[points.length - 1];
      return h("div", { className: "dsd-spark" },
        h("svg", { width: "100%", height: height, viewBox: "0 0 " + w + " " + height, preserveAspectRatio: "none" },
          h("defs", null, h("linearGradient", { id: gid, x1: "0", y1: "0", x2: "0", y2: "1" },
            h("stop", { offset: "0%", stopColor: color, stopOpacity: 0.20 }),
            h("stop", { offset: "100%", stopColor: color, stopOpacity: 0 }))),
          h("path", { d: area, fill: "url(#" + gid + ")" }),
          h("path", { d: line, fill: "none", stroke: color, strokeWidth: 2, strokeLinejoin: "round", strokeLinecap: "round" })),
        h("div", { className: "dsd-spark-meta" },
          h("span", null, last ? last.date : ""),
          h("span", { className: "dsd-spark-val" }, last ? fmt(last.capital) : "")));
    }

    function Stat(label, value, tone) {
      return h("div", { className: "dsd-stat" },
        h("div", { className: "dsd-stat-val " + (tone || "") }, value),
        h("div", { className: "dsd-stat-label" }, label));
    }

    function renderOrders(orders, hide) {
      if (!orders || orders.length === 0) return h("div", { className: "dsd-empty" }, "暂无订单");
      var rows = orders.map(function (o) {
        var tone = o.settled ? (o.profit > 0 ? "dsd-pos" : o.profit < 0 ? "dsd-neg" : "dsd-mut") : "dsd-pend";
        var pnl = o.settled ? (o.profit == null ? "—" : signed(o.profit)) : "⏳ 待投";
        var hcp = o.handicap == null ? "" : (o.betType === "大小球" ? Number(o.handicap).toFixed(2) : ((o.handicap >= 0 ? "+" : "") + Number(o.handicap).toFixed(2)));
        var pick = (o.betType || "") + " " + (o.pickLabel || "");
        var date = (o.settledAt || o.time || "").slice(5) || "—";
        var title = (o.league || "") + (o.reason ? " | " + o.reason : "");
        return h("tr", { key: o.lotaId + "|" + o.betType + "|" + o.pick + "|" + o.settledAt },
          h("td", { className: "dsd-num" }, date),
          h("td", { className: "dsd-td-match", title: title }, (o.match || "—") + (o.league ? " · " + o.league : "")),
          h("td", null, pick),
          h("td", { className: "dsd-num dsd-hcp" }, hcp || "—"),
          h("td", { className: "dsd-num" }, o.score || "—"),
          h("td", { className: "dsd-num" }, o.odds == null ? "—" : "@" + o.odds),
          h("td", { className: "dsd-num dsd-money" }, hide ? "•••" : fmt(o.betSize)),
          h("td", { className: "dsd-num " + tone }, pnl));
      });
      return h("table", { className: "dsd-table" },
        h("thead", null, h("tr", null,
          h("th", { className: "dsd-num" }, "日期"),
          h("th", null, "比赛"),
          h("th", null, "选择"),
          h("th", { className: "dsd-num" }, "盘口"),
          h("th", { className: "dsd-num" }, "比分"),
          h("th", { className: "dsd-num" }, "赔率"),
          h("th", { className: "dsd-num dsd-money" }, "投粮"),
          h("th", { className: "dsd-num" }, "净粮"))),
        h("tbody", null, rows));
    }

    function renderTodayMatches(tm) {
      if (!tm || !tm.matches || tm.matches.length === 0) {
        return h("div", { className: "dsd-empty" }, "当日无竞彩（缓存可能未刷新，跑一次 lota_fetcher.js refresh-range）");
      }
      var rows = tm.matches.map(function (m) {
        var done = m.state === 6;
        var score = done && m.score ? m.score : "";
        var matchText = m.home + (score ? " " + score + " " : "  ") + m.away;
        return h("tr", { key: m.lotaId, className: done ? "dsd-done" : "" },
          h("td", { className: "dsd-num" }, (m.time || "").slice(11, 16)),
          h("td", { className: "dsd-td-match", title: m.number }, matchText),
          h("td", null, m.league || "—"));
      });
      return h("table", { className: "dsd-table" },
        h("thead", null, h("tr", null,
          h("th", { className: "dsd-num" }, "时间"),
          h("th", null, "比赛"),
          h("th", null, "联赛"))),
        h("tbody", null, rows));
    }

    function Podium(props) {
      var top3 = props.rows.slice(0, 3);
      return h("div", { className: "dsd-podium" },
        top3.map(function (r) {
          var medal = r.rank === 1 ? "🥇" : r.rank === 2 ? "🥈" : "🥉";
          return h("div", { key: r.dog.name, className: "dsd-podium-card dsd-podium-" + r.rank, role: "button", tabIndex: 0, onClick: r.onSelect },
            h("div", { className: "dsd-podium-medal" }, medal),
            h("div", { style: { display: "flex", justifyContent: "center", marginTop: 8 } }, Avatar({ meta: r.meta, size: 48 })),
            h("div", { className: "dsd-podium-name" }, r.dog.name),
            h("div", { className: "dsd-podium-sharpe" }, "夏普 " + (r.dog.sharpe == null ? "—" : Number(r.dog.sharpe).toFixed(2))),
            h("div", { className: "dsd-podium-cap" }, money(r.dog.capital, props.hide)));
        }));
    }

    function DogCard(props) {
      var dog = props.dog, radar = props.radar, meta = props.meta, hide = props.hide, onSelect = props.onSelect;
      var color = powerColor(radar);
      return h("div", { className: "dsd-card dsd-card-btn", role: "button", tabIndex: 0, onClick: onSelect },
        h("div", { className: "dsd-card-head" },
          Avatar({ meta: meta, size: 44 }),
          h("div", { className: "dsd-card-name" },
            h("div", { className: "dsd-name" }, dog.name),
            h("div", { className: "dsd-sub" }, "夏普 " + (dog.sharpe == null ? "—" : Number(dog.sharpe).toFixed(2)) + " · 待投 " + dog.pendingCount)),
          h("div", { className: "dsd-cap" }, h("div", { className: "dsd-money dsd-strong" }, money(dog.capital, hide)))),
        h("div", { className: "dsd-radar-wrap" }, Radar({ values: radar, labels: DIM_LABELS, color: color, size: 224 })),
        h("div", { className: "dsd-card-foot" },
          h("span", { className: dog.pnl >= 0 ? "dsd-pos" : "dsd-neg" }, "净粮 " + signed(dog.pnl)),
          h("span", { className: dog.roi >= 0 ? "dsd-pos" : "dsd-neg" }, "ROI " + pct(dog.roi)),
          h("span", null, "进入 ›")));
    }

    function DogDetail(props) {
      var dog = props.dog, radar = props.radar, meta = props.meta, hide = props.hide, onBack = props.onBack;
      var color = powerColor(radar);
      var pending = dog.orders.filter(function (o) { return !o.settled; });
      var settled = dog.orders.filter(function (o) { return o.settled; });
      return h("div", null,
        h("button", { className: "dsd-btn dsd-back", onClick: onBack }, "← 返回"),
        h("div", { className: "dsd-detail-head" },
          h("div", { className: "dsd-avatar-ring" }, Avatar({ meta: meta, size: 112 })),
          h("div", { className: "dsd-detail-info" },
            h("div", { className: "dsd-name", style: { fontSize: 20 } }, dog.name),
            h("div", { className: "dsd-detail-meta" },
              "存粮 " + money(dog.capital, hide) + " · 满仓 " + money(dog.fullCapital, hide) + " · 锁粮 " + money(dog.lockedExposure, hide))),
          h("span", { className: "dsd-power-badge" }, "夏普 " + (dog.sharpe == null ? "—" : Number(dog.sharpe).toFixed(2)))),
        h("div", { className: "dsd-stats" },
          Stat("净粮", signed(dog.pnl), dog.pnl >= 0 ? "dsd-pos" : "dsd-neg"),
          Stat("咬中", dog.hitRate == null ? "—" : (dog.hitRate * 100).toFixed(0) + "%", ""),
          Stat("ROI", dog.roi == null ? "—" : pct(dog.roi), dog.roi >= 0 ? "dsd-pos" : "dsd-neg"),
          Stat("回撤", dog.mdd == null ? "—" : Number(dog.mdd).toFixed(1) + "%", ""),
          Stat("单数", String(dog.totalCount) + "单", ""),
          Stat("夏普", dog.sharpe == null ? "—" : Number(dog.sharpe).toFixed(2), dog.sharpe >= 0 ? "dsd-pos" : "dsd-neg")),
        h("div", { className: "dsd-detail-grid" },
          h("div", { className: "dsd-panel" },
            h("div", { className: "dsd-panel-title" }, "📈 资金曲线"),
            Sparkline({ points: dog.curve, color: color, height: 150 })),
          h("div", { className: "dsd-panel" },
            h("div", { className: "dsd-panel-title" }, "🧬 六维战力"),
            h("div", { className: "dsd-radar-wrap" }, Radar({ values: radar, labels: DIM_LABELS, color: color, size: 190 })))),
        h("div", { className: "dsd-sec" }, "⏳ 待投 · " + pending.length + " 单"),
        pending.length ? h("div", { className: "dsd-orders" }, renderOrders(pending, hide)) : h("div", { className: "dsd-empty" }, "无待投订单"),
        h("div", { className: "dsd-sec" }, "✅ 已结算 · " + settled.length + " 单"),
        settled.length ? h("div", { className: "dsd-orders" }, renderOrders(settled, hide)) : h("div", { className: "dsd-empty" }, "无已结算订单"));
    }

    // 会话头部右侧任务状态徽章：常驻显示运行中数，点击展开任务列表（单狗/群狗都考虑）
    function TaskStatusBadge() {
      var state = React.useState(null);
      var tasks = state[0], setTasks = state[1];
      var openState = React.useState(false);
      var open = openState[0], setOpen = openState[1];
      React.useEffect(function () {
        var timer = setInterval(function () {
          fetch("/ds-tasks")
            .then(function (r) { return r.json(); })
            .then(function (res) {
              if (res && Array.isArray(res.tasks)) setTasks(res.tasks);
            })
            .catch(function () {});
        }, 2500);
        return function () { clearInterval(timer); };
      }, []);

      var list = tasks || [];
      var running = list.filter(function (t) { return t.status === "running"; });
      var shown = (open ? list : list.slice(0, 8));

      var rows = shown.map(function (t) {
        var params = t.params || {};
        var dogs = params.dogs || (params.dog ? [params.dog] : (params.user ? [params.user] : null));
        var pct = t.total > 0 ? Math.round((Number(t.done) || 0) / t.total * 100) : (t.status === "running" ? null : 100);
        var icon = t.status === "running" ? "●" : t.status === "completed" ? "✓" : t.status === "failed" ? "✕" : "‖";
        var title = t.title || t.type || "任务";
        if (dogs && dogs.length) title += " · " + dogs.join("、");
        return h("div", { key: t.id, className: "dsts-row dsts-" + t.status },
          h("span", { className: "dsts-badge" }, icon),
          h("div", { style: { minWidth: 0 } },
            h("div", { className: "dsts-title" }, title),
            h("div", { className: "dsts-phase" },
              t.phase + (pct != null ? " " + pct + "%" : "") +
              (t.detail ? " · " + String(t.detail).slice(0, 80) : ""))));
      });

      return h("div", { className: "dsts-badge-root" },
        h("button", {
          className: "dsts-badge-btn",
          onClick: function () { setOpen(!open); },
          title: "任务状态（运行中/最近）",
        }, "📡 " + running.length + " 运行中"),
        open ? h("div", { className: "dsts-panel" },
          rows.length ? rows : h("div", { className: "dsts-phase" }, "暂无任务（跑分析/结算/因子流时实时显示）")) : null);
    }

    function Dashboard() {
      var dataState = React.useState(null);
      var data = dataState[0], setData = dataState[1];
      var errState = React.useState(null);
      var error = errState[0], setError = errState[1];
      var loadState = React.useState(true);
      var loading = loadState[0], setLoading = loadState[1];
      var hideState = React.useState(false);
      var hideMoney = hideState[0], setHideMoney = hideState[1];
      var selState = React.useState(null);
      var selected = selState[0], setSelected = selState[1];

      function load() {
        setLoading(true);
        fetch("/ds-dashboard")
          .then(function (r) { return r.json(); })
          .then(function (res) {
            if (res && res.error) setError(res.error);
            else { setData(res); setError(null); }
          })
          .catch(function (e) { setError(String((e && e.message) || e)); })
          .then(function () { setLoading(false); });
      }
      React.useEffect(function () { load(); }, []);

      var gen = data && data.generatedAt ? String(data.generatedAt).replace("T", " ").replace("Z", "").slice(0, 19) : "";
      var radars = data ? buildRadars(data.dogs) : {};
      var sorted = data ? data.dogs.slice().sort(function (a, b) {
        var sa = a.sharpe == null ? -Infinity : Number(a.sharpe);
        var sb = b.sharpe == null ? -Infinity : Number(b.sharpe);
        return sb - sa;
      }) : [];
      var rows = sorted.map(function (dog, i) {
        return { dog: dog, meta: metaFor(dog.name), rank: i + 1, onSelect: function () { setSelected(dog.name); } };
      });
      var selDog = selected && data ? (data.dogs.filter(function (d) { return d.name === selected; })[0] || null) : null;

      return h("div", { className: "dsd-root" },
        h("div", { className: "dsd-header" },
          h("div", null,
            h("div", { className: "dsd-h1" }, "🐕 斗狗场"),
            gen ? h("div", { className: "dsd-h2" }, "更新于 " + gen) : null),
          h("div", { className: "dsd-actions" },
            h("button", { className: "dsd-btn", onClick: load, disabled: loading }, loading ? "加载中…" : "🔄 刷新"),
            h("button", { className: "dsd-btn" + (hideMoney ? " on" : ""), onClick: function () { setHideMoney(!hideMoney); } }, hideMoney ? "👁 显示狗粮" : "🙈 隐藏狗粮"))),
        error ? h("div", { className: "dsd-error" }, "⚠️ " + error) : null,
        loading && !data ? h("div", { className: "dsd-empty" }, "加载中…") : null,
        data && !error ? (
          selDog ? h("div", null, DogDetail({ dog: selDog, radar: radars[selDog.name], meta: metaFor(selDog.name), hide: hideMoney, onBack: function () { setSelected(null); } }))
            : h("div", null,
                h("div", { className: "dsd-matches-panel" },
                  h("div", { className: "dsd-panel-title" }, "📋 当日竞彩 · " + (data.todayMatches && data.todayMatches.day ? data.todayMatches.day : "—") + " · " + (data.todayMatches ? data.todayMatches.count : 0) + " 场"),
                  h("div", { className: "dsd-orders" }, renderTodayMatches(data.todayMatches))),
                Podium({ rows: rows, hide: hideMoney }),
                h("div", { className: "dsd-grid" },
                  sorted.map(function (dog) {
                    return DogCard({ key: dog.name, dog: dog, radar: radars[dog.name], meta: metaFor(dog.name), hide: hideMoney, onSelect: function () { setSelected(dog.name); } });
                  })))
        ) : null);
    }

    function apply(ctx) {
      var slots = ctx.get("slots");
      if (!slots) return;
      var style = document.createElement("style");
      style.textContent = css;
      document.head.appendChild(style);
      ctx.effect(function () { return function () { style.remove(); }; }, "ds-dashboard.css");
      slots.inject("conversation.view", function () {
        return slots.register(
          { name: "conversation.view", id: "ds-dashboard", order: 20, label: "斗狗场" },
          function () { return h(Dashboard, null); });
      });
      slots.inject("conversation.session.header.utilities", function () {
        return slots.register(
          { name: "conversation.session.header.utilities", id: "ds-task-status", order: 40 },
          function () { return h(TaskStatusBadge, null); });
      });
    }

    exports.apply = apply;
    return module.exports;
  }
});
//# sourceMappingURL=client.js.map
