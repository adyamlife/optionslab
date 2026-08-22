// job_key → { label, api for "Run Now", apscheduler_id (null = manual-only), group }
const JOB_META = {
  morning_scan:     { label: "Morning Scan",                   api: "/api/paper-trades/morning-scan",     sched_id: "morning_scan",   group: "Trading" },
  afternoon_scan:   { label: "Afternoon Scan",                 api: "/api/paper-trades/afternoon-scan",   sched_id: "afternoon_scan", group: "Trading" },
  evening_check:    { label: "Evening Check",                  api: "/api/paper-trades/evening-check",    sched_id: "evening_check",  group: "Trading" },
  training_collect: { label: "Data Collect (Snapshots)",       api: "/api/training-data/collect",         sched_id: "collect",        group: "Trading" },
  oi_open:          { label: "OI Snapshot — Open",             api: "/api/archive/run",                   sched_id: "oi_open",       group: "Flywheel", api_body: {job:"oi", time_of_day:"open"} },
  oi_close:         { label: "OI Snapshot — Close",            api: "/api/archive/run",                   sched_id: "oi_close",      group: "Flywheel", api_body: {job:"oi", time_of_day:"close"} },
  daily_archive:    { label: "Daily Archive (Bars/VIX/Earnings)", api: "/api/archive/run",                sched_id: "daily_archive", group: "Flywheel", api_body: {job:"all"} },
  regime_backfill:      { label: "Regime Backfill",                api: "/api/training-data/backfill-regime", sched_id: null, group: "ML" },
  train_models:         { label: "Train ML Models",                api: "/api/training-data/train-models",    sched_id: null, group: "ML" },
  daily_iv_collect:          { label: "Daily IV Collect",               api: null, sched_id: null, group: "Layer B", schedule: "Mon–Fri 4:15 PM ET (cron)" },
  weekly_profile_build:      { label: "Weekly Profile Build",           api: null, sched_id: null, group: "Layer B", schedule: "Sun 8:00 PM ET (cron)" },
  weekly_calibration:        { label: "Weekly Calibration Check",       api: null, sched_id: null, group: "Layer B", schedule: "Sun 9:00 PM ET (cron)" },
  forecast_collect:          { label: "Ticker Forecast Collect",        api: "/api/scheduler/run-forecast-collect", sched_id: "forecast_collect", group: "Forecast", schedule: "Mon–Fri after scan (cron)" },
  forecast_calibration:      { label: "Forecast Calibration",           api: null, sched_id: null, group: "Forecast", schedule: "Sat/Sun 8:00 AM ET (cron)" },
};

const JOB_LABELS = {
  morning_scan:         "Morning Scan",
  afternoon_scan:       "Afternoon Scan",
  evening_check:        "Evening Check",
  training_collect:     "Data Collect",
  oi_open:              "OI Snapshot — Open",
  oi_close:             "OI Snapshot — Close",
  daily_archive:        "Daily Archive",
  regime_backfill:      "Regime Backfill",
  train_models:         "Train ML Models",
  daily_iv_collect:          "Daily IV Collect",
  weekly_profile_build:      "Weekly Profile Build",
  weekly_calibration:        "Weekly Calibration Check",
  forecast_collect:          "Ticker Forecast Collect",
  forecast_calibration:      "Forecast Calibration",
};

const AUDIT_MODEL_LABELS = {
  regime_classifier:       "Regime Classifier",
  direction_classifier:    "Direction Classifier",
  iv_direction_classifier: "IV Direction Classifier",
  meta_ensemble:           "Meta-Ensemble",
  pop_classifier:          "POP Classifier",
};

// ── Utilities ─────────────────────────────────────────────────────────────────

function fmt(n) { return n != null ? n.toLocaleString() : "—"; }

function stateCls(state) {
  if (state === "running") return "state-running";
  if (state === "done")    return "state-done";
  if (state === "error")   return "state-error";
  return "state-idle";
}

