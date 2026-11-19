"""Account registry: auto-detected local logins plus manually added accounts.

Auto accounts are derived from the presence of local CLI login files; manual
accounts are persisted as JSON under the user config directory with 0600
permissions (they may contain credentials). Custom labels for either kind of
account are stored separately with the same permissions.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Literal

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


class InvalidAccountLabel(ValueError):
    """A custom label violates the label length limit."""


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
        self._labels_file = self._config_dir / "labels.json"
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
        self._save_json(self._accounts_file, [account.model_dump(mode="json") for account in accounts])

    def _load_labels(self) -> dict[str, str]:
        try:
            raw = json.loads(self._labels_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {key: value for key, value in raw.items() if isinstance(value, str) and value}

    def _save_json(self, path: Path, data: list[dict] | dict[str, str]) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2) + "\n"
        # Write privately beside the target so an interrupted write cannot
        # truncate the live store and replacement stays on the same filesystem.
        temp = path.with_name(f".tmp-{uuid.uuid4().hex}-{path.name}")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)  # Ensure the exact mode even with a restrictive umask.
                with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    # -- public API ---------------------------------------------------------

    def add_account(
        self,
        provider: ProviderKind,
        label: str | None = None,
        credentials: dict[str, str] | None = None,
        *,
        origin: Literal["manual", "oauth"] = "manual",
    ) -> AccountConfig:
        """Persist a new account added manually or through OAuth."""
        account = AccountConfig(
            id=f"{provider.value}-{uuid.uuid4().hex[:8]}",
            provider=provider,
            label=(label or "").strip() or provider.value,
            credentials=credentials if credentials is not None else {},
            origin=origin,
        )
        accounts = self._load_manual()
        accounts.append(account)
        self._save_manual(accounts)
        return account

    def set_label(self, account_id: str, label: str | None) -> AccountInfo:
        """Set or clear a display name without changing the account config."""
        account = self.get_config(account_id)
        if account is None:
            raise KeyError(account_id)
        label = (label or "").strip()
        if len(label) > 80:
            raise InvalidAccountLabel("label must be at most 80 characters")
        labels = self._load_labels()
        if label:
            labels[account_id] = label
            self._save_json(self._labels_file, labels)
        else:
            self._drop_label(account_id, labels)
        return self._account_info(account, labels)

    def _drop_label(self, account_id: str, labels: dict[str, str]) -> None:
        if account_id in labels:
            del labels[account_id]
            self._save_json(self._labels_file, labels)

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
        self._drop_label(account_id, self._load_labels())

    def update_credentials(self, account_id: str, credentials: dict[str, str]) -> None:
        """Replace the stored credentials of a manual account and persist
        (0600, same rules as ``_save_manual``).

        Raises KeyError for unknown ids and for the auto-detected accounts:
        their credentials are the CLI-owned login files and are never
        written by the registry."""
        if self.is_auto(account_id):
            raise KeyError(account_id)
        accounts = self._load_manual()
        for index, account in enumerate(accounts):
            if account.id == account_id:
                accounts[index] = account.model_copy(
                    update={"credentials": dict(credentials)}
                )
                self._save_manual(accounts)
                return
        raise KeyError(account_id)

    def iter_configs(self) -> list[AccountConfig]:
        """All current accounts: auto-detected first, then manual."""
        return self._auto_configs() + self._load_manual()

    def get_config(self, account_id: str) -> AccountConfig | None:
        for account in self.iter_configs():
            if account.id == account_id:
                return account
        return None

    def get_info(self, account_id: str) -> AccountInfo:
        account = self.get_config(account_id)
        if account is None:
            raise KeyError(account_id)
        return self._account_info(account, self._load_labels())

    def list_infos(self) -> list[AccountInfo]:
        labels = self._load_labels()
        return [self._account_info(account, labels) for account in self.iter_configs()]

    def _account_info(self, account: AccountConfig, labels: dict[str, str]) -> AccountInfo:
        return AccountInfo(
            id=account.id,
            label=labels.get(account.id, account.label),
            default_label=account.label,
            custom_label=account.id in labels,
            provider=account.provider,
            platform=PROVIDER_PLATFORMS[account.provider],
            source="auto" if self.is_auto(account.id) else "manual",
        )


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
