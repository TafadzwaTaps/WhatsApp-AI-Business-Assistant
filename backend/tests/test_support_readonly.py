"""Support ("view as business") sessions are read-only, enforced in the backend, and audited."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import core.auth as auth


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


@pytest.fixture
def audit(monkeypatch):
    rows = []
    import crud.admin_audit as aa
    monkeypatch.setattr(aa, "log_admin_action", lambda **kw: rows.append(kw))
    auth._support_access_logged.clear()
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    return rows


def _h(**extra):
    claims = {"sub": "shop", "role": "business", "business_id": 5, **extra}
    return {"Authorization": "Bearer " + auth.create_access_token(claims)}


SUPPORT = {"impersonated_by": "root"}


@pytest.mark.parametrize("method,path", [
    ("POST", "/products"), ("PATCH", "/products/1"), ("DELETE", "/products/1"), ("PUT", "/orders/1/status"),
    ("POST", "/orders/1/refund"), ("POST", "/broadcast"), ("POST", "/campaigns/send"), ("POST", "/chat/send"),
    ("PATCH", "/me"), ("POST", "/me/payment-settings/paypal"), ("POST", "/billing/cancel"),
    ("POST", "/me/upload-avatar"), ("POST", "/voice/transcribe"), ("POST", "/products/import-csv"),
    ("POST", "/me/delete-account"),
])
def test_writes_are_blocked_for_support_sessions_whatever_the_route(client, audit, method, path):
    r = client.request(method, path, json={}, headers=_h(**SUPPORT))
    assert r.status_code == 403
    assert "read-only" in r.text.lower()
    assert audit and audit[-1]["action"] == "support.write_blocked"
    assert audit[-1]["actor_username"] == "root" and audit[-1]["business_id"] == 5
    assert audit[-1]["metadata"]["method"] == method and audit[-1]["metadata"]["path"] == path


def test_every_blocked_attempt_is_audited(client, audit):
    for _ in range(3):
        client.post("/products", json={}, headers=_h(**SUPPORT))
    assert [r["action"] for r in audit] == ["support.write_blocked"] * 3


def test_reads_work_and_are_audited_once_per_window(client, audit, monkeypatch):
    import crud
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop"})
    assert client.get("/me", headers=_h(**SUPPORT)).status_code == 200
    assert client.get("/me", headers=_h(**SUPPORT)).status_code == 200
    assert [r["action"] for r in audit] == ["support.access"]
    auth._support_access_logged[("root", 5)] -= auth._SUPPORT_ACCESS_LOG_INTERVAL + 1
    client.get("/me", headers=_h(**SUPPORT))
    assert [r["action"] for r in audit] == ["support.access", "support.access"]


def test_access_log_is_per_support_user_and_business(client, audit, monkeypatch):
    import crud
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop"})
    client.get("/me", headers=_h(impersonated_by="alice"))
    client.get("/me", headers=_h(impersonated_by="bob"))
    assert [r["actor_username"] for r in audit] == ["alice", "bob"]


@pytest.mark.parametrize("path", ["/debug/webhook", "/debug/schema", "/me/test-whatsapp"])
def test_reads_that_reach_outbound_services_are_blocked(client, audit, path):
    assert client.get(path, headers=_h(**SUPPORT)).status_code == 403
    assert audit[-1]["action"] == "support.write_blocked"


def test_dry_run_preview_stays_available(client, audit, monkeypatch):
    r = client.post("/campaigns/preview", json={"audience": "all", "message": "hello there"}, headers=_h(**SUPPORT))
    assert r.status_code != 403


def test_owner_and_superadmin_sessions_are_not_affected(client, audit, monkeypatch):
    import crud
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop"})
    monkeypatch.setattr(crud, "update_business", lambda b, d: {"id": b, **d.dict()})
    r = client.patch("/me", json={"description": "hello"}, headers=_h())                    # the owner
    assert r.status_code == 200 and audit == []
    root = {"Authorization": "Bearer " + auth.create_access_token({"sub": "root", "role": "superadmin"})}
    assert client.get("/debug/schema", headers=root).status_code == 200                      # admin tooling
    assert audit == []


def test_direct_calls_without_a_request_are_unchanged(audit):
    t = auth.create_access_token({"sub": "shop", "role": "business", "business_id": 5, "impersonated_by": "root"})
    assert auth.get_current_user(t)["impersonated_by"] == "root"


def test_audit_failure_never_breaks_the_block(client, monkeypatch):
    import crud.admin_audit as aa
    monkeypatch.setattr(aa, "log_admin_action", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    assert client.post("/products", json={}, headers=_h(**SUPPORT)).status_code == 403


def test_the_enforcement_is_in_the_backend_not_the_ui():
    import os
    html = open(os.path.join(os.path.dirname(__file__), "..", "..", "static", "dashboard.html"), encoding="utf8").read()
    assert "read-only support session" in html
    with pytest.raises(HTTPException):
        class R:
            method, client = "POST", None
            headers = {}
            class url: path = "/products"
        auth._enforce_support_read_only(R, {"impersonated_by": "root", "business_id": 5})


@pytest.mark.parametrize("path", ["/bookings/export", "/marketing/qr/download", "/billing/connect/status",
                                  "/billing/invoices", "/orders/5/invoice"])
def test_exports_downloads_and_invoices_are_blocked_for_support_sessions(client, audit, path):
    assert client.get(path, headers=_h(**SUPPORT)).status_code == 403
    assert audit[-1]["action"] == "support.write_blocked"


def test_a_failed_audit_write_is_logged_loudly(client, monkeypatch, caplog):
    import crud.admin_audit as aa
    import logging
    monkeypatch.setattr(aa, "log_admin_action", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    with caplog.at_level(logging.WARNING, logger="wazibot.auth"):
        assert client.post("/products", json={}, headers=_h(**SUPPORT)).status_code == 403
    assert any("SUPPORT AUDIT NOT RECORDED" in r.message for r in caplog.records)
