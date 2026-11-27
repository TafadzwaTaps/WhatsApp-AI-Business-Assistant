"""Security audit Stage 4b: the read-only Super Admin Security Center."""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import core.auth as auth
import crud.admin_audit as admin_audit
import crud.security_events as store
import services.security_center as center

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
HERE = pathlib.Path(__file__).resolve().parent


class _DB:
    """Returns the rows set per table and records every query step. `fail` raises for those tables."""

    def __init__(self, tables=None, fail=(), code=None):
        self.tables, self.fail, self.code, self.log = tables or {}, set(fail), code, []

    def table(self, name):
        db = self

        class Query:
            span = None

            def __getattr__(self, step):
                def call(*args, **_):
                    db.log.append((name, step, args))
                    if step == "range":
                        self.span = args
                    return self
                return call

            def execute(self):
                if name in db.fail:
                    error = RuntimeError("secret database detail")
                    error.code = db.code
                    raise error
                rows = db.tables.get(name, [])
                if self.span:
                    rows = rows[self.span[0]:self.span[1] + 1]
                return type("R", (), {"data": rows})()

        return Query()


@pytest.fixture
def use_db(monkeypatch):
    def install(**kwargs):
        db = _DB(**kwargs)
        for module in (store, admin_audit, sys.modules["core.db"]):
            monkeypatch.setattr(module, "supabase", db)
        return db
    return install


@pytest.fixture
def client(monkeypatch):
    from main import app
    monkeypatch.setattr(auth, "is_token_revoked", lambda jti: False)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda bid: (None, True))
    return TestClient(app)


def _bearer(role="superadmin", **extra):
    return {"Authorization": "Bearer " + auth.create_access_token({"sub": "root", "role": role, **extra})}


def _event(event_type, **extra):
    return {"id": 1, "event_type": event_type, "created_at": "2026-11-27T10:00:00+00:00", "metadata": {}, **extra}


PATHS = ["/admin/saas/security/overview", "/admin/saas/security/events", "/admin/saas/audit-logs"]


# ── Access ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PATHS)
def test_only_the_super_admin_may_read(client, use_db, path):
    use_db()
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_bearer("business", business_id=3)).status_code == 403
    assert client.get(path, headers=_bearer("business", business_id=3, impersonated_by="root")).status_code == 403
    assert client.get(path, headers=_bearer()).status_code == 200


def test_the_security_center_api_only_reads():
    from routes.security_center_routes import router
    assert {m for route in router.routes for m in route.methods} == {"GET"}


# ── Overview ─────────────────────────────────────────────────────────────────

def test_overview_counts_stored_events(client, use_db):
    use_db(tables={
        "security_events": [
            _event("cross_tenant_blocked", outcome="blocked", severity="high", occurrence_count=2),
            _event("login_failed", outcome="blocked", severity="low", occurrence_count=5),
            _event("data_export", outcome="allowed", severity="info"),
        ],
        "security_incidents": [{"severity": "critical", "status": "detected"},
                               {"severity": "high", "status": "resolved"}],
        "security_restrictions": [
            {"kind": "ip", "revoked_at": None, "expires_at": "2999-01-01T00:00:00+00:00"},
            {"kind": "ip", "revoked_at": None, "expires_at": "2000-01-01T00:00:00+00:00"},
            {"kind": "ip", "revoked_at": "2026-01-01", "expires_at": "2999-01-01T00:00:00+00:00"},
            {"kind": "account", "revoked_at": None, "expires_at": None}],
        "security_vulnerabilities": [{"status": "open"}, {"status": "resolved"}],
    })
    body = client.get("/admin/saas/security/overview?days=7", headers=_bearer()).json()
    events = body["events"]
    assert events["available"] and events["counts"]["cross_tenant"] == 2 and events["counts"]["failed_logins"] == 5
    assert events["blocked"] == 7 and events["by_severity"]["high"] == 2
    assert [(i["event_type"], i["occurrences"]) for i in events["to_review"]] == [("cross_tenant_blocked", 2)]
    assert body["open_incidents"] == {"total": 1, "critical": 1, "high": 0}
    assert body["active_ip_restrictions"] == 1 and body["open_vulnerabilities"] == 1
    assert body["monitoring"]["last_security_scan"] is None
    assert "score" not in str(body).lower()


def test_unreadable_data_is_unavailable_not_zero(client, use_db):
    use_db(fail=("security_events", "security_incidents", "security_restrictions", "security_vulnerabilities"))
    response = client.get("/admin/saas/security/overview", headers=_bearer())
    body = response.json()
    assert response.status_code == 200 and body["events"]["available"] is False
    assert body["events"]["counts"] is None and body["events"]["blocked"] is None
    assert body["open_incidents"] is body["active_ip_restrictions"] is body["open_vulnerabilities"] is None
    assert "secret database detail" not in response.text


@pytest.mark.parametrize("days", [0, 91])
def test_overview_period_is_bounded(client, use_db, days):
    use_db()
    assert client.get(f"/admin/saas/security/overview?days={days}", headers=_bearer()).status_code == 422


def test_every_event_type_has_an_explanation():
    assert set(center._CATALOG) == set(store.EVENT_TYPES)


def test_rows_without_a_classification_use_the_default_severity():
    plain = center.describe(_event("refresh_token_reuse", severity="info"))        # column default
    stored = center.describe(_event("login_failed", severity="medium", outcome="blocked"))
    assert (plain["severity"], plain["outcome"], stored["severity"]) == ("high", "blocked", "medium")
    unknown = center.describe(_event("something_new"))
    assert unknown["severity"] == "info" and unknown["why"]


