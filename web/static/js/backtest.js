function renderBacktestResults(data) {
  const el = document.getElementById("backtest-results");
  if (data.error) {
    el.innerHTML = `<p class="fail">${data.error}</p>`;
    return;
  }

  const s = data.summary;
  let html = `
    <div class="summary-grid">
      <div class="stat"><div class="label">Trades Taken</div><div class="value">${s.trades_taken}</div></div>
      <div class="stat"><div class="label">Skipped (No Trade)</div><div class="value">${s.skipped_no_trade}</div></div>
      <div class="stat"><div class="label">Skipped (Filter)</div><div class="value">${s.skipped_filter}</div></div>
      <div class="stat"><div class="label">Win Rate</div><div class="value">${s.win_rate}</div></div>
      <div class="stat"><div class="label">Avg Win</div><div class="value">${s.avg_win}</div></div>
      <div class="stat"><div class="label">Avg Loss</div><div class="value">${s.avg_loss}</div></div>
      <div class="stat"><div class="label">Expectancy</div><div class="value">${s.expectancy}</div></div>
    </div>
  `;

  if (data.by_structure && data.by_structure.length) {
    html += `<h3>By Structure</h3>`;
    html += `<table><thead><tr><th>Structure</th><th>N</th><th>Win Rate</th><th>Avg P&amp;L</th></tr></thead><tbody>`;
    for (const row of data.by_structure) {
      html += `<tr><td>${row.structure}</td><td>${row.n}</td><td>${row.win_rate}</td><td>${row.avg_pnl}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  if (data.top_trades && data.top_trades.length) {
    html += `<h3>Top 3 Trades by P&amp;L</h3>`;
    html += `<table><thead><tr><th>Date</th><th>Structure</th><th>Details</th><th>P&amp;L</th><th>Result</th><th>Flags</th><th>Structure Win Rate</th></tr></thead><tbody>`;
    for (const t of data.top_trades) {
      const resultCls = t.win ? "pass" : "fail";
      const resultText = t.win ? "Win" : "Loss";
      html += `<tr>
        <td>${t.date}</td>
        <td>${t.structure}</td>
        <td>${t.details}</td>
        <td>${t.pnl}</td>
        <td class="${resultCls}">${resultText}</td>
        <td>${t.flags}</td>
        <td>${t.structure_win_rate}</td>
      </tr>`;
    }
    html += `</tbody></table>`;
  }

  if (data.flags_summary && data.flags_summary.length) {
    html += `<h3>Flags Across All Trades</h3>`;
    html += `<table><thead><tr><th>Flag</th><th>Count</th></tr></thead><tbody>`;
    for (const f of data.flags_summary) {
      html += `<tr><td>${f.flag}</td><td>${f.count}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  el.innerHTML = html;
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("backtest-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = document.getElementById("backtest-status");
    const submitBtn = form.querySelector("button");
    status.textContent = "Running backtest... this can take a little while (downloads price history).";
    submitBtn.disabled = true;
    document.getElementById("backtest-results").innerHTML = "";

    const params = new URLSearchParams(new FormData(form));
    try {
      const res = await fetch("/api/backtest?" + params.toString());
      const data = await res.json();
      status.textContent = data.error ? "Error" : "Done.";
      renderBacktestResults(data);
    } catch (err) {
      status.textContent = "Request failed: " + err;
    } finally {
      submitBtn.disabled = false;
    }
  });
});
