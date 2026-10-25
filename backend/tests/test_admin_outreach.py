"""Tests for routes/admin_outreach_routes.py (never-activated list, restart trial, email)."""
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main
from core.auth import require_superadmin
from routes import admin_outreach_routes as r


class Rec:
    """Records updates; returns canned rows per table."""
    def __init__(self, tables):
        self.tables, self.updates, self._t, self._upd = tables, [], None, None

    def table(self, name):
        self._t, self._upd = name, None
        return self

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def update(self, payload):
        self._upd = payload
        return self

    def execute(self):
        if self._upd is not None:
            self.updates.append((self._t, self._upd))
            return type("R", (), {"data": []})()
        return type("R", (), {"data": self.tables.get(self._t, [])})()


@pytest.fixture
def client(monkeypatch):
    main.app.dependency_overrides[require_superadmin] = lambda: {"username": "root", "role": "superadmin"}
    audits = []
    monkeypatch.setattr(r, "log_admin_action", lambda **kw: audits.append(kw))
    yield TestClient(main.app), audits
    main.app.dependency_overrides.pop(require_superadmin, None)


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_lists_only_businesses_without_products_or_orders(client, monkeypatch):
    c, _ = client
    fake = Rec({
        "businesses": [
            {"id": 1, "name": "Idle", "owner_email": "a@x.com", "contact_phone": "+263 77 123 4567",
             "created_at": _iso(10), "billing_status": "trialing", "trial_ends_at": _iso(-20)},
            {"id": 2, "name": "HasProducts", "created_at": _iso(5)},
            {"id": 3, "name": "HasOrders", "created_at": _iso(5)},
        ],
    })
    # products/orders tables return rows for 2 and 3
    orig = fake.execute
    def execute():
        if fake._upd is None and fake._t == "products":
            return type("R", (), {"data": [{"business_id": 2}]})()
        if fake._upd is None and fake._t == "orders":
            return type("R", (), {"data": [{"business_id": 3}]})()
        return orig()
    fake.execute = execute
    monkeypatch.setattr(r, "_db", lambda: fake)

    res = c.get("/admin/outreach/never-activated")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["id"] == 1 and item["days_since_signup"] == 10
    assert item["whatsapp_link"].startswith("https://wa.me/263771234567?text=")
    assert item["email_link"].startswith("mailto:a@x.com")
    assert item["trial_state"] == "trialing"


def test_restart_trial_requires_reason(client, monkeypatch):
    c, audits = client
    fake = Rec({"businesses": [{"id": 1, "billing_status": "expired"}]})
    monkeypatch.setattr(r, "_db", lambda: fake)
    res = c.post("/admin/outreach/1/restart-trial", json={"reason": "  ", "days": 30})
    assert res.status_code == 422
    assert fake.updates == [] and audits == []


def test_restart_trial_updates_and_audits(client, monkeypatch):
    c, audits = client
    fake = Rec({"businesses": [{"id": 1, "billing_status": "expired", "features_json": {"x": 1},
                                "trial_ends_at": _iso(3)}]})
    monkeypatch.setattr(r, "_db", lambda: fake)
    res = c.post("/admin/outreach/1/restart-trial", json={"reason": "Never got to set up", "days": 14})
    assert res.status_code == 200
    table, payload = fake.updates[0]
    assert table == "businesses" and payload["billing_status"] == "trialing"
    assert payload["payment_failed_at"] is None
    assert payload["features_json"]["x"] == 1 and payload["features_json"]["trial_restarts"] == 1
    ends = datetime.fromisoformat(payload["trial_ends_at"])
    assert 13 <= (ends - datetime.now(timezone.utc)).days <= 14
    assert audits[0]["action"] == "business.trial_restart"
    assert audits[0]["reason"] == "Never got to set up" and audits[0]["business_id"] == 1


def test_restart_trial_refused_for_paid(client, monkeypatch):
    c, audits = client
    monkeypatch.setattr(r, "_db", lambda: Rec({"businesses": [{"id": 1, "billing_status": "active"}]}))
    res = c.post("/admin/outreach/1/restart-trial", json={"reason": "please restart", "days": 30})
    assert res.status_code == 409 and audits == []


def test_restart_trial_rejects_bad_days(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(r, "_db", lambda: Rec({"businesses": [{"id": 1}]}))
    assert c.post("/admin/outreach/1/restart-trial", json={"reason": "valid reason", "days": 999}).status_code == 422


def test_send_email_escapes_html_and_audits(client, monkeypatch):
    c, audits = client
    sent = {}
    import services.email_service as es
    monkeypatch.setattr(es, "_send", lambda to, subj, html: sent.update(to=to, subj=subj, html=html) or True)
    fake = Rec({"businesses": [{"id": 1, "owner_email": "o@x.com", "features_json": {}}]})
    monkeypatch.setattr(r, "_db", lambda: fake)
    res = c.post("/admin/outreach/1/send-email", json={"subject": "Hello", "message": "Need help? <script>x</script>"})
    assert res.status_code == 200
    assert sent["to"] == "o@x.com" and "<script>" not in sent["html"] and "&lt;script&gt;" in sent["html"]
    assert audits[0]["action"] == "business.outreach_email_sent"
    assert fake.updates[0][1]["features_json"]["last_outreach"]["channel"] == "email"


def test_send_email_failure_is_reported_and_audited(client, monkeypatch):
    c, audits = client
    import services.email_service as es
    monkeypatch.setattr(es, "_send", lambda *a, **k: False)
    monkeypatch.setattr(r, "_db", lambda: Rec({"businesses": [{"id": 1, "owner_email": "o@x.com"}]}))
    res = c.post("/admin/outreach/1/send-email", json={"subject": "Hi", "message": "hello there"})
    assert res.status_code == 502
    assert audits[0]["action"] == "business.outreach_email_failed"


def test_send_email_needs_owner_email_and_fields(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(r, "_db", lambda: Rec({"businesses": [{"id": 1, "owner_email": ""}]}))
    assert c.post("/admin/outreach/1/send-email", json={"subject": "Hi", "message": "hello there"}).status_code == 422
    assert c.post("/admin/outreach/1/send-email", json={"subject": "", "message": "hello there"}).status_code == 422


def test_endpoints_require_superadmin():
    main.app.dependency_overrides.pop(require_superadmin, None)
    c = TestClient(main.app)
    assert c.get("/admin/outreach/never-activated").status_code in (401, 403)
    assert c.post("/admin/outreach/1/restart-trial", json={"reason": "abcdef"}).status_code in (401, 403)
    assert c.post("/admin/outreach/1/send-email", json={"subject": "a", "message": "abcdef"}).status_code in (401, 403)


def test_wa_link_helpers():
    assert r.wa_link("") == "" and r.wa_link("12") == ""
    assert r.wa_link("+263 77-123 4567", "hi there") == "https://wa.me/263771234567?text=hi%20there"
    assert r.mailto_link("bad") == ""
