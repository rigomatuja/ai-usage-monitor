"""REST API for accounts and usage snapshots."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from time import monotonic
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ai_usage_monitor.models import (
    PROVIDER_PLATFORMS,
    AccountInfo,
    ProviderKind,
    UsageSnapshot,
)
from ai_usage_monitor.registry import AccountError, AccountRegistry
from ai_usage_monitor.poller import UsagePoller

logger = logging.getLogger(__name__)

_CLAUDE = ProviderKind.claude_personal.value
_CHATGPT = ProviderKind.chatgpt_personal.value
_OAUTH_PROVIDERS = (_CLAUDE, _CHATGPT)
_CHATGPT_CALLBACK_PATH = "/auth/callback"
CHATGPT_LISTENER_TIMEOUT_SECONDS = 600


class OAuthCallbackError(Exception):
    """An OAuth callback/completion step failed. Messages never contain
    token values; the ChatGPT listener turns this into an HTML 400 page."""


class _UnconsumedOAuthCallbackError(OAuthCallbackError):
    """The callback was rejected before consuming a sign-in session."""


_CALLBACK_ERROR_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<title>Sign-in failed</title></head><body>"
    "<h1>Sign-in failed</h1><p>{message}</p>"
    "<p><a href=\"/\">Back to the app</a></p></body></html>"
)

_CALLBACK_SUCCESS_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<meta http-equiv=\"refresh\" content=\"0; url=/\">"
    "<title>Account connected</title></head><body>"
    "<p>Account connected — redirecting…</p></body></html>"
)


def _html_error(message: str) -> HTMLResponse:
    return HTMLResponse(_CALLBACK_ERROR_HTML.format(message=message), status_code=400)


def _credential_dict(exchanged: dict) -> dict[str, str]:
    """Storage contract: credential values are strings."""
    return {
        key: value if isinstance(value, str) else str(value)
        for key, value in dict(exchanged).items()
        if value is not None
    }


class _ListenerDeadline:
    def __init__(self, timeout: float, clock: Callable[[], float]) -> None:
        self._timeout = timeout
        self._clock = clock
        self.extend()

    def extend(self) -> None:
        self._expires_at = self._clock() + self._timeout

    def remaining(self) -> float:
        return self._expires_at - self._clock()


async def _start_chatgpt_listener(
    port: int,
    complete: Callable[[str | None, str | None], Awaitable[str]],
    deadline: _ListenerDeadline,
    has_pending: Callable[[], bool],
) -> asyncio.Task:
    """Temporary loopback-only callback listener for the ChatGPT OAuth flow
    (redirect_uri host is a FIXED port in the client allow-list).

    Binds 127.0.0.1:<port> and serves GET /auth/callback until a callback
    consumes the pending session or the deadline expires.
    Returns the serving task right after binding succeeds so the
    login request does not block; raises OSError when the port is taken."""
    done = asyncio.Event()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session_consumed = False
        try:
            request_line = (await reader.readline()).decode("latin-1")
            while True:  # drain headers
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
            parts = request_line.split()
            parsed = urlsplit(parts[1] if len(parts) >= 2 else "/")
            if parsed.path != _CHATGPT_CALLBACK_PATH:
                payload = b"not found"
                writer.write(
                    f"HTTP/1.1 404 Not Found\r\nContent-Length: {len(payload)}"
                    "\r\nConnection: close\r\n\r\n".encode("latin-1") + payload
                )
                await writer.drain()
                return  # keep waiting for the real callback
            params = parse_qs(parsed.query)
            code = (params.get("code") or [None])[0]
            state = (params.get("state") or [None])[0]
            try:
                location = await complete(code, state)
                session_consumed = True
                head = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n"
                payload = b""
            except Exception as exc:
                session_consumed = not isinstance(exc, _UnconsumedOAuthCallbackError)
                # Never a stack trace, never token values.
                logger.warning("ChatGPT OAuth callback failed", exc_info=True)
                payload = _CALLBACK_ERROR_HTML.format(
                    message="Sign-in failed. Please try signing in again."
                ).encode("utf-8")
                head = (
                    "HTTP/1.1 400 Bad Request\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                )
            writer.write(
                f"{head}Content-Length: {len(payload)}"
                "\r\nConnection: close\r\n\r\n".encode("latin-1") + payload
            )
            await writer.drain()
        except Exception:
            logger.warning("ChatGPT callback listener I/O error", exc_info=True)
        finally:
            writer.close()
            if session_consumed:
                done.set()

    server = await asyncio.start_server(handle, "127.0.0.1", port)

    async def serve() -> None:
        async with server:
            while True:
                if done.is_set():
                    if not has_pending():
                        break
                    done.clear()
                remaining = deadline.remaining()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(done.wait(), remaining)
                except TimeoutError:
                    # A new login may have extended the deadline during this wait.
                    continue

    return asyncio.create_task(serve(), name="oauth-chatgpt-callback-listener")


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


def build_oauth_router(listener_factory: Callable | None = None) -> APIRouter:
    """OAuth login routes: POST /api/oauth/{provider}/login starts a pending
    PKCE/state session (and, for ChatGPT, a temporary callback listener),
    GET /api/oauth/claude/callback completes the Claude flow on the app port.
    OAuth clients and pending-session state live on ``app.state`` so tests
    can inject fakes."""
    router = APIRouter()

    def _oauth_clients(app: FastAPI) -> dict:
        clients = getattr(app.state, "oauth_clients", None)
        if clients is None:  # direct router use without create_app wiring
            from ai_usage_monitor.oauth import ChatGPTOAuthClient, ClaudeOAuthClient

            clients = {_CLAUDE: ClaudeOAuthClient(), _CHATGPT: ChatGPTOAuthClient()}
            app.state.oauth_clients = clients
        return clients

    @contextlib.asynccontextmanager
    async def _http_for(app: FastAPI):
        """Use the app-lifetime client when running under the lifespan,
        a short-lived one otherwise (e.g. in tests)."""
        http = getattr(app.state, "http", None)
        if http is not None:
            yield http
            return
        async with httpx.AsyncClient(timeout=30.0) as owned:
            yield owned

    def _chatgpt_completion(app: FastAPI):
        """Completion callback handed to the temporary listener: validate the
        state, exchange the code, store the account, poll once, and return
        the redirect target for the browser (the app origin)."""

        async def complete(code: str | None, state: str | None) -> str:
            pending = app.state.oauth_pending.get(_CHATGPT)
            if pending is None:
                raise _UnconsumedOAuthCallbackError("no pending sign-in session")
            if (
                not code
                or not state
                or not secrets.compare_digest(
                    state.encode(), pending["state"].encode()
                )
            ):
                raise _UnconsumedOAuthCallbackError("invalid state parameter")
            app.state.oauth_pending.pop(_CHATGPT, None)
            client = _oauth_clients(app)[_CHATGPT]
            async with _http_for(app) as http:
                try:
                    exchanged = await client.exchange_code(
                        http, code, pending["verifier"], pending["state"]
                    )
                except Exception as exc:
                    logger.warning("ChatGPT OAuth code exchange failed", exc_info=True)
                    raise OAuthCallbackError("token exchange failed") from exc
                credentials = _credential_dict(exchanged)
                account = app.state.registry.add_account(
                    ProviderKind.chatgpt_personal,
                    credentials.get("email") or "ChatGPT OAuth",
                    credentials,
                    origin="oauth",
                )
                try:  # poll once; non-fatal
                    await app.state.poller.poll_account(account.id, http)
                except Exception:
                    logger.warning(
                        "post-login poll failed for %s", account.id, exc_info=True
                    )
            return app.state.app_origin + "/"

        return complete

    @router.post("/api/oauth/{provider}/login")
    async def oauth_login(provider: str, request: Request) -> dict[str, str]:
        if provider not in _OAUTH_PROVIDERS:
            raise HTTPException(
                status_code=404, detail=f"unknown oauth provider: {provider}"
            )
        app = request.app
        client = _oauth_clients(app)[provider]
        verifier = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        authorize_url = client.build_authorize_url(verifier, state)
        if provider == _CHATGPT:
            task = getattr(app.state, "oauth_listener_task", None)
            if task is None or task.done():
                deadline = _ListenerDeadline(CHATGPT_LISTENER_TIMEOUT_SECONDS, monotonic)
                port = app.state.oauth_callback_port
                complete = _chatgpt_completion(app)
                try:
                    if listener_factory is None:
                        task = await _start_chatgpt_listener(
                            port, complete, deadline, lambda: _CHATGPT in app.state.oauth_pending
                        )
                    else:
                        task = await listener_factory(port, complete)
                except OSError as exc:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "could not start the local callback listener on port "
                            f"{port}; it may already be in use"
                        ),
                    ) from exc
                if task is not None:
                    app.state.oauth_listener_task = task
                    app.state.oauth_listener_deadline = deadline
                    task.add_done_callback(
                        lambda t: app.state.oauth_pending.pop(_CHATGPT, None)
                        if t is app.state.oauth_listener_task else None
                    )
            else:
                deadline = app.state.oauth_listener_deadline
            deadline.extend()
        # ONE pending login session per provider; a new call replaces it.
        app.state.oauth_pending[provider] = {"verifier": verifier, "state": state}
        return {"authorize_url": authorize_url}

    @router.get("/api/oauth/claude/callback", include_in_schema=False)
    async def claude_oauth_callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> Response:
        app = request.app
        pending = app.state.oauth_pending.get(_CLAUDE)
        # The returned code may be {code}#{state}: split and use both parts.
        if code and "#" in code and not state:
            code, _, state = code.partition("#")
        if pending is None:
            return _html_error("No pending sign-in session. Start the sign-in again.")
        if (
            not code
            or not state
            or not secrets.compare_digest(state.encode(), pending["state"].encode())
        ):
            return _html_error("Invalid or expired sign-in state.")
        app.state.oauth_pending.pop(_CLAUDE, None)
        client = _oauth_clients(app)[_CLAUDE]
        async with _http_for(app) as http:
            try:
                exchanged = await client.exchange_code(
                    http, code, pending["verifier"], pending["state"]
                )
            except Exception:
                # Never a stack trace, never token values.
                logger.warning("Claude OAuth code exchange failed", exc_info=True)
                return _html_error(
                    "Token exchange failed. Please start the sign-in again."
                )
            credentials = _credential_dict(exchanged)
            account = app.state.registry.add_account(
                ProviderKind.claude_personal,
                credentials.get("email") or "Claude OAuth",
                credentials,
                origin="oauth",
            )
            try:  # poll once; non-fatal
                await app.state.poller.poll_account(account.id, http)
            except Exception:
                logger.warning(
                    "post-login poll failed for %s", account.id, exc_info=True
                )
        return HTMLResponse(_CALLBACK_SUCCESS_HTML)

    return router
