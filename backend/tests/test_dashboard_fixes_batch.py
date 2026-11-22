"""Dashboard fix batch: (1) messages column detection (agent replies were untagged), (2) order Mark paid/unpaid
endpoint, (3) Stripe Connect onboarding errors/params, (4)/(5) front-end wiring checks.
Supabase, Stripe and WhatsApp are faked; nothing is sent or charged."""
import asyncio
import re
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

import crud
import crud.messages as cm
import routes.admin_routes as ar

ROOT = Path(__file__).resolve().parents[2]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═════════════ (1) messages column detection ═════════════
class _MsgDB:
    """messages table with known columns; selecting an unknown column raises, like PostgREST."""
    def __init__(self, cols, rows=()):
        self.cols, self.rows, self.inserted, self.probes = set(cols), list(rows), [], 0

    def table(self, name):
        db = self

        class Q:
            def select(self, c="*", **k):
                if c != "*":
                    db.probes += 1
                    if any(x.strip() not in db.cols for x in c.split(",")):
                        raise Exception({"code": "42703", "message": f"column messages.{c} does not exist"})
                return self

            def limit(self, n): return self
            def insert(self, row): db.inserted.append(row); self._row = row; return self
            def execute(self): return types.SimpleNamespace(data=[getattr(self, "_row", {"id": 1})])
        return Q()


@pytest.fixture
def msgdb(monkeypatch):
    db = _MsgDB({"id", "customer_id", "business_id", "text", "direction", "is_read", "status", "created_at",
                 "sender_type", "sender_name", "agent_id", "wa_message_id"})
    monkeypatch.setattr(cm, "supabase", db)
    cm._msg_col_cache.clear()
    yield db
    cm._msg_col_cache.clear()


def test_columns_are_found_even_when_the_table_is_empty(msgdb):
    # The old probe read the first ROW; an empty table cached a guessed list without sender_type forever.
    assert msgdb.rows == []
    assert cm._has_messages_col("sender_type") and cm._has_messages_col("sender_name") and cm._has_messages_col("agent_id")


def test_an_agent_reply_is_stored_with_its_sender(msgdb):
    cm.create_message(7, 1, "hello", "outgoing", sender_type="agent", sender_name="amy", agent_id="amy")
    row = msgdb.inserted[0]
    assert row["sender_type"] == "agent" and row["sender_name"] == "amy" and row["agent_id"] == "amy"


def test_a_missing_column_is_skipped_then_rechecked_later(msgdb, monkeypatch):
    msgdb.cols.discard("sender_type")
    assert cm._has_messages_col("sender_type") is False
    msgdb.cols.add("sender_type")                                   # migration applied meanwhile
    assert cm._has_messages_col("sender_type") is False             # inside the recheck window
    t0 = cm._msg_col_cache["sender_type"][1]
    import time
    monkeypatch.setattr(time, "monotonic", lambda: t0 + cm._COL_RECHECK_SECONDS + 1)
    assert cm._has_messages_col("sender_type") is True


def test_a_confirmed_column_is_not_probed_again(msgdb):
    cm._has_messages_col("sender_name")
    n = msgdb.probes
    for _ in range(5):
        cm._has_messages_col("sender_name")
    assert msgdb.probes == n


def test_a_failed_probe_never_raises(monkeypatch):
    cm._msg_col_cache.clear()

    class Boom:
        def table(self, n): raise RuntimeError("db down")
    monkeypatch.setattr(cm, "supabase", Boom())
    assert cm._has_messages_col("sender_type") is False
    cm._msg_col_cache.clear()


# ═════════════ (2) order Mark paid / unpaid ═════════════
USER = {"business_id": 1, "role": "business", "username": "owner"}


def _order(**kw):
    o = {"id": 9, "business_id": 1, "status": "pending", "payment_status": "awaiting_payment",
         "payment_method": "ecocash", "customer_phone": "2637", "total_price": 10.0}
    o.update(kw)
    return o


