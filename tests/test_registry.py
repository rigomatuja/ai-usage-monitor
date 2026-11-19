"""Tests for the account registry (auto-detection + manual persistence)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ai_usage_monitor.models import ProviderKind
from ai_usage_monitor.registry import (
    AUTO_CHATGPT_ID,
    AUTO_CLAUDE_ID,
    AccountError,
    AccountRegistry,
)


def make_registry(tmp_path: Path, claude: bool = False, codex: bool = False):
    home = tmp_path / "home"
    home.mkdir()
    if claude:
        creds = home / ".claude"
        creds.mkdir()
        (creds / ".credentials.json").write_text("{}")
    if codex:
        auth = home / ".codex"
        auth.mkdir()
        (auth / "auth.json").write_text("{}")
    return AccountRegistry(
        config_dir=tmp_path / "config",
        claude_credentials_path=home / ".claude" / ".credentials.json",
        codex_auth_path=home / ".codex" / "auth.json",
    )


class TestAutoDetection:
    def test_no_logins_no_accounts(self, tmp_path):
        registry = make_registry(tmp_path)
        assert registry.iter_configs() == []
        assert registry.list_infos() == []

    def test_claude_login_detected(self, tmp_path):
        registry = make_registry(tmp_path, claude=True)
        infos = registry.list_infos()
        assert [i.id for i in infos] == [AUTO_CLAUDE_ID]
        assert infos[0].source == "auto"
        assert infos[0].platform == "claude"
        assert infos[0].provider == ProviderKind.claude_personal

    def test_codex_login_detected(self, tmp_path):
        registry = make_registry(tmp_path, codex=True)
        infos = registry.list_infos()
        assert [i.id for i in infos] == [AUTO_CHATGPT_ID]
        assert infos[0].source == "auto"
        assert infos[0].platform == "openai"

    def test_both_logins_detected_in_order(self, tmp_path):
        registry = make_registry(tmp_path, claude=True, codex=True)
        assert [i.id for i in registry.list_infos()] == [
            AUTO_CLAUDE_ID,
            AUTO_CHATGPT_ID,
        ]

    def test_auto_account_cannot_be_removed(self, tmp_path):
        registry = make_registry(tmp_path, claude=True)
        with pytest.raises(AccountError):
            registry.remove_account(AUTO_CLAUDE_ID)


class TestManualAccounts:
    def test_add_persists_and_lists(self, tmp_path):
        registry = make_registry(tmp_path)
        account = registry.add_account(
            ProviderKind.anthropic_org,
            "Work org",
            {"api_key": "sk-ant-admin-x"},
        )
        assert account.provider == ProviderKind.anthropic_org
        assert account.credentials == {"api_key": "sk-ant-admin-x"}

        infos = registry.list_infos()
        assert len(infos) == 1
        assert infos[0].source == "manual"
        assert infos[0].id == account.id

        # A fresh registry over the same config dir sees the same account.
        reloaded = AccountRegistry(
            config_dir=tmp_path / "config",
            claude_credentials_path=tmp_path / "home" / ".claude" / ".credentials.json",
            codex_auth_path=tmp_path / "home" / ".codex" / "auth.json",
        )
        assert [i.id for i in reloaded.list_infos()] == [account.id]

    def test_legacy_account_without_origin_loads_as_manual_without_rewrite(self, tmp_path):
        registry = make_registry(tmp_path)
        path = tmp_path / "config" / "accounts.json"
        path.parent.mkdir()
        credentials = {"access_token": "legacy-at", "refresh_token": "legacy-rt"}
        payload = json.dumps([{
            "id": "legacy", "provider": "claude_personal", "label": "Legacy",
            "credentials": credentials,
        }])
        path.write_text(payload, encoding="utf-8")

        account = registry.get_config("legacy")

        assert account.origin == "manual"
        assert account.credentials == credentials
        assert path.read_text(encoding="utf-8") == payload

    def test_accounts_file_has_0600_permissions(self, tmp_path):
        registry = make_registry(tmp_path)
        registry.add_account(
            ProviderKind.openai_org, "OpenAI org", {"api_key": "sk-admin"}
        )
        path = tmp_path / "config" / "accounts.json"
        assert path.exists()
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_add_account_strips_blank_label(self, tmp_path):
        registry = make_registry(tmp_path)
        account = registry.add_account(ProviderKind.openai_org, "   ", {})
        assert account.label == "openai_org"

    def test_remove_manual_account(self, tmp_path):
        registry = make_registry(tmp_path)
        account = registry.add_account(
            ProviderKind.openai_org, "OpenAI org", {"api_key": "k"}
        )
        registry.remove_account(account.id)
        assert registry.list_infos() == []

    def test_remove_unknown_account_raises_keyerror(self, tmp_path):
        registry = make_registry(tmp_path)
        with pytest.raises(KeyError):
            registry.remove_account("nope")

    def test_manual_accounts_listed_after_auto(self, tmp_path):
        registry = make_registry(tmp_path, claude=True)
        account = registry.add_account(
            ProviderKind.anthropic_org, "Work org", {"api_key": "k"}
        )
        assert [i.id for i in registry.list_infos()] == [
            AUTO_CLAUDE_ID,
            account.id,
        ]

    def test_corrupt_accounts_file_degrades_to_empty(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        (config / "accounts.json").write_text("not json", encoding="utf-8")
        registry = AccountRegistry(
            config_dir=config,
            claude_credentials_path=tmp_path / "home" / ".claude" / ".credentials.json",
            codex_auth_path=tmp_path / "home" / ".codex" / "auth.json",
        )
        assert registry.list_infos() == []

    def test_manual_account_ids_are_unique(self, tmp_path):
        registry = make_registry(tmp_path)
        a = registry.add_account(ProviderKind.openai_org, "one", {})
        b = registry.add_account(ProviderKind.openai_org, "two", {})
        assert a.id != b.id


class TestUpdateCredentials:
    NEW_CREDENTIALS = {
        "access_token": "new-at",
        "refresh_token": "new-rt",
        "expires_at": "1893456000000",
    }

    def test_update_credentials_replaces_and_persists(self, tmp_path):
        registry = make_registry(tmp_path)
        account = registry.add_account(
            ProviderKind.claude_personal,
            "OAuth Claude",
            {"access_token": "old-at", "refresh_token": "old-rt", "expires_at": "1"},
            origin="oauth",
        )

        registry.update_credentials(account.id, dict(self.NEW_CREDENTIALS))

        config = registry.get_config(account.id)
        assert config.credentials == self.NEW_CREDENTIALS
        # Persisted with 0600 and visible to a fresh registry instance.
        assert (
            stat.S_IMODE(os.stat(tmp_path / "config" / "accounts.json").st_mode)
            == 0o600
        )
        reloaded = AccountRegistry(
            config_dir=tmp_path / "config",
            claude_credentials_path=tmp_path / "home" / ".claude" / ".credentials.json",
            codex_auth_path=tmp_path / "home" / ".codex" / "auth.json",
        )
        stored = reloaded.get_config(account.id)
        assert stored.credentials == self.NEW_CREDENTIALS
        assert stored.origin == "oauth"

    def test_update_credentials_preserves_identity_and_siblings(self, tmp_path):
        registry = make_registry(tmp_path)
        account = registry.add_account(
            ProviderKind.claude_personal, "OAuth Claude", {"access_token": "old"}
        )
        sibling = registry.add_account(
            ProviderKind.chatgpt_personal, "OAuth ChatGPT", {"access_token": "sib"}
        )

        registry.update_credentials(account.id, {"access_token": "new"})

        updated = registry.get_config(account.id)
        assert (updated.id, updated.label, updated.provider) == (
            account.id,
            "OAuth Claude",
            ProviderKind.claude_personal,
        )
        assert updated.credentials == {"access_token": "new"}
        assert registry.get_config(sibling.id).credentials == {"access_token": "sib"}

    def test_update_credentials_unknown_id_raises_keyerror(self, tmp_path):
        registry = make_registry(tmp_path)
        with pytest.raises(KeyError):
            registry.update_credentials("nope", {"access_token": "x"})

    def test_update_credentials_auto_accounts_raise_keyerror(self, tmp_path):
        registry = make_registry(tmp_path, claude=True, codex=True)
        for account_id in (AUTO_CLAUDE_ID, AUTO_CHATGPT_ID):
            with pytest.raises(KeyError):
                registry.update_credentials(account_id, {"access_token": "x"})
        # The CLI-owned login files are never written by the registry:
        # no accounts.json was created.
        assert not (tmp_path / "config" / "accounts.json").exists()
