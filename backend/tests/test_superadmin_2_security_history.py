"""
tests/test_superadmin_2_security_history.py — SuperAdmin 2.0 Phase 11:
persisted security-event history on top of the Phase 6 live snapshot.

Covers:
  - crud/security_events.py fails soft (never raises) when security_events
    is missing
  - the four threshold-crossing points actually persist an event:
    IP login-limit exceeded, account lockout, invalid webhook signature,
    signup-success limit exceeded — and that persistence failing does NOT
    stop the underlying security check from still firing
  - GET /admin/saas/security/history returns tracking_since=None (no
    fabricated trend) when no events exist, and real daily/type counts
    once they do
  - authorization: business role rejected
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _override_superadmin(client, username="root-admin"):
    import core.auth as auth
    client.app.dependency_overrides[auth.get_current_user] = lambda: {
        "username": username, "role": "superadmin", "business_id": None,
    }


def _override_business_role(client, business_id=1):
    import core.auth as auth
    client.app.dependency_overrides[auth.get_current_user] = lambda: {
        "username": "shopowner", "role": "business", "business_id": business_id,
    }


@pytest.fixture(autouse=True)
def _clear_overrides(client):
    yield
    client.app.dependency_overrides.clear()


# ── crud/security_events.py fail-soft behavior ───────────────────────────────

def test_log_security_event_never_raises_when_table_missing(fake_supabase, monkeypatch):
    from crud.security_events import log_security_event

    def _boom(*_a, **_kw):
        raise RuntimeError("relation security_events does not exist")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    log_security_event(event_type="ip_login_flagged", ip="1.2.3.4")  # must not raise


def test_list_security_events_returns_empty_on_error(fake_supabase, monkeypatch):
    from crud.security_events import list_security_events

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    assert list_security_events() == []


# ── Threshold-crossing points persist events, and never break on failure ────

def test_ip_login_limit_exceeded_persists_event_and_still_raises(fake_supabase, monkeypatch):
    import services.security as sec

    recorded = {}
    monkeypatch.setattr(sec, "_persist_security_event", lambda *a, **kw: recorded.update(kw or {"args": a}))
    monkeypatch.setattr(sec, "LOGIN_MAX_ATTEMPTS_PER_IP", 1)

    sec.record_failed_login_ip("9.9.9.9")
    with pytest.raises(sec.RateLimitExceeded):
        sec.check_ip_login_limit("9.9.9.9")
    assert recorded  # persistence was attempted


def test_account_lockout_persists_event_and_still_raises(monkeypatch):
    import services.security as sec

    recorded = {}
    monkeypatch.setattr(sec, "_persist_security_event", lambda *a, **kw: recorded.update(kw or {"args": a}))
    monkeypatch.setattr(sec, "LOGIN_MAX_ATTEMPTS_PER_ACCOUNT", 1)

    sec.record_failed_login_account("victim")
    with pytest.raises(sec.RateLimitExceeded):
        sec.check_account_login_lockout("victim")
    assert recorded


def test_persist_failure_never_breaks_the_actual_lockout(monkeypatch):
    """The whole point of _persist_security_event's own try/except: even if
    the underlying crud write blows up, the real security check (raising
    RateLimitExceeded) must still happen."""
    import services.security as sec
    import crud.security_events as sec_events

    def _boom(**_kw):
        raise RuntimeError("db is on fire")
    monkeypatch.setattr(sec_events, "log_security_event", _boom)
    monkeypatch.setattr(sec, "LOGIN_MAX_ATTEMPTS_PER_ACCOUNT", 1)

    sec.record_failed_login_account("victim2")
    with pytest.raises(sec.RateLimitExceeded):
        sec.check_account_login_lockout("victim2")


def test_webhook_invalid_signature_persists_event(monkeypatch):
    import services.security as sec

    recorded = []
    monkeypatch.setattr(sec, "_persist_security_event", lambda *a, **kw: recorded.append((a, kw)))

    ok = sec.verify_meta_signature(b"payload", "sha256=deadbeef", "supersecret")
    assert ok is False
    assert recorded and recorded[0][0][0] == "webhook_invalid_signature"


# ── Authorization ─────────────────────────────────────────────────────────────

def test_business_role_rejected_on_security_history(client):
    _override_business_role(client)
    assert client.get("/admin/saas/security/history").status_code == 403


# ── GET /admin/saas/security/history ─────────────────────────────────────────

def test_history_has_no_fabricated_trend_when_no_events(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["security_events"] = []
    resp = client.get("/admin/saas/security/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tracking_since"] is None
    assert body["daily_counts"] == []
    assert all(v == 0 for v in body["totals_by_type"].values())


def test_history_aggregates_daily_counts_by_type(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["security_events"] = [
        {"event_type": "account_lockout", "created_at": "2026-09-10T00:00:00+00:00"},
        {"event_type": "ip_login_flagged", "created_at": "2026-09-10T00:00:00+00:00"},
    ]
    resp = client.get("/admin/saas/security/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tracking_since"] == "2026-09-10T00:00:00+00:00"
    assert body["totals_by_type"]["account_lockout"] == 1
    assert body["totals_by_type"]["ip_login_flagged"] == 1
    assert body["daily_counts"][0]["date"] == "2026-09-10"
    assert body["daily_counts"][0]["account_lockout"] == 1
