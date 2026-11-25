/* AI Usage Monitor dashboard (T6, S4) — vanilla ES2020, no frameworks.
 * Built against the REST contract:
 *   GET    /api/accounts                  -> [{id, label, default_label, custom_label, provider, platform, source}]
 *   GET    /api/usage                     -> [{account_id, fetched_at, status, error, windows}]
 *   POST   /api/accounts                  {provider, label?, credentials} -> AccountInfo
 *   PATCH  /api/accounts/{id}             {label: string | null} -> AccountInfo
 *   DELETE /api/accounts/{id}            -> 204 (auto accounts are hidden)
 *   GET    /api/accounts/hidden          -> [AccountInfo]
 *   POST   /api/accounts/{id}/restore    -> AccountInfo
 *   POST   /api/usage/refresh/{id}        -> UsageSnapshot
 * Credentials are sent to the server only; they are never rendered back.
 */
"use strict";

const POLL_INTERVAL_MS = 60 * 1000; // server caches and polls providers at >= 300 s

let accounts = [];
let hiddenAccounts = [];
let pendingOauth = null;
let connectionLost = false;

const $ = (id) => document.getElementById(id);

const els = {
  banner: $("connection-banner"),
  groups: {
    claude: { cards: $("cards-claude"), count: $("count-claude"), name: "Claude" },
    openai: { cards: $("cards-openai"), count: $("count-openai"), name: "OpenAI" },
  },
  refreshBtn: $("refresh-btn"),
  form: $("add-account-form"),
  provider: $("provider"),
  label: $("account-label"),
  credentials: $("credentials"),
  formError: $("add-account-error"),
  hiddenAccounts: $("hidden-accounts"),
  hiddenList: $("hidden-list"),
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

const localDateTime = new Intl.DateTimeFormat(undefined, {
  weekday: "short", day: "numeric", month: "short",
  hour: "2-digit", minute: "2-digit", timeZoneName: "short",
});

function fmtLocalDateTime(iso) {
  const parts = localDateTime.formatToParts(new Date(iso));
  const { weekday, day, month, hour, minute, dayPeriod, timeZoneName } =
    Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  const time = `${hour}:${minute}${dayPeriod ? ` ${dayPeriod}` : ""}`;
  return `${weekday} ${day} ${month}, ${time} ${timeZoneName}`;
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
  const absolute = `resets ${fmtLocalDateTime(iso)}`;
  const diffMs = new Date(iso).getTime() - Date.now();
  if (diffMs <= 0) return absolute;
  const mins = Math.floor(diffMs / 60000);
  const hours = Math.floor(mins / 60);
  const relative = hours >= 24 ? `${Math.floor(hours / 24)}d ${hours % 24}h`
    : hours ? `${hours}h ${mins % 60}m` : `${mins}m`;
  return `${absolute} · in ${relative}`;
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
  const reset = win.resets_at ? el("div", "window-reset", fmtResets(win.resets_at)) : null;
  if (win.used_percent === null || win.used_percent === undefined) {
    // org daily token/cost buckets: plain stat line, no bar
    const line = el("div", "stat-line");
    line.append(el("span", "window-label", win.label));
    line.append(el("span", "window-detail", win.detail || ""));
    if (reset) line.append(reset);
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
  if (reset) box.append(reset);
  return box;
}

function renderCard(account, snapshot) {
  const card = el("article", "card");
  card.dataset.accountId = account.id;

  const head = el("div", "card-head");
  const title = el("h3", null, account.label);
  title.title = account.label;
  head.append(title);
  head.append(el("span", "source-badge", account.source));
  card.append(head);
  card.append(el("div", "card-default-label", account.custom_label
    ? `${account.provider} · ${account.default_label}`
    : account.provider));

  const usage = el("div", "card-usage");
  renderCardUsage(usage, snapshot);
  card.append(usage);

  const footer = el("div", "card-footer");
  const renameBtn = el("button", "rename-btn", "Rename");
  renameBtn.type = "button";
  const removeBtn = el("button", "remove-btn", "Remove");
  removeBtn.type = "button";
  renameBtn.addEventListener("click", () => openRenameEditor(account, footer, renameBtn, removeBtn));
  removeBtn.addEventListener("click", () => openRemoveConfirmation(account, card, footer, renameBtn, removeBtn));
  footer.append(renameBtn, removeBtn);
  card.append(footer);

  return card;
}

function renderCardUsage(usage, snapshot) {
  const status = el("div", "card-status");
  if (snapshot && snapshot.status === "error") {
    status.classList.add("error");
    status.textContent = `error: ${snapshot.error || "unknown error"}`;
  } else if (snapshot && snapshot.fetched_at) {
    status.classList.add("ok");
    status.textContent = "ok";
  } else {
    status.classList.add("pending");
    status.textContent = "no usage data yet";
  }
  if (snapshot?.fetched_at) {
    const fetchedAt = snapshot.fetched_at;
    status.textContent += ` · updated ${fmtRelativeAge(fetchedAt)} · ${fmtLocalDateTime(fetchedAt)}`;
  }
  usage.replaceChildren(status);

  if (snapshot && snapshot.windows && snapshot.windows.length) {
    const stale = snapshot.status === "error";
    const windows = el("div", stale ? "windows stale" : "windows");
    for (const win of snapshot.windows) windows.append(renderWindow(win));
    if (stale) status.append(el("span", "stale-tag", "stale"));
    usage.append(windows);
  }
}

function snapshotsByAccount() {
  return new Map(lastUsage.map((snap) => [snap.account_id, snap]));
}

// Claude/Anthropic accounts in one group, ChatGPT/OpenAI in the other.
function groupFor(account) {
  return account.platform === "claude" ? els.groups.claude : els.groups.openai;
}

function renderAll() {
  const snapshots = snapshotsByAccount();
  for (const group of Object.values(els.groups)) group.cards.textContent = "";
  for (const account of accounts) {
    groupFor(account).cards.append(renderCard(account, snapshots.get(account.id) || null));
  }
  for (const group of Object.values(els.groups)) {
    const count = group.cards.children.length;
    group.count.textContent = String(count);
    if (!count) {
      group.cards.append(el("div", "empty-state", `No ${group.name} accounts yet. Connect one below.`));
    }
  }
  renderHiddenAccounts();
}

function renderHiddenAccounts() {
  els.hiddenList.replaceChildren();
  for (const account of hiddenAccounts) {
    const item = el("li");
    const row = el("div", "hidden-row");
    const login = el("div", "hidden-login");
    login.append(el("div", null, account.label), el("div", "hidden-provider", account.provider));
    const restoreBtn = el("button", "rename-btn", "Restore");
    restoreBtn.type = "button";
    restoreBtn.addEventListener("click", async () => {
      restoreBtn.disabled = true;
      try {
        await restoreAccount(account.id);
      } catch (err) {
        showCardError(item, connectionLost ? "connection lost" : err.message);
      } finally {
        restoreBtn.disabled = false;
      }
    });
    row.append(login, restoreBtn);
    item.append(row);
    els.hiddenList.append(item);
  }
  els.hiddenAccounts.classList.toggle("hidden", hiddenAccounts.length === 0);
}

/* --- data loading ----------------------------------------------------------- */

let lastUsage = [];

async function loadAccounts() {
  accounts = await getJson("/api/accounts");
  hiddenAccounts = await getJson("/api/accounts/hidden").catch(() => []);
  const attempt = pendingOauth;
  if (!attempt?.id) return;
  const res = await fetchJson(`/api/oauth/attempts/${encodeURIComponent(attempt.id)}`, { cache: "no-store" }).catch(() => null);
  const result = res?.ok ? await res.json().catch(() => null) : null;
  if (pendingOauth !== attempt) return;
  if (result?.status === "completed") clearOauthStatus();
  if (result?.status === "failed" || result?.status === "expired") {
    showOauthError(res.headers.get("X-OAuth-Error"));
  }
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

function openRenameEditor(account, footer, renameBtn, removeBtn) {
  const editor = el("div", "rename-editor");
  const input = el("input");
  input.type = "text";
  input.setAttribute("aria-label", "Account label");
  input.value = account.label;
  editor.append(input);

  let saving = false;
  const close = () => {
    if (saving) return;
    editor.remove();
    removeBtn.disabled = false;
    renameBtn.hidden = false;
    renameBtn.focus();
  };
  const buttons = [];
  const addButton = (text, action) => {
    const button = el("button", "rename-btn", text);
    button.type = "button";
    button.addEventListener("click", action);
    buttons.push(button);
    editor.append(button);
  };
  const save = async (label) => {
    if (saving) return;
    saving = input.disabled = true;
    for (const button of buttons) button.disabled = true;
    try {
      const res = await fetchJson(`/api/accounts/${encodeURIComponent(account.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      if (!res.ok) {
        showCardError(editor, serverErrorMessage(res, await res.json().catch(() => null)));
        return;
      }
      await loadAccounts();
      renderAll();
    } catch (err) {
      showCardError(editor, connectionLost ? "connection lost" : err.message);
    } finally {
      saving = input.disabled = false;
      for (const button of buttons) button.disabled = false;
      if (editor.isConnected) input.focus();
    }
  };
  const saveInput = () => save(input.value.trim() || null);
  addButton("Save", saveInput);
  addButton("Cancel", close);
  if (account.custom_label) addButton("Reset to default", () => save(null));
  editor.addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "Enter" && event.target === input) {
      event.preventDefault();
      return saveInput();
    }
  });

  removeBtn.disabled = true;
  renameBtn.hidden = true;
  footer.append(editor);
  input.focus();
}

function openRemoveConfirmation(account, card, footer, renameBtn, removeBtn) {
  const confirmation = el("div", "remove-confirmation");
  const prompt = account.source === "auto"
    ? "Hide this local login? You can restore it below."
    : "Remove this account?";
  const confirm = el("button", "remove-btn remove-confirm-btn", "Confirm");
  const cancel = el("button", "rename-btn", "Cancel");
  confirm.type = cancel.type = "button";
  const close = () => {
    if (confirm.disabled) return;
    confirmation.remove();
    renameBtn.hidden = removeBtn.hidden = false;
    removeBtn.focus();
  };
  cancel.addEventListener("click", close);
  confirmation.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.isComposing) {
      event.preventDefault();
      close();
    }
  });
  confirm.addEventListener("click", async () => {
    confirm.disabled = cancel.disabled = true;
    try {
      await removeAccount(account.id, card);
    } finally {
      confirm.disabled = cancel.disabled = false;
      if (confirmation.isConnected) cancel.focus();
    }
  });
  renameBtn.hidden = removeBtn.hidden = true;
  confirmation.append(el("span", "remove-prompt", prompt), confirm, cancel);
  footer.append(confirmation);
  cancel.focus();
}

async function removeAccount(id, card) {
  try {
    const res = await fetchJson(`/api/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (res.status === 204) {
      await refreshAll();
    } else {
      showCardError(card, serverErrorMessage(res, await res.json().catch(() => null)));
    }
  } catch (err) {
    showCardError(card, connectionLost ? "connection lost" : err.message);
  }
}

async function restoreAccount(id) {
  const encodedId = encodeURIComponent(id);
  await postJson(`/api/accounts/${encodedId}/restore`);
  await postJson(`/api/usage/refresh/${encodedId}`).catch(() => {});
  await refreshAll();
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
    credentials: parseCredentials(els.credentials.value),
  };
  const label = els.label.value.trim();
  if (label) payload.label = label;
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

// Dashboard-origin POST -> 200 {authorize_url, attempt_id}; 404 unknown provider.
// The authorize URL comes from the server; app.js never hardcodes one.
const OAUTH_LOGIN_PATHS = {
  claude_personal: "/api/oauth/claude_personal/login",
  chatgpt_personal: "/api/oauth/chatgpt_personal/login",
};

function clearOauthStatus() {
  pendingOauth = null;
  els.oauthWaiting.classList.add("hidden");
  els.oauthWaiting.textContent = "";
  els.oauthError.classList.add("hidden");
}

function showOauthError(message) {
  clearOauthStatus();
  els.oauthError.textContent = message; // server error message, shown inline
  els.oauthError.classList.remove("hidden");
}

async function startOauthLogin(provider) {
  const tab = window.open("", "_blank");
  clearOauthStatus(); // clicking again resets any previous status
  const attempt = {};
  pendingOauth = attempt;
  let res;
  let body = null;
  try {
    res = await fetchJson(OAUTH_LOGIN_PATHS[provider], { method: "POST" });
    body = await res.json();
  } catch (err) {
    if (tab) tab.close();
    if (pendingOauth === attempt) showOauthError("connection lost");
    return;
  }
  if (pendingOauth !== attempt) {
    if (tab) tab.close();
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
  attempt.id = body.attempt_id;
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
  const dismiss = el("button", null, "Dismiss");
  dismiss.type = "button";
  dismiss.addEventListener("click", clearOauthStatus);
  els.oauthWaiting.append(" ", dismiss);
  els.oauthWaiting.classList.remove("hidden");
}

/* --- wiring ------------------------------------------------------------------ */

els.refreshBtn.addEventListener("click", onRefreshClick);
els.form.addEventListener("submit", onAddSubmit);
els.signinClaude.addEventListener("click", () => startOauthLogin("claude_personal"));
els.signinChatgpt.addEventListener("click", () => startOauthLogin("chatgpt_personal"));

refreshAll().catch((err) => console.error("initial load failed:", err));
setInterval(() => {
  loadUsage()
    .then(() => {
      const cards = Object.values(els.groups).flatMap((group) => [...group.cards.children]
        .filter((node) => node.dataset?.accountId));
      // Refresh usage without replacing card controls or hidden login rows.
      const snapshots = snapshotsByAccount();
      for (const card of cards) {
        const usage = card.querySelector(".card-usage");
        if (usage) renderCardUsage(usage, snapshots.get(card.dataset.accountId) || null);
      }
    })
    .catch((err) => console.error("usage poll failed:", err));
}, POLL_INTERVAL_MS);
