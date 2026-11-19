"""Phase 1 (reliability): transient failures are retried by Meta, duplicates stay harmless,
send retries cannot double-deliver, lost replies are detectable and recoverable.
WhatsApp, the AI and the database are all mocked; nothing is sent."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import requests

import crud
import crud.messages as messages_mod
import routes.webhook_routes as wh

from test_webhook_media_and_voice import (   # noqa: F401  (client is a pytest fixture)
    client, _envelope, _capture_generate_reply, CUSTOMER_PHONE, PHONE_NUMBER_ID,
)


def _text(wa_id, body):
    return {"type": "text", "from": CUSTOMER_PHONE, "id": wa_id, "text": {"body": body}}


class _Net(Exception):
    pass


# ── transient-error classification ────────────────────────────────────────────
@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("read timed out"), True),
    (ConnectionError("Connection reset by peer"), True),
    (Exception("503 Service Unavailable"), True),
    (Exception("server disconnected without sending a response"), True),
    (Exception('duplicate key value violates unique constraint "x"'), False),
    (Exception("invalid input syntax for type integer"), False),
    (Exception("null value in column violates not-null constraint"), False),
])
def test_transient_classification(exc, expected):
    assert wh._is_transient_error(exc) is expected


def test_postgres_connection_class_codes_are_transient():
    class E(Exception):
        code = "08006"
    class P(Exception):
        code = "23502"
    assert wh._is_transient_error(E("x"))
    assert not wh._is_transient_error(P("not null"))


def test_duplicate_is_never_transient():
    assert not wh._is_transient_error(Exception("duplicate key value violates unique constraint"))


# ── F8: lookups before anything is stored ─────────────────────────────────────
def test_transient_customer_lookup_failure_answers_503_and_never_calls_the_ai(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)

    def boom(*_a, **_k):
        raise _Net("connection reset by peer")
    monkeypatch.setattr(crud, "get_or_create_customer", boom)
    r = client.post("/webhook", json=_envelope(_text("wamid.1", "hi")))
    assert r.status_code == 503
    assert calls == []


def test_permanent_customer_lookup_failure_still_answers_200(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)

    def boom(*_a, **_k):
        raise ValueError("invalid input syntax for type integer")
    monkeypatch.setattr(crud, "get_or_create_customer", boom)
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert calls == []


def test_transient_business_lookup_failure_answers_503(client, monkeypatch):
    def boom(_pid):
        raise _Net("timed out")
    monkeypatch.setattr(crud, "get_business_by_phone_id", boom)
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 503


def test_unknown_business_is_still_200(client, monkeypatch):
    monkeypatch.setattr(crud, "get_business_by_phone_id", lambda pid: None)
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200


# ── F9: storing the incoming message ──────────────────────────────────────────
def test_transient_failure_storing_the_message_answers_503_without_a_reply(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)

    def boom(*_a, **_k):
        raise _Net("connection timeout")
    monkeypatch.setattr(crud, "create_message", boom)
    r = client.post("/webhook", json=_envelope(_text("wamid.1", "hi")))
    assert r.status_code == 503
    assert calls == []
    wh.send_whatsapp.assert_not_called()


def test_permanent_failure_storing_the_message_keeps_existing_behaviour(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    state = {"n": 0}

    def flaky(*_a, **kw):
        state["n"] += 1
        if kw.get("sender_type") is None and state["n"] == 1:
            raise ValueError("invalid input syntax")
        return {"id": state["n"]}
    monkeypatch.setattr(crud, "create_message", flaky)
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert len(calls) == 1                      # unchanged: it still answers


def test_duplicate_at_insert_is_200_no_reply_and_no_legacy_log_row(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    logged = []
    monkeypatch.setattr(crud, "log_message", lambda *a, **k: logged.append(a))

    def dup(*_a, **_k):
        raise Exception('duplicate key value violates unique constraint "messages_wa_message_id_key"')
    monkeypatch.setattr(crud, "create_message", dup)
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert calls == [] and logged == []


def test_legacy_log_failure_does_not_block_the_reply(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("chat_messages unavailable")
    monkeypatch.setattr(crud, "log_message", boom)
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert len(calls) == 1


def test_message_is_stored_before_the_legacy_log(client, monkeypatch):
    order = []
    monkeypatch.setattr(crud, "create_message", lambda *a, **k: order.append(("msg", k.get("sender_type"))) or {"id": 1})
    monkeypatch.setattr(crud, "log_message", lambda *a, **k: order.append(("log", a[2])))
    _capture_generate_reply(monkeypatch)
    client.post("/webhook", json=_envelope(_text("wamid.1", "hi")))
    assert order[0] == ("msg", None) and order[1] == ("log", "in")


# ── batches and redelivery ────────────────────────────────────────────────────
def test_batch_finishes_the_rest_then_asks_meta_to_retry(client, monkeypatch):
    seen = []

    def gen(**kw):
        seen.append(kw["message"])
        return "ok"
    monkeypatch.setattr(wh, "generate_reply", gen)
    real = {"n": 0}

    def get_customer(phone, biz):
        real["n"] += 1
        if real["n"] == 1:
            raise _Net("connection reset")
        return {"id": 42}
    monkeypatch.setattr(crud, "get_or_create_customer", get_customer)
    payload = _envelope(_text("wamid.1", "first"))
    payload["entry"][0]["changes"][0]["value"]["messages"].append(_text("wamid.2", "second"))
    r = client.post("/webhook", json=payload)
    assert r.status_code == 503
    assert seen == ["second"]                   # the healthy message was not dropped


def test_redelivery_after_503_is_processed_once(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    stored = set()
    attempts = {"n": 0}

    def get_customer(phone, biz):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _Net("timed out")
        return {"id": 42}

    def create(*a, **kw):
        wid = kw.get("wa_message_id")
        if wid:
            stored.add(wid)
        return {"id": len(stored) + 1}
    monkeypatch.setattr(crud, "get_or_create_customer", get_customer)
    monkeypatch.setattr(crud, "create_message", create)
    monkeypatch.setattr(crud, "message_exists", lambda wid: wid in stored)
    payload = _envelope(_text("wamid.9", "hello"))
    assert client.post("/webhook", json=payload).status_code == 503
    assert client.post("/webhook", json=payload).status_code == 200      # Meta redelivers
    assert len(calls) == 1
    assert client.post("/webhook", json=payload).status_code == 200      # and again: no new reply
    assert len(calls) == 1


def test_ai_crash_after_storing_replies_once_with_the_safe_fallback(client, monkeypatch):
    def boom(**_kw):
        raise RuntimeError("llm exploded")
    monkeypatch.setattr(wh, "generate_reply", boom)
    r = client.post("/webhook", json=_envelope(_text("wamid.1", "hi")))
    assert r.status_code == 200
    assert wh.send_whatsapp.call_count == 1


def test_whatsapp_send_failure_marks_reply_failed_and_stays_200(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    wh.send_whatsapp.return_value = {"error": {"code": 190, "message": "token"}}
    failed = []
    monkeypatch.setattr(crud.messages, "mark_message_failed", lambda mid: failed.append(mid))
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert failed == [1]


def test_status_only_payload_never_calls_the_ai(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    body = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "delivered"}]}}]}]}
    assert client.post("/webhook", json=body).status_code == 200
    assert calls == []


# ── F6: 24h window ────────────────────────────────────────────────────────────
def test_window_closed_error_is_labelled_and_still_200(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    wh.send_whatsapp.return_value = {"error": {"code": 131047, "message": "Re-engagement message"}}
    failed = []
    monkeypatch.setattr(crud.messages, "mark_message_failed", lambda mid: failed.append(mid))
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert failed == [1]
    events = [c.args[0] for c in wh._log_event.call_args_list]
    assert "wa.window_closed" in events


def test_wa_error_code_helper():
    assert wh._wa_error_code({"error": {"code": 131047}}) == 131047
    assert wh._wa_error_code({"error": "text"}) is None
    assert wh._wa_error_code({"messages": []}) is None
    assert wh._wa_error_code(None) is None


# ── F3: send retry policy ─────────────────────────────────────────────────────
def _send_module(monkeypatch, post):
    import main
    monkeypatch.setattr(main.http_requests, "post", post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return main


def test_read_timeout_is_not_retried_because_delivery_is_unknown(monkeypatch):
    posts = []

    def post(*a, **k):
        posts.append(1)
        raise requests.exceptions.ReadTimeout("read timed out")
    main = _send_module(monkeypatch, post)
    out = main.send_whatsapp("1", "tok", "2637", "hello")
    assert len(posts) == 1
    assert "error" in out and out.get("ambiguous") is True


def test_connect_errors_are_still_retried(monkeypatch):
    posts = []

    def post(*a, **k):
        posts.append(1)
        raise requests.exceptions.ConnectTimeout("connect timed out")
    main = _send_module(monkeypatch, post)
    out = main.send_whatsapp("1", "tok", "2637", "hello")
    assert len(posts) == 3 and "error" in out


def test_5xx_is_still_retried_then_succeeds(monkeypatch):
    seq = [500, 200]

    def post(*a, **k):
        r = MagicMock()
        r.status_code = seq.pop(0)
        r.json.return_value = {"messages": [{"id": "w"}]} if r.status_code == 200 else {"error": {"code": 1}}
        return r
    main = _send_module(monkeypatch, post)
    assert main.send_whatsapp("1", "tok", "2637", "hello").get("messages")


def test_worker_count_warning_exists_in_startup():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf8")
    assert "WEB_CONCURRENCY" in src and "single worker process" in src


# ── F1: detecting lost replies ────────────────────────────────────────────────
def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class _Q:
    """Filter-aware fake of the supabase query builder for the messages table."""
    def __init__(self, rows):
        self.rows, self.f, self._limit, self._order = rows, [], None, None

    def select(self, *_a, **_k):
        return self

    def eq(self, c, v):
        self.f.append(lambda r: r.get(c) == v)
        return self

    def gte(self, c, v):
        self.f.append(lambda r: (r.get(c) or "") >= v)
        return self

    def lte(self, c, v):
        self.f.append(lambda r: (r.get(c) or "") <= v)
        return self

    def in_(self, c, vals):
        self.f.append(lambda r: r.get(c) in vals)
        return self

    def order(self, c, desc=False):
        self._order = (c, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = [r for r in self.rows if all(f(r) for f in self.f)]
        if self._order:
            rows = sorted(rows, key=lambda r: r[self._order[0]], reverse=self._order[1])
        if self._limit:
            rows = rows[: self._limit]

        class R:
            pass
        out = R()
        out.data = rows
        return out


class _DB:
    def __init__(self, messages, customers=None, carts=None):
        self.messages, self.customers, self.carts = messages, customers or [], carts or []

    def table(self, name):
        return _Q({"messages": self.messages, "customers": self.customers, "carts": self.carts}.get(name, []))


def _msg(i, cid, direction, minutes_ago, text="x", biz=1):
    return {"id": i, "customer_id": cid, "business_id": biz, "direction": direction,
            "text": text, "created_at": _iso(minutes_ago), "wa_message_id": f"w{i}"}


def test_unanswered_inbound_detection(monkeypatch):
    rows = [
        _msg(1, 10, "incoming", 20, "lost reply"),                # unanswered
        _msg(2, 11, "incoming", 20, "answered"),
        _msg(3, 11, "outgoing", 19),
        _msg(4, 12, "incoming", 1, "too fresh"),                  # may still be in flight
        _msg(5, 13, "incoming", 25, "old, then newer"),
        _msg(6, 13, "incoming", 1, "newer in flight"),            # newest is fresh: hide the customer
        _msg(7, 14, "incoming", 20, "other tenant", biz=2),
    ]
    monkeypatch.setattr(messages_mod, "supabase", _DB(rows))
    got = messages_mod.get_unanswered_inbound(max_age_minutes=30, min_age_minutes=3)
    assert sorted(r["id"] for r in got) == [1, 7]
    only_biz1 = messages_mod.get_unanswered_inbound(max_age_minutes=30, min_age_minutes=3, business_id=1)
    assert [r["id"] for r in only_biz1] == [1]


def test_old_messages_outside_window_are_ignored(monkeypatch):
    monkeypatch.setattr(messages_mod, "supabase", _DB([_msg(1, 10, "incoming", 300)]))
    assert messages_mod.get_unanswered_inbound(max_age_minutes=30) == []


def test_has_reply_since(monkeypatch):
    rows = [_msg(1, 10, "incoming", 20), _msg(2, 10, "outgoing", 10)]
    monkeypatch.setattr(messages_mod, "supabase", _DB(rows))
    assert messages_mod.has_reply_since(10, 1, _iso(20)) is True
    assert messages_mod.has_reply_since(10, 1, _iso(5)) is False
    assert messages_mod.has_reply_since(10, 2, _iso(20)) is False      # other tenant


def test_chat_unanswered_endpoint_is_scoped_to_the_callers_business(monkeypatch):
    from routes import chat_routes
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return [{"customer_id": 10, "id": 1, "text": "t" * 500, "created_at": _iso(10)}]
    monkeypatch.setattr(crud.messages, "get_unanswered_inbound", fake)
    out = chat_routes.unanswered_messages(minutes=60, user={"business_id": 7})
    assert seen["business_id"] == 7
    assert out["count"] == 1 and len(out["items"][0]["text"]) == 160
    assert out["items"][0]["age_minutes"] in (9, 10)


# ── recovery endpoint ─────────────────────────────────────────────────────────
@pytest.fixture
def recovery(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    import core.db as db
    biz = {"id": 1, "is_active": True, "whatsapp_phone_id": PHONE_NUMBER_ID, "use_shared_number": False}
    state = {
        "cands": [{"id": 1, "customer_id": 42, "business_id": 1, "text": "I want pizza",
                   "created_at": _iso(10), "wa_message_id": "wamid.lost"}],
        "answered": False, "handoff": False, "biz": biz,
    }
    monkeypatch.setattr(crud.messages, "get_unanswered_inbound", lambda *a, **k: list(state["cands"]))
    monkeypatch.setattr(crud.messages, "has_reply_since", lambda *a, **k: state["answered"])
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: state["biz"])
    carts = [{"phone": CUSTOMER_PHONE, "business_id": 1, "updated_at": _iso(60)}]   # untouched since before the message
    state["carts"] = carts
    monkeypatch.setattr(db, "supabase", _DB([], [{"id": 42, "business_id": 1, "phone": CUSTOMER_PHONE}], carts))
    import services._ai_state as st
    monkeypatch.setattr(st, "is_in_human_handoff", lambda p, b: state["handoff"])
    state["calls"] = _capture_generate_reply(monkeypatch)
    state["created"] = []
    monkeypatch.setattr(crud, "create_message", lambda *a, **kw: state["created"].append((a, kw)) or {"id": 99})
    return client, state


H = {"X-Cron-Secret": "s3cret"}


def test_recovery_requires_the_cron_secret(recovery, monkeypatch):
    client, _ = recovery
    assert client.post("/webhook/recover-unanswered").status_code == 403
    assert client.post("/webhook/recover-unanswered", headers={"X-Cron-Secret": "bad"}).status_code == 403
    monkeypatch.delenv("CRON_SECRET")
    assert client.post("/webhook/recover-unanswered", headers=H).status_code == 503


def test_recovery_is_a_dry_run_by_default(recovery):
    client, st = recovery
    out = client.post("/webhook/recover-unanswered", headers=H).json()
    assert out["dry_run"] is True and out["replayed"] == 1
    assert st["calls"] == [] and wh.send_whatsapp.call_count == 0


def test_recovery_replays_once_without_storing_the_incoming_message_again(recovery):
    client, st = recovery
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["replayed"] == 1 and out["failed"] == 0
    assert [c["message"] for c in st["calls"]] == ["I want pizza"]
    assert wh.send_whatsapp.call_count == 1
    directions = [a[3] for a, _k in st["created"]]
    assert "incoming" not in directions and "outgoing" in directions


def test_recovery_skips_handoff_placeholders_shared_and_answered(recovery):
    client, st = recovery
    st["handoff"] = True
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["handoff"] == 1 and out["replayed"] == 0
    st["handoff"] = False
    st["cands"][0]["text"] = "[image]"
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["non_text"] == 1
    st["cands"][0]["text"] = "hello"
    st["biz"] = {**st["biz"], "use_shared_number": True}
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["shared_number"] == 1
    st["biz"] = {**st["biz"], "use_shared_number": False}
    st["answered"] = True
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["answered_meanwhile"] == 1 and out["replayed"] == 0
    assert st["calls"] == [] and wh.send_whatsapp.call_count == 0


def test_recovery_ignores_inactive_business(recovery):
    client, st = recovery
    st["biz"] = {**st["biz"], "is_active": False}
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["business"] == 1 and st["calls"] == []


def test_replay_flag_cannot_be_injected_through_the_webhook_body(client, monkeypatch):
    """A body carrying "_replay": true must still be deduplicated and stored normally."""
    calls = _capture_generate_reply(monkeypatch)
    stored = set()
    created = []

    def create(*a, **kw):
        created.append(a[3])
        if kw.get("wa_message_id"):
            stored.add(kw["wa_message_id"])
        return {"id": len(created)}
    monkeypatch.setattr(crud, "create_message", create)
    monkeypatch.setattr(crud, "message_exists", lambda wid: wid in stored)
    payload = _envelope(_text("wamid.forge", "hello"))
    payload["_replay"] = True
    assert client.post("/webhook", json=payload).status_code == 200
    assert client.post("/webhook", json=payload).status_code == 200     # redelivery
    assert len(calls) == 1                                              # deduplicated, not replayed
    assert created.count("incoming") == 1                               # the message was stored


def test_recovery_skips_when_state_changed_after_the_message(recovery):
    client, st = recovery
    st["carts"][0]["updated_at"] = _iso(5)          # state written AFTER the message (10 min ago)
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["state_changed"] == 1 and out["replayed"] == 0
    assert st["calls"] == [] and wh.send_whatsapp.call_count == 0


def test_recovery_replays_when_state_untouched_since_the_message(recovery):
    client, st = recovery
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["replayed"] == 1 and out["skipped"]["state_changed"] == 0


def test_recovery_rechecks_handoff_inside_the_lock(recovery, monkeypatch):
    client, st = recovery
    import services._ai_state as stmod
    answers = iter([False, True])                   # free at first, in handoff once we hold the lock
    monkeypatch.setattr(stmod, "is_in_human_handoff", lambda p, b: next(answers))
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["handoff"] == 1 and out["replayed"] == 0
    assert st["calls"] == []


# ── structured error codes beat message text ─────────────────────────────────
def test_structured_postgres_code_decides_not_the_message_text():
    class APIError(Exception):
        pass
    # A permanent constraint error whose text echoes the customer's words ("connection timeout")
    e = APIError({"code": "23502", "message": 'null value ... Failing row contains (1, "my connection timeout")'})
    assert not wh._is_transient_error(e)
    e2 = APIError({"code": "08006", "message": "anything"})
    assert wh._is_transient_error(e2)
    e3 = APIError({"code": "503", "message": "x"})
    assert wh._is_transient_error(e3)


def test_network_exception_types_are_transient_regardless_of_text():
    import httpx
    assert wh._is_transient_error(httpx.ConnectError("x"))
    assert wh._is_transient_error(requests.exceptions.ConnectionError("x"))
    assert wh._is_transient_error(TimeoutError("x"))
    assert not wh._is_transient_error(ValueError("invalid input"))


# ── QA follow-ups ─────────────────────────────────────────────────────────────
def test_forged_replay_flag_on_an_already_stored_message_does_not_reply(client, monkeypatch):
    """The real attack: re-send a stored wamid with _replay=true to bypass dedupe."""
    calls = _capture_generate_reply(monkeypatch)
    monkeypatch.setattr(crud, "message_exists", lambda wid: True)
    payload = _envelope(_text("wamid.stored", "pay now"))
    payload["_replay"] = True
    assert client.post("/webhook", json=payload).status_code == 200
    assert calls == []
    wh.send_whatsapp.assert_not_called()


def test_replay_flag_nested_inside_the_message_is_ignored(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    monkeypatch.setattr(crud, "message_exists", lambda wid: True)
    payload = _envelope(_text("wamid.stored", "pay now"))
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["_replay"] = True
    payload["entry"][0]["_replay"] = True
    assert client.post("/webhook", json=payload).status_code == 200
    assert calls == []


def test_timestamp_formats_are_compared_as_instants(monkeypatch):
    base = datetime.now(timezone.utc) - timedelta(minutes=20)
    inc = base.isoformat()                                         # ...+00:00 with microseconds
    out_z = (base + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")   # 'Z', no fraction
    rows = [
        {"id": 1, "customer_id": 10, "business_id": 1, "direction": "incoming", "text": "hi",
         "created_at": inc, "wa_message_id": "w1"},
        {"id": 2, "customer_id": 10, "business_id": 1, "direction": "outgoing", "text": "hello",
         "created_at": out_z, "wa_message_id": None},
    ]
    monkeypatch.setattr(messages_mod, "supabase", _DB(rows))
    assert messages_mod.get_unanswered_inbound(max_age_minutes=30, min_age_minutes=3) == []
    # and a reply that is EARLIER than the message (different formats) does not count
    rows[1]["created_at"] = (base - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert [r["id"] for r in messages_mod.get_unanswered_inbound(max_age_minutes=30, min_age_minutes=3)] == [1]


def test_ts_helper_handles_z_naive_and_garbage():
    a = messages_mod._ts("2026-11-19T10:00:00Z")
    b = messages_mod._ts("2026-11-19T10:00:00+00:00")
    c = messages_mod._ts("2026-11-19T10:00:00")
    assert a == b == c
    assert messages_mod._ts(None).year == 1970 and messages_mod._ts("junk").year == 1970


def test_recovery_clamps_limit_and_age(recovery):
    client, st = recovery
    seen = {}

    def fake(max_age, min_age, lim, *a, **k):
        seen.update(max_age=max_age, lim=lim)
        return []
    import crud as c
    c.messages.get_unanswered_inbound = fake
    client.post("/webhook/recover-unanswered?max_age_minutes=100000&limit=999", headers=H)
    assert seen["max_age"] == 120 and seen["lim"] == 150          # limit clamped to 50, then x3
    client.post("/webhook/recover-unanswered?max_age_minutes=1&limit=0", headers=H)
    assert seen["max_age"] == 5 and seen["lim"] == 3


def test_recovery_skips_when_handoff_state_is_unreadable(recovery, monkeypatch):
    client, st = recovery
    import services._ai_state as stmod
    monkeypatch.setattr(stmod, "is_in_human_handoff", lambda p, b: None)
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["skipped"]["handoff"] == 1 and out["replayed"] == 0 and st["calls"] == []


def test_recovery_skips_candidates_without_a_timestamp(recovery):
    client, st = recovery
    st["cands"][0]["created_at"] = None
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["replayed"] == 0 and st["calls"] == []


def test_recovery_counts_a_failed_replay_and_continues(recovery, monkeypatch):
    client, st = recovery
    st["cands"].append({"id": 2, "customer_id": 42, "business_id": 1, "text": "second",
                        "created_at": _iso(9), "wa_message_id": "wamid.2"})
    n = {"i": 0}

    async def flaky(payload):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("boom")
    monkeypatch.setattr(wh, "_handle_one_message", flaky)
    out = client.post("/webhook/recover-unanswered?dry_run=false", headers=H).json()
    assert out["failed"] == 1 and out["replayed"] == 1


def test_recovery_synthesizes_an_id_when_the_wamid_is_missing(recovery, monkeypatch):
    client, st = recovery
    st["cands"][0]["wa_message_id"] = None
    seen = {}

    async def capture(payload):
        seen.update(payload["entry"][0]["changes"][0]["value"]["messages"][0])
    monkeypatch.setattr(wh, "_handle_one_message", capture)
    client.post("/webhook/recover-unanswered?dry_run=false", headers=H)
    assert seen["id"] == "replay-1" and seen["text"] == {"body": "I want pizza"}


def test_ambiguous_read_timeout_marks_the_reply_failed_for_follow_up(client, monkeypatch):
    _capture_generate_reply(monkeypatch)
    wh.send_whatsapp.return_value = {"error": "read timeout - delivery unknown", "ambiguous": True}
    failed = []
    monkeypatch.setattr(crud.messages, "mark_message_failed", lambda mid: failed.append(mid))
    assert client.post("/webhook", json=_envelope(_text("wamid.1", "hi"))).status_code == 200
    assert failed == [1]


def test_chat_unanswered_route_requires_auth_and_bounds_minutes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import chat_routes
    app = FastAPI()
    app.include_router(chat_routes.router)
    c = TestClient(app)
    assert c.get("/chat/unanswered").status_code in (401, 403)
    app.dependency_overrides[chat_routes.require_business] = lambda: {"business_id": 1}
    assert c.get("/chat/unanswered?minutes=4").status_code == 422
    assert c.get("/chat/unanswered?minutes=241").status_code == 422


def test_payment_webhook_logs_a_failed_confirmation(monkeypatch, caplog):
    import logging
    src = open(wh.__file__, encoding="utf8").read()
    assert "confirmation not delivered" in src and "_wa_error_code(_sent)" in src
