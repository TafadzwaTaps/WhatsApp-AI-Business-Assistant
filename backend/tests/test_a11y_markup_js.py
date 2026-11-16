"""U2b: delegated Enter/Space handler and shared dialog focus helpers (dashboard.js, inbox.js)."""
import os, shutil, subprocess, pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", ["dashboard.js", "inbox.js"])
def test_a11y_helpers(name):
    js = os.path.join(HERE, "..", "..", "static", name)
    r = subprocess.run(["node", os.path.join(HERE, "a11y_markup_check.js"), js],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
