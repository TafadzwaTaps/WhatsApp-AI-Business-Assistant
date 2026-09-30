"""
tests/test_superadmin_2_campaign_abuse.py — SuperAdmin 2.0 Phase 10:
campaign-abuse detection.

Covers:
  - crud/campaign_log.py fails soft (never raises) when campaign_log is
    missing
  - a real (non-dry-run) campaign send is logged to campaign_log; a
    dry_run send is NOT
  - GET /admin/saas/usage/campaigns aggregates sends/recipients and
    ranks top senders, and is fail-soft when campaign_log is empty
  - POST /admin/saas/abuse/scan flags businesses over the recipient-volume
    threshold (medium risk) and, separately, businesses over the
    send-frequency threshold (low risk) — using the real CAMPAIGN_ABUSE_*
    constants rather than hardcoded numbers, so the test doesn't silently
    drift from the actual thresholds
  - authorization: business role rejected on the new usage endpoint
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


# ── crud/campaign_log.py fail-soft behavior ──────────────────────────────────

def test_log_campaign_send_never_raises_when_table_missing(fake_supabase, monkeypatch):
    from crud.campaign_log import log_campaign_send

    def _boom(*_a, **_kw):
        raise RuntimeError("relation campaign_log does not exist")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    log_campaign_send(business_id=1, audience="all", recipient_count=10, message_length=42)  # must not raise


def test_list_campaign_sends_returns_empty_on_error(fake_supabase, monkeypatch):
    from crud.campaign_log import list_campaign_sends

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    assert list_campaign_sends() == []


# ── campaign_send() logs real sends, not dry runs ────────────────────────────

def test_real_campaign_send_is_logged(client, fake_supabase, monkeypatch):
    import routes.business_routes as br

    def _fake_business():
        return {"username": "shopowner", "role": "business", "business_id": 1}
    client.app.dependency_overrides[br.require_business] = _fake_business

    fake_supabase.results["businesses"] = [{
        "id": 1, "subscription_tier": "growth", "billing_status": "active",
        "trial_ends_at": None, "trial_started_at": None,
    }]
    monkeypatch.setattr("core.platform_controls.is_paused", lambda flag: False)
    monkeypatch.setattr(
        "services.campaign_service.CampaignService.run",
        lambda **kw: {"sent": 7, "failed": 0, "dry_run": kw.get("dry_run", False)},
    )
    recorded = {}
    monkeypatch.setattr("crud.campaign_log.log_campaign_send", lambda **kw: recorded.update(kw))

    resp = client.post("/campaigns/send", json={"audience": "all", "message": "Hello there!"})
    assert resp.status_code == 200
    assert recorded["business_id"] == 1
    assert recorded["recipient_count"] == 7


def test_dry_run_campaign_send_is_not_logged(client, fake_supabase, monkeypatch):
    import routes.business_routes as br

    def _fake_business():
        return {"username": "shopowner", "role": "business", "business_id": 1}
    client.app.dependency_overrides[br.require_business] = _fake_business

    fake_supabase.results["businesses"] = [{
        "id": 1, "subscription_tier": "growth", "billing_status": "active",
        "trial_ends_at": None, "trial_started_at": None,
    }]
    monkeypatch.setattr("core.platform_controls.is_paused", lambda flag: False)
    monkeypatch.setattr(
        "services.campaign_service.CampaignService.run",
        lambda **kw: {"sent": 7, "failed": 0, "dry_run": True},
    )
    called = {"n": 0}
    def _fake_log(**kw):
        called["n"] += 1
    monkeypatch.setattr("crud.campaign_log.log_campaign_send", _fake_log)

    resp = client.post("/campaigns/send", json={"audience": "all", "message": "Hello there!", "dry_run": True})
    assert resp.status_code == 200
    assert called["n"] == 0


# ── Authorization ─────────────────────────────────────────────────────────────

def test_business_role_rejected_on_usage_campaigns(client):
    _override_business_role(client)
    assert client.get("/admin/saas/usage/campaigns").status_code == 403


# ── GET /admin/saas/usage/campaigns ──────────────────────────────────────────

def test_usage_campaigns_aggregates_and_ranks_top_senders(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["campaign_log"] = [
        {"business_id": 1, "recipient_count": 100},
        {"business_id": 1, "recipient_count": 50},
        {"business_id": 2, "recipient_count": 300},
    ]
    fake_supabase.results["businesses"] = [
        {"id": 1, "name": "Biz One"}, {"id": 2, "name": "Biz Two"},
    ]
    resp = client.get("/admin/saas/usage/campaigns")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_sends"] == 3
    assert body["total_recipients"] == 450
    assert body["top_businesses"][0]["business_id"] == 2   # highest recipients first
    assert body["top_businesses"][0]["recipients"] == 300


def test_usage_campaigns_is_zero_when_log_empty(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["campaign_log"] = []
    fake_supabase.results["businesses"] = []
    resp = client.get("/admin/saas/usage/campaigns")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_sends"] == 0
    assert body["total_recipients"] == 0
    assert body["top_businesses"] == []


# ── Abuse scan: campaign-volume signal ───────────────────────────────────────

def test_abuse_scan_flags_high_recipient_volume(client, fake_supabase, monkeypatch):
    _override_superadmin(client)
    from routes.saas_admin_routes import CAMPAIGN_ABUSE_MAX_RECIPIENTS_24H

    fake_supabase.results["businesses"] = [{"id": 9, "name": "Big Blaster", "owner_email": None, "contact_phone": None, "created_at": "2026-01-01T00:00:00+00:00"}]
    fake_supabase.results["risk_flags"] = []
    fake_supabase.results["business_risk_flags"] = []
    fake_supabase.results["campaign_log"] = [
        {"business_id": 9, "recipient_count": CAMPAIGN_ABUSE_MAX_RECIPIENTS_24H + 1},
    ]

    recorded_flags = []
    monkeypatch.setattr(
        "routes.saas_admin_routes.add_risk_flag",
        lambda business_id, risk_level, reason, evidence=None: recorded_flags.append(
            {"business_id": business_id, "risk_level": risk_level, "evidence": evidence}
        ),
    )
    monkeypatch.setattr("routes.saas_admin_routes.list_risk_flags", lambda **kw: [])
    monkeypatch.setattr("routes.saas_admin_routes.log_admin_action", lambda **kw: None)

    resp = client.post("/admin/saas/abuse/scan")
    assert resp.status_code == 200
    volume_flags = [f for f in recorded_flags if f["evidence"].get("signal") == "high_campaign_volume"]
    assert len(volume_flags) == 1
    assert volume_flags[0]["business_id"] == 9
    assert volume_flags[0]["risk_level"] == "medium"


def test_abuse_scan_flags_high_send_frequency_when_under_volume_threshold(client, fake_supabase, monkeypatch):
    _override_superadmin(client)
    from routes.saas_admin_routes import CAMPAIGN_ABUSE_MAX_SENDS_24H

    fake_supabase.results["businesses"] = [{"id": 9, "name": "Frequent Sender", "owner_email": None, "contact_phone": None, "created_at": "2026-01-01T00:00:00+00:00"}]
    fake_supabase.results["campaign_log"] = [
        {"business_id": 9, "recipient_count": 1}
        for _ in range(CAMPAIGN_ABUSE_MAX_SENDS_24H + 1)
    ]

    recorded_flags = []
    monkeypatch.setattr(
        "routes.saas_admin_routes.add_risk_flag",
        lambda business_id, risk_level, reason, evidence=None: recorded_flags.append(
            {"business_id": business_id, "risk_level": risk_level, "evidence": evidence}
        ),
    )
    monkeypatch.setattr("routes.saas_admin_routes.list_risk_flags", lambda **kw: [])
    monkeypatch.setattr("routes.saas_admin_routes.log_admin_action", lambda **kw: None)

    resp = client.post("/admin/saas/abuse/scan")
    assert resp.status_code == 200
    freq_flags = [f for f in recorded_flags if f["evidence"].get("signal") == "high_campaign_frequency"]
    assert len(freq_flags) == 1
    assert freq_flags[0]["risk_level"] == "low"


def test_abuse_scan_never_errors_when_campaign_log_missing(client, fake_supabase, monkeypatch):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = []

    def _boom(*_a, **_kw):
        raise RuntimeError("relation campaign_log does not exist")
    monkeypatch.setattr("crud.campaign_log.list_campaign_sends", _boom)

    resp = client.post("/admin/saas/abuse/scan")
    assert resp.status_code == 200  # the missing table degrades gracefully, doesn't 500
