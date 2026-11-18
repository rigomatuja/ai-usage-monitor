"""Background poller: keeps the last good usage snapshot per account."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from ai_usage_monitor.models import UsageSnapshot, UsageWindow
from ai_usage_monitor.providers.base import ProviderError, UsageProvider
from ai_usage_monitor.registry import AccountRegistry

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 300.0


class UsagePoller:
    """Polls every registered account and caches snapshots in memory.

    On provider failure the last good windows are kept in the error snapshot
    so the UI can show stale data with an explicit marker instead of blanks.
    """

    def __init__(
        self,
        registry: AccountRegistry,
        provider_factory: Callable | None = None,
        interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        from ai_usage_monitor.registry import default_provider_factory

        self._registry = registry
        self._provider_factory = provider_factory or default_provider_factory
        self._interval = interval
        self._snapshots: dict[str, UsageSnapshot] = {}
        self._last_good_windows: dict[str, list[UsageWindow]] = {}

    async def poll_account(
        self, account_id: str, http: httpx.AsyncClient
    ) -> UsageSnapshot:
        """Poll one account and store its snapshot. Raises KeyError if the
        account does not exist."""
        config = self._registry.get_config(account_id)
        if config is None:
            raise KeyError(account_id)

        try:
            provider: UsageProvider = self._provider_factory(config)
            windows = await provider.fetch(http)
        except ProviderError as exc:
            snapshot = UsageSnapshot(
                account_id=account_id,
                fetched_at=datetime.now(timezone.utc),
                status="error",
                error=str(exc),
                windows=list(self._last_good_windows.get(account_id, [])),
            )
        except Exception as exc:  # defensive: a provider bug must not kill polling
            logger.exception("unexpected provider failure for %s", account_id)
            snapshot = UsageSnapshot(
                account_id=account_id,
                fetched_at=datetime.now(timezone.utc),
                status="error",
                error=f"unexpected error: {exc}",
                windows=list(self._last_good_windows.get(account_id, [])),
            )
        else:
            snapshot = UsageSnapshot(
                account_id=account_id,
                fetched_at=datetime.now(timezone.utc),
                status="ok",
                windows=windows,
            )
            self._last_good_windows[account_id] = list(windows)

        self._snapshots[account_id] = snapshot
        return snapshot

    async def poll_all(self, http: httpx.AsyncClient) -> None:
        """Poll every current account; one account's failure never stops
        the others."""
        for account in self._registry.iter_configs():
            try:
                await self.poll_account(account.id, http)
            except KeyError:  # account vanished mid-iteration
                continue

    def snapshots(self) -> list[UsageSnapshot]:
        """Snapshots for all current accounts (only those already polled)."""
        return [
            self._snapshots[account.id]
            for account in self._registry.iter_configs()
            if account.id in self._snapshots
        ]

    def get_snapshot(self, account_id: str) -> UsageSnapshot | None:
        return self._snapshots.get(account_id)

    async def run(self, http: httpx.AsyncClient) -> None:
        """Poll all accounts forever, once per interval."""
        while True:
            await asyncio.sleep(self._interval)
            await self.poll_all(http)
