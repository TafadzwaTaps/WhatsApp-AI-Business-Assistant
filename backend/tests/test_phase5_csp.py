"""Phase 5 CSP migration, step 1: report-only policy, report endpoint, delegated actions, pilot pages."""
import os
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "..", "..", "static")
PILOT = {"store": "store.js", "forgot-password": "forgot-password.js", "reset-password": "reset-password.js"}


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _read(name):
    return open(os.path.join(STATIC, name), encoding="utf8").read()


# -- headers -------------------------------------------------------------------------------------
def test_enforced_policy_is_unchanged_and_report_only_is_strict(client):
    r = client.get("/static/reset-password.html")
    enforced = r.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline';" in enforced              # still enforced as before
    ro = r.headers["content-security-policy-report-only"]
    assert "script-src 'self';" in ro and "script-src-attr 'none'" in ro and "report-uri /csp-report" in ro
    assert "'unsafe-inline'" in ro.split("style-src")[1].split(";")[0]    # style-src deliberately unchanged
    assert "script-src 'self' 'unsafe-inline'" not in ro


def test_report_only_header_is_sent_on_html_only(client):
    assert "content-security-policy-report-only" not in client.get("/healthz").headers


def test_report_only_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("CSP_REPORT_ONLY", "0")
    assert "content-security-policy-report-only" not in client.get("/static/reset-password.html").headers


# -- report endpoint -----------------------------------------------------------------------------
@pytest.fixture
def reports(monkeypatch):
    import routes.csp_routes as cr
    monkeypatch.setattr(cr, "_counts", {})
    return cr


def _legacy(blocked="inline", page="https://wazibothq.com/static/x.html?token=SECRET#frag", directive="script-src-elem"):
    return {"csp-report": {"document-uri": page, "effective-directive": directive, "blocked-uri": blocked,
                           "source-file": "https://wazibothq.com/static/x.html?a=1", "line-number": 12}}


def test_report_is_accepted_counted_and_stripped_of_query_strings(client, reports):
    for _ in range(3):
        assert client.post("/csp-report", json=_legacy()).status_code == 204
    (key, row), = reports._counts.items()
    assert row[0] == 3
    assert "SECRET" not in repr(key) and "token" not in repr(key) and "frag" not in repr(key)
    assert key[0] == "https://wazibothq.com/static/x.html"


def test_new_reporting_api_format_is_understood(client, reports):
    body = [{"type": "csp-violation", "body": {"documentURL": "https://wazibothq.com/a?x=1", "effectiveDirective": "script-src-attr",
                                                "blockedURL": "inline", "sourceFile": "https://wazibothq.com/static/d.js", "lineNumber": 7}}]
    assert client.post("/csp-report", content=__import__("json").dumps(body), headers={"content-type": "application/reports+json"}).status_code == 204
    assert list(reports._counts)[0][1] == "script-src-attr"


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b"{}", b'{"csp-report": 5}', b"null", b'{"a":' + b"1" * 20000 + b"}"])
def test_garbage_and_oversized_reports_never_error(client, reports, payload):
    assert client.post("/csp-report", content=payload, headers={"content-type": "application/csp-report"}).status_code == 204
    assert reports._counts == {}


def test_data_and_javascript_urls_are_reduced_to_their_scheme(client, reports):
    client.post("/csp-report", json=_legacy(blocked="data:text/html;base64,AAAA" + "A" * 200))
    client.post("/csp-report", json=_legacy(blocked="javascript:alert(document.cookie)"))
    blocked = sorted(k[2] for k in reports._counts)
    assert blocked == ["data:", "javascript:"]


def test_memory_is_bounded(client, reports, monkeypatch):
    monkeypatch.setattr(reports, "_MAX_KEYS", 5)
    for i in range(20):
        client.post("/csp-report", json=_legacy(page=f"https://wazibothq.com/p{i}"))
    assert len(reports._counts) <= 5


def test_report_endpoint_is_rate_limited_silently(client, reports, monkeypatch):
    import routes.csp_routes as cr
    from services.security import RateLimitExceeded
    monkeypatch.setattr(cr, "rate_limit", lambda *a, **k: (_ for _ in ()).throw(RateLimitExceeded("csp_report", retry_after=60)))
    assert client.post("/csp-report", json=_legacy()).status_code == 204
    assert reports._counts == {}


