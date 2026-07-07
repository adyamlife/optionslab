// E*TRADE connection status bar + OAuth flow

let _etradePopup = null;

// ── Render the status bar ─────────────────────────────────────────────────────

function renderEtradeBar(status) {
  const el = document.getElementById("etrade-bar");
  if (!el) return;

  if (!status.configured) {
    el.innerHTML = `<div class="etrade-bar etrade-unconfigured">
      <span class="etrade-dot dot-off"></span>
      <span>E*TRADE: not configured — add keys to <code>config/secrets.toml</code></span>
    </div>`;
    return;
  }

  if (!status.authenticated) {
    el.innerHTML = `<div class="etrade-bar etrade-disconnected">
      <span class="etrade-dot dot-off"></span>
      <strong>E*TRADE:</strong> not connected
      <button class="btn-etrade-connect" id="btn-etrade-login">Connect Account</button>
      <span class="etrade-hint">${status.sandbox ? "(Sandbox)" : "(Live)"}</span>
    </div>`;
    document.getElementById("btn-etrade-login").addEventListener("click", startEtradeLogin);
    return;
  }

  const bal = status.balance;
  const mode = status.sandbox ? "Sandbox" : "Live";
  el.innerHTML = `<div class="etrade-bar etrade-connected">
    <span class="etrade-dot dot-on"></span>
    <strong>E*TRADE ${mode}:</strong>
    ${bal ? `
      <span class="etrade-acct">${bal.account_name}</span>
      <span class="etrade-stat">Buying Power: <strong>$${bal.buying_power.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}</strong></span>
      <span class="etrade-stat">Net Value: <strong>$${bal.net_value.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}</strong></span>
    ` : "connected"}
    <button class="btn-etrade-disconnect" id="btn-etrade-logout">Disconnect</button>
  </div>`;
  document.getElementById("btn-etrade-logout").addEventListener("click", etradeLogout);
}

// ── OAuth login flow ──────────────────────────────────────────────────────────

async function startEtradeLogin() {
  try {
    const res = await fetch("/api/etrade/login");
    const data = await res.json();
    if (!data.ok) { alert("E*TRADE login error: " + data.error); return; }

    // Show the manual verifier dialog immediately (E*TRADE sandbox doesn't redirect back)
    showVerifierDialog(data.authorize_url);
  } catch (e) {
    alert("Failed to start E*TRADE login: " + e);
  }
}

function showVerifierDialog(authorizeUrl) {
  // Open the E*TRADE authorization page in a new tab
  window.open(authorizeUrl, "_blank", "noopener");

  // Show an inline dialog asking for the verifier code
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.id = "etrade-verifier-overlay";
  overlay.innerHTML = `
    <div class="modal-box">
      <h3>E*TRADE Authorization</h3>
      <p class="hint">
        A new tab has opened with the E*TRADE login page.<br>
        After you authorize the app, E*TRADE will show you a <strong>verifier code</strong>.
        Enter it below.
      </p>
      <div class="modal-fields">
        <label>Verifier Code
          <input type="text" id="etrade-verifier-input" placeholder="e.g. 12345678" style="width:12rem;font-size:1.1rem;letter-spacing:0.1em">
        </label>
      </div>
      <div class="modal-actions">
        <button class="btn-primary" id="etrade-verifier-confirm">Submit</button>
        <button id="etrade-verifier-cancel">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.style.display = "flex";
  document.getElementById("etrade-verifier-input").focus();

  document.getElementById("etrade-verifier-confirm").addEventListener("click", async () => {
    const verifier = document.getElementById("etrade-verifier-input").value.trim();
    if (!verifier) { alert("Please enter the verifier code."); return; }
    overlay.remove();
    await submitVerifier(verifier);
  });
  document.getElementById("etrade-verifier-cancel").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
}

async function submitVerifier(verifier) {
  try {
    // The callback route exchanges token + verifier for access token
    const url = `/api/etrade/callback?oauth_verifier=${encodeURIComponent(verifier)}`;
    const res = await fetch(url);
    if (res.ok || res.redirected) {
      await refreshEtradeStatus();
    } else {
      const text = await res.text();
      alert("E*TRADE auth failed: " + text);
    }
  } catch (e) {
    alert("Error during E*TRADE auth: " + e);
  }
}

async function etradeLogout() {
  await fetch("/api/etrade/logout", { method: "POST" });
  await refreshEtradeStatus();
}

// ── Status polling ────────────────────────────────────────────────────────────

async function refreshEtradeStatus() {
  try {
    const res = await fetch("/api/etrade/status");
    const status = await res.json();
    renderEtradeBar(status);

    // If connected, also refresh the journal so capital shows live buying power
    if (status.authenticated && status.balance && typeof refreshJournal === "function") {
      refreshJournal();
    }

    // Handle ?etrade=connected redirect after OAuth callback
    const params = new URLSearchParams(window.location.search);
    if (params.get("etrade") === "connected") {
      history.replaceState(null, "", window.location.pathname);
    }
  } catch (e) {
    console.error("E*TRADE status check failed:", e);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  refreshEtradeStatus();
  // Refresh status every 5 minutes (tokens expire at midnight ET)
  setInterval(refreshEtradeStatus, 5 * 60 * 1000);
});
