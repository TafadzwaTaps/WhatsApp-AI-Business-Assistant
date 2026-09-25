"""
tests/test_webhook_intent_engine.py — Phase 2 regression tests.

Covers how services/intent_engine.py is wired into the live webhook
(routes/webhook_routes.py):

  1. By default (INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT unset/false),
     classification runs and is logged, but NEVER changes what happens
     next — every message, however it classifies, still reaches
     generate_reply() exactly as it did before Phase 2. This is the
     regression-safety net: Phase 2 must not change production behavior
     until explicitly turned on.
  2. With the env var enabled, a plain-text message that classifies as
     low-confidence WHILE THE CUSTOMER IS IDLE gets the spec's open
     clarification question instead of generate_reply() — but ONLY then.
  3. The intercept must never fire for: a customer mid-flow (active
     state — the existing state machine must always win there), a
     non-text message (image/video/location/etc.), or a message from
     the business's own agent number.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import crud
import routes.webhook_routes as webhook_routes


BUSINESS = {
    "id": 1, "name": "Test Biz", "is_active": True, "whatsapp_phone_id": "",
    "preferred_language": "en",
    "currency": "USD", "currency_symbol": "$", "welcome_message": "", "menu_header": "",
    "is_service_business": False, "default_slot_mins": 60, "pickup_enabled": True,
    "cash_enabled": True, "delivery_fee": 0, "category": "",
}
CUSTOMER = {"id": 42}
PHONE_NUMBER_ID = "1000000000"
CUSTOMER_PHONE = "263771234567"


def _build_app():
    app = FastAPI()
    app.include_router(webhook_routes.router)
    return app


def _envelope(msg_obj: dict) -> dict:
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": PHONE_NUMBER_ID}, "messages": [msg_obj],
    }}]}]}


def _text_msg(text, wa_id="wamid.1"):
    return {"type": "text", "from": CUSTOMER_PHONE, "id": wa_id, "text": {"body": text}}


@pytest.fixture
def client(monkeypatch, fake_supabase):
    monkeypatch.setattr(webhook_routes, "WHATSAPP_APP_SECRET", "")
    monkeypatch.setattr(webhook_routes, "SHARED_WA_TOKEN", "")
    monkeypatch.setattr(webhook_routes, "manager", MagicMock(broadcast=AsyncMock()))
    monkeypatch.setattr(webhook_routes, "send_whatsapp",
                         MagicMock(return_value={"messages": [{"id": "wamid_out"}]}))
    monkeypatch.setattr(webhook_routes, "_send_direct", MagicMock(return_value={}))
    monkeypatch.setattr(webhook_routes, "_log_event", MagicMock())

    monkeypatch.setattr(crud, "message_exists", lambda wa_id: False)
    monkeypatch.setattr(crud, "get_business_by_phone_id", lambda pid: dict(BUSINESS))
    monkeypatch.setattr(crud, "get_decrypted_token", lambda biz: "fake-wa-token")
    monkeypatch.setattr(crud, "get_or_create_customer", lambda phone, biz_id: dict(CUSTOMER))
    monkeypatch.setattr(crud, "log_message", lambda *a, **kw: None)
    monkeypatch.setattr(crud, "create_message", lambda *a, **kw: {"id": 1})
    monkeypatch.setattr(crud, "get_products", lambda biz_id: [])

    return TestClient(_build_app())


def _capture_generate_reply(monkeypatch):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return "ok, got it!"

    monkeypatch.setattr(webhook_routes, "generate_reply", _fake)
    return calls


# ── Default OFF: zero behavior change ───────────────────────────────────────

def test_intercept_is_off_by_default_even_for_unclassifiable_text(client, monkeypatch):
    """The core regression-safety test: with the env var unset, an
    unclassifiable message must still reach generate_reply() exactly as
    it did before Phase 2 — classification is observability-only."""
    monkeypatch.delenv("INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", raising=False)
    calls = _capture_generate_reply(monkeypatch)
    resp = client.post("/webhook", json=_envelope(_text_msg("asdkjfh qqqq zzz")))
    assert resp.status_code == 200
    assert len(calls) == 1
    # Normal flow: the AI's own reply (from generate_reply) still goes out
    # via WhatsApp exactly as before — this just confirms it's THAT reply,
    # not the intercept's clarification text.
    webhook_routes.send_whatsapp.assert_called_once()
    assert webhook_routes.send_whatsapp.call_args[0][3] == "ok, got it!"


def test_intercept_off_still_logs_classification(client, monkeypatch):
    monkeypatch.delenv("INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", raising=False)
    _capture_generate_reply(monkeypatch)
    client.post("/webhook", json=_envelope(_text_msg("hello")))
    logged_events = [c.args[0] for c in webhook_routes._log_event.call_args_list]
    assert "intent.classified" in logged_events


# ── Enabled: narrow, guarded intercept ──────────────────────────────────────

def test_intercept_fires_for_unclassifiable_text_while_idle(client, monkeypatch, fake_supabase):
    monkeypatch.setenv("INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", "true")
    fake_supabase.results["carts"] = []  # no state row -> default "browsing" (idle)
    calls = _capture_generate_reply(monkeypatch)

    resp = client.post("/webhook", json=_envelope(_text_msg("asdkjfh qqqq zzz")))

    assert resp.status_code == 200
    assert len(calls) == 0, "generate_reply must NOT run when the intercept fires"
    webhook_routes.send_whatsapp.assert_called_once()
    sent_text = webhook_routes.send_whatsapp.call_args[0][3]
    assert "are you looking for a product" in sent_text.lower()


def test_intercept_does_not_fire_for_confidently_classified_text(client, monkeypatch, fake_supabase):
    monkeypatch.setenv("INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", "true")
    fake_supabase.results["carts"] = []
    calls = _capture_generate_reply(monkeypatch)

    resp = client.post("/webhook", json=_envelope(_text_msg("hello")))

    assert resp.status_code == 200
    assert len(calls) == 1, "a high-confidence greeting must flow through normally"
    webhook_routes.send_whatsapp.assert_called_once()
    assert webhook_routes.send_whatsapp.call_args[0][3] == "ok, got it!"


def test_intercept_never_fires_mid_flow_even_when_unclassifiable(client, monkeypatch, fake_supabase):
    """
    Regression-safety test for the exact risk this design guards against:
    a customer mid-checkout replying with something the stateless
    classifier can't place (e.g. a bare "2") must NEVER be hijacked by
    the generic clarification message — the existing state machine
    already knows how to interpret it in context.
    """
    monkeypatch.setenv("INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", "true")
    fake_supabase.results["carts"] = [
        {"phone": CUSTOMER_PHONE, "business_id": 1, "state_data": {"state": "awaiting_payment"}}
    ]
    calls = _capture_generate_reply(monkeypatch)

    resp = client.post("/webhook", json=_envelope(_text_msg("2")))

    assert resp.status_code == 200
    assert len(calls) == 1, "mid-flow, the state machine must always run — never intercepted"
    webhook_routes.send_whatsapp.assert_called_once()
    assert webhook_routes.send_whatsapp.call_args[0][3] == "ok, got it!"


def test_intercept_never_fires_for_image_messages(client, monkeypatch, fake_supabase):
    monkeypatch.setenv("INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", "true")
    fake_supabase.results["carts"] = []
    calls = _capture_generate_reply(monkeypatch)

    resp = client.post("/webhook", json=_envelope({
        "type": "image", "from": CUSTOMER_PHONE, "id": "wamid.img", "image": {},
    }))

    assert resp.status_code == 200
    assert len(calls) == 1, "media messages must always reach the AI, never be intercepted"


def test_intercept_never_fires_for_agent_messages(client, monkeypatch, fake_supabase):
    monkeypatch.setenv("INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", "true")
    fake_supabase.results["carts"] = []
    biz_with_phone_id = dict(BUSINESS, whatsapp_phone_id=CUSTOMER_PHONE)
    monkeypatch.setattr(crud, "get_business_by_phone_id", lambda pid: biz_with_phone_id)
    calls = _capture_generate_reply(monkeypatch)

    # msg "from" == the business's own whatsapp_phone_id -> is_from_agent
    resp = client.post("/webhook", json=_envelope(_text_msg("asdkjfh qqqq zzz")))

    assert resp.status_code == 200
    assert len(calls) == 1, "an agent-echoed message must never be intercepted as a customer message"
