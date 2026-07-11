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

  const W = 760, H = 180, PAD = { t: 16, r: 20, b: 36, l: 60 };
  const iW = W - PAD.l - PAD.r;
  const iH = H - PAD.t - PAD.b;

  const vals  = points.map(p => p.cumulative);
  const minV  = Math.min(0, ...vals);
  const maxV  = Math.max(0, ...vals);
  const range = maxV - minV || 1;

  const xOf = (i)  => PAD.l + (i / (points.length - 1)) * iW;
  const yOf = (v)  => PAD.t + iH - ((v - minV) / range) * iH;
  const y0  = yOf(0);

  const pts = points.map((p, i) => `${xOf(i).toFixed(1)},${yOf(p.cumulative).toFixed(1)}`).join(" ");

  const tickCount = 4;
  const tickLines = Array.from({length: tickCount + 1}, (_, i) => {
    const v = minV + (range / tickCount) * i;
    const y = yOf(v).toFixed(1);
    const color = v === 0 ? "rgba(255,255,255,0.3)" : "rgba(255,255,255,0.06)";
    return `
      <line x1="${PAD.l}" y1="${y}" x2="${W - PAD.r}" y2="${y}" stroke="${color}" stroke-width="1"/>
      <text x="${PAD.l - 6}" y="${y}" dy="0.35em" text-anchor="end" font-size="10" fill="#888">${v >= 0 ? "+" : ""}$${v.toFixed(0)}</text>`;
  }).join("");

  const step = Math.max(1, Math.floor(points.length / 6));
  const xLabels = points
    .filter((_, i) => i % step === 0 || i === points.length - 1)
    .map((p) => {
      const i = points.indexOf(p);
      return `<text x="${xOf(i).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="10" fill="#888">${p.date.slice(5)}</text>`;
    }).join("");

  const dots = points.map((p, i) => {
    const fill = p.win ? "#4caf50" : "#e53935";
    const tip  = `${p.date} ${p.ticker} ${p.structure}: ${p.pnl >= 0 ? "+" : ""}$${p.pnl.toFixed(2)}`;
    return `<circle cx="${xOf(i).toFixed(1)}" cy="${yOf(p.cumulative).toFixed(1)}" r="4" fill="${fill}" opacity="0.85"><title>${esc(tip)}</title></circle>`;
  }).join("");

  const zeroLine = minV < 0 && maxV > 0
    ? `<line x1="${PAD.l}" y1="${y0.toFixed(1)}" x2="${W - PAD.r}" y2="${y0.toFixed(1)}" stroke="rgba(255,255,255,0.25)" stroke-width="1" stroke-dasharray="4,3"/>`
    : "";

  const lastX = xOf(points.length - 1).toFixed(1);
  const areaPath = `M${PAD.l},${y0.toFixed(1)} ${points.map((p,i)=>`L${xOf(i).toFixed(1)},${yOf(p.cumulative).toFixed(1)}`).join(" ")} L${lastX},${y0.toFixed(1)} Z`;
  const areaColor = vals[vals.length-1] >= 0 ? "rgba(76,175,80,0.12)" : "rgba(229,57,53,0.12)";
  const lineColor = vals[vals.length-1] >= 0 ? "#4caf50" : "#e53935";

  document.getElementById("pt-equity-chart").innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;display:block;overflow:visible">
      ${tickLines}
      ${zeroLine}
      <path d="${areaPath}" fill="${areaColor}"/>
      <polyline points="${pts}" fill="none" stroke="${lineColor}" stroke-width="2"/>
      ${dots}
      ${xLabels}
    </svg>`;
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

function buildTradeCard(trade, liveData) {
  const live      = liveData ?? {};
  const mark      = live.mark       ?? trade.latest_mark;
  const unrealized= live.unrealized ?? trade.latest_unrealized;
  const loading   = liveData === null;
  const isDebit   = (trade.structure ?? "").includes("Debit");

  const maxProfit = trade.entry_credit ?? 0;
  const debitPaid = trade.max_loss    ?? null;

  const pctDone = (mark != null && maxProfit > 0)
    ? (isDebit
        ? Math.round((mark / maxProfit) * 100)
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

  // strikes display
  const stk = trade.strikes ?? {};
  let strikesStr = "—";
  if (stk.put_long != null)  strikesStr = `${stk.put_long}/${stk.put_short} · ${stk.call_short}/${stk.call_long}`;
  else if (stk.short != null && stk.long != null) strikesStr = `${stk.long}/${stk.short}`;
  else if (stk.short != null) strikesStr = `${stk.short}`;

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

function renderOpenTrades(trades) {
  _openTrades = trades;
  const el = document.getElementById("pt-open-table");
  document.getElementById("pt-open-count").textContent = trades.length ? `(${trades.length})` : "";

  if (!trades.length) {
    el.innerHTML = `<p class="muted na">No open paper trades. Run a morning scan to add today's top-3.</p>`;
    renderPortfolioSummary([], {});
    return;
  }

  el.innerHTML = trades.map(t => buildTradeCard(t, null)).join("");
  renderPortfolioSummary(trades, {});
  setupTradeAnalysis(trades);
  setupGreeksDriftForTrades(trades);
}

