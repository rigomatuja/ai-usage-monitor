"""Tests for the usage poller (last-good snapshot semantics)."""

from __future__ import annotations

import logging

import httpx
import pytest

from ai_usage_monitor.models import ProviderKind, UsageWindow, WindowKind
from ai_usage_monitor.oauth import OAuthError
from ai_usage_monitor.poller import UsagePoller
from ai_usage_monitor.providers.base import ProviderError
from ai_usage_monitor.registry import AccountRegistry


class FakeProvider:
    """Scriptable provider used only by these tests."""

    def __init__(self, credentials):  # same signature as real providers
        self.credentials = dict(credentials)

    behavior = {"windows": [], "error": None}

    async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]:
        if FakeProvider.behavior["error"]:
            raise ProviderError(FakeProvider.behavior["error"])
        return [w.model_copy(deep=True) for w in FakeProvider.behavior["windows"]]


@pytest.fixture
def fake_windows():
    return [
        UsageWindow(kind=WindowKind.five_hour, label="5-hour session",
                    used_percent=42.0, detail=None, resets_at=None),
        UsageWindow(kind=WindowKind.weekly, label="Weekly",
                    used_percent=80.0, detail=None, resets_at=None),
    ]


@pytest.fixture
def setup(tmp_path, fake_windows):
    FakeProvider.behavior = {"windows": fake_windows, "error": None}
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    registry = AccountRegistry(
        config_dir=tmp_path / "config",
        claude_credentials_path=home / ".claude" / ".credentials.json",
        codex_auth_path=home / ".codex" / "auth.json",
    )
    poller = UsagePoller(
        registry, provider_factory=lambda config: FakeProvider(config.credentials)
    )
    return registry, poller


@pytest.fixture
async def http():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={}))) as client:
        yield client


class TestPoller:
    async def test_ok_snapshot_stored(self, setup, http, fake_windows):
        registry, poller = setup
        snapshot = await poller.poll_account("auto-claude-personal", http)
        assert snapshot.status == "ok"
        assert snapshot.windows == fake_windows
        assert poller.get_snapshot("auto-claude-personal") is snapshot

    async def test_poll_all_covers_every_account(self, setup, http):
        registry, poller = setup
        assert poller.snapshots() == []
        await poller.poll_all(http)
        assert len(poller.snapshots()) == len(registry.iter_configs())

    async def test_error_keeps_last_good_windows(self, setup, http, fake_windows):
        _, poller = setup
        await poller.poll_account("auto-claude-personal", http)

        FakeProvider.behavior = {"windows": [], "error": "429 rate limited"}
        snapshot = await poller.poll_account("auto-claude-personal", http)

        assert snapshot.status == "error"
        assert snapshot.error == "429 rate limited"
        # Last good windows survive inside the error snapshot.
        assert snapshot.windows == fake_windows

    async def test_unexpected_provider_exception_is_contained(self, setup, http):
        _, poller = setup

        class Exploding(FakeProvider):
            async def fetch(self, http):
                raise RuntimeError("provider bug")

        poller._provider_factory = lambda config: Exploding(config.credentials)
        snapshot = await poller.poll_account("auto-claude-personal", http)
        assert snapshot.status == "error"
        assert "unexpected error" in snapshot.error

    async def test_unknown_account_raises_keyerror(self, setup, http):
        _, poller = setup
        with pytest.raises(KeyError):
            await poller.poll_account("nope", http)

    async def test_recovery_after_error(self, setup, http, fake_windows):
        _, poller = setup
        FakeProvider.behavior = {"windows": [], "error": "down"}
        await poller.poll_account("auto-claude-personal", http)

        FakeProvider.behavior = {"windows": fake_windows, "error": None}
        snapshot = await poller.poll_account("auto-claude-personal", http)
        assert snapshot.status == "ok"
        assert snapshot.windows == fake_windows

    async def test_new_account_first_snapshot_error_empty_windows(
        self, setup, http
    ):
        registry, poller = setup
        FakeProvider.behavior = {"windows": [], "error": "401 unauthorized"}
        registry.add_account(ProviderKind.anthropic_org, "Work", {"api_key": "k"})
        snapshot = await poller.poll_all(http) or poller.snapshots()
        assert len(poller.snapshots()) == 2
        for snap in poller.snapshots():
            if snap.status == "error":
                assert snap.windows == []


