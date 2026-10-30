"use strict";

const byId = (id) => document.getElementById(id);
const buttons = Array.from(document.querySelectorAll("[data-action]"));
const number = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const clock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const money = (minor) => `${minor < 0 ? "-" : ""}INR ${number.format(Math.abs(minor) / 100)}`;
const PENDING = ["processing", "capture_pending", "void_pending"];
let snapshot = null;
let busy = false;
let verified = false;
let pendingTimer = null;
let expiresAt = null;
const events = [];

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Info and errors live in separate regions: errors need role=alert to be read
// out at once. Both regions stay in the DOM so the first message is announced.
function notice(message, error = false) {
  byId("notice").textContent = error ? "" : message;
  byId("alert").textContent = error ? message : "";
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
    // A sleeping or restarting host answers with its own HTML page.
    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : {};
    } catch {
      const error = new Error(response.status >= 500
        ? "The server is starting up or restarting. Wait a few seconds and try again."
        : `The server sent an unexpected response (HTTP ${response.status}).`);
      error.status = response.status;
      throw error;
    }
    if (!response.ok) {
      // Gateway refusals on capture or refund still carry the session snapshot.
      const wrapped = body.result?.error;
      const error = new Error(wrapped?.message || body.error?.message || "The request could not be completed. Try again.");
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return { body, status: response.status };
  } finally {
    clearTimeout(timeout);
  }
}

function paymentStatus(payment) {
  if (!payment) return ["Not created", "draft"];
  const refunds = payment.refunds || [];
  if (refunds.some((refund) => refund.status === "pending")) return ["Refund pending", "wait"];
  if (payment.status === "captured" && payment.amount_refunded > 0) return ["Partially refunded", "ok"];
  const labels = {
    processing: ["Processing", "wait"], authorized: ["Authorized", "info"], capture_pending: ["Capture pending", "wait"],
    captured: ["Captured", "ok"], refunded: ["Refunded", "ok"], failed: ["Failed", "bad"],
    voided: ["Voided", "draft"], void_pending: ["Void pending", "wait"],
  };
  return labels[payment.status] || [payment.status, "draft"];
}

function nextStep(payment, entries) {
  if (!payment) return "Authorize places a hold on the card. No money moves until capture.";
  const status = payment.status;
  if (PENDING.includes(status) || paymentStatus(payment)[0] === "Refund pending") {
    return "The gateway has not confirmed yet. This page checks again every few seconds.";
  }
  if (status === "failed") return `The gateway refused: ${payment.failure_reason || "no reason given"}. Start over to try again.`;
  if (status === "authorized") return "Replay the request to see it return the same payment, or capture INR 400.00 of the INR 500.00 hold.";
  if (status === "captured" && payment.amount_refunded === 0) return "Refund INR 150.00. The refund gets its own entries; the capture is never edited.";
  if (!verified && entries.length) return "Verify the ledger to check every balance against its entries.";
  return "Done. Start over to run it again.";
}

