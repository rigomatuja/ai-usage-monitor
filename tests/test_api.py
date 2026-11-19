"""Tests for the REST API contract (tests/test_static_assets.py pins the
frontend side of the same contract)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_usage_monitor.models import ProviderKind, UsageWindow, WindowKind
from ai_usage_monitor.oauth import OAuthError
from ai_usage_monitor.poller import UsagePoller
from ai_usage_monitor.registry import AccountRegistry


class FakeProvider:
    def __init__(self, credentials):
        self.credentials = dict(credentials)

    async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]:
        return [
            UsageWindow(
                kind=WindowKind.five_hour,
                label="5-hour session",
                used_percent=12.5,
                detail="Pro plan",
                resets_at=None,
            )
        ]


@pytest.fixture
def client(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text("{}")
    registry = AccountRegistry(
        config_dir=tmp_path / "config",
        claude_credentials_path=home / ".claude" / ".credentials.json",
        codex_auth_path=home / ".codex" / "auth.json",
    )
    poller = UsagePoller(
        registry, provider_factory=lambda config: FakeProvider(config.credentials)
    )
    from ai_usage_monitor.app import create_app

    app = create_app(registry=registry, poller=poller, start_background=False)
    with TestClient(app) as test_client:
        yield test_client


class TestAccountsEndpoints:
    def test_list_accounts_includes_auto(self, client):
        response = client.get("/api/accounts")
        assert response.status_code == 200
        accounts = response.json()
        assert [a["id"] for a in accounts] == ["auto-chatgpt-personal"]
        assert accounts[0]["source"] == "auto"
        assert accounts[0]["platform"] == "openai"
        assert accounts[0]["provider"] == "chatgpt_personal"

    def test_add_manual_account(self, client):
        response = client.post(
            "/api/accounts",
            json={
                "provider": "anthropic_org",
                "label": "Work org",
                "credentials": {"api_key": "sk-ant-admin-x"},
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["source"] == "manual"
        assert body["platform"] == "claude"
        assert body["provider"] == "anthropic_org"
        # The new account is listed and was polled immediately.
        assert any(a["id"] == body["id"] for a in client.get("/api/accounts").json())
        usage = client.get("/api/usage").json()
        assert any(u["account_id"] == body["id"] and u["status"] == "ok"
                   for u in usage)

    def test_add_account_ignores_client_supplied_origin(self, client):
        response = client.post(
            "/api/accounts",
            json={"provider": "claude_personal", "label": "Manual", "origin": "oauth"},
        )

        assert response.status_code == 201
        account = client.app.state.registry.get_config(response.json()["id"])
        assert account.origin == "manual"
        assert response.json()["source"] == "manual"
        assert "origin" not in response.json()

    def test_add_account_unknown_provider_is_422(self, client):
        response = client.post(
            "/api/accounts",
            json={"provider": "not-a-provider", "label": "x", "credentials": {}},
        )
        assert response.status_code == 422

    def test_add_account_blank_label_is_422(self, client):
        response = client.post(
            "/api/accounts",
            json={"provider": "openai_org", "label": "", "credentials": {}},
        )
        assert response.status_code == 422

    def test_delete_manual_account_returns_204(self, client):
        body = client.post(
            "/api/accounts",
            json={"provider": "openai_org", "label": "Org", "credentials": {}},
        ).json()
        response = client.delete(f"/api/accounts/{body['id']}")
        assert response.status_code == 204
        assert all(a["id"] != body["id"]
                   for a in client.get("/api/accounts").json())

    def test_delete_auto_account_returns_409(self, client):
        response = client.delete("/api/accounts/auto-chatgpt-personal")
        assert response.status_code == 409

    def test_delete_unknown_account_returns_404(self, client):
        response = client.delete("/api/accounts/nope")
        assert response.status_code == 404


class TestUsageEndpoints:
    def test_usage_empty_before_first_poll(self, client):
        assert client.get("/api/usage").json() == []

    def test_refresh_returns_snapshot(self, client):
        response = client.post("/api/usage/refresh/auto-chatgpt-personal")
        assert response.status_code == 200
        body = response.json()
        assert body["account_id"] == "auto-chatgpt-personal"
        assert body["status"] == "ok"
        assert body["windows"][0]["kind"] == "five_hour"
        assert body["windows"][0]["used_percent"] == 12.5

    def test_refresh_lists_after_poll(self, client):
        client.post("/api/usage/refresh/auto-chatgpt-personal")
        usage = client.get("/api/usage").json()
        assert [u["account_id"] for u in usage] == ["auto-chatgpt-personal"]

    def test_refresh_unknown_account_returns_404(self, client):
        response = client.post("/api/usage/refresh/nope")
        assert response.status_code == 404


# ---------------------------------------------------------------- OAuth (T3)


class FakeOAuthClient:
    """Fake oauth client (worker A contract) injected via app.state."""

    def __init__(self, exchange_result):
        self.exchange_result = dict(exchange_result)
        self.authorize_calls: list[tuple[str, str]] = []
        self.exchange_calls: list[tuple[str, str, str]] = []
        self.fail_exchange = False

    def build_authorize_url(self, verifier, state):
        self.authorize_calls.append((verifier, state))
        return f"https://auth.example.com/authorize?state={state}"

    async def exchange_code(self, http, code, verifier, state):
        self.exchange_calls.append((code, verifier, state))
        if self.fail_exchange:
            raise OAuthError("exchange rejected")
        return dict(self.exchange_result)


CLAUDE_EXCHANGE = {
    "access_token": "sk-ant-oat01-t",
    "refresh_token": "sk-ant-ort01-t",
    "expires_at": 1893456000000,
    "email": "user@example.com",
}

CHATGPT_EXCHANGE = {
    "access_token": "chatgpt-token",
    "refresh_token": "chatgpt-rt",
    "expires_at": 1893456000000,
    "account_id": "acct-123",
    "email": "gpt@example.com",
}


DASHBOARD_HEADERS = {"Origin": "http://127.0.0.1:61524"}


def _state(login):
    return parse_qs(urlsplit(login.json()["authorize_url"]).query)["state"][0]


def _attempt(login):
    return f"/api/oauth/attempts/{login.json()['attempt_id']}"


@pytest.fixture
def oauth_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    registry = AccountRegistry(
        config_dir=tmp_path / "config",
        claude_credentials_path=home / ".claude" / ".credentials.json",
        codex_auth_path=home / ".codex" / "auth.json",
    )
    poller = UsagePoller(
        registry, provider_factory=lambda config: FakeProvider(config.credentials)
    )
    claude = FakeOAuthClient(CLAUDE_EXCHANGE)
    chatgpt = FakeOAuthClient(CHATGPT_EXCHANGE)
    listener_handlers = []

    async def fake_listener(port, handler):
        await asyncio.sleep(0)
        listener_handlers.append((port, handler))
        return asyncio.get_running_loop().create_future()

    from ai_usage_monitor.app import create_app

    app = create_app(
        registry=registry,
        poller=poller,
        start_background=False,
        oauth_clients={
            "claude_personal": claude,
            "chatgpt_personal": chatgpt,
        },
        oauth_callback_port=1455,
        oauth_listener_factory=fake_listener,
        app_port=61524,
    )
    with TestClient(app, headers=DASHBOARD_HEADERS) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            registry=registry,
            claude=claude,
            chatgpt=chatgpt,
            listener_handlers=listener_handlers,
        )


def _login_claude(env) -> str:
    response = env.client.post("/api/oauth/claude_personal/login")
    assert response.status_code == 200
    return env.claude.authorize_calls[-1][1]


def _login_chatgpt(env):
    response = env.client.post("/api/oauth/chatgpt_personal/login")
    assert response.status_code == 200
    return env.listener_handlers[-1][1]


def test_listener_deadline_extend_resets_full_timeout():
    from ai_usage_monitor.api import _ListenerDeadline

    now = 100
    deadline = _ListenerDeadline(timeout=600, clock=lambda: now)
    assert deadline.remaining() == 600
    now += 599
    assert deadline.remaining() == 1
    deadline.extend()
    assert deadline.remaining() == 600
    now += 600
    assert deadline.remaining() == 0


class TestOAuthLogin:
    @pytest.mark.parametrize("host, headers", [
        ("127.0.0.1", {"Origin": "http://foreign.invalid"}),
        ("localhost", {"Origin": "http://localhost:61524", "Sec-Fetch-Site": "cross-site"}),
        ("127.0.0.1", {}),
    ])
    def test_cross_origin_login_preserves_pending(self, oauth_env, host, headers):
        path = "/api/oauth/claude_personal/login"
        client = oauth_env.client
        login = client.post(path, headers={"Origin": f"http://{host}:61524"})
        assert login.status_code == 200
        # Build a raw request so the missing-Origin case does not inherit defaults.
        response = client.send(httpx.Request("POST", str(login.request.url), headers=headers))
        assert (response.status_code, response.json()) == (403, {"detail": "Cross-origin OAuth login denied"})
        assert len(oauth_env.claude.authorize_calls) == 1
        assert client.get(_attempt(login)).json() == {"status": "pending"}
        assert client.get("/api/accounts").json() == []
        assert client.get("/api/oauth/claude/callback", params={"code": "auth-code", "state": _state(login)}).status_code == 200

    def test_claude_login_returns_authorize_url(self, oauth_env):
        response = oauth_env.client.post("/api/oauth/claude_personal/login")
        assert response.status_code == 200
        verifier, state = oauth_env.claude.authorize_calls[0]
        assert verifier and state
        attempt_id = response.json()["attempt_id"]
        assert attempt_id and response.json()["authorize_url"] == f"https://auth.example.com/authorize?state={state}"
        assert oauth_env.client.get(_attempt(response)).json() == {"status": "pending"}

    def test_chatgpt_login_starts_listener_on_injected_port(self, oauth_env):
        response = oauth_env.client.post("/api/oauth/chatgpt_personal/login")
        assert response.status_code == 200
        assert "authorize_url" in response.json()
        assert len(oauth_env.listener_handlers) == 1
        port, handler = oauth_env.listener_handlers[0]
        assert port == 1455
        assert callable(handler)

    def test_claude_login_does_not_start_listener(self, oauth_env):
        response = oauth_env.client.post("/api/oauth/claude_personal/login")
        assert response.status_code == 200
        assert oauth_env.listener_handlers == []

    def test_claude_logins_complete_independently(self, oauth_env):
        client = oauth_env.client
        path = "/api/oauth/claude_personal/login"
        first, second = client.post(path), client.post(path)
        assert first.status_code == second.status_code == 200
        for login in (first, second):
            assert client.get(_attempt(login)).json() == {"status": "pending"}
            assert client.get("/api/oauth/claude/callback", params={"code": "auth-code", "state": _state(login)}).status_code == 200
            assert client.get(_attempt(login)).json() == {"status": "completed"}
        assert len(client.get("/api/accounts").json()) == 2

    async def test_concurrent_chatgpt_logins(self, oauth_env):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=oauth_env.app), base_url="http://test",
            headers=DASHBOARD_HEADERS,
        ) as client:
            path = "/api/oauth/chatgpt_personal/login"
            first, second = await asyncio.gather(client.post(path), client.post(path))
            assert first.status_code == second.status_code == 200
            assert len(oauth_env.listener_handlers) == 1
            _, complete = oauth_env.listener_handlers[0]
            assert await complete("auth-code", _state(first)) == "http://127.0.0.1:61524/"
            assert (await client.get(_attempt(first))).json() == {"status": "completed"}
            assert (await client.get(_attempt(second))).json() == {"status": "pending"}
            assert [a["label"] for a in (await client.get("/api/accounts")).json()] == ["gpt@example.com"]

    async def test_finished_history_keeps_old_pending_attempt(self, oauth_env):
        client = oauth_env.client
        pending = client.post("/api/oauth/chatgpt_personal/login")
        assert pending.status_code == 200
        finished = []
        oauth_env.claude.fail_exchange = True
        for _ in range(60):
            login = client.post("/api/oauth/claude_personal/login")
            assert login.status_code == 200
            response = client.get("/api/oauth/claude/callback", params={"code": "auth-code", "state": _state(login)})
            assert response.status_code == 400
            finished.append(_attempt(login))
        assert client.get(_attempt(pending)).json() == {"status": "pending"}
        assert client.get("/api/accounts").json() == []
        for path in finished[:10]:
            assert client.get(path).status_code == 404
        for path in finished[10:]:
            assert client.get(path).json() == {"status": "failed"}
        _, complete = oauth_env.listener_handlers[0]
        assert await complete("auth-code", _state(pending)) == "http://127.0.0.1:61524/"
        assert client.get(_attempt(pending)).json() == {"status": "completed"}
        assert client.get(finished[10]).status_code == 404
        assert [a["label"] for a in client.get("/api/accounts").json()] == ["gpt@example.com"]

    def test_chatgpt_relogin_extends_live_listener_deadline(self, oauth_env, monkeypatch):
        from ai_usage_monitor import api

        now = 100
        monkeypatch.setattr(api, "monotonic", lambda: now, raising=False)
        _login_chatgpt(oauth_env)
        deadline = oauth_env.app.state.oauth_listener_deadline
        now += api.CHATGPT_LISTENER_TIMEOUT_SECONDS - 1
        assert deadline.remaining() == 1

        _login_chatgpt(oauth_env)

        assert len(oauth_env.listener_handlers) == 1
        assert oauth_env.app.state.oauth_listener_deadline is deadline
        assert deadline.remaining() == api.CHATGPT_LISTENER_TIMEOUT_SECONDS

    def test_unknown_provider_is_404(self, oauth_env):
        for provider in ("anthropic_org", "openai_org", "nope"):
            response = oauth_env.client.post(f"/api/oauth/{provider}/login")
            assert response.status_code == 404

    def test_listener_port_in_use_returns_503(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        registry = AccountRegistry(
            config_dir=tmp_path / "config",
            claude_credentials_path=home / ".claude" / ".credentials.json",
            codex_auth_path=home / ".codex" / "auth.json",
        )
        poller = UsagePoller(
            registry, provider_factory=lambda config: FakeProvider(config.credentials)
        )

        async def busy_listener(port, handler):
            raise OSError("address already in use")

        from ai_usage_monitor.app import create_app

        app = create_app(
            registry=registry,
            poller=poller,
            start_background=False,
            oauth_clients={"chatgpt_personal": FakeOAuthClient(CHATGPT_EXCHANGE)},
            oauth_listener_factory=busy_listener,
        )
        with TestClient(app, headers=DASHBOARD_HEADERS) as client:
            response = client.post("/api/oauth/chatgpt_personal/login")
            assert response.status_code == 503
            assert response.json() == {"detail": "could not start the local callback listener on port 1455; it may already be in use"}
            assert client.get("/api/accounts").json() == []
        assert not app.state.oauth_pending
        assert not app.state.oauth_attempts


class TestClaudeCallback:
    def test_happy_path_creates_manual_account_and_polls(self, oauth_env):
        client = oauth_env.client
        assert client.get("/api/oauth/attempts/unknown").status_code == 404
        login = client.post("/api/oauth/claude_personal/login")
        attempt = f"/api/oauth/attempts/{login.json()['attempt_id']}"
        assert client.get(attempt).json() == {"status": "pending"}
        state = oauth_env.claude.authorize_calls[0][1]

        response = oauth_env.client.get(
            "/api/oauth/claude/callback",
            params={"code": "auth-code", "state": state},
        )

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "url=/" in response.text
        assert client.get(attempt).json() == {"status": "completed"}
        # The oauth client received the pending verifier and state.
        code, verifier, state_arg = oauth_env.claude.exchange_calls[0]
        assert code == "auth-code"
        assert state_arg == state
        assert verifier == oauth_env.claude.authorize_calls[0][0]
        # A manual account was created with the exchanged token credentials
        # (expires_at epoch-ms int stored as string) and the email label.
        infos = oauth_env.registry.list_infos()
        assert [i.provider for i in infos] == [ProviderKind.claude_personal]
        assert infos[0].label == "user@example.com"
        assert infos[0].source == "manual"
        config = oauth_env.registry.get_config(infos[0].id)
        assert config.credentials == {
            "access_token": "sk-ant-oat01-t",
            "refresh_token": "sk-ant-ort01-t",
            "expires_at": "1893456000000",
            "email": "user@example.com",
        }
        assert config.origin == "oauth"
        # The new account was polled immediately.
        usage = oauth_env.client.get("/api/usage").json()
        assert any(
            u["account_id"] == infos[0].id and u["status"] == "ok" for u in usage
        )

    def test_code_state_twist_is_accepted(self, oauth_env):
        state = _login_claude(oauth_env)

        response = oauth_env.client.get(
            "/api/oauth/claude/callback",
            params={"code": f"auth-code#{state}"},
        )

        assert response.status_code == 200
        assert len(oauth_env.registry.list_infos()) == 1

    def test_state_mismatch_is_400_html_without_tokens(self, oauth_env):
        client = oauth_env.client
        claude = client.post("/api/oauth/claude_personal/login")
        chatgpt = client.post("/api/oauth/chatgpt_personal/login")

        response = client.get(
            "/api/oauth/claude/callback",
            params={"code": "auth-code", "state": _state(chatgpt)},
        )

        assert response.status_code == 400
        assert "text/html" in response.headers["content-type"]
        assert "Invalid or expired sign-in state." in response.text
        assert "sk-ant-oat01-t" not in response.text
        assert client.get("/api/accounts").json() == []
        assert client.get(_attempt(claude)).json() == {"status": "pending"}
        assert client.get(_attempt(chatgpt)).json() == {"status": "pending"}

    def test_missing_code_is_400_html(self, oauth_env):
        state = _login_claude(oauth_env)

        response = oauth_env.client.get(
            "/api/oauth/claude/callback", params={"state": state}
        )

        assert response.status_code == 400
        assert "text/html" in response.headers["content-type"]
        assert len(oauth_env.registry.list_infos()) == 0

    def test_expired_state_does_not_consume_another_attempt(self, oauth_env, monkeypatch):
        from ai_usage_monitor import api

        now = 100
        monkeypatch.setattr(api, "monotonic", lambda: now)
        client = oauth_env.client
        path = "/api/oauth/claude_personal/login"
        first = client.post(path)
        now += 1
        second = client.post(path)
        now += api.CHATGPT_LISTENER_TIMEOUT_SECONDS - 1
        response = client.get("/api/oauth/claude/callback", params={"code": "auth-code", "state": _state(first)})
        assert (response.status_code, "Invalid or expired sign-in state." in response.text) == (400, True)
        expired = client.get(_attempt(first))
        assert expired.json() == {"status": "expired"}
        assert expired.headers["X-OAuth-Error"] == "Sign-in expired. Please try signing in again."
        assert client.get(_attempt(second)).json() == {"status": "pending"}
        assert client.get("/api/accounts").json() == []
        assert oauth_env.claude.exchange_calls == []
        assert client.get("/api/oauth/claude/callback", params={"code": "auth-code", "state": _state(second)}).status_code == 200

    def test_exchange_failure_is_400_html_without_tokens(self, oauth_env):
        state = _login_claude(oauth_env)
        oauth_env.claude.fail_exchange = True

        response = oauth_env.client.get(
            "/api/oauth/claude/callback",
            params={"code": "auth-code", "state": state},
        )

        assert response.status_code == 400
        assert "text/html" in response.headers["content-type"]
        assert "sk-ant-oat01-t" not in response.text
        assert len(oauth_env.registry.list_infos()) == 0


@pytest.mark.parametrize("fail_exchange", [False, True])
async def test_chatgpt_listener_serves_both_attempts_and_rejects_unknown_state(
    oauth_env, monkeypatch, fail_exchange
):
    from ai_usage_monitor.app import create_app

    start_server = asyncio.start_server

    async def bind_ephemeral(*args, **kwargs):
        nonlocal port
        server = await start_server(*args, **kwargs)
        port = server.sockets[0].getsockname()[1]
        return server

    port = 0
    monkeypatch.setattr(asyncio, "start_server", bind_ephemeral)
    app = create_app(registry=oauth_env.registry, poller=oauth_env.app.state.poller,
                     start_background=False, oauth_callback_port=0,
                     oauth_clients={"chatgpt_personal": oauth_env.chatgpt})

    async def callback(state):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(f"GET /auth/callback?code=auth-code&state={state} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            return await asyncio.wait_for(reader.read(), 2)
        finally:
            writer.close()
            await writer.wait_closed()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=DASHBOARD_HEADERS) as client:
        path = "/api/oauth/chatgpt_personal/login"
        first, second = await client.post(path), await client.post(path)
        assert first.status_code == second.status_code == 200
        task = app.state.oauth_listener_task
        try:
            rejected = await callback("unknown-state")
            assert rejected.startswith(b"HTTP/1.1 400 Bad Request\r\n")
            assert b"Sign-in failed. Please try signing in again." in rejected
            assert (await client.get("/api/accounts")).json() == []
            assert oauth_env.chatgpt.exchange_calls == []
            assert not task.done()
            redirect = (b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:61524/\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            oauth_env.chatgpt.fail_exchange = fail_exchange
            assert await callback(_state(first)) == (rejected if fail_exchange else redirect)
            assert (await client.get(_attempt(first))).json() == {"status": "failed" if fail_exchange else "completed"}
            assert (await client.get(_attempt(second))).json() == {"status": "pending"}
            assert not task.done()
            assert len((await client.get("/api/accounts")).json()) == (0 if fail_exchange else 1)
            oauth_env.chatgpt.fail_exchange = False
            assert await callback(_state(second)) == redirect
            await asyncio.wait_for(asyncio.shield(task), 2)
            assert (await client.get(_attempt(second))).json() == {"status": "completed"}
            assert len((await client.get("/api/accounts")).json()) == (1 if fail_exchange else 2)
            assert oauth_env.chatgpt.exchange_calls == [
                ("auth-code", verifier, state) for verifier, state in oauth_env.chatgpt.authorize_calls
            ]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_chatgpt_listener_stops_when_last_pending_attempt_expires(oauth_env, monkeypatch):
    from ai_usage_monitor import api
    from ai_usage_monitor.app import create_app

    loop = asyncio.get_running_loop()
    real_time = loop.time
    elapsed = 0
    monkeypatch.setattr(loop, "time", lambda: real_time() + elapsed)
    monkeypatch.setattr(api, "monotonic", loop.time)
    app = create_app(registry=oauth_env.registry, poller=oauth_env.app.state.poller,
                     start_background=False, oauth_callback_port=0,
                     oauth_clients={"chatgpt_personal": oauth_env.chatgpt})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=DASHBOARD_HEADERS) as client:
        path = "/api/oauth/chatgpt_personal/login"
        login = await client.post(path)
        assert login.status_code == 200
        task = app.state.oauth_listener_task
        try:
            assert (await client.get(_attempt(login))).json() == {"status": "pending"}
            elapsed += api.CHATGPT_LISTENER_TIMEOUT_SECONDS + 1
            await asyncio.wait_for(asyncio.shield(task), 2)
            assert (await client.get(_attempt(login))).json() == {"status": "expired"}
            assert (await client.get("/api/accounts")).json() == []
            assert oauth_env.chatgpt.exchange_calls == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestChatGPTCompletion:
    """The temporary listener's completion callback mirrors the Claude
    callback: exchange, add account, poll once, redirect to the app origin."""

    async def test_completion_adds_account_and_redirects_to_origin(
        self, oauth_env
    ):
        oauth_env.client.post("/api/oauth/chatgpt_personal/login")
        state = oauth_env.chatgpt.authorize_calls[0][1]
        handler = oauth_env.listener_handlers[0][1]

        location = await handler("auth-code", state)

        assert location == "http://127.0.0.1:61524/"
        infos = oauth_env.registry.list_infos()
        assert [i.provider for i in infos] == [ProviderKind.chatgpt_personal]
        assert infos[0].label == "gpt@example.com"
        assert infos[0].source == "manual"
        config = oauth_env.registry.get_config(infos[0].id)
        assert config.credentials["account_id"] == "acct-123"
        assert config.credentials["access_token"] == "chatgpt-token"
        assert config.credentials["expires_at"] == "1893456000000"
        assert "oauth_provider" not in config.credentials
        assert config.origin == "oauth"
        usage = oauth_env.client.get("/api/usage").json()
        assert any(
            u["account_id"] == infos[0].id and u["status"] == "ok" for u in usage
        )

    async def test_completion_defaults_label_without_email(self, oauth_env):
        oauth_env.chatgpt.exchange_result = {
            k: v for k, v in CHATGPT_EXCHANGE.items() if k != "email"
        }
        oauth_env.client.post("/api/oauth/chatgpt_personal/login")
        state = oauth_env.chatgpt.authorize_calls[0][1]
        handler = oauth_env.listener_handlers[-1][1]

        await handler("auth-code", state)

        assert oauth_env.registry.list_infos()[0].label == "ChatGPT OAuth"

    async def test_completion_rejects_state_mismatch(self, oauth_env):
        from ai_usage_monitor.api import OAuthCallbackError

        oauth_env.client.post("/api/oauth/chatgpt_personal/login")
        handler = oauth_env.listener_handlers[0][1]

        with pytest.raises(OAuthCallbackError):
            await handler("auth-code", "wrong-state")
        assert len(oauth_env.registry.list_infos()) == 0

    async def test_completion_without_pending_session_rejected(self, oauth_env):
        from ai_usage_monitor.api import OAuthCallbackError

        handler = _login_chatgpt(oauth_env)
        state = oauth_env.chatgpt.authorize_calls[0][1]
        await handler("auth-code", state)

        with pytest.raises(OAuthCallbackError):
            await handler("auth-code", state)
        assert len(oauth_env.registry.list_infos()) == 1

    async def test_completion_exchange_failure_rejected(self, oauth_env):
        from ai_usage_monitor.api import OAuthCallbackError

        oauth_env.chatgpt.fail_exchange = True
        oauth_env.client.post("/api/oauth/chatgpt_personal/login")
        state = oauth_env.chatgpt.authorize_calls[0][1]
        handler = oauth_env.listener_handlers[0][1]

        with pytest.raises(OAuthCallbackError):
            await handler("auth-code", state)
        assert len(oauth_env.registry.list_infos()) == 0
