"""
tests/test_broadcast_shared_number_fallback.py

GAP FOUND (support issue): POST /business/broadcast only ever checked the
business's own dedicated whatsapp_token/whatsapp_phone_id and hard-failed
with "WhatsApp token not configured" otherwise — even for businesses using
WaziBot's shared WhatsApp number (the default, no-Meta-account-needed
onboarding option), which is the majority case. Every other outbound-
message code path (routes/chat_routes.py's send_message()) already falls
back to the module-level SHARED_WA_TOKEN/SHARED_PHONE_NUMBER_ID in this
exact situation. This test proves broadcast() now does the same, without
touching the dedicated-number path's existing behavior.

Calls broadcast() directly (bypassing the FastAPI dependency layer, which
only gates rate-limiting/plan/restriction checks that aren't relevant to
this fix) — same "call the route function directly" approach already used
elsewhere in this codebase for testing logic that lives in a route body.
"""

import pytest

import routes.business_routes as br


DEDICATED_BIZ = {"id": 1, "whatsapp_phone_id": "PHONE123", "whatsapp_token": "encrypted-blob"}
SHARED_ONLY_BIZ = {"id": 2, "whatsapp_phone_id": None, "whatsapp_token": None}
NO_CREDS_BIZ = {"id": 3, "whatsapp_phone_id": None, "whatsapp_token": None}


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(br, "_rate_check", lambda *a, **k: None)


@pytest.fixture
def sent_calls(monkeypatch):
    calls = []

    def _fake_send_whatsapp(phone_number_id, token, phone, message):
        calls.append({"phone_number_id": phone_number_id, "token": token, "phone": phone})
        return {"status": "sent"}

    monkeypatch.setattr(br, "send_whatsapp", _fake_send_whatsapp)
    return calls


def _broadcast(business, message="Hello there, thanks for shopping with us!"):
    body = br.BroadcastRequest(message=message)
    user = {"business_id": business["id"]}
    return br.broadcast(body, request=None, user=user, _plan=None, _restrict=None)


def test_dedicated_number_path_is_unaffected(monkeypatch, sent_calls):
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: DEDICATED_BIZ)
    monkeypatch.setattr(br.crud, "get_decrypted_token", lambda business: "real-dedicated-token")
    monkeypatch.setattr(br.crud, "get_all_customer_phones", lambda bid: ["+1555000111"])
    monkeypatch.setattr(br.crud, "log_message", lambda *a, **k: None)
    br.SHARED_WA_TOKEN = ""
    br.SHARED_PHONE_NUMBER_ID = ""

    result = _broadcast(DEDICATED_BIZ)

    assert result["sent"] == 1
    assert sent_calls[0]["phone_number_id"] == "PHONE123"
    assert sent_calls[0]["token"] == "real-dedicated-token"


def test_shared_number_business_can_now_broadcast(monkeypatch, sent_calls):
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: SHARED_ONLY_BIZ)
    monkeypatch.setattr(br.crud, "get_decrypted_token", lambda business: "")
    monkeypatch.setattr(br.crud, "get_all_customer_phones", lambda bid: ["+1555000222"])
    monkeypatch.setattr(br.crud, "log_message", lambda *a, **k: None)
    br.SHARED_WA_TOKEN = "shared-token-abc"
    br.SHARED_PHONE_NUMBER_ID = "SHARED_PHONE_ID"

    result = _broadcast(SHARED_ONLY_BIZ)

    assert result["sent"] == 1
    assert result["failed"] == 0
    assert sent_calls[0]["phone_number_id"] == "SHARED_PHONE_ID"
    assert sent_calls[0]["token"] == "shared-token-abc"


def test_no_dedicated_and_no_shared_still_fails_with_clear_error(monkeypatch, sent_calls):
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: NO_CREDS_BIZ)
    monkeypatch.setattr(br.crud, "get_decrypted_token", lambda business: "")
    br.SHARED_WA_TOKEN = ""
    br.SHARED_PHONE_NUMBER_ID = ""

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _broadcast(NO_CREDS_BIZ)

    assert exc_info.value.status_code == 400
    assert sent_calls == []


def test_dedicated_token_present_but_phone_id_missing_falls_back_to_shared(monkeypatch, sent_calls):
    """A business could have a saved token but no phone_id (e.g. mid-setup,
    or migrating away from shared) — this must still safely fall back to
    the shared number rather than crashing or silently using a None
    phone_id."""
    biz = {"id": 4, "whatsapp_phone_id": None, "whatsapp_token": "encrypted-blob"}
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda bid: biz)
    monkeypatch.setattr(br.crud, "get_decrypted_token", lambda business: "real-token-no-phone-id")
    monkeypatch.setattr(br.crud, "get_all_customer_phones", lambda bid: ["+1555000333"])
    monkeypatch.setattr(br.crud, "log_message", lambda *a, **k: None)
    br.SHARED_WA_TOKEN = "shared-token-abc"
    br.SHARED_PHONE_NUMBER_ID = "SHARED_PHONE_ID"

    result = _broadcast(biz)

    assert result["sent"] == 1
    assert sent_calls[0]["phone_number_id"] == "SHARED_PHONE_ID"
    assert sent_calls[0]["token"] == "shared-token-abc"