function stateLabel(state) {
  if (state === "running") return "⟳ Running";
  if (state === "done")    return "✓ Done";
  if (state === "error")   return "✗ Error";
  return "Idle";
}

// ── Scheduler status ──────────────────────────────────────────────────────────

async function runJob(api, label, btn, body) {
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    const opts = { method: "POST" };
    if (body) { opts.headers = {"Content-Type":"application/json"}; opts.body = JSON.stringify(body); }
    const r = await fetch(api, opts);
    const d = await r.json();
    btn.textContent = d.ok || d.running ? "Started" : "Failed";
    setTimeout(() => { btn.textContent = "Run Now"; btn.disabled = false; loadStatus(); }, 3000);
  } catch(e) {
    btn.textContent = "Error";
    setTimeout(() => { btn.textContent = "Run Now"; btn.disabled = false; }, 3000);
  }
}

async function loadStatus() {
  let data;
  try {
    const r = await fetch("/api/scheduler/status");
    data = await r.json();
  } catch(e) {
    document.getElementById("sched-error").style.display = "";
    document.getElementById("sched-error").textContent = "Failed to load scheduler status: " + e.message;
    return;
  }
  document.getElementById("sched-error").style.display = "none";

  const db = data.db || {};
  document.getElementById("db-regime").textContent  = fmt(db.regime_rows);
  document.getElementById("db-snaps").textContent   = fmt(db.snapshots);
  document.getElementById("db-labeled").textContent = fmt(db.labeled);
  document.getElementById("db-chain").textContent   = fmt(db.chain_snaps);

  const cq = data.collect_quality || {};
  if (cq.total != null) {
    const pct = cq.total > 0 ? Math.round(cq.collected / cq.total * 100) : 0;
    const color = pct >= 90 ? "var(--success,#4c4)" : pct >= 60 ? "var(--warn,#fa0)" : "var(--danger,#e55)";
    const el = document.getElementById("cq-summary");
    el.textContent = `${cq.collected} / ${cq.total} tickers (${pct}%)`;
    el.style.color = color;
    const errRow = document.getElementById("cq-error-row");
    if (cq.errors > 0 && cq.first_error) {
      errRow.style.display = "";
      document.getElementById("cq-first-error").textContent =
        `${cq.first_error.ticker}: ${cq.first_error.error}`;
    } else {
      errRow.style.display = "none";
    }
  } else {
    document.getElementById("cq-summary").textContent = "—";
  }

  const ml = data.ml_cache || {};
  const warmEl = document.getElementById("ml-warm");
  warmEl.textContent = ml.warm ? "Warm" : "Cold";
  warmEl.className = "sched-val " + (ml.warm ? "state-done" : "state-error");
  document.getElementById("ml-size").textContent = fmt(ml.size);
  document.getElementById("ml-age").textContent  = ml.age_human || "—";

  const cfg = data.scheduler_cfg || {};
  if (cfg.collect_interval_minutes) {
    document.getElementById("sched-cfg-info").textContent =
      `Interval: ${cfg.collect_interval_minutes}m  |  Window: ${cfg.collect_hour_start}:00 – ${cfg.collect_hour_end}:00 ET`;
  }

  const jobs     = data.scheduler_jobs || {};
  const statuses = data.job_status     || {};
  const tbody    = document.getElementById("sched-jobs-body");
  tbody.innerHTML = "";

  let lastGroup = null;
  for (const [key, meta] of Object.entries(JOB_META)) {
    if (meta.group !== lastGroup) {
      lastGroup = meta.group;
      const gh = document.createElement("tr");
      gh.innerHTML = `<td colspan="5" style="padding:.3rem .6rem .15rem;font-size:.75rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--text-muted);background:var(--surface2,#111)">${meta.group}</td>`;
      tbody.appendChild(gh);
    }

    const s         = statuses[key] || { state: "idle" };
    const schedJob  = meta.sched_id ? jobs[meta.sched_id] : null;
    const paused    = schedJob && schedJob.next_run_human === "paused";
    const nextHuman = schedJob ? (schedJob.next_run_human || "—") : (meta.schedule || "—");
    const dur       = s.age_min != null ? `${s.age_min}m ago` : "—";
    const errTip    = s.error ? ` title="${s.error.replace(/"/g,"'").slice(0,200)}"` : "";
    const isCron    = !meta.sched_id && !meta.api;

    const pauseBtn = meta.sched_id
      ? `<button class="btn-sm pause-btn" data-job-id="${meta.sched_id}" data-paused="${paused}"
           style="margin-left:.4rem">${paused ? "Resume" : "Pause"}</button>`
      : "";

    const actionCell = isCron
      ? `<span class="muted" style="font-size:.78rem">Ubuntu cron</span>`
      : `<button class="btn-sm run-btn" data-api="${meta.api}" data-label="${meta.label}">Run Now</button>${pauseBtn}`;

    const tr = document.createElement("tr");
    tr.dataset.apiBody = meta.api_body ? JSON.stringify(meta.api_body) : "";
    tr.innerHTML = `
      <td><strong>${meta.label}</strong>${paused ? ' <span class="muted" style="font-size:.8rem">(paused)</span>' : ""}</td>
      <td class="muted">${nextHuman}</td>
      <td class="${isCron ? "state-idle" : stateCls(s.state)}"${errTip}>${isCron ? "—" : stateLabel(s.state)}</td>
      <td class="muted">${isCron ? "—" : dur}</td>
      <td>${actionCell}</td>
    `;
    tbody.appendChild(tr);
  }

  tbody.querySelectorAll(".run-btn").forEach(btn => {
    const row  = btn.closest("tr");
    const body = row && row.dataset.apiBody ? JSON.parse(row.dataset.apiBody) : undefined;
    btn.addEventListener("click", () => runJob(btn.dataset.api, btn.dataset.label, btn, body));
  });

  tbody.querySelectorAll(".pause-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const jobId  = btn.dataset.jobId;
      const paused = btn.dataset.paused === "true";
      btn.disabled = true;
      const r = await fetch(`/api/scheduler/${paused ? "resume" : "pause"}/${jobId}`, { method: "POST" });
      const d = await r.json();
      if (d.ok) { setTimeout(loadStatus, 400); }
      else { btn.disabled = false; alert(d.error); }
    });
  });

  document.getElementById("last-updated").textContent =
    "Last updated: " + new Date().toLocaleTimeString();

  try {
    const ar = await fetch("/api/archive/status");
    const ad = await ar.json();
    if (ad.ok) {
      const c = ad.counts || {};
      document.getElementById("arc-bars").textContent = fmt(c.intraday_bars);
      document.getElementById("arc-vix").textContent  = fmt(c.vix_term_structure);
      document.getElementById("arc-oi").textContent   = fmt(c.oi_changes);
      document.getElementById("arc-earn").textContent = fmt(c.earnings_iv_tracker);
    }
  } catch(e) { /* non-fatal */ }

  const lb = data.layer_b || {};
  if (!lb.error) {
    document.getElementById("lb-iv-rows").textContent    = fmt(lb.iv_rows);
    document.getElementById("lb-iv-tickers").textContent = fmt(lb.iv_tickers);
    document.getElementById("lb-iv-date").textContent    = lb.iv_last || "—";
    document.getElementById("lb-profiles").textContent   = fmt(lb.profile_tickers);
    document.getElementById("lb-profile-date").textContent = lb.profile_last || "—";
    const wk = lb.calib_weeks ?? 0;
    const rem = lb.phase3_weeks_remaining ?? (20 - wk);
    document.getElementById("lb-calib-weeks").textContent = `${wk} / 20`;
    document.getElementById("lb-phase3-inline").textContent = rem > 0 ? rem : "✓ Ready";
  } else {
    ["lb-iv-rows","lb-iv-tickers","lb-iv-date","lb-profiles","lb-profile-date","lb-calib-weeks"].forEach(id => {
      document.getElementById(id).textContent = "—";
    });
    document.getElementById("lb-phase3-inline").textContent = "—";
  }

  // Forecast calibration stats
  const fc = data.forecast || {};
  if (!fc.error) {
    document.getElementById("fc-log-rows").textContent = fmt(fc.log_rows);
    document.getElementById("fc-val-rows").textContent = fmt(fc.val_rows);
    document.getElementById("fc-cov80").textContent    = fc.cov80  != null ? (fc.cov80 * 100).toFixed(1) + "%" : "—";
    document.getElementById("fc-cov50").textContent    = fc.cov50  != null ? (fc.cov50 * 100).toFixed(1) + "%" : "—";
    const bias = fc.bias_med;
    const biasEl = document.getElementById("fc-bias");
    biasEl.textContent = bias != null ? (bias >= 0 ? "+" : "") + (bias * 100).toFixed(2) + "%" : "—";
    biasEl.style.color = bias == null ? "" : bias > 0.02 ? "var(--danger,#e55)" : bias < -0.02 ? "var(--danger,#e55)" : "var(--success,#4c8)";
    document.getElementById("fc-vol-adj").textContent = fc.vol_adj_factor != null ? fc.vol_adj_factor.toFixed(2) : "—";
  } else {
    ["fc-log-rows","fc-val-rows","fc-cov80","fc-cov50","fc-bias","fc-vol-adj"].forEach(id => {
      document.getElementById(id).textContent = "—";
    });
  }
}

