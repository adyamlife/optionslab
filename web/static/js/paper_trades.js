/**
 * Paper Trades Dashboard
 * Phase 1 Refactored: Uses centralized utilities, config, and event manager
 */

// ── Helpers ───────────────────────────────────────────────────────────────────

// NOTE: These helper functions are defined in utils.js (Phase 1 refactoring)
// They are available globally when utils.js is loaded
// Fallback definitions for backwards compatibility

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// Derive option type suffix ("C", "P", or "") from structure name.
// Used to annotate strikes so B200C/S227.5C is unambiguous.
function _optSuffix(structure) {
  const s = (structure ?? "").toLowerCase();
  if (s.includes("call")) return "C";
  if (s.includes("put"))  return "P";
  return "";
}

// Format strikes with Buy/Sell role labels and C/P suffix.
// Returns e.g. "B200C / S227.5C", "B260P / S240P", "S22C",
// "BPut40 / SPut45 · SCall50 / BCall55" for 4-leg structures.
function formatStrikes(stk, structure, isDebit) {
  if (!stk) return "—";
  const opt = _optSuffix(structure);
  if (stk.put_long != null) {
    // 4-leg (IC / IBF): show each leg with role and type
    return `B${stk.put_long}P / S${stk.put_short}P · S${stk.call_short}C / B${stk.call_long}C`;
  }
  if (stk.short != null && stk.long != null) {
    return isDebit
      ? `B${stk.long}${opt} / S${stk.short}${opt}`
      : `S${stk.short}${opt} / B${stk.long}${opt}`;
  }
  if (stk.short != null) return `S${stk.short}${opt}`;
  return "—";
}

function fmt$(v, dec=2) {
  if (v == null) return "—";
  const n = parseFloat(v);
  return (n >= 0 ? "+" : "") + "$" + Math.abs(n).toFixed(dec);
}

function fmtPct(v) {
  if (v == null) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
}

function cls$(v) {
  if (v == null) return "na";
  return parseFloat(v) >= 0 ? "pass" : "fail";
}

function statusLabel(status) {
  const MAP = {
    open:           ["na",   "Open"],
    expired_profit: ["pass", "Expired — Win"],
    expired_loss:   ["fail", "Expired — Loss"],
    closed_target:  ["pass", "Closed at Target"],
    closed_stop:    ["fail", "Stopped Out"],
  };
  const [cls, label] = MAP[status] ?? ["na", status];
  return `<span class="pt-status-badge pt-status-${cls}">${label}</span>`;
}

function dteLabel(expiry) {
  if (!expiry) return "—";
  const today = new Date().toISOString().slice(0, 10);
  const diff  = Math.round((new Date(expiry) - new Date(today)) / 86400000);
  if (diff < 0)  return `<span class="fail">Expired</span>`;
  if (diff === 0) return `<span class="warn">Expires today</span>`;
  const cls = diff <= 3 ? "warn" : "pass";
  return `<span class="${cls}">${diff}d left</span>`;
}

function reasonLabel(reason) {
  const MAP = {
    expired:       "Expired",
    profit_target: "Profit Target",
    stop_loss:     "Stop Loss",
    max_profit:    "Max Profit Reached",
  };
  return MAP[reason] ?? reason;
}

// ── Summary cards ─────────────────────────────────────────────────────────────

function renderSummaryCards(data) {
  const o = data.overall || {};
  const noData = !o.count;

  function card(title, value, cls, sub) {
    return `
      <div class="pt-card">
        <div class="pt-card-title">${title}</div>
        <div class="pt-card-value ${cls ?? ""}">${value}</div>
        ${sub ? `<div class="pt-card-sub muted">${sub}</div>` : ""}
      </div>`;
  }

  const wr    = noData ? "—" : o.win_rate + "%";
  const wrCls = noData ? "na" : o.win_rate >= 60 ? "pass" : o.win_rate >= 45 ? "na" : "fail";
  const exp   = noData ? "—" : fmt$(o.expectancy, 3);
  const expCls= noData ? "na" : cls$(o.expectancy);
  const total = noData ? "—" : fmt$(o.total_pnl);
  const totCls= noData ? "na" : cls$(o.total_pnl);

  return `
    ${card("Open Trades",    data.open_count   ?? 0, "na")}
    ${card("Closed Trades",  data.closed_count ?? 0, "na", noData ? "Need trades to show stats" : `${o.wins}W / ${o.losses}L`)}
    ${card("Win Rate",       wr,    wrCls,  noData ? null : `Avg win ${fmt$(o.avg_win,3)} / Avg loss ${fmt$(o.avg_loss,3)}`)}
    ${card("Expectancy/sh",  exp,   expCls, "avg P&L per trade (per share)")}
    ${card("Total P&L",      total, totCls, "1 contract per trade")}`;
}

// ── Equity curve (SVG) ────────────────────────────────────────────────────────

function renderEquityCurve(points) {
  if (!points || points.length < 2) {
    document.getElementById("pt-equity-section").style.display = "none";
    return;
  }
  document.getElementById("pt-equity-section").style.display = "";

  const vals      = points.map(p => p.cumulative);
  const lastVal   = vals[vals.length - 1];
  const lineColor = lastVal >= 0 ? "#4caf50" : "#e53935";
  const fillColor = lastVal >= 0 ? "rgba(76,175,80,0.12)" : "rgba(229,57,53,0.12)";

  const winDots  = points.filter(p => p.win);
  const lossDots = points.filter(p => !p.win);

  const dotTrace = (pts, color, name) => ({
    x: pts.map(p => p.date),
    y: pts.map(p => p.cumulative),
    mode: "markers",
    type: "scatter",
    name,
    marker: { color, size: 7, opacity: 0.85 },
    customdata: pts.map(p => [p.ticker, p.structure, p.pnl]),
    hovertemplate: "%{customdata[0]} %{customdata[1]}<br>Trade P&L: %{customdata[2]:+$.2f}<br>Cumulative: %{y:+$.2f}<extra></extra>",
  });

  const traces = [
    {
      x: points.map(p => p.date),
      y: vals,
      mode: "lines",
      type: "scatter",
      name: "Cumulative P&L",
      line: { color: lineColor, width: 2 },
      fill: "tozeroy",
      fillcolor: fillColor,
      hoverinfo: "skip",
    },
    dotTrace(winDots,  "#4caf50", "Win"),
    dotTrace(lossDots, "#e53935", "Loss"),
  ];

  const isDark = document.documentElement.dataset.theme === "dark"
              || (document.documentElement.dataset.theme == null
                  && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const axisColor  = isDark ? "#666" : "#aaa";
  const labelColor = isDark ? "#aaa" : "#555";

  const layout = {
    paper_bgcolor: "transparent",
    plot_bgcolor:  "transparent",
    margin: { t: 12, b: 48, l: 64, r: 16 },
    height: 220,
    showlegend: false,
    xaxis: {
      type: "category",
      tickfont: { size: 11, color: labelColor },
      gridcolor: "transparent",
      linecolor: axisColor,
      tickcolor: axisColor,
      nticks: 8,
    },
    yaxis: {
      tickprefix: "$",
      tickfont: { size: 11, color: labelColor },
      gridcolor: isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)",
      zerolinecolor: isDark ? "rgba(255,255,255,0.25)" : "rgba(0,0,0,0.25)",
      zerolinewidth: 1,
      linecolor: axisColor,
    },
    hovermode: "closest",
  };

  Plotly.react("pt-equity-chart", traces, layout, {
    responsive: true,
    displayModeBar: false,
  });
}

// ── Breakdown table ───────────────────────────────────────────────────────────

