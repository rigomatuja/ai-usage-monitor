"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tagName, hidden = false) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this.classes = new Set(hidden ? ["hidden"] : []);
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
      toggle: (name, force) => force ? this.classes.add(name) : this.classes.delete(name),
    };
    this.textContent = "";
  }

  set textContent(text) {
    this.text = text;
    this.children = [];
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join("");
  }

  append(...children) {
    for (const child of children) {
      this.children.push(typeof child === "string" ? { textContent: child } : child);
      if (child instanceof Element) child.parent = this;
    }
  }

  find(predicate) {
    for (const child of this.children) {
      if (!(child instanceof Element)) continue;
      if (predicate(child)) return child;
      const descendant = child.find(predicate);
      if (descendant) return descendant;
    }
    return null;
  }

  querySelector(selector) {
    return selector.startsWith(".")
      ? this.find((child) => child.classList.contains(selector.slice(1)) ||
        child.className?.split(" ").includes(selector.slice(1)))
      : this.find((child) => child.tagName === selector);
  }

  remove() {
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
  }

  setAttribute() {}
  focus() { focused = this; }
  reset() {}

  replaceChildren(...children) {
    this.textContent = "";
    this.append(...children);
  }

  addEventListener(event, listener) {
    this.listeners.set(event, listener);
  }

  dispatch(type, details = {}) {
    const event = { target: this, preventDefault() {}, ...details };
    const listener = this.listeners.get(type);
    return listener ? listener(event) : this.parent?.dispatch(type, event);
  }

  click() {
    return this.dispatch("click");
  }
}

let focused;
let hiddenAccounts = [];

function account(id, provider = "claude_personal", source = "manual") {
  return { id, provider, source, label: id, platform: "claude", default_label: id, custom_label: false };
}

const now = "2026-11-19T12:15:00Z";
const snapshot = {
  account_id: "old", status: "ok", fetched_at: "2026-11-19T12:10:00Z",
  windows: [
    { label: "Weekly", used_percent: 30, resets_at: "2026-11-21T15:45:00Z" },
    { label: "Session", used_percent: 10, resets_at: "2026-11-19T14:30:00Z" },
    { label: "Daily", used_percent: null, resets_at: "2026-11-19T12:30:00Z" },
    { label: "Previous", used_percent: 50, resets_at: "2026-11-19T12:00:00Z" },
    { label: "No active session", used_percent: 0, resets_at: null },
    { kind: "weekly", label: "Weekly limit", used_percent: 30, resets_at: "2026-11-21T15:45:00Z" },
    { kind: "weekly", label: "Weekly no reset", used_percent: 5, resets_at: null },
    { kind: "weekly", label: "Weekly far", used_percent: 5, resets_at: "2026-11-30T12:00:00Z" },
    { kind: "weekly", label: "Weekly past", used_percent: 5, resets_at: "2026-11-19T12:00:00Z" },
  ],
};

function button(node, text) {
  return node.find((child) => child.tagName === "button" && child.textContent === text);
}

