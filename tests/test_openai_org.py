"""Tests for OpenAIOrgProvider (T4 / S5).

All HTTP is mocked with httpx.MockTransport; the network is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_usage_monitor.models import WindowKind
from ai_usage_monitor.providers.base import ProviderError
from ai_usage_monitor.providers.openai_org import OpenAIOrgProvider

LATEST_END = 1_763_606_400  # fixed unix second; latest usage/cost bucket
EARLIER_END = 1_763_520_000


def make_usage_body() -> dict:
    return {
        "object": "page",
        "data": [
            {
                "end_time": EARLIER_END,
                "results": [
                    {
                        "input_tokens": 200,
                        "output_tokens": 0,
                        "num_model_requests": 2,
                        "model": "gpt-4o",
                    }
                ],
            },
            {
                "end_time": LATEST_END,
                "results": [
                    {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "num_model_requests": 3,
                        "model": "gpt-4o",
                    },
                    {
                        "input_tokens": 400,
                        "output_tokens": 10,
                        "num_model_requests": 1,
                        "model": "o3",
                    },
                ],
            },
        ],
    }


def make_costs_body() -> dict:
    return {
        "object": "page",
        "data": [
            {
                "end_time": EARLIER_END,
                "results": [{"line_item": "gpt-4o", "cost": 2.0}],
            },
            {
                "end_time": LATEST_END,
                "results": [
                    {"line_item": "gpt-4o", "cost": 1.25},
                    {"line_item": "o3", "cost": 0.75},
                ],
            },
        ],
    }


async def fetch_with(
    provider: OpenAIOrgProvider, handler
) -> tuple[list, list[httpx.Request]]:
    """Run fetch against a MockTransport and return (windows, captured requests)."""
    requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport) as http:
        windows = await provider.fetch(http)
    return windows, requests


def make_handler(
    usage_response: httpx.Response, costs_response: httpx.Response
) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/organization/usage/completions":
            return usage_response
        assert request.url.path == "/v1/organization/costs"
        return costs_response

    return handler


def ok_provider() -> OpenAIOrgProvider:
    return OpenAIOrgProvider({"api_key": "sk-admin-test"})


# --- Happy path ---------------------------------------------------------------


async def test_happy_path_two_daily_windows_with_aggregation_and_cost():
    provider = ok_provider()
    handler = make_handler(
        httpx.Response(200, json=make_usage_body()),
        httpx.Response(200, json=make_costs_body()),
    )
    windows, requests = await fetch_with(provider, handler)

    assert len(requests) == 2
    assert len(windows) == 2
    assert [w.label for w in windows] == ["Tokens (24h)", "Tokens (7d)"]
    assert all(w.kind == WindowKind.daily for w in windows)
    assert all(w.used_percent is None for w in windows)
    assert all(w.resets_at is None for w in windows)
    # Latest usage bucket: (100+50) + (400+10) = 560; week total 760.
    # Costs: 2.0 + 1.25 + 0.75 = 4.00.
    assert windows[0].detail == "560 tokens · top: o3"
    assert windows[1].detail == "760 tokens · $4.00"


async def test_request_shape_headers_and_params():
    provider = ok_provider()
    handler = make_handler(
        httpx.Response(200, json=make_usage_body()),
        httpx.Response(200, json=make_costs_body()),
    )
    _, requests = await fetch_with(provider, handler)

    usage_req, costs_req = requests
    assert usage_req.url.host == "api.openai.com"
    assert usage_req.url.path == "/v1/organization/usage/completions"
    assert usage_req.headers["Authorization"] == "Bearer sk-admin-test"
    assert usage_req.url.params["limit"] == "7"
    assert usage_req.url.params["group_by[]"] == "model"

    costs_req_url = costs_req.url
    assert costs_req_url.path == "/v1/organization/costs"
    assert costs_req.headers["Authorization"] == "Bearer sk-admin-test"
    assert costs_req_url.params["limit"] == "7"

    now = datetime.now(UTC)
    window_start = now - timedelta(days=7)
    # start_time/end_time are unix seconds spanning roughly the last 7 days.
    usage_start = int(usage_req.url.params["start_time"])
    usage_end = int(usage_req.url.params["end_time"])
    assert int(window_start.timestamp()) - 60 <= usage_start <= int(now.timestamp())
    assert int(window_start.timestamp()) <= usage_end <= int(now.timestamp()) + 60
    costs_start = int(costs_req_url.params["start_time"])
    assert int(window_start.timestamp()) - 60 <= costs_start <= int(now.timestamp())


async def test_top_model_is_highest_token_model_in_latest_bucket():
    provider = ok_provider()
    handler = make_handler(
        httpx.Response(200, json=make_usage_body()),
        httpx.Response(200, json=make_costs_body()),
    )
    windows, _ = await fetch_with(provider, handler)
    # In the latest bucket o3 (410) beats gpt-4o (150).
    assert "top: o3" in windows[0].detail


# --- Tolerant parsing ---------------------------------------------------------


async def test_empty_buckets_yield_zero_token_windows():
    provider = ok_provider()
    handler = make_handler(
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": []}),
    )
    windows, _ = await fetch_with(provider, handler)
    assert windows[0].detail == "0 tokens"
    assert windows[1].detail == "0 tokens"
    assert "top:" not in windows[0].detail
    assert "$" not in windows[1].detail


async def test_missing_fields_do_not_raise():
    provider = ok_provider()
    usage_body = {
        "data": [
            {"end_time": LATEST_END},  # no results
            {"results": [{"model": "m"}]},  # no end_time, no token fields
        ]
    }
    costs_body = {"data": [{"end_time": LATEST_END, "results": [{"line_item": "x"}]}]}
    handler = make_handler(
        httpx.Response(200, json=usage_body),
        httpx.Response(200, json=costs_body),
    )
    windows, _ = await fetch_with(provider, handler)
    assert windows[0].detail == "0 tokens"
    assert windows[1].detail == "0 tokens"


@pytest.mark.parametrize(
    "costs_body",
    [
        {"data": [{"end_time": LATEST_END, "results": [{"line_item": "x"}]}]},
        {"data": [{"results": [{"cost": "not-a-number"}]}]},
        {"data": [{"results": [{"cost": {"amount": {"currency": "usd"}}}]}]},
        {"data": "unexpected-shape"},
        {"error": {"message": "nope"}},
        "totally-not-a-dict",
    ],
)
async def test_odd_cost_shape_degrades_to_tokens_only(costs_body):
    provider = ok_provider()
    handler = make_handler(
        httpx.Response(200, json=make_usage_body()),
        httpx.Response(200, json=costs_body),
    )
    windows, _ = await fetch_with(provider, handler)
    assert windows[1].detail == "760 tokens"
    assert "$" not in windows[1].detail


async def test_nested_cost_shape_is_parsed():
    provider = ok_provider()
    costs_body = {
        "data": [
            {
                "end_time": LATEST_END,
                "results": [{"line_item": "gpt-4o", "cost": {"amount": {"value": 3.5}}}],
            }
        ]
    }
    handler = make_handler(
        httpx.Response(200, json=make_usage_body()),
        httpx.Response(200, json=costs_body),
    )
    windows, _ = await fetch_with(provider, handler)
    assert windows[1].detail == "760 tokens · $3.50"


# --- Errors -------------------------------------------------------------------


@pytest.mark.parametrize("credentials", [{}, {"api_key": ""}])
async def test_missing_api_key_raises_provider_error_without_request(credentials):
    provider = OpenAIOrgProvider(credentials)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ProviderError):
            await provider.fetch(http)
    assert requests == []


@pytest.mark.parametrize("endpoint", ["usage", "costs"])
@pytest.mark.parametrize("status", [401, 400])
async def test_http_errors_raise_provider_error(endpoint, status):
    provider = ok_provider()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/organization/usage/completions":
            if endpoint == "usage":
                return httpx.Response(status, json={"error": "nope"})
            return httpx.Response(200, json=make_costs_body())
        if endpoint == "costs":
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, json=make_usage_body())

    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        await fetch_with(provider, handler)


@pytest.mark.parametrize("report", ["usage", "costs"])
async def test_request_failure_does_not_disclose_api_key(report):
    provider = OpenAIOrgProvider({"api_key": "synthetic-openai-secret"})

    def handler(request):
        if report == "costs" and request.url.path.endswith("/completions"):
            return httpx.Response(200, json=make_usage_body())
        raise httpx.ReadError(f"Request failed for {request.headers['authorization']}")

    with pytest.raises(ProviderError) as caught:
        await fetch_with(provider, handler)
    assert str(caught.value) == f"openai_org {report} report request failed: ReadError"


@pytest.mark.parametrize("endpoint", ["usage", "costs"])
async def test_malformed_json_raises_provider_error(endpoint):
    provider = ok_provider()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/organization/usage/completions":
            if endpoint == "usage":
                return httpx.Response(200, content=b"not json at all")
            return httpx.Response(200, json=make_costs_body())
        if endpoint == "costs":
            return httpx.Response(200, content=b"<html>nope</html>")
        return httpx.Response(200, json=make_usage_body())

    with pytest.raises(ProviderError, match="invalid JSON"):
        await fetch_with(provider, handler)