function renderBreakdown(containerId, data) {
  const el = document.getElementById(containerId);
  if (!data || !Object.keys(data).length) {
    el.innerHTML = `<p class="muted na">No closed trades yet.</p>`;
    return;
  }
  const rows = Object.entries(data).map(([label, s]) => {
    if (!s || s.count === 0) return "";
    const wrCls = s.win_rate >= 60 ? "pass" : s.win_rate >= 45 ? "na" : "fail";
    return `
      <tr>
        <td>${esc(label)}</td>
        <td class="na">${s.count}</td>
        <td class="${wrCls}">${s.win_rate ?? "—"}%</td>
        <td class="${cls$(s.expectancy)}">${fmt$(s.expectancy, 3)}</td>
        <td class="${cls$(s.total_pnl)}">${fmt$(s.total_pnl)}</td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <table class="journal-table">
      <thead><tr><th>Category</th><th>#</th><th>Win %</th><th>Expect./sh</th><th>Total P&L</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── Progress bar ──────────────────────────────────────────────────────────────

function buildProgressBar(credit, mark, target, stop, isDebit = false) {
  if (isDebit) {
    const pct       = Math.min(100, Math.max(0, (mark / credit) * 100));
    const targetPct = Math.min(98, (target / credit) * 100);
    const color     = mark >= target ? "#4caf50" : mark <= (credit - stop) ? "#e53935" : "#3a7bd5";
    return `
      <div class="pt-progress-bar" title="Spread val: $${mark.toFixed(3)}  Target: $${target.toFixed(3)}  Max: $${credit.toFixed(3)}">
        <div class="pt-progress-fill" style="width:${pct.toFixed(1)}%;background:${color}"></div>
        <div class="pt-progress-target" style="left:${targetPct.toFixed(1)}%"></div>
      </div>`;
  }
  const maxMark   = Math.max(stop, mark, credit * 1.1);
  const pct       = Math.min(100, Math.max(0, (1 - mark / maxMark) * 100));
  const targetPct = Math.min(98, (1 - target / maxMark) * 100);
  const color     = mark <= target ? "#4caf50" : mark >= stop ? "#e53935" : "#3a7bd5";
  return `
    <div class="pt-progress-bar" title="Mark: $${mark.toFixed(3)}  Target: $${target.toFixed(3)}  Stop: $${stop.toFixed(3)}">
      <div class="pt-progress-fill" style="width:${pct.toFixed(1)}%;background:${color}"></div>
      <div class="pt-progress-target" style="left:${targetPct.toFixed(1)}%"></div>
    </div>`;
}

// ── Open trade card (collapsed by default) ────────────────────────────────────

function buildMcBar(trade) {
  const p10 = trade.entry_mc_expiry_p10;
  const p25 = trade.entry_mc_expiry_p25;
  const p50 = trade.entry_mc_expiry_p50;
  const p75 = trade.entry_mc_expiry_p75;
  const p90 = trade.entry_mc_expiry_p90;
  if (p10 == null || p90 == null) return "";

  const model = (trade.entry_distribution_model_version ?? "").toUpperCase();
  const isBackfill = model.startsWith("BACKFILL");
  const modelShort = isBackfill
    ? "Backfill estimate"
    : model.startsWith("GARCH") ? "GARCH model" : model.startsWith("GBM") ? "GBM model" : model;

  const spot = trade.spot_at_entry;
  const entryTag = spot != null
    ? `<span class="pt-mc-entry-tag">Entry <strong>$${spot.toFixed(2)}</strong></span>`
    : "";

  const divId = `mc-chart-${trade.id.replace(/[^a-z0-9]/gi, "")}`;
  return `<div class="pt-mc-wrap">
    <div class="pt-mc-header">
      <span class="pt-mc-title">Where is <strong>${esc(trade.ticker)}</strong> likely to be at expiry? ${entryTag}</span>
      <span class="pt-mc-modeltag${isBackfill ? " pt-mc-backfill" : ""}" title="${esc(model)}">${esc(modelShort)}</span>
    </div>
    <div id="${divId}" class="pt-mc-plotly"></div>
  </div>`;
}

function renderMcChart(trade) {
  const p10 = trade.entry_mc_expiry_p10;
  const p25 = trade.entry_mc_expiry_p25;
  const p50 = trade.entry_mc_expiry_p50;
  const p75 = trade.entry_mc_expiry_p75;
  const p90 = trade.entry_mc_expiry_p90;
  if (p10 == null || p90 == null) return;

  const divId = `mc-chart-${trade.id.replace(/[^a-z0-9]/gi, "")}`;
  const el = document.getElementById(divId);
  if (!el || typeof Plotly === "undefined") return;

  const spot = trade.spot_at_entry ?? null;
  const pad  = (p90 - p10) * 0.12;
  const xmin = p10 - pad;
  const xmax = p90 + pad;
  const fmt  = v => "$" + v.toFixed(2);

  // Colour tokens from CSS vars (read computed)
  const style   = getComputedStyle(document.documentElement);
  const accent  = style.getPropertyValue("--accent").trim()       || "#4f8ef7";
  const textMut = style.getPropertyValue("--text-muted").trim()   || "#888";
  const bgCard  = style.getPropertyValue("--bg-card").trim()      || "#1e1e2e";

  // ── Traces ────────────────────────────────────────────────────────────
  // Stacked horizontal bars: left-gap | left-tail | core-50 | right-tail | right-gap
  const traces = [
    // invisible left spacer
    { type:"bar", orientation:"h", x:[p10-xmin], base:xmin,
      y:[""], marker:{color:"rgba(0,0,0,0)"}, hoverinfo:"skip", showlegend:false },
    // left tail  p10→p25
    { type:"bar", orientation:"h", x:[p25-p10], base:p10,
      y:[""],
      name:"Outer 25% (lower)",
      hovertemplate:`<b>Lower zone</b><br>25% chance price is between ${fmt(p10)} and ${fmt(p25)}<extra></extra>`,
      marker:{color:"rgba(251,191,36,0.35)", line:{color:"rgba(251,191,36,0.7)", width:1}} },
    // core 50%  p25→p75
    { type:"bar", orientation:"h", x:[p75-p25], base:p25,
      y:[""],
      name:"Middle 50%",
      hovertemplate:`<b>Most likely zone</b><br>50% chance price is between ${fmt(p25)} and ${fmt(p75)}<extra></extra>`,
      marker:{color:"rgba(79,142,247,0.55)", line:{color:"rgba(79,142,247,0.9)", width:1}} },
    // right tail  p75→p90
    { type:"bar", orientation:"h", x:[p90-p75], base:p75,
      y:[""],
      name:"Outer 25% (upper)",
      hovertemplate:`<b>Upper zone</b><br>25% chance price is between ${fmt(p75)} and ${fmt(p90)}<extra></extra>`,
      marker:{color:"rgba(251,191,36,0.35)", line:{color:"rgba(251,191,36,0.7)", width:1}} },
    // invisible right spacer
    { type:"bar", orientation:"h", x:[xmax-p90], base:p90,
      y:[""], marker:{color:"rgba(0,0,0,0)"}, hoverinfo:"skip", showlegend:false },
  ];

  // ── Shapes: median line only ──────────────────────────────────────────
  const shapes = [
    { type:"line", x0:p50, x1:p50, y0:-0.45, y1:0.45, xref:"x", yref:"y",
      line:{color:accent, width:2.5, dash:"solid"} },
  ];

  // ── Annotations: price labels ─────────────────────────────────────────
  const labelY = -0.62;
  const annotations = [
    { x:p10, y:labelY, xref:"x", yref:"y", text:fmt(p10), showarrow:false,
      font:{size:10, color:textMut}, xanchor:"center" },
    { x:p25, y:labelY, xref:"x", yref:"y", text:fmt(p25), showarrow:false,
      font:{size:10, color:"rgb(251,191,36)"}, xanchor:"center" },
    { x:p50, y:labelY, xref:"x", yref:"y",
      text:`<b>${fmt(p50)}</b><br><span style="font-size:9px">expected</span>`,
      showarrow:false, font:{size:10, color:accent}, xanchor:"center" },
    { x:p75, y:labelY, xref:"x", yref:"y", text:fmt(p75), showarrow:false,
      font:{size:10, color:"rgb(251,191,36)"}, xanchor:"center" },
    { x:p90, y:labelY, xref:"x", yref:"y", text:fmt(p90), showarrow:false,
      font:{size:10, color:textMut}, xanchor:"center" },
  ];

  // ── Layout ────────────────────────────────────────────────────────────
  const layout = {
    barmode: "stack",
    height: 110,
    margin: { t:22, b:38, l:4, r:4 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor:  "rgba(0,0,0,0)",
    xaxis: {
      range: [xmin, xmax],
      tickformat: "$.2f",
      showgrid: false, zeroline: false,
      showticklabels: false,
      fixedrange: true,
    },
    yaxis: { showticklabels:false, showgrid:false, zeroline:false, fixedrange:true },
    shapes,
    annotations,
    showlegend: false,
    hovermode: "closest",
  };

  Plotly.newPlot(el, traces, layout, {
    displayModeBar: false,
    responsive: true,
    staticPlot: false,
  });
}

function buildTradeCard(trade, liveData) {
  const live      = liveData ?? {};
  const mark      = live.mark       ?? trade.latest_mark;
  const unrealized= live.unrealized ?? trade.latest_unrealized;
  const loading   = liveData === null;
  const isDebit   = (trade.structure ?? "").includes("Debit")
                 || ["Long Strangle","Calendar Spread","Diagonal Spread"].includes(trade.structure);

  const maxProfit = isDebit ? (trade.max_profit ?? 0) : (trade.entry_credit ?? 0);
  const debitPaid = trade.entry_credit ?? null;

  const pctDone = (mark != null && maxProfit > 0)
    ? (isDebit
        ? Math.round(((mark - (trade.entry_credit ?? 0)) / maxProfit) * 100)
        : Math.round((1 - mark / maxProfit) * 100))
    : null;

  const unrCls = unrealized == null ? "na" : parseFloat(unrealized) >= 0 ? "pass" : "fail";
  const unrStr = unrealized != null ? fmt$(unrealized, 3) : (loading ? "…" : "—");
  const pctCls = pctDone == null ? "na" : pctDone >= 90 ? "pass" : pctDone >= 50 ? "na" : "muted";
  const markStr = mark != null ? "$" + mark.toFixed(3) : (loading ? "…" : "—");

  const liveDot = loading
    ? `<span class="pt-live-dot pt-live-dot-loading" title="Fetching…"></span>`
    : live.error
      ? `<span class="pt-live-dot pt-live-dot-err" title="${esc(live.error)}"></span>`
      : live.mark != null
        ? `<span class="pt-live-dot pt-live-dot-ok" title="Live quote"></span>`
        : ``;

  const progressBar = (mark != null && trade.profit_target != null && trade.stop_loss != null)
    ? buildProgressBar(maxProfit, mark, trade.profit_target, trade.stop_loss, isDebit) : "";

  // strikes display — B=buy (long), S=sell (short), with C/P suffix
  const stk = trade.strikes ?? {};
  const strikesStr = formatStrikes(stk, trade.structure, isDebit);

  // preserve expanded state across re-renders
  const wasExpanded = (() => {
    const el = document.querySelector(`.pt-trade-card[data-id="${CSS.escape(trade.id)}"]`);
    return el ? el.classList.contains("pt-expanded") : false;
  })();

  return `
    <div class="pt-trade-card${wasExpanded ? " pt-expanded" : ""}" data-id="${esc(trade.id)}">

      <div class="tc-header" role="button" tabindex="0" aria-expanded="${wasExpanded}">

        <div class="pt-hdr-row1">
          <span class="pt-card-ticker">${esc(trade.ticker)}</span>
          ${liveDot}
          <span class="pt-price-badge-slot"></span>
          <span class="pt-card-struct">${esc(trade.structure)}</span>
          <span class="pt-card-rank muted">#${trade.rank}</span>
          <span class="pt-hdr-sep"></span>
          <span class="pt-hdr-stat">
            <span class="pt-hdr-label">P&amp;L/sh</span>
            <span class="pt-hdr-val ${unrCls}">${unrStr}</span>
          </span>
          <span class="pt-hdr-stat">
            <span class="pt-hdr-label">% target</span>
            <span class="pt-hdr-val ${pctCls}">${pctDone != null ? pctDone + "%" : "—"}</span>
          </span>
          <span class="pt-hdr-stat">
            <span class="pt-hdr-label">DTE</span>
            <span class="pt-hdr-val">${dteLabel(trade.expiry)}</span>
          </span>
          <span class="pu-verdict-badge pu-verdict-loading">···</span>
          <button class="pt-collapse-btn" title="Expand / collapse" aria-label="Toggle details">▼</button>
        </div>

        <div class="pt-hdr-row2">
          <span class="pt-hdr-entry-item">
            <span class="pt-hdr-entry-label">Entered</span>
            <span class="pt-hdr-entry-val">${(trade.entered_at ?? "").slice(0,10) || "—"}</span>
          </span>
          <span class="pt-hdr-dot">·</span>
          <span class="pt-hdr-entry-item">
            <span class="pt-hdr-entry-label">Expiry</span>
            <span class="pt-hdr-entry-val">${esc(trade.expiry ?? "—")}</span>
          </span>
          <span class="pt-hdr-dot">·</span>
          <span class="pt-hdr-entry-item">
            <span class="pt-hdr-entry-label">Strikes</span>
            <span class="pt-hdr-entry-val">${esc(strikesStr)}</span>
          </span>
          <span class="pt-hdr-dot">·</span>
          <span class="pt-hdr-entry-item">
            <span class="pt-hdr-entry-label">${isDebit ? "Debit paid" : "Credit"}</span>
            <span class="pt-hdr-entry-val">$${maxProfit.toFixed(2)}</span>
          </span>
          <span class="pt-hdr-dot">·</span>
          <span class="pt-hdr-entry-item">
            <span class="pt-hdr-entry-label">Spot at entry</span>
            <span class="pt-hdr-entry-val">$${(trade.spot_at_entry ?? 0).toFixed(2)}</span>
          </span>
          <span class="pt-hdr-dot">·</span>
          <span class="pt-hdr-entry-item">
            <span class="pt-hdr-entry-label">Signal</span>
            <span class="pt-hdr-entry-val">${esc(trade.signal_rating ?? "—")}</span>
          </span>
          ${trade.iv_edge_vp != null ? `
          <span class="pt-hdr-dot">·</span>
          <span class="pt-hdr-entry-item" title="SVI surface edge at entry (positive = sold expensive IV)">
            <span class="pt-hdr-entry-label">IV Edge</span>
            <span class="pt-hdr-entry-val ${trade.iv_edge_vp > 1.5 ? "pass" : trade.iv_edge_vp < -1.5 ? "fail" : ""}">
              ${trade.iv_edge_vp > 0 ? "+" : ""}${trade.iv_edge_vp.toFixed(1)}vp
            </span>
          </span>` : ""}
        </div>

      </div>

      <div class="pt-card-body">
        <div class="pt-metrics-grid">
          <div class="pt-metric">
            <span class="pt-metric-label">Max Profit</span>
            <span class="pt-metric-value pass">$${maxProfit.toFixed(3)}</span>
          </div>
          <div class="pt-metric">
            <span class="pt-metric-label">${isDebit ? "Debit Paid" : "Max Loss"}</span>
            <span class="pt-metric-value na">${debitPaid != null ? "$" + debitPaid.toFixed(3) : "—"}</span>
          </div>
          <div class="pt-metric">
            <span class="pt-metric-label">Spread Value</span>
            <span class="pt-metric-value na">${markStr}</span>
          </div>
          <div class="pt-metric">
            <span class="pt-metric-label">Unrealized P&amp;L/sh</span>
            <span class="pt-metric-value ${unrCls}">${unrStr}</span>
          </div>
          <div class="pt-metric">
            <span class="pt-metric-label">% to Target</span>
            <span class="pt-metric-value ${pctCls}">${pctDone != null ? pctDone + "%" : "—"}</span>
          </div>
        </div>

        <div class="pt-card-meta-row">
          <span>Entered <strong>${(trade.entered_at ?? "").slice(0,10)}</strong></span>
          <span class="pt-meta-dot">·</span>
          <span>Expiry <strong>${esc(trade.expiry ?? "—")}</strong> ${dteLabel(trade.expiry)}</span>
          <span class="pt-meta-dot">·</span>
          <span>Signal <strong>${esc(trade.signal_rating ?? "—")}</strong></span>
          <span class="pt-meta-dot">·</span>
          <span>Spot at entry <strong>$${(trade.spot_at_entry ?? 0).toFixed(2)}</strong></span>
          ${trade.profit_target != null ? `<span class="pt-meta-dot">·</span><span class="muted">Target $${trade.profit_target.toFixed(3)}</span>` : ""}
        </div>

        ${progressBar ? `<div class="pt-card-progress">${progressBar}</div>` : ""}

        ${(() => {
          // Show latest iv_flag from snapshots if present
          const snaps = trade.snapshots ?? [];
          const lastFlag = [...snaps].reverse().find(s => s.iv_flag)?.iv_flag;
          if (!lastFlag) return "";
          const cls = lastFlag.includes("expensive") ? "warn" : "fail";
          return `<div class="pt-iv-flag pt-iv-flag-${cls}">⚠ IV Surface: ${esc(lastFlag)}</div>`;
        })()}

        ${buildMcBar(trade)}

        <div class="pt-drift-placeholder"></div>

        <div class="pt-tracking-placeholder lp-analysis-placeholder">
          <p class="lp-loading-text">Loading market analysis…</p>
        </div>

        <div class="pt-card-footer">
          <button class="pt-del-btn" data-id="${esc(trade.id)}" title="Remove this paper trade">✕ Remove</button>
        </div>
      </div>

    </div>`;
}

// ── Portfolio summary (below open trade cards) ────────────────────────────────

function renderPortfolioSummary(openTrades, marksMap) {
  const el = document.getElementById("pt-portfolio-summary");
  if (!el) return;
  if (!openTrades.length) { el.innerHTML = ""; return; }

  let totalInvested  = 0;
  let totalUnrlzd    = 0;
  let hasUnrlzd      = false;

  for (const t of openTrades) {
    // Capital at risk = max_loss per share × 100 shares/contract
    const risk = (t.max_loss ?? 0) * 100;
    totalInvested += risk;

    // Unrealized: prefer live data if available, else stored snapshot
    const liveUnr = marksMap && marksMap[t.id] ? marksMap[t.id].unrealized : null;
    const unr = liveUnr ?? t.latest_unrealized;
    if (unr != null) { totalUnrlzd += parseFloat(unr) * 100; hasUnrlzd = true; }
  }

  const unrCls = !hasUnrlzd ? "na" : totalUnrlzd >= 0 ? "pass" : "fail";
  const unrStr = hasUnrlzd ? fmt$(totalUnrlzd) : "—";

  el.innerHTML = `
    <div class="pt-portfolio-summary">
      <span class="pt-ps-label">Portfolio</span>
      <span class="pt-ps-item"><span class="muted">Open positions</span> <strong>${openTrades.length}</strong></span>
      <span class="pt-ps-sep">·</span>
      <span class="pt-ps-item"><span class="muted">Total at risk</span> <strong class="na">$${totalInvested.toFixed(0)}</strong></span>
      <span class="pt-ps-sep">·</span>
      <span class="pt-ps-item"><span class="muted">Unrealized P&L (total)</span> <strong class="${unrCls}">${unrStr}</strong></span>
    </div>`;
}

// ── Open trades ───────────────────────────────────────────────────────────────

let _openTrades  = [];
let _latestMarks = {};
let _analyzeMode = false;

let _openSortCol = null, _openSortDir = 1;

function _openSortVal(t, col) {
  const today = new Date().toISOString().slice(0, 10);
  switch (col) {
    case 0: return t.ticker ?? "";
    case 1: return t.structure ?? "";
    case 2: return t.entered_at ?? "";
    case 3: return t.expiry ?? "";
    case 4: return t.expiry ? Math.round((new Date(t.expiry) - new Date(today)) / 86400000) : 9999;
    case 5: { const s = t.strikes ?? {}; return s.short ?? s.put_short ?? 0; }
    case 6: return t.entry_credit ?? 0;
    case 7: return t.max_loss ?? 0;
    case 8: return t.latest_unrealized != null ? parseFloat(t.latest_unrealized) : -Infinity;
    case 9: return t.latest_unrealized != null ? parseFloat(t.latest_unrealized) * 100 : -Infinity;
    case 10: return t.signal_rating ?? "";
    default: return "";
  }
}

function renderOpenTradesTable(trades) {
  const el = document.getElementById("pt-open-table");
  if (!trades.length) {
    el.innerHTML = `<p class="muted na">No open paper trades. Run a morning scan to add today's top-3.</p>`;
    return;
  }

  const sorted = [...trades];
  if (_openSortCol !== null) {
    sorted.sort((a, b) => {
      const av = _openSortVal(a, _openSortCol), bv = _openSortVal(b, _openSortCol);
      return av < bv ? -_openSortDir : av > bv ? _openSortDir : 0;
    });
  }

  const rows = sorted.map(t => {
    const isDebit  = (t.structure ?? "").includes("Debit") || t.structure === "Long Strangle"
                  || t.structure === "Calendar Spread" || t.structure === "Diagonal Spread";
    const unr      = t.latest_unrealized;
    const unrTotal = unr != null ? parseFloat(unr) * 100 : null;
    const unrCls   = unr == null ? "na" : parseFloat(unr) >= 0 ? "pass" : "fail";
    const stk      = t.strikes ?? {};
    const strikesStr = formatStrikes(stk, t.structure, isDebit);

    return `
      <tr data-trade-id="${esc(t.id)}">
        <td><strong>${esc(t.ticker)}</strong></td>
        <td style="font-size:0.78rem;color:#aaa">${esc(t.structure)}</td>
        <td class="muted">${(t.entered_at ?? "").slice(0, 10) || "—"}</td>
        <td class="muted">${esc(t.expiry ?? "—")}</td>
        <td>${dteLabel(t.expiry)}</td>
        <td class="muted" style="font-size:0.8rem">${esc(strikesStr)}</td>
        <td class="spot-price muted" data-ticker="${esc(t.ticker)}">…</td>
        <td class="na">$${((isDebit ? t.max_profit : t.entry_credit) ?? t.entry_credit ?? 0).toFixed(3)}</td>
        <td class="muted" style="font-size:0.78rem">${isDebit ? "Debit" : "Max loss"}: $${(t.max_loss ?? 0).toFixed(3)}</td>
        <td class="pt-pnl-total ${unrCls}">${unrTotal != null ? fmt$(unrTotal) : "—"}</td>
        <td>
          <button class="pt-del-btn" data-id="${esc(t.id)}" title="Remove this paper trade">✕</button>
        </td>
      </tr>`;
  }).join("");

  const hdrs = ["Ticker","Structure","Entered","Expiry","DTE","Strikes","Price","Max Profit","Risk","P&amp;L $",""];
  const thRow = hdrs.map((h, i) => {
    if (i === hdrs.length - 1) return `<th></th>`;
    const isSorted = _openSortCol === i;
    const arrow = isSorted ? (_openSortDir === 1 ? " ▲" : " ▼") : "";
    return `<th class="sortable-th" data-col="${i}" style="cursor:pointer;user-select:none">${h}${arrow}</th>`;
  }).join("");

  el.innerHTML = `
    <div class="table-scroll">
      <table class="journal-table pt-trades-table">
        <thead><tr>${thRow}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  el.querySelectorAll("th.sortable-th").forEach(th => {
    th.addEventListener("click", () => {
      const col = parseInt(th.dataset.col);
      if (_openSortCol === col) _openSortDir *= -1;
      else { _openSortCol = col; _openSortDir = 1; }
      renderOpenTradesTable(_openTrades);
      renderPortfolioSummary(_openTrades, _latestMarks);
    });
  });

  // Row click → expand inline MC bar detail
  el.querySelectorAll("tbody tr[data-trade-id]").forEach(tr => {
    tr.style.cursor = "pointer";
    tr.addEventListener("click", e => {
      if (e.target.closest(".pt-del-btn")) return;
      const tradeId = tr.dataset.tradeId;
      const existing = tr.nextElementSibling;
      if (existing && existing.classList.contains("pt-row-detail")) {
        existing.remove();
        tr.classList.remove("pt-row-expanded");
        return;
      }
      const trade = (_openTrades || []).find(t => t.id === tradeId);
      if (!trade) return;
      const mcHtml = buildMcBar(trade);
      const detail = document.createElement("tr");
      detail.className = "pt-row-detail";
      detail.innerHTML = `<td colspan="11" style="padding:0 1rem 0.75rem;background:var(--bg-subtle)">
        ${mcHtml || '<span class="muted" style="font-size:.8rem">No MC distribution data for this trade.</span>'}
      </td>`;
      tr.after(detail);
      tr.classList.add("pt-row-expanded");
      renderMcChart(trade);
    });
  });

  // Fetch current prices for all unique tickers and fill price cells
  const uniqueTickers = [...new Set(sorted.map(t => t.ticker).filter(Boolean))];
  if (uniqueTickers.length) {
    fetch(`/api/quotes?tickers=${uniqueTickers.join(",")}`)
      .then(r => r.json())
      .then(prices => {
        el.querySelectorAll("td.spot-price[data-ticker]").forEach(cell => {
          const ticker = cell.dataset.ticker;
          const price  = prices[ticker];
          cell.textContent = price != null ? `$${price.toFixed(2)}` : "—";
        });
      })
      .catch(() => {
        el.querySelectorAll("td.spot-price").forEach(cell => cell.textContent = "—");
      });
  }
}

function renderOpenTradesCards(trades) {
  const el = document.getElementById("pt-open-table");
  if (!trades.length) {
    el.innerHTML = `<p class="muted na">No open paper trades. Run a morning scan to add today's top-3.</p>`;
    renderPortfolioSummary([], {});
    return;
  }
  el.innerHTML = trades.map(t => buildTradeCard(t, null)).join("");
  trades.forEach(t => renderMcChart(t));
  renderPortfolioSummary(trades, {});
}