async function exerciseAccounts(scenario, elements, requests, initial, poll) {
  const cards = elements.get("cards-claude");
  const [autoCard, manualCard] = cards.children;
  // Claude accounts are grouped apart from OpenAI ones, each group counted.
  assert.equal(elements.get("count-claude").textContent, String(cards.children.length));
  assert.equal(elements.get("count-openai").textContent, "0");
  // A platform with no connected account hides its whole column.
  const groupHidden = (platform) => elements.get(`group-${platform}`).hidden;
  assert.equal(groupHidden("claude"), false);
  assert.equal(groupHidden("openai"), true);
  assert.equal(elements.get("no-accounts").hidden, true);
  const [auto, manual] = initial;
  const hiddenPanel = elements.get("hidden-accounts");
  const hiddenList = elements.get("hidden-list");
  const openConfirmation = async (card, text) => {
    const remove = button(card, "Remove");
    assert.ok(remove, "Remove control exists");
    const renameControl = button(card, "Rename");
    await remove.click();
    assert.deepEqual(requests, [], "opening confirmation does not send a request");
    assert.equal(remove.hidden, true);
    assert.equal(renameControl.hidden, true);
    assert.match(card.textContent, text);
    assert.ok(button(card, "Confirm"));
    assert.ok(button(card, "Cancel"));
    return { remove, renameControl };
  };
  const heading = (card) => card.querySelector("h3").textContent;
  const rename = (card) => {
    const control = button(card, "Rename");
    assert.ok(control, "Rename control exists");
    control.click();
    const input = card.querySelector("input");
    assert.equal(input.value, heading(card), "editor starts with the current label");
    return input;
  };
  const patch = (account, label, response, status = 200) =>
    requests.push(["PATCH", `/api/accounts/${encodeURIComponent(account.id)}`, response, undefined,
      { payload: { label }, status }]);

  switch (scenario) {
    case "rename": {
      const input = rename(autoCard);
      assert.equal(button(autoCard, "Reset to default"), null);
      input.value = "  Work Claude  ";
      const updated = { ...auto, label: "Work Claude", custom_label: true };
      patch(auto, "Work Claude", updated);
      requests.push(["GET", "/api/accounts", [updated, manual]]);
      await input.dispatch("keydown", { key: "Enter" });
      const rendered = cards.children[0];
      assert.equal(heading(rendered), "Work Claude");
      assert.ok(rendered.textContent.includes("claude_personal · Claude (local login)"));
      assert.equal(rendered.querySelector("input"), null);
      break;
    }
    case "clear":
    case "reset-default": {
      const input = rename(manualCard);
      const reset = button(manualCard, "Reset to default");
      assert.ok(reset, "custom name can be reset directly");
      const updated = { ...manual, label: manual.default_label, custom_label: false };
      patch(manual, null, updated);
      requests.push(["GET", "/api/accounts", [auto, updated]]);
      if (scenario === "clear") {
        input.value = "  \t";
        await button(manualCard, "Save").click();
      } else {
        await reset.click();
      }
      assert.equal(heading(cards.children[1]), manual.default_label);
      assert.equal(cards.children[1].querySelector("input"), null);
      assert.equal(cards.children[1].textContent.split(manual.default_label).length, 2);
      break;
    }
    case "rename-error": {
      const input = rename(manualCard);
      input.value = "x".repeat(81);
      patch(manual, input.value, { detail: "Label must be at most 80 characters" }, 422);
      await button(manualCard, "Save").click();
      assert.equal(cards.children[1], manualCard);
      assert.equal(heading(manualCard), manual.label);
      assert.equal(manualCard.querySelector("input"), input);
      assert.equal(input.value, "x".repeat(81));
      assert.match(manualCard.textContent, /Label must be at most 80 characters/);
      requests.push(["GET", "/api/usage", [{ ...snapshot, account_id: manual.id }]]);
      poll();
      await new Promise((resolve) => setImmediate(resolve));
      assert.match(manualCard.querySelector(".card-status").textContent, /updated 5 min ago/);
      assert.equal(manualCard.querySelector("input"), input);
      assert.match(manualCard.textContent, /Label must be at most 80 characters/);
      await button(manualCard, "Cancel").dispatch("keydown", { key: "Escape" });
      assert.equal(manualCard.querySelector("input"), null);
      assert.doesNotMatch(manualCard.textContent, /Label must be at most 80 characters/);
      rename(manualCard).value = "unsaved";
      await button(manualCard, "Cancel").click();
      assert.equal(manualCard.querySelector("input"), null);
      assert.equal(heading(manualCard), manual.label);
      break;
    }
    case "add-blank":
    case "add-named": {
      const form = elements.get("add-account-form");
      elements.get("provider").value = "claude_personal";
      elements.get("account-label").value = scenario === "add-blank" ? "  " : "  Work Claude  ";
      elements.get("credentials").value = "credentials_path: fixture.json";
      const payload = { provider: "claude_personal", credentials: { credentials_path: "fixture.json" } };
      if (scenario === "add-named") payload.label = "Work Claude";
      const added = { ...account("added"), label: payload.label ?? "Claude" };
      requests.push(["POST", "/api/accounts", added, undefined, { payload }],
        ["GET", "/api/accounts", [...initial, added]], ["GET", "/api/usage", []]);
      await form.dispatch("submit");
      assert.equal(heading(cards.children[2]), added.label);
      break;
    }
    case "remove": {
      await openConfirmation(manualCard, /Remove this account\?/);
      requests.push(["DELETE", `/api/accounts/${manual.id}`, null, undefined, { status: 204 }],
        ["GET", "/api/accounts", [auto]], ["GET", "/api/usage", []]);
      await button(manualCard, "Confirm").click();
      assert.deepEqual(cards.children.map(heading), [auto.label]);
      assert.equal(hiddenPanel.classList.contains("hidden"), true);
      break;
    }
    case "remove-cancel": {
      for (const escape of [false, true]) {
        const { remove, renameControl } = await openConfirmation(manualCard, /Remove this account\?/);
        const cancel = button(manualCard, "Cancel");
        requests.push(["GET", "/api/usage", [{ ...snapshot, account_id: manual.id }]]);
        poll();
        await new Promise((resolve) => setImmediate(resolve));
        assert.match(manualCard.querySelector(".card-status").textContent, /updated 5 min ago/);
        assert.equal(button(manualCard, "Cancel"), cancel, "usage polling preserves confirmation");
        if (escape) await cancel.dispatch("keydown", { key: "Escape" });
        else await cancel.click();
        assert.equal(button(manualCard, "Confirm"), null);
        assert.equal(button(manualCard, "Cancel"), null);
        assert.equal(remove.hidden, false);
        assert.equal(renameControl.hidden, false);
        assert.equal(focused, remove);
        assert.deepEqual(cards.children.map(heading), [auto.label, manual.label]);
        assert.deepEqual(requests, []);
      }
      break;
    }
    case "remove-all": {
      await openConfirmation(manualCard, /Remove this account\?/);
      requests.push(["DELETE", `/api/accounts/${manual.id}`, null, undefined, { status: 204 }],
        ["GET", "/api/accounts", []], ["GET", "/api/usage", []]);
      await button(manualCard, "Confirm").click();
      assert.equal(groupHidden("claude"), true);
      assert.equal(groupHidden("openai"), true);
      assert.equal(elements.get("no-accounts").hidden, false);
      assert.equal(elements.get("count-claude").textContent, "0");
      assert.deepEqual(cards.children, []);
      break;
    }
    case "hide-auto": {
      await openConfirmation(autoCard, /Hide this local login\? You can restore it below\./);
      hiddenAccounts = [auto];
      requests.push(["DELETE", `/api/accounts/${auto.id}`, null, undefined, { status: 204 }],
        ["GET", "/api/accounts", [manual]], ["GET", "/api/usage", []]);
      await button(autoCard, "Confirm").click();
      assert.deepEqual(cards.children.map(heading), [manual.label]);
      assert.equal(hiddenPanel.classList.contains("hidden"), false);
      const [item] = hiddenList.children;
      assert.equal(item.tagName, "li");
      assert.ok(item.textContent.includes(auto.label));
      assert.ok(item.textContent.includes(auto.provider));
      assert.ok(button(item, "Restore"));
      break;
    }
    case "restore":
    case "restore-error": {
      const [login] = hiddenAccounts;
      assert.equal(hiddenPanel.classList.contains("hidden"), false);
      assert.deepEqual(cards.children.map(heading), [auto.label]);
      assert.equal(groupHidden("openai"), true);
      const [item] = hiddenList.children;
      if (scenario === "restore-error") {
        requests.push(["POST", `/api/accounts/${login.id}/restore`, { detail: "unknown account" }, undefined, { status: 404 }]);
        await button(item, "Restore").click();
        assert.equal(item.querySelector(".card-inline-error").textContent, "unknown account");
        requests.push(["POST", `/api/accounts/${login.id}/restore`, null, undefined, { offline: true }]);
        await button(item, "Restore").click();
        assert.equal(item.querySelector(".card-inline-error").textContent, "connection lost");
        assert.deepEqual(hiddenList.children, [item]);
        break;
      }
      hiddenAccounts = [];
      requests.push(["POST", `/api/accounts/${login.id}/restore`, login],
        ["POST", `/api/usage/refresh/${login.id}`, null, undefined, { status: 500 }],
        ["GET", "/api/accounts", [login, auto]], ["GET", "/api/usage", []]);
      await button(item, "Restore").click();
      assert.deepEqual(elements.get("cards-openai").children.map(heading), [login.label]);
      assert.equal(elements.get("count-openai").textContent, "1");
      assert.equal(groupHidden("openai"), false);
      assert.equal(hiddenPanel.classList.contains("hidden"), true);
      assert.deepEqual(hiddenList.children, []);
      break;
    }
    default:
      throw new Error(`Unknown account scenario: ${scenario}`);
  }
}

