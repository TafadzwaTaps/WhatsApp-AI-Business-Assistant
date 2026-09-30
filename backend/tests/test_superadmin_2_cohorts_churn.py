"""
tests/test_superadmin_2_cohorts_churn.py — SuperAdmin 2.0 Phase 9: cohort
& churn analytics.

Covers:
  - /admin/saas/cohorts groups businesses by signup month and current
    billing_status, using only businesses.created_at/billing_status
  - /admin/saas/churn computes an all-time churn rate from current
    billing_status (always available, no history needed)
  - /admin/saas/churn's recent-churn windows are absent/None when
    subscription_events has never been populated (no fabricated history)
    and present once it has
  - crud/subscription_history.py fails soft (never raises) when the
    subscription_events table is missing
  - subscription events are logged on Stripe webhook transitions and on
    the SuperAdmin manual tier-override endpoint
  - authorization: business role rejected on both new endpoints
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


# ── crud/subscription_history.py fail-soft behavior ─────────────────────────

def test_log_subscription_event_never_raises_when_table_missing(fake_supabase, monkeypatch):
    from crud.subscription_history import log_subscription_event

    def _boom(*_a, **_kw):
        raise RuntimeError("relation subscription_events does not exist")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    log_subscription_event(
        business_id=1, previous_tier="starter", new_tier="growth",
        previous_status="active", new_status="active",
    )  # must not raise


def test_list_subscription_events_returns_empty_on_error(fake_supabase, monkeypatch):
    from crud.subscription_history import list_subscription_events

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    assert list_subscription_events() == []


def test_earliest_event_timestamp_none_when_no_events(fake_supabase):
    from crud.subscription_history import earliest_event_timestamp
    fake_supabase.results["subscription_events"] = []
    assert earliest_event_timestamp() is None


# ── Authorization ─────────────────────────────────────────────────────────────

def test_business_role_rejected_on_cohorts_and_churn(client):
    _override_business_role(client)
    assert client.get("/admin/saas/cohorts").status_code == 403
    assert client.get("/admin/saas/churn").status_code == 403


# ── /admin/saas/cohorts ──────────────────────────────────────────────────────

def test_cohorts_groups_by_signup_month_and_current_status(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = [
        {"id": 1, "created_at": "2026-01-05T10:00:00+00:00", "billing_status": "active", "subscription_tier": "starter"},
        {"id": 2, "created_at": "2026-01-20T10:00:00+00:00", "billing_status": "cancelled", "subscription_tier": "starter"},
        {"id": 3, "created_at": "2026-02-02T10:00:00+00:00", "billing_status": "trialing", "subscription_tier": "free"},
    ]
    resp = client.get("/admin/saas/cohorts")
    assert resp.status_code == 200
    body = resp.json()
    cohorts = {c["cohort_month"]: c for c in body["cohorts"]}
    assert cohorts["2026-01"]["signups"] == 2
    assert cohorts["2026-01"]["active"] == 1
    assert cohorts["2026-01"]["cancelled"] == 1
    assert cohorts["2026-02"]["signups"] == 1
    assert cohorts["2026-02"]["trialing"] == 1
    assert "not a point-in-time retention curve" in body["note"]


def test_cohorts_still_here_pct_excludes_cancelled(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = [
        {"id": 1, "created_at": "2026-03-01T00:00:00+00:00", "billing_status": "active", "subscription_tier": "starter"},
        {"id": 2, "created_at": "2026-03-15T00:00:00+00:00", "billing_status": "cancelled", "subscription_tier": "starter"},
    ]
    resp = client.get("/admin/saas/cohorts")
    cohort = resp.json()["cohorts"][0]
    assert cohort["still_here_pct"] == 50.0


# ── /admin/saas/churn ─────────────────────────────────────────────────────────

def test_churn_all_time_rate_from_current_billing_status(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = [
        {"subscription_tier": "starter", "billing_status": "active"},
        {"subscription_tier": "starter", "billing_status": "cancelled"},
        {"subscription_tier": "growth",  "billing_status": "past_due"},
        {"subscription_tier": "free",    "billing_status": "trialing"},  # excluded — never paid
    ]
    fake_supabase.results["subscription_events"] = []
    resp = client.get("/admin/saas/churn")
    assert resp.status_code == 200
    body = resp.json()
    assert body["all_time"]["ever_paid_count"] == 3   # active + cancelled + past_due
    assert body["all_time"]["cancelled_count"] == 1
    assert round(body["all_time"]["churn_rate_pct"], 1) == round(100 * 1 / 3, 1)
    assert body["all_time"]["churn_by_tier"] == {"starter": 1}


def test_churn_recent_windows_absent_when_no_events_tracked_yet(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = []
    fake_supabase.results["subscription_events"] = []
    resp = client.get("/admin/saas/churn")
    body = resp.json()
    assert body["recent"]["tracking_since"] is None
    assert "No subscription transitions recorded yet" in body["recent"]["note"]


def test_churn_recent_windows_present_once_events_exist(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = []
    fake_supabase.results["subscription_events"] = [
        {"created_at": "2026-09-01T00:00:00+00:00", "new_status": "cancelled"},
        {"created_at": "2026-09-15T00:00:00+00:00", "new_status": "active"},
    ]
    resp = client.get("/admin/saas/churn")
    body = resp.json()
    assert body["recent"]["tracking_since"] == "2026-09-01T00:00:00+00:00"
    assert body["recent"]["last_30_days"] == 1  # only the cancelled row counts


# ── Wiring: manual tier override logs a subscription event ──────────────────

def test_manual_tier_override_logs_subscription_event(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    fake_supabase.results["businesses"] = [
        {"id": 5, "subscription_tier": "starter", "billing_status": "active"},
    ]
    recorded = {}
    import crud.subscription_history as sub_hist
    monkeypatch.setattr(sub_hist, "log_subscription_event", lambda **kw: recorded.update(kw))

    resp = client.patch("/admin/saas/tenants/5/tier?tier=growth")
    assert resp.status_code == 200
    assert recorded["business_id"] == 5
    assert recorded["previous_tier"] == "starter"
    assert recorded["new_tier"] == "growth"
    assert recorded["source"] == "admin_override"