function renderOpenTrades(trades) {
  _openTrades = trades;
  document.getElementById("pt-open-count").textContent = trades.length ? `(${trades.length})` : "";

  if (_analyzeMode) {
    renderOpenTradesCards(trades);
  } else {
    renderOpenTradesTable(trades);
    renderPortfolioSummary(trades, {});
  }
}

function _patchCardMetrics(cardEl, trade, live) {
  const isDebit   = (trade.structure ?? "").includes("Debit")
                 || ["Long Strangle","Calendar Spread","Diagonal Spread"].includes(trade.structure);
  const maxProfit = isDebit ? (trade.max_profit ?? 0) : (trade.entry_credit ?? 0);
  const mark      = live.mark       ?? trade.latest_mark;
  const unrealized= live.unrealized ?? trade.latest_unrealized;

  const pctDone = (mark != null && maxProfit > 0)
    ? (isDebit
        ? Math.round(((mark - (trade.entry_credit ?? 0)) / maxProfit) * 100)
        : Math.round((1 - mark / maxProfit) * 100))
    : null;

  const unrCls = unrealized == null ? "na" : parseFloat(unrealized) >= 0 ? "pass" : "fail";
  const unrStr = unrealized != null ? fmt$(unrealized, 3) : "—";
  const pctCls = pctDone == null ? "na" : pctDone >= 90 ? "pass" : pctDone >= 50 ? "na" : "muted";
  const markStr = mark != null ? "$" + mark.toFixed(3) : "—";

  // patch row1 stat values (P&L/sh, % target — no structural change)
  const statEls = cardEl.querySelectorAll(".pt-hdr-stat");
  statEls.forEach(el => {
    const lbl = el.querySelector(".pt-hdr-label")?.innerText?.trim();
    const val = el.querySelector(".pt-hdr-val");
    if (!val) return;
    if (lbl === "P&L/SH") {
      val.textContent = unrStr;
      val.className = `pt-hdr-val ${unrCls}`;
    } else if (lbl === "% TARGET") {
      val.textContent = pctDone != null ? pctDone + "%" : "—";
      val.className = `pt-hdr-val ${pctCls}`;
    }
  });

  // patch live dot
  const dot = cardEl.querySelector(".pt-live-dot");
  if (dot) {
    dot.className = live.error
      ? "pt-live-dot pt-live-dot-err"
      : live.mark != null ? "pt-live-dot pt-live-dot-ok" : "pt-live-dot";
    dot.title = live.error ? live.error : live.mark != null ? "Live quote" : "";
  }

  // patch body metrics (Spread Value, Unrealized P&L/sh, % to Target)
  const metricEls = cardEl.querySelectorAll(".pt-metric");
  metricEls.forEach(el => {
    const lbl = el.querySelector(".pt-metric-label")?.innerText?.trim();
    const val = el.querySelector(".pt-metric-value");
    if (!val) return;
    if (lbl === "Spread Value") {
      val.textContent = markStr;
    } else if (lbl === "Unrealized P&L/sh") {
      val.textContent = unrStr;
      val.className = `pt-metric-value ${unrCls}`;
    } else if (lbl === "% to Target") {
      val.textContent = pctDone != null ? pctDone + "%" : "—";
      val.className = `pt-metric-value ${pctCls}`;
    }
  });

  // patch progress bar in-place
  const progressSlot = cardEl.querySelector(".pt-card-progress");
  if (progressSlot && mark != null && trade.profit_target != null && trade.stop_loss != null) {
    progressSlot.innerHTML = buildProgressBar(maxProfit, mark, trade.profit_target, trade.stop_loss, isDebit);
  }
}

