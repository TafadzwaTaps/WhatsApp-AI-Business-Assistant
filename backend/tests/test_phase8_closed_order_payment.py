"""Phase 8 (F-1, F-2): a payment that arrives for a CANCELLED or REFUNDED order must never mark it paid,
never tell the customer it is confirmed, and must alert the owner. Provider, WhatsApp and DB are faked."""
import asyncio
import types

import pytest
from fastapi import HTTPException

import crud
import routes.admin_routes as ar
import routes.chat_routes as cr
import routes.webhook_routes as wr
import workflows.order_lifecycle as ol
from crud.orders import payment_blocked
from test_phase3_payments import (_success, _cap, _ord, _event, hook, _paid_message, _details)   # noqa: F401  (hook = fixture)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def alerts(monkeypatch):
    seen = []
    monkeypatch.setattr(crud, "alert_owner_payment_on_closed_order", lambda order, source: seen.append((order["id"], source)))
    return seen


# ── the rule itself ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("order,expected", [
    ({"payment_status": "cancelled", "status": "cancelled"}, True),
    ({"payment_status": "pending", "status": "cancelled"}, True),
    ({"payment_status": "refunded", "status": "refunded"}, True),
    ({"payment_status": "CANCELLED"}, True),
    ({"payment_status": "pending", "status": "pending"}, False),
    ({"payment_status": "awaiting_payment", "status": "awaiting_payment"}, False),
    ({"payment_status": "paid", "status": "confirmed"}, False),
    ({}, False), (None, False),
])
def test_payment_blocked(order, expected):
    assert payment_blocked(order) is expected


def test_the_atomic_claim_also_refuses_cancelled_in_the_database_filter():
    import inspect
    assert "payment_status.neq.cancelled" in inspect.getsource(crud.claim_order_paid)


# ── PayPal return page ────────────────────────────────────────────────────────
@pytest.mark.parametrize("kw", [{"payment_status": "cancelled", "status": "cancelled"}, {"status": "cancelled"}])
def test_paypal_return_does_not_capture_a_cancelled_order(monkeypatch, kw):
    out, claims, notes = _success(monkeypatch, order=_ord(**kw), capture=_cap())
    assert out["status"] == "error" and "not been charged" in out["detail"]
    assert claims == [] and notes == [] and _success.captured == []


def test_paypal_return_still_captures_a_normal_order(monkeypatch):
    out, claims, notes = _success(monkeypatch, order=_ord(), capture=_cap())
    assert out["status"] == "ok" and len(claims) == 1 and _success.captured


# ── PayPal webhook (money already captured by PayPal) ─────────────────────────
@pytest.mark.parametrize("kw", [{"payment_status": "cancelled", "status": "cancelled"}, {"status": "cancelled"}])
def test_paypal_webhook_on_a_cancelled_order_alerts_and_confirms_nothing(hook, alerts, kw):
    c, st = hook
    st["order"] = _ord(**kw)
    out = c.post("/payments/paypal/webhook", json=_event()).json()
    assert out["detail"] == "order_closed_manual_review"
    assert st["claims"] == [] and st["notes"] == [] and st["statuses"] == [] and alerts == [(9, "paypal_webhook")]


def test_paypal_webhook_catches_a_cancel_that_lands_between_check_and_claim(hook, alerts, monkeypatch):
    c, st = hook

    def claim(*a):                                    # the customer cancels at the worst moment
        st["order"] = _ord(payment_status="cancelled", status="cancelled")
        return False
    monkeypatch.setattr(cr.crud, "claim_order_paid", claim)
    out = c.post("/payments/paypal/webhook", json=_event()).json()
    assert out["detail"] == "order_closed_manual_review" and st["notes"] == [] and alerts == [(9, "paypal_webhook")]


def test_paypal_webhook_normal_order_is_unchanged(hook, alerts):
    c, st = hook
    assert c.post("/payments/paypal/webhook", json=_event()).json()["paid"] is True
    assert len(st["claims"]) == 1 and len(st["notes"]) == 1 and alerts == []


# ── generic /payment/webhook ──────────────────────────────────────────────────
def _pay_hook(monkeypatch, order, claim_result=True, claims=None):
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "s3cret")
    state = {"order": order}
    monkeypatch.setattr(wr, "get_order", lambda oid: state["order"])
    claims = [] if claims is None else claims

    def claim(*a):
        claims.append(a)
        if callable(claim_result):
            return claim_result(state)
        return claim_result
    monkeypatch.setattr(crud, "claim_order_paid", claim)
    monkeypatch.setattr(wr, "update_order_status_supabase", lambda *a, **k: {})
    sent = []
    monkeypatch.setattr(wr, "send_whatsapp", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "whatsapp_phone_id": "P"})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "T")
    req = types.SimpleNamespace(headers={"X-Payment-Webhook-Secret": "s3cret"})
    data = types.SimpleNamespace(reference="ORDER-9", amount=10.0)
    return state, claims, sent, lambda: _run(wr.payment_webhook(data, req))


