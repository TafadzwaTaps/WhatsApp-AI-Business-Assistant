"""jsArg(): values placed in inline handler JS strings round-trip and cannot break out."""
import os, shutil, subprocess, pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_jsarg_roundtrip_and_no_quoted_template_args():
    st = os.path.join(HERE, "..", "..", "static")
    r = subprocess.run(["node", os.path.join(HERE, "jsarg_check.js"),
                        os.path.join(st, "dashboard.js"), os.path.join(st, "inbox.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
