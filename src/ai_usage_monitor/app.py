"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ai_usage_monitor.api import build_router
from ai_usage_monitor.poller import POLL_INTERVAL_SECONDS, UsagePoller
from ai_usage_monitor.registry import AccountRegistry

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(
    registry: AccountRegistry | None = None,
    poller: UsagePoller | None = None,
    provider_factory: Callable | None = None,
    interval: float = POLL_INTERVAL_SECONDS,
    start_background: bool = True,
) -> FastAPI:
    registry = registry or AccountRegistry()
    if poller is None:
        poller = UsagePoller(
            registry, provider_factory=provider_factory, interval=interval
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if not start_background:
            yield
            return
        async with httpx.AsyncClient(timeout=30.0) as http:
            app.state.http = http
            await poller.poll_all(http)
            task = asyncio.create_task(poller.run(http))
            yield
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="AI Usage Monitor", lifespan=lifespan)
    app.state.registry = registry
    app.state.poller = poller
    app.include_router(build_router(registry, poller))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
