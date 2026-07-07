/**
 * Live Positions Modal Module
 * Handles action modal dialogs for position management
 * Phase 2 Refactoring: Modal isolation
 */

// ── Modal Building ─────────────────────────────────────────────────────────────

/**
 * Build the action modal HTML
 * @returns {string} Modal HTML
 */
function buildActionModal() {
  return `
    <div id="lp-action-modal" class="lp-modal-overlay">
      <div class="lp-modal-dialog">
        <div class="lp-modal-header">
          <h3 id="lp-action-title">Action</h3>
          <button class="lp-modal-close" data-action="close">&times;</button>
        </div>

        <div id="lp-action-body" class="lp-modal-body">
          <p class="hint" id="lp-action-details"></p>
        </div>

        <div class="lp-modal-fields" id="lp-action-fields"></div>

        <div class="lp-modal-actions">
          <button class="lp-btn-primary" id="lp-action-confirm">Confirm</button>
          <button class="lp-btn-secondary" id="lp-action-cancel">Cancel</button>
        </div>

        <div id="lp-action-result" class="lp-modal-result"></div>
      </div>
    </div>
  `;
}

// ── Modal Opening ──────────────────────────────────────────────────────────────

/**
 * Open action modal with specific action type
 * @param {string} type - Action type (edit, close, log, etc.)
 * @param {Object} data - Data related to action
 */
function openActionModal(type, data) {
  const modal = document.getElementById("lp-action-modal");
  if (!modal) {
    console.error("Modal not found");
    return;
  }

  const titleEl = document.getElementById("lp-action-title");
  const detailsEl = document.getElementById("lp-action-details");
  const fieldsEl = document.getElementById("lp-action-fields");
  const confirmBtn = document.getElementById("lp-action-confirm");

  // Clear previous state
  fieldsEl.innerHTML = "";
  document.getElementById("lp-action-result").classList.remove("is-visible");

  // Configure based on action type
  switch (type) {
    case "close":
      titleEl.textContent = "Close Position";
      detailsEl.textContent = `Close ${data.ticker} ${data.description || ""}?`;
      fieldsEl.innerHTML = `
        <label class="lp-modal-label">
          Current spread value ($ per share):
          <input type="number" class="lp-modal-input-short" id="lp-close-value" step="0.01" min="0" placeholder="e.g. 0.20">
        </label>
        <p class="hint">Credit spread: cost to buy back. Debit spread: proceeds from selling. Enter 0 to mark expired worthless.</p>
      `;
      confirmBtn.textContent = "Close Position";
      break;

    case "log":
      titleEl.textContent = "Log Trade";
      detailsEl.textContent = `Log trade for ${data.ticker}?`;
      fieldsEl.innerHTML = `
        <label class="lp-modal-label">
          Contracts:
          <input type="number" class="lp-modal-input-short" id="lp-log-contracts" value="1" min="1" max="20">
        </label>
      `;
      confirmBtn.textContent = "Log Trade";
      break;

    case "edit":
      titleEl.textContent = "Edit Position";
      detailsEl.textContent = `Edit ${data.ticker} ${data.description || ""}?`;
      fieldsEl.innerHTML = `
        <label class="lp-modal-label-full">
          New quantity:
          <input type="number" class="lp-modal-input-full" id="lp-edit-qty" value="${data.qty || 1}" step="1">
        </label>
      `;
      confirmBtn.textContent = "Update Position";
      break;

    default:
      console.warn("Unknown action type:", type);
      return;
  }

  // Store action metadata on modal for handlers
  modal.dataset.actionType = type;
  modal.dataset.actionData = JSON.stringify(data);

  // Show modal
  modal.classList.add("is-open");

  // Focus first input
  const firstInput = fieldsEl.querySelector("input");
  if (firstInput) {
    setTimeout(() => firstInput.focus(), 100);
  }
}

/**
 * Close the action modal
 */
