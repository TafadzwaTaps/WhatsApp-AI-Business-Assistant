"""Stage 3: cart / checkout expiry, price revalidation before payment, and the order cancellation flow.
A stateful in-memory database stands in for Supabase; no WhatsApp message and no payment provider is touched."""
import json
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

import services.ai as ai
import services.inventory as inventory
import services.stock_restore as sr
import workflows.order_lifecycle as ol
from services import _ai_lifecycle as lc

PHONE = "263700000001"
OTHER = "263700000002"


# ── stateful fake database ────────────────────────────────────────────────────
class _Q:
    def __init__(self, db, table):
        self.db, self.t, self.f, self.op, self.payload, self._limit, self._conflict = db, table, [], "select", None, None, None

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def upsert(self, row, on_conflict=None):
        self.op, self.payload, self._conflict = "upsert", row, on_conflict
        return self

    def update(self, row):
        self.op, self.payload = "update", row
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, c, v):
        self.f.append(lambda r: r.get(c) == v)
        return self

    def in_(self, c, vs):
        self.f.append(lambda r: r.get(c) in list(vs))
        return self

    def ilike(self, c, v):
        want = str(v).strip("%").lower()
        self.f.append(lambda r: str(r.get(c) or "").lower() == want)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        if self.t in self.db.missing:
            raise Exception({"code": "42P01", "message": f'relation "public.{self.t}" does not exist'})
        if self.db.fail_tables.get((self.t, self.op)):
            raise Exception("boom")
        rows = self.db.tables.setdefault(self.t, [])
        res = types.SimpleNamespace(data=[])
        with self.db.lock:
            if self.op == "insert":
                row = dict(self.payload)
                row.setdefault("id", len(rows) + 1)
                rows.append(row)
                res.data = [dict(row)]
            elif self.op == "upsert":
                keys = [k.strip() for k in (self._conflict or "id").split(",")]
                hit = [r for r in rows if all(r.get(k) == self.payload.get(k) for k in keys)]
                if hit:
                    hit[0].update(self.payload)
                    res.data = [dict(hit[0])]
                else:
                    row = dict(self.payload)
                    rows.append(row)
                    res.data = [dict(row)]
            else:
                hit = [r for r in rows if all(f(r) for f in self.f)]
                if self.op == "select":
                    out = [dict(r) for r in hit]
                    res.data = out[: self._limit] if self._limit else out
                elif self.op == "update":
                    for r in hit:
                        r.update(self.payload)
                    res.data = [dict(r) for r in hit]
                elif self.op == "delete":
                    for r in hit:
                        rows.remove(r)
                    res.data = [dict(r) for r in hit]
        return res


class _DB:
    def __init__(self, **tables):
        self.tables, self.lock, self.missing, self.fail_tables = tables, threading.RLock(), {"stock_movements"}, {}

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        raise Exception({"code": "PGRST202", "message": "no rpc"})

    def stock(self, pid):
        return next(p["stock"] for p in self.tables["products"] if p["id"] == pid)

    def order(self, oid):
        return next(o for o in self.tables["orders"] if o["id"] == oid)

    def cart_row(self, phone=PHONE, biz=1):
        return next((r for r in self.tables["carts"] if r["phone"] == phone and r["business_id"] == biz), None)


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


class _Payments:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return f"PAYMENT-REQUEST {lc.cart_total(kw['cart']):.2f}"


