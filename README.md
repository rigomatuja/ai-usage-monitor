# AI Usage Monitor

Local web app that shows current AI usage for Claude and ChatGPT/OpenAI
accounts: subscription usage windows (5-hour, weekly) and organization
token/cost usage, across multiple accounts per platform.

## Features (v1)

- **Personal subscriptions**: auto-detects local logins of Claude Code
  (`~/.claude/.credentials.json`) and Codex CLI (`~/.codex/auth.json`) and
  reads their live usage windows (5-hour session, weekly).
- **Organization accounts**: add Claude Team/Enterprise or OpenAI org accounts
  via an Admin API key; shows token and cost usage (24h and 7d).
- **Multiple accounts per platform**: auto-detected logins plus any number of
  manually added accounts (e.g. a second Claude login via its own
  credentials path, or several org keys).
- **Local web dashboard**: one card per account with colored progress bars,
  reset times, stale-data markers, and add/remove account forms. No internet
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
error. Claude's callback lands on the app's own port (default 61524).

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
permissions. Auto-detected accounts cannot be removed from the dashboard —
they disappear when the login file is gone. Tokens are never refreshed or
written back: the owning CLI manages its own credentials.

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
