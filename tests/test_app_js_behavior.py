"""Exercise dashboard behavior without a browser or network."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", [
    "blocked", "unchanged", "unrelated", "auto", "replacement", "dismiss",
    "restart", "unblocked", "same-provider", "failed", "expired",
])
def test_oauth_waiting_status(scenario):
    run_harness(scenario)


@pytest.mark.parametrize("scenario", [
    "rename", "clear", "reset-default", "rename-error", "add-blank", "add-named",
])
def test_account_names(scenario):
    run_harness(scenario)


@pytest.mark.parametrize("scenario", ["remove", "remove-cancel", "hide-auto", "restore", "restore-error"])
def test_account_removal(scenario):
    run_harness(scenario)


@pytest.mark.parametrize("scenario", ["dates", "no-reset", "error-date"])
def test_reset_dates(scenario):
    run_harness(scenario)


def run_harness(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the dashboard behavior harness")

    result = subprocess.run(
        [node, str(ROOT / "tests/js/oauth_fallback_harness.js"),
         str(ROOT / "src/ai_usage_monitor/web/static/app.js"), scenario],
        capture_output=True, text=True, timeout=5,
        env={**os.environ, "TZ": "Europe/Madrid", "LC_ALL": "en_GB.UTF-8"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"scenario": scenario, "passed": True}