document.getElementById("ml-refresh-btn").addEventListener("click", async function() {
  this.disabled = true;
  const status = document.getElementById("ml-refresh-status");
  status.textContent = "Refreshing… (this takes ~30s)";
  try {
    const r = await fetch("/api/ml/cache/refresh", { method: "POST" });
    const d = await r.json();
    status.textContent = d.ok ? `Done — ${d.size || ""} tickers cached` : `Error: ${d.error}`;
  } catch(e) {
    status.textContent = "Error: " + e.message;
  }
  this.disabled = false;
  loadStatus();
});

document.getElementById("refresh-btn").addEventListener("click", loadStatus);

let autoTimer;
function scheduleAuto() {
  clearInterval(autoTimer);
  if (document.getElementById("auto-refresh").checked) {
    autoTimer = setInterval(loadStatus, 30000);
  }
}
document.getElementById("auto-refresh").addEventListener("change", scheduleAuto);

loadStatus();
scheduleAuto();

// ── Run History Log ───────────────────────────────────────────────────────────

const _TRADING_JOBS = new Set(["morning_scan", "afternoon_scan", "evening_check"]);

async function loadLogs() {
  const filter  = document.getElementById("log-filter").value;
  const trading = filter === "__trading__";
  const url     = "/api/scheduler/logs" + (!filter || trading ? "" : "?job=" + encodeURIComponent(filter));
  let data;
  try {
    const r = await fetch(url);
    data = await r.json();
  } catch(e) {
    document.getElementById("log-tbody").innerHTML =
      `<tr><td colspan="5" class="state-error">Error loading logs: ${e.message}</td></tr>`;
    return;
  }
  let logs = data.logs || [];
  if (trading) logs = logs.filter(e => _TRADING_JOBS.has(e.job));
  const cap = filter ? 200 : 50;
  const tbody = document.getElementById("log-tbody");
  document.getElementById("log-count").textContent = `${Math.min(logs.length, cap)} of ${logs.length} entries`;
  if (!logs.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">No runs recorded yet — history accumulates after jobs fire.</td></tr>`;
    return;
  }
  tbody.innerHTML = logs.slice(0, cap).map(e => {
    const t = e.ts ? (() => {
      try {
        return new Date(e.ts).toLocaleString("en-US", {
          timeZone: "America/New_York",
          month: "2-digit", day: "2-digit",
          hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
        });
      } catch (_err) { return e.ts.replace("T", " ").slice(0, 19); }
    })() : "—";
    const cls = stateCls(e.state);
    const lbl = JOB_LABELS[e.job] || e.job;
    const dur = e.duration_s != null ? `${e.duration_s}s` : "—";
    const detail = e.state === "error"
      ? `<span class="state-error" title="${(e.trace || "").replace(/"/g,"'").slice(0,500)}">${(e.error || "").slice(0, 120)}</span>`
      : `<span class="muted">${(e.summary && e.summary !== "None" ? e.summary : "").slice(0, 120)}</span>`;
    return `<tr>
      <td class="muted" style="white-space:nowrap;font-size:.82rem">${t}</td>
      <td><strong>${lbl}</strong></td>
      <td class="${cls}">${stateLabel(e.state)}</td>
      <td class="muted">${dur}</td>
      <td style="font-size:.83rem">${detail}</td>
    </tr>`;
  }).join("");
}

