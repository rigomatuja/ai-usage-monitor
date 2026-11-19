"""Tests for AnthropicOrgProvider (T4 / S5).

All HTTP is mocked with httpx.MockTransport; the network is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_usage_monitor.models import WindowKind
from ai_usage_monitor.providers.anthropic_org import AnthropicOrgProvider
from ai_usage_monitor.providers.base import ProviderError

URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"


def make_result(
    model: str = "claude-sonnet-4",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> dict:
    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
    }


def make_response_body() -> dict:
    return {
        "buckets": [
            {
                "ending_at": "2026-11-20T00:00:00Z",
                "results": [
                    make_result("claude-sonnet-4", 200, 0, 0),
                    make_result("claude-haiku-3", 100, 0, 0),
                ],
            },
            {
                "ending_at": "2026-11-21T00:00:00Z",
                "results": [
                    make_result("claude-sonnet-4", 100, 50, 20),
                    make_result("claude-opus-4", 300, 10, 0),
                ],
            },
        ]
    }


async def fetch_with(provider: AnthropicOrgProvider, handler) -> tuple[list, list]:
    """Run fetch against a MockTransport and return (windows, captured requests)."""
    requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport) as http:
        windows = await provider.fetch(http)
    return windows, requests


def ok_handler(body: object) -> object:
    return lambda request: httpx.Response(200, json=body)


# --- Happy path ---------------------------------------------------------------


async def test_happy_path_two_daily_windows_with_aggregation():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})
    windows, requests = await fetch_with(provider, ok_handler(make_response_body()))

    assert len(requests) == 1
    assert len(windows) == 2
    assert [w.label for w in windows] == ["Tokens (24h)", "Tokens (7d)"]
    assert all(w.kind == WindowKind.daily for w in windows)
    assert all(w.used_percent is None for w in windows)
    assert all(w.resets_at is None for w in windows)
    # Latest bucket: (100+50+20) + (300+10) = 480. Week: 300 + 480 = 780.
    assert windows[0].detail == "480 tokens (model top: claude-opus-4)"
    assert windows[1].detail == "780 tokens"


async def test_request_shape_headers_and_params():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})
    _, requests = await fetch_with(provider, ok_handler(make_response_body()))

    request = requests[0]
    assert request.method == "GET"
    assert request.url.host == "api.anthropic.com"
    assert request.url.path == "/v1/organizations/usage_report/messages"
    assert request.headers["x-api-key"] == "sk-ant-admin-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.url.params["bucket_width"] == "1d"
    assert request.url.params["group_by"] == "model"
    assert request.url.params["limit"] == "7"
    # starting_at is an RFC3339 timestamp roughly 7 days in the past.
    starting_at = request.url.params["starting_at"]
    parsed = datetime.fromisoformat(starting_at.replace("Z", "+00:00"))
    now = datetime.now(UTC)
    assert now - timedelta(days=7, minutes=1) <= parsed <= now


async def test_top_model_is_highest_token_model_in_latest_bucket():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})
    windows, _ = await fetch_with(provider, ok_handler(make_response_body()))
    # In the latest bucket claude-opus-4 (310) beats claude-sonnet-4 (170).
    assert "top: claude-opus-4" in windows[0].detail


# --- Tolerant parsing ---------------------------------------------------------


async def test_empty_buckets_yield_zero_token_windows():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})
    windows, _ = await fetch_with(provider, ok_handler({"buckets": []}))
    assert windows[0].detail == "0 tokens"
    assert windows[1].detail == "0 tokens"
    assert "top:" not in windows[0].detail


async def test_missing_results_and_missing_token_fields_do_not_raise():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})
    body = {
        "buckets": [
            {"ending_at": "2026-11-21T00:00:00Z"},  # no results key
            {"ending_at": "2026-11-20T00:00:00Z", "results": [{"model": "m"}]},
            {"ending_at": "2026-11-19T00:00:00Z", "results": [{"foo": "bar"}]},
        ]
    }
    windows, _ = await fetch_with(provider, ok_handler(body))
    assert windows[0].detail == "0 tokens"
    assert windows[1].detail == "0 tokens"


async def test_unparseable_ending_at_does_not_raise():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})
    body = {
        "buckets": [
            {"ending_at": "not-a-date", "results": [make_result("m", 5, 5)]},
            {"results": [make_result("m", 1, 1)]},
        ]
    }
    windows, _ = await fetch_with(provider, ok_handler(body))
    assert windows[1].detail == "12 tokens"


async def test_pagination_token_is_ignored_not_followed():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})
    body = {**make_response_body(), "next_page": "page-token-abc"}
    windows, requests = await fetch_with(provider, ok_handler(body))
    assert len(requests) == 1  # pagination is not followed in v1
    assert len(windows) == 2
    assert windows[1].detail == "780 tokens"


# --- Errors -------------------------------------------------------------------


@pytest.mark.parametrize("credentials", [{}, {"api_key": ""}, {"api_key": "   "}])
async def test_missing_api_key_raises_provider_error_without_request(credentials):
    provider = AnthropicOrgProvider(credentials)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ProviderError):
            await provider.fetch(http)
    assert requests == []


@pytest.mark.parametrize("status", [401, 400])
async def test_http_errors_raise_provider_error(status):
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        await fetch_with(provider, handler)


async def test_request_failure_does_not_disclose_api_key():
    provider = AnthropicOrgProvider({"api_key": "synthetic-anthropic-secret"})

    def handler(request):
        raise httpx.ReadError(f"Request failed for {request.headers['x-api-key']}")

    with pytest.raises(ProviderError) as caught:
        await fetch_with(provider, handler)
    assert str(caught.value) == "anthropic_org usage report request failed: ReadError"


async def test_malformed_json_raises_provider_error():
    provider = AnthropicOrgProvider({"api_key": "sk-ant-admin-test"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(ProviderError, match="invalid JSON"):
        await fetch_with(provider, handler)
