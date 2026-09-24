"""
tests/test_password_reset_security.py — Phase 1 regression tests.

Covers the password-reset security fix: routes/password_reset_routes.py
used to expose TWO ways to reset a password —

  1. POST /auth/reset-password       — secure: single-use, expiring,
     server-generated token emailed to the account owner.
  2. POST /auth/reset-password-direct — insecure: changed the password
     given only a username or email, with no proof the caller controlled
     that account. A textbook account-takeover vector, self-documented
     in the endpoint's own docstring.

The fix disabled #2 by removing its @router decorator (so FastAPI never
registers the route) without deleting the function body, and pointed the
frontend "forgot password" page at the secure flow instead. These tests
assert the vulnerable endpoint is genuinely gone from the live app while
the secure flow's contract (rate limiting, no enumeration, strength
checks, token validation) still holds.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.password_reset_routes as password_reset_routes


def _build_app():
    app = FastAPI()
    app.include_router(password_reset_routes.router)
    return app


def test_direct_reset_endpoint_is_not_registered(monkeypatch):
    """
    The core regression test: the insecure endpoint must not be reachable
    at all — not rate-limited, not erroring, genuinely absent from the
    route table (404), so it can never be re-discovered and hit in prod.
    """
    client = TestClient(_build_app())
    resp = client.post("/auth/reset-password-direct", json={
        "identifier": "someone@example.com",
        "new_password": "NewPassw0rd!",
        "confirm_password": "NewPassw0rd!",
    })
    assert resp.status_code == 404


def test_direct_reset_route_missing_from_openapi_schema():
    """Belt-and-suspenders: confirm it's absent from the router's own
    route list, not just returning 404 for some unrelated reason."""
    paths = {route.path for route in password_reset_routes.router.routes}
    assert "/auth/reset-password-direct" not in paths
    # The three legitimate endpoints must still be there.
    assert "/auth/forgot-password" in paths
    assert "/auth/validate-reset-token" in paths
    assert "/auth/reset-password" in paths


def test_direct_password_reset_function_still_exists_but_unregistered():
    """
    Per the standing 'never delete working code' rule, the handler and its
    request model are kept in the module (for reference / audit trail) —
    just not wired up as a route. Confirms that's still true rather than
    the function having been silently deleted in some later edit.
    """
    assert hasattr(password_reset_routes, "reset_password_direct")
    assert hasattr(password_reset_routes, "DirectResetRequest")


def test_forgot_password_never_reveals_whether_email_exists(monkeypatch):
    """The secure flow's enumeration protection must be untouched by the
    fix: an unknown email gets exactly the same generic response."""
    monkeypatch.setattr(
        "services.password_reset_service.request_password_reset",
        lambda email, ip_address="", user_agent="": True,
    )
    client = TestClient(_build_app())
    resp = client.post(
        "/auth/forgot-password", json={"email": "nobody@nowhere.com"},
        headers={"x-forwarded-for": "10.0.0.1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "if an account" in data["message"].lower()


def test_forgot_password_is_rate_limited(monkeypatch):
    """5 requests/hour/IP — the 6th in the same window must be rejected.
    Uses its own X-Forwarded-For IP so it doesn't share the in-process
    rate-limit bucket with other tests hitting the same endpoint."""
    monkeypatch.setattr(
        "services.password_reset_service.request_password_reset",
        lambda email, ip_address="", user_agent="": True,
    )
    client = TestClient(_build_app())
    headers = {"x-forwarded-for": "10.0.0.2"}
    for _ in range(5):
        resp = client.post("/auth/forgot-password", json={"email": "a@b.com"}, headers=headers)
        assert resp.status_code == 200
    resp = client.post("/auth/forgot-password", json={"email": "a@b.com"}, headers=headers)
    assert resp.status_code == 429


def test_reset_password_rejects_weak_password(monkeypatch):
    client = TestClient(_build_app())
    resp = client.post("/auth/reset-password", json={
        "token": "some-token",
        "new_password": "weak",
        "confirm_password": "weak",
    })
    assert resp.status_code == 400


def test_reset_password_rejects_mismatched_confirmation(monkeypatch):
    client = TestClient(_build_app())
    resp = client.post("/auth/reset-password", json={
        "token": "some-token",
        "new_password": "GoodPassw0rd!",
        "confirm_password": "DifferentPassw0rd!",
    })
    assert resp.status_code == 400


def test_reset_password_succeeds_with_valid_token_and_strong_password(monkeypatch):
    monkeypatch.setattr(
        "services.password_reset_service.complete_password_reset",
        lambda token, new_password: {"ok": True},
    )
    client = TestClient(_build_app())
    resp = client.post("/auth/reset-password", json={
        "token": "a-valid-token",
        "new_password": "GoodPassw0rd1",
        "confirm_password": "GoodPassw0rd1",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_reset_password_rejects_invalid_or_expired_token(monkeypatch):
    monkeypatch.setattr(
        "services.password_reset_service.complete_password_reset",
        lambda token, new_password: {"ok": False, "error": "Link expired"},
    )
    client = TestClient(_build_app())
    resp = client.post("/auth/reset-password", json={
        "token": "an-expired-token",
        "new_password": "GoodPassw0rd1",
        "confirm_password": "GoodPassw0rd1",
    })
    assert resp.status_code == 400
