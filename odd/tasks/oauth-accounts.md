# AI Usage Monitor — OAuth account login (multi-account personal subscriptions)

Adds browser-based OAuth login ("Sign in with Claude" / "Sign in with ChatGPT") so users
can connect MULTIPLE personal accounts per platform, beyond the single login stored in
each local CLI file. Builds on v1 (odd/tasks/v1-web-monitor.md, merged to main).
Status: done (feature branch feature/oauth-accounts; merge is the user's decision).

## Specs

- S1 (verbatim): "tambien hay que aportar la posibilidad de añadir cuentas con oauth, ya que en caso de tener varias cuentas de cada plataforma solo existe una en el archivo local" — OAuth login in the app to add extra personal accounts per platform.
- S2 (context, from v1 S3): multiple accounts per platform, each monitored with its own windows.
- S3 (derived): OAuth accounts are stored locally (0600 accounts.json) with their tokens; the app refreshes ONLY tokens it obtained itself (rotation-aware); CLI-owned file logins are still never refreshed or written.

## Tasks

- T1 (S1-S3): Branch, spec doc, registry support for token credentials + update_credentials. Route: inline (parent). Commit: 1374b3e (spec+branch; registry landed in T3's commit)
- T2 (S1,S3): oauth.py: PKCE/state, authorize URLs, code exchange, refresh (rotation-aware), ChatGPT identity JWT decode; provider support for inline tokens. Route: worker A. Commit: 1374b3e
- T3 (S1,S3): API: POST /api/oauth/{provider}/login, GET /api/oauth/claude/callback (on app port), ChatGPT temp listener on 1455, poller pre-fetch refresh. Route: worker B. Commit: 3f1b7e3
- T4 (S1,S2): UI: "Sign in with Claude"/"Sign in with ChatGPT" buttons opening the authorize URL. Route: worker C. Commit: 41b9317
- T5 (S1-S3): Integration, full suite green, README, work-unit commits. Route: inline (parent). Commit: 86da1e5 (suite 187 passed; e2e: both authorize URLs verified live with exact constants; 404 unknown provider; dashboard 200)

## Architecture (frozen contract for parallel work)

Package layout additions:

```
src/ai_usage_monitor/oauth.py   # OAuth flows for both providers (worker A)
src/ai_usage_monitor/providers/claude_personal.py   # + inline access_token credentials (worker A)
src/ai_usage_monitor/providers/chatgpt_personal.py  # + inline access_token+account_id (worker A)
src/ai_usage_monitor/registry.py  # update_credentials(account_id, credentials) (worker B)
src/ai_usage_monitor/api.py       # /api/oauth/* routes (worker B)
src/ai_usage_monitor/app.py       # wire oauth routes (worker B)
src/ai_usage_monitor/poller.py    # pre-fetch refresh for token accounts (worker B)
src/ai_usage_monitor/web/static/  # login buttons (worker C)
```

### Account storage (T1, authoritative)

An OAuth account is an AccountConfig with provider `claude_personal` or
`chatgpt_personal` whose credentials dict holds tokens directly (all string values):

- claude_personal OAuth: `{"access_token": "sk-ant-oat01-...", "refresh_token": "sk-ant-ort01-...", "expires_at": "<epoch ms as string>", "email": "..."}` (email optional, for label)
- chatgpt_personal OAuth: `{"access_token": "...", "refresh_token": "...", "expires_at": "<epoch ms as string>", "account_id": "...", "email": "..."}`

`AccountRegistry.update_credentials(account_id: str, credentials: dict[str,str]) -> None`
replaces the stored credentials of a MANUAL account and persists (0600, same rules as
_save_manual). Raises KeyError for unknown accounts. (T1/T3, worker B implements
persistence semantics; T1 provides the scaffold only if needed — registry.py belongs to
worker B in this feature.)

Provider rule (T2): when `credentials["access_token"]` is present, providers use it
directly and never touch the local login files; file paths remain the fallback. ChatGPT
provider needs `credentials["account_id"]` when `access_token` is inline.

Refresh rule (T2/T3): ONLY accounts whose stored credentials contain a `refresh_token`
(obtained via our OAuth flow) are refreshed — proactively before fetch when within 60 s
of expiry, and reactively on 401 (one attempt, then ProviderError; poller keeps last
good). Both providers rotate refresh tokens: ALWAYS persist the full new token response.
CLI-owned file logins keep the v1 rule: never refresh, never write.

### REST API additions (T3/T4 boundary)

- `POST /api/oauth/{provider}/login` -> 200 `{authorize_url: str}`; 404 unknown provider.
  Starts a login session (PKCE verifier + state, in-memory, single pending session per
  provider; a new call replaces the previous one). For chatgpt_personal it also starts a
  temporary callback listener on port 1455 (see constants).
- `GET /api/oauth/claude/callback?code=...&state=...` (redirect_uri target on the app
  port) -> exchanges the code, stores the account (label: email or "Claude OAuth"),
  returns a small HTML success page that redirects to `/` ; on state mismatch or exchange
  failure returns an HTML error page (status 400) — never a stack trace.
- ChatGPT callback: the temporary listener on 1455 serves `/auth/callback`, exchanges,
  stores the account, and redirects the browser to `http://127.0.0.1:<app port>/`.
- OAuth-created accounts are `source: "manual"` AccountInfo with provider kind as above
  (removable via existing DELETE).

### OAuth endpoint details (researched 2026-11, sources in Log L3)

**Claude (Sign in with Claude)** — public client of Claude Code:
- Authorize: `GET https://claude.ai/oauth/authorize` with params `code=true`,
  `client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e`, `response_type=code`,
  `redirect_uri=http://localhost:<app port>/api/oauth/claude/callback` (dynamic
  localhost ports are on the client's allow-list — the CLI itself uses one),
  `scope=user:profile user:inference`, `code_challenge` (base64url SHA-256 of verifier),
  `code_challenge_method=S256`, `state` (base64url, 43 chars, secrets-based).
- Code format twist: the returned code may be `{code}#{state}` — split on `#` and use the
  first part as the code (validate state matches, whether from query param or after `#`).
- Exchange: `POST https://console.anthropic.com/v1/oauth/token` JSON body
  `{grant_type: "authorization_code", code, state, client_id, redirect_uri, code_verifier}`.
- Response: `{token_type, access_token ("sk-ant-oat01-..."), expires_in (seconds, ~8h),
  refresh_token ("sk-ant-ort01-..."), scope, organization: {uuid, name},
  account: {uuid, email_address}}` — use `account.email_address` for the label.
- Refresh: `POST https://console.anthropic.com/v1/oauth/token` JSON
  `{grant_type: "refresh_token", refresh_token, client_id}` — refresh token ROTATES;
  persist the new one every time.

**ChatGPT (Sign in with ChatGPT, Codex client)** — public client of Codex CLI:
- Constants: `client_id=app_EMoamEEZ73f0CkXaXp7hrann`,
  `authorize https://auth.openai.com/oauth/authorize`,
  `token https://auth.openai.com/oauth/token`,
  `redirect_uri http://localhost:1455/auth/callback` (port 1455 is FIXED in the client's
  allow-list — the temporary listener MUST bind 1455; make the port injectable for tests
  with this default), `scope=openid profile email offline_access`.
- Authorize params additionally: `id_token_add_organizations=true`,
  `codex_cli_simplified_flow=true`, plus `state` and PKCE S256 `code_challenge`.
- Exchange: `POST` token endpoint, `Content-Type: application/x-www-form-urlencoded`,
  body `{grant_type: "authorization_code", client_id, code, code_verifier, redirect_uri}`.
- Response: `{access_token, refresh_token, expires_in (seconds)}`.
- Identity: decode the access_token JWT payload (base64url, NO signature verification —
  these are our own tokens) → `payload["https://api.openai.com/auth"]["chatgpt_account_id"]`
  (required; also `chatgpt_plan_type`), `payload["https://api.openai.com/profile"]["email"]`
  (label). The account_id is REQUIRED for the usage endpoint header.
- Refresh: same endpoint, form-encoded `{grant_type: "refresh_token", refresh_token,
  client_id}` — refresh token may rotate; persist whatever comes back. Refresh when
  within 60 s of expiry (store expires_at = now + expires_in*1000 as epoch ms).

## Log

- L1 (verbatim): "tambien hay que aportar la posibilidad de añadir cuentas con oauth, ya que en caso de tener varias cuentas de cada plataforma solo existe una en el archivo local"
- L2 (prior context): port default changed to 61524 ("podemos usar algun puerto raro como por ejemplo 61524"); v1 merged to main and pushed.
- L3 (research, 2026-11): Claude flow from akashmohan.com/writings/claude-code-oauth (authorize/token params, code#state twist, token response shape, refresh rotation); ChatGPT flow from openai/codex codex-rs login server.rs + langchain-ai/openwiki openai-chatgpt-oauth.ts (client id, fixed port 1455, form-encoded exchange, JWT identity claims, refresh rotation). Both flows verified against multiple independent implementations.
- L4 (evidence): suite 187 passed. Live e2e 2026-11-18 on port 61524: POST login returns correct authorize URLs for both providers (exact client ids, PKCE S256, Claude redirect_uri on the app port, ChatGPT codex_cli_simplified_flow), 404 for non-personal providers, dashboard 200. Full browser round-trip not exercised (requires human credential interaction).
- L5 (integration notes): worker A's oauth module returns expires_at as epoch-ms int; worker B coerces to str before persisting per the storage contract (all string values). __main__.py now passes app_port so --port flows into the Claude redirect_uri. Known limitation: if a ChatGPT login is started but never completed, port 1455 stays bound until app shutdown; a second login attempt in that window returns 503.
- L6 (native review BLOCKED, lineage review-0c4db34761377d9b): 4-lens ordinary review STARTed (risk, resilience, readability, reliability; tier medium, correction budget 200). Reviewer model (glm5.3 via pi_host_relay) produced NO text on 3 attempts (review-risk x2, review-resilience x1), each stopReason: length with all 32768 output tokens consumed by reasoning, 0 text chars. This is a reviewer-model output-budget failure on this candidate's prompts, not a candidate defect; nothing was admitted and no lens slot was consumed. Capture loop stopped per the no-retry rule for unavailable verifiers. Human decision pending: retry later / disable review switch for this clone (ordinary delivery policy) / maintainer inspection of the lineage.
- L7 (retry, lineage review-aa37fd09a43b89dc, target 8cecd6cb): user chose option 1 (retry). Fresh START + group capture failed identically: review-resilience produced no text (stopReason: length, 32768/32768 tokens in reasoning, 0 text). Fourth identical reviewer-model failure. Retry exhausted; reviewer remains unavailable. Pending human decision: wait and retry at a later time (model may change), disable review switch clone-scoped (ordinary delivery policy: merge+push on suite+e2e evidence), or maintainer inspection of lineages review-0c4db34761377d9b / review-aa37fd09a43b89dc.
