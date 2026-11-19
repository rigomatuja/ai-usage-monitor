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
from ai_usage_monitor.poller import HTTP_TIMEOUT_SECONDS, UsagePoller

logger = logging.getLogger(__name__)

_CLAUDE = ProviderKind.claude_personal.value
_CHATGPT = ProviderKind.chatgpt_personal.value
_OAUTH_PROVIDERS = (_CLAUDE, _CHATGPT)
_CHATGPT_CALLBACK_PATH = "/auth/callback"
CHATGPT_LISTENER_TIMEOUT_SECONDS = 600
_FINISHED_ATTEMPT_LIMIT = 50
_ATTEMPT_ERRORS = {
    "failed": "Sign-in failed. Please try signing in again.",
    "expired": "Sign-in expired. Please try signing in again.",
}


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
        self.changed = asyncio.Event()
        self.closed = False
        # Set by the running listener: stops accepting connections at once
        # without closing the callback connection being served.
        self.close_listener: Callable[[], None] | None = None
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
    (redirect_uri uses a fixed port in the client allow-list).

    Binds 127.0.0.1:<port> and serves GET /auth/callback while ChatGPT
    attempts are pending and the deadline has not passed. Returns the serving
    task as soon as binding succeeds; raises OSError when the port is taken."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
                head = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n"
                payload = b""
            except Exception:
                # Never a stack trace, never token values.
                logger.warning("ChatGPT OAuth callback failed")
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

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    deadline.close_listener = server.close

    async def serve() -> None:
        async with server:
            try:
                while has_pending() and (remaining := deadline.remaining()) > 0:
                    deadline.changed.clear()
                    try:
                        await asyncio.wait_for(deadline.changed.wait(), remaining)
                    except TimeoutError:
                        # Recheck: a login may have extended the deadline.
                        continue
            finally:
                # The socket can close before the task finishes its connections.
                deadline.closed = True

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
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)

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
    """Dashboard-origin login POSTs start independent PKCE/state attempts.

    ChatGPT shares a temporary callback listener; Claude callbacks use the
    app port. Clients and pending attempts live on ``app.state``."""
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
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as owned:
            yield owned

    def _has_pending_chatgpt(app: FastAPI) -> bool:
        return any(p["provider"] == _CHATGPT for p in app.state.oauth_pending.values())

    def _mark_attempt(app: FastAPI, pending: dict, status: str) -> None:
        attempts = app.state.oauth_attempts
        attempt_id = pending["attempt_id"]
        # Move the entry to the end so eviction follows completion order.
        attempts.pop(attempt_id)
        attempts[attempt_id] = status
        finished = [key for key, value in attempts.items() if value != "pending"]
        for key in finished[:-_FINISHED_ATTEMPT_LIMIT]:
            del attempts[key]
        if pending["provider"] == _CHATGPT and not _has_pending_chatgpt(app):
            # Close the listening socket, not the callback connection. The
            # callback can still poll the new account and send its response.
            deadline = app.state.oauth_listener_deadline
            if deadline.close_listener is not None:
                deadline.close_listener()
            deadline.closed = True
            deadline.changed.set()

    def _pop_pending(app: FastAPI, state: str) -> dict:
        app.state.oauth_pending_timers.pop(state).cancel()
        return app.state.oauth_pending.pop(state)

    def _expire_pending(app: FastAPI, state: str) -> None:
        pending = _pop_pending(app, state)
        _mark_attempt(app, pending, "expired")

    def _consume_pending(
        app: FastAPI, provider: str, code: str | None, state: str | None
    ) -> dict:
        pending = app.state.oauth_pending.get(state)
        if pending is None or pending["provider"] != provider:
            raise _UnconsumedOAuthCallbackError("invalid state parameter")
        if monotonic() - pending["created_at"] >= CHATGPT_LISTENER_TIMEOUT_SECONDS:
            _expire_pending(app, state)
            raise _UnconsumedOAuthCallbackError("expired state parameter")
        if not code:
            raise _UnconsumedOAuthCallbackError("missing authorization code")
        return _pop_pending(app, state)

    @router.get("/api/oauth/attempts/{attempt_id}")
    async def oauth_attempt(attempt_id: str, request: Request, response: Response) -> dict[str, str]:
        status = request.app.state.oauth_attempts.get(attempt_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown oauth attempt")
        if status in _ATTEMPT_ERRORS:
            response.headers["X-OAuth-Error"] = _ATTEMPT_ERRORS[status]
        return {"status": status}

    async def _complete_sign_in(
        app: FastAPI, provider: str, label: str, code: str | None, state: str | None
    ) -> None:
        pending = _consume_pending(app, provider, code, state)
        message = "Token exchange failed. Please start the sign-in again."
        stored = False
        try:
            async with _http_for(app) as http:
                client = _oauth_clients(app)[provider]
                exchanged = await client.exchange_code(http, code, pending["verifier"], state)
                message = _ATTEMPT_ERRORS["failed"]
                credentials = _credential_dict(exchanged)
                account = app.state.registry.add_account(
                    ProviderKind(provider), credentials.get("email") or label, credentials,
                    origin="oauth",
                )
                stored = True
                _mark_attempt(app, pending, "completed")
                await app.state.poller.poll_account(account.id, http)
        except Exception:
            if not stored:
                logger.warning("%s OAuth callback: %s", provider, message)
                raise OAuthCallbackError(message) from None
            logger.warning("post-login poll or HTTP cleanup failed for %s", account.id)
        finally:
            if not stored:
                _mark_attempt(app, pending, "failed")

    def _chatgpt_completion(app: FastAPI):
        async def complete(code: str | None, state: str | None) -> str:
            await _complete_sign_in(app, _CHATGPT, "ChatGPT OAuth", code, state)
            return app.state.app_origin + "/"

        return complete

    @router.post(
        "/api/oauth/{provider}/login",
        responses={403: {"description": "Requests must originate from the dashboard"}},
    )
    async def oauth_login(provider: str, request: Request) -> dict[str, str]:
        if provider not in _OAUTH_PROVIDERS:
            raise HTTPException(
                status_code=404, detail=f"unknown oauth provider: {provider}"
            )
        app = request.app
        origins = request.headers.getlist("origin")
        dashboard_origins = (app.state.app_origin, f"http://localhost:{app.state.app_port}")
        if (
            len(origins) != 1
            or origins[0] not in dashboard_origins
            or "cross-site" in request.headers.getlist("sec-fetch-site")
        ):
            raise HTTPException(status_code=403, detail="Cross-origin OAuth login denied")
        async with app.state.oauth_login_lock:
            return await _start_login(app, provider)

    async def _start_login(app: FastAPI, provider: str) -> dict[str, str]:
        client = _oauth_clients(app)[provider]
        verifier = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        authorize_url = client.build_authorize_url(verifier, state)
        if provider == _CHATGPT:
            task = getattr(app.state, "oauth_listener_task", None)
            if task is None or task.done() or app.state.oauth_listener_deadline.closed:
                deadline = _ListenerDeadline(CHATGPT_LISTENER_TIMEOUT_SECONDS, monotonic)
                port = app.state.oauth_callback_port
                complete = _chatgpt_completion(app)
                try:
                    if listener_factory is None:
                        task = await _start_chatgpt_listener(
                            port, complete, deadline,
                            lambda: _has_pending_chatgpt(app),
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
                app.state.oauth_listener_task = task
                app.state.oauth_listener_deadline = deadline
        attempt_id = secrets.token_urlsafe(16)
        app.state.oauth_attempts[attempt_id] = "pending"
        app.state.oauth_pending[state] = {
            "provider": provider, "verifier": verifier, "attempt_id": attempt_id,
            "created_at": monotonic(),
        }
        app.state.oauth_pending_timers[state] = asyncio.get_running_loop().call_later(
            CHATGPT_LISTENER_TIMEOUT_SECONDS, _expire_pending, app, state
        )
        if provider == _CHATGPT:
            app.state.oauth_listener_deadline.extend()
        return {"authorize_url": authorize_url, "attempt_id": attempt_id}

    @router.get("/api/oauth/claude/callback", include_in_schema=False)
    async def claude_oauth_callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> Response:
        app = request.app
        # The returned code may be {code}#{state}: split and use both parts.
        if code:
            code, separator, embedded_state = code.partition("#")
            # A '#' always carries a state, even an empty one: an empty or
            # conflicting embedded state is rejected before any attempt is consumed.
            if separator:
                if not embedded_state or (
                    state and not secrets.compare_digest(state.encode(), embedded_state.encode())
                ):
                    return _html_error("Invalid or expired sign-in state.")
                state = embedded_state
        try:
            await _complete_sign_in(app, _CLAUDE, "Claude OAuth", code, state)
        except _UnconsumedOAuthCallbackError:
            return _html_error("Invalid or expired sign-in state.")
        except OAuthCallbackError as exc:
            return _html_error(str(exc))
        return HTMLResponse(_CALLBACK_SUCCESS_HTML)

    return router