function exerciseDates(scenario, card) {
  if (scenario === "error-date") {
    const status = card.querySelector(".card-status");
    assert.ok(status.classList.contains("error"));
    assert.match(status.textContent,
      /^error: offline · updated 5 min ago · Thu 19 Nov, 13:10 (?:CET|GMT\+1)$/);
    return;
  }
  const windows = card.querySelector(".windows").children;
  if (scenario === "no-reset") {
    assert.equal(windows[4].querySelector(".window-reset"), null);
    assert.match(windows[4].textContent, /No active session/);
    return;
  }
  if (scenario === "weekly-ticks") {
    const all = (node, cls) => {
      const found = [];
      const walk = (n) => {
        for (const child of n.children) {
          if (!(child instanceof Element)) continue;
          if (child.className?.split(" ").includes(cls)) found.push(child);
          walk(child);
        }
      };
      walk(node);
      return found;
    };
    for (let i = 0; i < 5; i++) {
      for (const cls of ["week-tick", "week-legend", "week-reset-seg", "week-now"]) {
        assert.equal(all(windows[i], cls).length, 0, `window ${i} has no ${cls}`);
      }
    }
    const w = windows[5];
    assert.equal(all(w, "week-tick").length, 6);
    assert.deepEqual(all(w, "week-tick").map((t) => t.style.left),
      [1, 2, 3, 4, 5, 6].map((i) => `${(i / 7) * 100}%`));
    const labels = all(w, "week-legend")[0].children.filter((c) => c instanceof Element);
    assert.deepEqual(labels.map((l) => l.textContent), ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]);
    assert.deepEqual(labels.map((l) => l.className.split(" ").includes("is-reset")),
      [false, false, false, false, false, false, true]);
    assert.equal(all(w, "week-reset-seg").length, 1);
    const marker = all(w, "week-now");
    assert.equal(marker.length, 1);
    assert.equal(Number.parseFloat(marker[0].style.left).toFixed(2), "69.35");
    const bare = windows[6];
    assert.equal(all(bare, "week-tick").length, 6);
    for (const cls of ["week-legend", "week-reset-seg", "week-now"]) {
      assert.equal(all(bare, cls).length, 0, `no-reset window has no ${cls}`);
    }
    for (const i of [7, 8]) {
      assert.equal(all(windows[i], "week-tick").length, 6);
      assert.equal(all(windows[i], "week-legend").length, 1);
      assert.equal(all(windows[i], "week-now").length, 0, `window ${i} now outside range`);
    }
    return;
  }
  const resetText = (index) => windows[index].querySelector(".window-reset")?.textContent;
  assert.match(resetText(0), /^resets Sat 21 Nov, 16:45 (?:CET|GMT\+1) · in 2d 3h$/);
  assert.match(resetText(1), /^resets Thu 19 Nov, 15:30 (?:CET|GMT\+1) · in 2h 15m$/);
  assert.match(resetText(2), /^resets Thu 19 Nov, 13:30 (?:CET|GMT\+1) · in 15m$/);
  assert.match(resetText(3), /^resets Thu 19 Nov, 13:00 (?:CET|GMT\+1)$/);
  assert.match(card.querySelector(".card-status").textContent,
    /^ok · updated 5 min ago · Thu 19 Nov, 13:10 (?:CET|GMT\+1)$/);
}

