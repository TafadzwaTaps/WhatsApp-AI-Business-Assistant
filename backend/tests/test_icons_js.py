"""U4: inline SVG icon references resolve to sprite symbols."""
import os, shutil, subprocess, pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_icon_symbols_resolve():
    r = subprocess.run(["node", os.path.join(HERE, "icons_check.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
