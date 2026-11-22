"""Customer-chat fix #3: automatic order-status notifications (once per order+status, 24h-window and template rules,
recorded skips, honest promises, shared by the dashboard and the lifecycle push). WhatsApp is always mocked."""
import asyncio
import types

import pytest

import crud
from crud import notification_ledger as ledger
import services.order_notifications as on
from test_phase7_job_claims import _DB, db   # noqa: F401  (stateful fake with the notification_log unique constraint)

BIZ = {"id": 1, "name": "Flavoury Foods", "whatsapp_phone_id": "PID", "address": "12 High St",
       "is_service_business": False, "features_json": {"order_status_notifications": True}}


def _order(**kw):
    o = {"id": 48, "business_id": 1, "customer_phone": "263771000001", "status": "pending",
         "payment_status": "pending_cash", "fulfillment_method": "pickup", "total_price": 7.53, "currency_symbol": "zł"}
    o.update(kw)
    return o


@pytest.fixture
def env(monkeypatch, db):
    sent, templates = [], []
    monkeypatch.setattr(on, "order_money", lambda order, business: "7,53 zł")
    from services import wa_templates, wa_outbound, wa_window
    monkeypatch.setattr(wa_templates, "resolve_credentials", lambda b: ("PID", "TOK"))
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set())
    monkeypatch.setattr(wa_outbound, "template_for_closed_window",
                        lambda business, bid, phone, purpose, ctx, ref, **k: templates.append((purpose, ctx, ref)) or {"ok": True})
    st = types.SimpleNamespace(sent=sent, templates=templates, db=db)
    st.sender = lambda pid, tok, phone, text: sent.append((phone, text)) or {"messages": [{"id": "w"}]}
    st.go = lambda order, status, **k: on.notify_order_status(order, status, business=k.pop("business", BIZ),
                                                              sender=k.pop("sender", st.sender), **k)
    return st


# ── wording per kind of order ──
@pytest.mark.parametrize("kind,status,needle", [
    ("pickup", "confirmed", "is confirmed"), ("pickup", "preparing", "being prepared"),
    ("pickup", "ready", "ready for pickup"), ("pickup", "completed", "was collected"),
    ("delivery", "confirmed", "is confirmed"), ("delivery", "preparing", "being prepared"),
    ("delivery", "out_for_delivery", "on its way"), ("delivery", "delivered", "was delivered"),
    ("pickup", "cancelled", "was cancelled"), ("delivery", "cancelled", "was cancelled"),
])
def test_message_per_kind(kind, status, needle):
    text = on.status_text(_order(fulfillment_method=kind), status, BIZ)
    assert text and needle in text and "ORDER-48" in text


@pytest.mark.parametrize("kind,status", [("pickup", "out_for_delivery"), ("pickup", "delivered"), ("delivery", "completed")])
def test_steps_that_do_not_exist_for_that_order_are_not_announced(kind, status):
    assert on.status_text(_order(fulfillment_method=kind), status, BIZ) is None


def test_pickup_ready_includes_the_address_and_never_says_on_the_way():
    t = on.status_text(_order(), "ready", BIZ)
    assert "12 High St" in t and "on its way" not in t.lower() and "delivery" not in t.lower()


def test_delivery_ready_does_not_say_pickup():
    assert "pickup" not in on.status_text(_order(fulfillment_method="delivery"), "ready", BIZ).lower()


def test_unknown_fulfilment_uses_neutral_wording():
    o = _order(fulfillment_method=None)
    assert "is ready!" in on.status_text(o, "ready", BIZ) and "pickup" not in on.status_text(o, "ready", BIZ).lower()
    assert on.status_text(o, "completed", BIZ) and on.status_text(o, "out_for_delivery", BIZ)


def test_cancelled_explains_next_steps():
    unpaid = on.status_text(_order(), "cancelled", BIZ)
    paid = on.status_text(_order(payment_status="paid"), "cancelled", BIZ, note="Out of stock")
    assert "No payment is due" in unpaid and "menu" in unpaid
    assert "refund" in paid and "Out of stock" in paid


@pytest.mark.parametrize("s", ["pending", "pending_cash", "awaiting_payment", "refunded", "paid", "", None])
def test_internal_statuses_are_not_notifiable(s):
    assert on.status_text(_order(), s, BIZ) is None


