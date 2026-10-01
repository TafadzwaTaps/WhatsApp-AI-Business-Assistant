"""
tests/test_security_audit_production_hardening.py — WaziBot production
secrets/auth/API security audit (read: do not re-run the audit, these are
regression tests for what it fixed).

Covers:
  1. routes/chat_routes.py — the "shared number" cross-tenant customer
     fallback in /chat/send and /chat/conversations/{id}/close used to
     resolve ANY customer_id regardless of which business it belonged to,
     as soon as a platform-wide shared number was configured at all. Now
     it only crosses business_id when BOTH the caller's business and the
     target customer's business have use_shared_number=True.
  2. services/security.verify_meta_signature — an unset WHATSAPP_APP_SECRET
     used to always skip signature verification (fail open everywhere,
     including on an actual Render deployment). Now it only fails open
     when RENDER is unset (local dev / CI); on Render it fails closed.
  3. billing/stripe_service.handle_stripe_webhook — a retried Stripe
     webhook delivery used to be reprocessed every time (no idempotency).
     Now a second delivery of the same event_id is skipped.
  4. routes/auth_routes.login — a legacy plaintext-password account is now
     transparently upgraded to a bcrypt hash on successful login.
  5. routes/business_routes./products/import-csv — previously had no
     upload size cap at all; now rejects anything over 10MB before
     buffering it into memory.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clear_overrides(client):
    yield
    client.app.dependency_overrides.clear()


def _override_business(client, business_id=7, username="shopowner"):
    import routes.business_routes as br
    client.app.dependency_overrides[br.require_business] = lambda: {
        "business_id": business_id, "username": username, "role": "business",
    }


# ── 1. Shared-number cross-tenant customer lookup ───────────────────────────

def test_shared_number_lookup_refuses_when_caller_is_not_shared(monkeypatch):
    import routes.chat_routes as cr
    monkeypatch.setattr(cr, "SHARED_WA_TOKEN", "shared-token")
    monkeypatch.setattr(cr, "SHARED_PHONE_NUMBER_ID", "shared-pid")

    # Caller's own business is NOT a shared-number tenant — the fallback
    # must refuse outright, without even querying for the customer.
    monkeypatch.setattr(cr.crud, "get_business_by_id", lambda bid: {"id": bid, "use_shared_number": False})

    result = cr._shared_number_customer_lookup(customer_id=999, bid=1)
    assert result is None


def test_shared_number_lookup_refuses_when_target_business_is_dedicated(monkeypatch):
    import routes.chat_routes as cr
    monkeypatch.setattr(cr, "SHARED_WA_TOKEN", "shared-token")
    monkeypatch.setattr(cr, "SHARED_PHONE_NUMBER_ID", "shared-pid")

    businesses = {
        1: {"id": 1, "use_shared_number": True},   # caller: shared
        2: {"id": 2, "use_shared_number": False},  # target: dedicated number
    }
    monkeypatch.setattr(cr.crud, "get_business_by_id", lambda bid: businesses.get(bid))

    class _FakeTable:
        def select(self, *_a, **_kw): return self
        def eq(self, *_a, **_kw): return self
        def limit(self, *_a, **_kw): return self
        def execute(self):
            class _R: data = [{"id": 999, "business_id": 2, "phone": "+100"}]
            return _R()

    class _FakeSupabase:
        def table(self, _name): return _FakeTable()

    monkeypatch.setitem(__import__("sys").modules, "core.db", type("_M", (), {"supabase": _FakeSupabase()})())

    # Business A (shared) trying to reach Business B's customer — but B is
    # on a dedicated number, so this must still be refused. This is the
    # exact exploit scenario the original bug allowed.
    result = cr._shared_number_customer_lookup(customer_id=999, bid=1)
    assert result is None


def test_shared_number_lookup_allows_when_both_businesses_are_shared(monkeypatch):
    import routes.chat_routes as cr
    monkeypatch.setattr(cr, "SHARED_WA_TOKEN", "shared-token")
    monkeypatch.setattr(cr, "SHARED_PHONE_NUMBER_ID", "shared-pid")

    businesses = {
        1: {"id": 1, "use_shared_number": True},
        2: {"id": 2, "use_shared_number": True},
    }
    monkeypatch.setattr(cr.crud, "get_business_by_id", lambda bid: businesses.get(bid))

    class _FakeTable:
        def select(self, *_a, **_kw): return self
        def eq(self, *_a, **_kw): return self
        def limit(self, *_a, **_kw): return self
        def execute(self):
            class _R: data = [{"id": 999, "business_id": 2, "phone": "+100"}]
            return _R()

    class _FakeSupabase:
        def table(self, _name): return _FakeTable()

    monkeypatch.setitem(__import__("sys").modules, "core.db", type("_M", (), {"supabase": _FakeSupabase()})())

    # Both businesses genuinely share the number — this is the legitimate
    # case the fallback exists for, and it must still work.
    result = cr._shared_number_customer_lookup(customer_id=999, bid=1)
    assert result is not None
    assert result["business_id"] == 2


def test_shared_number_lookup_noop_when_not_configured_at_all(monkeypatch):
    import routes.chat_routes as cr
    monkeypatch.setattr(cr, "SHARED_WA_TOKEN", "")
    monkeypatch.setattr(cr, "SHARED_PHONE_NUMBER_ID", "")
    # Should short-circuit before ever touching crud/db.
    result = cr._shared_number_customer_lookup(customer_id=999, bid=1)
    assert result is None


# ── 2. WhatsApp webhook signature — fail-closed on Render when unset ───────

def test_webhook_signature_fails_open_without_render_env(monkeypatch):
    from services.security import verify_meta_signature
    monkeypatch.delenv("RENDER", raising=False)
    # Local/CI — unchanged dev-mode behavior, matches existing tests that
    # rely on this (test_webhook_intent_engine.py, test_webhook_media_and_voice.py).
    assert verify_meta_signature(b"payload", "", "") is True


def test_webhook_signature_fails_closed_on_render_when_unset(monkeypatch):
    from services.security import verify_meta_signature
    monkeypatch.setenv("RENDER", "true")
    assert verify_meta_signature(b"payload", "sha256=whatever", "") is False


def test_webhook_signature_still_verifies_normally_when_secret_set(monkeypatch):
    import hmac, hashlib
    from services.security import verify_meta_signature
    monkeypatch.setenv("RENDER", "true")
    secret = "a-real-secret"
    payload = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_meta_signature(payload, sig, secret) is True
    assert verify_meta_signature(payload, "sha256=deadbeef", secret) is False


# ── 3. Stripe webhook idempotency ───────────────────────────────────────────

def test_stripe_webhook_skips_already_processed_event(monkeypatch):
    import billing.stripe_service as svc

    class _FakeEvent(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)

    fake_event = {"id": "evt_123", "type": "customer.subscription.updated",
                  "data": {"object": {"id": "sub_1"}}}

    class _FakeWebhook:
        @staticmethod
        def construct_event(payload, sig, secret):
            return fake_event

    monkeypatch.setattr(svc, "_stripe", lambda: type("_S", (), {"Webhook": _FakeWebhook})())
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")

    monkeypatch.setattr("crud.stripe_webhook_events.already_processed", lambda event_id: True)
    called = {}
    monkeypatch.setattr(svc, "_on_subscription_updated", lambda data: called.setdefault("ran", True))

    result = svc.handle_stripe_webhook(b"payload", "sig")
    assert result.get("duplicate") is True
    assert "ran" not in called  # handler must NOT run for an already-processed event


def test_stripe_webhook_processes_new_event_and_marks_it(monkeypatch):
    import billing.stripe_service as svc

    fake_event = {"id": "evt_456", "type": "customer.subscription.updated",
                  "data": {"object": {"id": "sub_1"}}}

    class _FakeWebhook:
        @staticmethod
        def construct_event(payload, sig, secret):
            return fake_event

    monkeypatch.setattr(svc, "_stripe", lambda: type("_S", (), {"Webhook": _FakeWebhook})())
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")

    monkeypatch.setattr("crud.stripe_webhook_events.already_processed", lambda event_id: False)
    marked = {}
    monkeypatch.setattr("crud.stripe_webhook_events.mark_processed",
                         lambda event_id, event_type: marked.update(id=event_id, type=event_type))
    called = {}
    monkeypatch.setattr(svc, "_on_subscription_updated", lambda data: called.setdefault("ran", True))

    result = svc.handle_stripe_webhook(b"payload", "sig")
    assert result.get("ok") is True
    assert called.get("ran") is True
    assert marked == {"id": "evt_456", "type": "customer.subscription.updated"}


# ── 4. Legacy plaintext password upgraded to bcrypt on login ───────────────

def test_login_upgrades_legacy_plaintext_password_to_bcrypt(client, monkeypatch):
    import routes.auth_routes as ar

    biz = {"id": 7, "owner_username": "shopowner", "owner_password": "plaintextpw",
           "is_active": True, "name": "Biz"}
    monkeypatch.setattr(ar.crud, "get_business_by_username", lambda u: biz)
    updated = {}
    monkeypatch.setattr(ar.crud, "update_business", lambda bid, data: updated.update(bid=bid, data=dict(data)))
    for fn in ("record_failed_login", "record_failed_login_ip", "record_failed_login_account",
               "clear_failed_logins", "clear_account_login_lockout"):
        monkeypatch.setattr(ar, fn, lambda *a, **kw: None)
    monkeypatch.setattr(ar, "is_login_locked", lambda *a, **kw: False)
    monkeypatch.setattr(ar, "check_ip_login_limit", lambda *a, **kw: None)
    monkeypatch.setattr(ar, "check_account_login_lockout", lambda *a, **kw: None)
    monkeypatch.setattr(ar, "_rate_check", lambda *a, **kw: None)

    resp = client.post("/auth/login", json={"username": "shopowner", "password": "plaintextpw"})
    assert resp.status_code == 200
    assert updated["bid"] == 7
    assert updated["data"]["owner_password"].startswith(("$2b$", "$2a$", "$2y$"))
    assert updated["data"]["owner_password"] != "plaintextpw"


# ── 5. CSV import size cap ──────────────────────────────────────────────────

def test_csv_import_rejects_oversized_file(client):
    _override_business(client)
    big_csv = b"name,price\n" + (b"a,1\n" * 3_000_000)  # well over 10MB
    assert len(big_csv) > 10 * 1024 * 1024
    resp = client.post(
        "/products/import-csv",
        files={"file": ("products.csv", big_csv, "text/csv")},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["detail"].lower()
