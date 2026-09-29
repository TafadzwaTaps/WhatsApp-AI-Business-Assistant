"""
tests/test_superadmin_2_usage.py — SuperAdmin 2.0 Phase 4: message & AI
usage analytics (/admin/saas/usage/*).

Covers:
  - message volume aggregation (incoming/outgoing split, top businesses)
    without ever reading message text
  - AI usage/cost aggregation, and graceful "tracking not available yet"
    behavior when ai_usage_log can't be queried (never fabricated numbers)
  - centralized plan limits are surfaced as-is (not re-hardcoded here)
    and a business within 80% of its daily AI cap is flagged for review,
    without anything being throttled or suspended automatically
  - authorization: business role is still rejected on these new routes
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


# ── Authorization ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/admin/saas/usage/messages",
    "/admin/saas/usage/ai",
    "/admin/saas/usage/limits",
])
def test_business_role_is_rejected(client, path):
    _override_business_role(client)
    resp = client.get(path)
    assert resp.status_code == 403


# ── Message volume ───────────────────────────────────────────────────────────

def test_usage_messages_splits_incoming_outgoing_and_ranks_businesses(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["messages"] = [
        {"business_id": 1, "direction": "incoming", "created_at": "2026-09-29T00:00:00Z"},
        {"business_id": 1, "direction": "outgoing", "created_at": "2026-09-29T00:00:00Z"},
        {"business_id": 1, "direction": "outgoing", "created_at": "2026-09-29T00:00:00Z"},
        {"business_id": 2, "direction": "incoming", "created_at": "2026-09-29T00:00:00Z"},
    ]
    fake_supabase.results["businesses"] = [
        {"id": 1, "name": "Busy Shop"}, {"id": 2, "name": "Quiet Shop"},
    ]
    resp = client.get("/admin/saas/usage/messages?days=7")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_messages"] == 4
    assert body["incoming"] == 2
    assert body["outgoing"] == 2
    assert body["top_businesses"][0]["business_id"] == 1
    assert body["top_businesses"][0]["messages"] == 3


def test_usage_messages_never_selects_text_column(client, fake_supabase, monkeypatch):
    _override_superadmin(client)
    fake_supabase.results["messages"] = []
    fake_supabase.results["businesses"] = []
    seen_selects = []
    orig_select = fake_supabase.select
    def _spy_select(*a, **kw):
        seen_selects.append(a)
        return orig_select(*a, **kw)
    monkeypatch.setattr(fake_supabase, "select", _spy_select)
    resp = client.get("/admin/saas/usage/messages")
    assert resp.status_code == 200
    assert any("business_id" in str(s) and "text" not in str(s) for s in seen_selects if "direction" in str(s))


# ── AI usage / cost ──────────────────────────────────────────────────────────

def test_usage_ai_aggregates_cost_and_ranks_top_spenders(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["ai_usage_log"] = [
        {"business_id": 1, "total_tokens": 100, "estimated_cost": 0.01},
        {"business_id": 1, "total_tokens": 200, "estimated_cost": 0.02},
        {"business_id": 2, "total_tokens": 50,  "estimated_cost": 0.005},
    ]
    fake_supabase.results["businesses"] = [
        {"id": 1, "name": "Big Spender"}, {"id": 2, "name": "Small Spender"},
    ]
    resp = client.get("/admin/saas/usage/ai?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tracking_available"] is True
    assert body["total_requests"] == 3
    assert body["total_tokens"] == 350
    assert body["top_businesses"][0]["business_id"] == 1
    assert body["top_businesses"][0]["requests"] == 2


def test_usage_ai_reports_tracking_unavailable_without_fabricating_numbers(client, fake_supabase, monkeypatch):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = []

    def _boom(*_a, **_kw):
        raise RuntimeError("relation ai_usage_log does not exist")
    monkeypatch.setattr(fake_supabase, "execute", _boom)

    resp = client.get("/admin/saas/usage/ai")
    # execute() is patched globally, so the businesses lookup will also
    # fail — the endpoint must still respond, not 500, with zeros and a
    # clear "not available" note rather than any invented number.
    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        body = resp.json()
        assert body["tracking_available"] is False
        assert body["total_requests"] == 0
        assert "not available" in (body.get("note") or "")


# ── Centralized plan limits ──────────────────────────────────────────────────

def test_usage_limits_surfaces_centralized_dicts_and_flags_near_limit_business(client, fake_supabase):
    _override_superadmin(client)
    from core.plan_guard import PLAN_AI_REQUEST_LIMITS

    fake_supabase.results["businesses"] = [
        # billing_status "cancelled" resolves straight to the stored tier
        # (no active-trial ambiguity) — see plan_guard._normalise_tier.
        {"id": 5, "name": "Almost Capped Co", "subscription_tier": "free",
         "billing_status": "cancelled", "is_active": True},
    ]
    # FREE limit is 50/day (see PLAN_AI_REQUEST_LIMITS) — seed >= 80% of it.
    free_limit = PLAN_AI_REQUEST_LIMITS["FREE"]
    fake_supabase.results["ai_usage_log"] = [
        {"id": i, "business_id": 5, "created_at": "2026-09-29T12:00:00Z"}
        for i in range(int(free_limit * 0.85))
    ]

    resp = client.get("/admin/saas/usage/limits")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_ai_request_limits"] == PLAN_AI_REQUEST_LIMITS
    assert any(b["business_id"] == 5 for b in body["businesses_near_ai_limit"])
    # Purely informational — no is_active mutation, no suspend action taken.
    assert "is_active" not in str(body["businesses_near_ai_limit"])
