"""Tests for ChatGPTPersonalProvider (T3).

All HTTP is mocked via httpx.MockTransport; tests never touch the network and
never read the real ~/.codex auth file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ai_usage_monitor.models import WindowKind
from ai_usage_monitor.providers.base import ProviderError
from ai_usage_monitor.providers.chatgpt_personal import ChatGPTPersonalProvider

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

ACCESS_TOKEN = "eyJ-test-access-token"
ACCOUNT_ID = "acct-123"


def write_auth(tmp_path: Path, payload: dict, name: str = "auth.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def auth_payload() -> dict:
    return {"tokens": {"access_token": ACCESS_TOKEN, "account_id": ACCOUNT_ID}}


def make_http(responses: list[httpx.Response], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


# ---------------------------------------------------------------- happy paths


async def test_happy_path(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 25.5,
                            "limit_window_seconds": 18000,
                            "reset_at": 1730000000,
                        },
                        "secondary_window": {
                            "used_percent": 8.0,
                            "limit_window_seconds": 604800,
                            "reset_at": "2026-11-08T00:00:00Z",
                        },
                    },
                    "credits": {"balance": 40},
                }
            )
        ],
        seen,
    )
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    windows = await provider.fetch(http)

    # Request shape: exact URL and headers per spec.
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == USAGE_URL
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["chatgpt-account-id"] == ACCOUNT_ID

    assert [w.kind for w in windows] == [WindowKind.five_hour, WindowKind.weekly]
    assert [w.label for w in windows] == ["5-hour session", "Weekly"]
    assert [w.used_percent for w in windows] == [25.5, 8.0]
    assert windows[0].resets_at == datetime.fromtimestamp(
        1730000000, tz=timezone.utc
    )
    assert windows[1].resets_at == datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
    # plan_type in detail; credits.balance goes into the five-hour detail.
    assert windows[0].detail == "Pro plan · 40 credits"
    assert windows[1].detail == "Pro plan"


async def test_window_identification_by_duration_not_slot(tmp_path):
    """Slots are swapped: primary is weekly, secondary is five_hour; a third
    unknown duration would be omitted."""
    auth = write_auth(tmp_path, auth_payload())
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 3.0,
                            "limit_window_seconds": 604800,
                            "reset_at": None,
                        },
                        "secondary_window": {
                            "used_percent": 90.0,
                            "limit_window_seconds": 18000,
                            "reset_at": None,
                        },
                    },
                }
            )
        ],
        seen,
    )
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    windows = await provider.fetch(http)

    assert [w.kind for w in windows] == [WindowKind.weekly, WindowKind.five_hour]
    assert [w.used_percent for w in windows] == [3.0, 90.0]


async def test_unknown_window_duration_omitted(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 50.0,
                            "limit_window_seconds": 3600,  # unknown duration
                            "reset_at": None,
                        },
                        "secondary_window": {
                            "used_percent": 10.0,
                            "limit_window_seconds": 18000,
                            "reset_at": None,
                        },
                    }
                }
            )
        ],
        seen,
    )
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    windows = await provider.fetch(http)
    assert [w.kind for w in windows] == [WindowKind.five_hour]


async def test_reset_at_numeric_string(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 1.0,
                            "limit_window_seconds": 18000,
                            "reset_at": "1730000000",  # numeric string, not ISO
                        }
                    }
                }
            )
        ],
        seen,
    )
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    windows = await provider.fetch(http)
    assert windows[0].resets_at == datetime.fromtimestamp(1730000000, tz=timezone.utc)


# ------------------------------------------------------- credentials handling


async def test_missing_auth_file(tmp_path):
    provider = ChatGPTPersonalProvider({"auth_path": str(tmp_path / "nope.json")})
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_missing_access_token(tmp_path):
    auth = write_auth(tmp_path, {"tokens": {"account_id": ACCOUNT_ID}})
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_missing_account_id(tmp_path):
    auth = write_auth(tmp_path, {"tokens": {"access_token": ACCESS_TOKEN}})
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_invalid_auth_file_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("not json at all")
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_auth_file_not_written(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    before = auth.read_text()
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([ok({})], [])
    await provider.fetch(http)
    assert auth.read_text() == before


# ---------------------------------------------------------- inline credentials


async def test_inline_access_token_and_account_id_used_and_files_never_read():
    """OAuth accounts carry tokens in credentials; no local auth file is touched."""
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 44.0,
                            "limit_window_seconds": 18000,
                            "reset_at": None,
                        }
                    },
                }
            )
        ],
        seen,
    )
    provider = ChatGPTPersonalProvider(
        {
            "access_token": ACCESS_TOKEN,
            "account_id": ACCOUNT_ID,
            "auth_path": "/nonexistent/inline-test-path/auth.json",
        }
    )
    windows = await provider.fetch(http)

    assert len(seen) == 1
    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["chatgpt-account-id"] == ACCOUNT_ID
    assert [w.used_percent for w in windows] == [44.0]
    assert windows[0].detail == "Pro plan"


async def test_inline_credentials_without_account_id_raises():
    provider = ChatGPTPersonalProvider(
        {
            "access_token": ACCESS_TOKEN,
            "auth_path": "/nonexistent/inline-test-path/auth.json",
        }
    )
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


# -------------------------------------------------------------- error shapes


async def test_http_401_raises(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([httpx.Response(401, json={"detail": "unauthorized"})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_http_429_raises(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([httpx.Response(429, json={"detail": "rate limited"})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_invalid_json_raises(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([httpx.Response(200, text="<html>boom</html>")], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


# ---------------------------------------------------------- tolerant parsing


async def test_null_rate_limit_yields_no_windows(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([ok({"plan_type": "free", "rate_limit": None})], [])
    assert await provider.fetch(http) == []


async def test_empty_response_yields_no_windows(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http([ok({})], [])
    assert await provider.fetch(http) == []


async def test_absent_optional_fields_omitted_not_error(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http(
        [
            ok(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 5.0,
                            "limit_window_seconds": 18000,
                            "reset_at": None,
                        },
                        "secondary_window": None,
                    }
                    # no plan_type, no credits
                }
            )
        ],
        [],
    )
    windows = await provider.fetch(http)
    assert [w.kind for w in windows] == [WindowKind.five_hour]
    assert windows[0].detail is None
    assert windows[0].resets_at is None


async def test_credits_without_five_hour_window_omitted(tmp_path):
    auth = write_auth(tmp_path, auth_payload())
    provider = ChatGPTPersonalProvider({"auth_path": str(auth)})
    http = make_http(
        [
            ok(
                {
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 2.0,
                            "limit_window_seconds": 604800,
                            "reset_at": None,
                        }
                    },
                    "credits": {"balance": 12},
                }
            )
        ],
        [],
    )
    windows = await provider.fetch(http)
    assert [w.kind for w in windows] == [WindowKind.weekly]
    assert windows[0].detail == "Pro plan"  # credits only attach to five_hour