async function main() {
  const [, , appPath, scenario] = process.argv;
  const ids = [
    "connection-banner", "cards", "cards-claude", "cards-openai", "count-claude",
    "count-openai", "refresh-btn", "add-account-form", "provider",
    "account-label", "credentials", "add-account-error", "signin-claude",
    "signin-chatgpt", "oauth-waiting", "oauth-error", "hidden-accounts", "hidden-list",
    "group-claude", "group-openai", "no-accounts",
  ];
  const elements = new Map(ids.map((id) => [id,
    new Element("div", id.startsWith("oauth-") || id === "hidden-accounts")]));
  const waiting = elements.get("oauth-waiting");
  const authorizeUrl = "https://example.invalid/authorize?state=claude";
  const old = account("old");
  const other = account("other", "chatgpt_personal");
  const dates = ["dates", "no-reset", "error-date", "weekly-ticks"].includes(scenario);
  const restoring = ["restore", "restore-error"].includes(scenario);
  const accounts = ["rename", "clear", "reset-default", "rename-error", "add-blank", "add-named",
    "remove", "remove-cancel", "remove-all", "hide-auto"].includes(scenario) || restoring;
  if (restoring) {
    hiddenAccounts = [{ ...account("auto-chatgpt-personal", "chatgpt_personal", "auto"),
      label: "Local ChatGPT", platform: "openai" }];
  }
  const initial = restoring ? [old] : accounts ? [
    { ...old, source: "auto", label: "Claude (local login)", default_label: "Claude (local login)" },
    { ...other, label: "Personal ChatGPT", default_label: "ChatGPT", custom_label: true },
  ] : [old, other];
  const popup = { location: {} };
  const popupResult = scenario === "unblocked" ? popup : null;
  let opens = 0;
  let attemptStatus = "pending";
  const usage = scenario === "error-date"
    ? [{ ...snapshot, status: "error", error: "offline", windows: [] }]
    : dates ? [snapshot] : [];
  const requests = [["GET", "/api/accounts", initial], ["GET", "/api/usage", usage]];
  let poll;
  const unexpected = [];
  const context = vm.createContext({
    Date: class extends Date { static now() { return Date.parse(now); } },
    document: {
      getElementById: (id) => elements.get(id),
      createElement: (tag) => new Element(tag),
    },
    window: {
      open(url, target) {
        assert.equal(url, "");
        assert.equal(target, "_blank");
        opens++;
        return popupResult;
      },
    },
    async fetch(url, options) {
      if (url === "/api/accounts/hidden" && (options?.method ?? "GET") === "GET") {
        return { ok: true, json: async () => hiddenAccounts };
      }
      if (url === `/api/oauth/attempts/attempt-${opens}`) {
        const messages = {
          failed: "Sign-in failed. Please try signing in again.",
          expired: "Sign-in expired. Please try signing in again.",
        };
        return {
          ok: true,
          headers: new Map([["X-OAuth-Error", messages[attemptStatus]]]),
          json: async () => ({ status: attemptStatus }),
        };
      }
      const request = requests.shift();
      if (!request) unexpected.push(url);
      assert.ok(request, `Unexpected request: ${url}`);
      const [method, path, body, opened, response = {}] = request;
      assert.equal(options?.method ?? "GET", method);
      assert.equal(url, path);
      if (opened !== undefined) assert.equal(opens, opened, "tab opened before POST");
      if (response.payload) {
        assert.equal(options.headers["Content-Type"], "application/json");
        assert.deepEqual(JSON.parse(options.body), response.payload);
      }
      if (response.offline) throw new TypeError("Fetch failed");
      const status = response.status ?? 200;
      return { ok: status < 400, status, json: async () => body };
    },
    console: { error: (message, err) => { throw new Error(message, { cause: err }); } },
    setInterval(callback) { poll = callback; },
  });
  vm.runInContext(fs.readFileSync(appPath, "utf8"), context, { filename: appPath });
  await new Promise((resolve) => setImmediate(resolve)); // dashboard's initial refresh

  function signIn(provider, url, opened) {
    requests.push(["POST", `/api/oauth/${provider}_personal/login`, { authorize_url: url, attempt_id: `attempt-${opened}` }, opened]);
    return elements.get(provider === "claude" ? "signin-claude" : "signin-chatgpt").click();
  }

  async function reload(accounts) {
    requests.push(["GET", "/api/accounts", accounts]);
    await context.loadAccounts();
  }

  function assertLink(url = authorizeUrl) {
    assert.equal(waiting.classList.contains("hidden"), false, "fallback stays visible");
    const link = waiting.children.find((child) => child.tagName === "a");
    assert.ok(link, "fallback link exists");
    assert.equal(link.textContent, "Open the sign-in page");
    assert.equal(link.href, url);
    assert.equal(link.target, "_blank");
    assert.equal(link.rel, "noopener");
  }

  if (accounts || dates) {
    if (accounts) await exerciseAccounts(scenario, elements, requests, initial, poll);
    else exerciseDates(scenario, elements.get("cards-claude").children[0]);
    assert.deepEqual(requests, []);
    assert.deepEqual(unexpected, []);
    process.stdout.write(JSON.stringify({ scenario, passed: true }));
    return;
  }

  await signIn("claude", authorizeUrl, 1);
  switch (scenario) {
    case "unblocked":
      assert.equal(popup.location.href, authorizeUrl);
      assert.equal(waiting.classList.contains("hidden"), false);
      assert.match(waiting.textContent, /Waiting for login to complete/);
      assert.equal(waiting.children.some((child) => child.tagName === "a"), false);
      break;
    case "blocked":
      assertLink();
      break;
    case "unchanged":
      await reload(initial);
      assertLink();
      break;
    case "unrelated":
      await reload([...initial, account("org", "anthropic_org")]);
      assertLink();
      break;
    case "auto":
      await reload([...initial, account("auto-new", "claude_personal", "auto")]);
      assertLink();
      break;
    case "replacement":
      attemptStatus = "completed";
      await reload([other, account("oauth-new")]);
      assert.equal(waiting.classList.contains("hidden"), true, "new OAuth account clears status despite removal");
      break;
    case "same-provider":
      await reload([...initial, account("manual-new")]);
      assertLink();
      attemptStatus = "completed";
      await reload([...initial, account("manual-new")]);
      assert.equal(waiting.classList.contains("hidden"), true);
      break;
    case "failed":
    case "expired": {
      assertLink();
      attemptStatus = scenario;
      await reload(initial);
      assert.equal(waiting.classList.contains("hidden"), true);
      assert.equal(waiting.textContent, "");
      const error = elements.get("oauth-error");
      assert.equal(error.classList.contains("hidden"), false);
      assert.equal(error.textContent, `Sign-in ${scenario}. Please try signing in again.`);
      break;
    }
    case "dismiss": {
      assertLink();
      const dismiss = waiting.children.find((child) => child.tagName === "button");
      assert.ok(dismiss, "dismiss button exists inside the waiting status");
      assert.equal(dismiss.textContent, "Dismiss");
      await dismiss.click();
      assert.equal(waiting.classList.contains("hidden"), true);
      await reload(initial);
      assert.equal(waiting.classList.contains("hidden"), true);
      break;
    }
    case "restart": {
      assertLink();
      const chatgptUrl = "https://example.invalid/authorize?state=chatgpt";
      await signIn("chatgpt", chatgptUrl, 2);
      assertLink(chatgptUrl);
      await reload([...initial, account("oauth-claude")]);
      assertLink(chatgptUrl);
      attemptStatus = "completed";
      await reload([...initial, account("oauth-chatgpt", "chatgpt_personal")]);
      assert.equal(waiting.classList.contains("hidden"), true);
      break;
    }
    default:
      throw new Error(`Unknown scenario: ${scenario}`);
  }
  assert.deepEqual(requests, []);
  assert.deepEqual(unexpected, []);
  process.stdout.write(JSON.stringify({ scenario, passed: true }));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
