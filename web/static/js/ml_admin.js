function escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function postJson(url) {
  const res = await fetch(url, { method: "POST" });
  return res.json();
}

async function getJson(url) {
  const res = await fetch(url);
  return res.json();
}

function renderJsonBlock(el, data) {
  el.innerHTML = `<pre class="ml-admin-json">${escHtml(JSON.stringify(data, null, 2))}</pre>`;
}

async function pollStatus(statusUrl, statusEl, resultEl, onDone) {
  const tick = async () => {
    let data;
    try {
      data = await getJson(statusUrl);
    } catch (err) {
      statusEl.textContent = `Error checking status: ${err.message || err}`;
      return;
    }
    const state = data.state || "idle";
    statusEl.textContent = `Status: ${state}`;
    if (state === "running") {
      setTimeout(tick, 5000);
    } else if (state === "done") {
      renderJsonBlock(resultEl, data.result || data);
      if (onDone) onDone(data);
    } else if (state === "error") {
      statusEl.textContent = `Status: error — ${data.error || "unknown error"}`;
      renderJsonBlock(resultEl, data);
    }
  };
  tick();
}

document.addEventListener("DOMContentLoaded", () => {
  const collectBtn = document.getElementById("ml-collect-btn");
  const labelBtn = document.getElementById("ml-label-btn");
  const summaryBtn = document.getElementById("ml-summary-btn");
  const backfillBtn = document.getElementById("ml-backfill-btn");
  const backfillStatusBtn = document.getElementById("ml-backfill-status-btn");

  const collectStatusEl = document.getElementById("ml-collect-status");
  const summaryResultsEl = document.getElementById("ml-summary-results");
  const backfillStatusEl = document.getElementById("ml-backfill-status");
  const backfillResultsEl = document.getElementById("ml-backfill-results");

  collectBtn.addEventListener("click", async () => {
    collectBtn.disabled = true;
    collectStatusEl.textContent = "Starting collection…";
    try {
      const start = await postJson("/api/training-data/collect");
      if (!start.ok && !start.running) {
        collectStatusEl.textContent = `Error: ${start.error || "failed to start"}`;
        return;
      }
      pollStatus("/api/training-data/collect/status", collectStatusEl, summaryResultsEl);
    } catch (err) {
      collectStatusEl.textContent = `Error: ${err.message || err}`;
    } finally {
      collectBtn.disabled = false;
    }
  });

  labelBtn.addEventListener("click", async () => {
    labelBtn.disabled = true;
    collectStatusEl.textContent = "Labeling pending snapshots…";
    try {
      const result = await postJson("/api/training-data/label");
      collectStatusEl.textContent = result.ok ? "Labeling complete." : `Error: ${result.error}`;
      renderJsonBlock(summaryResultsEl, result);
    } catch (err) {
      collectStatusEl.textContent = `Error: ${err.message || err}`;
    } finally {
      labelBtn.disabled = false;
    }
  });

  summaryBtn.addEventListener("click", async () => {
    summaryBtn.disabled = true;
    try {
      const result = await getJson("/api/training-data/summary");
      renderJsonBlock(summaryResultsEl, result);
    } catch (err) {
      summaryResultsEl.innerHTML = `<p class="fail">Error: ${escHtml(err.message || err)}</p>`;
    } finally {
      summaryBtn.disabled = false;
    }
  });

  backfillBtn.addEventListener("click", async () => {
    if (!confirm("This re-runs the full 2-year regime backfill across all tickers and overwrites regime_training.csv. Continue?")) return;
    backfillBtn.disabled = true;
    backfillStatusEl.textContent = "Starting backfill…";
    try {
      const start = await postJson("/api/training-data/backfill-regime");
      if (!start.ok && !start.running) {
        backfillStatusEl.textContent = `Error: ${start.error || "failed to start"}`;
        return;
      }
      pollStatus("/api/training-data/backfill-regime/status", backfillStatusEl, backfillResultsEl);
    } catch (err) {
      backfillStatusEl.textContent = `Error: ${err.message || err}`;
    } finally {
      backfillBtn.disabled = false;
    }
  });

  backfillStatusBtn.addEventListener("click", async () => {
    backfillStatusBtn.disabled = true;
    try {
      const data = await getJson("/api/training-data/backfill-regime/status");
      backfillStatusEl.textContent = `Status: ${data.state || "idle"}`;
      renderJsonBlock(backfillResultsEl, data.result || data);
    } catch (err) {
      backfillStatusEl.textContent = `Error: ${err.message || err}`;
    } finally {
      backfillStatusBtn.disabled = false;
    }
  });

  // ── Train models ───────────────────────────────────────────────────────────
  const trainBtn = document.getElementById("ml-train-btn");
  const trainStatusBtn = document.getElementById("ml-train-status-btn");
  const trainStatusEl = document.getElementById("ml-train-status");
  const trainResultsEl = document.getElementById("ml-train-results");

  trainBtn.addEventListener("click", async () => {
    trainBtn.disabled = true;
    trainStatusEl.textContent = "Starting training…";
    try {
      const start = await postJson("/api/training-data/train-models");
      if (!start.ok && !start.running) {
        trainStatusEl.textContent = `Error: ${start.error || "failed to start"}`;
        return;
      }
      pollStatus("/api/training-data/train-models/status", trainStatusEl, trainResultsEl);
    } catch (err) {
      trainStatusEl.textContent = `Error: ${err.message || err}`;
    } finally {
      trainBtn.disabled = false;
    }
  });

  trainStatusBtn.addEventListener("click", async () => {
    trainStatusBtn.disabled = true;
    try {
      const data = await getJson("/api/training-data/train-models/status");
      trainStatusEl.textContent = `Status: ${data.state || "idle"}`;
      renderJsonBlock(trainResultsEl, data.result || data);
    } catch (err) {
      trainStatusEl.textContent = `Error: ${err.message || err}`;
    } finally {
      trainStatusBtn.disabled = false;
    }
  });

  // ── Live predictions ───────────────────────────────────────────────────────
  const predictBtn = document.getElementById("ml-predict-btn");
  const predictStatusEl = document.getElementById("ml-predict-status");
  const predictResultsEl = document.getElementById("ml-predict-results");

  predictBtn.addEventListener("click", async () => {
    predictBtn.disabled = true;
    predictStatusEl.textContent = "Fetching live predictions…";
    try {
      const result = await getJson("/api/ml/predict");
      predictStatusEl.textContent = result.ok
        ? `Predictions complete — ${(result.predictions || []).length} tickers`
        : `Error: ${result.error}`;
      if (result.ok) renderPredictions(predictResultsEl, result);
      else renderJsonBlock(predictResultsEl, result);
    } catch (err) {
      predictStatusEl.textContent = `Error: ${err.message || err}`;
    } finally {
      predictBtn.disabled = false;
    }
  });
});

