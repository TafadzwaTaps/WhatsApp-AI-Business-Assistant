"""
tests/test_billing_lifecycle.py — Stripe subscription lifecycle (mocked Stripe,
in-memory database). No network, no real keys, no real customers.

Covers: checkout, webhook security/idempotency/retry/ordering, renewals,
failed payments & recovery, cancellation (scheduled / portal / deleted),
upgrades & downgrades, trial interplay, portal/invoice authorization,
cross-tenant isolation, reconciliation and secret hygiene.
"""

import hashlib
import hmac
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

import billing.stripe_service as svc

stripe_lib = pytest.importorskip("stripe")   # real library: used to verify real signatures

NOW = int(time.time())
DAY = 86400
EXT_COLS = set(svc._BILLING_EXT_COLS)


# ── In-memory Supabase ──────────────────────────────────────────────────────

class _Res:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload, self.filters, self.cols, self.lim = "select", None, [], "", None

    def select(self, cols="*", **_): self.op, self.cols = "select", cols; return self
    def insert(self, row, **_): self.op, self.payload = "insert", row; return self
    def update(self, patch, **_): self.op, self.payload = "update", patch; return self
    def upsert(self, row, **_): self.op, self.payload = "upsert", row; return self
    def delete(self, **_): self.op = "delete"; return self
    def eq(self, c, v): self.filters.append((c, v)); return self
    def order(self, *a, **k): return self
    def limit(self, n): self.lim = n; return self

    def _match(self, r): return all(r.get(c) == v for c, v in self.filters)

    def execute(self):
        db, rows = self.db, self.db.t.setdefault(self.name, [])
        if self.op == "select":
            if db.missing & {c.strip() for c in self.cols.split(",")}:
                raise Exception("PGRST204 column does not exist")
            out = [dict(r) for r in rows if self._match(r)]
            return _Res(out[: self.lim] if self.lim else out)
        keys = set(self.payload or {})
        if self.name == "businesses" and (db.fail_biz_writes or db.missing & keys):
            raise Exception("simulated database failure" if db.fail_biz_writes else "PGRST204 column does not exist")
        if self.op == "insert":
            if self.name == "stripe_webhook_events" and any(r["event_id"] == self.payload["event_id"] for r in rows):
                raise Exception("duplicate key value violates unique constraint")
            row = dict(self.payload)
            row.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
            rows.append(row)
            return _Res([row])
        if self.op == "upsert":
            for r in rows:
                if r.get("event_id") == self.payload.get("event_id"):
                    r.update(self.payload); return _Res([r])
            rows.append(dict(self.payload)); return _Res([self.payload])
        if self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit: r.update(self.payload)
            return _Res([dict(r) for r in hit])
        return _Res([])


class FakeDB:
    def __init__(self):
        self.t = {"businesses": [], "stripe_webhook_events": [], "subscription_events": []}
        self.missing, self.fail_biz_writes = set(), False

    def table(self, name): return _Q(self, name)
    def biz(self, bid): return next(r for r in self.t["businesses"] if r["id"] == bid)


# ── Fake Stripe ─────────────────────────────────────────────────────────────

class _StripeErr(Exception):
    def __init__(self, code="resource_missing"): super().__init__(code); self.code = code


def _price(pid, interval="month", amount=499, live=False, active=True, currency="usd"):
    return {"id": pid, "active": active, "livemode": live, "currency": currency, "unit_amount": amount,
            "recurring": {"interval": interval, "interval_count": 1}, "metadata": {}}


def make_sub(sub_id="sub_1", cust="cus_1", bid=1, price="price_sm", status="active", cancel=False,
             tier="starter", period_end=None):
    return {
        "id": sub_id, "customer": cust, "status": status, "cancel_at_period_end": cancel,
        "current_period_start": NOW, "current_period_end": period_end or NOW + 30 * DAY,
        "metadata": {"wazibot_business_id": str(bid), "tier": tier},
        "items": {"data": [{"id": "si_1", "price": _price(price)}]},
    }


class FakeStripe:
    def __init__(self):
        self.subs, self.customers, self.sessions, self.modifies = {}, {}, [], []
        self.portal_calls, self.refunds = [], 0
        self.prices = {"price_sm": _price("price_sm", "month", 499), "price_sa": _price("price_sa", "year", 4800),
                       "price_gm": _price("price_gm", "month", 1200), "price_ga": _price("price_ga", "year", 11500)}
        self.invoices = []
        self.apply_price_on_modify = True
        outer = self
        self.Webhook = stripe_lib.Webhook

        class Subscription:
            @staticmethod
            def retrieve(sid):
                if sid not in outer.subs: raise _StripeErr()
                return json.loads(json.dumps(outer.subs[sid]))
            @staticmethod
            def list(customer, status="all", limit=10):
                return {"data": [s for s in outer.subs.values() if s["customer"] == customer]}
            @staticmethod
            def modify(sid, **kw):
                outer.modifies.append((sid, kw))
                s = outer.subs[sid]
                if "cancel_at_period_end" in kw: s["cancel_at_period_end"] = kw["cancel_at_period_end"]
                if "items" in kw and outer.apply_price_on_modify:
                    s["items"]["data"][0]["price"] = outer.prices[kw["items"][0]["price"]]
                return json.loads(json.dumps(s))

        class Customer:
            @staticmethod
            def retrieve(cid):
                if cid not in outer.customers: raise _StripeErr()
                return outer.customers[cid]
            @staticmethod
            def create(**kw):
                cid = f"cus_new{len(outer.customers) + 1}"
                outer.customers[cid] = {"id": cid, "metadata": kw.get("metadata", {})}
                return outer.customers[cid]

        class Price:
            @staticmethod
            def retrieve(pid):
                if pid not in outer.prices: raise _StripeErr()
                return outer.prices[pid]

        class _CS:
            @staticmethod
            def create(**kw):
                outer.sessions.append(kw)
                return {"id": f"cs_{len(outer.sessions)}", "url": "https://checkout.stripe.test/x"}

        class _PS:
            @staticmethod
            def create(**kw):
                outer.portal_calls.append(kw)
                return {"url": "https://billing.stripe.test/p"}

        class Invoice:
            @staticmethod
            def list(customer, limit=12):
                return {"data": [i for i in outer.invoices if i["customer"] == customer]}

        self.Subscription, self.Customer, self.Price, self.Invoice = Subscription, Customer, Price, Invoice
        self.checkout = types.SimpleNamespace(Session=_CS)
        self.billing_portal = types.SimpleNamespace(Session=_PS)


