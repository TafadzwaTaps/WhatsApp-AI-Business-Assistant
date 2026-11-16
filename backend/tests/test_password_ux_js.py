"""Password-change UX helpers (formatApiDetail, validateNewPasswordInput) in static/dashboard.js."""
import os, shutil, subprocess, pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_password_ux_helpers():
    js = os.path.join(HERE, "..", "..", "static", "dashboard.js")
    r = subprocess.run(["node", os.path.join(HERE, "password_ux_check.js"), js],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
