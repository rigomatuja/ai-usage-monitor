"""Structural contract tests for the static dashboard assets (T6, S4).

These tests pin the REST contract and the required element hooks. They are
deliberately structural (no browser): they assert the assets exist, expose the
hooks the JavaScript needs, reference each REST path exactly as in the T5/T6
contract, and contain no external/CDN dependencies (must work fully offline).
They do not pin styling details.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ai_usage_monitor"
    / "web"
    / "static"
)


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles_css() -> str:
    return (STATIC_DIR / "styles.css").read_text(encoding="utf-8")


# --- index.html: required element ids / hooks -------------------------------


def test_static_assets_exist():
    for name in ("index.html", "app.js", "styles.css"):
        assert (STATIC_DIR / name).is_file(), f"missing static asset: {name}"


@pytest.mark.parametrize(
    "dom_id",
    [
        "cards",  # per-account card container
        "refresh-btn",  # global Refresh button
        "add-account-form",  # Add account form
        "provider",  # provider select (4 provider kinds)
        "account-label",  # label text input
        "credentials",  # credentials textarea
        "add-account-error",  # inline server error area for the form
        "connection-banner",  # non-blocking "connection lost" banner
    ],
)
def test_index_html_has_required_hooks(index_html: str, dom_id: str):
    assert f'id="{dom_id}"' in index_html, f"index.html missing id={dom_id!r}"


def test_index_html_loads_local_assets_only(index_html: str):
    assert 'src="app.js"' in index_html
    assert 'href="styles.css"' in index_html


def test_index_html_documents_credential_keys(index_html: str):
    # Small help text documenting which credentials key each provider expects.
    for key in ("credentials_path", "auth_path", "api_key"):
        assert key in index_html, f"credentials help text missing {key!r}"


# --- app.js: REST contract ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/accounts",
        "/api/usage",
        "/api/usage/refresh/",
    ],
)
def test_app_js_references_rest_paths(app_js: str, path: str):
    assert path in app_js, f"app.js does not reference {path!r}"


def test_app_js_handles_delete_for_accounts(app_js: str):
    assert "DELETE" in app_js
    assert "/api/accounts/" in app_js


def test_app_js_posts_account_json_body(app_js: str):
    assert "POST" in app_js
    assert "credentials" in app_js
    assert "provider" in app_js
    assert "label" in app_js


def test_app_js_polls_usage(app_js: str):
    # Auto-refresh: usage is polled on an interval (60s per spec).
    assert "/api/usage" in app_js
    assert re.search(r"setInterval|setTimeout", app_js), (
        "app.js should schedule a polling timer"
    )


def test_app_js_never_renders_credentials(app_js: str):
    # The word "credentials" may only appear in request-parsing/submission
    # contexts, never in DOM-writing calls (textContent/innerHTML/insertAdjacent...).
    writing_calls = re.findall(
        r"\.(?:textContent|innerHTML|outerHTML|insertAdjacentHTML)\s*=[^;]*", app_js
    ) + re.findall(r"\.insertAdjacentHTML\([^)]*\)", app_js)
    for call in writing_calls:
        assert "credentials" not in call, (
            f"credentials must never be rendered to the page: {call!r}"
        )


# --- index.html: OAuth sign-in hooks (T4, S1/S2) -----------------------------


@pytest.mark.parametrize(
    "dom_id",
    [
        "signin-claude",  # "Sign in with Claude" button
        "signin-chatgpt",  # "Sign in with ChatGPT" button
        "oauth-waiting",  # inline "Waiting for login to complete…" status line
        "oauth-error",  # inline server error area for the OAuth login
    ],
)
def test_index_html_has_oauth_hooks(index_html: str, dom_id: str):
    assert f'id="{dom_id}"' in index_html, f"index.html missing id={dom_id!r}"


def test_index_html_has_oauth_button_labels(index_html: str):
    assert "Sign in with Claude" in index_html
    assert "Sign in with ChatGPT" in index_html


def test_oauth_signin_section_precedes_add_account(index_html: str):
    # The sign-in section sits directly above the manual "Add account" form.
    assert index_html.index("signin-claude") < index_html.index("add-account-form")


# --- app.js: OAuth REST contract (T4) ----------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/oauth/claude_personal/login",
        "/api/oauth/chatgpt_personal/login",
    ],
)
def test_app_js_references_oauth_login_paths(app_js: str, path: str):
    assert path in app_js, f"app.js does not reference {path!r}"


def test_app_js_opens_returned_authorize_url(app_js: str):
    # The authorize_url returned by the login endpoint is opened in a new
    # tab/window; the server owns the absolute URL, app.js never hardcodes one.
    assert "window.open" in app_js
    assert "authorize_url" in app_js


def test_app_js_posts_oauth_login(app_js: str):
    assert re.search(r"method:\s*\"POST\"", app_js), (
        "app.js should POST to the oauth login endpoints"
    )


def test_app_js_shows_waiting_status_and_clears_it(app_js: str):
    assert "Waiting for login to complete" in app_js
    # the waiting line is tied to both sign-in buttons and cleared via the
    # accounts re-fetch path (any successful /api/accounts fetch clears it)
    assert "signin-claude" in app_js
    assert "signin-chatgpt" in app_js
    assert "oauth-waiting" in app_js
    assert "oauth-error" in app_js


# --- styles.css: progress-bar classes ---------------------------------------


def test_styles_css_defines_progress_bar_classes(styles_css: str):
    for selector in (".progress-bar", ".progress-fill"):
        assert selector in styles_css, f"styles.css missing {selector!r}"


def test_styles_css_defines_threshold_fill_classes(styles_css: str):
    # Colored fill by used_percent thresholds: green < 50, amber 50-79, red >= 80.
    for cls in ("green", "amber", "red"):
        assert f".progress-fill.{cls}" in styles_css, (
            f"styles.css missing .progress-fill.{cls}"
        )


def test_styles_css_defines_stat_line_and_stale(styles_css: str):
    # Windows without a percent render as a plain stat line; error snapshots
    # keep last-known windows dimmed with a "stale" tag.
    assert ".stat-line" in styles_css
    assert ".stale" in styles_css


# --- offline requirement: no CDN / external URLs ----------------------------


def test_no_external_urls_in_any_asset(index_html: str, app_js: str, styles_css: str):
    for name, content in (
        ("index.html", index_html),
        ("app.js", app_js),
        ("styles.css", styles_css),
    ):
        matches = re.findall(r"https?://[^\s\"'<>)]+", content)
        assert not matches, f"{name} contains external URLs: {matches}"
        assert "cdn" not in content.lower(), f"{name} mentions a CDN"
