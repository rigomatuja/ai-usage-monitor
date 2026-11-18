"""Provider base contract.

Each provider wraps exactly one usage endpoint family. Providers are cheap to
construct and must not perform I/O in ``__init__``; all network work happens in
``fetch``, called with a shared ``httpx.AsyncClient``.

Rules every provider follows (see odd/tasks/v1-web-monitor.md):
- Percent values are already on a 0-100 scale; never rescale.
- Never refresh tokens: the owning CLI manages the refresh flow, and racing it
  desynchronizes the CLI.
- Absent/unknown fields degrade to omitted windows, never errors; errors are
  raised as ``ProviderError`` only when the account cannot be polled at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

import httpx

from ai_usage_monitor.models import UsageWindow


class ProviderError(Exception):
    """An account could not be polled (bad credentials, HTTP error, ...)."""


class UsageProvider(ABC):
    """Base class for usage providers."""

    def __init__(self, credentials: Mapping[str, str]) -> None:
        self.credentials = dict(credentials)

    @abstractmethod
    async def fetch(self, http: httpx.AsyncClient) -> list[UsageWindow]:
        """Fetch current usage windows. Raises ProviderError on failure."""
        raise NotImplementedError