WH_SECRET = "whsec_unit_test_secret"


def signed(event: dict, secret=WH_SECRET, t=None):
    payload = json.dumps(event).encode()
    t = t or int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, f"t={t},v1={sig}"


def ev(eid, etype, obj, livemode=False):
    return {"id": eid, "object": "event", "type": etype, "livemode": livemode,
            "data": {"object": obj}, "created": NOW}


class Mailbox:
    def __init__(self): self.sent = []


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_unit_secret_value")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WH_SECRET)
    monkeypatch.setenv("WAZIBOT_URL", "https://wazibothq.com")
    monkeypatch.setenv("BILLING_PAST_DUE_GRACE_DAYS", "3")
    for k, v in {"STARTER_MONTHLY": "price_sm", "STARTER_ANNUAL": "price_sa",
                 "GROWTH_MONTHLY": "price_gm", "GROWTH_ANNUAL": "price_ga"}.items():
        monkeypatch.setenv(f"STRIPE_PRICE_{k}", v)
    db, fs, mail = FakeDB(), FakeStripe(), Mailbox()
    db.t["businesses"] = [
        {"id": 1, "name": "Biz One", "owner_email": "one@example.com", "subscription_tier": "free",
         "billing_status": "trialing", "trial_ends_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
         "stripe_customer_id": "cus_1", "stripe_subscription_id": None},
        {"id": 2, "name": "Biz Two", "owner_email": "two@example.com", "subscription_tier": "growth",
         "billing_status": "active", "trial_ends_at": None,
         "stripe_customer_id": "cus_2", "stripe_subscription_id": "sub_2"},
    ]
    fs.customers["cus_1"] = {"id": "cus_1", "metadata": {"wazibot_business_id": "1"}}
    fs.customers["cus_2"] = {"id": "cus_2", "metadata": {"wazibot_business_id": "2"}}
    fs.subs["sub_2"] = make_sub("sub_2", "cus_2", 2, "price_gm", tier="growth")

    import core.db as coredb
    import crud.stripe_webhook_events as ledger
    import crud.subscription_history as history
    monkeypatch.setattr(coredb, "supabase", db, raising=False)
    monkeypatch.setattr(ledger, "supabase", db)
    monkeypatch.setattr(history, "supabase", db)
    monkeypatch.setattr(svc, "_stripe", lambda: fs)
    svc._verify_price.__dict__.pop("_cache", None)

    fake_mail = types.ModuleType("services.email_service")
    for fn in ("send_subscription_confirmed", "send_subscription_cancelled", "send_payment_failed"):
        setattr(fake_mail, fn, (lambda name: lambda **kw: mail.sent.append((name, kw)))(fn))
    monkeypatch.setitem(sys.modules, "services.email_service", fake_mail)
    return types.SimpleNamespace(db=db, fs=fs, mail=mail)


def deliver(env, eid, etype, obj, **kw):
    payload, sig = signed(ev(eid, etype, obj, **kw))
    return svc.handle_stripe_webhook(payload, sig)


def activate(env, bid=1, sub_id="sub_1", cust="cus_1", price="price_sm", tier="starter"):
    env.fs.subs[sub_id] = make_sub(sub_id, cust, bid, price, tier=tier)
    session = {"id": "cs_x", "mode": "subscription", "payment_status": "paid", "subscription": sub_id,
               "customer": cust, "metadata": {"wazibot_business_id": str(bid)}, "client_reference_id": str(bid)}
    return deliver(env, f"evt_act_{sub_id}", "checkout.session.completed", session)


# ═══ Checkout ═══════════════════════════════════════════════════════════════

def test_checkout_uses_server_price_and_trusted_customer(env):
    r = svc.create_checkout_session(1, "starter", "monthly", customer_email="x@example.com")
    assert r["url"].startswith("https://checkout.stripe.test")
    p = env.fs.sessions[0]
    assert p["line_items"] == [{"price": "price_sm", "quantity": 1}]
    assert p["customer"] == "cus_1" and "customer_email" not in p     # no throw-away customers
    assert p["metadata"]["wazibot_business_id"] == "1" and p["client_reference_id"] == "1"
    assert p["idempotency_key"]
    for rejected in ("ui_mode", "branding_settings", "origin_context", "integration_identifier"):
        assert rejected not in p          # not valid on the pinned API version
    assert "trial_period_days" not in p["subscription_data"]       # WaziBot owns the trial


