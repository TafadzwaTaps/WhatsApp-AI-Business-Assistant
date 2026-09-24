"""
tests/test_webhook_media_and_voice.py — Phase 1 regression tests.

Covers the first three Phase 1 fixes to routes/webhook_routes.py:

  1. The media/voice text-overwrite bug: a prior version of receive_message()
     set a type-specific placeholder (e.g. "[voice_note]") and then
     unconditionally overwrote it with the (always-empty, for non-text
     payloads) top-level "text" field — silently dropping every image,
     document, sticker and voice note. These tests drive the real FastAPI
     route end-to-end (signature check disabled, only crud/AI/WhatsApp-send
     mocked) and assert generate_reply() actually receives non-empty text
     for every supported message type.
  2. Whisper transcription now being wired into the live webhook, instead
     of always hitting the "can't process audio yet" placeholder.
  3. Unsupported/invalid message types still being skipped gracefully.

These tests exercise routes/webhook_routes.py directly (not a copy), by
mounting its router on a bare FastAPI app and monkeypatching only the
boundary calls (crud.*, generate_reply, the WhatsApp senders, and the
Whisper transcription helper) — the same real parsing/branching code that
runs in production runs here.
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import crud
import routes.webhook_routes as webhook_routes


BUSINESS = {
    "id": 1,
    "name": "Test Biz",
    "is_active": True,
    "whatsapp_phone_id": "",
    "preferred_language": "en",
    # Pre-fill every field the biz_config self-heal block checks for, so
    # that block's own (unrelated) DB lookup never fires during these tests.
    "currency": "USD",
    "currency_symbol": "$",
    "welcome_message": "",
    "menu_header": "",
    "is_service_business": False,
    "default_slot_mins": 60,
    "pickup_enabled": True,
    "cash_enabled": True,
    "delivery_fee": 0,
    "category": "",
}
CUSTOMER = {"id": 42}
PHONE_NUMBER_ID = "1000000000"
CUSTOMER_PHONE = "263771234567"


def _build_app():
    app = FastAPI()
    app.include_router(webhook_routes.router)
    return app


def _envelope(msg_obj: dict) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                    "messages": [msg_obj],
                }
            }]
        }]
    }


@pytest.fixture
def client(monkeypatch, fake_supabase):
    # Signature check: empty app secret makes verify_meta_signature() a
    # deliberate no-op (dev-mode passthrough) — see services/security.py.
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
    """Patches generate_reply() to record its kwargs and return a fixed
    reply, so each test can assert exactly what text reached the AI."""
    calls = []

    def _fake_generate_reply(**kwargs):
        calls.append(kwargs)
        return "ok, got it!"

    monkeypatch.setattr(webhook_routes, "generate_reply", _fake_generate_reply)
    return calls


# ── 1. Text messages (control case — must keep working) ────────────────────

def test_text_message_reaches_ai_unmodified(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    resp = client.post("/webhook", json=_envelope({
        "type": "text", "from": CUSTOMER_PHONE, "id": "wamid.1",
        "text": {"body": "hello there"},
    }))
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"] == "hello there"


# ── 2. Image / document / sticker — the core overwrite-bug regression ──────

@pytest.mark.parametrize("msg_type,caption,expected_text", [
    ("image", "check this out", "check this out"),
    ("image", "", "[image]"),
    ("document", "invoice", "invoice"),
    ("document", "", "[image]"),
    ("sticker", "", "[image]"),
])
def test_media_message_text_is_not_overwritten_to_empty(client, monkeypatch, msg_type, caption, expected_text):
    """
    Regression test for the Phase 1 bug: text was built per-type, then
    unconditionally overwritten by the (empty, for non-text types)
    top-level "text" field, so the missing-fields check tripped and
    generate_reply() was never called. Now it must always receive the
    per-type placeholder/caption text.
    """
    calls = _capture_generate_reply(monkeypatch)
    media_obj = {"id": "media123"}
    if caption:
        media_obj["caption"] = caption
    resp = client.post("/webhook", json=_envelope({
        "type": msg_type, "from": CUSTOMER_PHONE, "id": f"wamid.{msg_type}",
        msg_type: media_obj,
    }))
    assert resp.status_code == 200
    assert len(calls) == 1, f"generate_reply should have been called for msg_type={msg_type!r}"
    assert calls[0]["message"] == expected_text
    assert calls[0]["message_has_image"] is True


# ── 3. Voice notes — Whisper transcription wired into the webhook ──────────

def test_voice_note_transcription_success_flows_into_ai(client, monkeypatch):
    """When transcription succeeds, the transcript — not the
    "[voice_note]" placeholder — must be what reaches generate_reply()."""
    calls = _capture_generate_reply(monkeypatch)

    async def _fake_transcribe(*, media_id, wa_token, language):
        assert media_id == "audiomedia1"
        assert wa_token == "fake-wa-token"
        return "please order two burgers"

    import services.whatsapp_service as whatsapp_service
    monkeypatch.setattr(whatsapp_service, "transcribe_whatsapp_voice_note", _fake_transcribe)

    resp = client.post("/webhook", json=_envelope({
        "type": "audio", "from": CUSTOMER_PHONE, "id": "wamid.voice1",
        "audio": {"id": "audiomedia1"},
    }))
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"] == "please order two burgers"
    assert calls[0]["voice_transcript"] == "please order two burgers"


def test_voice_note_transcription_failure_falls_back_gracefully(client, monkeypatch):
    """When transcription fails/returns nothing, generate_reply() must NOT
    be called — the webhook sends the friendly fallback message directly
    and never lets an empty/placeholder string reach the AI engine."""
    calls = _capture_generate_reply(monkeypatch)

    async def _fake_transcribe(*, media_id, wa_token, language):
        return None

    import services.whatsapp_service as whatsapp_service
    monkeypatch.setattr(whatsapp_service, "transcribe_whatsapp_voice_note", _fake_transcribe)

    resp = client.post("/webhook", json=_envelope({
        "type": "audio", "from": CUSTOMER_PHONE, "id": "wamid.voice2",
        "audio": {"id": "audiomedia2"},
    }))
    assert resp.status_code == 200
    assert len(calls) == 0
    webhook_routes.send_whatsapp.assert_called_once()
    sent_text = webhook_routes.send_whatsapp.call_args[0][3]
    assert "couldn't understand" in sent_text


# ── 4. Newly-supported types (video/location/contacts/interactive) ─────────

@pytest.mark.parametrize("msg_obj,expected_text", [
    ({"type": "video", "video": {"caption": "watch this"}}, "watch this"),
    ({"type": "video", "video": {}}, "[video]"),
    ({"type": "location", "location": {"latitude": 1, "longitude": 2}}, "[location]"),
    ({"type": "contacts", "contacts": [{}]}, "[contact_card]"),
    ({"type": "interactive", "interactive": {"type": "button_reply",
      "button_reply": {"id": "opt1", "title": "Yes please"}}}, "Yes please"),
])
def test_previously_dropped_message_types_now_reach_ai(client, monkeypatch, msg_obj, expected_text):
    calls = _capture_generate_reply(monkeypatch)
    full_msg = {"from": CUSTOMER_PHONE, "id": "wamid.newtype", **msg_obj}
    resp = client.post("/webhook", json=_envelope(full_msg))
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"] == expected_text


# ── 5. Invalid / unsupported message types are skipped, not crashed on ─────

def test_unsupported_message_type_is_skipped_not_crashed(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    resp = client.post("/webhook", json=_envelope({
        "type": "system", "from": CUSTOMER_PHONE, "id": "wamid.weird",
    }))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert len(calls) == 0


def test_malformed_payload_does_not_crash(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    resp = client.post("/webhook", json={"entry": []})
    assert resp.status_code == 200
    assert len(calls) == 0


def test_empty_media_with_no_id_and_no_caption_still_handled(client, monkeypatch):
    """An image message with no caption and (edge case) missing media id —
    should still resolve to the "[image]" placeholder and reach the AI
    rather than being dropped as "missing required fields"."""
    calls = _capture_generate_reply(monkeypatch)
    resp = client.post("/webhook", json=_envelope({
        "type": "image", "from": CUSTOMER_PHONE, "id": "wamid.bare",
        "image": {},
    }))
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"] == "[image]"


# ── 6. Webhook verification (GET /webhook) ──────────────────────────────────

def test_webhook_verify_succeeds_with_matching_token(monkeypatch):
    monkeypatch.setattr(webhook_routes, "VERIFY_TOKEN", "myverifytoken123")
    client = TestClient(_build_app())
    resp = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "myverifytoken123",
        "hub.challenge": "challenge-abc-123",
    })
    assert resp.status_code == 200
    assert resp.text == "challenge-abc-123"


def test_webhook_verify_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(webhook_routes, "VERIFY_TOKEN", "myverifytoken123")
    client = TestClient(_build_app())
    resp = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong-token",
        "hub.challenge": "challenge-abc-123",
    })
    assert resp.status_code == 403


def test_webhook_post_rejects_invalid_signature(monkeypatch, fake_supabase):
    """When an app secret IS configured, a bad/missing signature must be
    rejected with 403 rather than silently accepted."""
    monkeypatch.setattr(webhook_routes, "WHATSAPP_APP_SECRET", "a-real-secret")
    client = TestClient(_build_app())
    resp = client.post("/webhook", json=_envelope({
        "type": "text", "from": CUSTOMER_PHONE, "id": "wamid.sig",
        "text": {"body": "hi"},
    }), headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert resp.status_code == 403
