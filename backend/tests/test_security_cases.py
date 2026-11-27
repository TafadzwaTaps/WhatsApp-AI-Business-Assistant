"""Security audit Stage 4c: incidents and the vulnerability register."""
from __future__ import annotations

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

STATIC = pathlib.Path(__file__).resolve().parents[2] / "static"
HERE = pathlib.Path(__file__).resolve().parent
BASE = "/admin/saas/security"


class _DB:
    """A small in-memory database: enough filtering to test the real behaviour of each query."""

    DEFAULTS = {"security_incidents": {"resolved_at": None, "verification_evidence": None},
                "security_vulnerabilities": {"evidence": None, "last_verified_at": None}}

    def __init__(self):
        self.tables, self.fail, self.code, self.writes = {}, set(), None, []

    def rows(self, name):
        return self.tables.setdefault(name, [])

    def table(self, name):
        db = self

        class Query:
            def __init__(self):
                self.tests, self.op, self.payload, self.span = [], "select", None, None

            def select(self, *a, **k):
                return self

            def insert(self, row):
                self.op, self.payload = "insert", row
                return self

            def update(self, row):
                self.op, self.payload = "update", row
                return self

            def delete(self):
                raise AssertionError("nothing in the Security Center may delete")

            def eq(self, column, value):
                self.tests.append(lambda r: r.get(column) == value)
                return self

            def in_(self, column, values):
                self.tests.append(lambda r: r.get(column) in values)
                return self

            def is_(self, column, value):
                assert value == "null"
                self.tests.append(lambda r: r.get(column) is None)
                return self

            def range(self, start, end):
                self.span = (start, end)
                return self

            def limit(self, count):
                self.span = (0, count - 1)
                return self

            def order(self, *a, **k):
                return self

            def execute(self):
                if name in db.fail or (name, self.op) in db.fail:
                    error = RuntimeError("secret database detail")
                    error.code = db.code
                    raise error
                table = db.rows(name)
                if self.op == "insert":
                    row = {**db.DEFAULTS.get(name, {}), "id": len(table) + 1, **self.payload}
                    table.append(row)
                    db.writes.append((name, "insert"))
                    return type("R", (), {"data": [dict(row)]})()
                found = [r for r in table if all(t(r) for t in self.tests)]
                if self.op == "update":
                    for r in found:
                        r.update(self.payload)
                    db.writes.append((name, "update"))
                if self.span:
                    found = found[self.span[0]:self.span[1] + 1]
                return type("R", (), {"data": [dict(r) for r in found]})()

        return Query()


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    for module in (store, admin_audit, sys.modules["core.db"]):
        monkeypatch.setattr(module, "supabase", fake)
    return fake


@pytest.fixture
def api(monkeypatch):
    from main import app
    import services.security as sec
    monkeypatch.setattr(auth, "is_token_revoked", lambda jti: False)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda bid: (None, True))
    sec._rate_store.clear()
    client = TestClient(app)
    client.headers.update(_bearer())
    yield client
    sec._rate_store.clear()


def _bearer(role="superadmin", **extra):
    return {"Authorization": "Bearer " + auth.create_access_token({"sub": "root", "role": role, **extra})}