function applyLiveMarks(marksMap) {
  _latestMarks = marksMap;
  for (const trade of _openTrades) {
    const live = marksMap[trade.id];
    if (!live) continue;

    // Card view
    const cardEl = document.querySelector(`.pt-trade-card[data-id="${CSS.escape(trade.id)}"]`);
    if (cardEl) _patchCardMetrics(cardEl, trade, live);

    // Table view — patch P&L cells and price cell in the open-positions table
    const tableEl = document.getElementById("pt-open-table");
    if (tableEl) {
      // Price cell — keyed by ticker (multiple rows may share ticker)
      if (live.ul_price != null) {
        tableEl.querySelectorAll(`td.spot-price[data-ticker="${CSS.escape(trade.ticker)}"]`)
          .forEach(cell => { cell.textContent = `$${parseFloat(live.ul_price).toFixed(2)}`; cell.classList.remove("muted"); });
      }
      // P&L cells — keyed by trade id on the row
      const row = tableEl.querySelector(`tr[data-trade-id="${CSS.escape(trade.id)}"]`);
      if (row && live.unrealized != null) {
        const unr      = parseFloat(live.unrealized);
        const unrTotal = unr * 100;
        const cls      = unr >= 0 ? "pass" : "fail";
        const plTot = row.querySelector("td.pt-pnl-total");
        if (plTot) { plTot.textContent = fmt$(unrTotal); plTot.className = `pt-pnl-total ${cls}`; }
      }
    }
  }
  renderPortfolioSummary(_openTrades, marksMap);
  renderDayWiseLog(_allTrades, marksMap);
}

// ── Position health tracking (same /api/analyze rulebook signals Live Positions uses) ─

const _tickerAnalysisCache = {}; // ticker -> { data: row } | { error: true } | undefined (not yet fetched)

/**
 * Map a paper trade (+ latest live mark) into the `sp`-shaped object
 * lib/position-health.js expects (dte, expiry, structure, pnl_pct, etc.)
 */
function buildSpFromTrade(trade) {
  const live       = _latestMarks[trade.id] ?? {};
  const unrealized = live.unrealized ?? trade.latest_unrealized;
  const isDebit    = (trade.structure ?? "").includes("Debit")
                  || ["Long Strangle","Calendar Spread","Diagonal Spread"].includes(trade.structure);
  const maxProfit  = isDebit ? (trade.max_profit ?? 0) : (trade.entry_credit ?? 0);
  const debitPaid  = trade.entry_credit ?? null;
  const basis      = isDebit ? debitPaid : maxProfit;

  const pnl_pct = (unrealized != null && basis)
    ? (parseFloat(unrealized) / basis) * 100
    : null;

  const today = new Date().toISOString().slice(0, 10);
  const dte = trade.expiry ? Math.round((new Date(trade.expiry) - new Date(today)) / 86400000) : null;

  return {
    structure: trade.structure,
    dte,
    expiry: trade.expiry,
    pnl_pct,
    max_profit_ps: maxProfit,
    max_loss_ps: debitPaid,
  };
}

/**
 * Extract risk-defining short strikes from a Paper Trades trade record
 * (field names: trade.strikes.{put_short,call_short} for Iron Condor,
 * .short/.long for 2-strike spreads, .short alone for CSP/Covered Call).
 */
function getTradeShortStrikes(trade) {
  const s = trade.strikes || {};
  const strikes = [];
  if (s.put_short != null) strikes.push(s.put_short);
  if (s.call_short != null) strikes.push(s.call_short);
  if (!strikes.length && s.short != null) strikes.push(s.short);
  return strikes;
}

/**
 * Re-render the verdict badge + tracking card for one trade from whatever
 * is already in _tickerAnalysisCache (does not fetch). Folds in strike
 * proximity when the underlying price is already known (from a resolved
 * Greeks-drift fetch — Paper Trades has no other client-side source for
 * live spot price).
 */
async function applyTrackingToTrade(trade) {
  const cardEl = document.querySelector(`.pt-trade-card[data-id="${CSS.escape(trade.id)}"]`);
  if (!cardEl) return;

  const badge = cardEl.querySelector(".pu-verdict-badge");
  const priceSlot = cardEl.querySelector(".pt-price-badge-slot");
  const placeholder = cardEl.querySelector(".pt-tracking-placeholder");
  const cached = _tickerAnalysisCache[trade.ticker];
  if (!cached) return; // not fetched yet — leave the loading state in place

  if (cached.error) {
    if (badge) badge.outerHTML = `<span class="pu-verdict-badge na">N/A</span>`;
    if (placeholder) placeholder.innerHTML = `<p class="lp-error-text">⚠️ Error loading analysis</p>`;
    return;
  }

  if (priceSlot) {
    priceSlot.innerHTML = buildPriceBadge(cached.data);
  }

  const sp = buildSpFromTrade(trade);
  const drift = _driftCache[trade.id];
  const ulPrice = drift && !drift.error ? drift.current.ul_price : null;
  const proximity = ulPrice != null
    ? computeStrikeProximity(getTradeShortStrikes(trade), ulPrice)
    : null;

  try {
    // Score this trade server-side (single source of truth — see
    // scripts/decision_provider.py) using the analysis row already fetched
    // above plus this trade's own facts.
    const decision = await fetchDecision(cached.data, {
      structure: trade.structure,
      pnl_pct: sp.pnl_pct,
      dte: sp.dte,
      proximity: proximity ? {
        strike: proximity.strike,
        distance_pct: proximity.distancePct,
        risk_level: proximity.riskLevel,
      } : null,
    });

    if (badge) badge.outerHTML = buildVerdictBadge(decision) || `<span class="pu-verdict-badge na">N/A</span>`;
    if (placeholder) {
      const trackingHtml = buildPositionTrackingFeedback(decision);
      const feedbackHtml = buildPositionFeedback(sp, cached.data);
      const marketSignalsHtml = buildPositionMarketSignals(cached.data);
      placeholder.innerHTML = (trackingHtml || feedbackHtml || marketSignalsHtml)
        ? `<div class="lp-analysis-panels">${trackingHtml}${feedbackHtml}${marketSignalsHtml}</div>`
        : `<p class="muted">Not enough signal data to assess.</p>`;
    }
  } catch (e) {
    console.warn(`[Decision] Unavailable for ${trade.ticker}:`, e.message);
    if (badge) badge.outerHTML = `<span class="pu-verdict-badge na">N/A</span>`;
    if (placeholder) placeholder.innerHTML = `<p class="lp-error-text">⚠️ Error loading decision</p>`;
  }
}