def test_payment_webhook_on_a_cancelled_order_alerts_and_sends_no_confirmation(monkeypatch, alerts):
    _, claims, sent, call = _pay_hook(monkeypatch, _ord(payment_status="cancelled", status="cancelled"))
    out = call()
    assert out["success"] is False and claims == [] and sent == [] and alerts == [(9, "payment_webhook")]


def test_payment_webhook_cancel_between_check_and_claim(monkeypatch, alerts):
    def claim_result(state):
        state["order"] = _ord(payment_status="cancelled", status="cancelled")
        return False
    _, _, sent, call = _pay_hook(monkeypatch, _ord(), claim_result=claim_result)
    out = call()
    assert out["success"] is False and sent == [] and alerts == [(9, "payment_webhook")]


def test_payment_webhook_normal_order_is_unchanged(monkeypatch, alerts):
    _, claims, sent, call = _pay_hook(monkeypatch, _ord(customer_phone="2637"))
    out = call()
    assert out["success"] is True and len(claims) == 1 and len(sent) == 1 and alerts == []


def test_payment_webhook_already_paid_is_still_idempotent(monkeypatch, alerts):
    _, claims, sent, call = _pay_hook(monkeypatch, _ord(payment_status="paid"))
    assert call()["success"] is True and claims == [] and sent == [] and alerts == []


# ── customer types "paid" (PayPal) ────────────────────────────────────────────
def test_customer_paid_message_does_not_confirm_a_cancelled_order(monkeypatch, alerts):
    out, claims = _paid_message(monkeypatch, _details(), order=_ord(payment_status="cancelled", status="cancelled"))
    assert "verifying" in out.lower() and claims == [] and alerts == [(9, "customer_paid_message")]


# ── owner routes (F-2) ────────────────────────────────────────────────────────
def _owner_routes(monkeypatch, order):
    updates = []
    monkeypatch.setattr(ar, "get_order", lambda oid: order)
    monkeypatch.setattr(crud, "update_order_payment", lambda *a, **k: updates.append(a) or {})
    monkeypatch.setattr(ar, "update_order_status_supabase", lambda *a, **k: updates.append(("status",) + a) or {})
    return updates


@pytest.mark.parametrize("bad", [{"payment_status": "cancelled", "status": "cancelled"},
                                 {"payment_status": "refunded", "status": "refunded"}, {"status": "cancelled"}])
def test_approve_payment_refuses_a_closed_order(monkeypatch, bad):
    updates = _owner_routes(monkeypatch, _ord(**bad))
    with pytest.raises(HTTPException) as e:
        _run(ar.admin_approve_payment(9, ar.OrderAdminAction(), user={"business_id": 1, "role": "business"}))
    assert e.value.status_code == 409 and updates == []


@pytest.mark.parametrize("bad", [{"payment_status": "cancelled", "status": "cancelled"}, {"status": "refunded"}])
def test_lifecycle_update_refuses_a_closed_order(monkeypatch, bad):
    updates = _owner_routes(monkeypatch, _ord(**bad))
    with pytest.raises(HTTPException) as e:
        _run(ar.push_lifecycle_update(9, ar.OrderLifecycleUpdate(order_id=9, status="delivered"), user={"business_id": 1, "role": "business"}))
    assert e.value.status_code == 409 and updates == []


def test_manual_confirm_refuses_a_closed_order(monkeypatch):
    monkeypatch.setattr(ol, "get_order", lambda oid: _ord(payment_status="cancelled", status="cancelled"))
    claims = []
    monkeypatch.setattr(crud, "claim_order_paid", lambda *a: claims.append(a) or True)

    class Req:
        async def json(self):
            return {"order_id": 9, "reference": "ORDER-9", "amount": 10}
    with pytest.raises(HTTPException) as e:
        _run(cr.manual_payment_confirm(Req(), user={"business_id": 1}))
    assert e.value.status_code == 409 and claims == []


# ── the owner alert itself ────────────────────────────────────────────────────
def test_owner_alert_never_raises_and_messages_the_owner(monkeypatch):
    import crud.orders as co
    sent = []

    class Q:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return types.SimpleNamespace(data=[{"contact_phone": "263700", "whatsapp_phone_id": "P", "whatsapp_token": "T"}])

    monkeypatch.setattr(co, "supabase", types.SimpleNamespace(table=lambda n: Q()))
    monkeypatch.setattr(co, "send_whatsapp", lambda *a: sent.append(a), raising=False)
    co.alert_owner_payment_on_closed_order({"id": 9, "business_id": 1, "status": "cancelled"}, "test")
    assert len(sent) == 1 and sent[0][2] == "263700" and "ORDER-9" in sent[0][3]
    monkeypatch.setattr(co, "supabase", types.SimpleNamespace(table=lambda n: (_ for _ in ()).throw(RuntimeError("db"))))
    co.alert_owner_payment_on_closed_order({"id": 9, "business_id": 1}, "test")          # must not raise