function _patchCardMetrics(cardEl, trade, live) {
  const isDebit   = (trade.structure ?? "").includes("Debit");
  const maxProfit = trade.entry_credit ?? 0;
  const mark      = live.mark       ?? trade.latest_mark;
  const unrealized= live.unrealized ?? trade.latest_unrealized;

  const pctDone = (mark != null && maxProfit > 0)
    ? (isDebit ? Math.round((mark / maxProfit) * 100) : Math.round((1 - mark / maxProfit) * 100))
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
    const cardEl = document.querySelector(`.pt-trade-card[data-id="${CSS.escape(trade.id)}"]`);
    if (cardEl) _patchCardMetrics(cardEl, trade, live);
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
  const isDebit    = (trade.structure ?? "").includes("Debit");
  const maxProfit  = trade.entry_credit ?? 0;
  const debitPaid  = trade.max_loss ?? null;
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
          <th>P&L/sh</th><th>P&L $</th><th>P&L %</th><th>Signal</th><th></th>
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

    const rows = trades.map(t => {
      const x       = t.exit ?? {};
      const isOpen  = t.status === "open";
      const isDebit = (t.structure ?? "").includes("Debit");
      const win     = x.win;
      const rowCls  = isOpen ? "" : (win ? "pt-row-win" : "pt-row-loss");

      // For open trades: show live mark data if available
      const live    = marks[t.id] ?? {};
      const unr     = live.unrealized ?? t.latest_unrealized;
      const unrTotal= unr != null ? parseFloat(unr) * 100 : null;
      const unrCls  = unr == null ? "na" : parseFloat(unr) >= 0 ? "pass" : "fail";

      const pnlPs   = isOpen ? unr              : x.pnl_per_share;
      const pnlTot  = isOpen ? unrTotal         : x.pnl_total;
      const pnlCls  = isOpen ? unrCls           : (win ? "pass" : "fail");
      const exitTxt = isOpen ? "—"              : esc(reasonLabel(x.reason));
      const liveTip = isOpen && live.mark != null ? ` title="mark $${live.mark.toFixed(3)}"` : "";

      return `
        <tr class="${rowCls}">
          <td><strong>${esc(t.ticker)}</strong></td>
          <td style="font-size:0.78rem;color:#aaa">${esc(t.structure)}</td>
          <td class="muted">${esc(t.expiry ?? "—")}</td>
          <td class="na">$${(t.entry_credit ?? 0).toFixed(3)}</td>
          <td class="muted" style="font-size:0.78rem">${isDebit ? "Debit paid" : "Max loss"}: $${(t.max_loss ?? 0).toFixed(3)}</td>
          <td>${statusLabel(t.status)}</td>
          <td class="muted" style="font-size:0.75rem">${exitTxt}</td>
          <td class="${pnlCls}"${liveTip}>${pnlPs != null ? fmt$(pnlPs, 3) : "—"}</td>
          <td class="${pnlCls}">${pnlTot != null ? fmt$(pnlTot) : "—"}</td>
          <td class="muted" style="font-size:0.75rem">${esc(t.signal_rating ?? "—")}</td>
        </tr>`;
    }).join("");

    return `
      ${dayHeader}
      <div class="table-scroll" style="margin-bottom:1.4rem">
        <table class="journal-table pt-trades-table">
          <thead><tr>
            <th>Ticker</th><th>Structure</th><th>Expiry</th>
            <th>Max Profit</th><th>Risk</th><th>Status</th><th>Exit</th>
            <th>P&L/sh</th><th>P&L $</th><th>Signal</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }).join("");

  el.innerHTML = overallBanner + html;
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
    });
  });
}

// ── Main load ─────────────────────────────────────────────────────────────────

let _allTrades = [];

async function loadDashboard() {
  // Phase 3: Performance monitoring
  if (typeof window.PerformanceMonitor !== 'undefined') {
    window.PerformanceMonitor.mark('load-paper-dashboard');
  }

  try {
    // Phase 3: CacheManager wrapping
    const data = typeof window.CacheManager !== 'undefined'
      ? await window.CacheManager.get(
          'paper-trades-summary',
          () => fetch("/api/paper-trades/summary").then(r => r.json())
        )
      : await fetch("/api/paper-trades/summary").then(r => r.json());

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

    if (openTrades.length > 0) fetchLiveMarks();

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

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initCardCollapse();
  loadDashboard();
  startLiveRefresh();

  document.getElementById("pt-refresh-btn")
    .addEventListener("click", loadDashboard);

  document.getElementById("pt-morning-btn")
    .addEventListener("click", () => runScan("/api/paper-trades/morning-scan", "Morning Scan", "/api/paper-trades/morning-scan/status"));

  document.getElementById("pt-evening-btn")
    .addEventListener("click", () => runScan("/api/paper-trades/evening-check", "Evening Check"));

  document.addEventListener("click", e => {
    const btn = e.target.closest(".pt-del-btn");
    if (btn) deleteTrade(btn.dataset.id);
  });
});
