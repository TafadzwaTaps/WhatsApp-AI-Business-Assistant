"""
tests/test_superadmin_2_platform_controls.py — SuperAdmin 2.0 Phase 8:
emergency platform controls & feature flags.

Covers:
  - core/platform_controls.py fails OPEN (not paused / not enabled) on
    any DB error or missing table — a pause flag must be an explicit
    SuperAdmin action, never an accidental side effect of an outage
  - pausing requires a reason; un-pausing does not
  - each of the five enforcement points actually short-circuits when its
    flag is set: signup, campaign send, AI replies, outbound WhatsApp
    send, and the public generated-site page
  - every control change is audit-logged with previous/new state
  - per-business feature overrides reuse businesses.features_json (no
    parallel store) and are audit-logged
  - authorization: business role rejected on all the new endpoints
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


# ── Fail-open behavior ───────────────────────────────────────────────────────

def test_is_paused_fails_open_when_table_missing(fake_supabase, monkeypatch):
    from core.platform_controls import is_paused

    def _boom(*_a, **_kw):
        raise RuntimeError("relation platform_settings does not exist")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    assert is_paused("signups") is False
    assert is_paused("ai_replies") is False


def test_is_paused_true_only_when_explicitly_set(fake_supabase):
    from core.platform_controls import is_paused, set_pause

    assert is_paused("campaigns") is False  # unset — fails open
    set_pause("campaigns", True, "root-admin")
    fake_supabase.results["platform_settings"] = [
        {"key": "pause.campaigns", "value": {"paused": True}},
    ]
    assert is_paused("campaigns") is True


def test_unknown_flag_name_is_never_treated_as_paused():
    from core.platform_controls import is_paused
    assert is_paused("not_a_real_flag") is False


# ── Authorization + reason requirement ───────────────────────────────────────

def test_business_role_rejected_on_platform_controls(client):
    _override_business_role(client)
    assert client.get("/admin/saas/platform/controls").status_code == 403
    assert client.post("/admin/saas/platform/controls/signups?paused=true&reason=x").status_code == 403


def test_pausing_without_reason_is_rejected(client, fake_supabase):
    _override_superadmin(client)
    resp = client.post("/admin/saas/platform/controls/signups?paused=true")
    assert resp.status_code == 422


def test_unpausing_does_not_require_a_reason(client, fake_supabase):
    _override_superadmin(client)
    resp = client.post("/admin/saas/platform/controls/signups?paused=false")
    assert resp.status_code == 200


def test_unknown_flag_is_rejected(client, fake_supabase):
    _override_superadmin(client)
    resp = client.post("/admin/saas/platform/controls/not_a_flag?paused=true&reason=test")
    assert resp.status_code == 400


def test_pause_toggle_is_audit_logged_with_before_after(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    recorded = {}
    import routes.saas_admin_routes as saas_routes
    monkeypatch.setattr(saas_routes, "log_admin_action", lambda **kw: recorded.update(kw))

    resp = client.post("/admin/saas/platform/controls/campaigns?paused=true&reason=Suspected+spam+wave")
    assert resp.status_code == 200
    assert recorded["action"] == "platform.pause.campaigns"
    assert recorded["reason"] == "Suspected spam wave"
    assert recorded["metadata"]["new_state"] is True


# ── Enforcement points ───────────────────────────────────────────────────────

def test_signup_is_blocked_when_signups_paused(client, monkeypatch):
    import routes.auth_routes as auth_routes
    monkeypatch.setattr(
        "core.platform_controls.is_paused",
        lambda flag: flag == "signups",
    )
    resp = client.post("/auth/signup", json={
        "username": "newbiz", "password": "SuperSecret123!",
        "business_name": "New Biz", "email": "new@example.com",
    })
    assert resp.status_code == 503


def test_campaign_send_is_blocked_when_campaigns_paused(client, fake_supabase, monkeypatch):
    # Override require_business via routes.business_routes's OWN bound
    # reference (the established pattern in test_phase14_conversation_
    # analytics.py), not a fresh `from core.auth import get_current_user`.
    # tests/test_auth_security.py deliberately pops and re-imports
    # core.auth (sys.modules.pop + importlib.import_module) to test
    # SECRET_KEY randomization — in a full-suite run that leaves a NEW
    # core.auth module object in sys.modules, distinct from the one
    # routes.business_routes bound require_business/get_current_user to
    # at the app's original startup. Overriding the fresh import's
    # get_current_user (or minting a token with its create_access_token)
    # silently misses the dependency the live route actually uses.
    # Overriding business_routes's own reference sidesteps that entirely.
    import routes.business_routes as br

    def _fake_business():
        return {"username": "shopowner", "role": "business", "business_id": 1}
    client.app.dependency_overrides[br.require_business] = _fake_business

    # require_plan("STARTER") / require_not_restricted are dependency
    # FACTORIES resolved at route-decoration time — give the fake business
    # a plan/billing status that genuinely satisfies both real checks
    # rather than trying to patch the factories themselves.
    fake_supabase.results["businesses"] = [{
        "id": 1, "subscription_tier": "growth", "billing_status": "active",
        "trial_ends_at": None, "trial_started_at": None,
    }]
    monkeypatch.setattr("core.platform_controls.is_paused", lambda flag: flag == "campaigns")

    resp = client.post("/campaigns/send", json={"audience": "all", "message": "Hello there!"})
    assert resp.status_code == 503


def test_ai_replies_short_circuit_when_paused(monkeypatch):
    import services.ai as ai_module
    monkeypatch.setattr("core.platform_controls.is_paused", lambda flag: flag == "ai_replies")

    reply = ai_module.generate_reply(
        message="hi", phone="+1555", business_id=1, business_name="Test Biz", products=[],
    )
    assert "temporarily paused" in reply.lower()


def test_whatsapp_send_is_skipped_when_paused(monkeypatch):
    import main as main_module
    monkeypatch.setattr("core.platform_controls.is_paused", lambda flag: flag == "whatsapp_send")

    result = main_module.send_whatsapp("123", "tok", "+1555", "hello")
    assert result == {"error": "whatsapp_sending_paused"}


def test_site_page_shows_maintenance_when_website_gen_paused(client, monkeypatch):
    monkeypatch.setattr("core.platform_controls.is_paused", lambda flag: flag == "website_gen")
    resp = client.get("/site/some-slug")
    assert resp.status_code == 503
    assert "temporarily unavailable" in resp.text.lower()


def test_public_maintenance_status_endpoint_needs_no_auth(client, fake_supabase):
    resp = client.get("/platform/maintenance-status")
    assert resp.status_code == 200
    assert "enabled" in resp.json()


# ── Feature flags ────────────────────────────────────────────────────────────

def test_feature_flags_listing_never_invents_flags_that_were_not_set(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["platform_settings"] = []
    resp = client.get("/admin/saas/flags")
    assert resp.status_code == 200
    assert resp.json()["flags"] == []


def test_setting_a_feature_flag_is_audit_logged(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    recorded = {}
    import routes.saas_admin_routes as saas_routes
    monkeypatch.setattr(saas_routes, "log_admin_action", lambda **kw: recorded.update(kw))

    resp = client.post("/admin/saas/flags/new_checkout?enabled=true")
    assert resp.status_code == 200
    assert recorded["action"] == "platform.feature_flag"
    assert recorded["metadata"]["flag"] == "new_checkout"


# ── Per-business feature overrides reuse features_json ──────────────────────

def test_business_feature_override_merges_into_existing_features_json(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    fake_supabase.results["businesses"] = [{"id": 7, "features_json": {"beta_ui": True}}]
    recorded = {}
    import routes.saas_admin_routes as saas_routes
    monkeypatch.setattr(saas_routes, "log_admin_action", lambda **kw: recorded.update(kw))

    resp = client.post("/admin/saas/tenants/7/features", json={"custom_limit": 500})
    assert resp.status_code == 200
    body = resp.json()
    assert body["features_json"]["beta_ui"] is True     # preserved
    assert body["features_json"]["custom_limit"] == 500  # merged in
    assert recorded["action"] == "business.feature_override"
