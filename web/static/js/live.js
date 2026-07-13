// ── Greeks plain-English helpers ──────────────────────────────────────────────

function describeDirection(netDelta) {
  if (netDelta == null) return null;
  const a = Math.abs(netDelta);
  if (a < 0.05) return { text: "Direction neutral — minimal exposure to stock movement", cls: "na" };
  if (netDelta > 0) {
    if (a < 0.15) return { text: "Slightly bullish — stock just needs to avoid a sharp drop", cls: "pass" };
    if (a < 0.35) return { text: "Moderately bullish — expects stock to hold or rise", cls: "pass" };
    return { text: "Strongly bullish — large upside exposure (e.g. Jade Lizard naked put)", cls: "pass" };
  } else {
    if (a < 0.15) return { text: "Slightly bearish — stock just needs to avoid a sharp rally", cls: "fail" };
    if (a < 0.35) return { text: "Moderately bearish — expects stock to hold or fall", cls: "fail" };
    return { text: "Strongly bearish — large downside exposure", cls: "fail" };
  }
}

function describeTheta(netTheta) {
  if (netTheta == null) return null;
  const daily = Math.abs(netTheta) * 100; // $ per contract per day
  if (netTheta > 0)
    return {
      text: `Time works FOR you — earns ~$${daily.toFixed(2)}/day per contract as time passes`,
      cls: "pass",
    };
  return {
    text: `Time works AGAINST you — costs ~$${daily.toFixed(2)}/day per contract (move needed before decay erodes value)`,
    cls: "warn",
  };
}

function greeksBlock(netDelta, netTheta, netGamma, netVega) {
  const dir = describeDirection(netDelta);
  const th  = describeTheta(netTheta);
  if (!dir && !th) return "";
  const dirLine = dir
    ? `<div class="greek-line">
        <span class="greek-label">Direction:</span>
        <span class="greek-desc ${dir.cls}">${dir.text}</span>
        <span class="greek-tech">(Δ ${netDelta >= 0 ? "+" : ""}${netDelta})</span>
       </div>`
    : "";
  const thLine = th
    ? `<div class="greek-line">
        <span class="greek-label">Time Decay:</span>
        <span class="greek-desc ${th.cls}">${th.text}</span>
        <span class="greek-tech">(Θ ${netTheta >= 0 ? "+" : ""}${netTheta.toFixed(3)}/day per share)</span>
       </div>`
    : "";
  const gmLine = (netGamma != null)
    ? `<div class="greek-line">
        <span class="greek-label">Gamma:</span>
        <span class="greek-desc ${netGamma > 0 ? "greek-pos" : "greek-neg"}">${netGamma > 0 ? "Long gamma (profits from big moves)" : "Short gamma (decays in calm market)"}</span>
        <span class="greek-tech">(Γ ${netGamma >= 0 ? "+" : ""}${netGamma.toFixed(5)})</span>
       </div>`
    : "";
  const vgLine = (netVega != null)
    ? `<div class="greek-line">
        <span class="greek-label">Vega:</span>
        <span class="greek-desc ${netVega > 0 ? "greek-pos" : "greek-neg"}">${netVega > 0 ? "Long vega (benefits from IV expansion)" : "Short vega (benefits from IV contraction)"}</span>
        <span class="greek-tech">(ν ${netVega >= 0 ? "+" : ""}${netVega.toFixed(3)}/1% IV)</span>
       </div>`
    : "";
  return `<div class="greeks-explainer">${dirLine}${thLine}${gmLine}${vgLine}</div>`;
}

/**
 * Render Monte Carlo outcome + Kelly sizing suggestion.
 * @param {Object|null} mc - {prob_of_touch, worst_loss_95, expected_pnl, prob_profit_sim, n_sims}
 * @param {number|null} kelly - suggested fraction of capital (half-Kelly), or null
 */
function buildMonteCarloResult(mc, kelly) {
  if (!mc) {
    return `<p class="na">Monte Carlo not available for this structure (path-dependent or missing strike data).</p>`;
  }
  const touchCls = mc.prob_of_touch == null ? "na" : mc.prob_of_touch >= 50 ? "fail" : mc.prob_of_touch >= 25 ? "warn" : "pass";
  const pnlCls = mc.expected_pnl >= 0 ? "pass" : "fail";
  const kellyStr = kelly == null ? "—" : `${(kelly * 100).toFixed(1)}% of capital`;

  return `
    <div class="tc-mc-card">
      <div class="tc-mc-row"><span>Probability of Touch</span><span class="${touchCls}">${mc.prob_of_touch != null ? mc.prob_of_touch + "%" : "—"}</span></div>
      <div class="tc-mc-row"><span>95% Worst-Case Loss</span><span class="fail">${mc.worst_loss_95.toFixed(2)}</span></div>
      <div class="tc-mc-row"><span>Simulated Expected P&amp;L</span><span class="${pnlCls}">${mc.expected_pnl >= 0 ? "+" : ""}${mc.expected_pnl.toFixed(2)}</span></div>
      <div class="tc-mc-row"><span>Simulated POP</span><span>${mc.prob_profit_sim}%</span></div>
      <div class="tc-mc-row tc-mc-kelly"><span>Suggested Size (half-Kelly)</span><span>${kellyStr}</span></div>
      <p class="tc-mc-note muted">Based on ${mc.n_sims.toLocaleString()} simulated price paths. Sizing is informational only — nothing here executes a trade.</p>
    </div>
  `;
}

// Module-level store so sort can re-render without re-fetching
let _liveData = null;
let _sortKey = "signal";
let _sortDir = -1; // -1 = descending (best first), +1 = ascending

// Session-only exclusion set — "TICKER:Structure" strings excluded from top-3.
// Cleared on page refresh (never persisted to server).
const _excluded = new Set();

const SIGNAL_ORDER = { Strong: 4, Moderate: 3, Neutral: 2, Weak: 1, Conflicted: 0 };

// getRecommendedCandidate is provided by lib/position-health.js

/** Return numeric sort value for a row; null means "push to bottom". */
function sortValue(row, key) {
  const rec = getRecommendedCandidate(row);
  switch (key) {
    case "signal":   return row.signal_score ?? null;
    case "ev":       return rec?.ev ?? null;
    case "profit":   return rec?.max_profit ?? null;
    case "pop":      return rec?.pop ?? null;
    case "capital":  return rec?.capital_required ?? null;
    case "ticker":   return row.ticker ?? "";
    case "unusual":  return row.unusual_activity ? 1 : 0;
    case "bias":     return rec?.market_bias?.score ?? null;
    default:         return null;
  }
}

function sortedRows(rows) {
  return [...rows].sort((a, b) => {
    const va = sortValue(a, _sortKey);
    const vb = sortValue(b, _sortKey);
    // Nulls always last regardless of direction
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "string") return va.localeCompare(vb) * _sortDir;
    return (va - vb) * _sortDir;
  });
}

function fmtMoney(v) {
  return v != null ? "$" + v.toFixed(2) : "-";
}

// ── Sort bar ──────────────────────────────────────────────────────────────────

const SORT_BTNS = [
  { key: "signal",  label: "Signal",      title: "Sort by signal alignment score (Strong → Conflicted)" },
  { key: "ev",      label: "EV",          title: "Sort by expected value of the recommended trade (highest first)" },
  { key: "profit",  label: "Max Profit",  title: "Sort by max profit per share of the recommended trade" },
  { key: "pop",     label: "POP%",        title: "Sort by probability of profit of the recommended trade" },
  { key: "capital", label: "Capital Req", title: "Sort by capital required per contract (lowest first)" },
  { key: "ticker",  label: "Ticker A–Z",  title: "Sort alphabetically by ticker symbol" },
  { key: "unusual", label: "⚠ Unusual",   title: "Show tickers with unusual options activity (volume > 3× OI) first" },
  { key: "bias",    label: "⚡ Mkt Bias", title: "Sort by market bias score (Confirmed → Opposed). Reflects VIX, futures, and sector alignment with the trade structure." },
];

function renderSortBar() {
  const btns = SORT_BTNS.map(({ key, label, title }) => {
    const active = key === _sortKey;
    const arrow = active ? (_sortDir === -1 ? " ↓" : " ↑") : "";
    return `<button class="sort-btn${active ? " active" : ""}" data-sort="${key}" title="${title}">${label}${arrow}</button>`;
  }).join("");
  return `<div class="sort-bar"><span class="sort-label">Sort:</span>${btns}</div>`;
}

// buildHedgeBlock is provided by lib/hedge-block.js (shared with Live Positions)

// ── Market bias badge ─────────────────────────────────────────────────────────

function marketBiasBadge(bias) {
  if (!bias) return "";
  const CLS = {
    Confirmed: "bias-confirmed",
    Favorable: "bias-favorable",
    Neutral:   "bias-neutral",
    Caution:   "bias-caution",
    Opposed:   "bias-opposed",
  };
  const cls   = CLS[bias.label] ?? "bias-neutral";
  const pills = [
    bias.vix_regime   !== "Normal"  ? `VIX: ${bias.vix_regime}`      : "",
    bias.futures_bias !== "Neutral" ? `Futures: ${bias.futures_bias}` : "",
    bias.sector_bias  !== "Unknown" && bias.sector_bias !== "Neutral"
                                    ? `Sector: ${bias.sector_bias}`   : "",
    bias.risk_regime  && bias.risk_regime !== "Neutral"
                                    ? `Risk: ${bias.risk_regime}`     : "",
  ].filter(Boolean);
  const notes = (bias.notes || []);
  const detailHtml = [...pills.map(p => `<span class="bias-pill">${p}</span>`),
                      ...notes.map(n => `<span class="bias-note">${n}</span>`)].join("");
  return `<div class="mkt-bias-block ${cls}">
    <span class="bias-label">⚡ ${bias.label}</span>
    ${detailHtml ? `<span class="bias-details">${detailHtml}</span>` : ""}
  </div>`;
}

