"""REST API for accounts and usage snapshots."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ai_usage_monitor.models import (
    PROVIDER_PLATFORMS,
    AccountInfo,
    ProviderKind,
    UsageSnapshot,
)
from ai_usage_monitor.registry import AccountError, AccountRegistry
from ai_usage_monitor.poller import UsagePoller


class NewAccountRequest(BaseModel):
    provider: ProviderKind
    label: str = Field(min_length=1)
    credentials: dict[str, str] = Field(default_factory=dict)


def build_router(registry: AccountRegistry, poller: UsagePoller) -> APIRouter:
    router = APIRouter()

    async def _client_for(request: Request) -> httpx.AsyncClient:
        """Use the app-lifetime client when running under the lifespan,
        a short-lived one otherwise (e.g. in tests)."""
        http = getattr(request.app.state, "http", None)
        if http is not None:
            return http
        return httpx.AsyncClient(timeout=30.0)

    def _is_app_client(request: Request, http: httpx.AsyncClient) -> bool:
        return http is getattr(request.app.state, "http", None)

    @router.get("/api/accounts")
    async def list_accounts() -> list[AccountInfo]:
        return registry.list_infos()

    @router.get("/api/usage")
    async def list_usage() -> list[UsageSnapshot]:
        return poller.snapshots()

    @router.post(
        "/api/accounts", status_code=status.HTTP_201_CREATED
    )
    async def add_account(body: NewAccountRequest, request: Request) -> AccountInfo:
        account = registry.add_account(body.provider, body.label, body.credentials)
        # Poll immediately so the new account shows data without waiting
        # for the next background cycle; errors are stored, not raised.
        http = await _client_for(request)
        try:
            await poller.poll_account(account.id, http)
        finally:
            if not _is_app_client(request, http):
                await http.aclose()
        return AccountInfo(
            id=account.id,
            label=account.label,
            provider=account.provider,
            platform=PROVIDER_PLATFORMS[account.provider],
            source="manual",
        )

    @router.delete("/api/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove_account(account_id: str) -> Response:
        try:
            registry.remove_account(account_id)
        except AccountError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown account") from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/api/usage/refresh/{account_id}")
    async def refresh_usage(account_id: str, request: Request) -> UsageSnapshot:
        if registry.get_config(account_id) is None:
            raise HTTPException(status_code=404, detail="unknown account")
        http = await _client_for(request)
        try:
            return await poller.poll_account(account_id, http)
        finally:
            if not _is_app_client(request, http):
                await http.aclose()

    return router
