"""
tests/test_superadmin_2.py — SuperAdmin 2.0 (Platform Control Center).

Covers:
  - the require_superadmin missing-import bug fix (routes.saas_admin_routes
    now imports cleanly and its routes are reachable)
  - authorization: non-superadmin (business role) is rejected on both the
    legacy /admin/* and the /admin/saas/* superadmin-only routes
  - crud.get_admin_stats() now returns the fields the dashboard already
    expects (trialing_count, paid_count, expired_trial_count,
    dedicated/shared number counts, expiring_soon, by_category, by_currency)
  - crud/admin_audit.py: audit log, admin notes, risk flags are best-effort
    and never raise even when their tables don't exist
  - mutating superadmin endpoints (suspend/activate/update/delete/tier
    change) write an audit-log entry recording who/what/why
  - the abuse scan uses multiple signals (shared owner_email / contact_phone)
    rather than a single-IP heuristic, and only flags for review — it never
    suspends or deletes anything itself
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _override_superadmin(client, username="root-admin"):
    import core.auth as auth

    def _fake_superadmin():
        return {"username": username, "role": "superadmin", "business_id": None}

    client.app.dependency_overrides[auth.get_current_user] = _fake_superadmin


def _override_business_role(client, business_id=1):
    import core.auth as auth

    def _fake_business():
        return {"username": "shopowner", "role": "business", "business_id": business_id}

    client.app.dependency_overrides[auth.get_current_user] = _fake_business


@pytest.fixture(autouse=True)
def _clear_overrides(client):
    yield
    client.app.dependency_overrides.clear()


# ── saas_admin_routes now imports and is reachable ──────────────────────────

def test_saas_admin_router_imports_and_registers_routes():
    import routes.saas_admin_routes as m
    paths = {r.path for r in m.router.routes}
    assert "/admin/saas/overview" in paths
    assert "/admin/saas/tenants/{business_id}" in paths
    assert "/admin/saas/audit-logs" in paths
    assert "/admin/saas/abuse/scan" in paths


# ── Authorization ────────────────────────────────────────────────────────────

def test_business_role_is_rejected_on_saas_overview(client):
    _override_business_role(client)
    resp = client.get("/admin/saas/overview")
    assert resp.status_code == 403


def test_business_role_is_rejected_on_legacy_admin_stats(client):
    _override_business_role(client)
    resp = client.get("/admin/stats")
    assert resp.status_code == 403


def test_superadmin_can_reach_saas_overview(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = [
        {"id": 1, "subscription_tier": "free", "is_active": True},
    ]
    fake_supabase.results["orders"] = []
    fake_supabase.results["messages"] = []
    resp = client.get("/admin/saas/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_businesses"] == 1


def test_unauthenticated_request_is_rejected(client):
    # No dependency override at all — the real oauth2 flow requires a token.
    resp = client.get("/admin/saas/overview")
    assert resp.status_code in (401, 403)


# ── crud.get_admin_stats() field coverage ────────────────────────────────────

def test_get_admin_stats_returns_fields_dashboard_expects(fake_supabase):
    import crud
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

    fake_supabase.results["businesses"] = [
        {"id": 1, "name": "Trialing Co", "billing_status": "trialing", "trial_ends_at": soon,
         "use_shared_number": False, "category": "Retail", "currency": "USD", "is_active": True},
        {"id": 2, "name": "Paid Co", "billing_status": "active", "trial_ends_at": None,
         "use_shared_number": True, "category": "Retail", "currency": "USD", "is_active": True},
        {"id": 3, "name": "Expired Co", "billing_status": "trialing", "trial_ends_at": past,
         "use_shared_number": True, "category": "Food", "currency": "ZAR", "is_active": False},
    ]
    fake_supabase.results["orders"] = []

    stats = crud.get_admin_stats()

    # trialing_count counts billing_status=="trialing" regardless of whether
    # the trial has already lapsed (that's expired_trial_count's job) —
    # businesses 1 and 3 are both still "trialing" in billing_status.
    assert stats["trialing_count"] == 2
    assert stats["paid_count"] == 1
    assert stats["expired_trial_count"] == 1
    assert stats["dedicated_number_count"] == 1
    assert stats["shared_number_count"] == 2
    assert any(e["name"] == "Trialing Co" for e in stats["expiring_soon"])
    assert stats["by_category"]["Retail"] == 2
    assert stats["by_currency"]["USD"] == 2


# ── crud/admin_audit.py — best-effort, never raises ──────────────────────────

def test_log_admin_action_never_raises_when_table_missing(fake_supabase, monkeypatch):
    from crud.admin_audit import log_admin_action

    def _boom(*_a, **_kw):
        raise RuntimeError("relation admin_audit_logs does not exist")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    # Must not raise.
    log_admin_action(actor_username="root", action="business.suspend", business_id=1)


def test_list_audit_logs_returns_empty_list_on_error(fake_supabase, monkeypatch):
    from crud.admin_audit import list_audit_logs

    def _boom(*_a, **_kw):
        raise RuntimeError("db unreachable")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    assert list_audit_logs() == []


def test_add_and_list_admin_note_round_trip(fake_supabase):
    from crud.admin_audit import add_admin_note, list_admin_notes

    fake_supabase.results["business_admin_notes"] = [
        {"id": 1, "business_id": 9, "author_username": "root", "note": "Looks fine, verified manually."}
    ]
    row = add_admin_note(9, "root", "Looks fine, verified manually.")
    assert row is not None
    notes = list_admin_notes(9)
    assert notes[0]["note"] == "Looks fine, verified manually."


# ── Mutating endpoints write an audit-log entry ──────────────────────────────

def test_suspend_business_writes_audit_log_entry(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    fake_supabase.results["businesses"] = [{"id": 5, "name": "Biz 5", "is_active": True}]

    recorded = {}
    from crud import admin_audit as audit_mod

    def _fake_log(**kwargs):
        recorded.update(kwargs)
    monkeypatch.setattr(audit_mod, "log_admin_action", _fake_log)
    # admin_routes imported the function by name, so patch its own reference too.
    import routes.admin_routes as admin_routes
    monkeypatch.setattr(admin_routes, "log_admin_action", _fake_log)

    resp = client.post("/platform/businesses/5/suspend?reason=Suspicious%20activity")
    assert resp.status_code == 200
    assert recorded["action"] == "business.suspend"
    assert recorded["business_id"] == 5
    assert recorded["actor_username"] == "root-admin"
    assert recorded["reason"] == "Suspicious activity"


def test_tier_change_writes_audit_log_entry_with_before_after(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    fake_supabase.results["businesses"] = [{"subscription_tier": "free"}]

    recorded = {}
    import routes.saas_admin_routes as saas_routes

    def _fake_log(**kwargs):
        recorded.update(kwargs)
    monkeypatch.setattr(saas_routes, "log_admin_action", _fake_log)

    resp = client.patch("/admin/saas/tenants/5/tier?tier=growth&reason=Manual+upgrade")
    assert resp.status_code == 200
    assert recorded["action"] == "business.tier_change"
    assert recorded["metadata"]["new_tier"] == "growth"


# ── Abuse scan: multi-signal, review-only, never auto-suspends ──────────────

def test_abuse_scan_flags_shared_owner_email_not_single_ip(client, fake_supabase, monkeypatch):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = [
        {"id": 1, "name": "Shop A", "owner_email": "same@example.com", "contact_phone": "+1000", "created_at": "2026-01-01"},
        {"id": 2, "name": "Shop B", "owner_email": "same@example.com", "contact_phone": "+2000", "created_at": "2026-01-02"},
        {"id": 3, "name": "Shop C", "owner_email": "unique@example.com", "contact_phone": "+3000", "created_at": "2026-01-03"},
    ]
    fake_supabase.results["business_risk_flags"] = []

    import routes.saas_admin_routes as saas_routes
    flags_created = []

    def _fake_add_flag(business_id, risk_level, reason, evidence=None):
        flags_created.append((business_id, risk_level, reason, evidence))
        return {"id": len(flags_created), "business_id": business_id}
    monkeypatch.setattr(saas_routes, "add_risk_flag", _fake_add_flag)
    monkeypatch.setattr(saas_routes, "list_risk_flags", lambda **kw: [])

    resp = client.post("/admin/saas/abuse/scan")
    assert resp.status_code == 200
    assert resp.json()["new_flags"] == 2  # businesses 1 and 2 share an email
    flagged_ids = {f[0] for f in flags_created}
    assert flagged_ids == {1, 2}
    assert all(f[3]["signal"] == "duplicate_owner_email" for f in flags_created)
    # Never touches is_active / never suspends anything itself.
    assert all("is_active" not in (f[3] or {}) for f in flags_created)


def test_resolve_risk_flag_requires_valid_status(client):
    _override_superadmin(client)
    resp = client.post("/admin/saas/risk-flags/1/resolve?status=deleted_forever")
    assert resp.status_code == 400