// ── Greeks drift since entry (generalized from Live Positions) ───────────────

const _driftCache = {}; // trade.id -> {entry, current, drift} | {error: true}

/**
 * Re-render the drift card for one trade from whatever is already in
 * _driftCache (does not fetch). Also refreshes the verdict/tracking card,
 * since this is the point ul_price (needed for strike proximity) becomes
 * available for Paper Trades.
 */
function applyDriftToTrade(trade) {
  const cardEl = document.querySelector(`.pt-trade-card[data-id="${CSS.escape(trade.id)}"]`);
  if (!cardEl) return;
  const slot = cardEl.querySelector(".pt-drift-placeholder");

  const cached = _driftCache[trade.id];
  if (!cached || cached.error) return; // not fetched yet, or unavailable — leave empty

  if (slot) slot.innerHTML = buildGreeksDriftCard(cached);
  applyTrackingToTrade(trade); // re-score the verdict now that ul_price (proximity) is known
}

/**
 * Fetch Greeks drift for each open trade (one request per trade, since
 * drift is keyed by the trade's own stable id, not shared across trades
 * the way ticker analysis is).
 */
function setupGreeksDriftForTrades(trades) {
  trades.forEach(trade => {
    if (_driftCache[trade.id]) {
      applyDriftToTrade(trade); // already fetched on a prior render pass
      return;
    }
    fetchGreeksDriftForTrade(trade.id)
      .then(result => {
        _driftCache[trade.id] = result;
        applyDriftToTrade(trade);
      })
      .catch(e => {
        console.warn(`[Greeks Drift] Unavailable for trade ${trade.id}:`, e.message);
        _driftCache[trade.id] = { error: true };
      });
  });
}

/**
 * Fetch /api/analyze for each unique ticker among the open trades (dedup
 * across trades that share a ticker), then apply the result to every
 * matching card.
 */
function setupTradeAnalysis(trades) {
  const uniqueTickers = [...new Set(trades.map(t => t.ticker))];

  uniqueTickers.forEach(ticker => {
    const tickerTrades = trades.filter(t => t.ticker === ticker);

    fetchTickerAnalysis(ticker)
      .then(analysis => {
        _tickerAnalysisCache[ticker] = { data: analysis };
        tickerTrades.forEach(applyTrackingToTrade);
      })
      .catch(e => {
        console.error(`[PaperTrades] Failed to fetch analysis for ${ticker}:`, e);
        _tickerAnalysisCache[ticker] = { error: true };
        tickerTrades.forEach(applyTrackingToTrade);
      });
  });
}

// ── Closed trades table ───────────────────────────────────────────────────────

function renderClosedTrades(trades) {
  const el = document.getElementById("pt-closed-table");
  document.getElementById("pt-closed-count").textContent = trades.length ? `(${trades.length})` : "";

  if (!trades.length) {
    el.innerHTML = `<p class="muted na">No closed trades yet.</p>`;
    return;
  }

  const rows = [...trades].reverse().map(t => {
    const x   = t.exit ?? {};
    const win = x.win;
    return `
      <tr class="${win ? "pt-row-win" : "pt-row-loss"}">
        <td>${x.ts ? x.ts.slice(0,10) : "—"}</td>
        <td><strong>${esc(t.ticker)}</strong></td>
        <td class="muted" style="font-size:0.78rem">${esc(t.structure)}</td>
        <td>${esc(t.expiry ?? "—")}</td>
        <td class="na">$${(t.entry_credit ?? 0).toFixed(3)}</td>
        <td>${statusLabel(t.status)}</td>
        <td class="muted" style="font-size:0.78rem">${esc(reasonLabel(x.reason))}</td>
        <td class="${win ? "pass" : "fail"}">${fmt$(x.pnl_per_share, 3)}</td>
        <td class="${win ? "pass" : "fail"}">${fmt$(x.pnl_total)}</td>
        <td class="${win ? "pass" : "fail"}">${fmtPct(x.pnl_pct_of_max)}</td>
        <td class="muted" style="font-size:0.74rem">${esc(t.signal_rating ?? "—")}</td>
        <td>
          <button class="pt-del-btn" data-id="${esc(t.id)}" title="Remove this trade">✕</button>
        </td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <div class="table-scroll">
      <table class="journal-table pt-trades-table">
        <thead><tr>
          <th>Closed</th><th>Ticker</th><th>Structure</th><th>Expiry</th>
          <th>Max Profit</th><th>Status</th><th>Exit Reason</th>
          <th>P&L/sh</th><th>P&L $</th><th>% of Max</th><th>Signal</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ── Day-wise log tab ──────────────────────────────────────────────────────────

function renderDayWiseLog(allTrades, marksMap) {
  const el = document.getElementById("pt-daywise-table");
  if (!el) return;
  const marks = marksMap || _latestMarks || {};

  if (!allTrades.length) {
    el.innerHTML = `<p class="muted na">No trades recorded yet.</p>`;
    return;
  }

  // Group by entered_at date (YYYY-MM-DD)
  const byDate = {};
  for (const t of allTrades) {
    const day = (t.entered_at ?? "").slice(0, 10) || "Unknown";
    if (!byDate[day]) byDate[day] = [];
    byDate[day].push(t);
  }

  // ── Overall banner ────────────────────────────────────────────────────────
  const allClosed  = allTrades.filter(t => t.status !== "open");
  const allOpen    = allTrades.filter(t => t.status === "open");
  const totalWins  = allClosed.filter(t => t.exit?.win).length;
  const totalLosses= allClosed.length - totalWins;
  const closedTotal= allClosed.reduce((s, t) => s + (t.exit?.pnl_total ?? 0), 0);
  const liveTotal  = allOpen.reduce((s, t) => {
    const u = marks[t.id]?.unrealized ?? t.latest_unrealized;
    return s + (u != null ? parseFloat(u) * 100 : 0);
  }, 0);
  const overallPnl = closedTotal + liveTotal;
  const oCls       = overallPnl >= 0 ? "pass" : "fail";
  const winRate    = allClosed.length ? Math.round(totalWins / allClosed.length * 100) : null;

  const overallBanner = `
    <div class="pt-overall-banner">
      <div class="pt-ob-item">
        <span class="pt-ob-label">Overall P&amp;L</span>
        <span class="pt-ob-value ${oCls}">${fmt$(overallPnl)}</span>
        <span class="pt-ob-sub muted">closed ${fmt$(closedTotal)} · unrealized ${fmt$(liveTotal)}</span>
      </div>
      <div class="pt-ob-sep"></div>
      <div class="pt-ob-item">
        <span class="pt-ob-label">Closed Trades</span>
        <span class="pt-ob-value na">${allClosed.length}</span>
        <span class="pt-ob-sub muted">${totalWins}W / ${totalLosses}L${winRate != null ? " · " + winRate + "% win rate" : ""}</span>
      </div>
      <div class="pt-ob-sep"></div>
      <div class="pt-ob-item">
        <span class="pt-ob-label">Open Positions</span>
        <span class="pt-ob-value na">${allOpen.length}</span>
        <span class="pt-ob-sub muted">unrealized ${fmt$(liveTotal)}</span>
      </div>
    </div>`;

  // Render newest date first, with a running cumulative total
  const sortedDates = Object.keys(byDate).sort(); // oldest first for cumulative calc
  // Compute per-day P&L in chronological order for running total
  const dayPnls = sortedDates.map(day => {
    const trades  = byDate[day];
    const closed  = trades.filter(t => t.status !== "open");
    const openTs  = trades.filter(t => t.status === "open");
    const cPnl    = closed.reduce((s, t) => s + (t.exit?.pnl_total ?? 0), 0);
    const lPnl    = openTs.reduce((s, t) => {
      const u = marks[t.id]?.unrealized ?? t.latest_unrealized;
      return s + (u != null ? parseFloat(u) * 100 : 0);
    }, 0);
    return cPnl + lPnl;
  });
  // Build running totals (oldest→newest), then reverse to show newest first
  const running = [];
  let cum = 0;
  for (const p of dayPnls) { cum += p; running.push(cum); }
  // Zip and reverse
  const daysWithRunning = sortedDates.map((d, i) => ({ day: d, dayPnl: dayPnls[i], running: running[i] }))
                                     .reverse();

  const html = daysWithRunning.map(({ day, dayPnl, running: runTotal }) => {
    const trades = byDate[day];

    const closed   = trades.filter(t => t.status !== "open");
    const openTs   = trades.filter(t => t.status === "open");
    const wins     = closed.filter(t => t.exit?.win).length;
    const allClosed_day = closed.length === 0;
    const pnlCls   = dayPnl >= 0 ? "pass" : "fail";
    const runCls   = runTotal >= 0 ? "pass" : "fail";

    const dayHeader = `
      <div class="pt-day-header">
        <span class="pt-day-date">${day}</span>
        <span class="muted" style="font-size:0.8rem">${trades.length} trade${trades.length !== 1 ? "s" : ""}</span>
        ${closed.length ? `<span class="muted" style="font-size:0.8rem">${wins}W/${closed.length - wins}L</span>` : ""}
        ${openTs.length ? `<span class="pt-status-badge pt-status-na" style="font-size:0.7rem">${openTs.length} open</span>` : ""}
        <span class="pt-day-pnl-group">
          <span class="pt-day-pnl-label muted">Day:</span>
          <span class="${pnlCls}" style="font-size:0.82rem;font-weight:600" title="${allClosed_day ? "Live unrealized (1 contract each)" : "Closed P&L + live unrealized"}">${fmt$(dayPnl)}</span>
          <span class="pt-day-pnl-sep muted">·</span>
          <span class="pt-day-pnl-label muted">Running:</span>
          <span class="${runCls}" style="font-size:0.82rem;font-weight:700">${fmt$(runTotal)}</span>
        </span>
      </div>`;

    // Group trades within this day by (structure + scan_time) to detect same-scan batches
    const _scanGroups = {};
    for (const t of trades) {
      const key = `${t.structure}|${t.scan_time ?? "morning"}`;
      (_scanGroups[key] = _scanGroups[key] ?? []).push(t);
    }

    const _renderedIds = new Set();
    const rows = trades.flatMap(t => {
      if (_renderedIds.has(t.id)) return [];

      const scanKey = `${t.structure}|${t.scan_time ?? "morning"}`;
      const group   = _scanGroups[scanKey];

      // Calendar Spreads from the same scan on the same day → one combined row
      if (t.structure === "Calendar Spread" && group.length > 1) {
        group.forEach(g => _renderedIds.add(g.id));
        const scanLabel  = (t.scan_time === "afternoon") ? "PM" : "AM";
        const tickers    = group.map(g => g.ticker).join(", ");
        const allOpen    = group.every(g => g.status === "open");
        const anyOpen    = group.some(g => g.status === "open");
        const wins       = group.filter(g => !g.exit?.win === false && g.exit?.win).length;
        const rowCls     = allOpen ? "" : (wins === group.length ? "pt-row-win" : wins === 0 ? "pt-row-loss" : "");
        const totalDebit = group.reduce((s, g) => s + (g.max_loss ?? 0), 0);
        const totalPnl   = group.reduce((s, g) => {
          const lv = marks[g.id] ?? {};
          const u  = lv.unrealized ?? g.latest_unrealized;
          return s + (u != null ? parseFloat(u) * 100 : (g.exit?.pnl_total ?? 0));
        }, 0);
        const pnlCls = totalPnl >= 0 ? "pass" : "fail";
        const statusTxt = allOpen ? statusLabel("open")
          : anyOpen ? `<span class="pt-status-badge pt-status-na">Mixed</span>`
          : `<span class="pt-status-badge ${wins === group.length ? "pt-status-pass" : "pt-status-fail"}">${wins}W/${group.length - wins}L</span>`;
        return [`
          <tr class="${rowCls}">
            <td><strong>${esc(tickers)}</strong> <span class="muted" style="font-size:0.72rem">[${scanLabel}]</span></td>
            <td style="font-size:0.78rem;color:#aaa">Calendar Spread <span class="muted">(Call Debit)</span></td>
            <td class="muted">${esc(t.expiry ?? "—")}</td>
            <td class="na">—</td>
            <td class="muted" style="font-size:0.78rem">Debit paid: $${totalDebit.toFixed(3)} total</td>
            <td class="muted">—</td>
            <td>${statusTxt}</td>
            <td class="muted" style="font-size:0.75rem">—</td>
            <td class="${pnlCls}">${fmt$(totalPnl)}</td>
          </tr>`];
      }

      _renderedIds.add(t.id);
      const x       = t.exit ?? {};
      const isOpen  = t.status === "open";
      const isDebit = (t.structure ?? "").includes("Debit") || t.structure === "Calendar Spread"
                  || t.structure === "Long Strangle" || t.structure === "Diagonal Spread";
      const win     = x.win;
      const rowCls  = isOpen ? "" : (win ? "pt-row-win" : "pt-row-loss");

      const live    = marks[t.id] ?? {};
      const unr     = live.unrealized ?? t.latest_unrealized;
      const unrTotal= unr != null ? parseFloat(unr) * 100 : null;
      const unrCls  = unr == null ? "na" : parseFloat(unr) >= 0 ? "pass" : "fail";

      const pnlPs   = isOpen ? unr              : x.pnl_per_share;
      const pnlTot  = isOpen ? unrTotal         : x.pnl_total;
      const pnlCls  = isOpen ? unrCls           : (win ? "pass" : "fail");
      const exitTxt = isOpen ? "—"              : esc(reasonLabel(x.reason));
      const liveTip = isOpen && live.mark != null ? ` title="mark $${live.mark.toFixed(3)}"` : "";
      const structLabel = t.structure === "Calendar Spread"
        ? `Calendar Spread <span class="muted">(Call Debit)</span>`
        : esc(t.structure);

      const stk = t.strikes ?? {};
      const strikesStr = formatStrikes(stk, t.structure, isDebit);

      return [`
        <tr class="${rowCls}">
          <td><strong>${esc(t.ticker)}</strong></td>
          <td style="font-size:0.78rem;color:#aaa">${structLabel}</td>
          <td class="muted">${esc(t.expiry ?? "—")}</td>
          <td>${dteLabel(t.expiry)}</td>
          <td class="muted" style="font-size:0.8rem">${esc(strikesStr)}</td>
          <td class="spot-price muted" data-ticker="${esc(t.ticker)}">…</td>
          <td class="na">$${((isDebit ? t.max_profit : t.entry_credit) ?? t.entry_credit ?? 0).toFixed(3)}</td>
          <td class="muted" style="font-size:0.78rem">${isDebit ? "Debit" : "Max loss"}: $${(t.max_loss ?? 0).toFixed(3)}</td>
          <td>${statusLabel(t.status)}</td>
          <td class="muted" style="font-size:0.75rem">${exitTxt}</td>
          <td class="${pnlCls}">${pnlTot != null ? fmt$(pnlTot) : "—"}</td>
        </tr>`];
    }).join("");

    return `
      ${dayHeader}
      <div class="table-scroll" style="margin-bottom:1.4rem">
        <table class="journal-table pt-trades-table">
          <thead><tr>
            <th>Ticker</th><th>Structure</th><th>Expiry</th><th>DTE</th>
            <th>Strikes</th><th>Price</th><th>Max Profit</th><th>Risk</th>
            <th>Status</th><th>Exit</th>
            <th>P&amp;L $</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }).join("");

  el.innerHTML = overallBanner + html;

  // Fetch current prices and fill spot-price cells in day-wise log
  const dwTickers = [...new Set(
    [...el.querySelectorAll("td.spot-price[data-ticker]")].map(c => c.dataset.ticker).filter(Boolean)
  )];
  if (dwTickers.length) {
    fetch(`/api/quotes?tickers=${dwTickers.join(",")}`)
      .then(r => r.json())
      .then(prices => {
        el.querySelectorAll("td.spot-price[data-ticker]").forEach(cell => {
          const p = prices[cell.dataset.ticker];
          if (p != null) { cell.textContent = `$${p.toFixed(2)}`; cell.classList.remove("muted"); }
          else cell.textContent = "—";
        });
      })
      .catch(() => {
        el.querySelectorAll("td.spot-price[data-ticker]").forEach(cell => { cell.textContent = "—"; });
      });
  }
}

