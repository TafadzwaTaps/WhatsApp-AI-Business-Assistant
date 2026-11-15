"""Phase 1: webhook log hygiene + constant-time verify-token check. No network, no DB."""
import hashlib
import hmac
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main  # noqa: F401  (same import order as other tests)
from routes import webhook_routes as wr

SECRET = "test-app-secret"
PHONE = "263771234567"
TEXT = "my secret order text 98765"


def _client(monkeypatch):
    monkeypatch.setattr(wr, "WHATSAPP_APP_SECRET", SECRET)
    # Dedup hit => handler returns right after the STEP 1 log, touching nothing else.
    monkeypatch.setattr(wr.crud, "message_exists", lambda _id: True)
    app = FastAPI()
    app.include_router(wr.router)
    return TestClient(app)


def _post(client, payload):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def _payload(msg):
    msg = {"from": PHONE, "id": "wamid.TEST1", **msg}
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PNID"}, "messages": [msg]}}]}]}


def test_inbound_text_not_logged(monkeypatch, caplog):
    c = _client(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="wazibot"):
        r = _post(c, _payload({"type": "text", "text": {"body": TEXT}}))
    assert r.status_code == 200
    out = caplog.text
    assert "STEP 1 OK" in out
    assert PHONE not in out and TEXT not in out
    assert "4567" in out and "type=text" in out and f"len={len(TEXT)}" in out


def test_placeholder_types_still_processed_and_masked(monkeypatch, caplog):
    c = _client(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="wazibot"):
        r = _post(c, _payload({"type": "audio", "audio": {"id": "m1"}}))
    assert r.status_code == 200
    assert "STEP 1 OK" in caplog.text and "type=audio" in caplog.text
    assert PHONE not in caplog.text


def test_mask_phone():
    assert wr._mask_phone(PHONE) == "***4567"
    assert wr._mask_phone("") == "***" and wr._mask_phone(None) == "***"


def test_verify_token_flow_and_no_leak(monkeypatch, caplog):
    monkeypatch.setattr(wr, "VERIFY_TOKEN", "a-strong-random-token")
    c = TestClient(_app())
    with caplog.at_level(logging.DEBUG, logger="wazibot"):
        ok = c.get("/webhook", params={"hub.mode": "subscribe",
                                       "hub.verify_token": "a-strong-random-token",
                                       "hub.challenge": "CHAL123"})
        bad = c.get("/webhook", params={"hub.mode": "subscribe",
                                        "hub.verify_token": "wrong", "hub.challenge": "CHAL123"})
        missing = c.get("/webhook", params={"hub.mode": "subscribe", "hub.challenge": "CHAL123"})
    assert ok.status_code == 200 and ok.text == "CHAL123"
    assert bad.status_code == 403 and missing.status_code == 403
    assert "CHAL123" not in caplog.text and "a-strong-random-token" not in caplog.text


def test_placeholder_and_empty_token_rejected(monkeypatch):
    c = TestClient(_app())
    for tok in ("myverifytoken123", ""):
        monkeypatch.setattr(wr, "VERIFY_TOKEN", tok)
        r = c.get("/webhook", params={"hub.mode": "subscribe",
                                      "hub.verify_token": tok, "hub.challenge": "x"})
        assert r.status_code == 403


def _app():
    app = FastAPI()
    app.include_router(wr.router)
    return app
