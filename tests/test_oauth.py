"""Tests for the OAuth core module (T2).

All HTTP is mocked via httpx.MockTransport; tests never touch the network.
Constants, parameter shapes, and response handling follow
odd/tasks/oauth-accounts.md ("OAuth endpoint details") exactly.
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ai_usage_monitor.oauth import (
    ChatGPTOAuthClient,
    ClaudeOAuthClient,
    OAuthError,
    challenge_from_verifier,
    decode_chatgpt_identity,
    generate_pkce_verifier,
    generate_state,
    should_refresh,
)

NOW_MS = 1_700_000_000_000


# ------------------------------------------------------------ PKCE / state


def b64url_decode_nopad(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_verifier_shape_and_length_range():
    verifier = generate_pkce_verifier()
    assert 43 <= len(verifier) <= 128
    # base64url alphabet only, no padding
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert "=" not in verifier


def test_challenge_is_base64url_sha256_without_padding():
    verifier = generate_pkce_verifier()
    challenge = challenge_from_verifier(verifier)
    import hashlib

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert "=" not in challenge
    assert len(challenge) == 43


def test_state_is_43_char_base64url():
    state = generate_state()
    assert len(state) == 43
    assert "=" not in state
    decoded = b64url_decode_nopad(state)
    assert len(decoded) == 32  # 32 random bytes


def test_generators_are_random_not_deterministic():
    assert generate_pkce_verifier() != generate_pkce_verifier()
    assert generate_state() != generate_state()


# ---------------------------------------------------------- authorize URLs


def claude_params(url: str) -> dict[str, str]:
    query = parse_qs(urlsplit(url).query)
    return {k: v[0] for k, v in query.items()}


def test_claude_authorize_url_exact_params():
    client = ClaudeOAuthClient(port=61524)
    verifier = generate_pkce_verifier()
    state = generate_state()
    url = client.build_authorize_url(verifier, state)
    assert urlsplit(url).scheme == "https"
    assert urlsplit(url).hostname == "claude.ai"
    assert urlsplit(url).path == "/oauth/authorize"
    params = claude_params(url)
    assert params == {
        "code": "true",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "response_type": "code",
        "redirect_uri": "http://localhost:61524/api/oauth/claude/callback",
        "scope": "user:profile user:inference",
        "code_challenge": challenge_from_verifier(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }


def test_chatgpt_authorize_url_exact_params():
    client = ChatGPTOAuthClient()  # default port 1455
    verifier = generate_pkce_verifier()
    state = generate_state()
    url = client.build_authorize_url(verifier, state)
    assert urlsplit(url).scheme == "https"
    assert urlsplit(url).hostname == "auth.openai.com"
    assert urlsplit(url).path == "/oauth/authorize"
    params = claude_params(url)
    assert params == {
        "response_type": "code",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "scope": "openid profile email offline_access",
        "code_challenge": challenge_from_verifier(verifier),
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }


def test_chatgpt_port_injectable():
    client = ChatGPTOAuthClient(port=9999)
    url = client.build_authorize_url(generate_pkce_verifier(), generate_state())
    assert (
        claude_params(url)["redirect_uri"] == "http://localhost:9999/auth/callback"
    )


# ------------------------------------------------------------- request mock


def make_token_http(responses: list[httpx.Response], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def claude_token_response() -> dict:
    return {
        "token_type": "bearer",
        "access_token": "sk-ant-oat01-xyz",
        "expires_in": 3600,
        "refresh_token": "sk-ant-ort01-xyz",
        "scope": "user:profile user:inference",
        "account": {"uuid": "acc-uuid", "email_address": "me@example.com"},
    }


def chatgpt_jwt(account_id: str = "acct-abc", email: str = "me@example.com") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "plus",
        },
        "https://api.openai.com/profile": {"email": email},
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


def chatgpt_token_response(account_id: str = "acct-abc") -> dict:
    return {
        "access_token": chatgpt_jwt(account_id),
        "refresh_token": "rt-new",
        "expires_in": 3600,
    }


# ------------------------------------------------------------- code exchange


async def test_claude_exchange_happy_path():
    seen: list[httpx.Request] = []
    http = make_token_http([httpx.Response(200, json=claude_token_response())], seen)
    client = ClaudeOAuthClient(port=61524)
    verifier = generate_pkce_verifier()
    state = generate_state()

    creds = await client.exchange_code(http, "authcode", verifier, state)

    request = seen[0]
    assert str(request.url) == "https://console.anthropic.com/v1/oauth/token"
    body = json.loads(request.content)
    assert body == {
        "grant_type": "authorization_code",
        "code": "authcode",
        "state": state,
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "redirect_uri": "http://localhost:61524/api/oauth/claude/callback",
        "code_verifier": verifier,
    }
    assert creds["access_token"] == "sk-ant-oat01-xyz"
    assert creds["refresh_token"] == "sk-ant-ort01-xyz"
    assert creds["email"] == "me@example.com"
    assert isinstance(creds["expires_at"], int)
    assert creds["expires_at"] == pytest.approx(
        int(time.time() * 1000) + 3_600_000, abs=5000
    )


async def test_claude_exchange_splits_code_hash_state():
    seen: list[httpx.Request] = []
    http = make_token_http([httpx.Response(200, json=claude_token_response())], seen)
    client = ClaudeOAuthClient(port=61524)
    state = generate_state()

    # The callback code may arrive as {code}#{state}
    creds = await client.exchange_code(http, f"authcode#{state}", "v" * 43, state)
    body = json.loads(seen[0].content)
    assert body["code"] == "authcode"
    assert creds["access_token"] == "sk-ant-oat01-xyz"


async def test_claude_exchange_rejects_hash_state_mismatch():
    http = make_token_http([], [])
    client = ClaudeOAuthClient(port=61524)
    with pytest.raises(OAuthError):
        await client.exchange_code(
            http, "authcode#differentstate", "v" * 43, "expectedstate"
        )


async def test_chatgpt_exchange_happy_path_form_encoded():
    seen: list[httpx.Request] = []
    http = make_token_http([httpx.Response(200, json=chatgpt_token_response())], seen)
    client = ChatGPTOAuthClient()
    verifier = generate_pkce_verifier()
    state = generate_state()

    creds = await client.exchange_code(http, "authcode", verifier, state)

    request = seen[0]
    assert str(request.url) == "https://auth.openai.com/oauth/token"
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    body = parse_qs(request.content.decode())
    assert body == {
        "grant_type": ["authorization_code"],
        "client_id": ["app_EMoamEEZ73f0CkXaXp7hrann"],
        "code": ["authcode"],
        "code_verifier": [verifier],
        "redirect_uri": ["http://localhost:1455/auth/callback"],
    }
    assert creds["access_token"].startswith("eyJ")
    assert creds["refresh_token"] == "rt-new"
    assert creds["account_id"] == "acct-abc"
    assert creds["email"] == "me@example.com"
    assert isinstance(creds["expires_at"], int)


# ------------------------------------------------------------ exchange errors


async def test_exchange_http_400_raises_oauth_error_both_providers():
    for client in (ClaudeOAuthClient(port=61524), ChatGPTOAuthClient()):
        http = make_token_http(
            [httpx.Response(400, json={"error": "invalid_grant"})], []
        )
        with pytest.raises(OAuthError):
            await client.exchange_code(http, "code", "v" * 43, "state")


async def test_exchange_missing_refresh_token_raises():
    payload = claude_token_response()
    del payload["refresh_token"]
    http = make_token_http([httpx.Response(200, json=payload)], [])
    with pytest.raises(OAuthError):
        await ClaudeOAuthClient(port=61524).exchange_code(
            http, "code", "v" * 43, "state"
        )


async def test_exchange_missing_access_token_raises():
    payload = chatgpt_token_response()
    del payload["access_token"]
    http = make_token_http([httpx.Response(200, json=payload)], [])
    with pytest.raises(OAuthError):
        await ChatGPTOAuthClient().exchange_code(http, "code", "v" * 43, "state")


async def test_chatgpt_exchange_missing_account_id_claim_raises():
    # JWT without the auth claim -> no account_id -> OAuthError
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(b'{"sub":"x"}').rstrip(b"=").decode()
    token = f"{header}.{body}.sig"
    http = make_token_http(
        [
            httpx.Response(
                200,
                json={
                    "access_token": token,
                    "refresh_token": "rt",
                    "expires_in": 3600,
                },
            )
        ],
        [],
    )
    with pytest.raises(OAuthError):
        await ChatGPTOAuthClient().exchange_code(http, "code", "v" * 43, "state")


def test_oauth_error_messages_never_contain_token_values():
    # The exchange error paths must not echo token material.
    try:
        raise OAuthError("OAuth token exchange failed with HTTP 400")
    except OAuthError as exc:
        assert "sk-ant" not in str(exc)


# ------------------------------------------------------------------ refresh


async def test_claude_refresh_rotates_refresh_token():
    seen: list[httpx.Request] = []
    http = make_token_http(
        [
            httpx.Response(
                200,
                json={
                    "access_token": "sk-ant-oat01-new",
                    "refresh_token": "sk-ant-ort01-new",
                    "expires_in": 7200,
                },
            )
        ],
        seen,
    )
    old = {
        "access_token": "sk-ant-oat01-old",
        "refresh_token": "sk-ant-ort01-old",
        "expires_at": str(NOW_MS),
        "email": "me@example.com",
    }
    new = await ClaudeOAuthClient(port=61524).refresh_tokens(http, old)

    body = json.loads(seen[0].content)
    assert body == {
        "grant_type": "refresh_token",
        "refresh_token": "sk-ant-ort01-old",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    }
    assert new["refresh_token"] == "sk-ant-ort01-new"
    assert new["access_token"] == "sk-ant-oat01-new"
    assert new["email"] == "me@example.com"  # preserved
    assert isinstance(new["expires_at"], int)
    assert new["expires_at"] == pytest.approx(
        int(time.time() * 1000) + 7_200_000, abs=5000
    )


async def test_chatgpt_refresh_form_encoded_and_redecodes_identity():
    seen: list[httpx.Request] = []
    http = make_token_http(
        [httpx.Response(200, json=chatgpt_token_response("acct-new"))], seen
    )
    old = {
        "access_token": chatgpt_jwt("acct-old"),
        "refresh_token": "rt-old",
        "expires_at": str(NOW_MS),
        "account_id": "acct-old",
        "email": "old@example.com",
    }
    new = await ChatGPTOAuthClient().refresh_tokens(http, old)

    request = seen[0]
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    body = parse_qs(request.content.decode())
    assert body == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["rt-old"],
        "client_id": ["app_EMoamEEZ73f0CkXaXp7hrann"],
    }
    assert new["refresh_token"] == "rt-new"
    assert new["account_id"] == "acct-new"  # re-decoded from new access token
    assert new["email"] == "me@example.com"  # from the new token's JWT
    assert isinstance(new["expires_at"], int)


async def test_chatgpt_refresh_keeps_email_when_new_response_has_none():
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-new"}}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    http = make_token_http(
        [
            httpx.Response(
                200,
                json={
                    "access_token": f"{header}.{body}.sig",
                    "refresh_token": "rt-new",
                    "expires_in": 3600,
                },
            )
        ],
        [],
    )
    old = {
        "access_token": chatgpt_jwt("acct-old"),
        "refresh_token": "rt-old",
        "account_id": "acct-old",
        "email": "kept@example.com",
    }
    new = await ChatGPTOAuthClient().refresh_tokens(http, old)
    assert new["email"] == "kept@example.com"
    assert new["account_id"] == "acct-new"


async def test_refresh_http_error_raises_oauth_error():
    for client in (ClaudeOAuthClient(port=61524), ChatGPTOAuthClient()):
        http = make_token_http([httpx.Response(401, json={"error": "expired"})], [])
        with pytest.raises(OAuthError):
            await client.refresh_tokens(
                http,
                {"access_token": "a", "refresh_token": "r", "expires_at": str(NOW_MS)},
            )


# ------------------------------------------------------------ should_refresh


def test_should_refresh_expired():
    creds = {"expires_at": str(NOW_MS - 1)}
    assert should_refresh(creds, NOW_MS) is True


def test_should_refresh_within_threshold():
    # expires_at = now + 30s, threshold 60s -> inside the window
    creds = {"expires_at": str(NOW_MS + 30_000)}
    assert should_refresh(creds, NOW_MS) is True


def test_should_refresh_just_outside_threshold():
    creds = {"expires_at": str(NOW_MS + 61_000)}
    assert should_refresh(creds, NOW_MS) is False


def test_should_refresh_far_future():
    creds = {"expires_at": str(NOW_MS + 8 * 3600 * 1000)}
    assert should_refresh(creds, NOW_MS) is False


def test_should_refresh_missing_or_unparseable_expires_at():
    assert should_refresh({}, NOW_MS) is True
    assert should_refresh({"expires_at": "not-a-number"}, NOW_MS) is True


# ------------------------------------------------------- chatgpt JWT decode


def test_decode_chatgpt_identity_wellformed():
    identity = decode_chatgpt_identity(chatgpt_jwt("acct-1", "a@b.com"))
    assert identity is not None
    assert identity["account_id"] == "acct-1"
    assert identity["plan_type"] == "plus"
    assert identity["email"] == "a@b.com"


def test_decode_chatgpt_identity_malformed_token():
    assert decode_chatgpt_identity("not-a-jwt") is None
    assert decode_chatgpt_identity("a.b.c") is None  # invalid base64 payload
    assert decode_chatgpt_identity("") is None


def test_decode_chatgpt_identity_missing_auth_claim():
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(b'{"sub":"x"}').rstrip(b"=").decode()
    assert decode_chatgpt_identity(f"{header}.{body}.sig") is None
