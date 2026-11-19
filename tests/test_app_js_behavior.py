"""Exercise the dashboard's OAuth waiting status without a browser or network."""

import json
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
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the dashboard behavior harness")

    result = subprocess.run(
        [node, str(ROOT / "tests/js/oauth_fallback_harness.js"),
         str(ROOT / "src/ai_usage_monitor/web/static/app.js"), scenario],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"scenario": scenario, "passed": True}