document.getElementById("log-filter").addEventListener("change", loadLogs);
document.getElementById("log-refresh-btn").addEventListener("click", loadLogs);
loadLogs();

// ── Log File Viewer ───────────────────────────────────────────────────────────

let _logFileAutoTimer = null;

async function loadLogFile() {
  const file  = document.getElementById("log-file-select").value;
  const lines = document.getElementById("log-lines-select").value;
  const out   = document.getElementById("log-file-output");
  const meta  = document.getElementById("log-file-meta");
  const err   = document.getElementById("log-file-error");
  err.style.display = "none";
  out.textContent = "Loading…";
  try {
    const r = await fetch(`/api/logs?file=${encodeURIComponent(file)}&lines=${lines}`);
    const d = await r.json();
    if (!d.ok) {
      err.textContent = d.error || "Unknown error";
      err.style.display = "";
      out.textContent = "";
      meta.textContent = "";
      return;
    }
    if (!d.exists) {
      out.textContent = "(log file does not exist yet)";
      meta.textContent = "";
      return;
    }
    out.textContent = d.lines.join("");
    meta.textContent = `${d.total_lines} lines shown · ${d.size_kb} KB`;
    // Scroll to bottom
    out.scrollTop = out.scrollHeight;
  } catch(e) {
    err.textContent = "Failed to load log: " + e.message;
    err.style.display = "";
    out.textContent = "";
  }
}

