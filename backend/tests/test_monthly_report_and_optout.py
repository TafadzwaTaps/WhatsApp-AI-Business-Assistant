"""Monthly dashboard report: cadence, opt-out enforcement, unsubscribe link."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from services import weekly_report_service as wr


def _dt(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


# ── cadence ────────────────────────────────────────────────────────────────
def test_next_run_is_first_of_next_month_after_send():
    assert wr._next_monthly_run(_dt(2026, 10, 1, 9)) == _dt(2026, 11, 1, 8)

def test_next_run_later_this_month_when_before_8am_on_the_1st():
    assert wr._next_monthly_run(_dt(2026, 10, 1, 7)) == _dt(2026, 10, 1, 8)

def test_next_run_mid_month_and_year_rollover():
    assert wr._next_monthly_run(_dt(2026, 10, 7, 13)) == _dt(2026, 11, 1, 8)
    assert wr._next_monthly_run(_dt(2026, 12, 15)) == _dt(2027, 1, 1, 8)

def test_month_window_is_previous_calendar_month():
    start, end, label = wr._month_window(_dt(2026, 1, 1, 8))
    assert (start, end, label) == (_dt(2025, 12, 1), _dt(2026, 1, 1), "December 2025")


# ── sending / opt-out ──────────────────────────────────────────────────────
class _Rec:
    """Minimal supabase stand-in that records businesses updates."""
    def __init__(self, rows):
        self.rows, self.updates, self._t = rows, [], None
    def table(self, n): self._t = n; return self
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def ilike(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def gt(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def update(self, d): self.updates.append(d); return self
    def execute(self):
        class R: pass
        r = R(); r.data = self.rows if self._t == "businesses" else []
        return r


@pytest.fixture
def run(monkeypatch):
    def _run(rows):
        fake = _Rec(rows)
        monkeypatch.setattr(sys.modules["core.db"], "supabase", fake, raising=False)
        sent = []
        import services.email_service as es
        monkeypatch.setattr(es, "_send", lambda to, subject, html: sent.append((to, subject, html)) or True)
        return wr.send_weekly_reports(), sent, fake
    return _run


def test_opted_out_business_gets_nothing(run):
    res, sent, _ = run([{"id": 1, "name": "A", "owner_email": "a@x.com",
                         "features_json": {"pref_weekly_reports": False}}])
    assert sent == [] and res["skipped"] == 1 and res["sent"] == 0

def test_default_opted_in_business_gets_monthly_email_with_unsubscribe(run):
    res, sent, fake = run([{"id": 2, "name": "B", "owner_email": "b@x.com", "features_json": {}}])
    assert res["sent"] == 1
    to, subject, html = sent[0]
    assert "Monthly Report" in subject and "Weekly" not in subject
    # Every outgoing email (the monthly report included) carries the address-scoped
    # unsubscribe link; it is what a recipient who wants no more email clicks.
    assert "/email/unsubscribe-address?email=b%40x.com&token=" in html
    assert fake.updates and "last_monthly_report" in fake.updates[0]["features_json"]

def test_not_sent_twice_for_same_month(run):
    key = wr._month_window()[0].strftime("%Y-%m")
    res, sent, _ = run([{"id": 3, "name": "C", "owner_email": "c@x.com",
                         "features_json": {"last_monthly_report": key}}])
    assert sent == [] and res["skipped"] == 1

def test_null_features_json_and_missing_email_are_safe(run):
    res, sent, _ = run([{"id": 4, "name": "D", "owner_email": "d@x.com", "features_json": None},
                        {"id": 5, "name": "E", "owner_email": "", "features_json": {}}])
    assert res["sent"] == 1 and res["skipped"] == 1

def test_aliases_exist_for_old_names():
    assert wr.send_monthly_reports is wr.send_weekly_reports
    assert wr.attach_monthly_report_scheduler is wr.attach_weekly_report_scheduler


# ── unsubscribe endpoint ───────────────────────────────────────────────────
@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def test_address_unsubscribe_opts_out_every_business_of_that_address(client, monkeypatch):
    from services.email_service import _address_token
    fake = _Rec([{"id": 9, "owner_email": "Owner@X.com", "features_json": {"keep": 1}},
                 {"id": 10, "owner_email": "someone-else@x.com", "features_json": {}}])
    monkeypatch.setattr(sys.modules["core.db"], "supabase", fake, raising=False)
    tok = _address_token("owner@x.com")
    for method in (client.get, client.post):          # link click and RFC 8058 one-click
        fake.updates.clear()
        r = method(f"/email/unsubscribe-address?email=owner%40x.com&token={tok}")
        assert r.status_code == 200 and "unsubscribed" in r.text.lower()
        assert len(fake.updates) == 1                  # only the matching owner, not the other address
        fj = fake.updates[0]["features_json"]
        assert fj["keep"] == 1 and fj["pref_weekly_reports"] is False and fj["marketing_emails_opt_out"] is True


def test_address_unsubscribe_rejects_bad_or_wrong_scope_token(client, monkeypatch):
    from services.email_service import _unsubscribe_token, _address_token   # business-scoped token must not work
    fake = _Rec([{"id": 9, "owner_email": "owner@x.com", "features_json": {}}])
    monkeypatch.setattr(sys.modules["core.db"], "supabase", fake, raising=False)
    assert client.post("/email/unsubscribe-address?email=owner%40x.com&token=bad").status_code == 400
    assert client.post(f"/email/unsubscribe-address?email=owner%40x.com&token={_unsubscribe_token(9)}").status_code == 400
    # a valid token for a DIFFERENT address must not unsubscribe this one
    assert client.post(f"/email/unsubscribe-address?email=owner%40x.com&token={_address_token('other@x.com')}").status_code == 400
    assert fake.updates == []


def test_manual_trigger_requires_superadmin(client):
    import routes.growth_routes as gr
    client.app.dependency_overrides[gr.require_business] = lambda: {"business_id": 1, "username": "u", "role": "business"}
    try:
        assert client.post("/growth/send-weekly-reports").status_code == 403
    finally:
        client.app.dependency_overrides.clear()
