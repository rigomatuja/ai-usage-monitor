# AI Usage Monitor

Local web app that shows current AI usage for Claude and ChatGPT/OpenAI
accounts: subscription usage windows (5-hour, weekly) and organization
token/cost usage, across multiple accounts per platform.

## Features (v1)

- **Personal subscriptions**: auto-detects local logins of Claude Code
  (`~/.claude/.credentials.json`) and Codex CLI (`~/.codex/auth.json`) and
  reads their live usage windows (5-hour session, weekly). Claude always shows
  its 5-hour session window (0% "No active session" when none is running);
  ChatGPT shows it only when the plan reports one.
- **Organization accounts**: add Claude Team/Enterprise or OpenAI org accounts
  via an Admin API key; shows token and cost usage (24h and 7d).
- **Multiple accounts per platform**: auto-detected logins plus any number of
  manually added accounts (e.g. a second Claude login via its own
  credentials path, or several org keys).
- **Local web dashboard**: one card per account with colored progress bars,
  reset times (weekday, date and time in your browser's time zone, plus a
  countdown), stale-data markers, and controls to add or remove accounts.
  Any account, including auto-detected ones, can be renamed from its card (**Rename**);
  clearing the name restores the default. No internet
  dependencies; the UI is fully offline.

## Requirements

- Python 3.11+
- Linux, macOS or Windows

## Install & run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m ai_usage_monitor
# open http://127.0.0.1:61524
```

Options: `--host` (default 127.0.0.1), `--port` (default 61524 — an uncommon port
deliberately chosen so it does not clash with other local apps; change it with
`--port` if it is ever taken), `--verbose`.

> If your Python is externally managed (PEP 668) and `venv` lacks pip, run
> `python3 /tmp/get-pip.py` inside the venv (see
> `https://bootstrap.pypa.io/get-pip.py`).

## Adding accounts

### OAuth sign-in (multiple personal accounts)

Use the **Sign in with Claude** / **Sign in with ChatGPT** buttons in the
dashboard to connect additional personal accounts through the browser OAuth
flow. This is the recommended way to monitor several subscriptions of the
same platform: each sign-in adds a separate account with its own usage
windows, stored locally with `0600` permissions. The app refreshes only the
tokens it obtained itself (both providers rotate refresh tokens); local CLI
logins are never refreshed or written.

Notes: the ChatGPT flow uses a temporary local listener on port 1455 (fixed
by OpenAI's app registration); if that port is busy the login returns an
error. Claude's callback lands on the app's own port (default 61524) at
`/callback` (the path allowed by the Claude Code client). Each callback
arrival is logged ("... OAuth callback received") without its parameters.
Several sign-ins can be pending at once; each completes independently, and
pending sign-ins expire after 10 minutes. Login requests are accepted only
from the dashboard's own origin.

Known limitation (rotating refresh tokens): when a provider rotates a
refresh token, the old one stops working as soon as the provider processes
the refresh. The app never abandons an in-flight refresh (shutdown waits for
it) and retries saving rotated tokens every 30 seconds, but if the process is
killed while a refresh response is still in flight, the new token can be
lost. In that case the account shows an error: sign in again with the same
button.

### Manual API keys

Accounts are added in the dashboard's **Add account** form:

| Provider | Credentials keys | Notes |
|---|---|---|
| `claude_personal` | `credentials_path` (optional) | Defaults to `~/.claude/.credentials.json`; point it at another login's file for a second account — or use **Sign in with Claude** below |
| `chatgpt_personal` | `auth_path` (optional) | Defaults to `~/.codex/auth.json` — or use **Sign in with ChatGPT** below |
| `anthropic_org` | `api_key` | Admin API key from the Claude Console |
| `openai_org` | `api_key` | Admin API key from OpenAI |

Manual accounts are stored in the user config dir
(`~/.config/ai-usage-monitor/accounts.json` on Linux) with `0600`
permissions.

### Removing and restoring accounts

Select **Remove** on any account card, then **Confirm** on that card.
Manually added and OAuth accounts are deleted from the monitor. Removing an
[auto-detected login](#features-v1) hides it and stops usage polling. The hidden
preference is stored in `hidden.json` in the same config directory. The CLI login
files are untouched: the owning CLI still manages its credentials.

To resume monitoring, select **Restore** next to the login under
**Connect an account → Hidden local logins**. Only logins the app can still
find locally are listed.

The dashboard uses `DELETE /api/accounts/{account_id}` to remove or hide an
account, `GET /api/accounts/hidden` to list hidden local logins, and
`POST /api/accounts/{account_id}/restore` to restore one. Remove and restore
requests must originate from the dashboard (other origins get `403`).

## Development

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

All HTTP traffic in tests is mocked (`httpx.MockTransport`); no test ever
touches the network or your real login files.

## Roadmap

- v2: native desktop widget for Windows/macOS/Linux (KDE, GNOME) reusing this
  backend.

## Notes on data sources

Personal-subscription usage comes from endpoints used by the official CLIs;
they are undocumented and may change. The app polls at most every 5 minutes
and always keeps the last good snapshot on failure. Organization usage uses
the official Anthropic Admin API and OpenAI Admin usage API.