// buildPriceBadge is provided by lib/price-badge.js (shared with Live Positions and Paper Trades)

// ── Top Trades panel ──────────────────────────────────────────────────────────

function _signalPillClass(rating) {
  return { Strong: "tc-signal-strong", Moderate: "tc-signal-moderate",
           Neutral: "tc-signal-neutral", Weak: "tc-signal-weak",
           Conflicted: "tc-signal-conflicted" }[rating] ?? "tc-signal-neutral";
}

// ── Shared chip helper (used by renderTopTrades, buildTickerCard, buildMlExplainBlock) ──
const mkChip = (label, value, cls, tip) =>
  `<div class="tc-metric" title="${tip ?? ""}">
    <span class="tc-metric-label">${label}</span>
    <span class="tc-metric-value${cls ? " " + cls : ""}">${value}</span>
  </div>`;

// ── ML Explanation block (shared by top-3 and per-ticker cards) ──────────────
function buildMlExplainBlock(ml, rowCls = "tc-ml-row", signalCls = "tc-ml-signal",
                              valCls = "tc-ml-val", descCls = "tc-ml-desc",
                              wrapCls = "tc-ml-explain", titleCls = "tc-ml-explain-title") {
  if (!ml) return `<div class="${wrapCls}"><div class="${titleCls}">ML Analysis</div><p class="muted" style="font-size:.8rem;margin:0">ML cache is cold — no predictions yet. Visit the <a href="/scheduler">Scheduler</a> page to refresh.</p></div>`;
  if (!ml.ok) return `<div class="${wrapCls}"><div class="${titleCls}">ML Analysis</div><p class="muted" style="font-size:.8rem;margin:0">ML prediction unavailable: ${ml.error || "unknown error"}</p></div>`;

  const rows = [];
  const cls = (v, good, bad) => v >= good ? "pass" : v <= bad ? "fail" : "";

  if (ml.regime) {
    const rCls = ml.regime === "Uptrend" ? "pass" : ml.regime === "Downtrend" ? "fail" : "warn";
    const probaStr = ml.regime_proba
      ? Object.entries(ml.regime_proba).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${k} ${(v*100).toFixed(0)}%`).join(" · ")
      : "";
    const implication = ml.regime === "Uptrend"
      ? "Bull put spreads, covered calls, and call debit spreads align with trend."
      : ml.regime === "Downtrend"
      ? "Bear call spreads and put debit spreads align with trend. Avoid naked short puts."
      : "Range-bound — iron condors and short straddles may fit. Watch for breakout.";
    rows.push([`<span class="${valCls} ${rCls}">${ml.regime}</span>`,
      "ML Regime", `${implication}${probaStr ? " (" + probaStr + ")" : ""}`]);
  }

  if (ml.expected_move_pct != null) {
    const em = (ml.expected_move_pct * 100).toFixed(1);
    const fvol = ml.expected_vol != null ? ` (forecast vol: ${(ml.expected_vol*100).toFixed(1)}% ann.)` : "";
    rows.push([`<span class="${valCls}">±${em}%</span>`,
      "ML Move ±", `ML predicts ±${em}% over the next 10 days${fvol}. Place short strikes beyond this range for a cushion.`]);
  }

  if (ml.iv_direction) {
    const ivProb = ml.iv_expanding_prob != null ? ml.iv_expanding_prob : null;
    // Confidence for the stated direction: expanding → iv_expanding_prob, contracting → 1 - iv_expanding_prob
    const dirConf = ivProb != null
      ? (ml.iv_direction === "Expanding" ? ivProb : 1 - ivProb)
      : null;
    const confPct = dirConf != null ? Math.round(dirConf * 100) : null;
    const confStr = confPct != null ? ` (${confPct}% confidence)` : "";
    // Only colour as pass/warn when confidence is meaningful (>= 50%); below that it's neutral
    const ivCls = dirConf == null ? "" :
      dirConf < 0.50 ? "" :
      ml.iv_direction === "Expanding" ? "warn" : "pass";
    const weakSignal = dirConf != null && dirConf < 0.50;
    const ivDesc = weakSignal
      ? `IV direction signal is weak${confStr}. Model has no strong view on whether IV expands or contracts — treat as uncertain. Do not size based on IV direction alone.`
      : ml.iv_direction === "Expanding"
      ? `IV rank likely rising${confStr}. Credit spreads and iron condors face headwind — rising IV expands the position against you. Consider waiting or using wider wings.`
      : `IV rank likely falling${confStr}. Premium sellers have edge; debit spreads risk vol crush. Favour credit structures while IV contracts.`;
    rows.push([`<span class="${valCls} ${ivCls}">${ml.iv_direction}</span>`, "ML IV Dir", ivDesc]);
  }

  if (ml.p_up != null) {
    const pUp = ml.p_up * 100;
    const pCls = cls(pUp, 60, 40);
    const dir = ml.direction || (pUp >= 55 ? "Bullish lean" : pUp <= 45 ? "Bearish lean" : "No strong lean");
    const pDesc = pUp >= 60
      ? `Strong upside probability. Bullish structures (bull put spread, call debit spread) have model backing.`
      : pUp <= 40
      ? `Strong downside probability. Bearish structures (bear call spread, put debit spread) align with model.`
      : `Mixed directional signal. Non-directional trades (iron condor, short strangle) may be preferable.`;
    rows.push([`<span class="${valCls} ${pCls}">${pUp.toFixed(0)}%</span>`,
      "ML P(↑)", `${dir}. ${pDesc}`]);
  }

  if (ml.meta_score != null) {
    const m    = ml.meta_score;
    const mCls = cls(m, 65, 35);
    const pd   = ml.pred_dist;
    const conf = pd ? pd.confidence : null;
    const agr  = pd ? pd.model_agreement : null;
    const agrCls = agr === "High" ? "pass" : agr === "Low" ? "fail" : "warn";
    const agrBadge = agr ? ` <span class="${agrCls}" style="font-size:.75rem;font-weight:600">${agr} agreement</span>` : "";
    // When score is neutral (35–65) but agreement is High, the models are aligned on
    // a near-neutral score — not disagreeing. Distinguish from the true low-confidence case.
    const mDesc = m >= 65
      ? `Strong bullish consensus across all ML models. High-conviction setup.`
      : m <= 35
      ? `Strong bearish consensus across all ML models. Avoid bullish structures.`
      : agr === "High"
      ? `Models are in ${m >= 50 ? "slight bullish" : "slight bearish"} agreement (score near neutral). No strong directional edge — size conservatively and lean on rulebook signals.`
      : agr === "Low"
      ? `Models disagree — each pointing in a different direction. Treat as no signal; rely on rulebook only.`
      : `Models show no strong directional signal. Rely on rulebook signals and be cautious with size.`;
    const confStr = conf != null ? ` · Confidence ${(conf * 100).toFixed(0)}%` : "";
    rows.push([`<span class="${valCls} ${mCls}">${m.toFixed(0)}/100</span>${agrBadge}`,
      "ML Meta Score", `Stacked score from all 5 models (Regime, Return, Vol, POP, Anomaly).${confStr} ${mDesc}`]);
  }

  if (ml.pred_dist) {
    const pd = ml.pred_dist;
    if (pd.p10_pnl != null && pd.p90_pnl != null) {
      const p10Cls = pd.p10_pnl >= 0 ? "pass" : "fail";
      const p90Cls = pd.p90_pnl >= 0 ? "pass" : "fail";
      const evStr  = pd.ev_per_share != null ? ` · EV ${pd.ev_per_share >= 0 ? "+" : ""}$${pd.ev_per_share.toFixed(2)}/sh` : "";
      const srcStr = pd.vol_source ? ` (${pd.vol_source} engine)` : "";
      rows.push([
        `<span class="${valCls} ${p10Cls}">${pd.p10_pnl >= 0 ? "+" : ""}$${pd.p10_pnl.toFixed(2)}</span>` +
        ` — <span class="${valCls} ${p90Cls}">+$${pd.p90_pnl.toFixed(2)}</span>`,
        "MC P10/P90",
        `Monte Carlo 10th–90th percentile P&L per share${srcStr}.${evStr} A tighter range signals more predictable outcome; a wide range means high tail uncertainty.`
      ]);
    }
  }

  if (ml.anomaly_score != null) {
    const a = ml.anomaly_score;
    const aCls = ml.is_anomaly ? (a <= 20 ? "fail" : "warn") : "";
    const flags = (ml.anomaly_flags || []).slice(0, 3).join(", ") || "multi-feature outlier";
    const aDesc = ml.is_anomaly
      ? `UNUSUAL CONDITIONS: ${flags}. The ticker is outside normal historical patterns — ML model confidence is reduced. Use smaller position size.`
      : `Market conditions are within normal historical range. ML models are operating within their training distribution.`;
    rows.push([`<span class="${valCls} ${aCls}">${a.toFixed(0)}${ml.is_anomaly ? " ⚠" : ""}/100</span>`,
      "ML Anomaly", aDesc]);
  }

  if (!rows.length) return "";

  const rowsHtml = rows.map(([val, signal, desc]) =>
    `<div class="${rowCls}">
      <span class="${signalCls}">${signal}</span>
      ${val}
      <span class="${descCls}">${desc}</span>
    </div>`).join("");

  return `<div class="${wrapCls}">
    <div class="${titleCls}">ML Analysis</div>
    ${rowsHtml}
  </div>`;
}

function renderTopTrades(topTrades) {
  if (!topTrades || !topTrades.length) return "";

  const panels = topTrades.map((t, i) => {
    const profitCls  = t.meets_min_profit === true ? "pass" : t.meets_min_profit === false ? "fail" : "na";
    const lossCls    = t.meets_max_loss   === true ? "pass" : t.meets_max_loss   === false ? "fail" : "na";
    const newsCls    = { Bullish: "pass", Bearish: "fail", Mixed: "warn", Neutral: "na", "N/A": "na" }[t.news_sentiment] ?? "na";

    const isCalendar   = t.structure === "Calendar Spread";
    const isDiagonal   = t.structure === "Diagonal Spread";
    const isTimeSpread = isCalendar || isDiagonal;

    const popVal    = t.pop    != null ? `${t.pop}%`  : "—";
    const profitVal = isCalendar ? "N/A †" : t.max_profit != null ? fmtMoney(t.max_profit) : "—";
    const lossVal   = t.max_loss   != null ? fmtMoney(t.max_loss) : "—";
    const evVal     = isTimeSpread ? "N/A †" : (t.ev != null ? `${t.ev_is_proxy ? "*" : ""}${t.ev}` : "—");
    const capVal    = t.capital_required != null ? `$${t.capital_required.toFixed(0)}` : "—";
    const top3AnnGainPct = (!isCalendar && t.max_profit != null && t.max_loss != null && t.max_loss > 0 && t.dte > 0)
      ? ((t.max_profit / t.max_loss) * (365 / t.dte) * 100).toFixed(1) + "%"
      : "N/A";
    const top3AnnGainCls = top3AnnGainPct !== "N/A" ? (parseFloat(top3AnnGainPct) >= 50 ? "pass" : parseFloat(top3AnnGainPct) >= 20 ? "" : "warn") : "na";
    const signalTip = (t.signal_notes ?? []).join(" | ") || "No alignment data";

    const unusualBadge = t.unusual_activity
      ? `<span class="unusual-flag" title="Volume > 3× OI">⚠ Unusual</span>` : "";

    const excludeKey = `${t.ticker}:${t.structure}`;
    const chk = t.capital_check;
    const capitalWarnHtml = (chk && !chk.ok && chk.capital_type !== "shares") ? `
      <div class="capital-warn-bar">
        <span class="capital-warn-icon">⚠</span>
        <span class="capital-warn-msg">${chk.note}</span>
      </div>` : (chk && chk.requires_margin ? `
      <div class="capital-warn-bar capital-warn-margin">
        <span class="capital-warn-icon">🔒</span>
        <span class="capital-warn-msg">${chk.note}</span>
      </div>` : "");

    const excludeToggleHtml = `
      <label class="exclude-toggle" title="Remove this trade from Top 3 and promote the next best candidate">
        <input type="checkbox" class="exclude-chk" data-key="${excludeKey}"
               ${_excluded.has(excludeKey) ? "checked" : ""}>
        Exclude from Top 3
      </label>`;

    const aiHtml = t.ai_assessment ? (() => {
      const aiCls = { HIGH: "pass", MEDIUM: "warn", LOW: "fail" }[t.ai_confidence] ?? "na";
      return `<div class="top3-ai"><strong class="${aiCls}">${t.ai_provider} · ${t.ai_confidence}:</strong> ${t.ai_assessment}</div>`;
    })() : "";

    // ML chips for top-3
    const mlChipsHtml = (() => {
      const ml = t.ml;
      if (!ml || !ml.ok) return "";
      const chips = [];
      if (ml.regime) {
        const rCls = ml.regime === "Uptrend" ? "pass" : ml.regime === "Downtrend" ? "fail" : "warn";
        chips.push(mkChip("ML Regime", ml.regime, rCls,
          `Regime: ${ml.regime}. Probas: ${ml.regime_proba ? Object.entries(ml.regime_proba).map(([k,v])=>`${k} ${(v*100).toFixed(0)}%`).join(", ") : "N/A"}`));
      }
      if (ml.expected_move_pct != null) {
        chips.push(mkChip("ML EM±", `±${(ml.expected_move_pct*100).toFixed(1)}%`, "",
          `ML 10-day expected move (1σ). Forecast vol: ${ml.expected_vol != null ? (ml.expected_vol*100).toFixed(1)+"% ann." : "N/A"}`));
      }
      if (ml.iv_direction) {
        const ivCls = ml.iv_direction === "Expanding" ? "warn" : "pass";
        const ivProb = ml.iv_expanding_prob != null ? ` ${(ml.iv_expanding_prob*100).toFixed(0)}%` : "";
        chips.push(mkChip("ML IV", ml.iv_direction + ivProb, ivCls,
          ml.iv_direction === "Expanding" ? "IV likely rising — headwind for credit spreads." : "IV likely falling — premium sellers have edge."));
      }
      if (ml.p_up != null) {
        const pUpCls = ml.p_up >= 0.6 ? "pass" : ml.p_up <= 0.4 ? "fail" : "";
        chips.push(mkChip("ML P(↑)", `${(ml.p_up*100).toFixed(0)}%`, pUpCls,
          `ML direction model — probability next 10d return is positive.`));
      }
      if (ml.meta_score != null) {
        const metaCls = ml.meta_score >= 65 ? "pass" : ml.meta_score <= 35 ? "fail" : "";
        const pd  = ml.pred_dist;
        const agr = pd ? pd.model_agreement : null;
        const agrSuffix = agr ? ` · ${agr} agr.` : "";
        const conf = pd && pd.confidence != null ? ` Confidence ${(pd.confidence*100).toFixed(0)}%.` : "";
        chips.push(mkChip("ML Meta", `${ml.meta_score.toFixed(0)}/100${agrSuffix}`, metaCls,
          `Meta-ensemble (0–100): all 5 ML models stacked.${conf} ≥65 bullish, ≤35 bearish.`));
      }
      // MC distribution chip: p10/p90 range from GARCH Monte Carlo
      const pd = ml.pred_dist;
      if (pd && pd.p10_pnl != null && pd.p90_pnl != null) {
        const rangeStr = `$${pd.p10_pnl.toFixed(2)}–$${pd.p90_pnl.toFixed(2)}`;
        const rangeCls = pd.p10_pnl >= 0 ? "pass" : pd.p90_pnl >= 0 ? "warn" : "fail";
        const src = pd.vol_source ? ` (${pd.vol_source})` : "";
        chips.push(mkChip("MC P10/P90", rangeStr, rangeCls,
          `Monte Carlo P&L range per share${src}. P10=${pd.p10_pnl >= 0 ? "+" : ""}$${pd.p10_pnl.toFixed(2)}, P90=+$${pd.p90_pnl.toFixed(2)}.` +
          (pd.ev_per_share != null ? ` EV ${pd.ev_per_share >= 0 ? "+" : ""}$${pd.ev_per_share.toFixed(2)}/sh.` : "")));
      }
      if (ml.anomaly_score != null) {
        const anom = ml.anomaly_score;
        const anomCls = ml.is_anomaly ? (anom <= 20 ? "fail" : "warn") : "";
        const anomLabel = ml.is_anomaly ? `${anom.toFixed(0)}⚠` : `${anom.toFixed(0)}`;
        chips.push(mkChip("ML Anom", anomLabel, anomCls,
          `Anomaly score (100=normal, 0=extreme). ${ml.is_anomaly ? "UNUSUAL: " + (ml.anomaly_flags||[]).slice(0,2).join("; ") : "Within normal conditions."}`));
      }
      if (!chips.length) return "";
      return `<div class="tc-metric-grid top3-ml-chips">${chips.join("")}</div>`;
    })();

    // Expiry P&L
    const sp = candidateToSpread(t, t);
    const expiryPnl = (typeof renderPnlExplanation === "function") ? renderPnlExplanation(sp) : "";

    return `
      <div class="top3-panel${i === 0 ? " active" : ""}" data-top3="${i}">
        <div class="top3-card-hdr">
          <div class="top3-rank">#${i + 1}</div>
          <span class="top3-ticker">${t.ticker}</span>
          <span class="top3-structure-pill">${t.structure}</span>
          ${unusualBadge}
          <span class="top3-price ml-auto">${buildPriceBadge(t)}</span>
          ${excludeToggleHtml}
        </div>
        ${capitalWarnHtml}
        <div class="top3-body">
          <div class="top3-big-metrics">
            <div class="top3-metric">
              <span class="top3-metric-value">${popVal}</span>
              <span class="top3-metric-label">POP</span>
            </div>
            <div class="top3-metric">
              <span class="top3-metric-value ${profitCls}">${profitVal}</span>
              <span class="top3-metric-label">Max Profit</span>
            </div>
            <div class="top3-metric">
              <span class="top3-metric-value ${lossCls}">${lossVal}</span>
              <span class="top3-metric-label">Max Loss</span>
            </div>
            <div class="top3-metric">
              <span class="top3-metric-value">${evVal}</span>
              <span class="top3-metric-label">EV${t.ev_is_proxy ? "*" : ""}</span>
            </div>
            <div class="top3-metric">
              <span class="top3-metric-value">${capVal}</span>
              <span class="top3-metric-label">Capital Req</span>
            </div>
            <div class="top3-metric">
              <span class="top3-metric-value">${t.dte != null ? t.dte + "d" : "—"}</span>
              <span class="top3-metric-label">DTE</span>
            </div>
            <div class="top3-metric" title="Annualised return = (Max Profit / Capital) × (365 / DTE)">
              <span class="top3-metric-value ${top3AnnGainCls}">${top3AnnGainPct}</span>
              <span class="top3-metric-label">Ann. Gain</span>
            </div>
          </div>
          <div class="top3-info-grid">
            <span>Expiry: <strong>${t.expiry ?? "—"}</strong></span>
            <span>Take-profit: <strong>${fmtMoney(t.profit_target)}</strong></span>
            <span>ADX: <strong>${t.adx ?? "—"}</strong></span>
            <span>EMA200: <strong>${t.ema200_position ?? "—"}</strong></span>
            <span>PCR: <strong>${t.pcr ?? "—"}${t.pcr_sentiment && t.pcr_sentiment !== "N/A" ? " ("+t.pcr_sentiment+")" : ""}</strong></span>
            <span>News: <strong class="${newsCls}">${t.news_sentiment ?? "—"}</strong></span>
          </div>
          <div class="top3-signal-row">
            <span class="tc-signal-pill ${_signalPillClass(t.signal_rating)}" title="${signalTip}">${t.signal_rating ?? "—"}</span>
            ${(t.hedge || t.hedge_exact) ? `<span class="hedge-avail-badge">🛡 Hedge available</span>` : ""}
          </div>
          ${mlChipsHtml}
          ${buildMlExplainBlock(t.ml)}
          ${aiHtml}
          ${(t.signal_notes ?? []).length ? `<div class="signal-notes">${t.signal_notes.map(n => `<span class="signal-note">${n}</span>`).join("")}</div>` : ""}
          ${marketBiasBadge(t.market_bias)}
          <details class="top3-details-toggle">
            <summary>Trade details, Greeks &amp; Hedge</summary>
            <div class="top3-details-body">
              <p class="tab-details mb-sm">${t.details}</p>
              ${isCalendar ? `<p class="calendar-note">† Calendar: profit via theta/IV — not fixed at entry.</p>` : ""}
              ${isDiagonal ? `<p class="calendar-note">† Diagonal: estimated max profit, path-dependent.</p>` : ""}
              ${t.iv_term_note ? `<p class="tc-iv-note my-xs">📈 IV Term: ${t.iv_term_note}</p>` : ""}
              ${t.div_warning ? `<p class="div-warning">⚠️ Ex-div ${t.div_ex_date} — assignment risk on ITM shorts.</p>` : ""}
              ${greeksBlock(t.net_delta, t.net_theta, t.net_gamma, t.net_vega)}
              ${buildHedgeBlock(t.hedge, t.structure, t.hedge_exact, sp)}
            </div>
          </details>
          ${expiryPnl}
        </div>
      </div>`;
  }).join("");

  const tabBtns = topTrades.map((t, i) =>
    `<button class="top3-tab-btn${i === 0 ? " active" : ""}" data-top3="${i}">#${i+1} ${t.ticker} — ${t.structure}</button>`
  ).join("");

  return `
    <section class="panel top-trades">
      <h3>Top ${topTrades.length} Suggested Trades</h3>
      <p class="hint">Ranked by: criteria met → signal score + ML meta-score bonus → EV → lowest capital.
        * = EV proxy (Jade Lizard). † = not calculable at entry (Calendar/Diagonal).</p>
      <div class="top3-tab-bar">${tabBtns}</div>
      <div class="top3-panels">${panels}</div>
    </section>`;
}

