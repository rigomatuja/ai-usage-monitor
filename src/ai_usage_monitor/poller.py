"""Background poller: keeps the last good usage snapshot per account."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from ai_usage_monitor import oauth
from ai_usage_monitor.models import ProviderKind, UsageSnapshot, UsageWindow
from ai_usage_monitor.providers.base import ProviderError, UsageProvider
from ai_usage_monitor.registry import AccountRegistry

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 300.0


def _default_should_refresh(credentials: dict[str, str]) -> bool:
    """Proactively refresh when within 60 s of expiry (oauth contract)."""
    return oauth.should_refresh(credentials, now_ms=int(time.time() * 1000))


async def _default_refresh_fn(
    http: httpx.AsyncClient, provider: ProviderKind, credentials: dict[str, str]
) -> dict[str, str]:
    """Refresh via the provider's oauth client; the returned dict must be
    persisted in full (both providers rotate refresh tokens)."""
    client_cls = {
        ProviderKind.claude_personal: oauth.ClaudeOAuthClient,
        ProviderKind.chatgpt_personal: oauth.ChatGPTOAuthClient,
    }.get(provider)
    if client_cls is None:
        raise ValueError(f"unsupported OAuth provider: {provider}")
    return await client_cls().refresh_tokens(http, credentials)


def _stringify_credentials(credentials: dict) -> dict[str, str]:
    """Storage contract: credential values are strings (expires_at is an
    epoch-ms int from the oauth clients, stored as str)."""
    return {
        key: value if isinstance(value, str) else str(value)
        for key, value in dict(credentials).items()
        if value is not None
    }


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
        refresh_fn: Callable | None = None,
        should_refresh: Callable | None = None,
    ) -> None:
        from ai_usage_monitor.registry import default_provider_factory

        self._registry = registry
        self._provider_factory = provider_factory or default_provider_factory
        self._interval = interval
        self._refresh_fn = refresh_fn or _default_refresh_fn
        self._should_refresh = should_refresh or _default_should_refresh
        self._snapshots: dict[str, UsageSnapshot] = {}
        self._last_good_windows: dict[str, list[UsageWindow]] = {}
        self._unpersisted: dict[str, dict[str, str]] = {}

    def _persist(self, account_id: str, credentials: dict[str, str]) -> None:
        try:
            self._registry.update_credentials(account_id, credentials)
        except Exception:
            logger.warning("could not persist rotated credentials for %s", account_id)
            self._unpersisted[account_id] = credentials
        else:
            self._unpersisted.pop(account_id, None)

    async def _refresh(self, account_id, provider, credentials, http) -> dict[str, str] | None:
        try:
            refreshed = await self._refresh_fn(http, provider, credentials)
        except Exception as exc:
            logger.warning("token refresh failed for %s: %s", account_id, exc)
            return None
        rotated = _stringify_credentials(refreshed)
        self._persist(account_id, rotated)
        return rotated

    async def poll_account(
        self, account_id: str, http: httpx.AsyncClient
    ) -> UsageSnapshot:
        """Poll one account and store its snapshot. Raises KeyError if the
        account does not exist."""
        config = self._registry.get_config(account_id)
        if config is None:
            raise KeyError(account_id)

        credentials = self._unpersisted.get(account_id)
        if credentials is None:
            credentials = dict(config.credentials)
        else:
            self._persist(account_id, credentials)

        kind = config.provider
        refreshable = (
            config.origin == "oauth"
            and kind in (ProviderKind.claude_personal, ProviderKind.chatgpt_personal)
            and "refresh_token" in credentials
        )
        proactive_refresh_succeeded = False
        if refreshable and self._should_refresh(credentials):
            rotated = await self._refresh(account_id, kind, credentials, http)
            if rotated is not None:
                credentials = rotated
                proactive_refresh_succeeded = True
        if credentials != config.credentials:
            config = config.model_copy(update={"credentials": credentials})

        try:
            provider: UsageProvider = self._provider_factory(config)
            try:
                windows = await provider.fetch(http)
            except ProviderError as exc:
                if (
                    getattr(exc, "status_code", None) != 401
                    or not refreshable
                    or proactive_refresh_succeeded
                ):
                    raise
                rotated = await self._refresh(account_id, kind, credentials, http)
                if rotated is None:
                    raise
                provider = self._provider_factory(config.model_copy(update={"credentials": rotated}))
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
