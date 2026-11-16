"""UI follow-ups: handoff_reason in /chat/conversations rows and *_configured flags on /me."""
import crud.messages as messages_mod
from test_inbox_latest_chats_and_scrolling import _FakeSupabase


def _rows(monkeypatch, carts):
    tables = {
        "customers": [{"id": 1, "business_id": 7, "phone": "111", "last_seen": "2026-01-01"},
                      {"id": 2, "business_id": 7, "phone": "222", "last_seen": "2026-01-02"},
                      {"id": 3, "business_id": 7, "phone": "333", "last_seen": "2026-01-03"}],
        "messages": [],
        "carts": carts,
    }
    monkeypatch.setattr(messages_mod, "supabase", _FakeSupabase(tables))
    return {r["phone"]: r for r in messages_mod.get_chat_conversations(7)}


def test_handoff_reason_from_both_storage_shapes(monkeypatch):
    rows = _rows(monkeypatch, [
        {"business_id": 7, "phone": "111", "state_data": {"state": "human_handoff", "handoff_reason": "Refund request"}},
        {"business_id": 7, "phone": "222", "state_data": {"state": "human_handoff", "session": {"handoff_reason": "Complex request"}}},
        {"business_id": 7, "phone": "333", "state_data": {"state": "idle", "handoff_reason": "stale"}},
    ])
    assert rows["111"]["handoff_reason"] == "Refund request"
    assert rows["222"]["handoff_reason"] == "Complex request"
    assert rows["333"]["in_handoff"] is False and rows["333"]["handoff_reason"] == ""


def test_reason_missing_is_empty_string_and_other_tenant_ignored(monkeypatch):
    rows = _rows(monkeypatch, [
        {"business_id": 7, "phone": "111", "state_data": {"state": "human_handoff"}},
        {"business_id": 99, "phone": "222", "state_data": {"state": "human_handoff", "handoff_reason": "x"}},
    ])
    assert rows["111"]["in_handoff"] is True and rows["111"]["handoff_reason"] == ""
    assert rows["222"]["in_handoff"] is False


def test_me_exposes_configured_flags(monkeypatch):
    import routes.business_routes as br
    biz = {"id": 7, "ecocash_number": " 0771 ", "paypal_email": "", "bank_transfer_details": None,
           "blik_number": "123456", "owner_password": "x", "whatsapp_token": "t", "features_json": {}}
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda _id: dict(biz))
    out = br.get_me({"business_id": 7})
    assert out["ecocash_configured"] is True
    assert out["paypal_configured"] is False
    assert out["bank_transfer_configured"] is False
    assert out["blik_configured"] is True
    assert "owner_password" not in out and "whatsapp_token" not in out
