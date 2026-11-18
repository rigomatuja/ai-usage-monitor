"""ChatGPT personal (Plus/Pro) usage provider.

Reads the local Codex CLI auth file (default ``~/.codex/auth.json``) and polls
the ChatGPT backend usage endpoint.

Rules (odd/tasks/v1-web-monitor.md):
- Never refresh tokens and never write to the auth file: the Codex CLI owns
  the refresh flow, and racing it desynchronizes the CLI.
- Percent values from the endpoint are already 0-100; never rescale.
- Windows are identified by ``limit_window_seconds``, never by slot order.
- ``credits.balance`` is a credit COUNT, not dollars.
- Absent/unknown fields degrade to omitted windows, never errors; errors are
  raised as ``ProviderError`` only when the account cannot be polled at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ai_usage_monitor.models import UsageWindow, WindowKind
from ai_usage_monitor.providers.base import ProviderError, UsageProvider

DEFAULT_AUTH_PATH = "~/.codex/auth.json"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

_WINDOW_KINDS = {
    WindowKind.five_hour: "5-hour session",
    WindowKind.weekly: "Weekly",
}
# Window identity comes from the duration, never from the slot position.
_SECONDS_TO_WINDOW: dict[int, WindowKind] = {
    18000: WindowKind.five_hour,  # 5 hours
    604800: WindowKind.weekly,  # 7 days
}


def _parse_resets_at(value: Any) -> datetime | None:
    """Parse a reset timestamp that may be epoch seconds or ISO 8601.

    Numbers (and numeric strings) are unix seconds; strings are tried as ISO
    8601 first, then as a numeric string. Unparseable values degrade to None
    (tolerant parsing), never raise.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _format_balance(balance: float) -> str:
    if balance == int(balance):
        return str(int(balance))
    return f"{balance:g}"


class ChatGPTPersonalProvider(UsageProvider):
    """Usage provider for a personal ChatGPT Plus/Pro subscription."""

    async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]:
        access_token, account_id = self._load_credentials()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
        }
        try:
            response = await http.get(USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ChatGPT usage request failed: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"ChatGPT usage endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "ChatGPT usage endpoint returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError("ChatGPT usage response was not a JSON object")
        return self._parse_windows(payload)

    def _load_credentials(self) -> tuple[str, str]:
        """Read the auth file. Read-only; never writes or refreshes tokens."""
        raw_path = self.credentials.get("auth_path") or DEFAULT_AUTH_PATH
        path = Path(raw_path).expanduser()
        try:
            data = json.loads(path.read_text())
        except OSError as exc:
            raise ProviderError(
                f"Could not read ChatGPT auth file {path}: {exc}"
            ) from exc
        except ValueError as exc:
            raise ProviderError(
                f"ChatGPT auth file {path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"ChatGPT auth file {path} is not a JSON object")
        tokens = data.get("tokens")
        access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
        account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError(
                f"ChatGPT auth file {path} has no tokens.access_token"
            )
        if not isinstance(account_id, str) or not account_id:
            raise ProviderError(
                f"ChatGPT auth file {path} has no tokens.account_id"
            )
        return access_token, account_id

    def _parse_windows(self, payload: dict[str, Any]) -> list[UsageWindow]:
        plan_type = payload.get("plan_type")
        plan_detail = (
            f"{plan_type.title()} plan"
            if isinstance(plan_type, str) and plan_type
            else None
        )

        credits_balance = None
        credits = payload.get("credits")
        if isinstance(credits, dict):
            balance = credits.get("balance")
            if _is_number(balance):
                credits_balance = float(balance)

        rate_limit = payload.get("rate_limit")
        windows: list[UsageWindow] = []
        if not isinstance(rate_limit, dict):
            return windows
        for slot in ("primary_window", "secondary_window"):
            entry = rate_limit.get(slot)
            if not isinstance(entry, dict):
                continue
            seconds = entry.get("limit_window_seconds")
            kind = (
                _SECONDS_TO_WINDOW.get(int(seconds))
                if _is_number(seconds) and float(seconds).is_integer()
                else None
            )
            if kind is None:
                continue
            used_percent = entry.get("used_percent")
            if not _is_number(used_percent):
                continue
            detail = plan_detail
            if kind is WindowKind.five_hour and credits_balance is not None:
                parts = [plan_detail] if plan_detail else []
                parts.append(f"{_format_balance(credits_balance)} credits")
                detail = " · ".join(parts)
            windows.append(
                UsageWindow(
                    kind=kind,
                    label=_WINDOW_KINDS[kind],
                    used_percent=float(used_percent),
                    detail=detail,
                    resets_at=_parse_resets_at(entry.get("reset_at")),
                )
            )
        return windows
