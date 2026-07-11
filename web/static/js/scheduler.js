// job_key → { label, api for "Run Now", apscheduler_id (null = manual-only), group }
const JOB_META = {
  morning_scan:     { label: "Morning Scan",                   api: "/api/paper-trades/morning-scan",     sched_id: "morning_scan",  group: "Trading" },
  evening_check:    { label: "Evening Check",                  api: "/api/paper-trades/evening-check",    sched_id: "evening_check", group: "Trading" },
  training_collect: { label: "Data Collect (Snapshots)",       api: "/api/training-data/collect",         sched_id: "collect",       group: "Trading" },
  oi_open:          { label: "OI Snapshot — Open",             api: "/api/archive/run",                   sched_id: "oi_open",       group: "Flywheel", api_body: {job:"oi", time_of_day:"open"} },
  oi_close:         { label: "OI Snapshot — Close",            api: "/api/archive/run",                   sched_id: "oi_close",      group: "Flywheel", api_body: {job:"oi", time_of_day:"close"} },
  daily_archive:    { label: "Daily Archive (Bars/VIX/Earnings)", api: "/api/archive/run",                sched_id: "daily_archive", group: "Flywheel", api_body: {job:"all"} },
  regime_backfill:  { label: "Regime Backfill",                api: "/api/training-data/backfill-regime", sched_id: null,            group: "ML" },
  train_models:     { label: "Train ML Models",                api: "/api/training-data/train-models",    sched_id: null,            group: "ML" },
};

