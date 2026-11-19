"""Shared domain models for AI usage monitoring."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ProviderKind(str, Enum):
    """Kind of usage provider an account connects to."""

    claude_personal = "claude_personal"    # Claude Pro/Max subscription
    chatgpt_personal = "chatgpt_personal"  # ChatGPT Plus/Pro subscription
    anthropic_org = "anthropic_org"        # Claude Team/Enterprise Admin API
    openai_org = "openai_org"              # OpenAI organization Admin usage API


class WindowKind(str, Enum):
    """Kind of usage window a provider reports."""

    five_hour = "five_hour"
    weekly = "weekly"
    daily = "daily"  # org token/cost buckets (no percent)


class UsageWindow(BaseModel):
    """One usage window (e.g. 5-hour session limit, weekly limit, daily tokens)."""

    kind: WindowKind
    label: str
    used_percent: float | None = None  # None for org daily buckets
    detail: str | None = None
    resets_at: datetime | None = None


class UsageSnapshot(BaseModel):
    """Result of one successful or failed poll of one account."""

    account_id: str
    fetched_at: datetime
    status: Literal["ok", "error"]
    error: str | None = None
    windows: list[UsageWindow] = Field(default_factory=list)


class AccountInfo(BaseModel):
    """Public description of a configured account (no credentials)."""

    id: str
    label: str
    default_label: str
    custom_label: bool
    provider: ProviderKind
    platform: Literal["claude", "openai"]
    source: Literal["auto", "manual"]


class AccountConfig(BaseModel):
    """Configuration for an account, including how it was added."""

    id: str
    provider: ProviderKind
    label: str
    credentials: dict[str, str]
    origin: Literal["manual", "oauth"] = "manual"


PROVIDER_PLATFORMS: dict[ProviderKind, Literal["claude", "openai"]] = {
    ProviderKind.claude_personal: "claude",
    ProviderKind.chatgpt_personal: "openai",
    ProviderKind.anthropic_org: "claude",
    ProviderKind.openai_org: "openai",
}