# ── sending rules ──
def test_open_window_sends_free_text_once(env):
    assert env.go(_order(), "ready")["outcome"] == "sent"
    assert len(env.sent) == 1 and "ready for pickup" in env.sent[0][1]
    again = env.go(_order(), "ready")
    assert again["outcome"] == "duplicate" and len(env.sent) == 1


def test_each_status_is_sent_once_in_its_own_right(env):
    for s in ("confirmed", "preparing", "ready", "completed"):
        assert env.go(_order(), s)["notified"] is True
    assert len(env.sent) == 4
    assert env.go(_order(), "preparing")["outcome"] == "duplicate"


def test_not_applicable_step_sends_nothing_and_claims_nothing(env):
    r = env.go(_order(), "out_for_delivery")
    assert r["outcome"] == "skipped_not_notifiable" and env.sent == [] and env.db.claims("order_status") == []


def test_closed_window_without_a_template_sends_nothing_and_is_recorded(env, monkeypatch):
    from services import wa_window, wa_outbound
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set(phones))
    monkeypatch.setattr(wa_outbound, "template_for_closed_window", lambda *a, **k: {"ok": False, "skipped": "no_template"})
    r = env.go(_order(), "ready")
    assert r["outcome"] == "skipped_window_no_template" and env.sent == []
    assert env.db.claims("order_status") == []                                   # claim given back: a later retry is possible
    assert env.db.claims("order_status_skipped") == ["48:ready:window_closed_no_template"]


def test_closed_window_with_an_approved_template_uses_it_not_free_text(env, monkeypatch):
    from services import wa_window
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set(phones))
    r = env.go(_order(), "ready")
    assert r["outcome"] == "sent_template" and env.sent == []
    purpose, ctx, ref = env.templates[0]
    assert purpose == "order_update" and ctx["status"] == "ready" and ctx["order_id"] == "ORDER-48" and ref == "order:48:ready"


def test_a_window_error_from_meta_falls_back_to_the_template_only(env):
    r = env.go(_order(), "ready", sender=lambda *a: {"error": {"code": 131047, "message": "re-engagement"}})
    assert r["outcome"] == "sent_template" and len(env.templates) == 1


def test_a_failed_send_gives_the_claim_back_so_it_can_be_retried(env):
    r = env.go(_order(), "ready", sender=lambda *a: {"error": "boom"})
    assert r["outcome"] == "failed" and env.db.claims("order_status") == []
    assert env.go(_order(), "ready")["outcome"] == "sent"


def test_a_raising_sender_never_raises_out(env):
    def boom(*a):
        raise RuntimeError("network")
    r = env.go(_order(), "ready", sender=boom)
    assert r["outcome"] == "failed" and env.db.claims("order_status") == []


def test_no_phone_no_credentials_and_bad_input_are_skipped_not_errors(env, monkeypatch):
    assert env.go(_order(customer_phone=""), "ready")["outcome"] == "skipped_no_phone"
    assert on.notify_order_status(None, "ready")["outcome"] == "skipped_not_notifiable"
    assert on.notify_order_status({"id": 1}, "ready")["outcome"] == "skipped_not_notifiable"      # no business id
    from services import wa_templates
    monkeypatch.setattr(wa_templates, "resolve_credentials", lambda b: ("", ""))
    assert env.go(_order(), "ready")["outcome"] == "skipped_no_credentials" and env.sent == []


def test_a_ledger_outage_fails_closed(env):
    env.db.fail = True
    assert env.go(_order(), "ready")["outcome"] == "failed" and env.sent == []


def test_tenant_comes_from_the_order_not_the_caller(env):
    env.go(_order(business_id=1), "ready")
    assert env.db.claims("order_status") == ["48:ready"]
    r = env.go(_order(business_id=2), "ready")                  # same order id in another business is independent
    assert r["outcome"] == "sent" and len(env.sent) == 2


def test_text_override_still_follows_the_same_rules(env):
    env.go(_order(), "preparing", text_override="Custom owner note")
    assert env.sent[0][1] == "Custom owner note"
    assert env.go(_order(), "preparing", text_override="Custom owner note")["outcome"] == "duplicate"


# ── the dashboard status route ──
import routes.business_routes as br