const JOB_LABELS = {
  morning_scan:     "Morning Scan",
  evening_check:    "Evening Check",
  training_collect: "Data Collect",
  oi_open:          "OI Snapshot — Open",
  oi_close:         "OI Snapshot — Close",
  daily_archive:    "Daily Archive",
  regime_backfill:  "Regime Backfill",
  train_models:     "Train ML Models",
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
    const nextHuman = schedJob ? (schedJob.next_run_human || "—") : "—";
    const dur       = s.age_min != null ? `${s.age_min}m ago` : "—";
    const errTip    = s.error ? ` title="${s.error.replace(/"/g,"'").slice(0,200)}"` : "";

    const pauseBtn = meta.sched_id
      ? `<button class="btn-sm pause-btn" data-job-id="${meta.sched_id}" data-paused="${paused}"
           style="margin-left:.4rem">${paused ? "Resume" : "Pause"}</button>`
      : "";

    const tr = document.createElement("tr");
    tr.dataset.apiBody = meta.api_body ? JSON.stringify(meta.api_body) : "";
    tr.innerHTML = `
      <td><strong>${meta.label}</strong>${paused ? ' <span class="muted" style="font-size:.8rem">(paused)</span>' : ""}</td>
      <td class="muted">${nextHuman}</td>
      <td class="${stateCls(s.state)}"${errTip}>${stateLabel(s.state)}</td>
      <td class="muted">${dur}</td>
      <td>
        <button class="btn-sm run-btn" data-api="${meta.api}" data-label="${meta.label}">Run Now</button>
        ${pauseBtn}
      </td>
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

async function loadLogs() {
  const filter = document.getElementById("log-filter").value;
  const url    = "/api/scheduler/logs" + (filter ? "?job=" + encodeURIComponent(filter) : "");
  let data;
  try {
    const r = await fetch(url);
    data = await r.json();
  } catch(e) {
    document.getElementById("log-tbody").innerHTML =
      `<tr><td colspan="5" class="state-error">Error loading logs: ${e.message}</td></tr>`;
    return;
  }
  const logs  = data.logs || [];
  const tbody = document.getElementById("log-tbody");
  document.getElementById("log-count").textContent = `${Math.min(logs.length, 50)} of ${logs.length} entries`;
  if (!logs.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">No runs recorded yet — history accumulates after jobs fire.</td></tr>`;
    return;
  }
  tbody.innerHTML = logs.slice(0, 50).map(e => {
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
      : `<span class="muted">${(e.summary || "").slice(0, 120)}</span>`;
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

// ── Model Audit ───────────────────────────────────────────────────────────────

function renderCurveSVG(curves, label, W = 200, H = 140) {
  const PAD = 24;
  const iW = W - PAD * 2, iH = H - PAD * 2;

  const toSVG = (xv, yv) => ({ sx: PAD + xv * iW, sy: PAD + (1 - yv) * iH });

  const polyline = function(pts, color, dash) {
    if (pts.length < 2) return "";
    var pts_str = pts.map(function(p) { return p.sx.toFixed(1) + "," + p.sy.toFixed(1); }).join(" ");
    var dash_attr = dash ? " stroke-dasharray=\"" + dash + "\"" : "";
    return "<polyline points=\"" + pts_str + "\" fill=\"none\" stroke=\"" + color + "\" stroke-width=\"1.8\" stroke-linejoin=\"round\" stroke-linecap=\"round\"" + dash_attr + " />";
  };

  const dots = (pts, color) =>
    pts.map(p => `<circle cx="${p.sx.toFixed(1)}" cy="${p.sy.toFixed(1)}" r="2.5" fill="${color}"/>`).join("");

  const diag = `<line x1="${PAD}" y1="${PAD+iH}" x2="${PAD+iW}" y2="${PAD}"
    stroke="currentColor" stroke-width="1" opacity=".25" stroke-dasharray="4,3"/>`;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map(v => {
    const x = PAD + v * iW, y = PAD + (1 - v) * iH;
    return `<line x1="${x}" y1="${PAD+iH}" x2="${x}" y2="${PAD+iH+3}" stroke="currentColor" opacity=".3" stroke-width="1"/>
            <line x1="${PAD-3}" y1="${y}" x2="${PAD}" y2="${y}" stroke="currentColor" opacity=".3" stroke-width="1"/>`;
  }).join("");

  const axis = `<rect x="${PAD}" y="${PAD}" width="${iW}" height="${iH}"
    fill="none" stroke="currentColor" opacity=".15" stroke-width="1"/>`;

  const rawPts = (curves.raw?.x || []).map((x, i) => toSVG(x, curves.raw.y[i]));
  const calPts = curves.calibrated ? (curves.calibrated.x || []).map((x, i) => toSVG(x, curves.calibrated.y[i])) : [];

  const rawColor = "var(--text-muted, #666)";
  const calColor = "var(--pass, #4ade80)";

  const xLabel = `<text x="${PAD+iW/2}" y="${H-2}" text-anchor="middle" font-size="8" fill="currentColor" opacity=".4">Predicted</text>`;
  const yLabel = `<text x="6" y="${PAD+iH/2}" text-anchor="middle" font-size="8" fill="currentColor" opacity=".4"
    transform="rotate(-90,6,${PAD+iH/2})">Actual</text>`;

  const legend = calPts.length
    ? `<line x1="${PAD+2}" y1="${PAD-8}" x2="${PAD+12}" y2="${PAD-8}" stroke="${rawColor}" stroke-width="1.5" stroke-dasharray="3,2"/>
       <text x="${PAD+14}" y="${PAD-5}" font-size="7.5" fill="currentColor" opacity=".55">raw</text>
       <line x1="${PAD+38}" y1="${PAD-8}" x2="${PAD+48}" y2="${PAD-8}" stroke="${calColor}" stroke-width="1.5"/>
       <text x="${PAD+50}" y="${PAD-5}" font-size="7.5" fill="currentColor" opacity=".55">calibrated</text>`
    : `<line x1="${PAD+2}" y1="${PAD-8}" x2="${PAD+12}" y2="${PAD-8}" stroke="${rawColor}" stroke-width="1.5"/>
       <text x="${PAD+14}" y="${PAD-5}" font-size="7.5" fill="currentColor" opacity=".55">raw</text>`;

  return `<svg viewBox="0 0 ${W} ${H}" class="audit-svg" xmlns="http://www.w3.org/2000/svg">
    ${axis}${ticks}${diag}
    ${polyline(rawPts, rawColor, "4,3")}${dots(rawPts, rawColor)}
    ${calPts.length ? polyline(calPts, calColor) + dots(calPts, calColor) : ""}
    ${xLabel}${yLabel}${legend}
  </svg>`;
}

function renderAuditCard(name, r) {
  const label = AUDIT_MODEL_LABELS[name] || name;
  if (!r.ok) {
    return `<div class="audit-card">
      <div class="audit-card-title">${label}</div>
      <div class="audit-skip">${r.error || "unavailable"}</div>
    </div>`;
  }

  const br = r.brier_raw?.toFixed(4) ?? "—";
  const bc = r.brier_calibrated?.toFixed(4) ?? null;

  const brierHTML = `<div class="audit-brier">
    <div class="audit-brier-item">
      <span class="audit-brier-label">Brier (raw)</span>
      <span class="audit-brier-val ${bc ? 'raw' : ''}">${br}</span>
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
  <div class="muted" style="font-size:.75rem;margin-bottom:.4rem">${r.test_rows?.toLocaleString()} test rows · cutoff ${r.split_cutoff || "?"}</div>`;

  let curvesHTML = '<div class="audit-curves-row">';
  if (r.type === "multiclass") {
    curvesHTML += Object.entries(r.curves || {}).map(([cls, cv]) => {
      const svgBig   = renderCurveSVG(cv, cls, 560, 340);
      const svgSmall = renderCurveSVG(cv, cls);
      const titleAttr = `${label} — ${cls}`;
      return `<div class="audit-curve-wrap">
        <div class="audit-curve-label">${cls}</div>
        ${svgSmall}
        <button class="audit-max-btn" onclick="openAuditModal(${JSON.stringify(titleAttr)},${JSON.stringify(svgBig)})">&#x26F6;</button>
      </div>`;
    }).join("");
  } else {
    const svgBig   = renderCurveSVG(r.curve || {}, label, 560, 340);
    const svgSmall = renderCurveSVG(r.curve || {}, label);
    curvesHTML += `<div class="audit-curve-wrap">
      ${svgSmall}
      <button class="audit-max-btn" onclick="openAuditModal(${JSON.stringify(label)},${JSON.stringify(svgBig)})">&#x26F6;</button>
    </div>`;
  }
  curvesHTML += "</div>";

  return `<div class="audit-card">
    <div class="audit-card-title">${label}</div>
    ${brierHTML}
    ${curvesHTML}
  </div>`;
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

function openAuditModal(title, svgHTML) {
  document.getElementById("audit-modal-title").textContent = title;
  document.getElementById("audit-modal-svg-wrap").innerHTML = svgHTML;
  document.getElementById("audit-modal").classList.add("open");
}

function closeAuditModal() {
  document.getElementById("audit-modal").classList.remove("open");
}

document.getElementById("audit-modal-close").addEventListener("click", closeAuditModal);
document.getElementById("audit-modal").addEventListener("click", function(e) {
  if (e.target === this) closeAuditModal();
});
document.addEventListener("keydown", function(e) {
  if (e.key === "Escape") closeAuditModal();
});
