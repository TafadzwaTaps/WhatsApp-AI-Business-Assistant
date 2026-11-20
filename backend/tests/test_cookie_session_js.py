"""Cookie sessions, Stage 2 (client): runs the real static/cookie-session.js in Node against fakes."""
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "..", "..", "static")
PAGES = ["dashboard", "inbox", "signup", "pricing", "onboarding", "multidash"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cookie_session_client_behaviour():
    r = subprocess.run(["node", os.path.join(HERE, "cookie_session_check.js"), os.path.join(STATIC, "cookie-session.js")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr or r.stdout
    assert "cookie session client OK" in r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_script_is_valid_javascript():
    assert subprocess.run(["node", "--check", os.path.join(STATIC, "cookie-session.js")], capture_output=True, timeout=60).returncode == 0


@pytest.mark.parametrize("page", PAGES)
def test_every_api_page_loads_the_script_first(page):
    html = open(os.path.join(STATIC, page + ".html"), encoding="utf8").read()
    assert html.count('src="/static/cookie-session.js"') == 1
    head = html[:html.index("</head>")]
    assert "cookie-session.js" in head
    first_page_script = re.search(r'<script[^>]*src="/static/(?!cookie-session)', html)
    assert (first_page_script is None) or html.index("cookie-session.js") < first_page_script.start()


def test_the_script_is_external_and_never_stores_tokens():
    js = open(os.path.join(STATIC, "cookie-session.js"), encoding="utf8").read()
    assert "eval(" not in js and "innerHTML" not in js and "new Function" not in js
    assert "setItem('wazi_token'" not in js and 'setItem("wazi_token"' not in js


def test_the_server_flag_is_still_off_and_nothing_enables_it():
    import core.cookie_auth as ck
    os.environ.pop("COOKIE_AUTH_ENABLED", None)
    assert ck.enabled() is False


def test_the_staging_runbook_exists_and_states_the_rollback():
    doc = open(os.path.join(HERE, "..", "..", "docs", "ai-development", "COOKIE_AUTH_STAGE2_STAGING_RUNBOOK.md"), encoding="utf8").read()
    for must in ("COOKIE_AUTH_ENABLED", "staging", "Rollback", "X-CSRF-Token", "NOT enabled in production"):
        assert must in doc
