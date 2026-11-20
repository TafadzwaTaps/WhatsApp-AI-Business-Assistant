"""Phase 4 follow-ups: impersonation on money routes, unsubscribe secret, SECRET_KEY warning, reset length cap."""
import os
import subprocess
import sys
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import core.auth as auth

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MONEY_ROUTES = [
    ("POST", "/orders/{order_id}/approve-payment"), ("POST", "/orders/{order_id}/cancel"),
    ("POST", "/orders/{order_id}/refund"), ("PUT", "/orders/{order_id}/status"),
    ("POST", "/me/payment-settings"), ("POST", "/me/payment-settings/toggles"),
    ("POST", "/me/payment-settings/ecocash"), ("POST", "/me/payment-settings/paypal"),
    ("POST", "/me/payment-settings/banktransfer"), ("POST", "/me/payment-settings/blik"),
    ("POST", "/me/payment-settings/stripe"), ("POST", "/me/delete-account"),
    ("POST", "/me/referral/withdraw"),
]


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)


def _imp():
    return {"Authorization": "Bearer " + auth.create_access_token(
        {"sub": "shop", "role": "business", "business_id": 5, "impersonated_by": "root"})}


def test_dependency_refuses_support_sessions_only():
    with pytest.raises(HTTPException) as e:
        auth.require_not_impersonated({"role": "business", "business_id": 5, "impersonated_by": "root", "username": "x"})
    assert e.value.status_code == 403
    own = {"role": "business", "business_id": 5, "username": "x"}
    assert auth.require_not_impersonated(own) == own


@pytest.mark.parametrize("method,path", MONEY_ROUTES)
def test_every_money_route_carries_the_guard(method, path):
    import main
    from _routes_util import find_route
    route = find_route(main.app, path, method)
    assert any(d.call.__name__ == "require_not_impersonated" for d in route.dependant.dependencies), path


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/orders/1/refund", {}), ("POST", "/orders/1/cancel", {}), ("POST", "/orders/1/approve-payment", {}),
    ("POST", "/me/payment-settings/paypal", {"paypal_email": "x@example.com"}),
    ("POST", "/me/payment-settings/toggles", {}),
])
def test_support_session_gets_403_on_money_routes(client, authed, method, path, body):
    r = client.request(method, path, json=body, headers=_imp())
    assert r.status_code == 403 and ("read-only" in r.text.lower() or "viewing as another business" in r.text)


def test_support_session_cannot_change_the_owner_email(client, authed, monkeypatch):
    import crud
    saved = []
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "owner_email": "owner@example.com"})
    monkeypatch.setattr(crud, "update_business", lambda b, d: saved.append(d.dict()) or {"id": b})
    r = client.patch("/me", json={"owner_email": "attacker@example.com"}, headers=_imp())
    assert r.status_code == 403 and saved == []
    # support sessions are read-only: other profile edits are refused too (Phase 5 policy)
    r2 = client.patch("/me", json={"description": "hello"}, headers=_imp())
    assert r2.status_code == 403 and saved == []


def test_support_session_can_still_read(client, authed, monkeypatch):
    import crud
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop"})
    assert client.get("/me", headers=_imp()).status_code == 200


# -- unsubscribe tokens ---------------------------------------------------------------------------
def test_unsubscribe_secret_is_the_effective_signing_key_not_a_public_constant():
    import services.email_service as es
    assert es._derive_unsub_secret() == auth.SECRET_KEY
    assert es._derive_unsub_secret() != "wazibot-unsubscribe-fallback-secret"
    assert "wazibot-unsubscribe-fallback-secret" not in open(es.__file__, encoding="utf8").read().split("_derive_unsub_secret")[1][:900]
    tok = es._unsubscribe_token(7)
    assert es.verify_unsubscribe_token(7, tok) and not es.verify_unsubscribe_token(8, tok)


def test_unsubscribe_secret_falls_back_to_a_random_value_if_auth_is_unavailable(monkeypatch):
    import builtins
    import services.email_service as es
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "core.auth":
            raise ImportError("x")
        return real(name, *a, **k)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)
    a, b = es._derive_unsub_secret(), es._derive_unsub_secret()
    assert a != b and len(a) >= 32 and a != "wazibot-unsubscribe-fallback-secret"


# -- SECRET_KEY length: warn, never replace ------------------------------------------------------
def _import_auth_with_key(key):
    env = {**os.environ, "SECRET_KEY": key}
    code = "import core.auth as a; print('KEY=' + a.SECRET_KEY)"
    return subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)


def test_short_secret_key_is_kept_with_a_warning():
    out = _import_auth_with_key("abc123def")
    assert "KEY=abc123def" in out.stdout                 # sessions are not invalidated
    assert "only 9 characters" in out.stderr


def test_long_secret_key_is_silent_and_unchanged():
    key = "k" * 40
    out = _import_auth_with_key(key)
    assert f"KEY={key}" in out.stdout and "characters; use at least 32" not in out.stderr


def test_weak_default_key_is_still_replaced():
    out = _import_auth_with_key("secret")
    assert "KEY=secret" not in out.stdout and "KEY=" in out.stdout


# -- reset password length -----------------------------------------------------------------------
def test_reset_password_rejects_overlong_passwords(client):
    long_pw = "Aa1" + "x" * 70                                   # 73 bytes: bcrypt would ignore the tail
    r = client.post("/auth/reset-password", json={"token": "t" * 40, "new_password": long_pw, "confirm_password": long_pw})
    assert r.status_code == 400 and "too long" in r.text
    huge = "Aa1" + "x" * 500
    r = client.post("/auth/reset-password", json={"token": "t" * 40, "new_password": huge, "confirm_password": huge})
    assert r.status_code == 422


def test_reset_password_multibyte_password_over_72_bytes_is_rejected(client):
    pw = "Aa1" + "é" * 40                                        # 43 characters, 83 bytes
    r = client.post("/auth/reset-password", json={"token": "t" * 40, "new_password": pw, "confirm_password": pw})
    assert r.status_code == 400 and "too long" in r.text