@pytest.fixture
def world(monkeypatch):
    import core.db as coredb
    import crud
    from core import plan_guard
    db = _DB(
        businesses=[{"id": 1, "is_service_business": False}],
        products=[
            {"id": 1, "business_id": 1, "name": "Tea", "stock": 5, "price": 2.0},
            {"id": 2, "business_id": 1, "name": "Cup", "stock": 10, "price": 3.0},
            {"id": 3, "business_id": 1, "name": "Ebook", "stock": None, "price": 5.0},
        ],
        orders=[], carts=[],
    )
    for mod in (inventory, ol, sr, coredb):
        monkeypatch.setattr(mod, "supabase", db)
    monkeypatch.setattr(ol, "_orders_columns", {"id", "business_id", "customer_phone", "product_name", "quantity",
                                               "total_price", "status", "created_at", "payment_method", "items",
                                               "payment_status", "payment_reference", "paypal_order_id"})
    sr._reset_probes_for_tests()

    def get_cart(phone, biz):
        r = db.cart_row(phone, biz)
        return list(r.get("items") or []) if r else []

    def save_cart(phone, biz, cart):
        db.table("carts").upsert({"phone": phone, "business_id": biz, "items": cart, "updated_at": _ago()},
                                 on_conflict="phone,business_id").execute()

    def clear_cart(phone, biz):
        db.table("carts").upsert({"phone": phone, "business_id": biz, "items": [], "updated_at": _ago()},
                                 on_conflict="phone,business_id").execute()

    def update_order_payment(order_id, biz, patch):
        db.table("orders").update(patch).eq("id", order_id).eq("business_id", biz).execute()

    monkeypatch.setattr(crud, "get_cart", get_cart)
    monkeypatch.setattr(crud, "save_cart", save_cart)
    monkeypatch.setattr(crud, "clear_cart", clear_cart)
    monkeypatch.setattr(crud, "update_order_payment", update_order_payment)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": 1, "is_service_business": False})
    monkeypatch.setattr(crud, "get_business_payment_settings", lambda b: {})
    monkeypatch.setattr(crud, "get_user_memory", lambda p, b: None)
    monkeypatch.setattr(crud, "save_user_memory", lambda *a, **k: None)
    monkeypatch.setattr(crud, "get_or_create_customer", lambda p, b: {"id": 7})
    monkeypatch.setattr(plan_guard, "feature_access", lambda feature_key, business_id: {"allowed": True})
    monkeypatch.setattr(plan_guard, "get_access_status", lambda b: {"restricted": False})
    import services.payment_service as ps
    monkeypatch.setattr(ps, "available_methods", lambda s: ["ecocash", "cash"])
    pay = _Payments()
    monkeypatch.setattr(ai, "_process_payment", pay)
    handoffs = []

    def handoff(phone, biz, name, text, trigger_reason, issue, ai_summary, currency_sym="$"):
        handoffs.append(trigger_reason)
        return "HANDOFF"
    monkeypatch.setattr(ai, "_trigger_smart_handoff", handoff)
    w = types.SimpleNamespace(db=db, pay=pay, handoffs=handoffs)

    def put_cart(items, state="browsing", session=None, updated_at=None, last_activity=None, **extra):
        sd = {"state": state, "session": session or {}, **extra}
        if last_activity:
            sd["last_activity"] = last_activity
        db.tables["carts"][:] = [r for r in db.tables["carts"] if r["phone"] != PHONE]
        db.tables["carts"].append({"phone": PHONE, "business_id": 1, "items": items, "state_data": sd,
                                   "updated_at": updated_at or _ago()})

    def say(text, phone=PHONE):
        return ai.generate_reply(message=text, phone=phone, business_id=1, business_name="Test Shop",
                                 products=db.tables["products"], business_config={"id": 1, "is_service_business": False})

    def place_order(cart=None, phone=PHONE, **fields):
        o = ol.create_order_supabase(1, phone, cart or [{"name": "Tea", "qty": 2, "price": 2.0}])
        for k, v in fields.items():
            db.order(o["id"])[k] = v
        return o["id"]
    w.put_cart, w.say, w.place_order = put_cart, say, place_order
    yield w
    sr._reset_probes_for_tests()


def state(w):
    return (w.db.cart_row() or {}).get("state_data", {}).get("state")


TEA2 = [{"name": "Tea", "qty": 2, "price": 2.0}]


# ── checkout expiry (30 minutes) ──────────────────────────────────────────────
@pytest.mark.parametrize("st", sorted(lc.CHECKOUT_EXPIRABLE_STATES - {"order_preview"}))
def test_incomplete_checkout_states_expire_after_30_minutes_and_keep_the_cart(world, st):
    world.put_cart(TEA2, state=st, session={"cart_snapshot": TEA2}, updated_at=_ago(minutes=31),
                   last_activity=_ago(minutes=31))
    out = world.say("yes")
    assert "30 minutes" in out and "nothing was ordered" in out.lower()
    assert state(world) == "browsing"
    assert world.db.cart_row()["items"] == TEA2                       # cart preserved
    assert world.pay.calls == [] and world.db.tables["orders"] == []  # nothing created or charged


