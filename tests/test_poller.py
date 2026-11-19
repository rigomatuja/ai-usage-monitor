"""Tests for the usage poller (last-good snapshot semantics)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import DEFAULT, AsyncMock, Mock

import httpx
import pytest

from ai_usage_monitor.app import create_app
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
    """Only OAuth-origin personal accounts with refresh tokens can refresh."""

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
            ProviderKind.claude_personal, "OAuth Claude", dict(self.OLD_CREDENTIALS),
            origin="oauth",
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

    @pytest.mark.parametrize("persist_fails", [False, True])
    async def test_overlapping_polls_refresh_once(self, setup, http, monkeypatch, persist_fails):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        if persist_fails:
            monkeypatch.setattr(registry, "update_credentials", Mock(side_effect=OSError("disk")))

        async def rotate(*args):
            await asyncio.sleep(0.01)
            return self.ROTATED

        refresh = AsyncMock(side_effect=rotate)
        poller = UsagePoller(registry, lambda c: FakeProvider(c.credentials), refresh_fn=refresh,
                             should_refresh=lambda c: c["access_token"] == "old-at")
        snapshots = await asyncio.gather(*(poller.poll_account(account.id, http) for _ in range(2)))
        refresh.assert_awaited_once_with(http, ProviderKind.claude_personal, self.OLD_CREDENTIALS)
        assert [snapshot.status for snapshot in snapshots] == ["ok", "ok"]

    @pytest.mark.parametrize("persist_fails", [False, True])
    async def test_flush_unpersisted_retries_without_refresh(self, setup, http, monkeypatch, persist_fails):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        refresh = AsyncMock(return_value=self.ROTATED)
        persist = Mock(wraps=registry.update_credentials,
                       side_effect=[OSError("disk"), OSError("disk") if persist_fails else DEFAULT])
        monkeypatch.setattr(registry, "update_credentials", persist)
        poller = UsagePoller(registry, lambda c: FakeProvider(c.credentials),
                             refresh_fn=refresh, should_refresh=lambda c: True)
        assert (await poller.poll_account(account.id, http)).status == "ok"
        assert registry.get_config(account.id).credentials == self.OLD_CREDENTIALS
        assert await poller.flush_unpersisted() == ([account.id] if persist_fails else [])
        rotated = dict(self.ROTATED, expires_at="1893456000000")
        assert registry.get_config(account.id).credentials == (self.OLD_CREDENTIALS if persist_fails else rotated)
        if not persist_fails:
            assert await poller.flush_unpersisted() == []
        assert persist.call_count == 2
        refresh.assert_awaited_once_with(http, ProviderKind.claude_personal, self.OLD_CREDENTIALS)

    async def test_flush_loop_retries_without_polling(self, setup, http, monkeypatch):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        refresh = AsyncMock(return_value=self.ROTATED)
        provider_factory = Mock(side_effect=lambda c: FakeProvider(c.credentials))
        poller = UsagePoller(registry, provider_factory, refresh_fn=refresh,
                             should_refresh=lambda c: True)
        update = registry.update_credentials
        saved = asyncio.Event()
        writes = 0

        def persist(account_id, credentials):
            nonlocal writes
            writes += 1
            if writes <= 2:  # initial write and the first scheduled retry
                raise OSError("disk")
            update(account_id, credentials)
            saved.set()

        monkeypatch.setattr(registry, "update_credentials", persist)
        assert (await poller.poll_account(account.id, http)).status == "ok"
        assert registry.get_config(account.id).credentials == self.OLD_CREDENTIALS
        provider_factory.reset_mock()
        refresh.reset_mock()

        retry = asyncio.create_task(poller.run_flush_loop(interval=0.001))
        try:
            await asyncio.wait_for(saved.wait(), timeout=1)
        finally:
            retry.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retry

        assert writes == 3
        assert registry.get_config(account.id).credentials == dict(self.ROTATED, expires_at="1893456000000")
        provider_factory.assert_not_called()
        refresh.assert_not_called()

    async def test_flush_waits_for_poll_to_release_account_lock(self, setup, http, monkeypatch):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        refresh = AsyncMock(return_value=self.ROTATED)
        fetch_started = asyncio.Event()
        allow_fetch = asyncio.Event()
        update = registry.update_credentials
        persist = Mock(wraps=update, side_effect=OSError("disk"))
        monkeypatch.setattr(registry, "update_credentials", persist)

        class BlockingProvider(FakeProvider):
            async def fetch(self, http):
                fetch_started.set()
                await allow_fetch.wait()
                return await super().fetch(http)

        factory = Mock(side_effect=lambda c: FakeProvider(c.credentials))
        poller = UsagePoller(registry, factory, refresh_fn=refresh,
                             should_refresh=lambda c: c["access_token"] == "old-at")
        assert (await poller.poll_account(account.id, http)).status == "ok"
        factory.side_effect = lambda c: BlockingProvider(c.credentials)
        poll = asyncio.create_task(poller.poll_account(account.id, http))
        try:
            await asyncio.wait_for(fetch_started.wait(), timeout=1)
            persist.side_effect = None
            persist.reset_mock()
            assert registry.get_config(account.id).credentials == self.OLD_CREDENTIALS

            retry = asyncio.create_task(poller.flush_unpersisted())
            try:
                await asyncio.sleep(0)  # let the flusher reach the account lock
                persist.assert_not_called()
                assert not retry.done()
            finally:
                allow_fetch.set()
                unsaved = await asyncio.wait_for(retry, timeout=1)
        finally:
            allow_fetch.set()
            snapshot = await asyncio.wait_for(poll, timeout=1)

        assert snapshot.status == "ok"
        assert unsaved == []
        rotated = dict(self.ROTATED, expires_at="1893456000000")
        persist.assert_called_once_with(account.id, rotated)
        assert registry.get_config(account.id).credentials == rotated
        refresh.assert_awaited_once_with(http, ProviderKind.claude_personal, self.OLD_CREDENTIALS)

    @pytest.mark.parametrize("persist_fails", [False, True], ids=["reactive_401", "persist_failure"])
    async def test_rotated_credentials(self, setup, http, monkeypatch, persist_fails):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        refresh = AsyncMock(return_value=self.ROTATED)
        persist = Mock(wraps=registry.update_credentials, side_effect=OSError("disk") if persist_fails else None)
        monkeypatch.setattr(registry, "update_credentials", persist)
        seen = []

        class Provider(FakeProvider):
            async def fetch(self, http):
                token = self.credentials["access_token"]
                seen.append(token)
                if token == "old-at":
                    error = ProviderError("unauthorized")
                    error.status_code = 401
                    raise error
                return await super().fetch(http)

        poller = UsagePoller(registry, lambda c: Provider(c.credentials), refresh_fn=refresh,
                             should_refresh=lambda c: persist_fails and c["access_token"] == "old-at")
        assert (await poller.poll_account(account.id, http)).status == "ok"
        if persist_fails:
            assert registry.get_config(account.id).credentials == self.OLD_CREDENTIALS
            persist.side_effect = None
            assert (await poller.poll_account(account.id, http)).status == "ok"
        refresh.assert_awaited_once_with(http, ProviderKind.claude_personal, self.OLD_CREDENTIALS)
        assert seen == (["new-at", "new-at"] if persist_fails else ["old-at", "new-at"])
        assert registry.get_config(account.id).credentials == dict(
            self.ROTATED, expires_at="1893456000000"
        )

    @pytest.mark.parametrize("proactive_fails", [True, False], ids=["failed", "succeeded"])
    async def test_401_refresh_only_if_proactive_did_not_succeed(self, setup, http, proactive_fails):
        registry, _ = setup
        account = self._add_oauth_account(registry)
        refresh = AsyncMock(side_effect=[OAuthError("refresh rejected"), self.ROTATED]
                            if proactive_fails else [self.ROTATED])
        seen = []

        class Provider(FakeProvider):
            async def fetch(self, http):
                seen.append(self.credentials["access_token"])
                if len(seen) == 1:
                    error = ProviderError("unauthorized")
                    error.status_code = 401
                    raise error
                return await super().fetch(http)

        poller = UsagePoller(registry, lambda c: Provider(c.credentials), refresh_fn=refresh,
                             should_refresh=lambda c: True)
        snapshot = await poller.poll_account(account.id, http)

        assert refresh.await_count == (2 if proactive_fails else 1)
        assert seen == (["old-at", "new-at"] if proactive_fails else ["new-at"])
        assert snapshot.status == ("ok" if proactive_fails else "error")
        assert snapshot.error == (None if proactive_fails else "unauthorized")
        assert registry.get_config(account.id).credentials == dict(
            self.ROTATED, expires_at="1893456000000"
        )

    @pytest.mark.parametrize("proactive", [True, False], ids=["proactive", "reactive"])
    async def test_manual_account_with_forged_oauth_marker_never_refreshes(self, setup, http, proactive):
        registry, _ = setup
        credentials = dict(self.OLD_CREDENTIALS, oauth_provider="claude_personal")
        account = registry.add_account(ProviderKind.claude_personal, "Manual", credentials)
        refresh = AsyncMock(return_value=self.ROTATED)

        class UnauthorizedProvider(FakeProvider):
            async def fetch(self, http):
                error = ProviderError("unauthorized")
                error.status_code = 401
                raise error

        poller = UsagePoller(registry, lambda c: UnauthorizedProvider(c.credentials),
                             refresh_fn=refresh, should_refresh=lambda c: proactive)
        snapshot = await poller.poll_account(account.id, http)

        refresh.assert_not_called()
        assert snapshot.status == "error"
        assert snapshot.error == "unauthorized"
        stored = registry.get_config(account.id)
        assert stored.credentials == credentials
        assert stored.origin == "manual"

    @pytest.mark.parametrize("provider, origin", [
        (ProviderKind.claude_personal, "manual"),
        (ProviderKind.anthropic_org, "oauth"),
    ])
    async def test_refresh_requires_oauth_origin_and_personal_provider(self, setup, http, provider, origin):
        registry, _ = setup
        credentials = {"refresh_token": "test-rt"}
        account = registry.add_account(provider, "Test account", credentials, origin=origin)
        refresh = AsyncMock(return_value=self.ROTATED)
        poller = UsagePoller(registry, lambda c: FakeProvider(c.credentials), refresh_fn=refresh,
                             should_refresh=lambda c: True)
        assert (await poller.poll_account(account.id, http)).status == "ok"
        refresh.assert_not_called()
        assert registry.get_config(account.id).credentials == credentials

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

    async def test_lifespan_cancels_retry_before_final_flush(self, setup, monkeypatch, caplog):
        registry, poller = setup
        started = asyncio.Event()
        order = []

        async def retry():
            order.append("retry started")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                order.append("retry cancelled")

        async def flush():
            order.append("final flush")
            return ["unsaved-account"]

        monkeypatch.setattr(poller, "run_flush_loop", retry)
        monkeypatch.setattr(poller, "flush_unpersisted", flush)
        app = create_app(registry=registry, poller=poller)
        with pytest.raises(RuntimeError, match="shutdown after error"):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(started.wait(), timeout=1)
                raise RuntimeError("shutdown after error")

        assert order == ["retry started", "retry cancelled", "final flush"]
        assert caplog.messages == ["Rotated credentials unsaved at shutdown for unsaved-account"]

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