function scheduleLogAuto() {
  clearInterval(_logFileAutoTimer);
  if (document.getElementById("log-auto-refresh").checked) {
    _logFileAutoTimer = setInterval(loadLogFile, 30000);
  }
}

document.getElementById("log-file-select").addEventListener("change", loadLogFile);
document.getElementById("log-lines-select").addEventListener("change", loadLogFile);
document.getElementById("log-file-refresh-btn").addEventListener("click", loadLogFile);
document.getElementById("log-auto-refresh").addEventListener("change", scheduleLogAuto);

// ── Model Audit ───────────────────────────────────────────────────────────────

let _auditCurveStore = {};   // plotId → { curves, label }

function _plotId(name, cls) {
  return "audit-p-" + (name + (cls ? "-" + cls : "")).replace(/[^a-z0-9]/gi, "_");
}

function _plotTraces(curves) {
  const rawX = curves.raw?.x  || [];
  const rawY = curves.raw?.y  || [];
  const calX = curves.calibrated?.x || [];
  const calY = curves.calibrated?.y || [];
  const traces = [
    { name: "Perfect", x: [0, 1], y: [0, 1], mode: "lines",
      line: { color: "rgba(150,150,150,0.3)", dash: "dash", width: 1 },
      showlegend: false, hoverinfo: "skip" },
    { name: "Raw", x: rawX, y: rawY, mode: "lines+markers",
      line: { color: "rgba(150,150,150,0.75)", dash: "dot", width: 2 },
      marker: { size: 4, color: "rgba(150,150,150,0.75)" } },
  ];
  if (calX.length) {
    traces.push({ name: "Calibrated", x: calX, y: calY, mode: "lines+markers",
      line: { color: "#4ade80", width: 2 },
      marker: { size: 4, color: "#4ade80" } });
  }
  return traces;
}