// ── Ticker cards ──────────────────────────────────────────────────────────────

function buildTickerCard(row) {
  const candidates = row.candidates;
  const recIdx     = candidates.findIndex((c) => c.recommended);
  const rec        = candidates[recIdx] ?? null;

  // ── CSS class helpers ─────────────────────────────────────────────────────
  const adxCls    = row.adx == null ? "na" : row.adx > 25 ? "pass" : row.adx < 20 ? "warn" : "na";
  const rvolCls   = row.rel_volume == null ? "na" : row.rel_volume > 1.5 ? "pass" : row.rel_volume < 0.5 ? "warn" : "na";
  const pcrCls    = { Bullish: "pass", Bearish: "fail", Neutral: "na", "N/A": "na" }[row.pcr_sentiment] ?? "na";
  const ema200Cls = row.ema200_position === "above" ? "pass" : row.ema200_position === "below" ? "warn" : "na";
  const newsCls   = { Bullish: "pass", Bearish: "fail", Mixed: "warn", Neutral: "na", "N/A": "na" }[row.news_sentiment] ?? "na";
  const signalTip = (row.signal_notes ?? []).join(" | ") || "No alignment data";

  // ── Header ────────────────────────────────────────────────────────────────
  const unusualBadge = row.unusual_activity
    ? `<span class="unusual-flag" title="Total option volume > 3× open interest — unusual positioning">⚠ Unusual OI</span>` : "";

  const newsLink = (row.news_headlines ?? []).length
    ? `<a class="news-link" href="#" data-ticker="${row.ticker}" data-headlines='${JSON.stringify(row.news_headlines ?? [])}' data-bullish="${row.news_bullish ?? 0}" data-bearish="${row.news_bearish ?? 0}" data-sentiment="${row.news_sentiment ?? ""}">📰 ${row.news_sentiment ?? ""}</a>`
    : (row.news_sentiment ? `<span class="${newsCls} text-sm">${row.news_sentiment}</span>` : "");

  const oiDeltaTitle = row.oi_delta_calls != null
    ? `OI Δ: calls ${row.oi_delta_calls >= 0 ? "+" : ""}${row.oi_delta_calls} / puts ${row.oi_delta_puts >= 0 ? "+" : ""}${row.oi_delta_puts}`
    : "";
  const oiBadge = row.oi_delta_calls != null
    ? `<span class="oi-delta text-xs" title="${oiDeltaTitle}">OI Δ ${row.oi_delta_calls >= 0 ? "+" : ""}${row.oi_delta_calls}/${row.oi_delta_puts >= 0 ? "+" : ""}${row.oi_delta_puts}</span>`
    : "";

  // ── Signal notes strip ────────────────────────────────────────────────────
  const signalNotesHtml = (row.signal_notes ?? []).length
    ? `<div class="tc-signal-notes">${row.signal_notes.map(n => `<span class="signal-note">${n}</span>`).join("")}</div>`
    : "";

  // ── Market bias row ───────────────────────────────────────────────────────
  const biasHtml = row.market_bias ? `<div class="tc-bias-row">${marketBiasBadge(row.market_bias)}</div>` : "";

  // ── Left: signal metric chips ──────────────────────────────────────────────

  const metrics = [
    mkChip("Trend D", row.trend ?? "—", "", "Daily price trend from EMA crossover"),
    mkChip("Trend W", row.weekly_trend ?? "—", "", "Weekly price trend — higher timeframe confirmation"),
    mkChip("RSI", row.rsi ?? "—", "", "14-day Relative Strength Index. >70 overbought, <30 oversold."),
    mkChip("ADX", row.adx ?? "—", adxCls, "Average Directional Index. >25 = strong trend, <20 = choppy."),
    mkChip("IV Env", row.iv_env ?? "—", "", "Current implied volatility environment: High / Normal / Low."),
    mkChip("PCR", row.pcr != null ? `${row.pcr}` : "—", pcrCls, `Put/Call ratio. ${row.pcr_sentiment ?? ""}`),
    mkChip("EMA200", row.ema200_position ?? "—", ema200Cls, `200-day EMA. ${row.ema200 ? "($"+row.ema200+")" : ""} Price above = long-term uptrend.`),
    mkChip("RelVol", row.rel_volume != null ? row.rel_volume + "x" : "—", rvolCls, "Relative volume vs 20-day avg. >1.5x = elevated activity."),
    mkChip("IV Term", row.iv_term_shape ?? "—", "", row.iv_term_note ?? "IV term structure across expiries."),
    ...(row.hv20 != null ? [mkChip("HV20", (row.hv20 * 100).toFixed(1) + "%", "", "20-day historical (realised) volatility, annualised.")] : []),
    ...(row.iv_premium != null ? [mkChip("IV±HV", (row.iv_premium >= 0 ? "+" : "") + (row.iv_premium * 100).toFixed(1) + "%",
        row.iv_premium > 0.03 ? "pass" : row.iv_premium < -0.03 ? "warn" : "",
        `IV minus HV20. Positive = options rich → selling edge. IV/HV ratio: ${row.iv_hv_ratio?.toFixed(2) ?? "—"}`)] : []),
    ...(row.vol_skew_pct != null ? [mkChip("Skew", (row.vol_skew_pct >= 0 ? "+" : "") + row.vol_skew_pct.toFixed(1) + "%",
        row.vol_skew_pct > 5 ? "warn" : row.vol_skew_pct < -5 ? "pass" : "",
        "Put IV minus Call IV at ~5% OTM. Positive = downside fear skew.")] : []),
    ...(row.short_interest != null ? [mkChip("Short%", row.short_interest.toFixed(1) + "%",
        row.short_interest > 20 ? "fail" : row.short_interest > 10 ? "warn" : "",
        "Short interest as % of float. >20% = high squeeze risk.")] : []),
    ...(row.div_ex_date != null ? [mkChip("Ex-Div", row.div_ex_date,
        row.div_days_to_ex != null && row.div_days_to_ex <= 7 ? "warn" : "",
        `Ex-dividend date. Yield: ${row.div_yield != null ? row.div_yield + "%" : "N/A"}`)] : []),
    ...(row.analyst_label && row.analyst_label !== "N/A" ? [mkChip("Analysts",
        row.analyst_label,
        { Bullish: "pass", Bearish: "fail", Neutral: "" }[row.analyst_label] ?? "",
        `Analyst consensus: ${row.analyst_buy}B/${row.analyst_hold}H/${row.analyst_sell}S. Score: ${row.analyst_net_score?.toFixed(2) ?? "—"}`)] : []),
    mkChip("MACD", `${row.macd_trend ?? "—"}`, "", `MACD trend direction. Hist: ${row.macd_hist ?? "—"}`),
    ...(() => {
      const ml = row.ml;
      if (!ml) return [mkChip("ML", "No cache", "na", "ML cache cold — predictions not yet available. Trigger a cache refresh from the Scheduler page.")];
      if (!ml.ok) return [mkChip("ML", ml.error ? "Error" : "N/A", "na", ml.error || "ML prediction failed for this ticker.")];
      const chips = [];
      if (ml.regime) {
        const rCls = ml.regime === "Uptrend" ? "pass" : ml.regime === "Downtrend" ? "fail" : "warn";
        chips.push(mkChip("ML Regime", ml.regime, rCls, `ML regime classifier — ${ml.regime}. Probabilities: ${ml.regime_proba ? Object.entries(ml.regime_proba).map(([k,v])=>`${k} ${(v*100).toFixed(0)}%`).join(", ") : "N/A"}`));
      }
      if (ml.expected_move_pct != null) {
        const emStr = `±${(ml.expected_move_pct * 100).toFixed(1)}%`;
        chips.push(mkChip("ML EM±", emStr, "", `ML 10-day expected move (1σ) — size strikes at least ${emStr} from spot. Based on forecast vol ${ml.expected_vol != null ? (ml.expected_vol*100).toFixed(1)+"% ann." : ""}`));
      }
      if (ml.iv_direction) {
        const ivCls = ml.iv_direction === "Expanding" ? "warn" : "pass";
        const ivProb = ml.iv_expanding_prob != null ? ` ${(ml.iv_expanding_prob * 100).toFixed(0)}%` : "";
        chips.push(mkChip("ML IV", ml.iv_direction + ivProb, ivCls,
          `ML IV direction — ${ml.iv_direction === "Expanding"
            ? "IV rank likely rising: short-vol structures (credit spreads, iron condors) face headwind."
            : "IV rank likely falling: premium sellers have edge; debit spreads face vol crush risk."}`));
      }
      if (ml.p_up != null) {
        const pUpCls = ml.p_up >= 0.6 ? "pass" : ml.p_up <= 0.4 ? "fail" : "";
        chips.push(mkChip("ML P(↑)", `${(ml.p_up * 100).toFixed(0)}%`, pUpCls, `ML direction model — probability next 10d return is positive. ${ml.direction ?? ""}`));
      }
      if (ml.meta_score != null) {
        const metaCls = ml.meta_score >= 65 ? "pass" : ml.meta_score <= 35 ? "fail" : "";
        const pd2 = ml.pred_dist;
        const agr2 = pd2 ? pd2.model_agreement : null;
        const agrSuffix2 = agr2 ? ` · ${agr2} agr.` : "";
        const conf2 = pd2 && pd2.confidence != null ? ` Confidence ${(pd2.confidence*100).toFixed(0)}%.` : "";
        chips.push(mkChip("ML Meta", `${ml.meta_score.toFixed(0)}/100${agrSuffix2}`, metaCls,
          `Meta-ensemble score (0–100): stacked output of all 5 ML models.${conf2} ` +
          `≥65 = bullish lean, ≤35 = bearish lean, 35–65 = no strong consensus.`));
      }
      if (ml.anomaly_score != null) {
        const anom = ml.anomaly_score;
        const anomCls = ml.is_anomaly ? (anom <= 20 ? "fail" : "warn") : "";
        const anomLabel = ml.is_anomaly ? `${anom.toFixed(0)}⚠` : `${anom.toFixed(0)}`;
        const flagStr = (ml.anomaly_flags || []).slice(0, 2).join("; ") || "multi-feature outlier";
        chips.push(mkChip("ML Anom", anomLabel, anomCls,
          `Anomaly detector score (0–100): 100=normal, 0=extreme outlier. ` +
          (ml.is_anomaly ? `UNUSUAL: ${flagStr}. ML models may be outside training distribution.` : `Within normal market conditions.`)));
      }
      return chips;
    })(),
    // ── IV Surface edge chip ───────────────────────────────────────────────────
    ...(() => {
      const rec = (candidates || []).find(c => c.recommended);
      if (!rec || rec.iv_edge_vp == null) return [];
      const vp  = rec.iv_edge_vp;
      const lbl = rec.iv_edge_label || "fair";
      const cls = lbl === "expensive" ? "pass" : lbl === "cheap" ? "pass" : lbl === "overpay" ? "fail" : lbl === "undersell" ? "fail" : "";
      const icon = vp > 1.5 ? "↑" : vp < -1.5 ? "↓" : "≈";
      const tip = `SVI surface edge on short strike: ${vp > 0 ? "+" : ""}${vp.toFixed(1)} vol-pts vs fitted surface. `
        + `${lbl === "expensive" ? "Selling expensive IV — surface edge in your favour." : lbl === "cheap" ? "Buying cheap IV — good entry vs surface." : lbl === "overpay" ? "Buying expensive IV — paying above surface." : lbl === "undersell" ? "Selling cheap IV — surface edge against you." : "Strike is fairly priced vs the SVI surface."}`;
      return [mkChip("IV Edge", `${icon}${vp > 0 ? "+" : ""}${vp.toFixed(1)}vp`, cls, tip)];
    })(),
  ];

  // ── Right: recommended trade ────────────────────────────────────────────────
  function buildTradeSection(c) {
    if (!c || (c.max_profit == null && c.max_loss == null))
      return `<div class="tc-trade"><p class="na m-0">${c?.details || "No valid trade found for this structure."}</p></div>`;

    const profitCls   = c.meets_min_profit === true ? "pass" : c.meets_min_profit === false ? "fail" : "na";
    const lossCls     = c.meets_max_loss   === true ? "pass" : c.meets_max_loss   === false ? "fail" : "na";
    const isCalendar  = c.structure === "Calendar Spread";
    const isDiagonal  = c.structure === "Diagonal Spread";
    const isTimeSpread = isCalendar || isDiagonal;

    const popVal    = c.pop != null ? `${c.pop}%` : "—";
    const profitVal = isCalendar ? "N/A†" : (isDiagonal ? `~${fmtMoney(c.max_profit)}†` : fmtMoney(c.max_profit));
    const lossVal   = fmtMoney(c.max_loss);
    const evVal     = isTimeSpread ? "N/A†" : (c.ev ?? "—");
    const capVal    = c.capital_required != null ? `$${c.capital_required.toFixed(0)}` : "—";

    // max_profit is per-share; max_loss is per-share → return on risk = max_profit / max_loss
    const _annProfit = isCalendar ? null : c.max_profit;
    const _annLoss   = isCalendar ? null : c.max_loss;
    const annGainPct = (_annProfit != null && _annLoss != null && _annLoss > 0 && row.dte > 0)
      ? ((_annProfit / _annLoss) * (365 / row.dte) * 100).toFixed(1) + "%"
      : "N/A";
    const annGainCls = annGainPct !== "N/A" ? (parseFloat(annGainPct) >= 50 ? "pass" : parseFloat(annGainPct) >= 20 ? "" : "warn") : "na";

    const tradeNote = isCalendar
      ? `<p class="calendar-note">† Max Profit/EV not fixed at entry — realises via theta/IV. Max loss = debit paid.</p>`
      : isDiagonal
      ? `<p class="calendar-note">† Estimated max profit; path-dependent. Max loss = net debit.</p>`
      : "";

    // Expiry P&L
    const sp = candidateToSpread(c, row);
    const expiryPnl = (typeof renderPnlExplanation === "function") ? renderPnlExplanation(sp) : "";

    // How close price already is to the strike that would define max loss
    const proximityBadge = buildStrikeProximityBadge(getCandidateShortStrikes(c), row.spot);

    return `<div class="tc-trade">
      <div class="tc-trade-hdr">
        <span class="tc-structure-name">${c.structure}</span>
        <span class="tc-rec-star" title="Rulebook-recommended for current conditions">★</span>
        ${proximityBadge}
      </div>
      <div class="tc-trade-metrics">
        <div class="tc-trade-metric">
          <span class="tc-trade-metric-value">${popVal}</span>
          <span class="tc-trade-metric-label">POP</span>
        </div>
        <div class="tc-trade-metric">
          <span class="tc-trade-metric-value ${profitCls}">${profitVal}</span>
          <span class="tc-trade-metric-label">Max Profit</span>
        </div>
        <div class="tc-trade-metric">
          <span class="tc-trade-metric-value ${lossCls}">${lossVal}</span>
          <span class="tc-trade-metric-label">Max Loss</span>
        </div>
        <div class="tc-trade-metric" title="Annualised return = (Max Profit / Capital) × (365 / DTE). Calendar/Diagonal: N/A (path-dependent).">
          <span class="tc-trade-metric-value ${annGainCls}">${annGainPct}</span>
          <span class="tc-trade-metric-label">Ann. Gain</span>
        </div>
      </div>
      <div class="tc-trade-sub">
        <span>EV: <strong>${evVal}</strong></span>
        <span>Capital: <strong>${capVal}</strong></span>
        <span>DTE: <strong>${row.dte != null ? row.dte + "d" : "—"}</strong></span>
        <span>Expiry: <strong>${row.expiry ?? "—"}</strong></span>
        <span>Take-profit: <strong>${fmtMoney(c.profit_target)}</strong></span>
      </div>
      ${tradeNote}
      <p class="tc-trade-details">${c.details}</p>
      ${greeksBlock(c.net_delta, c.net_theta, c.net_gamma, c.net_vega)}
      ${expiryPnl}
      <button class="tc-mc-btn" type="button"
        data-row='${escHtml(JSON.stringify({ spot: row.spot, atm_iv: row.atm_iv, hv20: row.hv20, dte: row.dte, risk_free_rate: row.risk_free_rate }))}'
        data-candidate='${escHtml(JSON.stringify(c))}'>📊 Monte Carlo &amp; Sizing</button>
      <div class="tc-mc-result"></div>
    </div>`;
  }

  // ── Alternative structure tabs + panels ───────────────────────────────────
  // Only show structures that have an actual valid trade (max_profit is set).
  // Structures with "No strikes found" clutter the tab bar without useful info.
  const altCandidates = candidates.filter((c) => !c.recommended && c.max_profit != null);
  const altsHtml = altCandidates.length ? `
    <div class="tc-alts">
      <div class="tc-alts-label">Other Structures</div>
      <div class="tabs tabs-flush">
        ${altCandidates.map((c, i) =>
          `<button class="tab-btn tc-alt-btn" data-alt="${i}">${c.structure}</button>`
        ).join("")}
      </div>
      ${altCandidates.map((c, i) => {
        const profitCls  = c.meets_min_profit === true ? "pass" : c.meets_min_profit === false ? "fail" : "na";
        const lossCls    = c.meets_max_loss   === true ? "pass" : c.meets_max_loss   === false ? "fail" : "na";
        const isCalendar = c.structure === "Calendar Spread";
        const isDiag     = c.structure === "Diagonal Spread";
        const isTsp      = isCalendar || isDiag;
        const evCell     = isTsp ? `<div class="value na">N/A †</div>` : `<div class="value">${c.ev ?? "—"}</div>`;
        const profitCell = isCalendar ? `<div class="value na">N/A †</div>`
                         : isDiag     ? `<div class="value ${profitCls}">~${fmtMoney(c.max_profit)} †</div>`
                         :              `<div class="value ${profitCls}">${fmtMoney(c.max_profit)}</div>`;
        const popCell    = (isCalendar && c.pop == null) ? `<div class="value na">N/A †</div>` : `<div class="value">${c.pop ?? "—"}</div>`;
        const altAnnProfit = isCalendar ? null : c.max_profit;
        const altAnnLoss   = isCalendar ? null : c.max_loss;
        const altAnnGainPct = (altAnnProfit != null && altAnnLoss != null && altAnnLoss > 0 && row.dte > 0)
          ? ((altAnnProfit / altAnnLoss) * (365 / row.dte) * 100).toFixed(1) + "%"
          : "N/A";
        const altAnnGainCls = altAnnGainPct !== "N/A" ? (parseFloat(altAnnGainPct) >= 50 ? "pass" : parseFloat(altAnnGainPct) >= 20 ? "" : "warn") : "na";
        const altSp  = candidateToSpread(c, row);
        const altPnl = (typeof renderPnlExplanation === "function") ? renderPnlExplanation(altSp) : "";
        const altProximityBadge = buildStrikeProximityBadge(getCandidateShortStrikes(c), row.spot);
        return `
          <div class="tc-alt-panel" data-alt="${i}">
            ${altProximityBadge}
            <p class="tab-details">${c.details}</p>
            ${greeksBlock(c.net_delta, c.net_theta, c.net_gamma, c.net_vega)}
            <div class="tab-stats">
              <div class="stat"><div class="label" title="${COLUMN_HELP["Max Profit"]}">Max Profit</div>${profitCell}</div>
              <div class="stat"><div class="label" title="${COLUMN_HELP["Max Loss"]}">Max Loss</div><div class="value ${lossCls}">${fmtMoney(c.max_loss)}</div></div>
              <div class="stat"><div class="label" title="${COLUMN_HELP["POP%"]}">POP%</div>${popCell}</div>
              <div class="stat"><div class="label" title="${COLUMN_HELP["EV"]}">EV</div>${evCell}</div>
              <div class="stat"><div class="label" title="Annualised return = (Max Profit / Capital) × (365 / DTE). Calendar/Diagonal: N/A.">Ann. Gain</div><div class="value ${altAnnGainCls}">${altAnnGainPct}</div></div>
              <div class="stat"><div class="label" title="${COLUMN_HELP["Take-Profit"]}">Take-Profit</div><div class="value">${fmtMoney(c.profit_target)}</div></div>
            </div>
            ${altPnl}
          </div>`;
      }).join("")}
    </div>` : "";

  // ── Hedge ─────────────────────────────────────────────────────────────────
  const hasHedge = rec && (rec.hedge || rec.hedge_exact);
  const hedgeWrap = hasHedge
    ? `<div class="tc-hedge-wrap">${buildHedgeBlock(rec.hedge, rec.structure, rec.hedge_exact, candidateToSpread(rec, row))}</div>`
    : "";

  // ── Assemble ──────────────────────────────────────────────────────────────
  return `
    <div class="ticker-card">
      <div class="tc-header">
        <span class="tc-ticker">${row.ticker}</span>
        ${buildPriceBadge(row)}
        <div class="tc-badges">
          <span class="tc-signal-pill ${_signalPillClass(row.signal_rating)}" title="${signalTip}">${row.signal_rating ?? "—"}</span>
          ${unusualBadge}
          ${newsLink}
          ${oiBadge}
        </div>
        <div class="tc-meta-row">
          <span>Expiry <strong>${row.expiry ?? "—"} (${row.dte ?? "—"}d)</strong></span>
          <span>IV <strong>${row.iv_env ?? "—"}</strong></span>
          <span>Trend <strong>${row.trend ?? "—"}</strong></span>
        </div>
        <button class="tc-collapse-btn" title="Collapse / expand">▲</button>
      </div>
      <div class="tc-collapsible">
        ${signalNotesHtml}
        ${biasHtml}
        <div class="tc-body">
          <div class="tc-signals">
            <div class="tc-col-title">Market Signals</div>
            <div class="tc-metric-grid">${metrics.join("")}</div>
            ${buildMlExplainBlock(row.ml)}
          </div>
          ${buildTradeSection(rec)}
        </div>
        ${altsHtml}
        ${hedgeWrap}
      </div>
    </div>`;
}