@pytest.mark.parametrize("tier,period", [("platinum", "monthly"), ("starter", "weekly"), ("", "monthly"), ("free", "monthly")])
def test_checkout_rejects_invalid_plan_or_interval(env, tier, period):
    assert "error" in svc.create_checkout_session(1, tier, period)
    assert env.fs.sessions == []


def test_checkout_enterprise_is_contact_sales(env):
    r = svc.create_checkout_session(1, "enterprise", "monthly")
    assert r["code"] == "enterprise_contact_sales" and env.fs.sessions == []


def test_checkout_ignores_client_supplied_price_id(env):
    r = svc.create_checkout_session(1, "starter", "monthly", price_id_override="price_attacker_free")
    assert env.fs.sessions[0]["line_items"][0]["price"] == "price_sm"
    # an allow-listed price for a DIFFERENT plan is also refused as an override
    svc.create_checkout_session(1, "starter", "monthly", price_id_override="price_gm")
    assert env.fs.sessions[1]["line_items"][0]["price"] == "price_sm"


def test_checkout_rejects_foreign_redirect_urls(env):
    svc.create_checkout_session(1, "starter", "monthly", success_url="https://evil.example/steal", cancel_url="//evil")
    p = env.fs.sessions[0]
    assert p["success_url"].startswith("https://wazibothq.com/") and p["cancel_url"].startswith("https://wazibothq.com/")
    svc.create_checkout_session(1, "starter", "monthly", success_url="https://wazibothq.com/static/dashboard.html")
    assert env.fs.sessions[1]["success_url"] == "https://wazibothq.com/static/dashboard.html"


def test_checkout_blocks_duplicate_subscription_and_double_click(env):
    activate(env)
    r = svc.create_checkout_session(1, "growth", "monthly")
    assert r["code"] == "already_subscribed" and env.fs.sessions == []


def test_checkout_same_request_twice_uses_same_idempotency_key(env):
    svc.create_checkout_session(1, "starter", "monthly")
    svc.create_checkout_session(1, "starter", "monthly")
    assert env.fs.sessions[0]["idempotency_key"] == env.fs.sessions[1]["idempotency_key"]


def test_checkout_creates_single_customer_for_new_business(env):
    env.db.t["businesses"].append({"id": 3, "name": "Biz Three", "owner_email": "t@example.com",
                                   "subscription_tier": "free", "billing_status": "trialing",
                                   "stripe_customer_id": None, "stripe_subscription_id": None})
    svc.create_checkout_session(3, "starter", "monthly")
    assert env.db.biz(3)["stripe_customer_id"].startswith("cus_new")


def test_checkout_refuses_unverified_customer_mapping(env):
    env.db.biz(1)["stripe_customer_id"] = "cus_2"            # points at someone else's customer
    r = svc.create_checkout_session(1, "starter", "monthly")
    assert "error" in r and env.fs.sessions == []


def test_checkout_does_not_grant_access(env):
    before = dict(env.db.biz(1))
    svc.create_checkout_session(1, "starter", "monthly")
    assert env.db.biz(1) == before


def test_checkout_config_errors_are_clean(env, monkeypatch):
    monkeypatch.delenv("STRIPE_PRICE_STARTER_MONTHLY")
    r = svc.create_checkout_session(1, "starter", "monthly")
    assert "STRIPE_PRICE_" in r["error"] and "sk_test" not in r["error"]


def test_price_interval_and_environment_mismatch_blocked(env):
    env.fs.prices["price_sm"]["recurring"]["interval"] = "year"
    assert "error" in svc.create_checkout_session(1, "starter", "monthly")
    env.fs.prices["price_sm"] = _price("price_sm", "month", live=True)      # live price + test key
    svc._verify_price.__dict__.pop("_cache", None)
    assert "different Stripe environment" in svc.create_checkout_session(1, "starter", "monthly")["error"]


# ═══ Checkout completion & initial payment ═════════════════════════════════

def test_unpaid_checkout_session_grants_nothing(env):
    env.fs.subs["sub_1"] = make_sub()
    r = deliver(env, "evt_1", "checkout.session.completed",
                {"id": "cs", "mode": "subscription", "payment_status": "unpaid", "subscription": "sub_1",
                 "customer": "cus_1", "metadata": {"wazibot_business_id": "1"}})
    assert r["ok"] and env.db.biz(1)["subscription_tier"] == "free" and env.db.biz(1)["billing_status"] == "trialing"


def test_initial_payment_activates_plan_from_price(env):
    assert activate(env)["ok"]
    b = env.db.biz(1)
    assert (b["subscription_tier"], b["billing_status"], b["stripe_subscription_id"]) == ("starter", "active", "sub_1")
    assert b["billing_interval"] == "monthly" and b["stripe_price_id"] == "price_sm"
    assert [m[0] for m in env.mail.sent] == ["send_subscription_confirmed"]
    assert any(e["new_tier"] == "starter" for e in env.db.t["subscription_events"])      # audit trail


def test_tier_comes_from_price_not_from_metadata(env):
    env.fs.subs["sub_1"] = make_sub(price="price_sm", tier="growth")        # metadata lies
    session = {"id": "cs", "mode": "subscription", "payment_status": "paid", "subscription": "sub_1",
               "customer": "cus_1", "metadata": {"wazibot_business_id": "1"}}
    deliver(env, "evt_m", "checkout.session.completed", session)
    assert env.db.biz(1)["subscription_tier"] == "starter"