@pytest.fixture
def pay(monkeypatch):
    st = {"order": _order(), "claims": [], "updates": [], "status": [], "notes": [], "mem": []}
    monkeypatch.setattr(ar, "get_order", lambda oid: st["order"])
    monkeypatch.setattr(crud, "claim_order_paid", lambda *a: st["claims"].append(a) or st.get("claim", True))
    monkeypatch.setattr(crud, "update_order_payment", lambda *a: st["updates"].append(a) or {"id": a[0]})
    monkeypatch.setattr(ar, "update_order_status_supabase", lambda *a, **k: st["status"].append(a) or {})

    async def notify(o, m):
        st["notes"].append(m)
    monkeypatch.setattr(ar, "_notify_customer_payment", notify)
    import workflows.order_lifecycle as ol
    monkeypatch.setattr(ol, "_sync_user_memory_after_order", lambda b, p: st["mem"].append((b, p)))
    st["call"] = lambda paid, notify=False, user=USER: _run(
        ar.set_order_payment_state(9, ar.OrderPaymentToggle(paid=paid, notify=notify), user=user))
    return st


def test_mark_paid_claims_once_moves_the_order_forward_and_is_quiet(pay):
    out = pay["call"](True)
    assert out == {"ok": True, "order_id": 9, "payment_status": "paid", "changed": True}
    assert len(pay["claims"]) == 1 and pay["status"] == [(9, "confirmed")] and pay["notes"] == [] and pay["mem"] == [(1, "2637")]


def test_mark_paid_can_notify_the_customer(pay):
    pay["call"](True, notify=True)
    assert len(pay["notes"]) == 1 and "ORDER-9" in pay["notes"][0]


def test_mark_paid_twice_is_idempotent(pay):
    pay["order"] = _order(payment_status="paid")
    out = pay["call"](True)
    assert out["changed"] is False and pay["claims"] == [] and pay["notes"] == []


def test_mark_paid_does_not_touch_a_status_that_is_already_further_along(pay):
    pay["order"] = _order(status="preparing")
    pay["call"](True)
    assert pay["status"] == []


@pytest.mark.parametrize("bad", [{"status": "cancelled", "payment_status": "cancelled"},
                                 {"status": "refunded", "payment_status": "refunded"}, {"status": "cancelled"}])
@pytest.mark.parametrize("paid", [True, False])
def test_closed_orders_cannot_change_payment(pay, bad, paid):
    pay["order"] = _order(**bad)
    with pytest.raises(HTTPException) as e:
        pay["call"](paid)
    assert e.value.status_code == 409 and pay["claims"] == [] and pay["updates"] == []


def test_a_cancel_that_lands_during_the_claim_is_refused(pay, monkeypatch):
    def claim(*a):
        pay["order"] = _order(status="cancelled", payment_status="cancelled")
        return False
    monkeypatch.setattr(crud, "claim_order_paid", claim)
    with pytest.raises(HTTPException) as e:
        pay["call"](True)
    assert e.value.status_code == 409 and pay["notes"] == []


def test_a_database_error_on_the_claim_is_retryable(pay, monkeypatch):
    def claim(*a): raise RuntimeError("db")
    monkeypatch.setattr(crud, "claim_order_paid", claim)
    with pytest.raises(HTTPException) as e:
        pay["call"](True)
    assert e.value.status_code == 503


def test_mark_unpaid_puts_a_paid_order_back_to_waiting(pay):
    pay["order"] = _order(payment_status="paid", status="confirmed")
    out = pay["call"](False)
    assert out["payment_status"] == "awaiting_payment" and pay["updates"] == [(9, 1, {"payment_status": "awaiting_payment"})]
    assert pay["status"] == []                                       # order status untouched


def test_mark_unpaid_on_a_cash_order_goes_to_pending_cash(pay):
    pay["order"] = _order(payment_status="paid", payment_method="cash")
    assert pay["call"](False)["payment_status"] == "pending_cash"


def test_mark_unpaid_on_an_unpaid_order_changes_nothing(pay):
    out = pay["call"](False)
    assert out["changed"] is False and pay["updates"] == []


def test_another_business_cannot_change_the_payment(pay):
    pay["order"] = _order(business_id=2)
    with pytest.raises(HTTPException) as e:
        pay["call"](True)
    assert e.value.status_code == 404 and pay["claims"] == []


def test_the_route_is_registered_and_blocks_support_sessions():
    import inspect
    sig = inspect.signature(ar.set_order_payment_state)
    assert "require_not_impersonated" in str(sig.parameters["user"].default.dependency.__name__)
    assert any(getattr(r, "path", "") == "/orders/{order_id}/payment" and "POST" in r.methods for r in ar.router.routes)


# ═════════════ (3) Stripe Connect ═════════════
import billing.stripe_service as ss