function closeActionModal() {
  const modal = document.getElementById("lp-action-modal");
  if (modal) {
    modal.classList.remove("is-open");
  }
}

/**
 * Show result message in modal
 * @param {string} message - Result message
 * @param {boolean} isError - Whether to show as error
 */
function showModalResult(message, isError = false) {
  const resultEl = document.getElementById("lp-action-result");
  if (resultEl) {
    resultEl.className = (isError ? "lp-modal-result lp-error-text" : "lp-modal-result pass") + " is-visible";
    resultEl.textContent = message;
  }
}

// ── Modal Event Handlers ───────────────────────────────────────────────────────

/**
 * Set up modal event listeners
 * Should be called after modal is added to DOM
 */
function setupModalHandlers() {
  if (typeof eventManager === 'undefined') {
    console.warn("EventManager not available, using fallback");
    setupModalHandlersFallback();
    return;
  }

  const modal = document.getElementById("lp-action-modal");
  if (!modal) return;

  // Close button
  eventManager.onClick("#lp-action-modal .lp-modal-close", closeActionModal);

  // Cancel button
  eventManager.onClick("#lp-action-cancel", closeActionModal);

  // Confirm button
  eventManager.onClick("#lp-action-confirm", handleModalConfirm);

  // Close on overlay click
  eventManager.onClick("#lp-action-modal", (e) => {
    if (e.target.id === "lp-action-modal") {
      closeActionModal();
    }
  });
}

/**
 * Fallback handler setup (if eventManager not available)
 */
function setupModalHandlersFallback() {
  const modal = document.getElementById("lp-action-modal");
  if (!modal) return;

  const closeBtn = modal.querySelector(".lp-modal-close");
  const cancelBtn = document.getElementById("lp-action-cancel");
  const confirmBtn = document.getElementById("lp-action-confirm");

  closeBtn?.addEventListener("click", closeActionModal);
  cancelBtn?.addEventListener("click", closeActionModal);
  confirmBtn?.addEventListener("click", handleModalConfirm);

  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeActionModal();
  });
}

/**
 * Handle modal confirm action
 */
function handleModalConfirm() {
  const modal = document.getElementById("lp-action-modal");
  const actionType = modal?.dataset.actionType;

  console.log("Modal confirm:", actionType);

  switch (actionType) {
    case "close":
      const value = document.getElementById("lp-close-value")?.value;
      if (value == null || value === "") {
        showModalResult("Please enter a value", true);
      } else {
        showModalResult("Position closed successfully");
        setTimeout(() => closeActionModal(), 1500);
      }
      break;

    case "log":
      const contracts = document.getElementById("lp-log-contracts")?.value;
      if (!contracts || contracts < 1) {
        showModalResult("Please enter valid contracts", true);
      } else {
        showModalResult("Trade logged successfully");
        setTimeout(() => closeActionModal(), 1500);
      }
      break;

    case "edit":
      const qty = document.getElementById("lp-edit-qty")?.value;
      if (!qty || qty < 1) {
        showModalResult("Please enter valid quantity", true);
      } else {
        showModalResult("Position updated successfully");
        setTimeout(() => closeActionModal(), 1500);
      }
      break;

    default:
      showModalResult("Unknown action", true);
  }
}

// ── Modal Helpers ──────────────────────────────────────────────────────────────

/**
 * Check if modal is open
 * @returns {boolean}
 */
function isModalOpen() {
  const modal = document.getElementById("lp-action-modal");
  return !!(modal && modal.classList.contains("is-open"));
}

/**
 * Get current modal action data
 * @returns {Object|null}
 */
function getModalActionData() {
  const modal = document.getElementById("lp-action-modal");
  if (!modal || !modal.dataset.actionData) return null;

  try {
    return JSON.parse(modal.dataset.actionData);
  } catch (e) {
    console.warn("Failed to parse modal action data:", e);
    return null;
  }
}