@pytest.fixture
def route(monkeypatch, env):
    calls = {"status": []}
    row = _order()
    monkeypatch.setattr(crud, "get_order_by_id", lambda oid, bid: dict(row))
    state = {"transitioned": True, "status": "ready"}

    def upd(oid, status, business_id=None):
        calls["status"].append(status)
        return {"id": oid, "status": status, "_transitioned": state["transitioned"]}
    monkeypatch.setattr(br, "update_order_status_supabase", upd)
    import main
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: BIZ)
    monkeypatch.setattr(main, "send_whatsapp", env.sender)          # the real notifier, with WhatsApp mocked
    return types.SimpleNamespace(state=state, calls=calls, env=env)


def _put(status):
    return br.update_order_status_api(48, br.OrderStatusUpdate(status=status), user={"business_id": 1, "role": "business"})


def test_dashboard_status_change_notifies_the_customer(route):
    out = _put("ready")
    assert out["notification"]["customer_notified"] is True and len(route.env.sent) == 1


def test_same_status_again_does_not_notify(route):
    _put("ready")
    route.state["transitioned"] = False
    out = _put("ready")
    assert "notification" not in out and len(route.env.sent) == 1


def test_a_notification_failure_never_fails_the_status_update(route, monkeypatch):
    monkeypatch.setattr(on, "notify_order_status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out = _put("ready")
    assert out["status"] == "ready" and "notification" not in out


def test_the_dashboard_uses_the_fulfilment_of_the_order(route):
    _put("out_for_delivery")                                    # pickup order: not announced
    assert route.env.sent == []


# ── lifecycle push shares the logic ──
def test_lifecycle_push_goes_through_the_shared_notifier_once(monkeypatch, env):
    import routes.admin_routes as ar
    monkeypatch.setattr(ar, "get_order", lambda oid: _order(status="confirmed"))
    monkeypatch.setattr(ar, "update_order_status_supabase", lambda *a, **k: {})
    monkeypatch.setattr(crud, "update_order_payment", lambda *a: {})
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: BIZ)
    monkeypatch.setattr(ar, "send_whatsapp", env.sender)
    body = ar.OrderLifecycleUpdate(order_id=48, status="ready")
    run = lambda: asyncio.new_event_loop().run_until_complete(
        ar.push_lifecycle_update(48, body, user={"business_id": 1, "role": "business"}))
    first, second = run(), run()
    assert first["customer_notified"] is True and second["customer_notified"] is False
    assert second["notification"]["outcome"] == "duplicate" and len(env.sent) == 1


# ── honest promises ──
def test_updates_promised_depends_on_real_capability(monkeypatch):
    monkeypatch.delenv("SHARED_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("SHARED_WA_TOKEN", raising=False)
    ON = {"order_status_notifications": True}
    assert on.updates_promised({"whatsapp_phone_id": "PID", "features_json": ON}) is True
    assert on.updates_promised({"whatsapp_phone_id": "PID"}) is False               # default OFF
    assert on.updates_promised({"whatsapp_phone_id": "PID", "features_json": {}}) is False
    assert on.updates_promised({"features_json": ON}) is False                       # no credentials
    assert on.updates_promised({"whatsapp_phone_id": "PID", "features_json": {"order_status_notifications": False}}) is False
    monkeypatch.setenv("SHARED_PHONE_NUMBER_ID", "1")
    monkeypatch.setenv("SHARED_WA_TOKEN", "t")
    assert on.updates_promised({}) is False
    assert on.updates_promised({"features_json": ON}) is True


def test_promise_wording_is_conditional():
    monkeypatch_free = on.updates_promise_line({"whatsapp_phone_id": "PID", "features_json": {"order_status_notifications": True}})
    assert "message you here" in monkeypatch_free and "where is my order" in monkeypatch_free
    off = on.updates_promise_line({"whatsapp_phone_id": "PID", "features_json": {"order_status_notifications": False}})
    assert "message you" not in off and "where is my order" in off


def test_no_unconditional_notify_promises_left_in_customer_copy():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("services/ai.py", "services/_ai_lifecycle.py", "routes/admin_routes.py"):
        src = (root / rel).read_text(encoding="utf8")
        assert "We'll notify you as soon as" not in src and "We'll let you know when it's ready" not in src, rel
        assert "we'll tell you when it is ready" not in src, rel