// ── Range Analysis tab ────────────────────────────────────────────────────────

let _rangeLoaded = false;

function _raTheme() {
  const dark = document.documentElement.dataset.theme === "dark"
            || (!document.documentElement.dataset.theme
                && window.matchMedia("(prefers-color-scheme: dark)").matches);
  return {
    bg:      dark ? "#1e1e2e" : "#ffffff",
    grid:    dark ? "#333355" : "#e0e0e0",
    font:    dark ? "#ccccdd" : "#333344",
  };
}

function _raLayout(title, extra = {}) {
  const t = _raTheme();
  return {
    title:         { text: title, font: { color: t.font, size: 13 } },
    paper_bgcolor: t.bg, plot_bgcolor: t.bg,
    font:          { color: t.font, size: 11 },
    margin:        { t: 40, b: 90, l: 55, r: 20 },
    legend:        { orientation: "h", y: -0.28 },
    xaxis:         { gridcolor: t.grid, tickfont: { size: 10 } },
    yaxis:         { gridcolor: t.grid },
    ...extra,
  };
}

function _chartDiv(id, charts) {
  const div = document.createElement("div");
  div.id    = id;
  div.style.cssText = "width:520px;max-width:100%;height:340px;flex:1 1 460px";
  charts.appendChild(div);
  return div;
}

function _fmtCal(v) {
  if (v == null) return "—";
  const cls = v > 5 ? "pass" : v < -5 ? "fail" : "na";
  return `<span class="${cls}">${v > 0 ? "+" : ""}${v}pp</span>`;
}