def _incident(api, **extra):
    r = api.post(f"{BASE}/incidents", json={"title": "Odd sign-ins", "severity": "high", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _move(api, incident_id, status, **extra):
    return api.post(f"{BASE}/incidents/{incident_id}/status", json={"status": status, "note": "because", **extra})


def _audit(db, action):
    return [r for r in db.rows("admin_audit_logs") if r["action"] == action]


# ── Access ───────────────────────────────────────────────────────────────────

def _case_routes():
    from routes.security_cases_routes import router
    return [(m, re.sub(r"\{[^}]+\}", "1", r.path)) for r in router.routes for m in r.methods]


@pytest.mark.parametrize("method,path", _case_routes())
def test_every_route_is_super_admin_only(api, db, method, path):
    for headers in ({"Authorization": ""}, _bearer("business", business_id=3),
                    _bearer("business", business_id=3, impersonated_by="root")):
        assert api.request(method, path, headers=headers, json={}).status_code in (401, 403)
    assert not db.writes or set(db.writes) <= {("admin_audit_logs", "insert")}


def test_there_is_no_delete():
    assert {m for m, _ in _case_routes()} == {"GET", "POST", "PATCH"}


# ── Incidents ────────────────────────────────────────────────────────────────

def test_create_records_who_and_writes_timeline_and_audit(api, db):
    body = _incident(api, affected_business_ids=[3, 3, 1], affected_accounts=[" shop ", "", "shop"])
    incident = body["incident"]
    assert (incident["status"], incident["created_by"], incident["classification"]) == ("detected", "root", "suspicious")
    assert incident["affected_business_ids"] == [1, 3] and incident["affected_accounts"] == ["shop"]
    assert body["warnings"] == [] and len(db.rows("security_incident_notes")) == 1
    audit, = _audit(db, "incident.create")
    assert (audit["target_type"], audit["target_id"]) == ("security_incident", "1")


@pytest.mark.parametrize("bad", [
    {"title": "  "}, {"severity": "urgent"}, {"classification": "breach"}, {"affected_business_ids": [0]},
    {"title": "x" * 201}, {"status": "resolved"}, {"created_by": "someone"}, {"affected_accounts": ["x" * 121]}])
def test_invalid_or_forged_fields_are_refused(api, db, bad):
    r = api.post(f"{BASE}/incidents", json={"title": "t", "severity": "low", **bad})
    assert r.status_code == 422 and not db.rows("security_incidents")


def test_events_are_linked_once_and_never_moved(api, db):
    db.rows("security_events").extend([{"id": n, "event_type": "login_failed", "incident_id": None, "metadata": {}}
                                       for n in (10, 11)])
    first = _incident(api, event_ids=[10])
    assert first["linked"] == 1 and not first["warnings"]
    second = _incident(api, event_ids=[10, 11, 999])
    assert second["linked"] == 1 and "not linked" in second["warnings"][0]
    assert [e["incident_id"] for e in db.rows("security_events")] == [1, 2]
    detail = api.get(f"{BASE}/incidents/1").json()
    assert [e["id"] for e in detail["events"]] == [10] and detail["events"][0]["why"]


def test_unlinking_needs_a_reason_and_keeps_the_event(api, db):
    db.rows("security_events").append({"id": 5, "event_type": "login_failed", "incident_id": None, "metadata": {}})
    _incident(api, event_ids=[5])
    url = f"{BASE}/incidents/1/events/5/unlink"
    assert api.post(url, json={"reason": " "}).status_code == 422
    assert api.post(url, json={"reason": "wrong incident"}).status_code == 200
    assert db.rows("security_events") == [{"id": 5, "event_type": "login_failed", "incident_id": None, "metadata": {}}]
    assert api.post(url, json={"reason": "again"}).status_code == 404
    assert _audit(db, "incident.unlink_events")[0]["reason"] == "wrong incident"


def test_resolution_needs_verification_step_and_evidence(api, db):
    _incident(api)
    assert _move(api, 1, "resolved", verification_evidence="fixed").status_code == 409     # skips steps
    assert _move(api, 1, "under_investigation").status_code == 200
    assert _move(api, 1, "awaiting_verification").status_code == 200
    assert _move(api, 1, "resolved").status_code == 422
    assert _move(api, 1, "resolved", verification_evidence="   ").status_code == 422
    done = _move(api, 1, "resolved", verification_evidence="retested, blocked")
    assert done.status_code == 200
    incident = done.json()["incident"]
    assert incident["resolved_at"] and incident["verification_evidence"] == "retested, blocked"
    assert api.get(f"{BASE}/incidents/1").json()["allowed_statuses"] == ["under_investigation"]


def test_reopening_clears_the_resolution_but_keeps_history(api, db):
    _incident(api)
    for status, extra in (("under_investigation", {}), ("awaiting_verification", {}),
                          ("resolved", {"verification_evidence": "first fix"}), ("under_investigation", {})):
        assert _move(api, 1, status, **extra).status_code == 200
    incident = db.rows("security_incidents")[0]
    assert incident["resolved_at"] is None and incident["status"] == "under_investigation"
    assert _audit(db, "incident.status")[-1]["metadata"]["previous_evidence"] == "first fix"
    assert any("first fix" in n["body"] for n in db.rows("security_incident_notes"))


def test_status_change_needs_a_note_and_a_known_incident(api, db):
    _incident(api)
    r = api.post(f"{BASE}/incidents/1/status", json={"status": "contained", "note": ""})
    assert r.status_code == 422
    assert _move(api, 99, "contained").status_code == 404


def test_details_change_records_before_and_after(api, db):
    _incident(api, summary="old")
    assert api.patch(f"{BASE}/incidents/1", json={"severity": "low"}).status_code == 422      # no reason
    r = api.patch(f"{BASE}/incidents/1", json={"severity": "low", "summary": "", "title": "Odd sign-ins",
                                               "reason": "triaged"})
    assert r.status_code == 200
    audit, = _audit(db, "incident.update")
    assert audit["metadata"] == {"before": {"summary": "old", "severity": "high"},
                                 "after": {"summary": None, "severity": "low"}}
    assert api.patch(f"{BASE}/incidents/1", json={"severity": "low", "reason": "x"}).status_code == 422


def test_a_concurrent_edit_is_refused(api, db, monkeypatch):
    import services.security_cases as cases
    _incident(api)
    real = cases._one

    edits = []

    def stale(*args):                    # someone else saves right after we read
        row = real(*args)
        edits.append(1)
        db.rows("security_incidents")[0].update(status="contained", updated_at=f"later-{len(edits)}")
        return row

    monkeypatch.setattr(cases, "_one", stale)
    assert _move(api, 1, "under_investigation").status_code == 409
    assert api.patch(f"{BASE}/incidents/1", json={"severity": "low", "reason": "r"}).status_code == 409
    assert db.rows("security_incidents")[0]["severity"] == "high"


def test_notes_are_added_and_missing_note_storage_is_reported(api, db):
    _incident(api)
    assert api.post(f"{BASE}/incidents/1/notes", json={"kind": "containment", "body": "blocked"}).status_code == 201
    assert api.post(f"{BASE}/incidents/1/notes", json={"kind": "status_change", "body": "x"}).status_code == 422
    db.fail.add(("security_incident_notes", "insert"))
    r = api.post(f"{BASE}/incidents/1/notes", json={"body": "lost"})
    assert r.status_code == 503 and "secret database detail" not in r.text
    moved = _move(api, 1, "contained")
    assert moved.status_code == 200 and moved.json()["warnings"]          # the change stands, and says so


@pytest.mark.parametrize("code,expected", [("42P01", "database change"), (None, "Could not")])
def test_missing_tables_and_failures_are_explained_safely(api, db, code, expected):
    db.fail, db.code = {"security_incidents", "security_vulnerabilities"}, code
    for kind in ("incidents", "vulnerabilities"):
        listing = api.get(f"{BASE}/{kind}")
        assert listing.status_code == 200 and listing.json()["available"] is False
        assert expected in listing.json()["problem"] and "secret database detail" not in listing.text
    r = api.post(f"{BASE}/incidents", json={"title": "t", "severity": "low"})
    assert r.status_code == 503 and expected in r.json()["detail"] and not _audit(db, "incident.create")


# ── Vulnerabilities ──────────────────────────────────────────────────────────

def _finding(api, **extra):
    return api.post(f"{BASE}/vulnerabilities", json={"title": "Old library", "severity": "medium",
                                                     "component": "backend", "source": "dependency_audit", **extra})


def _change(api, **body):
    return api.patch(f"{BASE}/vulnerabilities/1", json={"reason": "review", **body})


def test_findings_are_created_open(api, db):
    r = _finding(api, cve="CVE-2026-12345", owner="")
    assert r.status_code == 201 and r.json()["vulnerability"]["status"] == "open"
    assert "owner" not in db.rows("security_vulnerabilities")[0] and _audit(db, "vulnerability.create")
    assert _finding(api, cve="CVE-bad").status_code == 422
    assert _finding(api, status="resolved").status_code == 422
    db.fail.add(("security_vulnerabilities", "insert"))
    db.code = "23505"
    assert _finding(api, external_ref="a").status_code == 409


@pytest.mark.parametrize("status", ["mitigated", "resolved", "accepted_risk"])
def test_closing_a_finding_needs_evidence(api, db, status):
    _finding(api)
    assert _change(api, status=status).status_code == 422
    assert _change(api, status=status, evidence="  ").status_code == 422
    r = _change(api, status=status, evidence="upgraded and rescanned")
    assert r.status_code == 200
    verified = r.json()["vulnerability"]["last_verified_at"]
    assert (verified is None) == (status == "accepted_risk")


def test_old_evidence_cannot_close_a_reopened_finding_or_be_erased(api, db):
    _finding(api)
    assert _change(api, status="resolved", evidence="first").status_code == 200
    assert _change(api, evidence="").status_code == 422                                # still closed
    assert _change(api, status="open").status_code == 200
    assert _change(api, status="resolved").status_code == 422                          # needs new evidence
    assert _change(api, status="resolved", evidence="first").status_code == 200        # stated again: accepted
    assert [a["metadata"]["after"]["status"] for a in _audit(db, "vulnerability.update")
            if "status" in a["metadata"]["after"]] == ["resolved", "open", "resolved"]


def test_filters_and_paging(api, db):
    for n in range(3):
        _finding(api)
    body = api.get(f"{BASE}/vulnerabilities?status=open&severity=medium&page_size=2").json()
    assert len(body["vulnerabilities"]) == 2 and body["has_more"] is True
    assert api.get(f"{BASE}/vulnerabilities?status=resolved").json()["vulnerabilities"] == []
    assert api.get(f"{BASE}/incidents?status=nope").status_code == 422


def test_the_database_is_never_asked_to_delete():
    source = (HERE.parent / "services" / "security_cases.py").read_text(encoding="utf-8")
    assert ".delete(" not in source


# ── Dashboard ────────────────────────────────────────────────────────────────

def test_cases_screens_are_wired():
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    script = (STATIC / "security_cases.js").read_text(encoding="utf-8")
    assert html.index("/static/security_center.js") < html.index("/static/security_cases.js")
    wanted = set(re.findall(r"'(sc-[a-z]+(?:-[a-z]+)*)'", script))
    wanted |= {f"{p}-{part}" for p in ("sc-incidents", "sc-vulns") for part in ("page", "prev", "next")}
    wanted |= {f"{m}-close" for m in ("sc-new-modal", "sc-inc-modal", "sc-vuln-modal")}
    missing = sorted(i for i in wanted if f'id="{i}"' not in html)
    assert wanted and not missing, missing
    ids = re.findall(r'\bid="(sc-[^"]+)"', html)
    assert len(ids) == len(set(ids)), "duplicate ids"
    for name in ("security_cases.js",):
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", (STATIC / name).read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cases_script_behaviour_in_node():
    result = subprocess.run(["node", str(HERE / "security_cases_check.js"), str(STATIC)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "security cases OK" in result.stdout