class TestPrefetchRefresh:
    """Pre-fetch refresh: only accounts whose stored credentials contain a
    refresh_token (our OAuth flow) are ever refreshed and persisted; the
    CLI-owned auto accounts with empty credential dicts are never touched."""

    OLD_CREDENTIALS = {
        "access_token": "old-at",
        "refresh_token": "old-rt",
        "expires_at": "1",
    }
    ROTATED = {
        "access_token": "new-at",
        "refresh_token": "new-rt",
        "expires_at": 1893456000000,
    }

    def _add_oauth_account(self, registry):
        return registry.add_account(
            ProviderKind.claude_personal, "OAuth Claude", dict(self.OLD_CREDENTIALS)
        )

    def _no_refresh(self, http, provider, credentials):
        raise AssertionError("refresh must not be called")

    async def test_refresh_due_rotates_persists_and_fetch_sees_new_token(
        self, setup, http
    ):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        refresh_calls = []

        async def refresh(http_arg, provider, credentials):
            refresh_calls.append((provider, dict(credentials)))
            return dict(self.ROTATED)

        seen = []

        def factory(config):
            seen.append(dict(config.credentials))
            return FakeProvider(config.credentials)

        poller = UsagePoller(
            registry,
            provider_factory=factory,
            refresh_fn=refresh,
            should_refresh=lambda credentials: True,
        )

        snapshot = await poller.poll_account(account.id, http)

        assert snapshot.status == "ok"
        assert refresh_calls == [
            (ProviderKind.claude_personal, dict(self.OLD_CREDENTIALS))
        ]
        # The provider fetch saw the rotated access token.
        assert seen[-1]["access_token"] == "new-at"
        # The rotated credentials (epoch-ms coerced to str) were persisted.
        assert registry.get_config(account.id).credentials == {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "expires_at": "1893456000000",
        }

    async def test_no_refresh_token_in_credentials_never_refreshed(
        self, setup, http
    ):
        registry, _ = setup  # auto-claude-personal has an empty credentials dict
        poller = UsagePoller(
            registry,
            provider_factory=lambda config: FakeProvider(config.credentials),
            refresh_fn=self._no_refresh,
            should_refresh=lambda credentials: True,
        )

        snapshot = await poller.poll_account("auto-claude-personal", http)

        assert snapshot.status == "ok"

    async def test_no_refresh_when_token_not_due(self, setup, http):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        poller = UsagePoller(
            registry,
            provider_factory=lambda config: FakeProvider(config.credentials),
            refresh_fn=self._no_refresh,
            should_refresh=lambda credentials: False,
        )

        snapshot = await poller.poll_account(account.id, http)

        assert snapshot.status == "ok"
        assert registry.get_config(account.id).credentials == self.OLD_CREDENTIALS

    async def test_refresh_failure_proceeds_with_existing_token(
        self, setup, http, fake_windows, caplog
    ):
        registry, _ = setup
        account = self._add_oauth_account(registry)

        async def refresh(http_arg, provider, credentials):
            raise OAuthError("refresh rejected by provider")

        seen = []

        def factory(config):
            seen.append(dict(config.credentials))
            return FakeProvider(config.credentials)

        poller = UsagePoller(
            registry,
            provider_factory=factory,
            refresh_fn=refresh,
            should_refresh=lambda credentials: True,
        )

        with caplog.at_level(logging.WARNING):
            snapshot = await poller.poll_account(account.id, http)

        # Fetch still happened, with the existing access token.
        assert snapshot.status == "ok"
        assert snapshot.windows == fake_windows
        assert seen[-1]["access_token"] == "old-at"
        # Stored credentials unchanged.
        assert registry.get_config(account.id).credentials == self.OLD_CREDENTIALS
        # Failure was logged at warning level.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("refresh" in r.getMessage().lower() for r in warnings)
