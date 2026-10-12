"""
tests/test_system_audit_fixes.py — regression tests for the full-system audit.

Each test pins one confirmed defect:
  1. refresh tokens must not authenticate API calls
  2. suspended businesses lose API access (cache-bounded)
  3. PayPal webhook must fail closed on a real deployment when unconfigured
  4. PayPal success URL must not trust the client `reference`, and must check amount
  5. storefront Buy Now button: no script injection / apostrophe breakage
  6. 5xx responses never leak internal error text
  7. overlapping scheduled-campaign runs can't double-send
"""

import asyncio
import re
import sys
import types
from html.parser import HTMLParser

import pytest
from fastapi import HTTPException

import core.auth as auth


# ── 1 & 2. Authentication ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_auth_caches():
    auth._pwd_ts_cache.clear()
    auth._active_cache.clear()
    yield
    auth._pwd_ts_cache.clear()
    auth._active_cache.clear()


def _tok(kind="access", **extra):
    data = {"sub": "owner", "role": "business", "business_id": 7, **extra}
    return (auth.create_refresh_token if kind == "refresh" else auth.create_access_token)(data)


def test_access_token_still_works(fake_supabase):
    fake_supabase.results["businesses"] = [{"password_changed_at": None, "is_active": True}]
    assert auth.get_current_user(_tok())["business_id"] == 7


def test_refresh_token_cannot_be_used_as_access_token(fake_supabase):
    fake_supabase.results["businesses"] = [{"password_changed_at": None, "is_active": True}]
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(_tok("refresh"))
    assert e.value.status_code == 401


def test_refresh_token_still_valid_for_refresh_endpoint():
    payload = auth.decode_token(_tok("refresh"))
    assert payload["type"] == "refresh"          # /auth/refresh keeps working


def test_suspended_business_is_locked_out(fake_supabase):
    fake_supabase.results["businesses"] = [{"password_changed_at": None, "is_active": False}]
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(_tok())
    assert e.value.status_code == 403


def test_impersonation_of_suspended_business_still_allowed_for_support(fake_supabase):
    fake_supabase.results["businesses"] = [{"password_changed_at": None, "is_active": False}]
    t = auth.create_impersonation_token(7, "owner", "superadmin")
    assert auth.get_current_user(t)["impersonated_by"] == "superadmin"


@pytest.mark.parametrize("row", [{"password_changed_at": None}, {"password_changed_at": None, "is_active": None}])
def test_missing_or_null_is_active_means_active(fake_supabase, row):
    fake_supabase.results["businesses"] = [row]
    assert auth.get_current_user(_tok())["business_id"] == 7


def test_auth_lookup_failure_fails_open(monkeypatch):
    def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(sys.modules["core.db"].supabase, "table", boom, raising=False)
    assert auth.get_current_user(_tok())["business_id"] == 7


def test_password_change_still_invalidates_old_tokens(fake_supabase):
    fake_supabase.results["businesses"] = [{"password_changed_at": "2026-01-02", "is_active": True}]
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(_tok(pwd_ts="2026-01-01"))
    assert e.value.status_code == 401


# ── App-level tests (real FastAPI app) ──────────────────────────────────────

@pytest.fixture
def app_client():
    import main
    from fastapi.testclient import TestClient
    return main.app, TestClient(main.app, raise_server_exceptions=False)


# ── 3. PayPal webhook ───────────────────────────────────────────────────────

