"""Unsubscribe link on every email + login by username or email."""
import os
from fastapi.testclient import TestClient

import main
import services.email_service as es
from routes import auth_routes


def _capture(monkeypatch):
    sent = {}
    class R: status_code = 200; text = ""
    import requests
    def post(url, headers=None, json=None, timeout=None):
        sent.update(json); return R()
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(es, "_RESEND_API_KEY", "k")
    return sent


def test_every_email_gets_unsubscribe_link_and_headers(monkeypatch):
    sent = _capture(monkeypatch)
    assert es._send("a@x.com", "Receipt", "<html><body><p>hi</p></body></html>")
    assert "/email/unsubscribe-address?email=a%40x.com" in sent["html"]
    assert sent["html"].index("Unsubscribe") < sent["html"].index("</body>")
    assert "unsubscribe-address" in sent["headers"]["List-Unsubscribe"]
    assert sent["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_plain_fragment_and_no_duplicate(monkeypatch):
    sent = _capture(monkeypatch)
    es._send("a@x.com", "s", "<p>plain</p>")
    assert sent["html"].count("Unsubscribe") == 1
    page = es._base_template("t", "<p>x</p>", business_id=5)
    es._send("a@x.com", "s", page)
    assert sent["html"].count("/email/unsubscribe") == 1


def test_unsubscribe_endpoint_token_and_post(monkeypatch):
    saved = []
    monkeypatch.setattr(auth_routes, "_opt_out_address", lambda e: saved.append(e))
    c = TestClient(main.app)
    tok = es._address_token("A@x.com")
    assert c.get("/email/unsubscribe-address", params={"email": "a@x.com", "token": tok}).status_code == 200
    assert c.post("/email/unsubscribe-address", params={"email": "a@x.com", "token": tok}).status_code == 200
    assert c.get("/email/unsubscribe-address", params={"email": "a@x.com", "token": "bad"}).status_code == 400
    assert saved == ["a@x.com", "a@x.com"]


def _login_env(monkeypatch, biz_list):
    monkeypatch.setattr(auth_routes.crud, "get_business_by_username", lambda u: None)
    monkeypatch.setattr(auth_routes.crud, "get_businesses_by_email", lambda e: biz_list)
    monkeypatch.setattr(auth_routes, "verify_password", lambda p, h: p == h)
    return TestClient(main.app)


def test_login_with_email_matches_password(monkeypatch):
    c = _login_env(monkeypatch, [
        {"id": 1, "name": "A", "owner_username": "alpha", "owner_password": "pw1"},
        {"id": 2, "name": "B", "owner_username": "beta", "owner_password": "pw2"}])
    r = c.post("/auth/login", json={"username": "Owner@X.com", "password": "pw2"})
    assert r.status_code == 200, r.text
    assert r.json()["business_id"] == 2 and r.json()["role"] == "business"


def test_login_with_email_wrong_password_and_username_still_works(monkeypatch):
    c = _login_env(monkeypatch, [{"id": 1, "name": "A", "owner_username": "alpha", "owner_password": "pw1"}])
    assert c.post("/auth/login", json={"username": "o2@x.com", "password": "nope"}).status_code == 401
    monkeypatch.setattr(auth_routes.crud, "get_business_by_username",
                        lambda u: {"id": 9, "name": "Z", "owner_username": "zed", "owner_password": "pw"})
    r = c.post("/auth/login", json={"username": "zed", "password": "pw"})
    assert r.status_code == 200 and r.json()["business_id"] == 9