def test_product_purchase_never_touches_merchant_plan(env, monkeypatch):
    called = []
    monkeypatch.setattr(svc, "_on_product_purchase_completed", lambda o: called.append(o))
    deliver(env, "evt_p", "checkout.session.completed",
            {"id": "cs_p", "mode": "payment", "metadata": {"wazibot_business_id": "1"}})
    assert called and env.db.biz(1)["subscription_tier"] == "free"


def test_incomplete_subscription_does_not_disturb_trial(env):
    env.fs.subs["sub_1"] = make_sub(status="incomplete")
    deliver(env, "evt_i", "customer.subscription.created", env.fs.subs["sub_1"])
    b = env.db.biz(1)
    assert b["billing_status"] == "trialing" and b["subscription_tier"] == "free"


# ═══ Webhook security / reliability ═════════════════════════════════════════

def test_valid_signature_accepted_invalid_rejected(env):
    env.fs.subs["sub_1"] = make_sub()
    payload, good = signed(ev("evt_s", "customer.subscription.updated", env.fs.subs["sub_1"]))
    assert svc.handle_stripe_webhook(payload, good)["ok"]
    payload, bad = signed(ev("evt_s2", "customer.subscription.updated", env.fs.subs["sub_1"]), secret="whsec_wrong")
    r = svc.handle_stripe_webhook(payload, bad)
    assert r["status"] == 400
    assert svc.handle_stripe_webhook(payload, "")["status"] == 400
    assert svc.handle_stripe_webhook(b"not json", good)["status"] == 400
    assert not any(e["event_id"] == "evt_s2" for e in env.db.t["stripe_webhook_events"])


def test_tampered_body_rejected(env):
    env.fs.subs["sub_1"] = make_sub()
    payload, sig = signed(ev("evt_t", "customer.subscription.updated", env.fs.subs["sub_1"]))
    assert svc.handle_stripe_webhook(payload.replace(b"sub_1", b"sub_9"), sig)["status"] == 400