def test_a_reply_inside_30_minutes_is_not_expired_and_refreshes_the_clock(world):
    world.put_cart(TEA2, state="checkout", session={"cart_snapshot": TEA2}, updated_at=_ago(minutes=29),
                   last_activity=_ago(minutes=29))
    out = world.say("cash")
    assert out.startswith("PAYMENT-REQUEST")
    sd = world.db.cart_row()["state_data"]
    assert lc.parse_ts(sd["last_activity"]) > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_expired_checkout_with_a_normal_message_explains_then_answers(world):
    world.put_cart(TEA2, state="checkout", session={"cart_snapshot": TEA2}, updated_at=_ago(hours=2),
                   last_activity=_ago(hours=2))
    out = world.say("menu")
    assert out.startswith("⏱️") and "Type *checkout* to start again" in out and len(out) > 200


def test_the_customer_can_restart_after_expiry(world):
    world.put_cart(TEA2, state="checkout", session={"cart_snapshot": TEA2}, updated_at=_ago(hours=2),
                   last_activity=_ago(hours=2))
    world.say("cash")                                                 # notice
    out = world.say("checkout")
    assert state(world) in ("confirm_order", "checkout") or "checkout" in out.lower() or "pay" in out.lower()


@pytest.mark.parametrize("st", ["awaiting_payment", "awaiting_proof", "awaiting_fulfillment", "awaiting_address",
                                "awaiting_appointment_time", "manual_review", "human_handoff", "survey",
                                "awaiting_reminder_response"])
def test_states_with_a_placed_order_or_a_human_never_expire(world, st):
    world.put_cart([], state=st, session={"order_id": 5}, updated_at=_ago(hours=48), last_activity=_ago(hours=48))
    assert lc.expire_stale(PHONE, 1) is None
    assert state(world) == st


def test_confirmed_and_paid_orders_are_never_touched_by_expiry(world):
    oid = world.place_order(payment_status="paid", status="confirmed")
    world.put_cart(TEA2, state="checkout", session={"cart_snapshot": TEA2}, updated_at=_ago(hours=5),
                   last_activity=_ago(hours=5))
    lc.expire_stale(PHONE, 1)
    assert world.db.order(oid)["status"] == "confirmed" and world.db.order(oid)["payment_status"] == "paid"


def test_expiry_failure_never_breaks_the_conversation(world):
    world.db.fail_tables[("carts", "select")] = True
    assert lc.expire_stale(PHONE, 1) is None


# ── cart expiry (30 days) ─────────────────────────────────────────────────────
def test_a_cart_idle_for_31_days_is_emptied_but_history_and_row_are_kept(world):
    oid = world.place_order()
    world.put_cart(TEA2, updated_at=_ago(days=31))
    out = lc.expire_stale(PHONE, 1)
    assert out and "30 days" in out
    assert world.db.cart_row()["items"] == []
    assert world.db.order(oid)["status"] == "pending"                 # order history untouched


def test_a_cart_idle_for_29_days_is_kept(world):
    world.put_cart(TEA2, updated_at=_ago(days=29))
    assert lc.expire_stale(PHONE, 1) is None and world.db.cart_row()["items"] == TEA2


def test_a_new_cart_can_be_started_after_expiry(world):
    world.put_cart(TEA2, updated_at=_ago(days=40))
    lc.expire_stale(PHONE, 1)
    out = world.say("2 cups")
    assert any(i["name"] == "Cup" for i in world.db.cart_row()["items"])
    assert "Cup" in out


def test_recent_activity_after_an_old_checkout_marker_does_not_expire_the_cart(world):
    world.put_cart(TEA2, updated_at=_ago(hours=1), last_activity=_ago(days=60))
    assert lc.expire_stale(PHONE, 1) is None and world.db.cart_row()["items"] == TEA2