def test_paypal_webhook_fails_closed_in_production_without_webhook_id(app_client, monkeypatch):
    _, c = app_client
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    monkeypatch.setenv("RENDER", "true")
    r = c.post("/payments/paypal/webhook", json={"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}})
    assert r.status_code == 503


def test_paypal_webhook_dev_mode_unchanged_without_webhook_id(app_client, monkeypatch):
    _, c = app_client
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    r = c.post("/payments/paypal/webhook", json={"event_type": "SOMETHING.ELSE"})
    assert r.status_code == 200


def test_paypal_webhook_rejects_bad_signature_when_configured(app_client, monkeypatch):
    _, c = app_client
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-123")
    import services.payment_service as ps
    monkeypatch.setattr(ps, "verify_paypal_webhook_signature", lambda **k: False)
    assert c.post("/payments/paypal/webhook", json={"event_type": "X"}).status_code == 400


# ── 4. PayPal success URL ───────────────────────────────────────────────────

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_paypal_success_ignores_client_reference_for_order_lookup(monkeypatch):
    import routes.chat_routes as cr
    import services.payment_service as ps
    import workflows.order_lifecycle as ol
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: {
        "paid": True, "reference": "", "internal_order_id": None, "amount": 1.0})
    looked_up = []
    monkeypatch.setattr(ol, "get_order", lambda oid: looked_up.append(oid) or {"id": oid, "business_id": 1, "total_price": 50})
    updated = []
    monkeypatch.setattr(cr.crud, "update_order_payment", lambda *a, **k: updated.append(a))
    out = _run(cr.paypal_success(request=None, token="PP-1", PayerID="x", reference="ORDER-9"))
    assert out["status"] == "error" and looked_up == [] and updated == []   # victim order 9 untouched


def test_paypal_success_rejects_amount_mismatch(monkeypatch):
    import routes.chat_routes as cr
    import services.payment_service as ps
    import workflows.order_lifecycle as ol
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: {
        "paid": True, "reference": "ORDER-9", "internal_order_id": 9, "amount": 1.0})
    monkeypatch.setattr(ol, "get_order", lambda oid: {"id": 9, "business_id": 1, "total_price": 50.0, "payment_status": "pending"})
    updated = []
    monkeypatch.setattr(cr.crud, "update_order_payment", lambda *a, **k: updated.append(a))
    out = _run(cr.paypal_success(request=None, token="PP-1", PayerID="x", reference=""))
    assert out["status"] == "error" and updated == []


def test_paypal_success_marks_paid_when_amount_matches(monkeypatch):
    import routes.chat_routes as cr
    import services.payment_service as ps
    import workflows.order_lifecycle as ol
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: {
        "paid": True, "reference": "ORDER-9", "internal_order_id": 9, "amount": 50.0})
    monkeypatch.setattr(ol, "get_order", lambda oid: {"id": 9, "business_id": 1, "total_price": 50.0, "payment_status": "pending"})
    updated = []
    monkeypatch.setattr(cr.crud, "update_order_payment", lambda *a, **k: updated.append(a))

    async def _noop(*a, **k): return None
    monkeypatch.setattr(cr, "_notify_customer_payment", _noop)
    out = _run(cr.paypal_success(request=None, token="PP-1", PayerID="x", reference=""))
    assert out["status"] == "ok" and len(updated) == 1


# ── 5. Storefront XSS ───────────────────────────────────────────────────────

class _Attrs(HTMLParser):
    def __init__(self): super().__init__(); self.onclicks = []
    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "onclick" and v and v.startswith("wzBuyNow"): self.onclicks.append(v)


@pytest.mark.parametrize("name", ["x',alert(1),'", "Joe's Haircut", 'say "hi"', "back\\slash", "</script><b>"])
def test_buy_now_handler_is_a_single_safe_call(name):
    import json
    from services import site_generator as sg
    html = sg._product_card_html({"id": 5, "name": name, "price": 3, "stock": 1}, "$", "263", biz_name="B", business_id=7)
    p = _Attrs(); p.feed(html)
    assert len(p.onclicks) == 1
    call = p.onclicks[0]                       # exactly what the browser will execute
    assert call == f'wzBuyNow(7,"5",{json.dumps(name)},3.0,"$")'
    # round-trips as JSON arguments → the name can only ever be a string value
    args = json.loads("[" + call[len("wzBuyNow("):-1] + "]")
    assert args[2] == name


