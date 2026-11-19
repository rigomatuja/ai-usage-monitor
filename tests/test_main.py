"""Verify CLI access logging without starting Uvicorn."""

import logging
import sys
from unittest.mock import Mock

import pytest

from ai_usage_monitor import __main__ as cli


@pytest.fixture
def access_logger(monkeypatch):
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "filters", [])
    monkeypatch.setattr(sys, "argv", ["ai-usage-monitor"])
    monkeypatch.setattr(cli, "create_app", Mock())
    run = Mock(side_effect=lambda *args, **kwargs: filters_at_run.extend(logger.filters))
    filters_at_run = []
    monkeypatch.setattr(cli.uvicorn, "run", run)
    cli.main()
    return logger, run, filters_at_run


def test_main_installs_access_log_filter(access_logger):
    _, run, filters_at_run = access_logger
    assert any(isinstance(f, logging.Filter) for f in filters_at_run)
    run.assert_called_once()
    assert run.call_args.kwargs.get("access_log", True)


@pytest.mark.parametrize("target, expected", [
    ("/callback?code=SECRETCODE&state=SECRETSTATE", "/callback?<redacted>"),
    ("/auth/callback?code=SECRETCODE&state=SECRETSTATE", "/auth/callback?<redacted>"),
    ("/api/usage", "/api/usage"),
])
def test_access_log_redacts_query_strings(access_logger, target, expected):
    logger, _, _ = access_logger
    record = logging.LogRecord(
        logger.name, logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", target, "1.1", 200), None,
    )
    assert logger.filter(record)
    message = record.getMessage()
    assert message == f'127.0.0.1:1234 - "GET {expected} HTTP/1.1" 200'
    assert "SECRETCODE" not in message and "SECRETSTATE" not in message