def test_old_cart_with_an_active_order_state_is_not_emptied(world):
    world.put_cart(TEA2, state="awaiting_payment", updated_at=_ago(days=45))
    assert lc.expire_stale(PHONE, 1) is None and world.db.cart_row()["items"] == TEA2


# ── price revalidation ────────────────────────────────────────────────────────
def _checkout(w, items=None):
    items = items or TEA2
    w.put_cart(items, state="checkout", session={"cart_snapshot": items})


def _set_price(w, pid, price):
    next(p for p in w.db.tables["products"] if p["id"] == pid)["price"] = price


def test_unchanged_prices_go_straight_to_the_payment_request(world):
    _checkout(world)
    assert world.say("cash") == "PAYMENT-REQUEST 4.00"


def test_a_stale_cart_price_is_never_used_and_the_customer_must_confirm_the_new_total(world):
    _checkout(world)
    _set_price(world, 1, 3.0)                                         # was 2.00 in the cart
    out = world.say("cash")
    assert "prices changed" in out.lower() and "$4.00" in out and "$6.00" in out
    assert world.pay.calls == []                                      # no payment request at the old price
    assert state(world) == "price_changed_confirm"
    assert world.db.cart_row()["items"][0]["price"] == 3.0            # cart now shows the current price


def test_confirming_the_new_total_pays_at_the_new_price(world):
    _checkout(world)
    _set_price(world, 1, 3.0)
    world.say("cash")
    out = world.say("yes")
    assert out == "PAYMENT-REQUEST 6.00" and len(world.pay.calls) == 1
    assert world.pay.calls[0]["cart"][0]["price"] == 3.0


def test_declining_the_new_total_returns_to_the_cart_without_an_order(world):
    _checkout(world)
    _set_price(world, 1, 3.0)
    world.say("cash")
    out = world.say("no")
    assert state(world) == "browsing" and world.pay.calls == [] and world.db.tables["orders"] == []
    assert "$6.00" in out


def test_cancel_while_confirming_a_new_total_is_a_cart_cancel(world):
    _checkout(world)
    _set_price(world, 1, 3.0)
    world.say("cash")
    out = world.say("cancel")
    assert "cancelled" in out.lower() and state(world) == "browsing" and world.pay.calls == []


def test_an_unrelated_answer_while_confirming_re_asks_and_never_pays(world):
    _checkout(world)
    _set_price(world, 1, 3.0)
    world.say("cash")
    out = world.say("maybe later")
    assert "yes" in out.lower() and world.pay.calls == [] and state(world) == "price_changed_confirm"


def test_a_second_price_change_after_the_prompt_asks_again(world):
    _checkout(world)
    _set_price(world, 1, 3.0)
    world.say("cash")
    _set_price(world, 1, 4.0)
    out = world.say("yes")
    assert "$6.00" in out and "$8.00" in out and world.pay.calls == []
    assert state(world) == "price_changed_confirm"


def test_a_price_decrease_is_also_confirmed_not_silently_applied(world):
    _checkout(world)
    _set_price(world, 1, 1.0)
    out = world.say("cash")
    assert "$4.00" in out and "$2.00" in out and world.pay.calls == []


def test_insufficient_stock_blocks_payment(world):
    _checkout(world, [{"name": "Tea", "qty": 9, "price": 2.0}])
    out = world.say("cash")
    assert "only 5 left" in out and world.pay.calls == [] and state(world) == "browsing"


def test_out_of_stock_and_removed_products_block_payment(world):
    world.db.tables["products"][0]["stock"] = 0
    _checkout(world)
    assert "out of stock" in world.say("cash") and world.pay.calls == []
    world.db.tables["products"].pop(0)
    _checkout(world)
    assert "no longer available" in world.say("cash") and world.pay.calls == []


def test_unlimited_stock_products_are_not_blocked(world):
    _checkout(world, [{"name": "Ebook", "qty": 50, "price": 5.0}])
    assert world.say("cash").startswith("PAYMENT-REQUEST")


