"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tagName, hidden = false) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
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
    this.children.push(...children.map((child) =>
      typeof child === "string" ? { textContent: child } : child
    ));
  }

  replaceChildren(...children) {
    this.textContent = "";
    this.append(...children);
  }

  addEventListener(event, listener) {
    this.listeners.set(event, listener);
  }

  click() {
    return this.listeners.get("click")();
  }
}

function account(id, provider = "claude_personal", source = "manual") {
  return { id, provider, source, label: id, platform: "claude" };
}

async function main() {
  const [, , appPath, scenario] = process.argv;
  const ids = [
    "connection-banner", "cards", "refresh-btn", "add-account-form", "provider",
    "account-label", "credentials", "add-account-error", "signin-claude",
    "signin-chatgpt", "oauth-waiting", "oauth-error",
  ];
  const elements = new Map(ids.map((id) => [id, new Element("div", id.startsWith("oauth-"))]));
  const waiting = elements.get("oauth-waiting");
  const authorizeUrl = "https://example.invalid/authorize?state=claude";
  const old = account("old");
  const other = account("other", "chatgpt_personal");
  const initial = [old, other];
  const popup = { location: {} };
  const popupResult = scenario === "unblocked" ? popup : null;
  let opens = 0;
  const requests = [["GET", "/api/accounts", initial], ["GET", "/api/usage", []]];
  const context = vm.createContext({
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
      const request = requests.shift();
      assert.ok(request, `Unexpected request: ${url}`);
      const [method, path, body, opened] = request;
      assert.equal(options?.method ?? "GET", method);
      assert.equal(url, path);
      if (opened !== undefined) assert.equal(opens, opened, "tab opened before POST");
      return { ok: true, json: async () => body };
    },
    console: { error: (message, err) => { throw new Error(message, { cause: err }); } },
    setInterval() {},
  });
  vm.runInContext(fs.readFileSync(appPath, "utf8"), context, { filename: appPath });
  await new Promise((resolve) => setImmediate(resolve)); // dashboard's initial refresh

  function signIn(provider, url, opened) {
    requests.push(["POST", `/api/oauth/${provider}_personal/login`, { authorize_url: url }, opened]);
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
      await reload([other, account("oauth-new")]);
      assert.equal(waiting.classList.contains("hidden"), true, "new OAuth account clears status despite removal");
      break;
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
      await reload([...initial, account("oauth-chatgpt", "chatgpt_personal")]);
      assert.equal(waiting.classList.contains("hidden"), true);
      break;
    }
    default:
      throw new Error(`Unknown scenario: ${scenario}`);
  }
  assert.deepEqual(requests, []);
  process.stdout.write(JSON.stringify({ scenario, passed: true }));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
