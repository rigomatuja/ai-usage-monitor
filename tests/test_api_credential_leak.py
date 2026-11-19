"""Security probes: credentials must never leak in API error responses,
and the accounts store must never be created group/world-readable."""

from __future__ import annotations

import os
import stat

from fastapi.testclient import TestClient

from ai_usage_monitor.app import create_app
from ai_usage_monitor.poller import UsagePoller
from ai_usage_monitor.registry import AccountRegistry


def _client(tmp_path):
    registry = AccountRegistry(
        config_dir=tmp_path / "config",
        claude_credentials_path=tmp_path / "no-a",
        codex_auth_path=tmp_path / "no-b",
    )
    app = create_app(
        registry=registry,
        poller=UsagePoller(registry, provider_factory=lambda config: None),
        start_background=False,
    )
    return TestClient(app)


def test_422_does_not_echo_credential_values(tmp_path):
    with _client(tmp_path) as client:
        response = client.post(
            "/api/accounts",
            json={
                "provider": "not-a-provider",
                "credentials": {"api_key": "sk-SECRET-XYZ"},
            },
        )  # unknown provider -> 422 (labels are optional since v1.3)
        assert response.status_code == 422
        assert "sk-SECRET-XYZ" not in str(response.json())


def test_accounts_store_is_created_with_0600_from_the_start(
    tmp_path, monkeypatch
):
    """The file must be opened with mode 0o600 at creation, not chmod'd
    after a world-readable write."""
    seen_modes: list[int] = []
    real_open = os.open

    def spy_open(path, flags, mode=None, *args, **kwargs):
        if str(path).endswith("accounts.json"):
            seen_modes.append(mode)
        return real_open(path, flags, *args, **kwargs) if mode is None else real_open(
            path, flags, mode, *args, **kwargs
        )

    monkeypatch.setattr("ai_usage_monitor.registry.os.open", spy_open)

    from ai_usage_monitor.models import ProviderKind

    registry = AccountRegistry(config_dir=tmp_path / "config")
    registry.add_account(ProviderKind.openai_org, "Org", {"api_key": "k"})

    assert seen_modes, "accounts.json was not created via os.open"
    assert all(mode is not None and mode == 0o600 for mode in seen_modes), (
        f"accounts.json opened with insecure mode(s): {seen_modes}"
    )
    mode = stat.S_IMODE(os.stat(tmp_path / "config" / "accounts.json").st_mode)
    assert mode == 0o600