def test_a_price_lookup_failure_never_falls_back_to_the_cart_price(world, monkeypatch):
    _checkout(world)

    def boom(b, n):
        raise RuntimeError("db down")
    monkeypatch.setattr(inventory, "get_product_by_name", boom)
    out = world.say("cash")
    assert "couldn't confirm the current prices" in out and world.pay.calls == []


def test_a_placed_orders_amount_is_never_rewritten_afterwards(world):
    oid = world.place_order()
    total = world.db.order(oid)["total_price"]
    _set_price(world, 1, 99.0)
    world.put_cart([], state="awaiting_payment", session={}, pending_payment={"order_id": oid})
    world.say("hello")
    assert world.db.order(oid)["total_price"] == total


def test_reprice_pure_function_reports_each_change():
    prods = {"Tea": {"price": 3.0, "stock": 5}, "Cup": {"price": 3.0, "stock": 1}}
    r = lc.reprice_cart(1, [{"name": "Tea", "qty": 1, "price": 2.0}, {"name": "Cup", "qty": 2, "price": 3.0}],
                        lookup=lambda b, n: prods.get(n))
    assert r["changed"] == [("Tea", 2.0, 3.0)] and r["short"] == [("Cup", 2, 1)] and r["ok"] is False


def test_service_businesses_ignore_stock_in_revalidation():
    r = lc.reprice_cart(1, [{"name": "Haircut", "qty": 1, "price": 9.0}], is_service=True,
                        lookup=lambda b, n: {"price": 9.0, "stock": 0})
    assert r["ok"] and not r["changed"]


# ── cancelling an unfinished checkout ─────────────────────────────────────────
@pytest.mark.parametrize("st", ["checkout", "confirm_order"])
def test_cancelling_an_unfinished_checkout_keeps_the_cart_and_creates_nothing(world, st):
    world.put_cart(TEA2, state=st, session={"cart_snapshot": TEA2})
    out = world.say("cancel")
    assert "Checkout cancelled" in out and "Nothing was ordered" in out
    assert state(world) == "browsing" and world.db.cart_row()["items"] == TEA2 and world.db.tables["orders"] == []


def test_cancel_with_nothing_placed_says_so(world):
    world.put_cart(TEA2)
    assert "Nothing to cancel" in world.say("cancel my order")
    assert world.db.tables["orders"] == [] and state(world) == "browsing"


# ── cancelling a placed order ─────────────────────────────────────────────────
@pytest.mark.parametrize("phrase", ["cancel", "cancel my order", "I want to cancel my order", "cancel order",
                                    "please cancel the order"])
def test_cancel_phrases_ask_for_confirmation_first(world, phrase):
    oid = world.place_order()
    world.put_cart([], state="browsing")
    out = world.say(phrase) if phrase != "cancel" else world.say("cancel my order")
    assert f"Cancel ORDER-{oid}?" in out and "yes, cancel" in out.lower()
    assert world.db.order(oid)["status"] == "pending"                  # not cancelled yet
    assert state(world) == "awaiting_cancel_confirm"


@pytest.mark.parametrize("confirm", ["yes, cancel", "yes cancel", "yes", "Yes, cancel my order"])
def test_confirmation_cancels_the_order_and_restores_stock_once(world, confirm):
    oid = world.place_order()
    assert world.db.stock(1) == 3
    world.put_cart([], state="browsing")
    world.say("cancel my order")
    out = world.say(confirm)
    assert f"ORDER-{oid} has been cancelled" in out
    assert world.db.order(oid)["status"] == "cancelled" and world.db.stock(1) == 5
    assert state(world) == "browsing"


def test_duplicate_confirmations_cancel_and_restore_only_once(world):
    oid = world.place_order()
    world.put_cart([], state="browsing")
    world.say("cancel my order")
    world.say("yes, cancel")
    out = world.say("yes, cancel")                                    # sent twice
    assert "nothing waiting" in out.lower()
    assert world.db.stock(1) == 5 and world.db.order(oid)["status"] == "cancelled"


def test_a_repeated_confirmation_after_the_cancel_does_not_double_restore(world):
    oid = world.place_order()
    world.put_cart([], state="awaiting_cancel_confirm", session={"order_id": oid})
    world.say("yes, cancel")
    world.put_cart([], state="awaiting_cancel_confirm", session={"order_id": oid})   # a retried webhook
    out = world.say("yes, cancel")
    assert "already cancelled" in out and world.db.stock(1) == 5