@pytest.mark.parametrize("name,msg,expect", [
    ("InvalidRequestError", "You can only create new accounts if you've signed up for Connect", "Connect is not enabled"),
    ("AuthenticationError", "Invalid API Key provided: sk_live_***abc", "API key"),
    ("InvalidRequestError", "Invalid email address: ", "email"),
    ("APIConnectionError", "network", "Could not reach Stripe"),
    ("InvalidRequestError", "something unexpected", "could not complete this request"),
])
def test_connect_error_messages_are_specific_and_safe(name, msg, expect):
    exc = type(name, (Exception,), {})(msg)
    out = ss._connect_error_message(exc)
    assert expect in out and "sk_" not in out


class _FakeStripe:
    def __init__(self, fail=None):
        self.created, self.links, self.modified, self.fail = [], [], [], fail
        outer = self

        class Account:
            @staticmethod
            def create(**kw):
                if outer.fail:
                    raise outer.fail
                outer.created.append(kw)
                return {"id": "acct_1"}

            @staticmethod
            def modify(aid, **kw): outer.modified.append((aid, kw))

        class AccountLink:
            @staticmethod
            def create(**kw):
                outer.links.append(kw)
                return {"url": "https://connect.stripe.com/setup/x"}
        self.Account, self.AccountLink = Account, AccountLink


@pytest.fixture
def connect(monkeypatch):
    import core.db as coredb

    class Q:
        def select(self, *a): return self
        def eq(self, *a): return self
        def limit(self, *a): return self
        def update(self, *a): return self
        def execute(self): return types.SimpleNamespace(data=[{}])
    monkeypatch.setattr(coredb, "supabase", types.SimpleNamespace(table=lambda n: Q()))
    fake = _FakeStripe()
    monkeypatch.setattr(ss, "_stripe", lambda: fake)
    monkeypatch.setenv("WAZIBOT_URL", "https://example.test")
    return fake


def test_connect_requests_capabilities_and_returns_to_the_dashboard(connect):
    out = ss.create_connect_account(1, "Shop", "owner@example.com")
    assert out["url"].startswith("https://connect.stripe.com")
    kw = connect.created[0]
    assert kw["email"] == "owner@example.com" and kw["capabilities"]["transfers"] == {"requested": True}
    assert kw["capabilities"]["card_payments"] == {"requested": True}
    link = connect.links[0]
    assert link["return_url"] == "https://example.test/dashboard?stripe_connect=success"
    assert link["refresh_url"] == "https://example.test/dashboard?stripe_connect=refresh"


@pytest.mark.parametrize("email", ["", None, "   ", "not-an-email", "a@b"])
def test_connect_never_sends_an_empty_or_invalid_email(connect, email):
    ss.create_connect_account(1, "Shop", email)
    assert "email" not in connect.created[0]


def test_connect_failure_returns_the_specific_message(connect, monkeypatch):
    connect.fail = type("InvalidRequestError", (Exception,), {})("signed up for Connect")
    out = ss.create_connect_account(1, "Shop", "owner@example.com")
    assert "Connect is not enabled" in out["error"]


# ═════════════ (4)/(5) front-end wiring ═════════════
JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf8")
HTML = (ROOT / "static" / "dashboard.html").read_text(encoding="utf8")


def test_order_drawer_has_payment_controls():
    for needle in ("order-drawer-markpaid-btn", "order-drawer-markunpaid-btn", "order-drawer-pay-badge"):
        assert needle in HTML
    assert "setOrderPaymentFromDrawer" in JS and "`/orders/${order.id}/payment`" in JS
    assert re.search(r"openOrderDrawer[\s\S]{0,4000}_renderOrderDrawerPayment\(order\)", JS)


def test_conversations_can_load_older_messages():
    assert "loadOlderChatMessages" in JS and "offset=${offset}" in JS and "_chatScrollHandler" in JS
    assert "Load older messages" in JS


@pytest.mark.parametrize("platform", ["whatsapp", "telegram", "facebook", "messenger", "x", "linkedin", "reddit",
                                      "pinterest", "email", "sms", "instagram", "tiktok", "native"])
def test_referral_share_platforms_are_wired(platform):
    assert f"shareRef('{platform}')" in HTML and f"case '{platform}'" in JS


def test_existing_referral_buttons_are_still_there():
    for needle in ("shareRefWhatsApp()", "copyRef('code')", "copyRef('link')", "copyRefMessage()", "submitReferralWithdrawal()"):
        assert needle in HTML