function render() {
  const payment = snapshot?.payment ?? null;
  const entries = snapshot?.ledger || [];
  const [label, tone] = paymentStatus(payment);
  const status = byId("payment-status");
  status.textContent = label;
  status.className = `status status--${tone}`;
  byId("crumb-id").textContent = payment ? payment.id : "New payment";
  byId("crumb-id").classList.toggle("id", Boolean(payment));
  byId("copy-id").hidden = !payment;
  const gateway = byId("payment-gateway");
  gateway.hidden = !payment?.gateway;
  gateway.textContent = payment?.gateway === "stripe" ? "Stripe test mode" : `Simulated gateway ${payment?.gateway || ""}`;
  byId("next-step").textContent = nextStep(payment, entries);

  byId("balance").textContent = money(snapshot?.balance_minor || 0);
  byId("captured").textContent = money(payment?.amount_captured || 0);
  byId("refunded").textContent = money(payment?.amount_refunded || 0);
  byId("empty-ledger").hidden = entries.length > 0;
  byId("ledger-table-wrap").hidden = entries.length === 0;

  const transfers = [...new Set(entries.map((entry) => entry.transfer_id))];
  const rows = byId("ledger-rows");
  rows.replaceChildren();
  let debits = 0;
  let credits = 0;
  for (const entry of entries) {
    const row = element("tr");
    const debit = entry.amount_minor < 0 ? -entry.amount_minor : 0;
    const credit = entry.amount_minor > 0 ? entry.amount_minor : 0;
    debits += debit;
    credits += credit;
    const transfer = element("td", "transfer", `T${transfers.indexOf(entry.transfer_id) + 1}`);
    transfer.title = entry.transfer_id;
    row.append(element("td", "", entry.account), transfer,
      element("td", "num", debit ? number.format(debit / 100) : ""),
      element("td", "num", credit ? number.format(credit / 100) : ""));
    rows.append(row);
  }
  byId("debit-total").textContent = number.format(debits / 100);
  byId("credit-total").textContent = number.format(credits / 100);
  const ledgerStatus = byId("ledger-status");
  ledgerStatus.textContent = entries.length ? debits === credits ? "Balanced" : "Out of balance" : "Empty";
  ledgerStatus.className = `status status--${entries.length ? debits === credits ? "ok" : "bad" : "draft"}`;

  for (const [name, id] of [["balances_match", "check-balances"], ["transfers_balance", "check-transfers"], ["payments_match", "check-payments"]]) {
    const cell = byId(id);
    const passed = snapshot?.checks?.[name];
    cell.textContent = verified ? passed ? "Pass" : "Fail" : "Not run";
    cell.className = `check${verified ? passed ? " check--pass" : " check--fail" : ""}`;
  }

  const refundPending = (payment?.refunds || []).some((refund) => refund.status === "pending");
  const enabled = {
    create: !payment,
    retry: Boolean(payment),
    capture: payment?.status === "authorized",
    refund: payment?.status === "captured" && payment.amount_refunded === 0 && !refundPending,
    check: entries.length > 0,
    reset: Boolean(snapshot),
  };
  for (const button of buttons) {
    button.disabled = busy || !enabled[button.dataset.action];
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }
  // The primary style follows the next useful step.
  const next = ["create", "capture", "refund", "check"].find((action) => enabled[action] && !(action === "check" && verified));
  for (const button of buttons) button.classList.toggle("action--primary", button.dataset.action === next);

  byId("session-status").textContent = !snapshot ? "No session"
    : expiresAt ? `Session ends at ${new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(expiresAt)}`
      : "Session active";
}

const meanings = {
  create: "Authorized INR 500.00. A hold, so no ledger entries yet.",
  retry: "Same request key, so the stored result came back. No second payment, no new entries.",
  capture: "Captured INR 400.00. One transfer, two entries, committed with the payment's new state. The rest of the hold is released.",
  refund: "Refunded INR 150.00 as a new transfer. The capture's entries are left as they were.",
  check: "Read balances, transfers and payment totals in one consistent snapshot and compared them.",
  reset: "Ended this session. The ledger rows stay in the database.",
};

function logEvent(action, method, path, status, body, error) {
  let text = meanings[action];
  if (status === 202) text = "The gateway has not confirmed the outcome. Recovery will settle it; the page keeps checking.";
  if (body?.result?.status === "failed") text = `The gateway refused: ${body.result.failure_reason || "no reason given"}.`;
  if (error) text = error.message;
  events.push({ method, path, status, text, replayed: body?.replayed === true, at: new Date() });
  const list = byId("activity");
  list.replaceChildren();
  for (const event of [...events].reverse()) {
    const item = element("li", "event");
    const line = element("div", "event-line");
    const code = element("span", `event-code ${event.status >= 400 || !event.status ? "is-bad" : event.status === 202 ? "is-wait" : "is-ok"}`,
      event.status ? String(event.status) : "failed");
    // Shorten the payment ID so the operation at the end of the path stays visible.
    const path = element("span", "event-path", event.path.replace(/([0-9a-f]{8})-[0-9a-f-]{27}/, "$1..."));
    path.title = event.path;
    line.append(element("span", "event-method", event.method), path);
    if (event.replayed) line.append(element("span", "tag", "replayed"));
    line.append(code, element("span", "event-time", clock.format(event.at)));
    item.append(line, element("p", "event-text", event.text));
    list.append(item);
  }
  byId("empty-activity").hidden = events.length > 0;
  byId("activity").hidden = events.length === 0;
  byId("request-count").textContent = events.length ? `${events.length} ${events.length === 1 ? "request" : "requests"}` : "None yet";
  if (body) {
    byId("response-details").hidden = false;
    byId("response-json").textContent = JSON.stringify(body.result || body, null, 2);
  }
}