async function loadRangeAnalysis() {
  if (_rangeLoaded) return;
  _rangeLoaded = true;

  const overview = document.getElementById("pt-range-overview");
  const charts   = document.getElementById("pt-range-charts");
  const detail   = document.getElementById("pt-range-detail");
  overview.innerHTML = `<span class="muted">Loading…</span>`;

  let data;
  try {
    const res = await fetch("/api/paper-trades/range-analysis");
    data = await res.json();
    if (!data.ok) throw new Error(data.error || "API error");
  } catch(e) {
    overview.innerHTML = `<p class="fail">Failed to load: ${esc(String(e))}</p>`;
    return;
  }

  const ov  = data.overall;
  const ss  = data.structure_summaries   || {};
  const sc  = data.structure_calibration || {};
  const ovc = data.overall_calibration   || [];
  const dtc = data.dte_calibration       || [];
  const recs = data.records              || [];

  // ── Per-structure headline stats (computed from records) ────────────────────
  // Bucket trades by structure; compute mean predicted POP and realized win rate.
  const _structStats = {};
  for (const r of recs) {
    const s = r.structure;
    if (!_structStats[s]) _structStats[s] = { n: 0, sumPred: 0, wins: 0 };
    _structStats[s].n++;
    // delta_implied_pop is already a percentage (0-100)
    _structStats[s].sumPred += (r.delta_implied_pop ?? 0);
    const z = r.zone || "";
    // Win = any positive outcome across all structure types
    if (z === "full_win" || z === "partial_win" || z === "contained" || z === "expired_otm")
      _structStats[s].wins++;
  }

  const STRUCT_ORDER = [
    "Iron Condor", "Call Debit Spread", "Put Debit Spread",
    "Long Strangle", "Covered Call", "Cash Secured Put",
    "Call Credit Spread", "Put Credit Spread",
  ];

  const headlineRows = STRUCT_ORDER
    .filter(s => _structStats[s])
    .map(struct => {
      const st = _structStats[struct];
      const smallSample = st.n < 10;
      const pred = st.n ? +(st.sumPred / st.n).toFixed(1) : null;
      const real = st.n ? +(st.wins / st.n * 100).toFixed(1) : null;
      const err  = (pred != null && real != null) ? +(real - pred).toFixed(1) : null;
      const errStr  = err == null ? "—" : (err > 0 ? "+" : "") + err + "pp";
      const errCls  = err == null ? "muted"
                    : err >  5 ? "pass" : err < -5 ? "fail" : "muted";
      // Selection quality note: if predicted < 50% the strategy is deliberately
      // choosing sub-50% setups — not necessarily a calibration problem.
      const selNote = pred != null && pred < 50
        ? `<span class="muted" style="font-size:0.72rem">low-prob selection</span>`
        : pred != null && pred > 75
        ? `<span class="muted" style="font-size:0.72rem">high-prob selection</span>`
        : "";
      const nDisplay = smallSample ? `${st.n}*` : `${st.n}`;
      return `<tr>
        <td><strong>${esc(struct)}</strong></td>
        <td class="num">${pred != null ? pred + "%" : "—"}</td>
        <td class="num">${real != null ? real + "%" : "—"}</td>
        <td class="num ${errCls}" style="font-weight:600">${errStr}</td>
        <td class="num muted">${nDisplay} &nbsp;${selNote}</td>
      </tr>`;
    }).join("");

  // ── Overview cards ──────────────────────────────────────────────────────────
  const calErr = ov.overall_calibration_error;
  const calCls = calErr == null ? "na" : calErr > 5 ? "pass" : calErr < -5 ? "fail" : "na";
  overview.innerHTML = `
    <h3 style="margin:0 0 0.6rem">Calibration at a Glance</h3>
    <div class="table-scroll" style="margin-bottom:1.5rem">
      <table class="journal-table" style="font-size:0.82rem;min-width:480px">
        <thead><tr>
          <th>Structure</th>
          <th class="num">Predicted</th>
          <th class="num">Realized</th>
          <th class="num">Error</th>
          <th class="num">n</th>
        </tr></thead>
        <tbody>${headlineRows}</tbody>
      </table>
      <p class="muted" style="font-size:0.72rem;margin-top:0.35rem">
        Error = realized − predicted. Green = model conservative. Red = model optimistic.
        * n &lt; 10 — treat as directional only. &nbsp;|&nbsp; "low-prob selection" = strategy deliberately chooses &lt;50% setups; calibration may be fine but strategy intent warrants review.
      </p>
    </div>
    <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1.25rem">
      <div class="pt-card" style="min-width:140px">
        <div class="pt-card-label">Trades Analysed</div>
        <div class="pt-card-val">${ov.total_trades}</div>
      </div>
      <div class="pt-card" style="min-width:160px">
        <div class="pt-card-label">Avg Δ-Implied POP</div>
        <div class="pt-card-val na">${ov.avg_delta_implied_pop ?? "—"}%</div>
      </div>
      <div class="pt-card" style="min-width:160px">
        <div class="pt-card-label">Realized Win Rate</div>
        <div class="pt-card-val ${ov.correct_pct >= 50 ? 'pass' : 'fail'}">${ov.correct_pct}%</div>
      </div>
      <div class="pt-card" style="min-width:200px">
        <div class="pt-card-label">Overall Calibration Error</div>
        <div class="pt-card-val ${calCls}">${calErr != null ? (calErr > 0 ? "+" : "") + calErr + "pp" : "—"}</div>
        <div class="pt-card-sub muted" style="font-size:0.73rem">realized − predicted &nbsp;·&nbsp; positive = conservative model</div>
      </div>
    </div>
    <p class="hint" style="margin-bottom:0.5rem">
      <strong>Delta ≠ probability.</strong> Δ-Implied POP is a Black-Scholes heuristic derived from stored leg IV —
      not a risk-neutral probability. Calibration error shows how far this heuristic's prediction diverges from what actually happened.
      Gold standard is ML pop_score (stored for new trades only).
    </p>`;

  // ── Chart 1: Calibration chart — predicted vs realized per POP bucket ───────
  charts.innerHTML = "";

  if (ovc.length) {
    const c1 = _chartDiv("ra-cal-overall", charts);
    const buckets  = ovc.map(r => r.bucket);
    const pred     = ovc.map(r => r.mean_predicted);
    const realized = ovc.map(r => r.realized_pct);
    const ns       = ovc.map(r => `n=${r.n}`);
    Plotly.newPlot(c1, [
      { name: "Δ-Implied POP (predicted)", x: buckets, y: pred,
        type: "bar", marker: { color: "#6366f1" }, text: ns, textposition: "outside" },
      { name: "Realized win rate",         x: buckets, y: realized,
        type: "bar", marker: { color: "#22c55e" } },
    ], _raLayout("POP Calibration: Predicted vs Realized (All Structures)", {
      barmode: "group",
      yaxis: { gridcolor: _raTheme().grid, range: [0, 110], ticksuffix: "%" },
    }), { responsive: true, displayModeBar: false });
  }

  // ── Chart 2: Calibration error bar (shows +/- deviation per bucket) ──────────
  if (ovc.length) {
    const c2 = _chartDiv("ra-cal-error", charts);
    const buckets = ovc.map(r => r.bucket);
    const errors  = ovc.map(r => r.calibration_error);
    const colors  = errors.map(e => e == null ? "#888" : e > 5 ? "#22c55e" : e < -5 ? "#ef4444" : "#94a3b8");
    Plotly.newPlot(c2, [
      { name: "Calibration error (realized − predicted)",
        x: buckets, y: errors, type: "bar",
        marker: { color: colors },
        text: errors.map(e => e == null ? "" : (e > 0 ? "+" : "") + e + "pp"),
        textposition: "outside" },
    ], _raLayout("Calibration Error by POP Bucket", {
      barmode: "relative",
      yaxis: { gridcolor: _raTheme().grid, ticksuffix: "pp",
               title: { text: "Error (pp)", font: { size: 11 } } },
      shapes: [{ type: "line", x0: -0.5, x1: buckets.length - 0.5, y0: 0, y1: 0,
                 line: { color: "#888", width: 1, dash: "dot" } }],
    }), { responsive: true, displayModeBar: false });
  }

  // ── Chart 3: Outcome zone breakdown per structure (stacked %) ────────────────
  const structNames = [], fullWin = [], partial = [], lossArr = [];
  for (const [struct, s] of Object.entries(ss)) {
    structNames.push(struct);
    const zones = s.zones || {};
    const fw = (zones.full_win    || zones.contained   || zones.expired_otm || {}).pct ?? 0;
    const pt = (zones.partial_win || {}).pct ?? 0;
    const ls = (zones.loss        || zones.loss_range  ||
                zones.breached_put_side || zones.breached_call_side || {}).pct ?? 0;
    // Sum all breach/loss zones
    const allLoss = Object.entries(zones)
      .filter(([k]) => k.startsWith("loss") || k.startsWith("breach") || k === "assigned")
      .reduce((a, [, v]) => a + (v.pct || 0), 0);
    fullWin.push(fw);
    partial.push(pt);
    lossArr.push(allLoss);
  }
  const c3 = _chartDiv("ra-chart-zones", charts);
  Plotly.newPlot(c3, [
    { name: "Win / Contained",  x: structNames, y: fullWin,  type: "bar", marker: { color: "#22c55e" } },
    { name: "Partial Win",      x: structNames, y: partial,  type: "bar", marker: { color: "#f59e0b" } },
    { name: "Loss / Breach",    x: structNames, y: lossArr,  type: "bar", marker: { color: "#ef4444" } },
  ], _raLayout("Outcome Zone Breakdown by Structure (%)", {
    barmode: "stack",
    yaxis: { gridcolor: _raTheme().grid, range: [0, 100], ticksuffix: "%" },
  }), { responsive: true, displayModeBar: false });

  // ── Chart 4: Debit spread % of max profit captured (histogram) ──────────────
  const cdsRecs = recs.filter(r => r.structure === "Call Debit Spread");
  const pdsRecs = recs.filter(r => r.structure === "Put Debit Spread");
  const cdsPcts = cdsRecs.map(r => r.pnl_pct_of_max).filter(v => v != null);
  const pdsPcts = pdsRecs.map(r => r.pnl_pct_of_max).filter(v => v != null);
  if (cdsPcts.length || pdsPcts.length) {
    const c4 = _chartDiv("ra-chart-pct-max", charts);
    Plotly.newPlot(c4, [
      { name: "Call Debit", x: cdsPcts, type: "histogram", opacity: 0.75,
        xbins: { start: -200, end: 120, size: 20 }, marker: { color: "#3b82f6" } },
      { name: "Put Debit",  x: pdsPcts, type: "histogram", opacity: 0.75,
        xbins: { start: -200, end: 120, size: 20 }, marker: { color: "#a855f7" } },
    ], _raLayout("% of Max Profit Captured — Debit Spreads", {
      barmode: "overlay",
      xaxis: { gridcolor: _raTheme().grid, title: { text: "% of Max" } },
      yaxis: { gridcolor: _raTheme().grid, title: { text: "# Trades" } },
      shapes: [{ type: "line", x0: 0, x1: 0, y0: 0, y1: 1, yref: "paper",
                 line: { color: "#f59e0b", width: 1.5, dash: "dash" } }],
    }), { responsive: true, displayModeBar: false });
  }

  // ── Chart 5: DTE calibration ─────────────────────────────────────────────────
  if (dtc.length > 1) {
    const c5 = _chartDiv("ra-cal-dte", charts);
    const dteBuckets = dtc.map(r => r.bucket);
    Plotly.newPlot(c5, [
      { name: "Δ-Implied POP", x: dteBuckets, y: dtc.map(r => r.mean_predicted),
        type: "bar", marker: { color: "#6366f1" },
        text: dtc.map(r => `n=${r.n}`), textposition: "outside" },
      { name: "Realized",      x: dteBuckets, y: dtc.map(r => r.realized_pct),
        type: "bar", marker: { color: "#22c55e" } },
    ], _raLayout("Calibration by DTE Bucket", {
      barmode: "group",
      yaxis: { gridcolor: _raTheme().grid, range: [0, 110], ticksuffix: "%" },
    }), { responsive: true, displayModeBar: false });
  }

  // ── Chart 6: IC range scatter (price vs box) if enough trades ────────────────
  const icRecs = recs.filter(r => r.structure === "Iron Condor");
  if (icRecs.length) {
    const c6 = _chartDiv("ra-chart-ic", charts);
    const labels = icRecs.map((r, i) => `${r.ticker} #${i+1}`);
    Plotly.newPlot(c6, [
      { name: "Put short (lower bound)", x: labels, y: icRecs.map(r => r.lower_bound),
        mode: "markers", marker: { color: "#ef4444", size: 9, symbol: "triangle-up" } },
      { name: "Price at expiry", x: labels, y: icRecs.map(r => r.actual_expiry_price),
        mode: "markers", marker: { color: "#22c55e", size: 11, symbol: "circle" } },
      { name: "Call short (upper bound)", x: labels, y: icRecs.map(r => r.upper_bound),
        mode: "markers", marker: { color: "#ef4444", size: 9, symbol: "triangle-down" } },
    ], _raLayout(`IC: Price at Expiry vs Short-Strike Range (n=${icRecs.length})`, {
      yaxis: { gridcolor: _raTheme().grid, title: { text: "Price ($)" } },
    }), { responsive: true, displayModeBar: false });
  }

  // ── Structure summary table ───────────────────────────────────────────────────
  const PRED_DESC = {
    "Iron Condor":        "Price stays between short_put ↔ short_call (containment)",
    "Call Debit Spread":  "Price crosses breakeven = long_strike + debit (threshold)",
    "Put Debit Spread":   "Price crosses breakeven = long_strike − debit (threshold)",
    "Long Strangle":      "Price exceeds either breakeven (big-move)",
    "Call Credit Spread": "Short call expires OTM (credit stays)",
    "Put Credit Spread":  "Short put expires OTM (credit stays)",
    "Covered Call":       "Short call expires OTM — stock not called away",
    "Cash Secured Put":   "Short put expires OTM — stock not put to you",
  };

  const structRows = Object.entries(ss).map(([struct, s]) => {
    const calRows = sc[struct] || [];
    const calSummary = calRows.map(r => {
      const tiny = r.n < 5;
      const rowStyle = tiny ? "opacity:0.55" : "";
      const errHtml  = tiny
        ? `<span class="muted">~${r.calibration_error != null ? (r.calibration_error > 0 ? "+" : "") + r.calibration_error + "pp" : "—"}</span>`
        : _fmtCal(r.calibration_error);
      const nBadge = `<span class="muted" style="font-size:0.68rem;margin-left:3px">(n=${r.n}${tiny ? " ⚠" : ""})</span>`;
      return `<span style="${rowStyle};font-size:0.72rem">` +
        `<span class="muted">${r.bucket}: ${r.mean_predicted}% → ${r.realized_pct}% </span>` +
        errHtml + nBadge +
        `</span>`;
    }).join("<br>");

    const zonesHtml = Object.entries(s.zones || {})
      .map(([z, v]) => {
        const cls = z.startsWith("loss") || z.startsWith("breach") || z === "assigned" ? "fail"
                  : z.includes("win") || z === "contained" || z === "expired_otm"       ? "pass"
                  : "na";
        return `<span class="${cls}">${z.replace(/_/g," ")}: ${v.pct}%</span>`;
      }).join(" &nbsp;|&nbsp; ");

    return `<tr>
      <td><strong>${esc(struct)}</strong></td>
      <td class="muted" style="font-size:0.75rem">${PRED_DESC[struct] || "—"}</td>
      <td>${s.n}</td>
      <td>${s.avg_delta_implied_pop ?? "—"}%</td>
      <td>${zonesHtml}</td>
      <td style="font-size:0.78rem">${calSummary || "—"}</td>
    </tr>`;
  }).join("");

  detail.innerHTML = `
    <h3 style="margin-bottom:0.75rem">Structure-by-Structure Prediction Calibration</h3>
    <div class="table-scroll">
      <table class="journal-table pt-trades-table">
        <thead><tr>
          <th>Structure</th>
          <th>Prediction Type</th>
          <th>n</th>
          <th>Avg Predicted POP</th>
          <th>Outcome Zones</th>
          <th>Calibration (by POP bucket)</th>
        </tr></thead>
        <tbody>${structRows}</tbody>
      </table>
    </div>
    <p class="hint" style="margin-top:1rem;font-size:0.78rem">
      <strong>Calibration error</strong> = realized% − predicted%.
      Positive (green) = model is conservative — trades won more often than predicted.
      Negative (red) = model is optimistic — trades won less than predicted.
      Buckets with n &lt; 5 are statistically unreliable.
    </p>`;
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function initTabs() {
  const tabs    = document.querySelectorAll(".pt-tab-btn");
  const panels  = document.querySelectorAll(".pt-tab-panel");

  tabs.forEach(btn => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      tabs.forEach(b   => b.classList.toggle("active", b.dataset.tab === target));
      panels.forEach(p => p.classList.toggle("active", p.dataset.tab === target));
      if (target === "rangeanalysis") loadRangeAnalysis();
    });
  });
}