// ── Exclude-toggle re-fetch ───────────────────────────────────────────────────
// Re-runs build_top_trades server-side with current exclusion set.
// Uses the already-scanned rows cached in _liveData — no full re-scan.
function _refetchTopTrades() {
  if (!_liveData) return;
  const resultsEl = document.getElementById("liveResults");
  if (!resultsEl) return;

  const section = resultsEl.querySelector(".top-trades");
  if (section) {
    section.innerHTML = `<p class="hint" style="padding:1rem">Recalculating top trades…</p>`;
  }

  const excludeParam = [..._excluded].map(encodeURIComponent).join(",");
  // Re-call the analyze endpoint with the cached tickers + exclusions.
  // Pass tickers from the last scan so we don't re-scan everything.
  const tickers = (_liveData.rows || []).map(r => r.ticker).join(",");
  const url = `/api/analyze?tickers=${encodeURIComponent(tickers)}&exclude=${excludeParam}`;

  // Use a short SSE stream — we only need the 'done' event (top_trades)
  const es = new EventSource(url);
  es.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "done") {
        es.close();
        _liveData.top_trades = msg.top_trades;
        const html = renderTopTrades(msg.top_trades);
        const existing = resultsEl.querySelector(".top-trades");
        if (existing) {
          existing.insertAdjacentHTML("afterend", html || "");
          existing.remove();
        } else if (html) {
          resultsEl.insertAdjacentHTML("afterbegin", html);
        }
      }
    } catch (_) {}
  };
  es.onerror = () => {
    es.close();
    const s = resultsEl.querySelector(".top-trades");
    if (s) s.innerHTML = `<p class="fail" style="padding:1rem">Recalculation failed — try again.</p>`;
  };
}