function _plotLayout(small) {
  const grid = "rgba(150,150,150,0.12)";
  const font = { size: small ? 9 : 11, color: "rgba(200,200,200,0.8)" };
  return {
    paper_bgcolor: "transparent", plot_bgcolor: "transparent",
    font,
    margin: small ? { t:4, b:20, l:28, r:4 } : { t:10, b:52, l:52, r:10 },
    xaxis: { range: [0, 1], gridcolor: grid, zeroline: false,
             title: small ? "" : "Mean predicted prob.",
             tickfont: { size: 9 }, showticklabels: true },
    yaxis: { range: [0, 1], gridcolor: grid, zeroline: false,
             title: small ? "" : "Fraction of positives",
             tickfont: { size: 9 } },
    showlegend: !small,
    legend: { orientation: "h", y: -0.22, font: { size: 10 } },
  };
}

const _plotCfg = { responsive: true, displayModeBar: false };

function renderAuditCard(name, r) {
  const label = AUDIT_MODEL_LABELS[name] || name;
  if (!r.ok) {
    return `<div class="audit-card">
      <div class="audit-card-title">${label}</div>
      <div class="audit-skip">${r.error || "unavailable"}</div>
    </div>`;
  }

  const tr  = r.training || {};
  const mi  = r.model_info || {};
  const br  = tr.brier_raw?.toFixed(4) ?? "—";
  const bc  = tr.brier_calibrated?.toFixed(4) ?? null;
  const nRows   = mi.test_rows?.toLocaleString() ?? "?";
  const cutoff  = tr.split_cutoff || tr.meta_cutoff || mi.test_cutoff || "?";

  const brierHTML = `<div class="audit-brier">
    <div class="audit-brier-item">
      <span class="audit-brier-label">Brier (raw)</span>
      <span class="audit-brier-val ${bc ? "raw" : ""}">${br}</span>
    </div>
    ${bc ? `<div class="audit-brier-item">
      <span class="audit-brier-label">Brier (calibrated)</span>
      <span class="audit-brier-val improved">${bc}</span>
    </div>
    <div class="audit-brier-item">
      <span class="audit-brier-label">Improvement</span>
      <span class="audit-brier-val improved">-${(parseFloat(br)-parseFloat(bc)).toFixed(4)}</span>
    </div>` : ""}
  </div>
  <div class="muted" style="font-size:.75rem;margin-bottom:.4rem">${nRows} test rows · cutoff ${String(cutoff).slice(0,10)}</div>`;

  let curvesHTML = '<div class="audit-curves-row">';
  if (r.type === "multiclass") {
    const perClass = tr.per_class_curves || {};
    curvesHTML += Object.entries(perClass).map(([cls]) => {
      const pid = _plotId(name, cls);
      return `<div class="audit-curve-wrap">
        <div class="audit-curve-label">${cls}</div>
        <div id="${pid}" class="audit-plot"></div>
        <button class="audit-max-btn" onclick="openAuditModal(${JSON.stringify(label + ' — ' + cls).replace(/"/g,'&quot;')},${JSON.stringify(pid).replace(/"/g,'&quot;')})">&#x26F6;</button>
      </div>`;
    }).join("");
  } else {
    const pid = _plotId(name, null);
    curvesHTML += `<div class="audit-curve-wrap">
      <div id="${pid}" class="audit-plot"></div>
      <button class="audit-max-btn" onclick="openAuditModal(${JSON.stringify(label).replace(/"/g,'&quot;')},${JSON.stringify(pid).replace(/"/g,'&quot;')})">&#x26F6;</button>
    </div>`;
  }
  curvesHTML += "</div>";

  return `<div class="audit-card">
    <div class="audit-card-title">${label}</div>
    ${brierHTML}
    ${curvesHTML}
  </div>`;
}