def test_details_are_redacted_and_fallback_fields_read():
    shown = center.describe(_event("admin_action", metadata={"token": "abc", "endpoint": "/x", "rule": "r"}))
    assert shown["details"]["token"] == "[redacted]" and (shown["endpoint"], shown["rule"]) == ("/x", "r")


# ── Event explorer ───────────────────────────────────────────────────────────

def test_filters_reach_the_query_and_pages_are_reported(client, use_db):
    db = use_db(tables={"security_events": [_event("login_failed", id=n) for n in range(30)]})
    url = ("/admin/saas/security/events?severity=low&event_type=login_failed&business_id=4&username=a_b%25"
           "&endpoint=/auth&ip=1.2.3.4&incident_id=2&date_from=2026-11-01&date_to=2026-11-02&q=shop&page_size=10")
    body = client.get(url, headers=_bearer()).json()
    assert body["available"] and body["has_more"] and len(body["events"]) == 10
    steps = {(step, args) for _, step, args in db.log}
    for expected in (("eq", ("severity", "low")), ("eq", ("event_type", "login_failed")), ("eq", ("business_id", 4)),
                     ("eq", ("ip", "1.2.3.4")), ("eq", ("incident_id", 2)), ("gte", ("created_at", "2026-11-01")),
                     ("lt", ("created_at", "2026-11-03")), ("ilike", ("username", "%a\\_b\\%%")),
                     ("ilike", ("endpoint", "%/auth%")), ("range", (0, 10))):
        assert expected in steps, expected
    assert ("or_", ('event_type.ilike."*shop*",username.ilike."*shop*",ip.ilike."*shop*"',)) in steps
    last = client.get("/admin/saas/security/events?page=3&page_size=10", headers=_bearer()).json()
    assert len(last["events"]) == 10 and last["has_more"] is False


@pytest.mark.parametrize("query", [
    "q=a,ip.eq.1", "q=x)", 'q="', "q=a*", "q=%25", "q=a%5Cb", "severity=urgent", "page=0", "page_size=500",
    "business_id=abc", "date_from=yesterday", "date_from=2026-11-03&date_to=2026-11-01"])
def test_unsafe_or_invalid_filters_are_refused(client, use_db, query):
    db = use_db()
    assert client.get(f"/admin/saas/security/events?{query}", headers=_bearer()).status_code == 422
    assert not db.log


@pytest.mark.parametrize("code,expected", [("42703", "database change"), (None, "could not be read")])
def test_a_failed_query_says_so_without_leaking_it(client, use_db, code, expected):
    use_db(fail=("security_events",), code=code)
    response = client.get("/admin/saas/security/events?severity=high", headers=_bearer())
    body = response.json()
    assert response.status_code == 200 and body["available"] is False and body["events"] == []
    assert expected in body["problem"] and "secret database detail" not in response.text


# ── Audit trail ──────────────────────────────────────────────────────────────

def test_audit_trail_filters_and_pages(client, use_db):
    db = use_db(tables={"admin_audit_logs": [{"id": n, "action": "x"} for n in range(6)]})
    body = client.get("/admin/saas/audit-logs?limit=5&actor=root&action=x&business_id=2"
                      "&date_from=2026-11-01&date_to=2026-11-01", headers=_bearer()).json()
    assert len(body["logs"]) == 5 and body["has_more"] is True
    steps = {(step, args) for _, step, args in db.log}
    for expected in (("eq", ("actor_username", "root")), ("eq", ("action", "x")), ("eq", ("business_id", 2)),
                     ("gte", ("created_at", "2026-11-01")), ("lt", ("created_at", "2026-11-02")),
                     ("range", (0, 5))):
        assert expected in steps, expected
    second = client.get("/admin/saas/audit-logs?limit=5&offset=5", headers=_bearer()).json()
    assert len(second["logs"]) == 1 and second["has_more"] is False


@pytest.mark.parametrize("path", ["/admin/saas/security/events", "/admin/saas/audit-logs"])
def test_the_last_calendar_day_is_accepted(client, use_db, path):
    use_db()
    assert client.get(f"{path}?date_to=9999-12-31", headers=_bearer()).status_code == 200


def test_audit_page_size_is_bounded(client, use_db):
    db = use_db()
    client.get("/admin/saas/audit-logs?limit=100000&offset=-4", headers=_bearer())
    assert ("admin_audit_logs", "range", (0, 500)) in db.log


def test_event_export_includes_the_new_columns():
    from services.data_export import EXPORT_DATASETS
    assert {"severity", "outcome", "occurrence_count"} <= set(EXPORT_DATASETS["security_events"]["fields"])


# ── Dashboard ────────────────────────────────────────────────────────────────

def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_section_is_wired_into_the_dashboard():
    html, dashboard, script = _read("dashboard.html"), _read("dashboard.js"), _read("security_center.js")
    assert 'id="section-admin-security"' in html and '<script src="/static/security_center.js"></script>' in html
    assert "showSection('admin-security',this)" in dashboard and "loadSecurityCenter()" in dashboard
    wanted = set(re.findall(r"(?:byId|value|on)\('(sc-[a-z-]+)'", script))
    wanted |= {f"{p}-{part}" for p in ("sc-events", "sc-audit") for part in ("page", "prev", "next")}
    missing = sorted(i for i in wanted if f'id="{i}"' not in html)
    assert wanted and not missing, missing
    section = html.split('id="section-admin-security"')[1].split("SUPERADMIN PLATFORM CONTROLS")[0]
    assert not re.search(r"\son[a-z]+\s*=", section) and not re.search(r"\son[a-z]+\s*=\s*[\"']", script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_script_escapes_data_and_never_shows_unknown_as_zero():
    result = subprocess.run(["node", str(HERE / "security_center_check.js"), str(STATIC / "security_center.js")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "security center OK" in result.stdout
