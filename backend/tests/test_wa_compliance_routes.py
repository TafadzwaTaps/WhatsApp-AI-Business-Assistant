"""Owner API for WhatsApp consent and the template registry (mock database)."""
import pytest
from fastapi.testclient import TestClient

import core.auth as auth
import services.wa_consent as wc
import services.wa_templates as wt
import routes.wa_compliance_routes as wr

from test_wa_consent_templates import _DB          # noqa: F401  (stateful fake Supabase incl. the consent view)


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


@pytest.fixture
def db(monkeypatch):
    import core.db as coredb
    d = _DB()
    monkeypatch.setattr(coredb, "supabase", d)
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    monkeypatch.setattr(wr, "rate_limit", lambda *a, **k: None)
    wc._reset_for_tests(); wt.clear_cache()
    return d


def _h(bid=1, **extra):
    return {"Authorization": "Bearer " + auth.create_access_token({"sub": "owner", "role": "business", "business_id": bid, **extra})}


P1, P2 = "263771000111", "263771000222"


def test_consent_overview(client, db):
    wc.record(1, P1, "opted_in", "dashboard", "x"); wc.record(1, P2, "opted_out", "customer_message"); wc.record(2, P1, "opted_in", "dashboard", "x")
    r = client.get("/me/whatsapp/consent", headers=_h())
    assert r.status_code == 200 and r.json() == {"available": True, "enforced": True, "opted_in": 1, "opted_out": 1}


def test_overview_when_the_migration_is_missing(client, db):
    db.missing.add("whatsapp_consent_events")
    assert client.get("/me/whatsapp/consent", headers=_h()).json() == {"available": False, "enforced": False, "opted_in": 0, "opted_out": 0}


def test_recording_consent_requires_a_description_of_how_it_was_collected(client, db):
    r = client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1], "status": "opted_in", "source": "website_form"})
    assert r.status_code == 422 and db.tables.get("whatsapp_consent_events", []) == []
    r = client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1, "+263 771 000 222"], "status": "opted_in",
                                                                 "source": "website_form", "note": "ticked the box on our order form"})
    assert r.status_code == 200 and r.json() == {"ok": True, "recorded": 2, "rejected": 0}
    rows = db.tables["whatsapp_consent_events"]
    assert {x["phone"] for x in rows} == {P1, P2} and all(x["business_id"] == 1 and x["recorded_by"] == "owner" and x["source"] == "website_form" for x in rows)


def test_an_owner_can_record_an_opt_out_without_a_note(client, db):
    r = client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1], "status": "opted_out", "source": "phone_call"})
    assert r.status_code == 200 and wc.statuses(1, [P1]) == {P1: "opted_out"}


def test_the_customer_message_source_cannot_be_forged_from_the_dashboard(client, db):
    r = client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1], "status": "opted_in", "source": "customer_message", "note": "x"})
    assert r.status_code == 422 and db.tables.get("whatsapp_consent_events", []) == []


def test_bad_phone_numbers_are_counted_as_rejected_not_recorded(client, db):
    r = client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1, "abc", "12"], "status": "opted_in", "source": "import", "note": "csv"})
    assert r.json() == {"ok": True, "recorded": 1, "rejected": 2}


def test_input_limits(client, db):
    assert client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [], "status": "opted_in", "source": "import", "note": "x"}).status_code == 422
    assert client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1] * 501, "status": "opted_in", "source": "import", "note": "x"}).status_code == 422
    assert client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1], "status": "hello", "source": "import", "note": "x"}).status_code == 422


def test_consent_is_recorded_for_the_callers_business_only(client, db):
    client.post("/me/whatsapp/consent", headers=_h(bid=2), json={"phones": [P1], "status": "opted_in", "source": "qr", "note": "scanned"})
    assert wc.statuses(2, [P1]) == {P1: "opted_in"} and wc.statuses(1, [P1]) == {}


def test_recording_without_the_migration_is_a_clear_503(client, db):
    db.missing.add("whatsapp_consent_events")
    r = client.post("/me/whatsapp/consent", headers=_h(), json={"phones": [P1], "status": "opted_out", "source": "import"})
    assert r.status_code == 503


def test_support_sessions_cannot_record_consent_or_register_templates(client, db):
    h = _h(impersonated_by="root")
    assert client.post("/me/whatsapp/consent", headers=h, json={"phones": [P1], "status": "opted_in", "source": "import", "note": "x"}).status_code == 403
    assert client.post("/me/whatsapp/templates", headers=h, json={"purpose": "campaign", "name": "promo"}).status_code == 403
    assert client.get("/me/whatsapp/consent", headers=h).status_code == 200            # looking is allowed


def test_unauthenticated_requests_are_refused(client, db):
    assert client.get("/me/whatsapp/consent").status_code == 401
    assert client.post("/me/whatsapp/templates", json={}).status_code == 401


# ── templates ──────────────────────────────────────────────────────────────────────────────────────
def test_register_list_and_approve_a_template(client, db):
    r = client.post("/me/whatsapp/templates", headers=_h(), json={"purpose": "booking_reminder", "name": "booking_v1", "language": "en_US",
                                                                  "param_spec": ["customer_name", "date"]})
    assert r.status_code == 200 and r.json()["template"]["status"] == "pending" and r.json()["template"]["category"] == "utility"
    tid = r.json()["template"]["id"]
    assert wt.get_template(1, "booking_reminder") is None                              # pending templates are never used
    assert client.post(f"/me/whatsapp/templates/{tid}/status", headers=_h(), json={"status": "approved"}).status_code == 200
    assert wt.get_template(1, "booking_reminder")["name"] == "booking_v1"
    listing = client.get("/me/whatsapp/templates", headers=_h()).json()
    assert [t["name"] for t in listing["templates"]] == ["booking_v1"] and listing["purposes"]["campaign"] == "marketing"


@pytest.mark.parametrize("body", [{"purpose": "nope", "name": "x_1"}, {"purpose": "campaign", "name": "Has Spaces"},
                                  {"purpose": "campaign", "name": "ok_1", "language": "english"},
                                  {"purpose": "campaign", "name": "ok_1", "param_spec": ["password"]}])
def test_invalid_template_registration_is_a_422(client, db, body):
    assert client.post("/me/whatsapp/templates", headers=_h(), json=body).status_code == 422


def test_another_business_cannot_approve_my_template(client, db):
    tid = client.post("/me/whatsapp/templates", headers=_h(bid=1), json={"purpose": "campaign", "name": "promo_1"}).json()["template"]["id"]
    assert client.post(f"/me/whatsapp/templates/{tid}/status", headers=_h(bid=2), json={"status": "approved"}).status_code == 404
    assert wt.get_template(1, "campaign") is None
    assert client.get("/me/whatsapp/templates", headers=_h(bid=2)).json()["templates"] == []


def test_bad_status_values_are_rejected(client, db):
    tid = client.post("/me/whatsapp/templates", headers=_h(), json={"purpose": "campaign", "name": "promo_1"}).json()["template"]["id"]
    assert client.post(f"/me/whatsapp/templates/{tid}/status", headers=_h(), json={"status": "super-approved"}).status_code == 422


def test_template_registry_missing_is_a_clear_503(client, db):
    db.missing.add("whatsapp_templates")
    assert client.post("/me/whatsapp/templates", headers=_h(), json={"purpose": "campaign", "name": "promo_1"}).status_code == 503
    assert client.get("/me/whatsapp/templates", headers=_h()).json()["templates"] == []
