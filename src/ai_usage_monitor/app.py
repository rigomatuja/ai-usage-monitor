"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ai_usage_monitor.models import ProviderKind

from ai_usage_monitor.api import build_oauth_router, build_router
from ai_usage_monitor.oauth import ChatGPTOAuthClient, ClaudeOAuthClient
from ai_usage_monitor.poller import HTTP_TIMEOUT_SECONDS, POLL_INTERVAL_SECONDS, UsagePoller
from ai_usage_monitor.registry import AccountRegistry

STATIC_DIR = Path(__file__).parent / "web" / "static"
logger = logging.getLogger(__name__)


def create_app(
    registry: AccountRegistry | None = None,
    poller: UsagePoller | None = None,
    provider_factory: Callable | None = None,
    interval: float = POLL_INTERVAL_SECONDS,
    start_background: bool = True,
    oauth_clients: dict | None = None,
    oauth_callback_port: int = 1455,
    oauth_listener_factory: Callable | None = None,
    app_port: int = 61524,
) -> FastAPI:
    registry = registry or AccountRegistry()
    if poller is None:
        poller = UsagePoller(
            registry, provider_factory=provider_factory, interval=interval
        )

    async def finish_rotations():
        await poller.drain_rotations()
        unsaved = await poller.flush_unpersisted()
        if unsaved:
            logger.error("Rotated credentials unsaved at shutdown for %s", ", ".join(unsaved))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if not start_background:
            try:
                yield
            finally:
                await finish_rotations()
            return
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
            app.state.http = http
            tasks = [asyncio.create_task(poller.run_flush_loop())]
            try:
                await poller.poll_all(http)
                tasks.append(asyncio.create_task(poller.run(http)))
                yield
            finally:
                listener_task = getattr(app.state, "oauth_listener_task", None)
                if listener_task is not None:
                    tasks.append(listener_task)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await finish_rotations()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        task.result()

    app = FastAPI(title="AI Usage Monitor", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def scrub_validation_errors(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422 responses must not echo submitted input: request bodies may
        contain credentials (e.g. admin API keys)."""
        errors = [
            {key: value for key, value in error.items() if key != "input"}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    app.state.registry = registry
    app.state.poller = poller
    # OAuth clients (injectable fakes) and pending-session state live on
    # app.state; the Claude client needs the app port for its redirect_uri,
    # the ChatGPT client the fixed callback port (1455 by default).
    if oauth_clients is None:
        oauth_clients = {
            ProviderKind.claude_personal.value: ClaudeOAuthClient(port=app_port),
            ProviderKind.chatgpt_personal.value: ChatGPTOAuthClient(
                port=oauth_callback_port
            ),
        }
    app.state.oauth_clients = oauth_clients
    app.state.oauth_pending = {}
    app.state.oauth_pending_timers = {}
    app.state.oauth_attempts = {}
    app.state.oauth_login_lock = asyncio.Lock()
    app.state.oauth_callback_port = oauth_callback_port
    app.state.app_port = app_port
    app.state.app_origin = f"http://127.0.0.1:{app_port}"
    app.include_router(build_router(registry, poller))
    app.include_router(build_oauth_router(listener_factory=oauth_listener_factory))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
