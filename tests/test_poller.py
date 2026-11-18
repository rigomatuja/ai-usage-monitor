"""Tests for the usage poller (last-good snapshot semantics)."""

from __future__ import annotations

import httpx
import pytest

from ai_usage_monitor.models import ProviderKind, UsageWindow, WindowKind
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