# ── Stripe Connect: a saved account id that this platform cannot use (seen in production logs) ──
STALE_TEXT = "You requested an account link for an account that is not connected to your platform or does not exist."


class _StaleStripe(_FakeStripe):
    def __init__(self, link_error_for=None, create_error=None):
        super().__init__()
        self.link_error_for, self.create_error, self.n = set(link_error_for or ()), create_error, 0
        outer = self
        base_create = self.Account.create

        class Account:
            @staticmethod
            def create(**kw):
                if outer.create_error:
                    raise outer.create_error
                outer.n += 1
                outer.created.append(kw)
                return {"id": f"acct_new{outer.n}"}

            @staticmethod
            def modify(aid, **kw):
                raise type("PermissionError", (Exception,), {})("platform_account_required")

        class AccountLink:
            @staticmethod
            def create(**kw):
                if kw["account"] in outer.link_error_for:
                    raise type("InvalidRequestError", (Exception,), {})(STALE_TEXT)
                outer.links.append(kw)
                return {"url": "https://connect.stripe.com/setup/" + kw["account"]}
        self.Account, self.AccountLink = Account, AccountLink


@pytest.fixture
def stale(monkeypatch):
    import core.db as coredb
    db = {"id": "acct_old", "updates": []}

    class Q:
        def select(self, *a): return self
        def eq(self, *a): return self
        def limit(self, *a): return self
        def update(self, row): db["updates"].append(row); return self
        def execute(self): return types.SimpleNamespace(data=[{"stripe_account_id": db["id"]}])
    monkeypatch.setattr(coredb, "supabase", types.SimpleNamespace(table=lambda n: Q()))
    monkeypatch.setenv("WAZIBOT_URL", "https://example.test")
    return db


def test_a_saved_account_this_platform_cannot_use_is_replaced(stale, monkeypatch):
    fake = _StaleStripe(link_error_for={"acct_old"})
    monkeypatch.setattr(ss, "_stripe", lambda: fake)
    out = ss.create_connect_account(3, "Flavoury Foods", "owner@example.com")
    assert out["account_id"] == "acct_new1" and out["url"].endswith("acct_new1")
    assert stale["updates"] == [{"stripe_account_id": "acct_new1"}]


def test_the_saved_account_is_kept_when_a_new_one_cannot_be_created(stale, monkeypatch):
    # Platform without Connect: creating fails, so the old id must not be overwritten.
    fake = _StaleStripe(link_error_for={"acct_old"},
                        create_error=type("InvalidRequestError", (Exception,), {})("You can only create new accounts if you've signed up for Connect"))
    monkeypatch.setattr(ss, "_stripe", lambda: fake)
    out = ss.create_connect_account(3, "Shop", "owner@example.com")
    assert "Connect is not enabled" in out["error"] and stale["updates"] == []


def test_an_unrelated_link_error_does_not_replace_the_saved_account(stale, monkeypatch):
    fake = _StaleStripe()
    monkeypatch.setattr(ss, "_stripe", lambda: fake)

    def boom(**kw):
        raise type("APIConnectionError", (Exception,), {})("network")
    fake.AccountLink.create = staticmethod(boom)
    out = ss.create_connect_account(3, "Shop", "owner@example.com")
    assert "Could not reach Stripe" in out["error"] and fake.created == [] and stale["updates"] == []


def test_a_healthy_saved_account_is_reused(stale, monkeypatch):
    fake = _StaleStripe()
    monkeypatch.setattr(ss, "_stripe", lambda: fake)
    out = ss.create_connect_account(3, "Shop", "owner@example.com")
    assert out["account_id"] == "acct_old" and fake.created == []


def test_platform_account_required_is_reported_as_connect_not_enabled():
    exc = type("PermissionError", (Exception,), {"code": "platform_account_required"})(
        "Only Stripe Connect platforms can work with other accounts.")
    assert "Connect is not enabled" in ss._connect_error_message(exc)


def test_a_brand_new_account_is_never_replaced_again_in_the_same_call(stale, monkeypatch):
    stale["id"] = None
    fake = _StaleStripe(link_error_for={"acct_new1"})
    monkeypatch.setattr(ss, "_stripe", lambda: fake)
    out = ss.create_connect_account(3, "Shop", "owner@example.com")
    assert "error" in out and len(fake.created) == 1
