# AI Usage Monitor — v1 (local web app)

Python app that shows current AI usage for Claude and ChatGPT/OpenAI accounts, in a local
web UI. v1 covers the platform-independent core; the OS desktop widget is a later version.
Status: done (feature branch feature/v1-web-monitor; merge is the user's decision).

## Specs

- S1: "montar una aplicacion usando python, que monte un widget compatible con windows, mac y linux (cualquier distro y cualquier escritorio kde o gnome) que muestre el consumo actual de AI" — v1 delivers the platform-independent part as a local web app; the desktop widget shell is deferred to a later version.
- S2: "subscripciones de claude" y "subscriciones de gpt" — accounts on both platforms.
- S3: "El progrmaa debe permitir conectar multipoles cuentas de cada plataforma y monitorear su uso en cualquier de las ventanas de consumo que usen." — multiple accounts per platform; every usage window each provider reports (5-hour, weekly, daily token/cost buckets).
- S4: "Dado que lo del widget puede resultar complejo, podemos valora montarlo en una pequeña aplicacion web y verlo en navegador apra la primera version" — v1 is a small local web app viewed in the browser.
- S5: Decision (user): v1 supports BOTH personal subscriptions (via local CLI logins) and organization/team accounts (via admin API keys).
- S6: Decision (user): multi-account via BOTH auto-detected local logins AND manually configured accounts.

## Tasks

- T1 (S1,S4): Scaffold: git branch, pyproject, package layout, shared models, provider base contract. Route: inline. Commit: 686eacf
- T2 (S2,S3,S5,S6): Claude personal provider (oauth/usage endpoint). Route: worker A. Commit: bdb4b28
- T3 (S2,S3,S5,S6): ChatGPT personal provider (wham/usage endpoint). Route: worker A. Commit: bdb4b28
- T4 (S5): Organization providers: Anthropic usage_report/messages; OpenAI usage/completions + costs. Route: worker B. Commit: f7c39e7
- T5 (S3,S6): Account registry (auto-detect + manual config), poller with cache, REST API, app wiring. Route: inline (parent). Commit: 6a54207
- T6 (S4): Web dashboard: single page, card per account, per-window progress bars, add-account form. Route: worker C. Commit: e98bf35
- T7 (S1-S6): Full test suite green, README, end-to-end verification with real local logins, work-unit commits. Route: inline (parent). Commit: e98bf35 (suite 118 passed; e2e: Claude weekly 8%, ChatGPT weekly 22% Pro plan; dashboard 200)

## Architecture (frozen contract for parallel work)

Python 3.11+ package `ai_usage_monitor` (src layout), FastAPI + uvicorn + httpx + pydantic v2
+ platformdirs. No build step for the frontend (vanilla HTML/CSS/JS served as static files).

```
src/ai_usage_monitor/
  models.py          # ProviderKind, WindowKind, UsageWindow, UsageSnapshot, AccountInfo, AccountConfig
  providers/base.py  # UsageProvider ABC + ProviderError; fetch(httpx.AsyncClient) -> list[UsageWindow]
  providers/claude_personal.py    # ClaudePersonalProvider
  providers/chatgpt_personal.py   # ChatGPTPersonalProvider
  providers/anthropic_org.py      # AnthropicOrgProvider
  providers/openai_org.py         # OpenAIOrgProvider
  registry.py         # auto-detect local logins + manual accounts from config file
  poller.py           # asyncio loop, >=300s interval, keeps last good snapshot on error
  api.py              # FastAPI router (contract below)
  app.py              # app factory + lifespan
  __main__.py         # python -m ai_usage_monitor [--host --port]
  web/static/         # index.html, app.js, styles.css
tests/                # pytest + pytest-asyncio; HTTP mocked via httpx.MockTransport
```

### Shared models (T1, authoritative)

```python
class ProviderKind(str, Enum):
    claude_personal = "claude_personal"    # Claude Pro/Max subscription
    chatgpt_personal = "chatgpt_personal"  # ChatGPT Plus/Pro subscription
    anthropic_org = "anthropic_org"        # Claude Team/Enterprise Admin API
    openai_org = "openai_org"              # OpenAI organization Admin usage API

class WindowKind(str, Enum):
    five_hour = "five_hour"
    weekly = "weekly"
    daily = "daily"      # org token/cost buckets (no percent)

class UsageWindow(BaseModel):
    kind: WindowKind
    label: str                      # e.g. "5-hour session", "Weekly", "Tokens (7d)"
    used_percent: float | None      # None for org daily buckets
    detail: str | None              # e.g. "Pro plan", "8.4M tokens · $12.40", "123 credits"
    resets_at: datetime | None      # None when unknown/not applicable

class UsageSnapshot(BaseModel):
    account_id: str
    fetched_at: datetime
    status: Literal["ok", "error"]
    error: str | None
    windows: list[UsageWindow]

class AccountInfo(BaseModel):
    id: str
    label: str
    provider: ProviderKind
    platform: Literal["claude", "openai"]
    source: Literal["auto", "manual"]

class AccountConfig(BaseModel):     # manual accounts, persisted as JSON (0600)
    id: str
    provider: ProviderKind
    label: str
    credentials: dict[str, str]    # provider-specific keys, see provider contracts
```

### Provider contract (T1, authoritative)

`UsageProvider.__init__(credentials: Mapping[str, str])` — must not perform I/O.
`async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]` — one provider call;
raises `ProviderError(message)` on any failure (bad credentials, HTTP error, unexpected
shape). NEVER refresh tokens (both CLIs own the refresh flow; racing them desyncs the CLI).
Percent values from providers are already 0–100; do not rescale.

### REST API contract (T5/T6 boundary)

- `GET  /api/accounts` -> `list[AccountInfo]`
- `GET  /api/usage` -> `list[UsageSnapshot]` (last good snapshot per account; error snapshots carry `status:"error"` and keep `windows` from last success when available)
- `POST /api/accounts` body `{provider, label, credentials}` -> `AccountInfo` (persisted; 422 on unknown provider)
- `DELETE /api/accounts/{id}` -> 204 (manual accounts only; auto accounts 409)
- `POST /api/usage/refresh/{account_id}` -> `UsageSnapshot` (immediate poll)
- `GET  /` and `/static/*` -> dashboard

### Provider endpoint details (researched 2026-11, sources in Log L4)

**Claude personal (T2)** — credentials: `{"credentials_path": "~/.claude/.credentials.json"}`
(default that path). File JSON: `claudeAiOauth.accessToken` (+ `subscriptionType`).
`GET https://api.anthropic.com/api/oauth/usage` with headers `Authorization: Bearer
<accessToken>`, `anthropic-beta: oauth-2025-04-20`, `User-Agent: claude-code/1.0.88`.
Response: prefer a `limits[]` array (`kind`: `session` → five_hour, `weekly_all` → weekly,
`weekly_scoped` → weekly with model name appended to label; fields `percent` 0–100,
`resets_at` nullable, `is_active`), fallback to top-level `five_hour` / `seven_day` objects
with `utilization` (0–100) + `resets_at`. Tolerant parsing: absent keys → window omitted,
never an error. Honor 429/`Retry-After` by raising `ProviderError` (poller keeps last good).

**ChatGPT personal (T3)** — credentials: `{"auth_path": "~/.codex/auth.json"}` (default that
path). File JSON: `tokens.access_token`, `tokens.account_id`.
`GET https://chatgpt.com/backend-api/wham/usage` with headers `Authorization: Bearer
<access_token>`, `ChatGPT-Account-Id: <account_id>`. Response: `plan_type`; `rate_limit.primary_window`
and `rate_limit.secondary_window` (nullable) each with `used_percent`, `limit_window_seconds`
(18000 → five_hour, 604800 → weekly; identify windows by duration, never by slot), `reset_at`;
optional `credits.balance` (a CREDIT COUNT, not dollars — label it "credits"). Tolerant parsing.

**Anthropic org (T4)** — credentials: `{"api_key": "sk-ant-admin..."}`.
`GET https://api.anthropic.com/v1/organizations/usage_report/messages?starting_at=<RFC3339
-7d>&bucket_width=1d&group_by=model&limit=7` with headers `x-api-key`, `anthropic-version:
2023-06-01`. Response: `buckets[].ending_at` + `buckets[].results[]` with `input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `model`. Aggregate: one `daily` window for the
last 24h bucket (detail: "X tokens (model top: M)") + one `daily` window for the 7-day total
(label "Tokens (7d)", detail "Y tokens"). No percent.

**OpenAI org (T4)** — credentials: `{"api_key": "sk-admin..."}`.
`GET https://api.openai.com/v1/organization/usage/completions?start_time=<unix>&end_time=
<unix>&limit=7&group_by[]=model` and `GET /v1/organization/costs?start_time=<unix>&limit=7`,
both `Authorization: Bearer <api_key>`. Response buckets `end_time` + `results[]` with
`input_tokens`, `output_tokens`, `num_model_requests`, `model` (usage) and `results[].cost`
/ `line_item` (costs; if cost shape varies, degrade gracefully to tokens-only). Aggregate
same as Anthropic org: daily window for 24h + 7d total (detail includes dollars when available).

## Log

- L1 (verbatim): "esto e sun proyecto nuevo. el objetivo del proyecto es montar una aplicacion usando python, que monte un widget compatible con windows, mac y linux (cualquier distro y cualquier escritorio kde o gnome) que muestre el consumo actual de AI: - subscripciones de claude - subscriciones de gpt / El progrmaa debe permitir conectar multipoles cuentas de cada plataforma y monitorear su uso en cualquier de las ventanas de consumo que usen. / Dado que lo del widget puede resultar complejo, podemos valora montarlo en una pequeña aplicacion web y verlo en navegador apra la primera version"
- L2 (decision): user chose "Ambas desde la v1" — both personal subscriptions and org/team admin API accounts in v1 (S5).
- L3 (decision): user chose "Ambos mecanismos" — auto-detected local logins plus manually configured accounts (S6).
- L4 (research): endpoints verified against solvr.dev provider survey and usageowl PROVIDERS.md (2026-09/07); Anthropic + OpenAI Admin API docs. Key rules: never refresh tokens; percent is 0–100; ChatGPT windows identified by limit_window_seconds; credits are not dollars; poll interval >= 300s; keep last good value on error.
- L5 (environment): system Python 3.14 externally managed, no pip/venv/uv; solved with `python3 -m venv --without-pip` + get-pip.py inside the venv (documented in README).
- L6 (evidence): e2e run 2026-11-18 with real local logins: GET /api/accounts lists both auto accounts; GET /api/usage returns Claude weekly 8% (+ scoped weekly 0%) and ChatGPT weekly 22% "Pro plan"; dashboard serves 200. Full suite: 118 passed. Tests mock all HTTP and never read real login files.
- L7 (defects found and fixed during integration): create_app ignored an externally supplied poller (tests triggered real provider calls); api.py closed-client AttributeError via request.app.state.http; test fixtures leaked real home logins (both paths now explicit per fixture).
- L8 (slice note): feature branch holds 5 work-unit commits (~3950 authored lines, fresh project); slice boundary recorded here per delivery guidance. Merge/push left to the user.
- L9 (independent verification, Risk: item 2 credentials): gentle-ai-verify passed all 8 claims but found one severe change-caused defect: accounts.json was created 0o664 (umask default) before chmod 0600 — transient credential exposure; plus advisory: FastAPI 422 echoed submitted credentials back. Both fixed (os.open with 0o600 at creation; RequestValidationError handler scrubbing "input"), pinned by tests/test_api_credential_leak.py (RED observed first). Suite: 120 passed. Verifier scratch dir /tmp/gpERSxig/gayCucKr/tmp.SsuKMTzAAG may be removed.
