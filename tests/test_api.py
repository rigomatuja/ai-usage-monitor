"""Tests for the REST API contract (tests/test_static_assets.py pins the
frontend side of the same contract)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
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
def accounts_app(tmp_path):
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

    return create_app(registry=registry, poller=poller, start_background=False)


@pytest.fixture
def client(accounts_app):
    with TestClient(accounts_app) as test_client:
        yield test_client


@pytest.fixture
def non_raising_client(accounts_app):
    with TestClient(accounts_app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestAccountsEndpoints:
    @pytest.mark.parametrize("failure, warning_class", [
        ("accounts-replace", None),
        ("labels-read", "PermissionError"),
        ("labels-parse", "JSONDecodeError"),
        ("labels-shape", "ValueError"),
        ("labels-replace", "OSError"),
    ])
    def test_delete_preserves_both_files_on_store_failure(
        self, non_raising_client, tmp_path, monkeypatch, caplog, failure, warning_class,
    ):
        client = non_raising_client
        added = client.post(
            "/api/accounts",
            json={"provider": "openai_org", "credentials": {"api_key": "synthetic"}},
        )
        assert added.status_code == 201
        account_id = added.json()["id"]
        path = f"/api/accounts/{account_id}"
        accounts = tmp_path / "config" / "accounts.json"
        labels = accounts.with_name("labels.json")
        if failure == "labels-parse":
            labels_before = b'{"incomplete":'
        elif failure == "labels-shape":
            labels_before = b'[]'
        else:
            labels_before = ('{\r\n  "%s": "Café"\r\n}\r\n' % account_id).encode("utf-8")
        labels.write_bytes(labels_before)
        accounts_before = accounts.read_bytes()
        listing_before = client.get("/api/accounts").json()
        usage_before = client.get("/api/usage").json()

        read_text, replace = Path.read_text, os.replace
        store_events = []

        def checked_read(file, *args, **kwargs):
            if file == labels:
                store_events.append("labels-read")
                if failure == "labels-read":
                    raise PermissionError("label store unreadable")
            return read_text(file, *args, **kwargs)

        def checked_replace(source, target):
            if target in (accounts, labels):
                event = f"{target.stem}-replace"
                store_events.append(event)
                if event == failure:
                    raise OSError("replacement failed")
            return replace(source, target)

        with monkeypatch.context() as faults:
            faults.setattr(Path, "read_text", checked_read)
            faults.setattr(os, "replace", checked_replace)
            result = client.delete(path)

        assert labels.read_bytes() == labels_before
        warnings = [record for record in caplog.records if record.name == "ai_usage_monitor.registry"]
        listing = client.get("/api/accounts")
        if failure == "accounts-replace":
            assert (result.status_code, result.text) == (500, "Internal Server Error")
            assert store_events == ["accounts-replace"]
            assert accounts.read_bytes() == accounts_before
            assert (listing.status_code, listing.json()) == (200, listing_before)
            assert client.get("/api/usage").json() == usage_before
            assert warnings == []
            return

        assert (result.status_code, result.content) == (204, b"")
        assert store_events == ["accounts-replace", "labels-read"] + (
            ["labels-replace"] if failure == "labels-replace" else []
        )
        assert json.loads(accounts.read_bytes()) == []
        other = listing_before[0]
        assert (listing.status_code, listing.json()) == (200, [other])
        assert client.get("/api/usage").json() == []
        assert [(r.levelno, r.getMessage(), r.exc_info) for r in warnings] == [
            (logging.WARNING, f"Could not clean up account labels ({warning_class})", None),
        ]
        if failure == "labels-read":
            renamed = client.patch(
                f"/api/accounts/{other['id']}", json={"label": "Other"}, headers=DASHBOARD_HEADERS,
            )
            assert (renamed.status_code, renamed.json()) == (
                200, {**other, "label": "Other", "custom_label": True},
            )
            assert json.loads(labels.read_bytes()) == {other["id"]: "Other"}

    def test_list_accounts_ignores_orphan_labels(self, client, tmp_path):
        before = client.get("/api/accounts").json()
        config = tmp_path / "config"
        config.mkdir()
        labels = config / "labels.json"
        orphaned = b'{"openai_org-deadbeef":"Old org","auto-claude-personal":"Old login"}'
        labels.write_bytes(orphaned)

        listing = client.get("/api/accounts")

        assert (listing.status_code, listing.json()) == (200, before)
        assert labels.read_bytes() == orphaned

    def test_list_accounts_includes_auto(self, client):
        response = client.get("/api/accounts")
        assert response.status_code == 200
        accounts = response.json()
        assert [a["id"] for a in accounts] == ["auto-chatgpt-personal"]
        assert accounts[0]["source"] == "auto"
        assert accounts[0]["platform"] == "openai"
        assert accounts[0]["provider"] == "chatgpt_personal"

    @pytest.mark.parametrize("label", ["Work org", "é" * 80])
    def test_add_manual_account(self, client, label):
        response = client.post(
            "/api/accounts",
            json={
                "provider": "anthropic_org",
                "label": f" {label} ",
                "credentials": {"api_key": "sk-ant-admin-x"},
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["source"] == "manual"
        assert body["platform"] == "claude"
        assert body["provider"] == "anthropic_org"
        assert body["label"] == body["default_label"] == label
        assert body["custom_label"] is False
        # The new account is listed and was polled immediately.
        assert body in client.get("/api/accounts").json()
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

    @pytest.mark.parametrize("fields", [{}, {"label": ""}, {"label": "   "}])
    def test_add_account_missing_or_blank_label_uses_default(self, client, fields):
        response = client.post("/api/accounts", json={"provider": "openai_org", **fields})
        assert response.status_code == 201
        body = response.json()
        assert body["label"] == body["default_label"] == "openai_org"
        assert body["custom_label"] is False
        assert body in client.get("/api/accounts").json()

    @pytest.mark.parametrize("raw_label, detail", [
        (br'"x\udfff"', "label must be valid UTF-8"),
        (br'"x\u0000"', "label must not contain control characters"),
        (br'" \t "', "label must not contain control characters"),
        (br'"x\u0085"', "label must not contain control characters"),
        (b'" ' + b'x' * 81 + b' "', "label must be at most 80 characters"),
    ])
    def test_add_account_rejects_invalid_label_without_writing(
        self, non_raising_client, tmp_path, raw_label, detail,
    ):
        client = non_raising_client
        before = client.get("/api/accounts").json()
        usage = client.get("/api/usage").json()
        secret = b"synthetic-credential"
        response = client.post(
            "/api/accounts", headers={"Content-Type": "application/json"},
            content=(b'{"provider":"openai_org","credentials":{"api_key":"'
                     + secret + b'"},"label":' + raw_label + b'}'),
        )

        assert secret not in response.content
        assert response.status_code == 422
        assert response.json() == {"detail": detail}
        config = tmp_path / "config"
        assert not (config / "accounts.json").exists()
        assert not (config / "labels.json").exists()
        listing = client.get("/api/accounts")
        assert (listing.status_code, listing.json()) == (200, before)
        assert client.get("/api/usage").json() == usage

    @pytest.mark.parametrize("clear", [None, "", "   "])
    def test_rename_auto_and_clear_to_default(self, client, clear):
        before, = client.get("/api/accounts").json()
        assert before == {
            "id": "auto-chatgpt-personal",
            "label": "ChatGPT (local login)",
            "default_label": "ChatGPT (local login)",
            "custom_label": False,
            "provider": "chatgpt_personal",
            "platform": "openai",
            "source": "auto",
        }
        path = f"/api/accounts/{before['id']}"
        renamed = {**before, "label": "Personal", "custom_label": True}
        response = client.patch(path, json={"label": " Personal "}, headers=DASHBOARD_HEADERS)
        assert (response.status_code, response.json()) == (200, renamed)
        assert client.get("/api/accounts").json() == [renamed]

        response = client.patch(path, json={"label": clear}, headers=DASHBOARD_HEADERS)
        assert (response.status_code, response.json()) == (200, before)
        assert client.get("/api/accounts").json() == [before]
        assert client.get("/api/usage").json() == []

    def test_rename_manual_accepts_80_characters_after_trimming(self, client):
        original = client.post("/api/accounts", json={"provider": "openai_org"}).json()
        before = client.get("/api/accounts").json()
        usage_before = client.get("/api/usage").json()
        name = "x" * 80
        expected = {**original, "label": name, "custom_label": True}
        response = client.patch(
            f"/api/accounts/{original['id']}", json={"label": f" {name} "},
            headers={"Origin": "http://localhost:61524"},
        )
        assert (response.status_code, response.json()) == (200, expected)
        assert client.get("/api/accounts").json() == [before[0], expected]
        assert client.get("/api/usage").json() == usage_before

    @pytest.mark.parametrize("account_id, label, headers, status, detail", [
        ("auto-chatgpt-personal", "x" * 81, {"Origin": "http://127.0.0.1:61524"},
         422, "label must be at most 80 characters"),
        ("unknown", "Work", {"Origin": "http://127.0.0.1:61524"}, 404, "unknown account"),
        ("auto-chatgpt-personal", "Work", {}, 403, "Cross-origin account rename denied"),
        ("auto-chatgpt-personal", "Work", {"Origin": "http://foreign.invalid"},
         403, "Cross-origin account rename denied"),
        ("auto-chatgpt-personal", "Work", {"Origin": "http://localhost:61524", "Sec-Fetch-Site": "cross-site"},
         403, "Cross-origin account rename denied"),
    ])
    def test_rejected_rename_leaves_accounts_and_usage_unchanged(
        self, client, account_id, label, headers, status, detail,
    ):
        accounts = client.get("/api/accounts").json()
        usage = client.get("/api/usage").json()
        response = client.patch(f"/api/accounts/{account_id}", json={"label": label}, headers=headers)
        assert (response.status_code, response.json()) == (status, {"detail": detail})
        assert client.get("/api/accounts").json() == accounts
        assert client.get("/api/usage").json() == usage

    @pytest.mark.parametrize("payload, detail", [
        (br'{"label":"\ud800"}', "label must be valid UTF-8"),
        (br'{"label":"A\u0000B"}', "label must not contain control characters"),
        (br'{"label":" \t "}', "label must not contain control characters"),
        (br'{"label":"A\u007fB"}', "label must not contain control characters"),
        (br'{"label":"A\u0085B"}', "label must not contain control characters"),
    ])
    def test_rename_rejects_invalid_characters_without_writing(
        self, non_raising_client, tmp_path, payload, detail,
    ):
        client = non_raising_client
        path = "/api/accounts/auto-chatgpt-personal"
        rename = client.patch(path, json={"label": "Personal"}, headers=DASHBOARD_HEADERS)
        assert rename.status_code == 200
        labels_path = tmp_path / "config" / "labels.json"
        before = labels_path.read_bytes()
        usage = client.get("/api/usage").json()

        response = client.patch(
            path, content=payload, headers={**DASHBOARD_HEADERS, "Content-Type": "application/json"},
        )

        assert response.status_code == 422
        assert response.json() == {"detail": detail}
        assert labels_path.read_bytes() == before
        listing = client.get("/api/accounts")
        assert (listing.status_code, listing.json()) == (200, [rename.json()])
        assert client.get("/api/usage").json() == usage

    @pytest.mark.parametrize("invalid_label", [23, "\ud800", "A\u0085B", " ", "x" * 81])
    def test_list_accounts_ignores_invalid_stored_label(self, non_raising_client, tmp_path, invalid_label):
        client = non_raising_client
        default, = client.get("/api/accounts").json()
        manual = client.post("/api/accounts", json={"provider": "openai_org"}).json()
        labels = {default["id"]: invalid_label, manual["id"]: "Valid name"}
        labels_path = tmp_path / "config" / "labels.json"
        before = json.dumps(labels).encode("utf-8")
        labels_path.write_bytes(before)

        listing = client.get("/api/accounts")

        assert listing.status_code == 200
        assert listing.json() == [default, {**manual, "label": "Valid name", "custom_label": True}]
        assert labels_path.read_bytes() == before

    def test_delete_manual_account_returns_204(self, client, tmp_path):
        body = client.post(
            "/api/accounts",
            json={"provider": "openai_org", "label": "Org", "credentials": {}},
        ).json()
        path = f"/api/accounts/{body['id']}"
        rename = client.patch(path, json={"label": "Renamed"}, headers=DASHBOARD_HEADERS)
        assert rename.status_code == 200
        response = client.delete(path)
        assert (response.status_code, response.content) == (204, b"")
        assert all(a["id"] != body["id"] for a in client.get("/api/accounts").json())
        config = tmp_path / "config"
        assert json.loads((config / "accounts.json").read_bytes()) == []
        assert json.loads((config / "labels.json").read_bytes()) == {}

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
    @pytest.mark.parametrize("callback", ["/callback", "/api/oauth/claude/callback"])
    def test_happy_path_creates_manual_account_and_polls(self, oauth_env, callback, caplog):
        client = oauth_env.client
        assert client.get("/api/oauth/attempts/unknown").status_code == 404
        login = client.post("/api/oauth/claude_personal/login")
        attempt = f"/api/oauth/attempts/{login.json()['attempt_id']}"
        assert client.get(attempt).json() == {"status": "pending"}
        state = oauth_env.claude.authorize_calls[0][1]

        with caplog.at_level(logging.INFO, logger="ai_usage_monitor.api"):
            response = oauth_env.client.get(
                callback, params={"code": "auth-code", "state": state, "extra": "query-marker"},
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
        assert _api_info_messages(caplog) == ["Claude OAuth callback received"]
        # The new account was polled immediately.
        usage = oauth_env.client.get("/api/usage").json()
        assert any(
            u["account_id"] == infos[0].id and u["status"] == "ok" for u in usage
        )

    @pytest.mark.parametrize("include_query_state", [False, True])
    def test_code_state_twist_is_accepted(self, oauth_env, include_query_state):
        state = _login_claude(oauth_env)
        params = {"code": f"auth-code#{state}"}
        if include_query_state:
            params["state"] = state

        response = oauth_env.client.get("/api/oauth/claude/callback", params=params)

        assert response.status_code == 200
        assert len(oauth_env.registry.list_infos()) == 1

    @pytest.mark.parametrize("embedded_case", ["other_attempt", "non_ascii", "empty"])
    def test_conflicting_code_and_query_states_leave_both_attempts_pending(self, oauth_env, monkeypatch, embedded_case):
        from ai_usage_monitor.oauth import ClaudeOAuthClient

        exchange = oauth_env.claude.exchange_code

        async def validate_exchange(http, code, verifier, state):
            await exchange(http, code, verifier, state)
            ClaudeOAuthClient._split_code(code, state)
            return oauth_env.claude.exchange_result

        monkeypatch.setattr(oauth_env.claude, "exchange_code", validate_exchange)
        client = oauth_env.client
        login_path = "/api/oauth/claude_personal/login"
        callback = "/api/oauth/claude/callback"
        first, second = client.post(login_path), client.post(login_path)
        assert first.status_code == second.status_code == 200
        embedded = {"other_attempt": _state(second), "non_ascii": "ñ", "empty": ""}[embedded_case]

        response = client.get(callback, params={"code": f"auth-code#{embedded}", "state": _state(first)})

        assert response.status_code == 400
        assert client.get(_attempt(first)).json() == {"status": "pending"}
        assert client.get(_attempt(second)).json() == {"status": "pending"}
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert "Invalid or expired sign-in state." in response.text
        assert oauth_env.claude.exchange_calls == []
        assert client.get("/api/accounts").json() == []
        assert client.get(callback, params={"code": "auth-code", "state": _state(first)}).status_code == 200
        assert client.get(_attempt(first)).json() == {"status": "completed"}
        assert client.get(_attempt(second)).json() == {"status": "pending"}
        assert len(client.get("/api/accounts").json()) == 1

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

    @pytest.mark.parametrize("fail_at", ["store", "poll"])
    async def test_storage_failure_is_terminal_but_poll_failure_is_nonfatal(self, oauth_env, monkeypatch, fail_at):
        def fail(*args, **kwargs):
            raise OSError("disk failure: sk-ant-oat01-t")

        async def fail_poll(*args):
            fail()

        if fail_at == "store":
            monkeypatch.setattr(oauth_env.registry, "add_account", fail)
        else:
            monkeypatch.setattr(oauth_env.app.state.poller, "poll_account", fail_poll)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oauth_env.app, raise_app_exceptions=False),
                                     base_url="http://test", headers=DASHBOARD_HEADERS) as client:
            login = await client.post("/api/oauth/claude_personal/login")
            response = await client.get("/api/oauth/claude/callback", params={"code": "auth-code", "state": _state(login)})
            assert len(oauth_env.claude.exchange_calls) == 1
            attempt = (await client.get(_attempt(login))).json()
            accounts = (await client.get("/api/accounts")).json()
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        if fail_at == "store":
            assert (response.status_code, attempt, accounts) == (400, {"status": "failed"}, [])
            assert "Sign-in failed. Please try signing in again." in response.text
            assert all(s not in response.text for s in ("sk-ant-oat01-t", "disk failure", "Traceback"))
        else:
            assert (response.status_code, attempt) == (200, {"status": "completed"})
            assert [a["label"] for a in accounts] == ["user@example.com"]

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


@pytest.mark.parametrize("fail_at", [None, "exchange", "store", "poll"])
async def test_chatgpt_listener_serves_both_attempts_and_rejects_unknown_state(
    oauth_env, monkeypatch, fail_at
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
            def fail(*args, **kwargs):
                raise OSError("disk failure: chatgpt-token")

            async def fail_poll(*args):
                fail()

            failed = fail_at in ("exchange", "store")
            with monkeypatch.context() as patch:
                patch.setattr(oauth_env.chatgpt, "fail_exchange", fail_at == "exchange")
                if fail_at == "store":
                    patch.setattr(oauth_env.registry, "add_account", fail)
                elif fail_at == "poll":
                    patch.setattr(app.state.poller, "poll_account", fail_poll)
                assert await callback(_state(first)) == (rejected if failed else redirect)
            assert b"Content-Type: text/html" in rejected and b"chatgpt-token" not in rejected
            assert (await client.get(_attempt(first))).json() == {"status": "failed" if failed else "completed"}
            assert (await client.get(_attempt(second))).json() == {"status": "pending"}
            assert not task.done()
            assert len((await client.get("/api/accounts")).json()) == (0 if failed else 1)
            assert await callback(_state(second)) == redirect
            await asyncio.wait_for(asyncio.shield(task), 2)
            assert (await client.get(_attempt(second))).json() == {"status": "completed"}
            assert len((await client.get("/api/accounts")).json()) == (1 if failed else 2)
            assert oauth_env.chatgpt.exchange_calls == [
                ("auth-code", verifier, state) for verifier, state in oauth_env.chatgpt.authorize_calls
            ]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_chatgpt_listener_signals_stop_before_post_login_poll_finishes(oauth_env, monkeypatch):
    from ai_usage_monitor.app import create_app

    start_server = asyncio.start_server
    port = 0
    listening_sockets = []
    socket_fds_at_poll_entry = []

    async def bind_ephemeral(*args, **kwargs):
        nonlocal port
        server = await start_server(*args, **kwargs)
        listening_sockets.extend(server.sockets)
        port = server.sockets[0].getsockname()[1]
        return server

    poller = oauth_env.app.state.poller
    original_poll = poller.poll_account
    polling, release = asyncio.Event(), asyncio.Event()

    async def slow_poll(*args):
        socket_fds_at_poll_entry.extend(sock.fileno() for sock in listening_sockets)
        polling.set()
        await release.wait()
        return await original_poll(*args)

    monkeypatch.setattr(asyncio, "start_server", bind_ephemeral)
    monkeypatch.setattr(poller, "poll_account", slow_poll)
    app = create_app(registry=oauth_env.registry, poller=poller, start_background=False,
                     oauth_callback_port=0, oauth_clients={"chatgpt_personal": oauth_env.chatgpt})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=DASHBOARD_HEADERS) as client:
        login = await client.post("/api/oauth/chatgpt_personal/login")
        assert login.status_code == 200
        task = app.state.oauth_listener_task
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(f"GET /auth/callback?code=auth-code&state={_state(login)} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            try:
                await asyncio.wait_for(polling.wait(), 2)
                # The listening socket is already closed when the poll starts.
                assert socket_fds_at_poll_entry == [-1]
                # A new login must start a fresh listener, not reuse this one.
                assert app.state.oauth_listener_deadline.closed
                assert (await client.get(_attempt(login))).json() == {"status": "completed"}
                assert (await client.get("/api/usage")).json() == []
                assert app.state.oauth_listener_deadline.changed.is_set()
            finally:
                release.set()
                response = await asyncio.wait_for(reader.read(), 2)
            assert response == (b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:61524/\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await asyncio.wait_for(asyncio.shield(task), 2)
            assert [snapshot["status"] for snapshot in (await client.get("/api/usage")).json()] == ["ok"]
        finally:
            writer.close()
            await writer.wait_closed()
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


def _api_info_messages(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name == "ai_usage_monitor.api" and record.levelno == logging.INFO]


class TestChatGPTCompletion:
    """The temporary listener's completion callback mirrors the Claude
    callback: exchange, add account, poll once, redirect to the app origin."""

    async def test_completion_adds_account_and_redirects_to_origin(self, oauth_env, caplog):
        oauth_env.client.post("/api/oauth/chatgpt_personal/login")
        state = oauth_env.chatgpt.authorize_calls[0][1]
        handler = oauth_env.listener_handlers[0][1]

        with caplog.at_level(logging.INFO, logger="ai_usage_monitor.api"):
            location = await handler("auth-code", state)

        assert location == "http://127.0.0.1:61524/"
        assert _api_info_messages(caplog) == ["ChatGPT OAuth callback received"]
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

    @pytest.mark.parametrize("email", [None, "a" * 64 + "@example-domain.com"])
    async def test_completion_defaults_label_without_usable_email(self, oauth_env, email):
        oauth_env.chatgpt.exchange_result = {
            k: v for k, v in CHATGPT_EXCHANGE.items() if k != "email"
        }
        if email is not None:
            oauth_env.chatgpt.exchange_result["email"] = email
        login = oauth_env.client.post("/api/oauth/chatgpt_personal/login")
        assert login.status_code == 200
        state = _state(login)
        handler = oauth_env.listener_handlers[-1][1]

        assert await handler("auth-code", state) == "http://127.0.0.1:61524/"

        assert oauth_env.client.get(_attempt(login)).json() == {"status": "completed"}
        listing = oauth_env.client.get("/api/accounts")
        assert listing.status_code == 200
        info, = listing.json()
        assert info["label"] == info["default_label"] == "ChatGPT OAuth"
        assert info["custom_label"] is False

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


def test_dashboard_assets_referenced_by_index_are_served(client):
    """Every stylesheet/script the dashboard HTML references must resolve,
    from the page URL "/", to a served asset (regression: relative paths
    resolved to /styles.css and /app.js, which returned 404)."""
    import re
    from urllib.parse import urljoin, urlparse

    page = client.get("/")
    assert page.status_code == 200
    refs = re.findall(r'<(?:link[^>]*href|script[^>]*src)="([^"]+)"', page.text)
    assert len(refs) >= 2  # stylesheet + script
    for ref in refs:
        path = urlparse(urljoin("http://testserver/", ref)).path
        response = client.get(path)
        assert response.status_code == 200, f"{ref} resolves to {path}: {response.status_code}"


def test_recreated_account_does_not_inherit_orphaned_label(
    client, tmp_path, monkeypatch,
):
    import uuid

    create = {"provider": "openai_org", "label": "New account"}
    old = client.post("/api/accounts", json=create)
    assert old.status_code == 201
    old_id = old.json()["id"]
    headers = {"Origin": "http://localhost:61524"}
    assert client.patch(
        f"/api/accounts/{old_id}", headers=headers, json={"label": "Café privé"},
    ).status_code == 200

    labels = tmp_path / "config" / "labels.json"
    replace = os.replace

    def deny_label_replace(source, target):
        if target == labels:
            raise PermissionError("labels replacement denied")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", deny_label_replace)
    assert client.delete(f"/api/accounts/{old_id}").status_code == 204
    assert "Café privé" not in client.get("/api/accounts").text

    colliding_uuid = uuid.UUID(old_id.rsplit("-", 1)[1] + "0" * 24)
    uuid4 = uuid.uuid4
    candidates = iter([colliding_uuid])
    monkeypatch.setattr(uuid, "uuid4", lambda: next(candidates, None) or uuid4())

    new = client.post("/api/accounts", json=create)
    assert new.status_code == 201
    info = new.json()
    listing = client.get("/api/accounts").json()
    persisted = next(row for row in listing if row["id"] == info["id"])
    for row in (info, persisted):
        assert row["label"] == row["default_label"] == "New account"
        assert row["custom_label"] is False