// ── Main render ───────────────────────────────────────────────────────────────

function renderTickerSection(rows) {
  // Filter: only rows with a recommended candidate
  const visible = [];
  let skipped = 0;
  for (const row of rows) {
    if (row.status && row.status.startsWith("SKIP")) { skipped++; continue; }
    if (!row.candidates || !row.candidates.length)   { skipped++; continue; }
    if (!row.candidates.some((c) => c.recommended))  { skipped++; continue; }
    visible.push(row);
  }

  if (!visible.length) {
    return `<p class="na">No tickers currently have a recommended trade (${skipped} skipped/no-trade).</p>`;
  }

  const sorted = sortedRows(visible);
  const cards  = sorted.map(buildTickerCard).join("");
  const hint   = `<p class="hint">Showing ${visible.length} tickers with a rulebook-recommended structure (${skipped} hidden — no trade or skipped).
    Bold tab (*) = structure recommended by the rulebook's situation matrix for this ticker right now.
    POP = approx. probability of profit (from option delta). EV = approx. expected value per share, assuming POP.
    † = value not calculable at entry (Calendar Spread).</p>`;
  return renderSortBar() + cards + hint;
}

function renderLiveResults(data) {
  const el = document.getElementById("live-results");
  if (data.error) {
    el.innerHTML = `<p class="fail">${data.error}</p>`;
    return;
  }
  _liveData = data;
  const tickerHtml = renderTickerSection(data.rows);
  if (data.top_trades !== null) {
    const topTradesHtml = renderTopTrades(data.top_trades);
    el.innerHTML = topTradesHtml + tickerHtml;
  } else {
    // Streaming update — preserve existing top trades, only refresh ticker rows
    const existing = el.querySelector(".top-trades");
    el.innerHTML = (existing ? existing.outerHTML : "") + tickerHtml;
  }
}

