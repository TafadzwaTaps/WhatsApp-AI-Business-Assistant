"""Phase 5 frontend reliability: runs the real dashboard.js / inbox.js functions in Node against fakes
(shared token refresh, request timeout and parsing, clipboard, WebSocket backoff/resync, inbox refresh)."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "..", "..", "static")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_phase5_frontend_behaviour():
    r = subprocess.run(["node", os.path.join(HERE, "phase5_check.js"),
                        os.path.join(STATIC, "dashboard.js"), os.path.join(STATIC, "inbox.js")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr or r.stdout
    assert "phase5 frontend OK" in r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", ["dashboard.js", "inbox.js", "verify-email.js"])
def test_scripts_are_syntactically_valid(name):
    r = subprocess.run(["node", "--check", os.path.join(STATIC, name)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
