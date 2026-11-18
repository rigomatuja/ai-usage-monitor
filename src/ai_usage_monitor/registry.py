"""Account registry: auto-detected local logins plus manually added accounts.

Auto accounts are derived from the presence of local CLI login files; manual
accounts are persisted as JSON under the user config directory with 0600
permissions (they may contain credentials).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from platformdirs import user_config_dir

from ai_usage_monitor.models import (
    PROVIDER_PLATFORMS,
    AccountConfig,
    AccountInfo,
    ProviderKind,
)
from ai_usage_monitor.providers.base import UsageProvider

CLAUDE_CREDENTIALS_FILENAME = ".claude/.credentials.json"
CODEX_AUTH_FILENAME = ".codex/auth.json"

AUTO_CLAUDE_ID = "auto-claude-personal"
AUTO_CHATGPT_ID = "auto-chatgpt-personal"


class AccountError(Exception):
    """A manual account operation failed (e.g. removing an auto account)."""


class AccountRegistry:
    """Registry of auto-detected and manual accounts."""

    def __init__(
        self,
        config_dir: Path | None = None,
        claude_credentials_path: Path | None = None,
        codex_auth_path: Path | None = None,
    ) -> None:
        self._config_dir = config_dir or Path(user_config_dir("ai-usage-monitor"))
        self._accounts_file = self._config_dir / "accounts.json"
        self._claude_path = claude_credentials_path or (
            Path.home() / CLAUDE_CREDENTIALS_FILENAME
        )
        self._codex_path = codex_auth_path or Path.home() / CODEX_AUTH_FILENAME

    # -- auto accounts ----------------------------------------------------

    def _auto_configs(self) -> list[AccountConfig]:
        configs: list[AccountConfig] = []
        if self._claude_path.exists():
            configs.append(
                AccountConfig(
                    id=AUTO_CLAUDE_ID,
                    provider=ProviderKind.claude_personal,
                    label="Claude (local login)",
                    credentials={},
                )
            )
        if self._codex_path.exists():
            configs.append(
                AccountConfig(
                    id=AUTO_CHATGPT_ID,
                    provider=ProviderKind.chatgpt_personal,
                    label="ChatGPT (local login)",
                    credentials={},
                )
            )
        return configs

    def is_auto(self, account_id: str) -> bool:
        return account_id in (AUTO_CLAUDE_ID, AUTO_CHATGPT_ID)

    # -- manual accounts ----------------------------------------------------

    def _load_manual(self) -> list[AccountConfig]:
        if not self._accounts_file.exists():
            return []
        try:
            raw = json.loads(self._accounts_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [AccountConfig.model_validate(item) for item in raw]

    def _save_manual(self, accounts: list[AccountConfig]) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([account.model_dump(mode="json") for account in accounts], indent=2) + "\n"
        # Create the file with 0600 from the very first write: write_text()
        # would create it world/group-readable (umask default) until a later
        # chmod, exposing credentials in a transient window on multi-user
        # hosts.
        fd = os.open(
            self._accounts_file,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        # Defense in depth for files created by older versions.
        os.chmod(self._accounts_file, 0o600)

    # -- public API ---------------------------------------------------------

    def add_account(
        self, provider: ProviderKind, label: str, credentials: dict[str, str]
    ) -> AccountConfig:
        """Persist a new manual account."""
        account = AccountConfig(
            id=f"{provider.value}-{uuid.uuid4().hex[:8]}",
            provider=provider,
            label=label.strip() or provider.value,
            credentials=credentials,
        )
        accounts = self._load_manual()
        accounts.append(account)
        self._save_manual(accounts)
        return account

    def remove_account(self, account_id: str) -> None:
        """Remove a manual account. Raises AccountError for auto accounts,
        KeyError for unknown ids."""
        if self.is_auto(account_id):
            raise AccountError(
                f"Account {account_id} is auto-detected from a local login; "
                "it cannot be removed from the registry."
            )
        accounts = self._load_manual()
        remaining = [a for a in accounts if a.id != account_id]
        if len(remaining) == len(accounts):
            raise KeyError(account_id)
        self._save_manual(remaining)

    def iter_configs(self) -> list[AccountConfig]:
        """All current accounts: auto-detected first, then manual."""
        return self._auto_configs() + self._load_manual()

    def get_config(self, account_id: str) -> AccountConfig | None:
        for account in self.iter_configs():
            if account.id == account_id:
                return account
        return None

    def list_infos(self) -> list[AccountInfo]:
        return [
            AccountInfo(
                id=account.id,
                label=account.label,
                provider=account.provider,
                platform=PROVIDER_PLATFORMS[account.provider],
                source="auto" if self.is_auto(account.id) else "manual",
            )
            for account in self.iter_configs()
        ]


PROVIDER_CLASSES: dict[ProviderKind, str] = {
    ProviderKind.claude_personal: (
        "ai_usage_monitor.providers.claude_personal:ClaudePersonalProvider"
    ),
    ProviderKind.chatgpt_personal: (
        "ai_usage_monitor.providers.chatgpt_personal:ChatGPTPersonalProvider"
    ),
    ProviderKind.anthropic_org: (
        "ai_usage_monitor.providers.anthropic_org:AnthropicOrgProvider"
    ),
    ProviderKind.openai_org: "ai_usage_monitor.providers.openai_org:OpenAIOrgProvider",
}


def default_provider_factory(config: AccountConfig) -> UsageProvider:
    """Build the provider for an account config (lazy module import)."""
    module_path, class_name = PROVIDER_CLASSES[config.provider].split(":")
    import importlib

    module = importlib.import_module(module_path)
    provider_cls = getattr(module, class_name)
    return provider_cls(config.credentials)