// ── Main load ─────────────────────────────────────────────────────────────────

let _allTrades = [];

async function loadDashboard() {
  _analyzeMode = false;
  // Phase 3: Performance monitoring
  if (typeof window.PerformanceMonitor !== 'undefined') {
    window.PerformanceMonitor.mark('load-paper-dashboard');
  }

  try {
    // Phase 3: CacheManager wrapping
    const data = await fetch("/api/paper-trades/summary").then(r => r.json());

    if (!data.ok) throw new Error(data.error || "API error");

    document.getElementById("pt-summary-cards").innerHTML = renderSummaryCards(data);
    renderEquityCurve(data.equity_curve || []);

    const hasClosed = (data.closed_count ?? 0) > 0;
    document.getElementById("pt-breakdowns").style.display = hasClosed ? "" : "none";
    if (hasClosed) {
      renderBreakdown("pt-by-structure", data.by_structure);
      renderBreakdown("pt-by-signal",    data.by_signal);
    }

    const openTrades   = data.open_trades     || [];
    const closedTrades = data.recent_closed   || [];
    _allTrades = data.all_trades || [...openTrades, ...closedTrades];

    renderOpenTrades(openTrades);
    renderClosedTrades(closedTrades);
    renderDayWiseLog(_allTrades);

    // Auto-fetch live marks on page load to show real-time P&L and price in the table
    if (openTrades.length > 0) fetchLiveMarks();
    startLiveRefresh();

    // Phase 3: Record performance
    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure('load-paper-dashboard');
    }

  } catch(e) {
    // Phase 3: Record performance even on error
    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure('load-paper-dashboard');
    }
    document.getElementById("pt-summary-cards").innerHTML =
      `<div class="pt-card"><p class="fail">Error loading data: ${esc(String(e))}</p></div>`;
  }
}

function fetchLiveMarks() {
  const marksMap = {};
  const es = new EventSource("/api/paper-trades/live-marks");

  es.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.done) {
      es.close();
      return;
    }
    if (msg.error) {
      console.warn("live-marks stream error:", msg.error);
      es.close();
      return;
    }
    if (msg.id && msg.data) {
      marksMap[msg.id] = msg.data;
      // Apply this trade's mark immediately as it arrives
      applyLiveMarks(marksMap);
    }
  };

  es.onerror = () => {
    es.close();
    console.warn("live-marks SSE connection failed");
  };
}

// ── Controls ──────────────────────────────────────────────────────────────────

function _setScanRunning(statusEl, btn, label) {
  statusEl.innerHTML  = `<span class="pt-spinner"></span>${label} running… (1–3 min)`;
  statusEl.className  = "status running";
  if (btn) btn.disabled = true;
}

function _setScanDone(statusEl, btn, label, detail) {
  statusEl.textContent = `✓ ${label} complete. ${detail}`;
  statusEl.className   = "status pass";
  if (btn) btn.disabled = false;
}

function _setScanError(statusEl, btn, msg) {
  statusEl.textContent = `Error: ${msg}`;
  statusEl.className   = "status fail";
  if (btn) btn.disabled = false;
}

async function runScan(endpoint, label, statusEndpoint) {
  // Phase 3: Performance monitoring
  if (typeof window.PerformanceMonitor !== 'undefined') {
    window.PerformanceMonitor.mark(`run-scan:${label}`);
  }

  const statusEl = document.getElementById("pt-run-status");
  const btn      = document.querySelector(`[data-scan="${endpoint}"]`);
  _setScanRunning(statusEl, btn, label);
  try {
    const res  = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: true }),
    });
    const data = await res.json();
    if (data.skipped) {
      statusEl.textContent = `Skipped: ${data.reason}`;
      statusEl.className   = "status";
      if (btn) btn.disabled = false;
      return;
    }
    if (!data.ok && !data.running) {
      _setScanError(statusEl, btn, data.error || "Unknown error");
      return;
    }
    // Background run — poll status endpoint
    if (data.running && statusEndpoint) {
      const interval = setInterval(async () => {
        try {
          const sr = await fetch(statusEndpoint);
          const sd = await sr.json();
          if (sd.state === "done") {
            clearInterval(interval);
            const r      = sd.result || {};
            const detail = r.recorded != null ? `Recorded ${r.recorded} trade(s).` : `Updated ${r.updated ?? 0} trade(s).`;
            _setScanDone(statusEl, btn, label, detail);
            await loadDashboard();
          } else if (sd.state === "error") {
            clearInterval(interval);
            _setScanError(statusEl, btn, sd.error);
          }
        } catch(_) {}
      }, 10000);
      return;
    }
    // Synchronous result (evening check)
    const detail = data.recorded != null
      ? `Recorded ${data.recorded} trade(s).`
      : `Updated ${data.updated ?? 0} trade(s).`;
    _setScanDone(statusEl, btn, label, detail);
    await loadDashboard();

    // Phase 3: Record performance
    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure(`run-scan:${label}`);
    }
  } catch(e) {
    // Phase 3: Record performance even on error
    if (typeof window.PerformanceMonitor !== 'undefined') {
      window.PerformanceMonitor.measure(`run-scan:${label}`);
    }
    _setScanError(statusEl, btn, e);
  }
}

async function deleteTrade(id) {
  if (!confirm(`Remove paper trade ${id}?`)) return;
  const res = await fetch(`/api/paper-trades/delete/${encodeURIComponent(id)}`, { method: "DELETE" });
  const d   = await res.json();
  if (d.ok) await loadDashboard();
  else alert("Delete failed: " + d.error);
}

// ── Collapse toggle ───────────────────────────────────────────────────────────

function initCardCollapse() {
  document.addEventListener("click", e => {
    // collapse button OR clicking the header row itself (but not on interactive children)
    const hdr = e.target.closest(".tc-header");
    if (!hdr) return;
    if (e.target.closest(".pt-del-btn") || e.target.closest(".pt-price-badge-slot a")) return;
    const card = hdr.closest(".pt-trade-card");
    if (!card) return;
    const expanded = card.classList.toggle("pt-expanded");
    hdr.setAttribute("aria-expanded", expanded);
    hdr.querySelector(".pt-collapse-btn").textContent = expanded ? "▲" : "▼";
  });
  document.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const hdr = e.target.closest(".tc-header");
    if (!hdr) return;
    e.preventDefault();
    hdr.click();
  });
}

// ── Background live-marks refresh (every 5 min) ───────────────────────────────

let _liveRefreshTimer = null;

function startLiveRefresh() {
  if (_liveRefreshTimer) clearInterval(_liveRefreshTimer);
  _liveRefreshTimer = setInterval(() => {
    if (_openTrades.length > 0) fetchLiveMarks();
  }, 5 * 60 * 1000);
}

// ── Analyze All (on-demand) ───────────────────────────────────────────────────

function analyzeAllTrades() {
  if (!_openTrades.length) return;
  const btn = document.getElementById("pt-analyze-btn");
  if (btn) { btn.disabled = true; btn.textContent = "⚡ Analyzing…"; }

  // Switch to card view
  _analyzeMode = true;
  renderOpenTradesCards(_openTrades);

  // Live marks via SSE
  fetchLiveMarks();
  // Greeks drift + market analysis per trade
  setupGreeksDriftForTrades(_openTrades);
  setupTradeAnalysis(_openTrades);
  // Start background refresh now that analysis is running
  startLiveRefresh();

  setTimeout(() => {
    if (btn) { btn.disabled = false; btn.textContent = "⚡ Analyze All"; }
  }, 5000);
}

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initCardCollapse();
  loadDashboard();

  document.getElementById("pt-refresh-btn")
    .addEventListener("click", loadDashboard);

  document.getElementById("pt-morning-btn")
    .addEventListener("click", () => runScan("/api/paper-trades/morning-scan", "Morning Scan", "/api/paper-trades/morning-scan/status"));

  document.getElementById("pt-evening-btn")
    .addEventListener("click", () => runScan("/api/paper-trades/evening-check", "Evening Check"));

  document.getElementById("pt-analyze-btn")
    .addEventListener("click", analyzeAllTrades);

  document.addEventListener("click", e => {
    const btn = e.target.closest(".pt-del-btn");
    if (btn) deleteTrade(btn.dataset.id);
  });
});
