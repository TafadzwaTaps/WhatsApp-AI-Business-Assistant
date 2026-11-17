"""Phase 2 (WhatsApp reliability): batch processing, handoff safety, delivery status,
duplicate detection, per-customer serialisation and 429 backoff. WhatsApp is mocked;
nothing is sent."""
import asyncio
import types
from unittest.mock import MagicMock

import pytest

import crud.messages as messages_mod
import routes.webhook_routes as wh

# Reuse the existing mocked-webhook harness.
from test_webhook_media_and_voice import (   # noqa: F401  (client is a pytest fixture)
    client, _envelope, _capture_generate_reply, CUSTOMER_PHONE, PHONE_NUMBER_ID,
)


def _text(wa_id, body):
    return {"type": "text", "from": CUSTOMER_PHONE, "id": wa_id, "text": {"body": body}}


# ── W1: every message in a delivery is processed ────────────────────────────

def test_batch_of_messages_in_one_change_all_reach_the_ai(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    payload = _envelope(_text("wamid.1", "first"))
    payload["entry"][0]["changes"][0]["value"]["messages"].append(_text("wamid.2", "second"))
    assert client.post("/webhook", json=payload).status_code == 200
    assert [c["message"] for c in calls] == ["first", "second"]


def test_multiple_entries_and_changes_are_all_processed(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    p1 = _envelope(_text("wamid.1", "one"))
    p2 = _envelope(_text("wamid.2", "two"))
    p1["entry"].append(p2["entry"][0])
    p1["entry"][0]["changes"].append(_envelope(_text("wamid.3", "three"))["entry"][0]["changes"][0])
    client.post("/webhook", json=p1)
    assert sorted(c["message"] for c in calls) == ["one", "three", "two"]


def test_one_crashing_message_does_not_drop_the_rest(client, monkeypatch):
    seen = []

    def flaky(**kw):
        seen.append(kw["message"])
        if kw["message"] == "boom":
            raise RuntimeError("ai exploded")
        return "ok"
    monkeypatch.setattr(wh, "generate_reply", flaky)
    payload = _envelope(_text("wamid.1", "boom"))
    payload["entry"][0]["changes"][0]["value"]["messages"].append(_text("wamid.2", "after"))
    assert client.post("/webhook", json=payload).status_code == 200
    assert seen == ["boom", "after"]


def test_malformed_payloads_return_200_and_do_nothing(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    for bad in ({}, {"entry": "x"}, {"entry": [None]}, {"entry": [{"changes": [{"value": 5}]}]},
                {"entry": [{"changes": [{"value": {"messages": ["nope"]}}]}]}, [1, 2]):
        assert client.post("/webhook", json=bad).status_code == 200
    assert calls == []


def test_split_preserves_envelope():
    data = _envelope(_text("a", "x"))
    data["entry"][0]["changes"][0]["value"]["contacts"] = [{"wa_id": "1"}]
    data["entry"][0]["changes"][0]["value"]["messages"].append(_text("b", "y"))
    parts = list(wh._split_webhook_payload(data))
    assert len(parts) == 2
    assert all(p["entry"][0]["changes"][0]["value"]["contacts"] == [{"wa_id": "1"}] for p in parts)
    assert [p["entry"][0]["changes"][0]["value"]["messages"][0]["id"] for p in parts] == ["a", "b"]


# ── W2/W3: never speak over a human agent ───────────────────────────────────

def test_unreadable_state_means_silence_not_a_reply(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    import services._ai_state as st
    monkeypatch.setattr(st, "is_in_human_handoff", lambda phone, bid: None)
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert calls == []
    wh.send_whatsapp.assert_not_called()


def test_voice_fallback_is_not_sent_during_handoff(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    import services._ai_state as st
    monkeypatch.setattr(st, "is_in_human_handoff", lambda phone, bid: True)
    voice = {"type": "audio", "from": CUSTOMER_PHONE, "id": "wamid.v", "audio": {"id": "m1"}}
    client.post("/webhook", json=_envelope(voice))
    wh.send_whatsapp.assert_not_called()


def test_voice_fallback_still_sent_when_not_in_handoff(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    import services._ai_state as st
    monkeypatch.setattr(st, "is_in_human_handoff", lambda phone, bid: False)
    voice = {"type": "audio", "from": CUSTOMER_PHONE, "id": "wamid.v", "audio": {"id": "m1"}}
    client.post("/webhook", json=_envelope(voice))
    assert wh.send_whatsapp.called


def test_probe_reports_none_on_db_error_and_true_for_handoff(monkeypatch):
    import services._ai_state as st
    import core.db as db

    class Boom:
        def table(self, *_):
            raise RuntimeError("db down")
    monkeypatch.setattr(db, "supabase", Boom())
    assert st.is_in_human_handoff("1", 1) is None

    class Row:
        def __init__(self, sd): self.sd = sd
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def limit(self, *_): return self
        def execute(self): return types.SimpleNamespace(data=[{"state_data": self.sd}])
    monkeypatch.setattr(db, "supabase", Row({"state": "human_handoff"}))
    assert st.is_in_human_handoff("1", 1) is True
    monkeypatch.setattr(db, "supabase", Row({"state": "browsing"}))
    assert st.is_in_human_handoff("1", 1) is False


# ── W5: duplicate detection by SQLSTATE ─────────────────────────────────────

def test_duplicate_key_detection():
    class PgErr(Exception):
        def __init__(self, code, msg): super().__init__(msg); self.code = code
    assert wh._is_duplicate_key_error(PgErr("23505", "x"))
    assert wh._is_duplicate_key_error(Exception({"code": "23505", "message": "dup"}))
    assert wh._is_duplicate_key_error(Exception('duplicate key value violates unique constraint "uq_messages_wa_message_id"'))
    assert not wh._is_duplicate_key_error(Exception("connection reset"))
    assert not wh._is_duplicate_key_error(PgErr("42P01", "relation does not exist"))


# ── W7: per-customer serialisation ──────────────────────────────────────────

def test_same_sender_shares_one_lock_different_senders_do_not():
    async def go():
        a1 = wh._customer_lock("111")
        async with a1:
            assert wh._customer_lock("111") is a1          # same lock while held
            assert wh._customer_lock("222") is not a1
    asyncio.new_event_loop().run_until_complete(go())


def test_messages_from_one_customer_are_never_processed_concurrently(monkeypatch):
    order = []

    async def fake_handle(single):
        order.append(("start", single["id"]))
        await asyncio.sleep(0.01)
        order.append(("end", single["id"]))

    async def run():
        async def one(i):
            async with wh._customer_lock("same"):
                await fake_handle({"id": i})
        await asyncio.gather(one(1), one(2), one(3))
    asyncio.new_event_loop().run_until_complete(run())
    for i in range(0, 6, 2):                      # strictly start/end pairs, never interleaved
        assert order[i][0] == "start" and order[i + 1] == ("end", order[i][1])


# ── W4: delivery status ─────────────────────────────────────────────────────

class _Tbl:
    def __init__(self, rows): self.rows, self._f, self._upd = rows, [], None
    def select(self, *_): return self
    def eq(self, col, val): self._f.append((col, val)); return self
    def limit(self, *_): return self
    def update(self, patch): self._upd = patch; return self
    def execute(self):
        hit = [r for r in self.rows if all(r.get(c) == v for c, v in self._f)]
        if self._upd is not None:
            for r in hit: r.update(self._upd)
        return types.SimpleNamespace(data=hit)


class _Db:
    def __init__(self, rows): self.rows = rows
    def table(self, name): return _Tbl(self.rows)


def test_status_progression_never_goes_backwards(monkeypatch):
    rows = [{"id": 1, "wa_message_id": "w1", "status": "sent", "direction": "outgoing"}]
    monkeypatch.setattr(messages_mod, "supabase", _Db(rows))
    assert messages_mod.apply_delivery_status("w1", "delivered") is True
    assert messages_mod.apply_delivery_status("w1", "read") is True
    assert messages_mod.apply_delivery_status("w1", "delivered") is False   # late receipt ignored
    assert rows[0]["status"] == "read"
    assert messages_mod.apply_delivery_status("w1", "failed") is False      # failure cannot undo read
    assert rows[0]["status"] == "read"


def test_failed_status_recorded_and_unknown_inputs_ignored(monkeypatch):
    rows = [{"id": 1, "wa_message_id": "w1", "status": "sent", "direction": "outgoing"}]
    monkeypatch.setattr(messages_mod, "supabase", _Db(rows))
    assert messages_mod.apply_delivery_status("w1", "failed", "131047: window") is True
    assert rows[0]["status"] == "failed"
    assert messages_mod.apply_delivery_status("nope", "read") is False
    assert messages_mod.apply_delivery_status("w1", "weird") is False
    assert messages_mod.apply_delivery_status("", "read") is False


def test_receipts_never_modify_incoming_rows(monkeypatch):
    rows = [{"id": 1, "wa_message_id": "w1", "status": "sent", "direction": "incoming"}]
    monkeypatch.setattr(messages_mod, "supabase", _Db(rows))
    assert messages_mod.apply_delivery_status("w1", "read") is False
    assert rows[0]["status"] == "sent"


def test_missing_column_error_is_not_mistaken_for_a_duplicate():
    assert not wh._is_duplicate_key_error(Exception('column messages.wa_message_id does not exist'))
    assert wh._is_duplicate_key_error(Exception("unique_violation"))   # the real Postgres condition name


def test_probe_is_retried_once_before_staying_silent(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    import services._ai_state as st
    seq = iter([None, False])
    monkeypatch.setattr(st, "is_in_human_handoff", lambda phone, bid: next(seq))
    client.post("/webhook", json=_envelope(_text("wamid.1", "hi")))
    assert len(calls) == 1          # transient failure on the first read, second read succeeded


def test_status_only_webhook_updates_messages(client, monkeypatch):
    seen = []
    monkeypatch.setattr(messages_mod, "apply_delivery_status", lambda w, s, d=None: seen.append((w, s, d)))
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.A", "status": "delivered"},
        {"id": "wamid.B", "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]},
        {"status": "read"},
    ]}}]}]}
    calls = _capture_generate_reply(monkeypatch)
    assert client.post("/webhook", json=payload).status_code == 200
    assert calls == []
    assert seen[0] == ("wamid.A", "delivered", None)
    assert seen[1][0:2] == ("wamid.B", "failed") and "131047" in seen[1][2]
    assert len(seen) == 2


def test_outgoing_wamid_is_stored_after_send(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    stored = []
    monkeypatch.setattr(messages_mod, "set_message_wa_id", lambda mid, wid: stored.append((mid, wid)))
    client.post("/webhook", json=_envelope(_text("wamid.1", "hello")))
    assert stored == [(1, "wamid_out")]


# ── W9: rate-limit backoff ──────────────────────────────────────────────────

def _resp(status, body):
    return types.SimpleNamespace(status_code=status, json=lambda: body)


def test_send_whatsapp_retries_on_429_and_rate_limit_codes(monkeypatch):
    import main
    ok = {"messages": [{"id": "wamid.ok"}]}
    for first in (_resp(429, {"error": {"code": 4}}), _resp(400, {"error": {"code": 130429}})):
        seq = [first, _resp(200, ok)]
        posted = []
        monkeypatch.setattr(main.http_requests, "post", lambda *a, **k: (posted.append(1), seq.pop(0))[1])
        monkeypatch.setattr("time.sleep", lambda s: None)
        assert main.send_whatsapp("pid", "tok", "263700000000", "hi") == ok
        assert len(posted) == 2


def test_send_whatsapp_still_does_not_retry_real_client_errors(monkeypatch):
    import main
    posted = []
    bad = {"error": {"code": 131030, "message": "recipient not in allowed list"}}
    monkeypatch.setattr(main.http_requests, "post", lambda *a, **k: (posted.append(1), _resp(400, bad))[1])
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert main.send_whatsapp("pid", "tok", "263700000000", "hi", _shared_fallback=False) == bad
    assert len(posted) == 1


# ── W8: a failed send is recorded, not retried by Meta ─────────────────────

def test_failed_send_marks_the_stored_reply_failed(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    wh.send_whatsapp.return_value = {"error": "boom"}
    failed = []
    monkeypatch.setattr(messages_mod, "mark_message_failed", lambda mid: failed.append(mid))
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hello"))).status_code == 200
    assert failed == [1]


def test_successful_send_is_not_marked_failed(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    failed = []
    monkeypatch.setattr(messages_mod, "mark_message_failed", lambda mid: failed.append(mid))
    client.post("/webhook", json=_envelope(_text("wamid.1", "hello")))
    assert failed == []


def test_mark_message_failed_updates_status_and_never_raises(monkeypatch):
    rows = [{"id": 7, "status": "sent", "direction": "outgoing"}]
    monkeypatch.setattr(messages_mod, "supabase", _Db(rows))
    messages_mod.mark_message_failed(7)
    assert rows[0]["status"] == "failed"

    class Boom:
        def table(self, *_): raise RuntimeError("db down")
    monkeypatch.setattr(messages_mod, "supabase", Boom())
    messages_mod.mark_message_failed(7)          # must not raise
    messages_mod.mark_message_failed(None)
