"""Anthropic organization (Admin API) usage provider.

Covers the org side of S5: Claude Team/Enterprise accounts monitored with an
Admin API key instead of a personal CLI login. See odd/tasks/v1-web-monitor.md
("Provider endpoint details" → Anthropic org).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from ai_usage_monitor.models import UsageWindow, WindowKind
from ai_usage_monitor.providers.base import ProviderError, UsageProvider

_USAGE_REPORT_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"


def _int(value: object) -> int:
    """Tolerantly coerce a token counter to int; missing/garbage counts as 0."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _fmt_tokens(count: int) -> str:
    """Human-readable token count: 1234567 -> 1.2M, 8500 -> 8.5k, 420 -> 420."""
    if count >= 1_000_000:
        text = f"{count / 1_000_000:.1f}"
    elif count >= 1_000:
        text = f"{count / 1_000:.1f}"
    else:
        return str(count)
    return (text.removesuffix(".0") if text.endswith(".0") else text) + (
        "M" if count >= 1_000_000 else "k"
    )


def _parse_rfc3339(value: object) -> datetime | None:
    """Parse an RFC3339 timestamp; unparseable/missing values return None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _bucket_stats(results: object) -> tuple[int, dict[str, int]]:
    """Sum tokens (input + output + cache read) per result, grouped by model."""
    total = 0
    models: dict[str, int] = {}
    if not isinstance(results, list):
        return total, models
    for result in results:
        if not isinstance(result, dict):
            continue
        tokens = (
            _int(result.get("input_tokens"))
            + _int(result.get("output_tokens"))
            + _int(result.get("cache_read_input_tokens"))
        )
        total += tokens
        model = result.get("model")
        if isinstance(model, str) and model:
            models[model] = models.get(model, 0) + tokens
    return total, models


class AnthropicOrgProvider(UsageProvider):
    """Claude Team/Enterprise usage via the Admin API messages usage report."""

    def _api_key(self) -> str:
        key = self.credentials.get("api_key", "").strip()
        if not key:
            raise ProviderError(
                "anthropic_org provider requires an 'api_key' credential"
            )
        return key

    async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]:
        api_key = self._api_key()
        # Time windows are computed per call, never at construction.
        now = datetime.now(UTC)
        params = {
            "starting_at": (now - timedelta(days=7))
            .isoformat()
            .replace("+00:00", "Z"),
            "bucket_width": "1d",
            "group_by": "model",
            "limit": "7",
        }
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        # Pagination is not followed in v1: if the API returns a next_page
        # token we simply aggregate the page that was returned (the 7 daily
        # buckets we ask for fit in a single page at limit=7).
        response = await http.get(_USAGE_REPORT_URL, params=params, headers=headers)
        if response.status_code >= 400:
            raise ProviderError(
                f"anthropic_org usage report failed: HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "anthropic_org usage report returned invalid JSON"
            ) from exc

        buckets = data.get("buckets") if isinstance(data, dict) else None
        if not isinstance(buckets, list):
            buckets = []

        latest_ending: datetime | None = None
        latest_total = 0
        latest_models: dict[str, int] = {}
        week_total = 0
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            ending_at = _parse_rfc3339(bucket.get("ending_at"))
            bucket_total, models = _bucket_stats(bucket.get("results"))
            week_total += bucket_total
            if latest_ending is None or (
                ending_at is not None and ending_at > latest_ending
            ):
                latest_ending, latest_total, latest_models = (
                    ending_at,
                    bucket_total,
                    models,
                )

        if latest_models:
            top_model = max(latest_models.items(), key=lambda item: item[1])[0]
            detail_24h = (
                f"{_fmt_tokens(latest_total)} tokens (model top: {top_model})"
            )
        else:
            detail_24h = f"{_fmt_tokens(latest_total)} tokens"

        return [
            UsageWindow(
                kind=WindowKind.daily,
                label="Tokens (24h)",
                used_percent=None,
                detail=detail_24h,
                resets_at=None,
            ),
            UsageWindow(
                kind=WindowKind.daily,
                label="Tokens (7d)",
                used_percent=None,
                detail=f"{_fmt_tokens(week_total)} tokens",
                resets_at=None,
            ),
        ]
