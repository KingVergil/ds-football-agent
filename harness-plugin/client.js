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
.dsd-dog-list{display:flex;flex-direction:column;gap:10px}
.dsd-dog-row{display:block}
.dsd-row-card{display:flex;align-items:center;gap:12px;padding:10px 14px;flex-wrap:wrap}
.dsd-row-main{display:flex;align-items:center;gap:12px;flex:1 1 360px;min-width:0}
.dsd-row-name{flex:1;min-width:0}
.dsd-row-metrics{display:flex;align-items:center;gap:16px;text-align:right;flex-shrink:0}
.dsd-row-metric-label{font-size:10px;color:var(--dsw-alias-label-secondary)}
.dsd-row-enter{font-size:11px;color:var(--dsw-alias-label-secondary);white-space:nowrap;margin-left:8px}
.dsd-row-actions{display:flex;align-items:center;gap:8px;min-width:0;flex:1 1 420px}
.dsd-row-actions-title{font-size:11px;font-weight:800;color:var(--dsw-alias-label-secondary);white-space:nowrap}
@media(max-width:1080px){.dsd-row-actions{flex:1 1 100%;justify-content:flex-start}.dsd-row-actions-title{display:none}}
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
.dsd-orders-compact{max-height:340px;margin-top:0}
.dsd-compact-grid{margin-top:14px}
.dsd-table{width:100%;border-collapse:collapse;font-size:11px}
.dsd-table th{text-align:left;padding:5px 8px;background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-secondary);font-weight:600;position:sticky;top:0;white-space:nowrap}
.dsd-table td{padding:5px 8px;border-top:1px solid var(--dsw-alias-border-l1);white-space:nowrap}
.dsd-table tbody tr:hover td{background:var(--dsw-alias-bg-layer-2)}
.dsd-num{text-align:right;font-variant-numeric:tabular-nums}
.dsd-hcp{text-align:right;font-variant-numeric:tabular-nums;font-size:10px;color:var(--dsw-alias-label-secondary);padding:5px 3px}
.dsd-td-match{max-width:200px;overflow:hidden;text-overflow:ellipsis}
.dsd-match-cell{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dsd-reason-line{font-size:10px;color:var(--dsw-alias-label-secondary);white-space:normal;word-break:break-all;max-width:220px;line-height:1.4;margin-top:2px}
.dsd-factor-list{display:flex;flex-direction:column;gap:6px;max-height:320px;overflow:auto}
.dsd-factor-chip{background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:6px 8px}
.dsd-factor-name{font-size:11px;font-weight:800}
.dsd-factor-desc{font-size:10px;color:var(--dsw-alias-label-secondary);line-height:1.4;margin-top:2px;white-space:normal;word-break:break-all}
.dsd-factor-meta{font-size:10px;color:var(--dsw-alias-label-secondary);margin-top:3px;font-variant-numeric:tabular-nums}
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
.dsd-scope{display:inline-flex;align-items:center;gap:4px;font-size:10px;line-height:1;padding:3px 7px;border-radius:999px;border:1px solid var(--dsw-alias-border-l2);color:var(--dsw-alias-label-secondary);background:var(--dsw-alias-bg-layer-2);margin-left:6px;white-space:nowrap}
.dsd-scope.alpha{color:var(--dsw-alias-brand-primary);border-color:var(--dsw-alias-brand-primary)}
.dsd-new{color:var(--dsw-alias-state-warn-primary);border-color:var(--dsw-alias-state-warn-primary)}
.dsd-obs{color:#f0b429;border-color:#f0b429;background:rgba(240,180,41,.08)}
.dsd-running-chip{display:inline-flex;align-items:center;gap:5px;font-size:10px;line-height:1;padding:3px 8px;border-radius:999px;border:1px solid var(--dsw-alias-brand-primary);color:var(--dsw-alias-brand-primary);background:var(--dsw-alias-bg-layer-2);margin-left:6px;white-space:nowrap}
.dsd-running-chip .dot{width:6px;height:6px;border-radius:50%;background:var(--dsw-alias-brand-primary);animation:dsd-pulse 1s infinite}
@keyframes dsd-pulse{0%,100%{opacity:1}50%{opacity:.3}}
.dsd-flow{background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:14px;padding:12px;margin-bottom:14px}
.dsd-flow-title{font-size:12px;font-weight:700;margin-bottom:8px}
.dsd-flow-actions{display:flex;flex-wrap:wrap;gap:6px}
.dsd-flow-btn{appearance:none;font-size:11px;line-height:1;padding:7px 9px;border-radius:9px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);cursor:pointer;white-space:nowrap;transition:border-color .12s,color .12s}
.dsd-flow-btn:hover{border-color:var(--dsw-alias-brand-primary);color:var(--dsw-alias-brand-primary)}
.dsd-flow-hint{font-size:10px;color:var(--dsw-alias-label-secondary);margin-top:7px}
.dsd-replay-panel{background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);border-radius:16px;padding:12px 16px;margin-bottom:16px}
.dsd-replay-item{border-top:1px solid var(--dsw-alias-border-l1);padding:8px 0}
.dsd-replay-item:first-of-type{border-top:none;padding-top:0}
.dsd-replay-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dsd-replay-name{font-size:12px;font-weight:800}
.dsd-replay-meta{font-size:11px;color:var(--dsw-alias-label-secondary)}
.dsd-replay-suggestion{font-size:11px;color:var(--dsw-alias-label-secondary);white-space:pre-wrap;word-break:break-all;max-height:80px;overflow:auto;margin:6px 0;background:var(--dsw-alias-bg-layer-2);border-radius:8px;padding:6px 8px}
.dsd-replay-suggestion-edit{width:100%;box-sizing:border-box;min-height:72px;font-size:11px;line-height:1.5;color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l2);border-radius:8px;padding:6px 8px;resize:vertical;font-family:inherit}
.dsd-replay-suggestion-edit:focus{outline:none;border-color:var(--dsw-alias-brand-primary)}
.dsd-session-badge{display:inline-flex;align-items:center;gap:4px;font-size:10px;line-height:1;padding:3px 8px;border-radius:999px;border:1px solid var(--dsw-alias-border-l2);color:var(--dsw-alias-label-secondary);white-space:nowrap}
.dsd-session-badge.paused{color:var(--dsw-alias-state-warn-primary);border-color:var(--dsw-alias-state-warn-primary)}
.dsd-session-badge.running{color:var(--dsw-alias-brand-primary);border-color:var(--dsw-alias-brand-primary)}
.dsd-session-badge.finished{color:var(--dsw-alias-state-success-primary);border-color:var(--dsw-alias-state-success-primary)}
.dsd-log-box{font-size:10px;line-height:1.5;color:var(--dsw-alias-label-secondary);background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:6px 8px;max-height:140px;overflow:auto;white-space:pre-wrap;word-break:break-all;margin:6px 0}
.dsd-replay-form{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end}
.dsd-replay-form .dsd-field{margin:0}
.dsd-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:640px){.dsd-form-grid{grid-template-columns:1fr}}
.dsd-field{display:flex;flex-direction:column;gap:3px;margin-bottom:10px}
.dsd-field-label{font-size:10px;color:var(--dsw-alias-label-secondary);font-weight:600}
.dsd-input,.dsd-select,.dsd-textarea{box-sizing:border-box;width:100%;font-size:12px;line-height:1.5;color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l2);border-radius:8px;padding:6px 8px;font-family:inherit}
.dsd-input:focus,.dsd-select:focus,.dsd-textarea:focus{outline:none;border-color:var(--dsw-alias-brand-primary)}
.dsd-textarea{min-height:96px;resize:vertical}
.dsd-check-row{display:flex;align-items:center;gap:6px;font-size:12px;padding:6px 0}
.dsd-check-row input{margin:0}
.dsd-color-row{display:flex;align-items:center;gap:8px}
.dsd-color-row .dsd-input{width:96px}
.dsd-form-hint{font-size:10px;color:var(--dsw-alias-label-secondary);margin-top:4px;line-height:1.5}
.dsd-form-error{border:1px solid var(--dsw-alias-state-error-primary);color:var(--dsw-alias-state-error-primary);border-radius:8px;padding:6px 10px;font-size:11px;margin-bottom:10px;background:var(--dsw-alias-bg-layer-1)}
.dsd-replay-section{margin-top:4px}
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
    var FALLBACK_DOG_META = [
      { emoji: "🐕", c1: "#a1c4fd", c2: "#c2e9fb" },
      { emoji: "🐩", c1: "#f6d365", c2: "#fda085" },
      { emoji: "🐕‍🦺", c1: "#84fab0", c2: "#8fd3f4" },
      { emoji: "🦮", c1: "#fbc2eb", c2: "#a6c1ee" },
    ];
    function fallbackMeta(name) {
      var sum = 0;
      for (var i = 0; i < name.length; i++) sum = (sum * 31 + name.charCodeAt(i)) >>> 0;
      return FALLBACK_DOG_META[sum % FALLBACK_DOG_META.length];
    }
    function metaFor(name, dog) {
      var base = DOG_META[name] || fallbackMeta(name);
      var img = dog && dog.avatarUrl ? dog.avatarUrl : "";
      return {
        emoji: (dog && dog.emoji) || base.emoji,
        c1: (dog && dog.c1) || base.c1,
        c2: (dog && dog.c2) || base.c2,
        img: img,
      };
    }
    function scopeLabel(scope) {
      if (scope === "beidan") return "北单";
      if (scope === "all") return "全量";
      return "竞彩";
    }
    function taskLabel(t) {
      if (!t) return "";
      if (t.type === "analyze" || t.type === "analyze-direct") return "⚡ 分析中";
      if (t.type === "replay") return "▶️ 回放中";
      if (t.type === "settle" || t.type === "settle-flow") return "🧾 结算中";
      if (t.type === "factor-flow" || t.type === "factor-induction" || t.type === "factor-review") return "🧬 因子流";
      if (t.type === "role") return "📖 读数据";
      return "⏳ 运行中";
    }
    /** 运行中任务 → 狗名映射（分析/回放/结算/角色工具都能挂到对应狗上）。 */
    function runningDogTasks(tasks) {
      var pri = { "analyze-direct": 0, "analyze": 0, "replay": 1, "settle": 2, "settle-flow": 2, "factor-flow": 3 };
      var list = (tasks || []).filter(function (t) { return t.status === "running"; });
      list.sort(function (a, b) {
        return (pri[a.type] != null ? pri[a.type] : 9) - (pri[b.type] != null ? pri[b.type] : 9);
      });
      var map = {};
      list.forEach(function (t) {
        var p = t.params || {};
        var names = [];
        if (typeof p.dog === "string" && p.dog) names.push(p.dog);
        if (typeof p.user === "string" && p.user) names.push(p.user);
        if (Array.isArray(p.dogs)) names = names.concat(p.dogs);
        names.forEach(function (n) { if (!map[n]) map[n] = t; });
      });
      return map;
    }
    function RunningChip(props) {
      var t = props.task;
      if (!t) return null;
      var pct = t.total > 0 ? Math.round((Number(t.done) || 0) / t.total * 100) + "%" : "";
      var text = taskLabel(t) + (pct ? " " + pct : "") + (t.phase ? " · " + t.phase : "");
      return h("span", {
        className: "dsd-running-chip",
        title: text + (t.detail ? "｜" + String(t.detail).slice(0, 120) : ""),
      },
        h("span", { className: "dot" }),
        text);
    }
    function bjDate(ts) {
      return new Date(ts + 8 * 3600 * 1000).toISOString().slice(0, 10);
    }
    function footballDayBj() {
      return bjDate(Date.now() - (12 * 3600 + 60) * 1000);
    }
    function daysAgoBj(n) {
      return bjDate(Date.now() - n * 24 * 3600 * 1000);
    }

    // 按钮只存在于斗狗场页面内：点击后复制对应指令，到会话输入框粘贴发送。
    function copyText(text) {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          var cp = navigator.clipboard.writeText(text);
          if (cp && cp.catch) cp.catch(function () {});
        }
      } catch (e) { /* 剪贴板失败不阻断 */ }
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
        var date = (o.matchDay || o.settledAt || o.time || "").slice(5) || "—";
        var title = (o.league || "") + (o.reason ? " | " + o.reason : "");
        return h("tr", { key: o.lotaId + "|" + o.betType + "|" + o.pick + "|" + (o.matchDay || o.settledAt || o.time) },
          h("td", { className: "dsd-num" }, date),
          h("td", { className: "dsd-td-match", title: title },
            h("div", { className: "dsd-match-cell" }, (o.match || "—") + (o.league ? " · " + o.league : "")),
            !o.settled && o.reason ? h("div", { className: "dsd-reason-line" }, "理由：" + o.reason) : null),
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

    function renderFactorList(factors) {
      if (factors == null || factors.length === 0) return h("div", { className: "dsd-empty" }, "无活跃因子（全部休眠/退役或尚未归纳）");
      return h("div", { className: "dsd-factor-list" },
        factors.map(function (f) {
          return h("div", { key: f.factor, className: "dsd-factor-chip" },
            h("div", { className: "dsd-factor-name" }, f.factor),
            f.desc ? h("div", { className: "dsd-factor-desc" }, f.desc) : null,
            h("div", { className: "dsd-factor-meta" },
              "样本 " + f.total + " · 命中 " + f.hit + " · 净 " + signed(f.profit) + (f.lastSeen ? " · 最近 " + f.lastSeen : "")));
        }));
    }

    function renderTodayMatches(tm) {
      if (!tm || !tm.matches || tm.matches.length === 0) {
        return h("div", { className: "dsd-empty" }, "当日无竞彩（缓存可能未刷新，等定时任务或去斗狗场点刷新）");
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

    function singleDogActionDefs(dog) {
      var name = dog.name || "";
      var fday = footballDayBj();
      var today = bjDate(Date.now());
      return [
        {
          id: "analyze",
          label: "⚡ 分析",
          title: "python 桥直启：live 分析（数据准备 + LLM 决策 + 下单）",
          func: "analyze",
          payload: { dog: name, func: "analyze", day: fday, opts: { live: true, prefetched: false, jingcai_only: true } },
        },
        {
          id: "settle",
          label: "🧾 结算",
          title: "python 桥直启：结算某足球日未结算订单",
          func: "settle",
          payload: { dog: name, func: "settle", day: fday, opts: {} },
        },
        {
          id: "induct",
          label: "🧬 归纳",
          title: "python 桥直启：因子归纳/去重（alpha 狗的跨狗逻辑由引擎内部判定）",
          func: "factor-induction",
          payload: { dog: name, func: "factor-induction", opts: {} },
        },
        {
          id: "review",
          label: "🪦 Review",
          title: "python 桥直启：因子退役评估（结构性，可带 user_notes）",
          func: "factor-review",
          payload: { dog: name, func: "factor-review", end: today, start: daysAgoBj(6), opts: {} },
        },
        {
          id: "status",
          label: "📊 状态",
          title: "python 桥直启：资金/待结算/因子状态",
          func: "status",
          payload: { dog: name, func: "status", opts: {} },
        },
      ];
    }

    function DogFlowActions(props) {
      var dog = props.dog, hint = props.hint;
      var actions = singleDogActionDefs(dog);
      var msgState = React.useState("");
      var msg = msgState[0], setMsg = msgState[1];
      var busyState = React.useState(false);
      var busy = busyState[0], setBusy = busyState[1];

      // 全部按钮 = 直接 POST /ds-run（python 桥，不经 LLM/不经 bash），进度经 /ds-tasks 轮询。
      function runFunc(def) {
        if (busy) return;
        setBusy(true);
        setMsg("");
        fetch("/ds-run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(def.payload),
        })
          .then(function (r) { return r.json().then(function (body) { return { status: r.status, body: body }; }); })
          .then(function (res) {
            setBusy(false);
            if (res.body && res.body.ok) {
              setMsg("✅ " + (res.body.message || "已启动（📡 任务徽章看进度，结果自动刷新）"));
            } else {
              setMsg("⚠️ " + ((res.body && res.body.error) || ("启动失败 HTTP " + res.status)));
            }
          })
          .catch(function (e) {
            setBusy(false);
            setMsg("⚠️ " + String((e && e.message) || e));
          });
      }

      return h("div", null,
        h("div", { className: "dsd-flow-actions" },
          actions.map(function (a) {
            return h("button", {
              key: a.id,
              className: "dsd-flow-btn",
              title: a.title,
              disabled: busy,
              onClick: function () {
                runFunc(a);
              },
            }, a.label);
          })),
        (msg || hint) ? h("div", { className: "dsd-flow-hint" }, msg || hint) : null);
    }

    function ReplaySessionsPanel(props) {
      var sessions = props.sessions || [];
      var paused = sessions.filter(function (s) { return s.status === "paused"; });
      function resume(sandbox, body) {
        if (props.onFire) {
          var cmd = "续跑回放：调用 ds_replay(sandbox=\"" + sandbox + "\"";
          if (body.action === "to_end") cmd += ", to_end=true";
          else cmd += ", induction_notes=" + JSON.stringify(body.induction_notes || "维持现有因子方向，收紧退役标准");
          cmd += ")。";
          props.onFire(cmd);
        }
      }
      if (!paused.length) return null;
      return h("div", { className: "dsd-replay-panel" },
        h("div", { className: "dsd-panel-title" }, "⏸ 半交互回放暂停 · " + paused.length + " 个会话（点会话名进入该狗详情操作）"),
        paused.map(function (s) {
          var dogLabel = s.dog || "全部";
          var meta = "已完成 " + s.daysDone + "/" + s.daysTotal + " 天 · 剩余 " + s.remainingDays + " 天 · 下一天 " + (s.nextDay || "—") + " · " + dogLabel;
          return h("div", { key: s.name, className: "dsd-replay-item" },
            h("div", { className: "dsd-replay-head" },
              h("span", {
                className: "dsd-replay-name",
                style: { cursor: "pointer", textDecoration: "underline dotted" },
                title: "进入详情页操作该会话",
                onClick: function () { if (props.onSelect) props.onSelect(s); },
              }, s.name),
              h("span", { className: "dsd-replay-meta" }, meta),
              h("button", {
                className: "dsd-flow-btn",
                title: "把已有方向建议作为 induction_notes 继续下一周期",
                onClick: function () { resume(s.name, { action: "continue", induction_notes: s.directionSuggestion || "维持现有因子方向，收紧退役标准" }); },
              }, "▶ 继续"),
              h("button", {
                className: "dsd-flow-btn",
                title: "剩余周期一路到底，不再暂停",
                onClick: function () { resume(s.name, { action: "to_end" }); },
              }, "⏩ 一路到底")),
            s.directionSuggestion ? h("div", { className: "dsd-replay-suggestion" }, s.directionSuggestion) : null,
            s.lastError ? h("div", { className: "dsd-error" }, s.lastError) : null);
        }));
    }

    function isDayStr(s) {
      return typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s);
    }

    /** 主页「创建狗」表单：覆盖当前狗的全部设置（人设/范围/初始资金/α模式/头像配色）。 */
    function CreateDogPanel(props) {
      var dogs = props.dogs || [];
      var initForm = {
        name: "",
        copyFrom: "",
        persona: "",
        scope: "jc",
        initial_capital: "10000",
        alpha_mode: false,
        max_exposure_pct: "40",
        truncate: false,
        max_orders: "",
        min_orders: "",
        enabled: false,
        emoji: "",
        c1: "#a1c4fd",
        c2: "#c2e9fb",
      };
      var formState = React.useState(initForm);
      var form = formState[0], setForm = formState[1];
      var errState = React.useState("");
      var error = errState[0], setError = errState[1];
      var busyState = React.useState(false);
      var busy = busyState[0], setBusy = busyState[1];

      function set(key, val) {
        var n = Object.assign({}, form);
        n[key] = val;
        setForm(n);
      }
      function loadDefaultPersona(force) {
        fetch("/ds-persona/" + encodeURIComponent("跟风狗"))
          .then(function (r) { return r.json(); })
          .then(function (res) {
            var text = res && typeof res.persona === "string" ? res.persona.trim() : "";
            if (!text) return;
            setForm(function (prev) {
              if (!force && prev.persona && prev.persona.trim()) return prev;
              return Object.assign({}, prev, { persona: text });
            });
          })
          .catch(function () {});
      }
      // 默认人设：跟风狗（可编辑；清空提交则由后端同样回退到跟风狗人设）
      React.useEffect(function () { loadDefaultPersona(false); }, []);
      function pickSource(name) {
        if (!name) {
          setForm(function (prev) { return Object.assign({}, prev, { copyFrom: "" }); });
          loadDefaultPersona(true);
          return;
        }
        var d = dogs.filter(function (x) { return x.name === name; })[0] || null;
        if (!d) return;
        setForm(Object.assign({}, form, {
          copyFrom: name,
          scope: d.scope || "jc",
          initial_capital: d.initialCapital != null ? String(d.initialCapital) : "10000",
          alpha_mode: !!d.alphaMode,
          max_exposure_pct: d.limits && d.limits.max_exposure_pct != null ? String(d.limits.max_exposure_pct) : "40",
          truncate: !!(d.limits && d.limits.truncate),
          max_orders: d.limits && d.limits.max_orders != null ? String(d.limits.max_orders) : "",
          min_orders: d.limits && d.limits.min_orders != null ? String(d.limits.min_orders) : "",
          enabled: !!d.enabled,
          emoji: d.emoji || "",
          c1: d.c1 || "#a1c4fd",
          c2: d.c2 || "#c2e9fb",
        }));
        fetch("/ds-persona/" + encodeURIComponent(name))
          .then(function (r) { return r.json(); })
          .then(function (res) {
            if (res && res.persona) set("persona", res.persona);
          })
          .catch(function () {});
      }
      function submit() {
        if (!form.name.trim()) { setError("请先填狗名"); return; }
        setBusy(true);
        setError("");
        fetch("/ds-dogs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: form.name.trim(),
            persona: form.persona,
            scope: form.scope,
            initial_capital: Number(form.initial_capital) || 10000,
            alpha_mode: form.alpha_mode,
            limits: {
              max_exposure_pct: form.max_exposure_pct === "" ? null : Number(form.max_exposure_pct),
              truncate: form.truncate,
              max_orders: form.max_orders === "" ? null : Number(form.max_orders),
              min_orders: form.min_orders === "" ? null : Number(form.min_orders),
            },
            enabled: form.enabled,
            emoji: form.emoji,
            c1: form.c1,
            c2: form.c2,
          }),
        })
          .then(function (r) { return r.json(); })
          .then(function (res) {
            setBusy(false);
            if (res && res.ok) {
              if (props.onCreated) props.onCreated(res.dog);
            } else {
              setError((res && res.error) || "创建失败");
            }
          })
          .catch(function (e) {
            setBusy(false);
            setError(String((e && e.message) || e));
          });
      }

      var input = function (key, opts) {
        opts = opts || {};
        return h("input", {
          className: "dsd-input",
          type: opts.type || "text",
          placeholder: opts.placeholder || "",
          value: form[key] == null ? "" : String(form[key]),
          onChange: function (ev) { set(key, ev.target.value); },
        });
      };

      return h("div", { className: "dsd-panel", style: { marginBottom: 16 } },
        h("div", { className: "dsd-panel-title" }, "➕ 创建新狗 · 设置齐全（人设/范围/资金/限额/α模式/观察期/配色）"),
        error ? h("div", { className: "dsd-form-error" }, "⚠️ " + error) : null,
        h("div", { className: "dsd-form-grid" },
          h("div", { className: "dsd-field" },
            h("label", { className: "dsd-field-label" }, "狗名（必填，中英文/数字/下划线）"),
            input("name", { placeholder: "如 猎犬1号" })),
          h("div", { className: "dsd-field" },
            h("label", { className: "dsd-field-label" }, "复制自（可选：套用现有狗的设置）"),
            h("select", {
              className: "dsd-select",
              value: form.copyFrom,
              onChange: function (ev) { pickSource(ev.target.value); },
            },
              h("option", { value: "" }, "不复制"),
              dogs.map(function (d) { return h("option", { key: d.name, value: d.name }, d.name); })))),
        h("div", { className: "dsd-field" },
          h("label", { className: "dsd-field-label" }, "人设 persona（默认已填「跟风狗」人设，可修改；可稍后在 data/roles/<狗名>/persona.md 补充）"),
          h("textarea", {
            className: "dsd-textarea",
            value: form.persona,
            onChange: function (ev) { set("persona", ev.target.value); },
          })),
        h("div", { className: "dsd-form-grid" },
          h("div", { className: "dsd-field" },
            h("label", { className: "dsd-field-label" }, "日常比赛范围"),
            h("select", {
              className: "dsd-select",
              value: form.scope,
              onChange: function (ev) { set("scope", ev.target.value); },
            },
              h("option", { value: "jc" }, "竞彩 jc（默认）"),
              h("option", { value: "beidan" }, "北单 beidan"),
              h("option", { value: "all" }, "全量 all"))),
          h("div", { className: "dsd-field" },
            h("label", { className: "dsd-field-label" }, "初始资金（initial_capital）"),
            input("initial_capital", { type: "number" }))),
        h("div", { className: "dsd-form-grid" },
          h("div", null,
            h("label", { className: "dsd-check-row" },
              h("input", {
                type: "checkbox",
                checked: !!form.alpha_mode,
                onChange: function (ev) { set("alpha_mode", ev.target.checked); },
              }), "α 模式（alpha 跨狗归纳狗）")),
          h("div", { className: "dsd-field" },
            h("label", { className: "dsd-field-label" }, "表情（默认按名字哈希兜底）"),
            input("emoji", { placeholder: "如 🐶" }))),
        h("div", { className: "dsd-form-grid" },
          h("div", { className: "dsd-field" },
            h("label", { className: "dsd-field-label" }, "单日总仓上限 max_exposure_pct (%)"),
            input("max_exposure_pct", { type: "number", placeholder: "40" })),
          h("div", { className: "dsd-field" },
            h("label", { className: "dsd-field-label" }, "单数上限 max_orders（留空=不限）"),
            input("max_orders", { type: "number", placeholder: "不限" }))),
        h("div", { className: "dsd-form-grid" },
          h("div", null,
            h("label", { className: "dsd-check-row" },
              h("input", {
                type: "checkbox",
                checked: !!form.truncate,
                onChange: function (ev) { set("truncate", ev.target.checked); },
              }), "超仓整单丢弃 truncate（不勾=等比缩放）")),
          h("div", null,
            h("label", { className: "dsd-check-row" },
              h("input", {
                type: "checkbox",
                checked: !!form.enabled,
                onChange: function (ev) { set("enabled", ev.target.checked); },
              }), "进全量默认列表（不勾=👀 观察期，仅显式指定/看板可见）"))),
        h("div", { className: "dsd-field" },
          h("label", { className: "dsd-field-label" }, "头像配色（渐变起止色；头像图片可放 头像/<狗名>.png 自动识别）"),
          h("div", { className: "dsd-color-row" },
            input("c1", { type: "text" }),
            h("span", { className: "dsd-form-hint" }, "→"),
            input("c2", { type: "text" }))),
        h("div", { className: "dsd-form-hint" }, "创建即同步：注册表 dogs.json + ds_roles + Python 角色（roles/<狗>/persona.md 为人设唯一源，<狗>.json 存限额，已存在不覆盖）。默认观察期，仅显式指定或勾选后进全量。"),
        h("div", { className: "dsd-flow-actions", style: { marginTop: 10 } },
          h("button", { className: "dsd-btn", onClick: submit, disabled: busy }, busy ? "创建中…" : "✅ 创建"),
          h("button", { className: "dsd-btn", onClick: function () { if (props.onClose) props.onClose(); } }, "取消")));
    }

    /** 详情页「回放」区：范围/选项 → 启动；本狗的回放会话（paused 可编辑方向续跑/回退/一路到底）。 */
    function ReplaySection(props) {
      var dog = props.dog;
      var name = dog.name || "";
      var today = bjDate(Date.now());
      var startState = React.useState(daysAgoBj(6));
      var start = startState[0], setStart = startState[1];
      var endState = React.useState(today);
      var end = endState[0], setEnd = endState[1];
      var modeState = React.useState("interactive");
      var mode = modeState[0], setMode = modeState[1];
      var everyState = React.useState("7");
      var every = everyState[0], setEvery = everyState[1];
      var resetState = React.useState("none");
      var reset = resetState[0], setReset = resetState[1];
      var restoreState = React.useState(false); // 模拟跑：结束还原起点
      var restore = restoreState[0], setRestore = restoreState[1];
      var skipLlmState = React.useState(false); // 演示模式：跳过 LLM 秒级跑完，直接看交互卡片
      var skipLlm = skipLlmState[0], setSkipLlm = skipLlmState[1];
      var errState = React.useState("");
      var error = errState[0], setError = errState[1];
      // 每沙箱一份可编辑方向草稿（sandbox → 文本）；轮询刷新时保留用户编辑
      var draftsState = React.useState({});
      var drafts = draftsState[0], setDrafts = draftsState[1];
      var rewindDayState = React.useState({});
      var rewindDay = rewindDayState[0], setRewindDay = rewindDayState[1];

      var sessions = (props.sessions || []).filter(function (s) {
        return s.dog === name;
      }).slice(0, 5);
      var tasks = (props.tasks || []).filter(function (t) {
        if (t.type !== "replay") return false;
        var p = t.params || {};
        return !p.dog || p.dog === name;
      });

      function draftFor(s) {
        return drafts[s.name] != null ? drafts[s.name] : (s.directionSuggestion || "");
      }
      function setDraft(sandbox, val) {
        var n = Object.assign({}, drafts);
        n[sandbox] = val;
        setDrafts(n);
      }
      function setRewind(sandbox, val) {
        var n = Object.assign({}, rewindDay);
        n[sandbox] = val;
        setRewindDay(n);
      }

      function startReplay() {
        if (!isDayStr(start) || !isDayStr(end)) { setError("日期必须是 YYYY-MM-DD"); return; }
        if (start > end) { setError("起始日不能晚于结束日"); return; }
        setError("");
        var e = Math.max(1, Math.min(30, Number(every) || 7));
        var resetArg = reset === "zero" ? ', reset="zero"' : "";
        var restoreArg = restore ? ", restore_after=true" : "";
        var skipLlmArg = skipLlm ? ", skip_llm=true" : "";
        var cmd = "启动回放：调用 ds_replay(dog=\"" + name + "\", start=\"" + start + "\", end=\"" + end
          + "\", mode=\"" + mode + "\", factor_review_every=" + e
          + resetArg + restoreArg + skipLlmArg + ")。半交互暂停后把 direction_suggestion 呈现给用户确认/编辑，再带 induction_notes 续跑。";
        if (props.onFire) props.onFire(cmd);
      }

      var field = function (labelText, ctrl, hint) {
        return h("div", { className: "dsd-field" },
          h("label", { className: "dsd-field-label" }, labelText),
          ctrl,
          hint ? h("div", { className: "dsd-form-hint" }, hint) : null);
      };

      var form = h("div", { className: "dsd-panel" },
        h("div", { className: "dsd-panel-title" }, "▶️ 回放 · " + name),
        h("div", { className: "dsd-replay-form" },
          field("起始足球日", h("input", {
            className: "dsd-input", type: "date", value: start,
            onChange: function (ev) { setStart(ev.target.value); },
          })),
          field("结束足球日（含）", h("input", {
            className: "dsd-input", type: "date", value: end,
            onChange: function (ev) { setEnd(ev.target.value); },
          })),
          field("模式", h("select", {
            className: "dsd-select", value: mode,
            onChange: function (ev) { setMode(ev.target.value); },
          },
            h("option", { value: "interactive" }, "半交互（每周期暂停）"),
            h("option", { value: "auto" }, "一路到底"))),
          field("退役周期(天)", h("input", {
            className: "dsd-input", type: "number", min: "1", max: "30", value: every,
            style: { width: 64 },
            onChange: function (ev) { setEvery(ev.target.value); },
          })),
          field("起点", h("select", {
            className: "dsd-select", value: reset,
            onChange: function (ev) { setReset(ev.target.value); },
          },
            h("option", { value: "none" }, "当前状态"),
            h("option", { value: "zero" }, "从 0 重置"))),
          field("结束处理", h("select", {
            className: "dsd-select", value: restore ? "yes" : "no",
            onChange: function (ev) { setRestore(ev.target.value === "yes"); },
          },
            h("option", { value: "no" }, "沙箱保留（默认，待转正）"),
            h("option", { value: "yes" }, "还原起点（模拟）"))),
          field("演示模式", h("select", {
            className: "dsd-select", value: skipLlm ? "yes" : "no",
            onChange: function (ev) { setSkipLlm(ev.target.value === "yes"); },
          },
            h("option", { value: "no" }, "正常（调 LLM）"),
            h("option", { value: "yes" }, "跳过 LLM（秒级，看交互）"))),
          h("div", { className: "dsd-field" },
            h("button", { className: "dsd-btn on", onClick: startReplay }, "🚀 开始回放"))),
        error ? h("div", { className: "dsd-form-error" }, "⚠️ " + error) : null,
        h("div", { className: "dsd-form-hint" },
          "沙箱回放：replays/sandboxes/<狗>_<MMDD>/，桥写沙箱 workspace，线上零影响；老狗复制到起始日结算后/因子归纳前。交给会话 agent 启动并接管（agent 调 ds_replay 工具）。半交互暂停后：对话区 agent 呈现方向建议，本区卡片可编辑，确认后带 induction_notes 续跑；完成后可转正（替换线上）或放弃。"));

      var sessionCards = sessions.map(function (s) {
        var badgeCls = "dsd-session-badge " + (s.status || "");
        var badgeText = s.status === "paused" ? "⏸ 已暂停" : s.status === "running" ? "▶ 运行中" : s.status === "finished" ? "✓ 已完成" : "‖ " + s.status;
        var meta = s.start + " ~ " + s.end + " · " + s.daysDone + "/" + s.daysTotal + " 天"
          + (s.status === "paused" ? " · 下一天 " + (s.nextDay || "—") : "")
          + (s.status === "finished" ? " · 沙箱完成，待转正/放弃" : "")
          + (s.skipLlm ? " · 演示模式" : "");
        var body = [];
        if (s.status === "paused") {
          body.push(
            h("div", { className: "dsd-field" },
              h("label", { className: "dsd-field-label" }, "下一轮因子归纳/退役方向（可编辑，将作为 induction_notes 注入下一周期）"),
              h("textarea", {
                className: "dsd-replay-suggestion-edit",
                value: draftFor(s),
                onChange: function (ev) { setDraft(s.name, ev.target.value); },
              })));
          body.push(
            h("div", { className: "dsd-flow-actions" },
              h("button", {
                className: "dsd-flow-btn",
                title: "把编辑后的方向作为 induction_notes 继续下一周期",
                onClick: function () {
                  if (props.onFire) props.onFire("续跑回放：调用 ds_replay(sandbox=\"" + s.name + "\", induction_notes=" + JSON.stringify(draftFor(s) || "维持现有因子方向，收紧退役标准") + ")。");
                },
              }, "▶ 继续（应用方向）"),
              h("button", {
                className: "dsd-flow-btn",
                title: "剩余周期一路到底，不再暂停",
                onClick: function () {
                  if (props.onFire) props.onFire("续跑回放：调用 ds_replay(sandbox=\"" + s.name + "\", to_end=true)。");
                },
              }, "⏩ 一路到底"),
              h("input", {
                className: "dsd-input",
                type: "date",
                style: { width: 140 },
                value: rewindDay[s.name] || s.nextDay || "",
                onChange: function (ev) { setRewind(s.name, ev.target.value); },
              }),
              h("button", {
                className: "dsd-flow-btn",
                title: "回到某天开始状态重跑（恢复前一天终态并截断其后轨迹）",
                onClick: function () {
                  var d = rewindDay[s.name] || s.nextDay || "";
                  if (!d) return;
                  if (props.onFire) props.onFire("回退回放：调用 ds_replay(sandbox=\"" + s.name + "\", rewind_to=\"" + d + "\")。");
                },
              }, "⏪ 回退到该日")));
        } else if (s.status === "running") {
          var task = tasks.filter(function (t) { return t.status === "running"; })[0] || null;
          var pct = task && task.total > 0 ? Math.round((Number(task.done) || 0) / task.total * 100) : null;
          body.push(
            h("div", { className: "dsd-replay-meta" },
              "任务进度：" + (task ? (task.phase || "") + (pct != null ? " " + pct + "%" : "") + (task.detail ? " · " + String(task.detail).slice(0, 60) : "") : "准备中…")));
          if (s.logTail && s.logTail.length) {
            body.push(h("div", { className: "dsd-log-box" }, s.logTail.join("\n")));
          }
        } else if (s.status === "finished") {
          body.push(
            h("div", { className: "dsd-replay-meta" },
              "报告：" + (s.reportExists ? "replays/sandboxes/" + s.name + "/report.md" : "生成中…") +
              (s.factsExists ? " ｜事实：replays/sandboxes/" + s.name + "/facts.json" : "")),
            h("div", { className: "dsd-flow-actions" },
              h("button", {
                className: "dsd-flow-btn",
                title: "把沙箱 workspace 整目录替换线上（先备份）",
                onClick: function () {
                  if (!window.confirm("确认转正？线上 roles/" + name + " 会被沙箱替换（已自动备份到 backups/promote_*）。")) return;
                  fetch("/ds-sandbox/" + encodeURIComponent(s.name) + "/promote", { method: "POST" }).catch(function () {});
                },
              }, "✅ 转正（替换线上）"),
              h("button", {
                className: "dsd-flow-btn",
                title: "放弃沙箱，线上不动",
                onClick: function () {
                  if (!window.confirm("确认放弃沙箱 " + s.name + "？线上不受影响。")) return;
                  fetch("/ds-sandbox/" + encodeURIComponent(s.name) + "/abort", { method: "POST" }).catch(function () {});
                },
              }, "🗑 放弃")));
        }
        if (s.lastError) body.push(h("div", { className: "dsd-form-error" }, "⚠️ " + s.lastError));
        return h("div", { key: s.name, className: "dsd-replay-item" },
          h("div", { className: "dsd-replay-head" },
            h("span", { className: "dsd-replay-name" }, s.name),
            h("span", { className: badgeCls }, badgeText),
            h("span", { className: "dsd-replay-meta" }, meta)),
          body);
      });

      return h("div", { className: "dsd-replay-section" },
        form,
        sessions.length
          ? h("div", { className: "dsd-panel", style: { marginTop: 12 } },
              h("div", { className: "dsd-panel-title" }, "📼 " + name + " 的回放会话"),
              sessionCards)
          : h("div", { className: "dsd-form-hint", style: { marginBottom: 12 } }, "暂无该狗的回放沙箱（replays/sandboxes/<狗>_<MMDD>/）"));
    }

    function Podium(props) {
      var top3 = props.rows.slice(0, 3);
      return h("div", { className: "dsd-podium" },
        top3.map(function (r) {
          var medal = r.rank === 1 ? "🥇" : r.rank === 2 ? "🥈" : "🥉";
          return h("div", { key: r.dog.name, className: "dsd-podium-card dsd-podium-" + r.rank, role: "button", tabIndex: 0, onClick: r.onSelect },
            h("div", { className: "dsd-podium-medal" }, medal),
            h("div", { style: { display: "flex", justifyContent: "center", marginTop: 8 } }, Avatar({ meta: r.meta, size: 48 })),
            h("div", { className: "dsd-podium-name" }, r.dog.name,
              r.dog.observation ? h("span", { className: "dsd-scope dsd-obs" }, "👀 观察") : null),
            h("div", { className: "dsd-podium-sharpe" }, "夏普 " + (r.dog.sharpe == null ? "—" : Number(r.dog.sharpe).toFixed(2))),
            h("div", { className: "dsd-podium-cap" }, money(r.dog.capital, props.hide)));
        }));
    }

    function DogRow(props) {
      var dog = props.dog, meta = props.meta, hide = props.hide, onSelect = props.onSelect, onFire = props.onFire, sessionId = props.sessionId;
      var runningTask = props.runningTask || null;
      var sub = "夏普 " + (dog.sharpe == null ? "—" : Number(dog.sharpe).toFixed(2)) + " · " + scopeLabel(dog.scope) + (dog.alphaMode ? " · α" : "") + " · 待投 " + dog.pendingCount + (dog.inStorage === false ? " · 未初始化" : "");
      return h("div", { className: "dsd-dog-row" },
        h("div", { className: "dsd-card dsd-row-card" },
          h("div", { className: "dsd-row-main dsd-card-btn", role: "button", tabIndex: 0, onClick: onSelect },
            Avatar({ meta: meta, size: 44 }),
            h("div", { className: "dsd-row-name" },
              h("div", { className: "dsd-name" }, dog.name,
                RunningChip({ task: runningTask }),
                h("span", { className: "dsd-scope" + (dog.alphaMode ? " alpha" : "") }, scopeLabel(dog.scope)),
                dog.inStorage === false ? h("span", { className: "dsd-scope dsd-new" }, "新") : null,
                dog.observation ? h("span", { className: "dsd-scope dsd-obs" }, "👀 观察") : null),
              h("div", { className: "dsd-sub" }, sub)),
            h("div", { className: "dsd-row-metrics" },
              h("div", null,
                h("div", { className: "dsd-money dsd-strong" }, money(dog.capital, hide)),
                h("div", { className: "dsd-row-metric-label" }, "存粮")),
              h("div", null,
                h("div", { className: dog.pnl >= 0 ? "dsd-pos" : "dsd-neg" }, signed(dog.pnl)),
                h("div", { className: "dsd-row-metric-label" }, "净粮")),
              h("div", null,
                h("div", { className: dog.roi >= 0 ? "dsd-pos" : "dsd-neg" }, pct(dog.roi)),
                h("div", { className: "dsd-row-metric-label" }, "ROI")),
              h("span", { className: "dsd-row-enter" }, "详情 ›"))),
          h("div", { className: "dsd-row-actions" },
            h("span", { className: "dsd-row-actions-title" }, "操作 · 分析直接启动"),
            h(DogFlowActions, { dog: dog, onFire: onFire, sessionId: sessionId }))));
    }

    function DogDetail(props) {
      var dog = props.dog, radar = props.radar, meta = props.meta, hide = props.hide, onBack = props.onBack;
      var runningTask = props.runningTask || null;
      var color = powerColor(radar);
      var pending = dog.orders.filter(function (o) { return !o.settled; });
      var settled = dog.orders.filter(function (o) { return o.settled; });
      return h("div", null,
        h("button", { className: "dsd-btn dsd-back", onClick: onBack }, "← 返回"),
        h("div", { className: "dsd-detail-head" },
          h("div", { className: "dsd-avatar-ring" }, Avatar({ meta: meta, size: 112 })),
          h("div", { className: "dsd-detail-info" },
            h("div", { className: "dsd-name", style: { fontSize: 20 } }, dog.name,
              RunningChip({ task: runningTask }),
              h("span", { className: "dsd-scope" + (dog.alphaMode ? " alpha" : "") }, scopeLabel(dog.scope)),
              dog.inStorage === false ? h("span", { className: "dsd-scope dsd-new" }, "未初始化") : null,
              dog.observation ? h("span", { className: "dsd-scope dsd-obs" }, "👀 观察") : null),
            h("div", { className: "dsd-detail-meta" },
              "存粮 " + money(dog.capital, hide) + " · 满仓 " + money(dog.fullCapital, hide) + " · 锁粮 " + money(dog.lockedExposure, hide) + (dog.initialCapital != null ? " · 初始 " + money(dog.initialCapital, hide) : ""))),
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
        // ▶️ 回放区（范围/选项/启动 + 会话交互），订单列表在回放运行中由轮询实时刷新
        h(ReplaySection, {
          dog: dog,
          sessions: props.sessions || [],
          tasks: props.tasks || [],
          onFire: props.onFire,
        }),
        h("div", { className: "dsd-detail-grid dsd-compact-grid" },
          h("div", { className: "dsd-panel" },
            h("div", { className: "dsd-panel-title" }, "📋 订单 · 待投 " + pending.length + " · 已结算 " + settled.length),
            h("div", { className: "dsd-orders dsd-orders-compact" }, renderOrders(pending.concat(settled), hide))),
          h("div", { className: "dsd-panel" },
            h("div", { className: "dsd-panel-title" }, "🧬 正在应用因子 · " + ((dog.factors || []).length) + " 个"),
            renderFactorList(dog.factors || []))));
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

    function Dashboard(props) {
      var inputActions = props && props.inputActions;
      var sessionId = props && props.sessionId;
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
      var createState = React.useState(false);
      var showCreate = createState[0], setShowCreate = createState[1];
      var prepMsgState = React.useState("");
      var prepMsg = prepMsgState[0], setPrepMsg = prepMsgState[1];
      var settleAllMsgState = React.useState("");
      var settleAllMsg = settleAllMsgState[0], setSettleAllMsg = settleAllMsgState[1];
      var inductAllMsgState = React.useState("");
      var inductAllMsg = inductAllMsgState[0], setInductAllMsg = inductAllMsgState[1];

      // 顶部「准备」：某足球日数据预取（全局，不属单狗）→ POST /ds-run func=prepare
      function prepToday() {
        setPrepMsg("");
        fetch("/ds-run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ func: "prepare", day: footballDayBj(), opts: { mode: "live", jingcai_only: true } }),
        })
          .then(function (r) { return r.json().then(function (body) { return { status: r.status, body: body }; }); })
          .then(function (res) {
            if (res.body && res.body.ok) setPrepMsg("✅ " + (res.body.message || "数据准备已启动（📡 看进度）"));
            else setPrepMsg("⚠️ " + ((res.body && res.body.error) || ("启动失败 HTTP " + res.status)));
          })
          .catch(function (e) { setPrepMsg("⚠️ " + String((e && e.message) || e)); });
      }

      // 顶部「结算全部」：对所有 live 狗并行发起当天足球日结算（POST /ds-run func=settle）
      function settleAll() {
        setSettleAllMsg("");
        var dogs = (data && data.dogs ? data.dogs.filter(function (d) { return d.enabled; }) : []);
        if (!dogs.length) { setSettleAllMsg("⚠️ 当前没有 live 狗可结算"); return; }
        var day = footballDayBj();
        Promise.all(dogs.map(function (d) {
          return fetch("/ds-run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dog: d.name, func: "settle", day: day, opts: {} }),
          }).then(function (r) { return r.json().catch(function () { return {}; }); });
        })).then(function (res) {
          var ok = res.filter(function (x) { return x && x.ok; }).length;
          setSettleAllMsg("✅ 已发起 " + ok + "/" + dogs.length + " 只狗结算（" + day + "，📡 看进度）");
        }).catch(function (e) {
          setSettleAllMsg("⚠️ " + String((e && e.message) || e));
        });
      }

      // 顶部「归纳全部」：batch 模式——非 alpha 并行 → 结束后 alpha barrier 串行（POST /ds-induct-all）
      function inductAll() {
        setInductAllMsg("");
        fetch("/ds-induct-all", { method: "POST" })
          .then(function (r) { return r.json().then(function (body) { return { status: r.status, body: body }; }); })
          .then(function (res) {
            if (res.body && res.body.ok) {
              setInductAllMsg("✅ " + (res.body.message || "归纳全部已启动（📡 看进度）"));
            } else {
              setInductAllMsg("⚠️ " + ((res.body && res.body.error) || ("启动失败 HTTP " + res.status)));
            }
          })
          .catch(function (e) { setInductAllMsg("⚠️ " + String((e && e.message) || e)); });
      }

      function load(silent) {
        if (!silent) setLoading(true);
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
      // 常规轮询（静默）：保证 detail 里的订单/会话状态及时跟上
      React.useEffect(function () {
        var timer = setInterval(function () { load(true); }, 10000);
        return function () { clearInterval(timer); };
      }, []);
      // 忙时快刷：有运行中任务或运行中回放会话时每 4 秒刷一次（回放逐日下单 → 订单列表实时变化）
      React.useEffect(function () {
        if (!data) return;
        var busy = (data.tasks || []).some(function (t) { return t.status === "running"; })
          || (data.replays || []).some(function (s) { return s.status === "running"; });
        if (!busy) return;
        var timer = setInterval(function () { load(true); }, 4000);
        return function () { clearInterval(timer); };
      }, [data]);

      function firePrompt(prompt) {
        if (inputActions && typeof inputActions.setDraft === "function" && typeof inputActions.submit === "function") {
          inputActions.setDraft(prompt);
          inputActions.submit("queue");
          return;
        }
        copyText(prompt);
      }

      var gen = data && data.generatedAt ? String(data.generatedAt).replace("T", " ").replace("Z", "").slice(0, 19) : "";
      var radars = data ? buildRadars(data.dogs) : {};
      var sorted = data ? data.dogs.slice().sort(function (a, b) {
        var sa = a.sharpe == null ? -Infinity : Number(a.sharpe);
        var sb = b.sharpe == null ? -Infinity : Number(b.sharpe);
        return sb - sa;
      }) : [];
      var rows = sorted.map(function (dog, i) {
        return {
          dog: dog,
          meta: metaFor(dog.name, dog),
          rank: i + 1,
          onSelect: function () { setSelected(dog.name); },
        };
      });
      var selDog = selected && data ? (data.dogs.filter(function (d) { return d.name === selected; })[0] || null) : null;
      // 运行中任务 → 狗名：每只狗一行「运行中」徽章（分析/回放/结算/因子流都能看到）
      var runningMap = runningDogTasks(data && data.tasks);

      return h("div", { className: "dsd-root" },
        h("div", { className: "dsd-header" },
          h("div", null,
            h("div", { className: "dsd-h1" }, "🐕 斗狗场"),
            gen ? h("div", { className: "dsd-h2" }, "更新于 " + gen) : null),
          h("div", { className: "dsd-actions" },
            h("button", { className: "dsd-btn", onClick: load, disabled: loading }, loading ? "加载中…" : "🔄 刷新"),
            h("button", { className: "dsd-btn", onClick: prepToday, title: "预取当天足球日比赛 + 赔率/特征段（全局数据准备）" }, "📦 准备"),
            h("button", { className: "dsd-btn", onClick: settleAll, title: "对所有 live 狗并行结算当天足球日" }, "🧾 结算全部"),
            h("button", { className: "dsd-btn", onClick: inductAll, title: "归纳全部：非 alpha 并行 → alpha barrier 串行（等价 batch_agents.sh factor-induction）" }, "🧬 归纳全部"),
            h("button", { className: "dsd-btn", onClick: function () { setSelected(null); setShowCreate(!showCreate); }, disabled: !!selDog }, showCreate ? "✖ 关闭创建" : "➕ 创建狗"),
            h("button", { className: "dsd-btn" + (hideMoney ? " on" : ""), onClick: function () { setHideMoney(!hideMoney); } }, hideMoney ? "👁 显示狗粮" : "🙈 隐藏狗粮"))),
        prepMsg ? h("div", { className: "dsd-flow-hint", style: { marginBottom: 8 } }, prepMsg) : null,
        settleAllMsg ? h("div", { className: "dsd-flow-hint", style: { marginBottom: 8 } }, settleAllMsg) : null,
        inductAllMsg ? h("div", { className: "dsd-flow-hint", style: { marginBottom: 8 } }, inductAllMsg) : null,
        error ? h("div", { className: "dsd-error" }, "⚠️ " + error) : null,
        loading && !data ? h("div", { className: "dsd-empty" }, "加载中…") : null,
        data && !error ? (
          selDog ? h("div", null, DogDetail({
            dog: selDog,
            radar: radars[selDog.name],
            meta: metaFor(selDog.name, selDog),
            hide: hideMoney,
            onBack: function () { setSelected(null); },
            sessions: data.replays || [],
            tasks: data.tasks || [],
            onFire: firePrompt,
            runningTask: runningMap[selDog.name] || null,
          }))
            : h("div", null,
                showCreate ? h(CreateDogPanel, {
                  dogs: sorted,
                  onClose: function () { setShowCreate(false); },
                  onCreated: function (dog) {
                    setShowCreate(false);
                    load();
                    if (dog && dog.name) setSelected(dog.name);
                  },
                }) : null,
                h("div", { className: "dsd-matches-panel" },
                  h("div", { className: "dsd-panel-title" }, "📋 当日竞彩 · " + (data.todayMatches && data.todayMatches.day ? data.todayMatches.day : "—") + " · " + (data.todayMatches ? data.todayMatches.count : 0) + " 场"),
                  h("div", { className: "dsd-orders" }, renderTodayMatches(data.todayMatches))),
                ReplaySessionsPanel({
                  sessions: data.replays || [],
                  onFire: firePrompt,
                  onSelect: function (s) {
                    var first = s.dog || null;
                    if (first && sorted.some(function (d) { return d.name === first; })) setSelected(first);
                  },
                }),
                Podium({ rows: rows, hide: hideMoney }),
                h("div", { className: "dsd-dog-list" },
                  sorted.map(function (dog) {
                    return DogRow({
                      key: dog.name,
                      dog: dog,
                      meta: metaFor(dog.name, dog),
                      hide: hideMoney,
                      onSelect: function () { setSelected(dog.name); },
                      onFire: firePrompt,
                      sessionId: sessionId,
                      runningTask: runningMap[dog.name] || null,
                    });
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
          {
            name: "conversation.view",
            id: "ds-dashboard",
            order: 20,
            label: "斗狗场",
            // 会话上下文（对齐 trajectory 插件）：把当前 sessionId 注入视图 props，
            // 分析直启时带给 host，让子分析代理挂在本会话 agent 下 → 对话区实时可见。
            inject: function (sessionId) { return { sessionId: sessionId }; },
          },
          function (props) { return h(Dashboard, props || {}); });
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