def test_webhook_without_secret_configured_is_503(env, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    assert svc.handle_stripe_webhook(b"{}", "x")["status"] == 503


def test_wrong_environment_event_rejected(env):
    env.fs.subs["sub_1"] = make_sub()
    r = deliver(env, "evt_live", "customer.subscription.updated", env.fs.subs["sub_1"], livemode=True)
    assert r["status"] == 400 and env.db.biz(1)["subscription_tier"] == "free"


def test_repeated_event_is_idempotent(env):
    activate(env)
    mails = len(env.mail.sent)
    r = deliver(env, "evt_act_sub_1", "checkout.session.completed",
                {"id": "cs_x", "mode": "subscription", "payment_status": "paid", "subscription": "sub_1",
                 "customer": "cus_1", "metadata": {"wazibot_business_id": "1"}})
    assert r.get("duplicate") is True and len(env.mail.sent) == mails
    assert [e["status"] for e in env.db.t["stripe_webhook_events"]] == ["processed"]


def test_concurrent_delivery_is_told_to_retry(env):
    env.db.t["stripe_webhook_events"].append({"event_id": "evt_c", "event_type": "x", "status": "processing",
                                              "attempts": 1, "updated_at": datetime.now(timezone.utc).isoformat()})
    env.fs.subs["sub_1"] = make_sub()
    r = deliver(env, "evt_c", "customer.subscription.updated", env.fs.subs["sub_1"])
    assert r["status"] == 409 and env.db.biz(1)["subscription_tier"] == "free"


def test_database_failure_returns_5xx_then_retry_succeeds(env):
    env.fs.subs["sub_1"] = make_sub()
    env.db.fail_biz_writes = True
    r = deliver(env, "evt_r", "customer.subscription.updated", env.fs.subs["sub_1"])
    assert r["status"] == 500                                    # Stripe will retry
    assert env.db.t["stripe_webhook_events"][0]["status"] == "failed"
    assert env.db.biz(1)["subscription_tier"] == "free"
    env.db.fail_biz_writes = False
    r = deliver(env, "evt_r", "customer.subscription.updated", env.fs.subs["sub_1"])
    assert r["ok"] and env.db.biz(1)["subscription_tier"] == "starter"
    assert env.db.t["stripe_webhook_events"][0]["status"] == "processed"


def test_unknown_event_type_acknowledged(env):
    assert deliver(env, "evt_u", "customer.created", {"id": "cus_zz"})["ok"]


def test_missing_business_mapping_grants_nothing(env):
    s = make_sub("sub_9", "cus_unknown", 1)
    s["metadata"] = {}
    env.fs.subs["sub_9"] = s
    r = deliver(env, "evt_n", "customer.subscription.updated", s)
    assert r["ok"] and env.db.biz(1)["subscription_tier"] == "free" and env.db.biz(2)["subscription_tier"] == "growth"


def test_invalid_business_in_metadata_grants_nothing(env):
    s = make_sub("sub_9", "cus_1", 999)
    env.fs.subs["sub_9"] = s
    assert deliver(env, "evt_x", "customer.subscription.updated", s)["ok"]
    assert env.db.biz(1)["subscription_tier"] == "free"


def test_other_business_subscription_cannot_overwrite(env):
    activate(env)                                                # biz1 -> starter
    # subscription of biz2's customer claiming to belong to biz1
    s = make_sub("sub_evil", "cus_2", 1, "price_gm", tier="growth")
    env.fs.subs["sub_evil"] = s
    deliver(env, "evt_e", "customer.subscription.updated", s)
    assert env.db.biz(1)["subscription_tier"] == "starter" and env.db.biz(1)["stripe_subscription_id"] == "sub_1"
    assert env.db.biz(2)["stripe_subscription_id"] == "sub_2"


def test_events_for_one_business_do_not_affect_another(env):
    activate(env)
    s2 = env.fs.subs["sub_2"]; s2["status"] = "canceled"
    deliver(env, "evt_2c", "customer.subscription.deleted", s2)
    assert env.db.biz(2)["subscription_tier"] == "free"
    assert env.db.biz(1)["subscription_tier"] == "starter" and env.db.biz(1)["billing_status"] == "active"


def test_out_of_order_stale_event_does_not_regress_state(env):
    activate(env)
    stale = make_sub(status="past_due")                          # old payload says past_due...
    deliver(env, "evt_old", "customer.subscription.updated", stale)
    assert env.db.biz(1)["billing_status"] == "active"           # ...Stripe's current object says active


def test_stale_deleted_event_for_old_subscription_ignored(env):
    activate(env, sub_id="sub_new")
    old = make_sub("sub_old", "cus_1", 1, status="canceled")
    env.fs.subs["sub_old"] = old
    deliver(env, "evt_od", "customer.subscription.deleted", old)
    b = env.db.biz(1)
    assert (b["subscription_tier"], b["billing_status"], b["stripe_subscription_id"]) == ("starter", "active", "sub_new")


def test_second_live_subscription_never_overwrites_first(env):
    activate(env)
    env.fs.subs["sub_dup"] = make_sub("sub_dup", "cus_1", 1, "price_gm", tier="growth")
    deliver(env, "evt_d", "customer.subscription.created", env.fs.subs["sub_dup"])
    assert env.db.biz(1)["stripe_subscription_id"] == "sub_1" and env.db.biz(1)["subscription_tier"] == "starter"


def test_billing_works_before_migration_applied(env):
    env.db.missing = set(EXT_COLS)
    assert activate(env)["ok"]
    b = env.db.biz(1)
    assert (b["subscription_tier"], b["billing_status"]) == ("starter", "active")


# ═══ Renewal, failure, recovery ═════════════════════════════════════════════

def test_renewal_updates_period_without_side_effects(env):
    activate(env)
    env.db.biz(1)["name"] = "Custom Name"
    env.fs.subs["sub_1"]["current_period_end"] = NOW + 61 * DAY
    mails = len(env.mail.sent)
    deliver(env, "evt_inv1", "invoice.paid", {"id": "in_1", "subscription": "sub_1", "customer": "cus_1"})
    deliver(env, "evt_inv1b", "invoice.payment_succeeded", {"id": "in_1", "subscription": "sub_1", "customer": "cus_1"})
    b = env.db.biz(1)
    assert b["current_period_end"].startswith(datetime.fromtimestamp(NOW + 61 * DAY, tz=timezone.utc).strftime("%Y-%m-%d"))
    assert b["stripe_last_invoice_id"] == "in_1" and b["name"] == "Custom Name"
    assert len(env.mail.sent) == mails                           # renewals are silent


def test_failed_payment_sets_past_due_once(env):
    activate(env)
    env.fs.subs["sub_1"]["status"] = "past_due"
    inv = {"id": "in_f", "subscription": "sub_1", "customer": "cus_1"}
    deliver(env, "evt_f1", "invoice.payment_failed", inv)
    first = env.db.biz(1)["payment_failed_at"]
    assert env.db.biz(1)["billing_status"] == "past_due" and env.db.biz(1)["subscription_tier"] == "starter"
    deliver(env, "evt_f2", "invoice.payment_failed", inv)        # Stripe retry attempt
    assert env.db.biz(1)["payment_failed_at"] == first
    assert [m[0] for m in env.mail.sent].count("send_payment_failed") == 1


def test_recovery_after_failed_payment(env):
    activate(env)
    env.fs.subs["sub_1"]["status"] = "past_due"
    deliver(env, "evt_f", "invoice.payment_failed", {"id": "in_f", "subscription": "sub_1"})
    env.fs.subs["sub_1"]["status"] = "active"
    deliver(env, "evt_ok", "invoice.paid", {"id": "in_f", "subscription": "sub_1"})
    b = env.db.biz(1)
    assert b["billing_status"] == "active" and b["payment_failed_at"] is None


def test_past_due_grace_window_controls_access(env):
    from core import plan_guard as pg
    activate(env)
    b = env.db.biz(1)
    b["billing_status"] = "past_due"
    b["payment_failed_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    p = pg._get_business_plan(1)
    assert pg._normalise_tier(p["tier"], p["billing_status"], p["trial_ends_at"]) == "STARTER"
    b["payment_failed_at"] = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    p = pg._get_business_plan(1)
    assert p["billing_status"] == "unpaid"
    assert pg._normalise_tier(p["tier"], p["billing_status"], p["trial_ends_at"]) == "FREE"
    assert env.db.biz(1)["billing_status"] == "past_due"         # derived, stored value untouched


@pytest.mark.parametrize("status", ["cancelled", "unpaid", "paused", "incomplete"])
def test_non_entitled_statuses_never_grant_stale_paid_tier(status):
    from core.plan_guard import _normalise_tier
    assert _normalise_tier("growth", status, None) == "FREE"


# ═══ Cancellation ═══════════════════════════════════════════════════════════

def test_cancel_at_period_end_keeps_access_and_syncs(env):
    activate(env)
    r = svc.cancel_subscription(1)
    assert r["ok"] and r["access_until"] and env.fs.modifies[-1][1] == {"cancel_at_period_end": True}
    b = env.db.biz(1)
    assert b["cancel_at_period_end"] is True and b["billing_status"] == "active" and b["subscription_tier"] == "starter"
    assert [m[0] for m in env.mail.sent].count("send_subscription_cancelled") == 1
    deliver(env, "evt_cu", "customer.subscription.updated", env.fs.subs["sub_1"])    # Stripe echo
    assert [m[0] for m in env.mail.sent].count("send_subscription_cancelled") == 1


def test_cancellation_can_be_reversed(env):
    activate(env)
    svc.cancel_subscription(1)
    assert svc.resume_subscription(1)["ok"]
    assert env.db.biz(1)["cancel_at_period_end"] is False
    assert svc.resume_subscription(1)["code"] == "not_cancelling"


def test_cancel_without_subscription_is_clean_error(env):
    assert svc.cancel_subscription(1)["code"] == "no_subscription"
    assert env.fs.modifies == []


def test_cannot_cancel_via_foreign_subscription_mapping(env):
    env.db.biz(1)["stripe_subscription_id"] = "sub_2"            # tampered mapping (cus_2's sub)
    r = svc.cancel_subscription(1)
    assert r["code"] == "mismatch" and env.fs.modifies == []


def test_subscription_deleted_ends_paid_access_and_keeps_data(env):
    activate(env)
    env.db.biz(1)["name"] = "Keep Me"
    env.fs.subs["sub_1"]["status"] = "canceled"
    deliver(env, "evt_del", "customer.subscription.deleted", env.fs.subs["sub_1"])
    b = env.db.biz(1)
    assert (b["subscription_tier"], b["billing_status"]) == ("free", "cancelled")
    assert b["name"] == "Keep Me" and b["stripe_customer_id"] == "cus_1" and b["stripe_subscription_id"] == "sub_1"
    assert "send_subscription_cancelled" in [m[0] for m in env.mail.sent]
    # Customer can start a fresh subscription afterwards
    assert svc.create_checkout_session(1, "starter", "monthly").get("url")


def test_refund_and_dispute_events_do_not_change_access(env):
    activate(env)
    deliver(env, "evt_rf", "charge.refunded", {"id": "ch_1", "customer": "cus_1", "amount_refunded": 499, "currency": "usd"})
    deliver(env, "evt_dp", "charge.dispute.created", {"id": "dp_1", "customer": "cus_1", "amount": 499, "currency": "usd"})
    b = env.db.biz(1)
    assert (b["subscription_tier"], b["billing_status"]) == ("starter", "active")


# ═══ Plan changes ═══════════════════════════════════════════════════════════

def test_upgrade_requires_confirmation_then_prorates_on_same_subscription(env):
    activate(env)
    assert svc.change_plan(1, "growth", "monthly")["code"] == "confirmation_required"
    assert env.fs.modifies == []
    r = svc.change_plan(1, "growth", "monthly", confirm_proration=True)
    sid, kw = env.fs.modifies[-1]
    assert r["effective"] and sid == "sub_1" and kw["proration_behavior"] == "always_invoice"
    assert kw["payment_behavior"] == "pending_if_incomplete" and kw["items"][0]["price"] == "price_gm"
    assert env.db.biz(1)["subscription_tier"] == "growth"
    assert len(env.fs.subs) == 2                                  # still one subscription per business


def test_failed_upgrade_payment_keeps_old_plan(env):
    activate(env)
    env.fs.apply_price_on_modify = False                          # Stripe left it pending
    r = svc.change_plan(1, "growth", "monthly", confirm_proration=True)
    assert r["effective"] is False and env.db.biz(1)["subscription_tier"] == "starter"


def test_monthly_to_annual_is_an_upgrade_and_interval_recorded(env):
    activate(env)
    assert svc.change_plan(1, "starter", "annual", confirm_proration=True)["effective"]
    assert env.db.biz(1)["billing_interval"] == "annual"


def test_downgrade_and_shorter_interval_go_through_portal(env):
    activate(env, price="price_gm", tier="growth")
    for t, p in (("starter", "monthly"), ("growth", "monthly")):
        pass
    assert svc.change_plan(1, "starter", "monthly", confirm_proration=True)["code"] == "use_portal"
    env.fs.subs["sub_1"]["items"]["data"][0]["price"] = env.fs.prices["price_ga"]
    deliver(env, "evt_ann", "customer.subscription.updated", env.fs.subs["sub_1"])
    assert svc.change_plan(1, "growth", "monthly", confirm_proration=True)["code"] == "use_portal"
    assert env.fs.modifies == []                                  # no silent downgrade/credit


def test_plan_change_rejected_when_past_due_or_cancelling_or_invalid(env):
    activate(env)
    assert svc.change_plan(1, "enterprise", "monthly")["error"]
    assert svc.change_plan(1, "growth", "weekly")["error"]
    svc.cancel_subscription(1)
    assert svc.change_plan(1, "growth", "monthly", confirm_proration=True)["code"] == "cancel_scheduled"
    svc.resume_subscription(1)
    env.fs.subs["sub_1"]["status"] = "past_due"
    assert svc.change_plan(1, "growth", "monthly", confirm_proration=True)["code"] == "payment_required"


def test_portal_plan_change_is_synced_from_stripe(env):
    activate(env)
    env.fs.subs["sub_1"]["items"]["data"][0]["price"] = env.fs.prices["price_gm"]
    deliver(env, "evt_pp", "customer.subscription.updated", env.fs.subs["sub_1"])
    assert env.db.biz(1)["subscription_tier"] == "growth" and env.db.biz(1)["stripe_subscription_id"] == "sub_1"


# ═══ Trial ══════════════════════════════════════════════════════════════════

def test_trial_is_wazibot_controlled_and_not_reset(env):
    from core import plan_guard as pg
    p = pg._get_business_plan(1)
    assert pg._normalise_tier(p["tier"], p["billing_status"], p["trial_ends_at"]) == "PRO"
    ends = env.db.biz(1)["trial_ends_at"]
    for _ in range(3):                                            # repeated checkout attempts
        svc.create_checkout_session(1, "starter", "monthly")
    assert all("trial_period_days" not in s["subscription_data"] for s in env.fs.sessions)
    assert env.db.biz(1)["trial_ends_at"] == ends
    env.db.biz(1)["trial_ends_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    p = pg._get_business_plan(1)
    assert pg._normalise_tier(p["tier"], p["billing_status"], p["trial_ends_at"]) == "FREE"


def test_upgrading_during_trial_keeps_trial_date_history(env):
    ends = env.db.biz(1)["trial_ends_at"]
    activate(env)
    assert env.db.biz(1)["billing_status"] == "active" and env.db.biz(1)["trial_ends_at"] == ends


# ═══ Portal, invoices, status, tenant isolation ═════════════════════════════

def test_portal_uses_verified_server_side_customer(env):
    activate(env)
    r = svc.create_portal_session(1)
    assert r["url"] and env.fs.portal_calls[0]["customer"] == "cus_1"
    assert env.fs.portal_calls[0]["return_url"].startswith("https://wazibothq.com/")


def test_portal_refuses_tampered_customer_mapping(env):
    env.db.biz(1)["stripe_customer_id"] = "cus_2"
    r = svc.create_portal_session(1)
    assert r["code"] == "mismatch" and env.fs.portal_calls == []


def test_portal_requires_a_customer(env):
    env.db.biz(1)["stripe_customer_id"] = None
    assert svc.create_portal_session(1)["code"] == "no_customer"


def test_portal_route_accepts_no_client_supplied_customer():
    import inspect
    from routes_saas.billing_routes import billing_portal
    assert list(inspect.signature(billing_portal).parameters) == ["user"]


def test_invoice_history_scoped_to_own_customer(env):
    env.fs.invoices = [
        {"id": "in_a", "customer": "cus_1", "number": "A-1", "status": "paid", "amount_paid": 499, "amount_due": 0,
         "currency": "usd", "created": NOW, "hosted_invoice_url": "https://invoice.stripe.test/a", "invoice_pdf": "https://pdf"},
        {"id": "in_b", "customer": "cus_2", "number": "B-1", "status": "paid", "amount_paid": 1200, "amount_due": 0,
         "currency": "usd", "created": NOW, "hosted_invoice_url": "https://invoice.stripe.test/b", "invoice_pdf": ""},
    ]
    out = svc.list_invoices(1)
    assert [i["id"] for i in out["invoices"]] == ["in_a"] and out["invoices"][0]["amount_paid"] == 4.99
    assert out["invoices"][0]["currency"] == "USD"


def test_tenant_cannot_write_billing_fields_via_profile_update():
    from routes.business_routes import BusinessUpdate
    body = BusinessUpdate(**{"name": "x", "subscription_tier": "growth", "billing_status": "active",
                             "stripe_customer_id": "cus_victim", "stripe_subscription_id": "sub_victim",
                             "trial_ends_at": "2999-01-01"}).dict(exclude_none=True)
    assert body == {"name": "x"}


def test_status_reports_cancellation_and_hides_nothing_unverified(env):
    activate(env)
    svc.cancel_subscription(1)
    s = svc.get_subscription_status(1)
    assert s["cancel_at_period_end"] is True and s["access_ends_at"] and s["billing_status"] == "active"
    assert s["tier"] == "starter"


def test_status_features_drop_when_entitlement_lapsed(env):
    activate(env)
    b = env.db.biz(1)
    b["billing_status"] = "past_due"
    b["payment_failed_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    assert svc.get_subscription_status(1)["features"] == svc.TIERS["free"]


# ═══ HTTP layer ═════════════════════════════════════════════════════════════

@pytest.fixture
def client(env, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    try:
        from routes_saas import billing_routes as br
    except Exception as exc:                                       # pragma: no cover
        pytest.skip(f"billing_routes import chain unavailable: {exc}")
    from core.auth import require_business
    app = fastapi.FastAPI()
    app.include_router(br.router)
    app.dependency_overrides[require_business] = lambda: {"business_id": 1}
    return TestClient(app), app, require_business


def test_checkout_endpoint_requires_authentication(env):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from routes_saas import billing_routes as br
    app = fastapi.FastAPI(); app.include_router(br.router)
    c = TestClient(app)
    for method, path in (("post", "/billing/checkout"), ("post", "/billing/cancel"), ("post", "/billing/portal"),
                         ("post", "/billing/resume"), ("post", "/billing/change-plan"), ("get", "/billing/invoices"),
                         ("get", "/billing/status")):
        r = c.get(path) if method == "get" else c.post(path, json={"tier": "starter", "confirm": True})
        assert r.status_code in (401, 403), path
    assert env.fs.sessions == [] and env.fs.modifies == []


def test_checkout_endpoint_binds_to_token_business_not_body(client, env):
    c, _, _ = client
    r = c.post("/billing/checkout", json={"tier": "starter", "billing_period": "monthly", "business_id": 2,
                                          "price_id": "price_free", "amount": 1})
    assert r.status_code == 200
    p = env.fs.sessions[0]
    assert p["metadata"]["wazibot_business_id"] == "1" and p["customer"] == "cus_1"
    assert p["line_items"][0]["price"] == "price_sm"


def test_checkout_endpoint_status_codes(client, env):
    c, _, _ = client
    assert c.post("/billing/checkout", json={"tier": "bogus"}).status_code == 400
    activate(env)
    assert c.post("/billing/checkout", json={"tier": "growth"}).status_code == 409
    assert c.post("/billing/cancel", json={"confirm": False}).status_code == 400
    assert c.post("/billing/cancel", json={"confirm": True}).status_code == 200
    assert c.post("/billing/change-plan", json={"tier": "growth"}).status_code == 409


def test_webhook_endpoint_uses_raw_body_and_status_codes(client, env):
    c, _, _ = client
    env.fs.subs["sub_1"] = make_sub()
    payload, sig = signed(ev("evt_http", "customer.subscription.updated", env.fs.subs["sub_1"]))
    assert c.post("/billing/webhook", content=payload, headers={"stripe-signature": sig}).status_code == 200
    assert c.post("/billing/webhook", content=payload, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    env.db.fail_biz_writes = True
    p2, s2 = signed(ev("evt_http2", "customer.subscription.updated", env.fs.subs["sub_1"]))
    assert c.post("/billing/webhook", content=p2, headers={"stripe-signature": s2}).status_code == 500


def test_success_page_does_not_claim_activation(client):
    c, _, _ = client
    body = c.get("/billing/success?session_id=cs_test_1").json()
    assert "activated" not in body["message"].lower() and "session_id" not in body


# ═══ Reconciliation & hygiene ═══════════════════════════════════════════════

def test_reconcile_reports_without_changing_then_repairs_deterministically(env):
    activate(env)
    env.fs.subs["sub_1"]["status"] = "canceled"                   # missed webhook
    rep = svc.reconcile_billing(fix=False)
    item = next(d for d in rep["discrepancies"] if d["business_id"] == 1)
    assert "stripe_canceled_local_entitled" in item["kinds"] and rep["dry_run"] is True
    assert env.db.biz(1)["subscription_tier"] == "starter"       # report only
    rep2 = svc.reconcile_billing(fix=True)
    assert env.db.biz(1)["subscription_tier"] == "free" and rep2["repaired"] >= 1
    assert svc.reconcile_billing(fix=True)["repaired"] == 0       # idempotent
    assert env.fs.modifies == []                                  # never touches Stripe state


def test_reconcile_surfaces_failed_webhooks_and_missing_stripe_sub(env):
    env.db.t["stripe_webhook_events"].append({"event_id": "evt_bad", "event_type": "invoice.paid", "status": "failed",
                                              "attempts": 2, "last_error": "Boom", "updated_at": "now"})
    env.db.biz(1).update(stripe_subscription_id="sub_gone", subscription_tier="starter", billing_status="active")
    rep = svc.reconcile_billing()
    assert rep["failed_webhook_events"][0]["event_id"] == "evt_bad"
    assert any(d.get("kind") == "stripe_subscription_missing" for d in rep["discrepancies"])


def test_config_check_never_leaks_secrets(env):
    out = json.dumps(svc.billing_config_check())
    assert "sk_test_unit_secret_value" not in out and WH_SECRET not in out
    assert json.loads(out)["key_mode"] == "test" and json.loads(out)["ok"] is True


def test_config_check_flags_missing_configuration(env, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET"); monkeypatch.delenv("STRIPE_PRICE_GROWTH_ANNUAL")
    out = svc.billing_config_check()
    assert not out["ok"] and any("WEBHOOK" in p for p in out["problems"]) and any("growth_annual" in p for p in out["problems"])


def test_logs_contain_no_secrets_or_payment_data(env, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    activate(env)
    svc.cancel_subscription(1)
    svc.create_portal_session(1)
    env.fs.subs["sub_1"]["status"] = "past_due"
    deliver(env, "evt_lg", "invoice.payment_failed", {"id": "in_1", "subscription": "sub_1"})
    text = caplog.text
    for secret in ("sk_test_unit_secret_value", WH_SECRET, "one@example.com", "4242"):
        assert secret not in text


def test_missing_stripe_configuration_fails_safe(env, monkeypatch):
    monkeypatch.setattr(svc, "_stripe", lambda: None)
    assert "error" in svc.create_checkout_session(1, "starter", "monthly")
    assert "error" in svc.cancel_subscription(1) and "error" in svc.create_portal_session(1)
    assert "error" in svc.change_plan(1, "growth", "monthly", True)
    assert svc.list_invoices(1)["invoices"] == []
