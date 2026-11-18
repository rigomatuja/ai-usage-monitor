"""Tests for the REST API contract (tests/test_static_assets.py pins the
frontend side of the same contract)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_usage_monitor.models import ProviderKind, UsageWindow, WindowKind
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
