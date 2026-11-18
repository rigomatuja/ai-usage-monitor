"""Claude personal (Pro/Max) usage provider.

Reads the local Claude CLI OAuth credentials file (default
``~/.claude/.credentials.json``) and polls the OAuth usage endpoint.

Rules (odd/tasks/v1-web-monitor.md):
- Never refresh tokens and never write to the credentials file: the Claude CLI
  owns the refresh flow, and racing it desynchronizes the CLI.
- Percent values from the endpoint are already 0-100; never rescale.
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

DEFAULT_CREDENTIALS_PATH = "~/.claude/.credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-code/1.0.88"

_WINDOW_KINDS = {
    WindowKind.five_hour: "5-hour session",
    WindowKind.weekly: "Weekly",
}
_LIMIT_KIND_TO_WINDOW: dict[str, WindowKind] = {
    "session": WindowKind.five_hour,
    "weekly_all": WindowKind.weekly,
    "weekly_scoped": WindowKind.weekly,
}


def _parse_resets_at(value: Any) -> datetime | None:
    """Parse a reset timestamp that may be epoch seconds or ISO 8601.

    Unparseable values degrade to None (tolerant parsing), never raise.
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


class ClaudePersonalProvider(UsageProvider):
    """Usage provider for a personal Claude Pro/Max subscription."""

    async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]:
        token, subscription_type = self._load_credentials()
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": USER_AGENT,
        }
        try:
            response = await http.get(USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Claude usage request failed: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"Claude usage endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Claude usage endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Claude usage response was not a JSON object")
        return self._parse_windows(payload, subscription_type)

    def _load_credentials(self) -> tuple[str, str | None]:
        """Inline OAuth tokens win; otherwise read the credentials file.
        Read-only; never writes or refreshes."""
        inline_token = self.credentials.get("access_token")
        if inline_token:
            # OAuth account (T2): tokens come from the registry, never the
            # local CLI login file.
            return inline_token, None
        raw_path = self.credentials.get("credentials_path") or DEFAULT_CREDENTIALS_PATH
        path = Path(raw_path).expanduser()
        try:
            data = json.loads(path.read_text())
        except OSError as exc:
            raise ProviderError(
                f"Could not read Claude credentials file {path}: {exc}"
            ) from exc
        except ValueError as exc:
            raise ProviderError(
                f"Claude credentials file {path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"Claude credentials file {path} is not a JSON object")
        oauth = data.get("claudeAiOauth")
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(token, str) or not token:
            raise ProviderError(
                f"Claude credentials file {path} has no claudeAiOauth.accessToken"
            )
        subscription_type = data.get("subscriptionType")
        return token, subscription_type if isinstance(subscription_type, str) else None

    def _parse_windows(
        self, payload: dict[str, Any], subscription_type: str | None
    ) -> list[UsageWindow]:
        limits = payload.get("limits")
        if isinstance(limits, list) and limits:
            return self._parse_limits(limits, subscription_type)
        return self._parse_top_level(payload, subscription_type)

    def _parse_limits(
        self, limits: list[Any], subscription_type: str | None
    ) -> list[UsageWindow]:
        windows: list[UsageWindow] = []
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            limit_kind = entry.get("kind")
            kind = _LIMIT_KIND_TO_WINDOW.get(limit_kind)
            if kind is None:
                continue
            percent = entry.get("percent")
            if not _is_number(percent):
                continue
            label = _WINDOW_KINDS[kind]
            if limit_kind == "weekly_scoped":
                model = entry.get("model") or entry.get("model_display_name")
                if isinstance(model, str) and model:
                    label = f"{label} · {model}"
            windows.append(
                UsageWindow(
                    kind=kind,
                    label=label,
                    used_percent=float(percent),
                    detail=subscription_type,
                    resets_at=_parse_resets_at(entry.get("resets_at")),
                )
            )
        return windows

    def _parse_top_level(
        self, payload: dict[str, Any], subscription_type: str | None
    ) -> list[UsageWindow]:
        windows: list[UsageWindow] = []
        for key, kind in (
            ("five_hour", WindowKind.five_hour),
            ("seven_day", WindowKind.weekly),
        ):
            entry = payload.get(key)
            if not isinstance(entry, dict):
                continue
            utilization = entry.get("utilization")
            if not _is_number(utilization):
                continue
            windows.append(
                UsageWindow(
                    kind=kind,
                    label=_WINDOW_KINDS[kind],
                    used_percent=float(utilization),
                    detail=subscription_type,
                    resets_at=_parse_resets_at(entry.get("resets_at")),
                )
            )
        return windows