// Re-render only the ticker section (keep top trades untouched)
function reRenderTickers() {
  if (!_liveData) return;
  const el = document.getElementById("live-results");
  // Replace everything after the top-trades panel
  const topSection = el.querySelector(".top-trades");
  // Remove old sort bar + cards + hint (everything that isn't .top-trades)
  [...el.children].forEach((child) => {
    if (!child.classList.contains("top-trades")) child.remove();
  });
  el.insertAdjacentHTML("beforeend", renderTickerSection(_liveData.rows));
}

// ── Market Context panel ──────────────────────────────────────────────────────

function pctCls(v) {
  if (v == null) return "na";
  return v > 0.5 ? "pass" : v < -0.5 ? "fail" : "na";
}

function pctStr(v) {
  if (v == null) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
}

function renderMarketContext(data) {
  const el = document.getElementById("mkt-ctx-body");
  if (!el) return;

  // Age label
  const ageEl = document.getElementById("mkt-ctx-age");
  if (ageEl && data.fetched_at) {
    const mins = Math.round((Date.now() / 1000 - data.fetched_at) / 60);
    ageEl.textContent = mins < 1 ? "just fetched" : `fetched ${mins}m ago`;
  }

  // ── Futures row ────────────────────────────────────────────────────────────
  const futuresHtml = (data.futures || []).map(f => `
    <div class="mkt-future-chip">
      <span class="mkt-chip-label">${f.label}</span>
      <span class="mkt-chip-price">${f.price != null ? f.price.toLocaleString() : "—"}</span>
      <span class="mkt-chip-pct ${pctCls(f.change_pct)}">${pctStr(f.change_pct)}</span>
    </div>`).join("");

  // ── VIX ───────────────────────────────────────────────────────────────────
  const vix = data.vix;
  const vixCls = vix ? (vix.price > 25 ? "fail" : vix.price > 18 ? "warn" : "pass") : "";
  const vixHtml = vix ? `
    <div class="mkt-future-chip">
      <span class="mkt-chip-label">VIX</span>
      <span class="mkt-chip-price ${vixCls}">${vix.price.toFixed(2)}</span>
      <span class="mkt-chip-pct ${pctCls(vix.change_pct)}">${pctStr(vix.change_pct)}</span>
    </div>` : "";

  // ── Sector bars ───────────────────────────────────────────────────────────
  const sectors = data.sectors || [];
  const maxAbs  = Math.max(0.1, ...sectors.map(s => Math.abs(s.change_pct ?? 0)));
  const sectorHtml = sectors.map(s => {
    const pct  = s.change_pct;
    const cls  = pctCls(pct);
    const barW = pct != null ? Math.round(Math.abs(pct) / maxAbs * 100) : 0;
    return `
      <div class="mkt-sector-row" title="${s.ticker}: ${pctStr(pct)}">
        <span class="mkt-sector-label">${s.label}</span>
        <div class="mkt-sector-bar-wrap">
          <div class="mkt-sector-bar ${cls}" style="width:${barW}%"></div>
        </div>
        <span class="mkt-sector-pct ${cls}">${pctStr(pct)}</span>
      </div>`;
  }).join("");

  el.innerHTML = `
    <div class="mkt-ctx-futures">${futuresHtml}${vixHtml}</div>
    <div class="mkt-ctx-sectors">${sectorHtml}</div>`;
}

