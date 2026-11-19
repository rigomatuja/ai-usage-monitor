"""OAuth login flows for Claude and ChatGPT personal accounts (T2).

Pure OAuth core: PKCE/state generation, authorize URLs, code exchange, and
rotation-aware token refresh. No browser opening and no socket servers here —
those live in the API layer (worker B, odd/tasks/oauth-accounts.md T3).

Endpoint details are authoritative in odd/tasks/oauth-accounts.md
("OAuth endpoint details", researched 2026-11). Only
``secrets``/``hashlib``/``base64``/``httpx`` are used.

Rules (S3): refresh is rotation-aware — the caller must ALWAYS persist the
full credentials dict returned by ``refresh_tokens``. This module never
touches local CLI login files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any

import httpx

__all__ = [
    "ChatGPTOAuthClient",
    "ClaudeOAuthClient",
    "OAuthError",
    "challenge_from_verifier",
    "decode_chatgpt_identity",
    "generate_pkce_verifier",
    "generate_state",
    "should_refresh",
]


class OAuthError(Exception):
    """An OAuth flow step failed. Messages never contain token values."""


# ----------------------------------------------------------------- PKCE/state


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce_verifier() -> str:
    """Return a 43-char base64url PKCE code verifier (32 random bytes)."""
    return _b64url_nopad(secrets.token_bytes(32))


def challenge_from_verifier(verifier: str) -> str:
    """S256 challenge: unpadded base64url of SHA-256 of the verifier."""
    return _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())


def generate_state() -> str:
    """Return a 43-char base64url state (32 random bytes)."""
    return _b64url_nopad(secrets.token_bytes(32))


def _now_ms() -> int:
    return int(time.time() * 1000)


# -------------------------------------------------------------------- Claude


class ClaudeOAuthClient:
    """Sign in with Claude (public client of Claude Code)."""

    AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
    TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    SCOPE = "user:profile user:inference"
    CALLBACK_PATH = "/callback"
    # Default app port (odd log L2: "podemos usar algun puerto raro como
    # por ejemplo 61524"); worker B passes the actual app port.
    DEFAULT_PORT = 61524

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self.redirect_uri = f"http://localhost:{port}{self.CALLBACK_PATH}"

    def build_authorize_url(self, verifier: str, state: str) -> str:
        params = {
            "code": "true",
            "client_id": self.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": self.SCOPE,
            "code_challenge": challenge_from_verifier(verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
        query = "&".join(
            f"{name}={_url_quote(value)}" for name, value in params.items()
        )
        return f"{self.AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self, http: httpx.AsyncClient, code: str, verifier: str, state: str
    ) -> dict[str, str]:
        """Exchange an authorization code for token credentials.

        The returned code may arrive as ``{code}#{state}``; split on ``#`` and
        validate the suffix against the expected state.
        """
        actual_code, actual_state = self._split_code(code, state)
        body = {
            "grant_type": "authorization_code",
            "code": actual_code,
            "state": actual_state,
            "client_id": self.CLIENT_ID,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        payload = await self._post_json(http, body)
        required = ("access_token", "refresh_token", "expires_in")
        missing = [key for key in required if _missing(payload, key)]
        if missing:
            raise OAuthError(
                f"Claude token exchange response is missing {', '.join(missing)}"
            )
        email = None
        account = payload.get("account")
        if isinstance(account, dict) and isinstance(
            account.get("email_address"), str
        ):
            email = account["email_address"]
        credentials: dict[str, str] = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": _now_ms() + int(payload["expires_in"]) * 1000,
        }
        if email:
            credentials["email"] = email
        return credentials

    @staticmethod
    def _split_code(code: str, state: str) -> tuple[str, str]:
        if "#" in code:
            actual_code, _, suffix = code.partition("#")
            if suffix != state:
                raise OAuthError("OAuth state mismatch")
            return actual_code, state
        return code, state

    async def _post_json(
        self, http: httpx.AsyncClient, body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = await http.post(self.TOKEN_URL, json=body)
        except httpx.HTTPError as exc:
            raise OAuthError(f"OAuth token request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise OAuthError(
                f"OAuth token request returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthError("OAuth token response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise OAuthError("OAuth token response was not a JSON object")
        return payload

    async def refresh_tokens(
        self, http: httpx.AsyncClient, credentials: dict[str, str]
    ) -> dict[str, str]:
        """Refresh via the refresh token; Claude ALWAYS rotates — the caller
        must persist the returned dict in full."""
        body = {
            "grant_type": "refresh_token",
            "refresh_token": credentials.get("refresh_token", ""),
            "client_id": self.CLIENT_ID,
        }
        payload = await self._post_json(http, body)
        return self._credentials_from_refresh(payload, credentials)

    def _credentials_from_refresh(
        self, payload: dict[str, Any], old: dict[str, str]
    ) -> dict[str, str]:
        if _missing(payload, "access_token"):
            raise OAuthError("OAuth refresh response is missing access_token")
        new: dict[str, str] = dict(old)
        new["access_token"] = payload["access_token"]
        # Rotation-aware: persist whatever refresh_token comes back; Claude
        # rotates on every refresh, but keep the old one if none is returned.
        if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
            new["refresh_token"] = payload["refresh_token"]
        if _missing(payload, "expires_in"):
            raise OAuthError("OAuth refresh response is missing expires_in")
        new["expires_at"] = _now_ms() + int(payload["expires_in"]) * 1000
        return new


# ------------------------------------------------------------------- ChatGPT


def decode_chatgpt_identity(access_token: str) -> dict[str, str] | None:
    """Decode the ChatGPT access-token JWT payload (no signature check).

    Returns ``{"account_id", "plan_type"?, "email"?}`` or ``None`` when the
    token or the auth claim is unparseable.
    """
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(payload, dict):
            return None
        auth = payload.get("https://api.openai.com/auth")
        if not isinstance(auth, dict):
            return None
        account_id = auth.get("chatgpt_account_id")
        if not isinstance(account_id, str) or not account_id:
            return None
        identity = {"account_id": account_id}
        plan_type = auth.get("chatgpt_plan_type")
        if isinstance(plan_type, str) and plan_type:
            identity["plan_type"] = plan_type
        profile = payload.get("https://api.openai.com/profile")
        if isinstance(profile, dict):
            email = profile.get("email")
            if isinstance(email, str) and email:
                identity["email"] = email
        return identity
    except (ValueError, TypeError):
        return None


class ChatGPTOAuthClient:
    """Sign in with ChatGPT (public client of Codex CLI)."""

    AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
    TOKEN_URL = "https://auth.openai.com/oauth/token"
    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    SCOPE = "openid profile email offline_access"
    CALLBACK_PATH = "/auth/callback"
    # Port 1455 is FIXED in the client's allow-list; injectable for tests.
    DEFAULT_PORT = 1455

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self.redirect_uri = f"http://localhost:{port}{self.CALLBACK_PATH}"

    def build_authorize_url(self, verifier: str, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.CLIENT_ID,
            "redirect_uri": self.redirect_uri,
            "scope": self.SCOPE,
            "code_challenge": challenge_from_verifier(verifier),
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "codex_cli_rs",
        }
        query = "&".join(
            f"{name}={_url_quote(value)}" for name, value in params.items()
        )
        return f"{self.AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self, http: httpx.AsyncClient, code: str, verifier: str, state: str
    ) -> dict[str, str]:
        body = {
            "grant_type": "authorization_code",
            "client_id": self.CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": self.redirect_uri,
        }
        payload = await _post_form(http, self.TOKEN_URL, body)
        required = ("access_token", "refresh_token", "expires_in")
        missing = [key for key in required if _missing(payload, key)]
        if missing:
            raise OAuthError(
                f"ChatGPT token exchange response is missing {', '.join(missing)}"
            )
        identity = decode_chatgpt_identity(payload["access_token"])
        if identity is None:
            raise OAuthError(
                "ChatGPT access token does not carry an account id claim"
            )
        credentials = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": _now_ms() + int(payload["expires_in"]) * 1000,
            "account_id": identity["account_id"],
        }
        if "email" in identity:
            credentials["email"] = identity["email"]
        return credentials

    async def refresh_tokens(
        self, http: httpx.AsyncClient, credentials: dict[str, str]
    ) -> dict[str, str]:
        """Refresh; the refresh token may rotate — persist whatever comes back."""
        body = {
            "grant_type": "refresh_token",
            "refresh_token": credentials.get("refresh_token", ""),
            "client_id": self.CLIENT_ID,
        }
        payload = await _post_form(http, self.TOKEN_URL, body)
        if _missing(payload, "access_token"):
            raise OAuthError("OAuth refresh response is missing access_token")
        if _missing(payload, "expires_in"):
            raise OAuthError("OAuth refresh response is missing expires_in")
        new = dict(credentials)
        new["access_token"] = payload["access_token"]
        if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
            new["refresh_token"] = payload["refresh_token"]
        new["expires_at"] = _now_ms() + int(payload["expires_in"]) * 1000
        identity = decode_chatgpt_identity(payload["access_token"])
        if identity is not None:
            new["account_id"] = identity["account_id"]
            if "email" in identity:
                new["email"] = identity["email"]
            # No email in the new token -> keep the existing one (already in new).
        return new


# ------------------------------------------------------------------ helpers


def _missing(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    return value is None or value == ""


def _url_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


async def _post_form(
    http: httpx.AsyncClient, url: str, body: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await http.post(url, data=body)
    except httpx.HTTPError as exc:
        raise OAuthError(f"OAuth token request failed: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise OAuthError(f"OAuth token request returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthError("OAuth token response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OAuthError("OAuth token response was not a JSON object")
    return payload


def should_refresh(
    credentials: dict[str, str], now_ms: int, threshold_ms: int = 60_000
) -> bool:
    """True when the token is expired, within ``threshold_ms`` of expiry, or
    ``expires_at`` is missing/unparseable."""
    raw = credentials.get("expires_at")
    try:
        expires_at = int(str(raw))
    except (TypeError, ValueError):
        return True
    return now_ms >= expires_at - threshold_ms
