"""OpenAI organization (Admin usage API) usage provider.

Covers the org side of S5: OpenAI organization accounts monitored with an
Admin API key against the usage/completions and costs endpoints. See
odd/tasks/v1-web-monitor.md ("Provider endpoint details" → OpenAI org).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from ai_usage_monitor.models import UsageWindow, WindowKind
from ai_usage_monitor.providers.base import ProviderError, UsageProvider

_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
_COSTS_URL = "https://api.openai.com/v1/organization/costs"


def _int(value: object) -> int:
    """Tolerantly coerce a counter to int; missing/garbage counts as 0."""
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


def _cost_value(result: object) -> float | None:
    """Extract a USD cost from one cost-bucket result; None if not parseable."""
    if not isinstance(result, dict):
        return None
    cost = result.get("cost")
    if isinstance(cost, bool):
        return None
    if isinstance(cost, (int, float)):
        return float(cost)
    if isinstance(cost, str):
        try:
            return float(cost)
        except ValueError:
            return None
    if isinstance(cost, dict):
        # Some deployments nest the value, e.g. {"amount": {"value": 1.5}}.
        for path in (("amount", "value"), ("value",), ("amount",)):
            node: object = cost
            ok = True
            for key in path:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    ok = False
                    break
            if ok and isinstance(node, (int, float)) and not isinstance(node, bool):
                return float(node)
    return None


def _costs_total(payload: object) -> float | None:
    """Sum USD costs across all cost buckets.

    Returns None when the cost shape is unrecognizable or no cost value can
    be extracted, so callers degrade gracefully to a tokens-only detail.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    total = 0.0
    extracted_any = False
    for bucket in data:
        if not isinstance(bucket, dict):
            continue
        results = bucket.get("results")
        if not isinstance(results, list):
            continue
        for result in results:
            value = _cost_value(result)
            if value is not None:
                total += value
                extracted_any = True
    return total if extracted_any else None


class OpenAIOrgProvider(UsageProvider):
    """OpenAI organization usage via the Admin usage/completions + costs APIs."""

    def _api_key(self) -> str:
        key = self.credentials.get("api_key", "").strip()
        if not key:
            raise ProviderError("openai_org provider requires an 'api_key' credential")
        return key

    async def _get_json(
        self,
        http: httpx.AsyncClient,
        url: str,
        params: dict[str, object],
        headers: dict[str, str],
        what: str,
    ) -> object:
        response = await http.get(url, params=params, headers=headers)
        if response.status_code >= 400:
            raise ProviderError(f"openai_org {what} failed: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"openai_org {what} returned invalid JSON") from exc

    async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]:
        api_key = self._api_key()
        # Time windows are computed per call, never at construction.
        now = datetime.now(UTC)
        start_time = int((now - timedelta(days=7)).timestamp())
        end_time = int(now.timestamp())
        headers = {"Authorization": f"Bearer {api_key}"}

        usage = await self._get_json(
            http,
            _USAGE_URL,
            params={
                "start_time": start_time,
                "end_time": end_time,
                "limit": 7,
                "group_by[]": "model",
            },
            headers=headers,
            what="usage report",
        )
        costs = await self._get_json(
            http,
            _COSTS_URL,
            params={"start_time": start_time, "limit": 7},
            headers=headers,
            what="costs report",
        )

        # Usage buckets: end_time (unix seconds) + results[] with token counts.
        # Bucket matching between usage and costs is intentionally simple and
        # tolerant: window (a) uses the latest usage bucket and the 7d total
        # sums usage and costs independently instead of joining on end_time.
        usage_data = usage.get("data") if isinstance(usage, dict) else None
        if not isinstance(usage_data, list):
            usage_data = []
        latest_end = -1
        latest_total = 0
        latest_models: dict[str, int] = {}
        week_total = 0
        for bucket in usage_data:
            if not isinstance(bucket, dict):
                continue
            bucket_end = _int(bucket.get("end_time"))
            results = bucket.get("results")
            results = results if isinstance(results, list) else []
            bucket_total = 0
            models: dict[str, int] = {}
            for result in results:
                if not isinstance(result, dict):
                    continue
                # num_model_requests is a request count, not tokens: excluded.
                tokens = _int(result.get("input_tokens")) + _int(
                    result.get("output_tokens")
                )
                bucket_total += tokens
                week_total += tokens
                model = result.get("model")
                if isinstance(model, str) and model:
                    models[model] = models.get(model, 0) + tokens
            if bucket_end > latest_end:
                latest_end, latest_total, latest_models = (
                    bucket_end,
                    bucket_total,
                    models,
                )

        total_cost = _costs_total(costs)

        if latest_models:
            top_model = max(latest_models.items(), key=lambda item: item[1])[0]
            detail_24h = f"{_fmt_tokens(latest_total)} tokens · top: {top_model}"
        else:
            detail_24h = f"{_fmt_tokens(latest_total)} tokens"
        detail_7d = f"{_fmt_tokens(week_total)} tokens"
        if total_cost is not None:
            detail_7d += f" · ${total_cost:.2f}"

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
                detail=detail_7d,
                resets_at=None,
            ),
        ]