async function loadMarketContext() {
  const el = document.getElementById("mkt-ctx-body");
  if (!el) return;
  try {
    const res  = await fetch("/api/market-context");
    const data = await res.json();
    if (data.ok) renderMarketContext(data);
    else el.innerHTML = `<p class="muted hint">Market data unavailable: ${data.error}</p>`;
  } catch (e) {
    if (el) el.innerHTML = `<p class="muted hint">Market data fetch failed.</p>`;
  }
}

// ── Event listeners ───────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  // Market context panel
  if (document.getElementById("mkt-ctx-panel")) {
    loadMarketContext();
    document.getElementById("mkt-ctx-refresh")?.addEventListener("click", () => {
      document.getElementById("mkt-ctx-body").innerHTML = `<p class="muted hint">Refreshing…</p>`;
      loadMarketContext();
    });
  }

  // Live Suggestions panel collapse
  const livePanelToggle = document.getElementById("live-panel-toggle");
  const livePanelBody   = document.getElementById("live-panel-body");
  if (livePanelToggle && livePanelBody) {
    livePanelToggle.addEventListener("click", () => {
      const collapsed = livePanelBody.style.display === "none";
      livePanelBody.style.display = collapsed ? "" : "none";
      livePanelToggle.innerHTML = collapsed ? "&#9660;" : "&#9650;";
    });
  }

  const liveForm = document.getElementById("live-form");

  liveForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const status    = document.getElementById("live-status");
    const liveBtn   = document.getElementById("run-live");
    const resultsEl = document.getElementById("live-results");
    liveBtn.disabled = true;

    const params   = new URLSearchParams(new FormData(liveForm));
    const selected = typeof window.wlGetSelected === "function" ? window.wlGetSelected() : [];
    if (selected.length > 0) params.set("tickers", selected.join(","));
    const tickerList   = selected.length > 0 ? selected : (window.__WATCHLIST__ || []);
    const totalTickers = tickerList.length || WATCHLIST_SIZE;

    // Progress bar
    status.innerHTML = `
      <div class="scan-progress">
        <div class="scan-progress-label">
          <span class="scan-spinner"></span>
          <span id="scan-progress-text">Starting scan…</span>
        </div>
        <div class="scan-progress-track">
          <div class="scan-progress-bar" id="scan-progress-bar" style="width:0%"></div>
        </div>
      </div>`;

    const progressBar  = document.getElementById("scan-progress-bar");
    const progressText = document.getElementById("scan-progress-text");

    // Immediately render placeholder cards for all tickers
    const placeholderCards = tickerList.map(t =>
      `<div class="ticker-card ticker-card-pending" data-ticker="${t}">
        <div class="tc-header">
          <span class="tc-ticker">
            <span class="scan-spinner tc-pending-spinner"></span>
            ${t}
          </span>
          <div class="tc-badges"></div>
        </div>
      </div>`
    ).join("");

    resultsEl.innerHTML = `
      <section class="panel top-trades">
        <h3>Top 3 Suggested Trades</h3>
        <div class="top-trades-placeholder">
          <span class="scan-spinner"></span>
          <span>Scanning watchlist — top trades will appear here when complete…</span>
        </div>
      </section>
      ${placeholderCards}`;

    const allRows = [];
    const es = new EventSource("/api/analyze?" + params.toString());

    es.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type === "ticker") {
        allRows.push(msg.row);
        const done = allRows.length;
        const pct  = totalTickers > 0 ? Math.round((done / totalTickers) * 100) : 0;
        progressBar.style.width = pct + "%";
        progressText.textContent = `Scanning ${done} / ${totalTickers} — ${msg.row.ticker} ✓`;

        // Replace placeholder card for this ticker with real collapsed card
        const placeholder = resultsEl.querySelector(`.ticker-card[data-ticker="${msg.row.ticker}"]`);
        if (placeholder) {
          // Build real card, then insert it collapsed
          const tempDiv = document.createElement("div");
          // Build a minimal rows array with just this ticker to reuse renderTickerSection logic
          // but we directly build the card via buildTickerCard and collapse it
          const row = msg.row;
          const st = row.status || "";
          const isHardSkip = st.startsWith("SKIP") || st.startsWith("TIMEOUT") || st.startsWith("ERROR") || st.startsWith("No price") || st.startsWith("No option");
          const hasRec = row.candidates && row.candidates.some(c => c.recommended);

          if (isHardSkip) {
            // Earnings blackout, timeout, data error — show dimmed label only
            placeholder.classList.remove("ticker-card-pending");
            placeholder.classList.add("ticker-card-skip");
            placeholder.querySelector(".tc-pending-spinner")?.remove();
            placeholder.querySelector(".tc-header").insertAdjacentHTML("beforeend",
              `<span class="tc-skip-reason">${st}</span>`);
          } else {
            // Has market data (even if no recommended trade) — render full collapsed card
            tempDiv.innerHTML = buildTickerCard(row);
            const card = tempDiv.firstElementChild;
            // Mark "no trade" cards visually
            if (!hasRec) card.classList.add("ticker-card-notrade");
            // Start collapsed
            const collapsible = card.querySelector(".tc-collapsible");
            if (collapsible) collapsible.style.display = "none";
            const btn = card.querySelector(".tc-collapse-btn");
            if (btn) btn.textContent = "▼";
            placeholder.replaceWith(card);
          }
        }
      } else if (msg.type === "done") {
        es.close();
        progressBar.style.width = "100%";
        status.innerHTML = `<span class="scan-done">✓ Done — ${msg.total} tickers scanned.</span>`;

        // Render final top trades and refresh with complete data
        _liveData = { rows: allRows, top_trades: msg.top_trades };
        const topTradesHtml = renderTopTrades(msg.top_trades);
        const existingTop = resultsEl.querySelector(".top-trades");
        if (existingTop) {
          existingTop.insertAdjacentHTML("afterend", topTradesHtml || "");
          existingTop.remove();
        } else if (topTradesHtml) {
          resultsEl.insertAdjacentHTML("afterbegin", topTradesHtml);
        }

        // Remove any remaining pending placeholders (tickers that returned no data)
        resultsEl.querySelectorAll(".ticker-card-pending").forEach(el => el.remove());

        liveBtn.disabled = false;
      }
    };

    es.onerror = () => {
      es.close();
      status.innerHTML = `<span class="fail">Stream error — check server.</span>`;
      liveBtn.disabled = false;
    };
  });

  // News modal
  document.getElementById("live-results").addEventListener("click", (e) => {
    const newsLink = e.target.closest(".news-link");
    if (newsLink) {
      e.preventDefault();
      const ticker    = newsLink.dataset.ticker;
      const headlines = JSON.parse(newsLink.dataset.headlines || "[]");
      const bullish   = parseInt(newsLink.dataset.bullish) || 0;
      const bearish   = parseInt(newsLink.dataset.bearish) || 0;
      const sentiment = newsLink.dataset.sentiment;
      const sentCls   = { Bullish: "pass", Bearish: "fail", Mixed: "warn", Neutral: "na" }[sentiment] ?? "na";
      const rows = headlines.map(h => {
        const w = new Set(h.toLowerCase().replace(/[,']/g,"").split(" "));
        const BULL = new Set(["upgrade","beat","beats","surges","surge","rally","raises","record","growth","strong","outperform","bullish","breakout","positive","profit","gains","gain","exceeds","jumps","soars","buy"]);
        const BEAR = new Set(["downgrade","miss","misses","falls","fall","cut","cuts","warning","lawsuit","layoff","layoffs","loss","losses","decline","declines","weak","underperform","bearish","concern","concerns","negative","recall","slump","drops","drop","disappoints","disappointing"]);
        const isBull = [...w].some(x => BULL.has(x));
        const isBear = [...w].some(x => BEAR.has(x));
        const cls  = isBull ? "pass" : isBear ? "fail" : "na";
        const label = isBull ? "Bullish" : isBear ? "Bearish" : "Neutral";
        return `<tr><td>${h}</td><td class="${cls}">${label}</td></tr>`;
      }).join("");
      document.getElementById("news-modal-title").textContent = `${ticker} — News Headlines`;
      document.getElementById("news-modal-summary").innerHTML =
        `Sentiment: <strong class="${sentCls}">${sentiment}</strong> &nbsp;|&nbsp; Bullish: <strong>${bullish}</strong> &nbsp;|&nbsp; Bearish: <strong>${bearish}</strong>`;
      document.getElementById("news-modal-rows").innerHTML = rows || "<tr><td colspan='2'>No headlines available.</td></tr>";
      document.getElementById("news-modal").style.display = "flex";
    }
  });

  document.getElementById("news-modal-close").addEventListener("click", () => {
    document.getElementById("news-modal").style.display = "none";
  });
  document.getElementById("news-modal").addEventListener("click", (e) => {
    if (e.target === document.getElementById("news-modal"))
      document.getElementById("news-modal").style.display = "none";
  });

  // Delegated: alt structure tabs, collapse, top3 tabs, sort bar
  document.getElementById("live-results").addEventListener("click", async (e) => {
    // Ticker card collapse toggle
    const collapseBtn = e.target.closest(".tc-collapse-btn");
    if (collapseBtn) {
      const card       = collapseBtn.closest(".ticker-card");
      const collapsible = card.querySelector(".tc-collapsible");
      const collapsed  = collapsible.style.display === "none";
      collapsible.style.display = collapsed ? "" : "none";
      collapseBtn.textContent   = collapsed ? "▲" : "▼";
      return;
    }

    // Monte Carlo + Kelly sizing — lazy-loaded on click, not run eagerly
    // for every candidate on every ticker
    const mcBtn = e.target.closest(".tc-mc-btn");
    if (mcBtn) {
      const resultEl = mcBtn.nextElementSibling;
      mcBtn.disabled = true;
      mcBtn.textContent = "Running simulation…";
      try {
        const row = JSON.parse(mcBtn.dataset.row);
        const candidate = JSON.parse(mcBtn.dataset.candidate);
        const res = await fetch("/api/candidate/enrich", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ row, candidate }),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || "Failed to enrich candidate");
        resultEl.innerHTML = buildMonteCarloResult(data.monte_carlo, data.kelly_fraction);
        mcBtn.remove();
      } catch (err) {
        resultEl.innerHTML = `<p class="fail">⚠ ${escHtml(err.message || String(err))}</p>`;
        mcBtn.disabled = false;
        mcBtn.textContent = "📊 Monte Carlo & Sizing";
      }
      return;
    }

    // Alt structure tabs (new tab-style)
    const altBtn = e.target.closest(".tc-alt-btn");
    if (altBtn) {
      const card  = altBtn.closest(".ticker-card");
      const altId = altBtn.dataset.alt;
      const isOpen = altBtn.classList.contains("active");
      card.querySelectorAll(".tc-alt-btn").forEach(b => b.classList.remove("active"));
      card.querySelectorAll(".tc-alt-panel").forEach(p => p.classList.remove("active"));
      if (!isOpen) {
        altBtn.classList.add("active");
        const panel = card.querySelector(`.tc-alt-panel[data-alt="${altId}"]`);
        if (panel) panel.classList.add("active");
      }
      return;
    }

    // Top-3 tab switching
    const top3Tab = e.target.closest(".top3-tab-btn");
    if (top3Tab) {
      const section = top3Tab.closest(".top-trades");
      const idx = top3Tab.dataset.top3;
      section.querySelectorAll(".top3-tab-btn").forEach(b => b.classList.toggle("active", b === top3Tab));
      section.querySelectorAll(".top3-panel").forEach(p => p.classList.toggle("active", p.dataset.top3 === idx));
      return;
    }

    // Exclude-from-top-3 toggle
    const excludeChk = e.target.closest(".exclude-chk");
    if (excludeChk) {
      const key = excludeChk.dataset.key;
      if (excludeChk.checked) _excluded.add(key); else _excluded.delete(key);
      _refetchTopTrades();
      return;
    }

    // Log Trade (ticker card — data-trade JSON)
    const logBtn = e.target.closest(".btn-log-trade");
    if (logBtn && logBtn.dataset.trade) {
      try {
        const tradeData = JSON.parse(logBtn.dataset.trade.replace(/&quot;/g, '"'));
        if (typeof openLogModal === "function") openLogModal(tradeData);
      } catch (err) { console.error("Log trade parse error:", err); }
      return;
    }

    // Log Trade (top-3: individual data- attributes)
    const topLogBtn = e.target.closest(".log-trade-btn");
    if (topLogBtn && !topLogBtn.dataset.trade) {
      const d = topLogBtn.dataset;
      const tradeData = {
        ticker:           d.ticker,
        structure:        d.structure,
        expiry:           d.expiry,
        entry_value:      parseFloat(d.entry_value) || 0,
        max_profit:       d.max_profit  ? parseFloat(d.max_profit)  : null,
        max_loss:         d.max_loss    ? parseFloat(d.max_loss)    : null,
        capital_required: d.capital_required ? parseFloat(d.capital_required) : null,
        is_credit:        d.is_credit === "true",
        details:          d.details,
        net_delta:        d.net_delta ? parseFloat(d.net_delta) : null,
        net_theta:        d.net_theta ? parseFloat(d.net_theta) : null,
      };
      if (typeof openLogModal === "function") openLogModal(tradeData);
      return;
    }

    // Sort bar
    const sortBtn = e.target.closest(".sort-btn");
    if (sortBtn) {
      const key = sortBtn.dataset.sort;
      if (key === _sortKey) {
        _sortDir *= -1;
      } else {
        _sortKey = key;
        _sortDir = (key === "capital" || key === "ticker") ? 1 : -1;
      }
      reRenderTickers();
    }
  });
});