async function refreshPending() {
  clearTimeout(pendingTimer);
  const payment = snapshot?.payment;
  const refundPending = (payment?.refunds || []).some((refund) => refund.status === "pending");
  if (!payment || (!PENDING.includes(payment.status) && !refundPending)) return;
  pendingTimer = setTimeout(async () => {
    try {
      snapshot = (await request("/demo/state")).body;
      render();
      refreshPending();
    } catch (error) {
      notice(error.message, true);
    }
  }, 3000);
}

function focusNext() {
  const target = buttons.find((button) => button.classList.contains("action--primary") && !button.disabled)
    || buttons.find((button) => !button.disabled);
  target?.focus();
}

async function act(action) {
  if (busy) return;
  busy = true;
  render();
  notice("Sending request...");
  let method = "POST";
  let path = "/demo/payments";
  try {
    let response;
    if (action === "create") {
      const session = await request("/demo/session", "POST");
      if (session.body.expires_at) expiresAt = new Date(session.body.expires_at);
      response = await request(path, method);
    } else if (action === "retry") {
      response = await request(path, method);
    } else if (action === "check") {
      [method, path] = ["GET", "/demo/state"];
      response = await request(path);
      verified = true;
    } else if (action === "reset") {
      [method, path] = ["DELETE", "/demo/session"];
      response = await request(path, method);
    } else {
      path = `/demo/payments/${snapshot.payment.id}/${action}`;
      response = await request(path, method);
    }
    logEvent(action, method, path, response.status, response.body);
    if (action === "reset") {
      snapshot = null;
      expiresAt = null;
      verified = false;
      byId("response-details").hidden = true;
      notice("Session ended. Authorize a new payment to run the flow again.");
      return;
    }
    snapshot = response.body.snapshot || response.body;
    if (action !== "check") verified = false;
    const failedCheck = action === "check" && Object.values(snapshot.checks).some((value) => !value);
    if (response.status === 202) notice("Waiting on the gateway. This page will update when the outcome is known.");
    else if (snapshot.payment?.status === "failed") notice("The gateway refused the payment. The request log has the reason.", true);
    else if (failedCheck) notice("An accounting check failed for this session.", true);
    else notice({
      create: "Authorized. The merchant balance is still INR 0.00.",
      retry: "Replayed. Same payment, same result, nothing charged twice.",
      capture: "Captured INR 400.00. Both sides of the transfer are in the ledger.",
      refund: "Refunded INR 150.00. The merchant keeps INR 250.00.",
      check: "All three accounting checks passed.",
    }[action]);
    refreshPending();
  } catch (error) {
    logEvent(action, method, path, error.status, error.body, error.name === "AbortError" ? new Error("Timed out with no answer.") : error);
    if (error.body?.snapshot) snapshot = error.body.snapshot;
    if (error.status === 401) {
      snapshot = null;
      expiresAt = null;
      verified = false;
      notice("Your session ended, possibly because the server restarted. Authorize a new payment to begin again.", true);
    } else {
      notice(error.name === "AbortError" ? "No answer after a minute. Reload the page to see where the payment stands before trying again." : error.message, true);
    }
  } finally {
    busy = false;
    render();
    focusNext();
  }
}

for (const button of buttons) button.addEventListener("click", () => act(button.dataset.action));
byId("copy-id").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(snapshot.payment.id);
    byId("copy-id").textContent = "Copied";
    setTimeout(() => { byId("copy-id").textContent = "Copy ID"; }, 1500);
  } catch {
    notice("Could not copy. Select the ID and copy it by hand.", true);
  }
});

busy = true;
render();
// A cold start on the free host can take most of a minute.
const slowStart = setTimeout(() => notice("Waking the server. The first request after a quiet spell can take up to a minute."), 2500);
request("/demo/state")
  .then((response) => {
    snapshot = response.body;
    if (snapshot.payment) notice("Picked up your session where you left it.");
    else notice("");
    refreshPending();
  })
  .catch((error) => {
    if (error.status === 401) notice("");
    else notice("Could not reach the server. Try authorizing a payment; it will retry the connection.", true);
  })
  .finally(() => {
    clearTimeout(slowStart);
    busy = false;
    render();
  });