def test_yes_cancel_with_no_pending_question_does_nothing(world):
    oid = world.place_order()
    world.put_cart([], state="browsing")
    out = world.say("yes, cancel")
    assert "nothing waiting" in out.lower() and world.db.order(oid)["status"] == "pending"


def test_declining_keeps_the_order(world):
    oid = world.place_order()
    world.put_cart([], state="browsing")
    world.say("cancel my order")
    out = world.say("no thanks")
    assert "kept" in out and world.db.order(oid)["status"] == "pending" and world.db.stock(1) == 3
    assert state(world) == "browsing"


def test_a_paid_order_is_never_cancelled_or_refunded_automatically(world):
    oid = world.place_order(payment_status="paid", status="confirmed")
    world.put_cart([], state="browsing")
    out = world.say("cancel my order")
    assert f"Cancel ORDER-{oid}?" in out
    out = world.say("yes, cancel")
    assert out == "HANDOFF" and world.handoffs and "paid" in world.handoffs[0].lower()
    assert world.db.order(oid)["status"] == "confirmed" and world.db.order(oid)["payment_status"] == "paid"
    assert world.db.stock(1) == 3                                      # stock not restored for a paid order


@pytest.mark.parametrize("status", ["delivered", "completed"])
def test_delivered_orders_go_to_a_human_not_to_auto_cancel(world, status):
    oid = world.place_order(status=status)
    r = lc.cancel_order(oid, PHONE, 1)
    assert r["result"] == "needs_human" and world.db.order(oid)["status"] == status


def test_an_order_under_payment_review_goes_to_a_human(world):
    oid = world.place_order(status="payment_review")
    assert lc.cancel_order(oid, PHONE, 1)["result"] == "needs_human"
    assert world.db.order(oid)["status"] == "payment_review"


def test_one_customer_can_never_cancel_or_see_another_customers_order(world):
    oid = world.place_order(phone=OTHER)
    assert lc.cancel_order(oid, PHONE, 1)["result"] == "not_found"
    assert world.db.order(oid)["status"] == "pending"
    world.put_cart([], state="browsing")
    out = world.say(f"cancel order {oid}")
    assert f"ORDER-{oid}" not in out
    assert world.db.order(oid)["status"] == "pending"


def test_another_business_is_not_reachable(world):
    oid = world.place_order()
    assert lc.cancel_order(oid, PHONE, 2)["result"] == "not_found"


def test_cancelling_an_already_cancelled_order_is_reported_not_repeated(world):
    oid = world.place_order()
    assert lc.cancel_order(oid, PHONE, 1)["result"] == "cancelled"
    assert lc.cancel_order(oid, PHONE, 1)["result"] == "already_cancelled"
    assert world.db.stock(1) == 5


def test_awaiting_payment_cancel_now_really_cancels_after_confirmation(world):
    oid = world.place_order()
    world.put_cart([], state="awaiting_payment", session={},
                   pending_payment={"order_id": oid, "method": "ecocash", "reference": f"ORDER-{oid}"})
    out = world.say("cancel")
    assert f"Cancel ORDER-{oid}?" in out and world.db.order(oid)["status"] == "pending"
    out = world.say("yes, cancel")
    assert "has been cancelled" in out
    assert world.db.order(oid)["status"] == "cancelled" and world.db.stock(1) == 5


def test_declining_in_awaiting_payment_resumes_the_payment_step(world):
    oid = world.place_order()
    pp = {"order_id": oid, "method": "ecocash", "reference": f"ORDER-{oid}"}
    world.put_cart([], state="awaiting_payment", session={}, pending_payment=pp)
    world.say("cancel")
    world.say("no")
    sd = world.db.cart_row()["state_data"]
    assert sd["state"] == "awaiting_payment" and sd["pending_payment"] == pp


def test_cancelling_a_cash_order_before_it_is_confirmed(world):
    oid = world.place_order(status="pending_cash", payment_method="cash")
    r = lc.cancel_order(oid, PHONE, 1)
    assert r["result"] == "cancelled" and world.db.stock(1) == 5


