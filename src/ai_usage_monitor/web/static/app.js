/* AI Usage Monitor dashboard (T6, S4) — vanilla ES2020, no frameworks.
 * Built against the REST contract:
 *   GET    /api/accounts                  -> [{id, label, provider, platform, source}]
 *   GET    /api/usage                     -> [{account_id, fetched_at, status, error, windows}]
 *   POST   /api/accounts                  {provider, label, credentials} -> AccountInfo
 *   DELETE /api/accounts/{id}             -> 204 (409 for auto accounts)
 *   POST   /api/usage/refresh/{id}        -> UsageSnapshot
 * Credentials are sent to the server only; they are never rendered back.
 */
"use strict";

const POLL_INTERVAL_MS = 60 * 1000; // server caches and polls providers at >= 300 s

let accounts = [];
let connectionLost = false;

const $ = (id) => document.getElementById(id);

const els = {
  banner: $("connection-banner"),
  cards: $("cards"),
  refreshBtn: $("refresh-btn"),
  form: $("add-account-form"),
  provider: $("provider"),
  label: $("account-label"),
  credentials: $("credentials"),
  formError: $("add-account-error"),
  signinClaude: $("signin-claude"),
  signinChatgpt: $("signin-chatgpt"),
  oauthWaiting: $("oauth-waiting"),
  oauthError: $("oauth-error"),
};

/* --- connection banner ----------------------------------------------------- */

function setConnectionLost(lost) {
  if (lost === connectionLost) return;
  connectionLost = lost;
  els.banner.classList.toggle("hidden", !lost);
}

/* --- fetch helpers ---------------------------------------------------------- */

async function fetchJson(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (err) {
    setConnectionLost(true); // backend down / network failure
    throw err;
  }
  setConnectionLost(false); // any successful HTTP exchange clears the banner
  return res;
}

async function getJson(url) {
  const res = await fetchJson(url);
  if (!res.ok) {
    throw new Error(`GET ${url} failed: HTTP ${res.status}`);
  }
  return res.json();
}

function serverErrorMessage(res, body) {
  // FastAPI-style {"detail": ...} or plain text
  if (body && typeof body === "object" && body.detail) {
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  }
  return (body && typeof body === "object" && body.error) || res.statusText || `HTTP ${res.status}`;
}

async function postJson(url, body) {
  const res = await fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let parsed = null;
  try {
    parsed = await res.json();
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) {
    const err = new Error(serverErrorMessage(res, parsed));
    err.status = res.status;
    throw err;
  }
  return parsed;
}

/* --- time formatting -------------------------------------------------------- */

function fmtLocalTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtRelativeAge(iso) {
  const secs = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  const rem = mins % 60;
  if (hours < 24) return rem ? `${hours} h ${rem} min ago` : `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return `${days} d ago`;
}

function fmtResets(iso) {
  const target = new Date(iso).getTime();
  const diffMs = target - Date.now();
  if (diffMs <= 0) return "reset";
  const mins = Math.floor(diffMs / 60000);
  if (mins < 24 * 60) {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return m ? `resets in ${h}h ${m}m` : `resets in ${m}m`;
  }
  return `resets at ${fmtLocalTime(iso)}`;
}

/* --- rendering -------------------------------------------------------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fillClassForPercent(pct) {
  if (pct >= 80) return "red";
  if (pct >= 50) return "amber";
  return "green";
}

function renderWindow(win) {
  if (win.used_percent === null || win.used_percent === undefined) {
    // org daily token/cost buckets: plain stat line, no bar
    const line = el("div", "stat-line");
    line.append(el("span", "window-label", win.label));
    line.append(el("span", "window-detail", win.detail || ""));
    return line;
  }
  const box = el("div", "window");
  const pct = Math.max(0, Math.min(100, win.used_percent));
  const head = el("div", "window-head");
  head.append(el("span", "window-label", win.label));
  head.append(el("span", "window-percent", `${Math.round(pct)}%`));
  const bar = el("div", "progress-bar");
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-valuenow", String(Math.round(pct)));
  const fill = el("div", `progress-fill ${fillClassForPercent(win.used_percent)}`);
  fill.style.width = `${pct}%`;
  bar.append(fill);
  box.append(head, bar);
  if (win.detail) box.append(el("div", "window-detail", win.detail));
  if (win.resets_at) box.append(el("div", "window-reset", fmtResets(win.resets_at)));
  return box;
}

function renderCard(account, snapshot) {
  const card = el("article", "card");
  card.dataset.accountId = account.id;

  const head = el("div", "card-head");
  head.append(el("h3", null, account.label));
  head.append(el("span", `badge platform-${account.platform}`, account.platform));
  head.append(el("span", "source-badge", account.source));
  head.append(el("span", "provider-kind", account.provider));
  card.append(head);

  const status = el("div", "card-status");
  if (snapshot && snapshot.status === "error") {
    status.classList.add("error");
    status.textContent = `error: ${snapshot.error || "unknown error"}`;
  } else if (snapshot && snapshot.fetched_at) {
    status.classList.add("ok");
    status.textContent = `ok · updated ${fmtRelativeAge(snapshot.fetched_at)}`;
  } else {
    status.classList.add("ok");
    status.textContent = "no usage data yet";
  }
  card.append(status);

  if (snapshot && snapshot.windows && snapshot.windows.length) {
    const stale = snapshot.status === "error";
    const windows = el("div", stale ? "windows stale" : "windows");
    for (const win of snapshot.windows) windows.append(renderWindow(win));
    if (stale) {
      const tag = el("span", "stale-tag", "stale");
      card.querySelector(".card-status").append(tag);
    }
    card.append(windows);
  }

  if (account.source === "manual") {
    const footer = el("div", "card-footer");
    const removeBtn = el("button", "remove-btn", "Remove");
    removeBtn.type = "button";
    removeBtn.addEventListener("click", () => removeAccount(account.id, card));
    footer.append(removeBtn);
    card.append(footer);
  }

  return card;
}

function renderAll() {
  els.cards.textContent = "";
  if (!accounts.length) {
    els.cards.append(el("div", "empty-state", "No accounts configured yet. Add one below."));
    return;
  }
  const snapshots = new Map(
    (lastUsage || []).map((snap) => [snap.account_id, snap])
  );
  for (const account of accounts) {
    els.cards.append(renderCard(account, snapshots.get(account.id) || null));
  }
}

/* --- data loading ----------------------------------------------------------- */

let lastUsage = [];

async function loadAccounts() {
  accounts = await getJson("/api/accounts");
  // Any successful accounts fetch means a pending OAuth login is either done
  // (the callback stored the account) or moot — clear the waiting status.
  els.oauthWaiting.classList.add("hidden");
}

async function loadUsage() {
  lastUsage = await getJson("/api/usage");
}

async function refreshAll() {
  await loadAccounts();
  await loadUsage();
  renderAll();
}

/* --- actions ----------------------------------------------------------------- */

async function onRefreshClick() {
  els.refreshBtn.disabled = true;
  try {
    for (const account of accounts) {
      try {
        await postJson(`/api/usage/refresh/${encodeURIComponent(account.id)}`);
      } catch (err) {
        console.error(`refresh failed for ${account.id}:`, err);
      }
    }
    await refreshAll();
  } finally {
    els.refreshBtn.disabled = false;
  }
}

async function removeAccount(id, card) {
  let res;
  try {
    res = await fetchJson(`/api/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (err) {
    showCardError(card, "connection lost");
    return;
  }
  if (res.status === 204) {
    await refreshAll();
    return;
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* ignore */
  }
  showCardError(card, serverErrorMessage(res, body)); // e.g. 409 for auto accounts
}

