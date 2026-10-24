"use strict";

const byId = (id) => document.getElementById(id);
const buttons = Array.from(document.querySelectorAll("[data-action]"));
const number = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const money = (minor) => `${minor < 0 ? "-" : ""}INR ${number.format(Math.abs(minor) / 100)}`;
let snapshot = null;
let busy = false;
let verified = false;
let pendingTimer = null;

function notice(message, error = false) {
  const element = byId("notice");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = !message;
}

async function request(path, method = "GET") {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 65000);
  try {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: method === "POST" ? { "Content-Type": "application/json" } : {},
      body: method === "POST" ? "{}" : undefined,
      signal: controller.signal,
    });
    const body = await response.json();
    if (!response.ok) {
      const error = new Error(body.error?.message || "The demo could not complete this request. Please try again.");
      error.status = response.status;
      throw error;
    }
    return { body, status: response.status };
  } finally {
    clearTimeout(timeout);
  }
}

function render() {
  const payment = snapshot?.payment;
  const entries = snapshot?.ledger || [];
  const status = payment?.status || "ready";
  const labels = { ready: "Ready", processing: "Processing", authorized: "Authorized", capture_pending: "Capture pending", captured: "Captured", refunded: "Refunded", failed: "Failed", voided: "Voided", void_pending: "Void pending" };
  const statusElement = byId("payment-status");
  statusElement.textContent = labels[status] || status;
  statusElement.className = `pill ${["authorized", "captured", "refunded"].includes(status) ? "positive" : status.includes("pending") || status === "processing" ? "pending" : status === "failed" ? "negative" : "neutral"}`;
  byId("balance").textContent = money(snapshot?.balance_minor || 0);
  byId("payment-count").textContent = payment ? "1" : "0";
  byId("payment-reference").textContent = payment?.id || "Waiting";
  byId("entry-count").textContent = entries.length ? `${entries.length} signed entries` : "No entries yet";
  byId("empty-ledger").hidden = entries.length > 0;
  byId("ledger-table-wrap").hidden = entries.length === 0;

  const total = entries.reduce((sum, entry) => sum + entry.amount_minor, 0);
  const balanceLabel = byId("ledger-status");
  balanceLabel.textContent = entries.length ? total === 0 ? "Balanced" : "Needs review" : "Waiting";
  balanceLabel.className = `pill ${entries.length ? total === 0 ? "positive" : "negative" : "neutral"}`;
  byId("entry-sum").textContent = money(total);
  const rows = byId("ledger-rows");
  rows.replaceChildren();
  const postings = [...new Set(entries.map((entry) => entry.transfer_id))];
  for (const entry of entries) {
    const row = document.createElement("tr");
    for (const [text, className] of [
      [entry.account, ""],
      [`${postings.indexOf(entry.transfer_id) + 1}`, "posting-label"],
      [`${entry.amount_minor > 0 ? "+" : ""}${money(entry.amount_minor)}`, `amount ${entry.amount_minor > 0 ? "credit" : "debit"}`],
    ]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      cell.className = className;
      row.appendChild(cell);
    }
    rows.appendChild(row);
  }
  for (const [name, id] of [["balances_match", "check-balances"], ["transfers_balance", "check-transfers"], ["payments_match", "check-payments"]]) {
    const element = byId(id);
    const passed = snapshot?.checks[name];
    element.textContent = verified ? passed ? "Pass" : "Needs review" : entries.length ? "Ready to verify" : "Waiting";
    element.className = `check-state ${verified ? passed ? "pass" : "fail" : ""}`;
  }
  const enabled = {
    create: !payment,
    retry: Boolean(payment),
    capture: status === "authorized",
    refund: status === "captured" && payment.amount_refunded === 0,
    check: entries.length > 0,
  };
  for (const button of buttons) {
    button.disabled = busy || !enabled[button.dataset.action];
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }
}

function showEvidence(action, response) {
  byId("http-status").textContent = `HTTP ${response.status}`;
  byId("replayed").textContent = response.body.replayed === true ? "Yes, original result returned" : action === "check" ? "Read only" : "No";
  const descriptions = {
    create: "The gateway authorized INR 500.00. No ledger posting is needed for an authorization.",
    retry: "The original request key returned its stored result. No second payment or posting was created.",
    capture: "The gateway confirmed INR 400.00. A debit and credit committed with the payment state.",
    refund: "The gateway returned INR 150.00. Two new entries record the refund without editing the capture.",
    check: "These checks compare your balances, transfers, and payment totals in one database snapshot.",
  };
  let description = descriptions[action];
  if (response.status === 202) description = "The gateway outcome is pending. Keep this payment and wait for recovery before starting another charge.";
  if (response.body.result?.status === "failed") description = "The gateway returned a definite failure. The payment record contains the reason.";
  byId("request-description").textContent = description;
  byId("response-details").hidden = false;
  byId("response-json").textContent = JSON.stringify(response.body.result || response.body, null, 2);
}

async function refreshPending() {
  clearTimeout(pendingTimer);
  const payment = snapshot?.payment;
  if (!payment || !["processing", "capture_pending", "void_pending"].includes(payment.status)) return;
  try {
    snapshot = (await request("/demo/state")).body;
    render();
    pendingTimer = setTimeout(refreshPending, 3000);
  } catch (error) {
    notice(error.message, true);
  }
}

async function act(action) {
  if (busy) return;
  busy = true;
  render();
  notice("Sending request...");
  try {
    let response;
    if (action === "create") {
      await request("/demo/session", "POST");
      response = await request("/demo/payments", "POST");
    } else if (action === "retry") {
      response = await request("/demo/payments", "POST");
    } else if (action === "check") {
      response = await request("/demo/state");
      verified = true;
    } else {
      response = await request(`/demo/payments/${snapshot.payment.id}/${action}`, "POST");
    }
    snapshot = response.body.snapshot || response.body;
    if (action !== "check") verified = false;
    showEvidence(action, response);
    const messages = {
      create: "Payment authorized. The merchant balance is still INR 0.00.",
      retry: "Same payment, same result. The request was replayed.",
      capture: "Captured INR 400.00. Both sides are now in the ledger.",
      refund: "Refunded INR 150.00. The merchant keeps INR 250.00.",
      check: "All three accounting checks passed for this session.",
    };
    const failedCheck = action === "check" && Object.values(snapshot.checks).some((value) => !value);
    if (response.status === 202) notice("This payment is pending. The page will check its state.");
    else if (snapshot.payment?.status === "failed") notice("The payment failed. Inspect the response for its reason.", true);
    else notice(failedCheck ? "A ledger check failed. This session needs review." : messages[action], failedCheck);
    refreshPending();
  } catch (error) {
    if (error.status === 401) {
      snapshot = null;
      verified = false;
      notice("Your session ended. Authorize a new test payment to begin again.", true);
    } else {
      notice(error.name === "AbortError" ? "The request timed out. Reload to check its state before trying again." : error.message, true);
    }
  } finally {
    busy = false;
    render();
  }
}

for (const button of buttons) button.addEventListener("click", () => act(button.dataset.action));
busy = true;
render();
request("/demo/state")
  .then((response) => { snapshot = response.body; refreshPending(); })
  .catch((error) => { if (error.status !== 401) notice("The demo could not connect. You can try starting a payment again.", true); })
  .finally(() => { busy = false; render(); });