def test_cancel_failure_changes_nothing(world, monkeypatch):
    oid = world.place_order()

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ol, "update_order_status_supabase", boom)
    assert lc.cancel_order(oid, PHONE, 1)["result"] == "error"
    assert world.db.order(oid)["status"] == "pending" and world.db.stock(1) == 3


def test_explicit_refund_requests_still_reach_the_refund_handoff(world):
    world.place_order()
    world.put_cart([], state="browsing")
    assert world.say("I want a refund") == "HANDOFF"
    assert world.handoffs and "refund" in world.handoffs[-1].lower()


def test_helpers_recognise_the_supported_phrases():
    for t in ("cancel my order", "I want to cancel my order", "cancel order 12", "please cancel the order"):
        assert lc.is_cancel_order_request(t)
    for t in ("yes, cancel", "Yes cancel", "yes, cancel it"):
        assert lc.is_yes_cancel(t)
    assert not lc.is_cancel_order_request("where is my order") and not lc.is_yes_cancel("yes")


def test_ts_parser_handles_supabase_formats():
    for s in ("2026-11-20T10:00:00+00:00", "2026-11-20T10:00:00.12+00:00", "2026-11-20T10:00:00.123456789Z",
              "2026-11-20T10:00:00"):
        assert lc.parse_ts(s) is not None
    assert lc.parse_ts("garbage") is None and lc.parse_ts(None) is None


def test_cart_recovery_skips_carts_idle_for_over_30_days():
    import inspect
    import growth.cart_recovery as cr
    assert "30 * 86400" in inspect.getsource(cr)


# ── review follow-ups ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("word", ["ok", "sure", "alright", "okay", "go ahead"])
def test_a_casual_ok_never_confirms_a_cancellation(world, word):
    oid = world.place_order()
    world.put_cart([], state="awaiting_cancel_confirm", session={"order_id": oid, "resume_state": "browsing"})
    world.say(word)
    assert world.db.order(oid)["status"] == "pending" and world.db.stock(1) == 3


def test_a_stale_cancel_question_counts_as_declined(world):
    oid = world.place_order()
    world.put_cart([], state="awaiting_cancel_confirm", session={"order_id": oid, "resume_state": "browsing"},
                   updated_at=_ago(hours=5))
    world.say("yes")
    assert world.db.order(oid)["status"] == "pending"


@pytest.mark.parametrize("text", ["don't cancel my order", "do not cancel the order", "never cancel my order"])
def test_negations_are_not_cancel_requests(text):
    assert not lc.is_cancel_order_request(text)


@pytest.mark.parametrize("st", ["checkout", "confirm_order"])
def test_a_question_about_cancelling_does_not_reset_a_checkout(world, st):
    world.put_cart(TEA2, state=st, session={"cart_snapshot": TEA2})
    world.say("can I cancel the order later?")
    assert state(world) == st


def test_payment_landing_during_the_cancel_keeps_the_paid_marker_and_hands_off(world, monkeypatch):
    oid = world.place_order()
    real = ol.update_order_status_supabase

    def pay_then_cancel(*a, **k):
        out = real(*a, **k)
        world.db.order(oid)["payment_status"] = "paid"           # the payment lands right after the cancel
        return out
    monkeypatch.setattr(ol, "update_order_status_supabase", pay_then_cancel)
    assert lc.cancel_order(oid, PHONE, 1)["result"] == "needs_human"
    assert world.db.order(oid)["payment_status"] == "paid"


def test_awaiting_proof_cancel_without_an_order_keeps_the_refund_warning(world):
    world.put_cart([], state="awaiting_proof", session={}, pending_proof={"order_id": 999})
    assert "already made a payment" in world.say("cancel")


@pytest.mark.parametrize("raw,dial,expected", [("9123456789", "91", "919123456789"), ("2012345678", "20", "202012345678"),
                                               ("+07700900123", None, None)])
def test_phone_edge_cases_from_review(raw, dial, expected):
    from services.phone_utils import normalise_phone
    assert normalise_phone(raw, dial) == expected
