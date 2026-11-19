"""Tests for ClaudePersonalProvider (T2).

All HTTP is mocked via httpx.MockTransport; tests never touch the network and
never read the real ~/.claude credentials file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from ai_usage_monitor.models import WindowKind
from ai_usage_monitor.providers.base import ProviderError
from ai_usage_monitor.providers.claude_personal import ClaudePersonalProvider

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def write_credentials(tmp_path: Path, payload: dict, name: str = "creds.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def credentials_for(path: Path) -> dict[str, str]:
    return {"credentials_path": str(path)}


def make_http(responses: list[httpx.Response], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


ACCESS_TOKEN = "sk-ant-oat01-test-token"


def cred_payload() -> dict:
    return {
        "claudeAiOauth": {"accessToken": ACCESS_TOKEN},
        "subscriptionType": "claude_code_max",
    }


# ---------------------------------------------------------------- happy paths


async def test_limits_array_happy_path(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "limits": [
                        {
                            "kind": "session",
                            "percent": 42.5,
                            "resets_at": "2026-11-05T18:00:00Z",
                            "is_active": True,
                        },
                        {
                            "kind": "weekly_all",
                            "percent": 10,
                            "resets_at": None,
                            "is_active": False,
                        },
                        {
                            "kind": "weekly_scoped",
                            "percent": 5,
                            "model": "claude-opus-4-5",
                            "resets_at": "2026-11-08T00:00:00Z",
                        },
                    ]
                }
            )
        ],
        seen,
    )
    provider = ClaudePersonalProvider(credentials_for(creds))
    windows = await provider.fetch(http)

    # Request shape: exact URL and headers per spec.
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == USAGE_URL
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert request.headers["user-agent"] == "claude-code/1.0.88"

    assert [w.kind for w in windows] == [
        WindowKind.five_hour,
        WindowKind.weekly,
        WindowKind.weekly,
    ]
    assert [w.label for w in windows] == [
        "5-hour session",
        "Weekly",
        "Weekly · claude-opus-4-5",
    ]
    assert [w.used_percent for w in windows] == [42.5, 10.0, 5.0]
    assert windows[0].resets_at == datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
    assert windows[1].resets_at is None
    assert windows[2].resets_at == datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
    # subscriptionType from the credentials file surfaces as detail.
    assert all(w.detail == "claude_code_max" for w in windows)


async def test_fallback_top_level_shape(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "five_hour": {"utilization": 30.5, "resets_at": "2026-11-05T18:00:00Z"},
                    "seven_day": {"utilization": 12.0, "resets_at": None},
                }
            )
        ],
        seen,
    )
    provider = ClaudePersonalProvider(credentials_for(creds))
    windows = await provider.fetch(http)

    assert [w.kind for w in windows] == [WindowKind.five_hour, WindowKind.weekly]
    assert [w.label for w in windows] == ["5-hour session", "Weekly"]
    assert [w.used_percent for w in windows] == [30.5, 12.0]
    assert windows[0].resets_at == datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)
    assert windows[1].resets_at is None


async def test_limits_array_preferred_over_top_level(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    seen: list[httpx.Request] = []
    http = make_http(
        [
            ok(
                {
                    "limits": [{"kind": "weekly_all", "percent": 7.0, "resets_at": None}],
                    "five_hour": {"utilization": 99.0, "resets_at": None},
                }
            )
        ],
        seen,
    )
    provider = ClaudePersonalProvider(credentials_for(creds))
    windows = await provider.fetch(http)

    assert [w.kind for w in windows] == [WindowKind.weekly]
    assert [w.used_percent for w in windows] == [7.0]


# ------------------------------------------------------- credentials handling


async def test_custom_credentials_path_used(tmp_path):
    creds = write_credentials(tmp_path, cred_payload(), name="my-oauth.json")
    seen: list[httpx.Request] = []
    http = make_http([ok({"limits": []})], seen)
    provider = ClaudePersonalProvider({"credentials_path": str(creds)})
    windows = await provider.fetch(http)
    assert windows == []


async def test_missing_credentials_file(tmp_path):
    provider = ClaudePersonalProvider(
        {"credentials_path": str(tmp_path / "does-not-exist.json")}
    )
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_unreadable_credentials_file(tmp_path):
    if hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() == 0:
        pytest.skip("permissions not enforced for root")
    creds = write_credentials(tmp_path, cred_payload())
    creds.chmod(0o000)
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([ok({})], [])
    try:
        with pytest.raises(ProviderError):
            await provider.fetch(http)
    finally:
        creds.chmod(0o600)


async def test_missing_access_token(tmp_path):
    creds = write_credentials(tmp_path, {"claudeAiOauth": {"refreshToken": "x"}})
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_missing_claude_oauth_block(tmp_path):
    creds = write_credentials(tmp_path, {"something": "else"})
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([ok({})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_credentials_file_not_written(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    before = creds.read_text()
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([ok({"limits": []})], [])
    await provider.fetch(http)
    assert creds.read_text() == before


# -------------------------------------------------------------- error shapes


async def test_http_401_raises(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([httpx.Response(401, json={"error": "bad token"})], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_http_429_raises(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http(
        [httpx.Response(429, headers={"Retry-After": "300"}, json={})], []
    )
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_invalid_json_raises(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([httpx.Response(200, text="<html>not json</html>")], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


async def test_non_object_json_raises(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([ok([1, 2, 3])], [])
    with pytest.raises(ProviderError):
        await provider.fetch(http)


# ---------------------------------------------------------- inline credentials


async def test_inline_access_token_used_and_files_never_read():
    """OAuth accounts carry tokens in credentials; no local file is touched."""
    seen: list[httpx.Request] = []
    http = make_http([ok({"limits": [{"kind": "session", "percent": 12.0}]})], seen)
    provider = ClaudePersonalProvider(
        {
            "access_token": ACCESS_TOKEN,
            "credentials_path": "/nonexistent/inline-test-path/creds.json",
        }
    )
    windows = await provider.fetch(http)

    assert len(seen) == 1
    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert [w.used_percent for w in windows] == [12.0]
    assert windows[0].detail is None  # no subscriptionType without a file


async def test_inline_credentials_do_not_require_credentials_path_key():
    seen: list[httpx.Request] = []
    http = make_http([ok({"limits": []})], seen)
    provider = ClaudePersonalProvider({"access_token": ACCESS_TOKEN})
    assert await provider.fetch(http) == []
    assert str(seen[0].url) == USAGE_URL


# ---------------------------------------------------------- tolerant parsing


async def test_empty_response_yields_no_windows(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http([ok({})], [])
    assert await provider.fetch(http) == []


async def test_unknown_limit_kind_omitted(tmp_path):
    creds = write_credentials(tmp_path, cred_payload())
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http(
        [ok({"limits": [{"kind": "daily", "percent": 5.0, "resets_at": None}]})], []
    )
    assert await provider.fetch(http) == []


async def test_absent_optional_fields_omitted_not_error(tmp_path):
    creds = write_credentials(tmp_path, {"claudeAiOauth": {"accessToken": ACCESS_TOKEN}})
    provider = ClaudePersonalProvider(credentials_for(creds))
    http = make_http(
        [
            ok(
                {
                    "five_hour": {"utilization": 3.0},  # no resets_at
                    "seven_day": {"resets_at": None},  # no utilization -> omitted
                }
            )
        ],
        [],
    )
    windows = await provider.fetch(http)
    assert [w.kind for w in windows] == [WindowKind.five_hour]
    assert windows[0].used_percent == 3.0
    assert windows[0].resets_at is None
    assert windows[0].detail is None  # no subscriptionType in credentials file