function renderPredictions(el, result) {
  const preds = result.predictions || [];
  if (!preds.length) { el.innerHTML = "<p class='muted'>No predictions returned.</p>"; return; }

  const modelOk = result.model_status || {};
  const trained = Object.entries(modelOk).filter(([, v]) => v).map(([k]) => k).join(", ");
  const missing = Object.entries(modelOk).filter(([, v]) => !v).map(([k]) => k);

  let html = "";
  if (missing.length) {
    html += `<p class="muted" style="color:var(--warn)">Models not trained: ${escHtml(missing.join(", "))} — train first above.</p>`;
  }
  if (trained) {
    html += `<p class="muted">Active models: ${escHtml(trained)}</p>`;
  }

  html += `<table class="ml-pred-table">
    <thead><tr>
      <th>Ticker</th><th>Regime</th><th>Probabilities</th>
      <th>Exp. Return</th><th>Exp. Vol</th><th title="1-sigma expected move over 10 trading days">Exp. Move (10d)</th>
      <th title="Will IV rank expand or contract over 10 days?">IV Direction</th>
      <th title="Meta-ensemble: stacked score from all 5 models (0=bearish, 100=bullish)">Meta Score</th>
      <th title="Model agreement: how consistently all signals point the same direction">Agreement</th>
      <th title="Anomaly detector: 100=normal, 0=extreme outlier vs training history">Anomaly</th>
      <th>P(Up)</th><th>RSI</th><th>Trend</th><th>SPY Trend</th>
    </tr></thead><tbody>`;

  for (const p of preds) {
    if (!p.ok) {
      html += `<tr><td>${escHtml(p.ticker)}</td><td colspan="7" style="color:var(--err)">${escHtml(p.error || "error")}</td></tr>`;
      continue;
    }
    const regime = p.regime || "—";
    const proba = p.regime_proba
      ? Object.entries(p.regime_proba).map(([k, v]) => `${escHtml(k)}: ${(v * 100).toFixed(0)}%`).join(" · ")
      : "—";
    const ret = p.expected_return != null ? `${(p.expected_return * 100).toFixed(2)}%` : "—";
    const vol = p.expected_vol != null ? `${(p.expected_vol * 100).toFixed(2)}%` : "—";
    const em    = p.expected_move_pct != null ? `±${(p.expected_move_pct * 100).toFixed(1)}%` : "—";
    const ivDir = p.iv_direction || "—";
    const ivProb = p.iv_expanding_prob != null ? ` (${(p.iv_expanding_prob * 100).toFixed(0)}%)` : "";
    const ivDirCls = p.iv_direction === "Expanding" ? "style=\"color:var(--warn)\"" : p.iv_direction === "Contracting" ? "style=\"color:var(--pass)\"" : "";
    const pUp   = p.p_up != null ? `${(p.p_up * 100).toFixed(0)}%` : "—";
    const live  = p.live || {};
    html += `<tr>
      <td><strong>${escHtml(p.ticker)}</strong></td>
      <td>${escHtml(regime)}</td>
      <td class="muted" style="font-size:.8em">${proba}</td>
      <td>${escHtml(ret)}</td>
      <td>${escHtml(vol)}</td>
      <td><strong>${escHtml(em)}</strong></td>
      <td ${ivDirCls}>${escHtml(ivDir)}${escHtml(ivProb)}</td>
      <td>${p.meta_score != null ? `<strong>${p.meta_score.toFixed(0)}</strong>/100` : "—"}</td>
      <td>${(() => {
        const pd = p.pred_dist;
        if (!pd) return "—";
        const agr = pd.model_agreement;
        const col = agr === "High" ? "var(--pass)" : agr === "Low" ? "var(--fail)" : "var(--warn)";
        const conf = pd.confidence != null ? ` ${(pd.confidence*100).toFixed(0)}%` : "";
        return `<span style="color:${col};font-weight:600">${agr||"—"}${conf}</span>`;
      })()}</td>
      <td>${p.anomaly_score != null
        ? `${p.anomaly_score.toFixed(0)}${p.is_anomaly ? " ⚠" : ""}`
        : "—"}</td>
      <td>${escHtml(pUp)}</td>
      <td>${live.rsi != null ? live.rsi.toFixed(1) : "—"}</td>
      <td>${escHtml(live.trend || "—")}</td>
      <td>${escHtml(live.spy_trend || "—")}</td>
    </tr>`;
  }
  html += "</tbody></table>";

  if ((result.warnings || []).length) {
    html += `<details><summary class="muted">Warnings (${result.warnings.length})</summary>
      <pre class="ml-admin-json">${escHtml(result.warnings.join("\n"))}</pre></details>`;
  }
  el.innerHTML = html;
}