def test_summary_is_superadmin_only(client, reports):
    import core.auth as auth
    client.post("/csp-report", json=_legacy())
    assert client.get("/admin/csp-reports").status_code == 401
    biz = {"Authorization": "Bearer " + auth.create_access_token({"sub": "u", "role": "business", "business_id": 5})}
    assert client.get("/admin/csp-reports", headers=biz).status_code in (401, 403)
    root = {"Authorization": "Bearer " + auth.create_access_token({"sub": "root", "role": "superadmin"})}
    r = client.get("/admin/csp-reports", headers=root)
    assert r.status_code == 200 and r.json()["distinct"] == 1 and r.json()["reports"][0]["count"] == 1


# -- the delegated dispatcher --------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dispatcher_behaviour_in_node():
    r = subprocess.run(["node", os.path.join(HERE, "phase5_csp_check.js"), os.path.join(STATIC, "csp-actions.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
    assert "csp actions OK" in r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", ["csp-actions.js", "store.js", "forgot-password.js", "reset-password.js"])
def test_new_scripts_are_valid(name):
    assert subprocess.run(["node", "--check", os.path.join(STATIC, name)], capture_output=True, timeout=60).returncode == 0


# -- migrated pages ------------------------------------------------------------------------------
@pytest.mark.parametrize("page,js", list(PILOT.items()))
def test_migrated_pages_have_no_inline_script_or_handlers(page, js):
    html = _read(page + ".html")
    assert not re.search(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/(ld\+)?json\")[^>]*>", html), "inline <script> left"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline handler left"
    assert "javascript:" not in html
    assert f'src="/static/{js}"' in html and os.path.exists(os.path.join(STATIC, js))


@pytest.mark.parametrize("page,js", [("reset-password", "reset-password.js"), ("forgot-password", "forgot-password.js")])
def test_every_action_in_the_markup_is_registered(page, js):
    html, code = _read(page + ".html"), _read(js)
    used = set(re.findall(r'data-(?:click|input|change|keydown|submit)="([^"]+)"', html))
    registered = set(re.findall(r"wzActions\.register\('([^']+)'", code))
    assert used and used <= registered, used - registered
    assert 'src="/static/csp-actions.js"' in html
    assert html.index("csp-actions.js") < html.index(f"/static/{js}")          # dispatcher loads first


def test_reset_page_markup_arguments_replaced_inline_strings():
    html = _read("reset-password.html")
    assert 'data-target="new-pass" data-eye="eye1"' in html and 'data-target="confirm-pass" data-eye="eye2"' in html
    assert "toggleEye(el.dataset.target, el.dataset.eye)" in _read("reset-password.js")


def test_page_behaviour_code_was_moved_not_rewritten():
    js = _read("reset-password.js")
    for must in ("validate-reset-token", "/auth/reset-password", "function onPasswordInput", "TextEncoder().encode(pw).length <= 72"):
        assert must in js
    assert "/api/store/" in _read("store.js") or "fetch(" in _read("store.js")


def test_reports_cannot_forge_log_lines(client, reports, caplog):
    import logging
    nl = chr(13) + chr(10)
    forged = 'https://wazibothq.com/a' + nl + 'INFO wazibot.auth: login ok admin'
    with caplog.at_level(logging.INFO, logger='wazibot.csp'):
        client.post('/csp-report', json=_legacy(blocked=forged, directive='script-src' + nl + 'FAKE'))
    assert caplog.records
    assert all(chr(10) not in r.getMessage() and chr(13) not in r.getMessage() for r in caplog.records)
    for key in reports._counts:
        assert all(chr(10) not in part and chr(13) not in part for part in key)
    assert len(reports._counts) == 1


def test_oversized_body_is_refused_from_the_content_length_header(client, reports, monkeypatch):
    seen = {"read": False}
    from starlette.requests import Request
    orig = Request.body

    async def spy(self):
        seen["read"] = True
        return await orig(self)
    monkeypatch.setattr(Request, "body", spy)
    r = client.post("/csp-report", content=b"x" * 20000, headers={"content-type": "application/csp-report"})
    assert r.status_code == 204 and seen["read"] is False