def test_cart_script_escapes_item_names_and_currency_symbol():
    from services import site_generator as sg
    pal = {"text": "#fff", "surface": "#000", "border": "#111", "muted": "#999", "bg": "#000", "surface2": "#000"}
    out = sg._buy_now_html(7, "$'</script><script>x()", "USD", pal, "#0f0")
    assert "_wzEsc(i.name)" in out and "</script><script>" not in out     # "<\/script>" cannot close the tag
    assert out.count("</script>") == 1          # only the generator's own closing tag


# ── 6. Error-message hygiene ────────────────────────────────────────────────

def test_5xx_details_are_not_leaked_but_4xx_and_503_are_preserved(app_client):
    app, c = app_client

    @app.get("/__audit_test/500")
    def _e500(): raise HTTPException(500, 'relation "businesses" does not exist: SELECT secret')

    @app.get("/__audit_test/422")
    def _e422(): raise HTTPException(422, "Price must be positive")

    @app.get("/__audit_test/503")
    def _e503(): raise HTTPException(503, "Cron endpoint not configured")

    @app.get("/__audit_test/401")
    def _e401(): raise HTTPException(401, "nope", headers={"WWW-Authenticate": "Bearer"})

    r = c.get("/__audit_test/500")
    assert r.status_code == 500 and "relation" not in r.text and "secret" not in r.text.lower()
    assert c.get("/__audit_test/422").json()["detail"] == "Price must be positive"
    assert c.get("/__audit_test/503").json()["detail"] == "Cron endpoint not configured"
    r401 = c.get("/__audit_test/401")
    assert r401.status_code == 401 and r401.headers.get("www-authenticate") == "Bearer"


# ── 7. Scheduled campaigns ──────────────────────────────────────────────────

def test_scheduled_run_skips_campaign_already_claimed(monkeypatch):
    import routes.growth_routes as gr
    import services.campaign_service as cs
    calls = {"run": 0}

    class _CS:
        @staticmethod
        def run(**kw): calls["run"] += 1; return {"sent": 1, "failed": 0}
    monkeypatch.setattr(cs, "CampaignService", _CS)

    class _Q:
        def __init__(self, parent, table): self.p, self.t, self.op = parent, table, None
        def select(self, *a): self.op = "select"; return self
        def update(self, d): self.op = "update"; self.d = d; return self
        def eq(self, *a): return self
        def lte(self, *a): return self
        def execute(self):
            if self.op == "select":
                return types.SimpleNamespace(data=[{"id": 1, "business_id": 3, "audience": "all", "message": "hi"}])
            if self.d.get("status") == "processing":
                return types.SimpleNamespace(data=[])      # someone else already claimed it
            return types.SimpleNamespace(data=[{"id": 1}])

    class _DB:
        def table(self, name): return _Q(self, name)
    monkeypatch.setattr(sys.modules["core.db"], "supabase", _DB(), raising=False)
    out = _run(gr.run_scheduled_campaigns())
    assert calls["run"] == 0 and out["processed"] == 0


def test_scheduled_run_sends_when_claim_succeeds(monkeypatch):
    import routes.growth_routes as gr
    import services.campaign_service as cs
    calls = {"run": 0}

    class _CS:
        @staticmethod
        def run(**kw): calls["run"] += 1; return {"sent": 2, "failed": 0}
    monkeypatch.setattr(cs, "CampaignService", _CS)

    class _Q:
        def select(self, *a): self.op = "select"; return self
        def update(self, d): self.op = "update"; return self
        def eq(self, *a): return self
        def lte(self, *a): return self
        def execute(self):
            if self.op == "select":
                return types.SimpleNamespace(data=[{"id": 1, "business_id": 3, "audience": "all", "message": "hi"}])
            return types.SimpleNamespace(data=[{"id": 1}])

    class _DB:
        def table(self, name): return _Q()
    monkeypatch.setattr(sys.modules["core.db"], "supabase", _DB(), raising=False)
    out = _run(gr.run_scheduled_campaigns())
    assert calls["run"] == 1 and out["processed"] == 1 and out["sent"] == 2