function attachAuditCharts(data) {
  Object.keys(_auditCurveStore).forEach(id => { try { Plotly.purge(id); } catch(_) {} });
  _auditCurveStore = {};

  for (const [name, r] of Object.entries(data.models || {})) {
    if (!r.ok) continue;
    const label = AUDIT_MODEL_LABELS[name] || name;
    const tr    = r.training || {};
    if (r.type === "multiclass") {
      for (const [cls, classData] of Object.entries(tr.per_class_curves || {})) {
        const pid = _plotId(name, cls);
        const el  = document.getElementById(pid);
        if (!el) continue;
        const cv = classData.curves_quantile || {};
        _auditCurveStore[pid] = { curves: cv, label: label + " — " + cls };
        Plotly.newPlot(pid, _plotTraces(cv), _plotLayout(true), _plotCfg);
      }
    } else {
      const pid = _plotId(name, null);
      const el  = document.getElementById(pid);
      if (!el) continue;
      const cv = tr.curves_quantile || {};
      _auditCurveStore[pid] = { curves: cv, label };
      Plotly.newPlot(pid, _plotTraces(cv), _plotLayout(true), _plotCfg);
    }
  }
}

async function runAudit() {
  const btn   = document.getElementById("audit-run-btn");
  const body  = document.getElementById("audit-body");
  const errEl = document.getElementById("audit-error");
  const ageEl = document.getElementById("audit-age");

  btn.disabled = true;
  btn.textContent = "Running…";
  errEl.style.display = "none";
  body.innerHTML = `<p class="muted" style="font-size:.85rem">Computing calibration curves — this may take 10–20s…</p>`;

  try {
    const r = await fetch("/api/ml/audit");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();

    if (data.error) {
      errEl.textContent = data.error;
      errEl.style.display = "";
      body.innerHTML = "";
      return;
    }

    const ts = data.generated_at
      ? new Date(data.generated_at).toLocaleTimeString("en-US", {hour:"2-digit", minute:"2-digit"})
      : "";
    ageEl.textContent = ts ? `Generated at ${ts}` : "";

    const cards = Object.entries(data.models || {})
      .map(([name, r]) => renderAuditCard(name, r))
      .join("");
    body.innerHTML = `<div class="audit-grid">${cards}</div>`;
    attachAuditCharts(data);

  } catch(e) {
    errEl.textContent = "Audit failed: " + e.message;
    errEl.style.display = "";
    body.innerHTML = "";
  } finally {
    btn.textContent = "Run Audit";
    btn.disabled = false;
  }
}

document.getElementById("audit-run-btn").addEventListener("click", runAudit);

// ── Audit chart modal ─────────────────────────────────────────────────────────

function openAuditModal(title, sourcePlotId) {
  const stored = _auditCurveStore[sourcePlotId];
  if (!stored) return;
  document.getElementById("audit-modal-title").textContent = title;
  Plotly.newPlot("audit-modal-plot", _plotTraces(stored.curves), _plotLayout(false), _plotCfg);
  document.getElementById("audit-modal").classList.add("open");
}

function closeAuditModal() {
  document.getElementById("audit-modal").classList.remove("open");
  try { Plotly.purge("audit-modal-plot"); } catch(_) {}
}

document.getElementById("audit-modal-close").addEventListener("click", closeAuditModal);
document.getElementById("audit-modal").addEventListener("click", function(e) {
  if (e.target === this) closeAuditModal();
});
document.addEventListener("keydown", function(e) {
  if (e.key === "Escape") closeAuditModal();
});