function showCardError(card, message) {
  const existing = card.querySelector(".card-inline-error");
  if (existing) existing.remove();
  card.append(el("div", "card-inline-error", message));
}

function parseCredentials(text) {
  // one "key: value" per line -> object; keys come from the documented help text
  const out = {};
  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const idx = line.indexOf(":");
    if (idx <= 0) continue;
    out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return out;
}

async function onAddSubmit(event) {
  event.preventDefault();
  els.formError.classList.add("hidden");
  const payload = {
    provider: els.provider.value,
    label: els.label.value.trim(),
    credentials: parseCredentials(els.credentials.value),
  };
  try {
    await postJson("/api/accounts", payload);
  } catch (err) {
    els.formError.textContent = err.message; // server error message, shown inline
    els.formError.classList.remove("hidden");
    return;
  }
  els.form.reset();
  await refreshAll();
}

/* --- OAuth sign in ------------------------------------------------------------ */

// POST /api/oauth/{provider}/login -> 200 {authorize_url}; 404 unknown provider.
// The authorize URL comes from the server; app.js never hardcodes one.
const OAUTH_LOGIN_PATHS = {
  claude: "/api/oauth/claude_personal/login",
  chatgpt: "/api/oauth/chatgpt_personal/login",
};

function clearOauthStatus() {
  els.oauthWaiting.classList.add("hidden");
  els.oauthError.classList.add("hidden");
}

function showOauthError(message) {
  els.oauthWaiting.classList.add("hidden");
  els.oauthError.textContent = message; // server error message, shown inline
  els.oauthError.classList.remove("hidden");
}

async function startOauthLogin(providerKey) {
  const tab = window.open("", "_blank");
  clearOauthStatus(); // clicking again resets any previous status
  let res;
  let body = null;
  try {
    res = await fetchJson(OAUTH_LOGIN_PATHS[providerKey], { method: "POST" });
    body = await res.json();
  } catch (err) {
    if (tab) tab.close();
    showOauthError("connection lost");
    return;
  }
  if (!res.ok) {
    if (tab) tab.close();
    showOauthError(serverErrorMessage(res, body));
    return;
  }
  if (!body || !body.authorize_url) {
    if (tab) tab.close();
    showOauthError("server did not return an authorize URL");
    return;
  }
  els.oauthWaiting.textContent = "Waiting for login to complete\u2026";
  if (tab) {
    tab.location.href = body.authorize_url;
  } else {
    const link = document.createElement("a");
    link.href = body.authorize_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open the sign-in page";
    els.oauthWaiting.replaceChildren(link);
  }
  els.oauthWaiting.classList.remove("hidden");
}

/* --- wiring ------------------------------------------------------------------ */

els.refreshBtn.addEventListener("click", onRefreshClick);
els.form.addEventListener("submit", onAddSubmit);
els.signinClaude.addEventListener("click", () => startOauthLogin("claude"));
els.signinChatgpt.addEventListener("click", () => startOauthLogin("chatgpt"));

refreshAll().catch((err) => console.error("initial load failed:", err));
setInterval(() => {
  loadUsage()
    .then(renderAll)
    .catch((err) => console.error("usage poll failed:", err));
}, POLL_INTERVAL_MS);
