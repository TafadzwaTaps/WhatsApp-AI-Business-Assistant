"""Inbox triage helpers (classify/filter/count) behave correctly."""
import os, shutil, subprocess, pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inbox_filters_logic():
    js = os.path.join(HERE, "..", "..", "static", "inbox.js")
    r = subprocess.run(["node", os.path.join(HERE, "inbox_filters_check.js"), js],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
