"""
tests/test_superadmin_2_impersonation.py — SuperAdmin 2.0 Phase 13:
impersonation ("View as Business").

Covers:
  - core/auth.create_impersonation_token() issues a role="business" token
    carrying business_id, an "impersonated_by" claim, and a short expiry
    distinct from ACCESS_TOKEN_EXPIRE_MINUTES
  - get_current_user() surfaces impersonated_by for such a token, and a
    normal login token (no impersonated_by claim) is unaffected
  - POST /admin/saas/tenants/{id}/impersonate: requires a reason (422
    without one), 404 for an unknown business, 400 for a business with no
    owner_username, success returns a usable token and is audit-logged
    with action="business.impersonate_start"
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


# ── core/auth.py token mechanics ─────────────────────────────────────────────

def test_impersonation_token_carries_expected_claims():
    import core.auth as auth

    token = auth.create_impersonation_token(
        business_id=42, owner_username="bizowner", impersonated_by="root-admin",
    )
    user = auth.get_current_user(token)
    assert user["role"] == "business"
    assert user["business_id"] == 42
    assert user["username"] == "bizowner"
    assert user["impersonated_by"] == "root-admin"


def test_impersonation_token_expiry_is_short_and_distinct_from_normal_access_token():
    import core.auth as auth
    from jose import jwt

    token = auth.create_impersonation_token(
        business_id=1, owner_username="bizowner", impersonated_by="root-admin",
    )
    payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    exp_minutes = (payload["exp"] - payload.get("iat", payload["exp"])) if "iat" in payload else None
    # Simplest robust check: decode and confirm it's well inside a normal
    # 8h session, without depending on "iat" (not set by this codebase).
    import time
    remaining_minutes = (payload["exp"] - time.time()) / 60
    assert remaining_minutes <= auth.IMPERSONATION_TOKEN_EXPIRE_MINUTES + 1
    assert auth.IMPERSONATION_TOKEN_EXPIRE_MINUTES < auth.ACCESS_TOKEN_EXPIRE_MINUTES


def test_normal_login_token_has_no_impersonated_by_claim():
    import core.auth as auth

    token = auth.create_access_token({"sub": "bizowner", "role": "business", "business_id": 1})
    user = auth.get_current_user(token)
    assert "impersonated_by" not in user


def test_impersonation_token_carries_pwd_ts_when_given():
    import core.auth as auth

    token = auth.create_impersonation_token(
        business_id=1, owner_username="bizowner", impersonated_by="root-admin",
        pwd_ts="2026-01-01T00:00:00+00:00",
    )
    from jose import jwt
    payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    assert payload["pwd_ts"] == "2026-01-01T00:00:00+00:00"


# ── Authorization ─────────────────────────────────────────────────────────────

def test_business_role_rejected_on_impersonate_endpoint(client):
    _override_business_role(client)
    resp = client.post("/admin/saas/tenants/1/impersonate?reason=support")
    assert resp.status_code == 403


# ── POST /admin/saas/tenants/{id}/impersonate ────────────────────────────────

def test_impersonate_without_reason_is_rejected(client, fake_supabase):
    _override_superadmin(client)
    resp = client.post("/admin/saas/tenants/1/impersonate")
    assert resp.status_code == 422


def test_impersonate_unknown_business_404(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = []
    resp = client.post("/admin/saas/tenants/999/impersonate?reason=support+ticket+42")
    assert resp.status_code == 404


def test_impersonate_business_with_no_owner_username_rejected(client, fake_supabase):
    _override_superadmin(client)
    fake_supabase.results["businesses"] = [{"id": 1, "name": "Biz", "owner_username": None, "is_active": True}]
    resp = client.post("/admin/saas/tenants/1/impersonate?reason=support")
    assert resp.status_code == 400


def test_impersonate_success_returns_usable_token_and_is_audit_logged(client, fake_supabase, monkeypatch):
    _override_superadmin(client, username="root-admin")
    fake_supabase.results["businesses"] = [{
        "id": 7, "name": "Acme Foods", "owner_username": "acmeowner",
        "is_active": True, "password_changed_at": None,
    }]
    recorded = {}
    import routes.saas_admin_routes as saas_routes
    monkeypatch.setattr(saas_routes, "log_admin_action", lambda **kw: recorded.update(kw))

    resp = client.post("/admin/saas/tenants/7/impersonate?reason=Customer+asked+for+help+with+a+stuck+order")
    assert resp.status_code == 200
    body = resp.json()
    assert body["business_id"] == 7
    assert body["username"] == "acmeowner"
    assert body["impersonated_by"] == "root-admin"
    assert "access_token" in body

    # The returned token really does decode to a usable business session.
    import core.auth as auth
    decoded_user = auth.get_current_user(body["access_token"])
    assert decoded_user["role"] == "business"
    assert decoded_user["business_id"] == 7
    assert decoded_user["impersonated_by"] == "root-admin"

    assert recorded["action"] == "business.impersonate_start"
    assert recorded["business_id"] == 7
    assert recorded["reason"] == "Customer asked for help with a stuck order"
