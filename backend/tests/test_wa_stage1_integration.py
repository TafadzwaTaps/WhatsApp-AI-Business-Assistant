"""Stage 1 integration: consent + 24-hour window + approved templates across every business-initiated path.
Everything is mocked: no WhatsApp call, no live template, no real database."""
from datetime import datetime, timedelta, timezone

import pytest

import services.wa_consent as wc
import services.wa_templates as wt
import services.wa_window as ww

from test_wa_consent_templates import _DB, Ledger     # noqa: F401  (stateful fake Supabase incl. the consent view)

NOW = datetime.now(timezone.utc)
OPEN, NOCONS, OUT, CLOSED, CLOSED2 = "263771000001", "263771000002", "263771000003", "263771000004", "263771000005"


def _ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


@pytest.fixture
def env(monkeypatch):
    """Business 1 with customers: OPEN (opted in, wrote 2h ago), NOCONS (never opted in), OUT (opted out),
    CLOSED / CLOSED2 (opted in, last wrote 40h ago)."""
    import core.db as coredb
    import crud.notification_ledger as nl
    d = _DB()
    monkeypatch.setattr(coredb, "supabase", d)
    wc._reset_for_tests(); wt.clear_cache()
    monkeypatch.delenv("WA_MARKETING_CONSENT_REQUIRED", raising=False)
    led = Ledger()
    monkeypatch.setattr(nl, "claim", led.claim)
    monkeypatch.setattr(nl, "release", led.release)
    phones = {OPEN: 2, NOCONS: 2, OUT: 2, CLOSED: 40, CLOSED2: 40}
    d.tables["customers"] = [{"id": i, "business_id": 1, "phone": p} for i, p in enumerate(phones, 1)]
    d.tables["messages"] = [{"customer_id": i, "business_id": 1, "direction": "incoming", "created_at": _ago(h)}
                            for i, h in enumerate(phones.values(), 1)]
    for p in (OPEN, CLOSED, CLOSED2):
        wc.record(1, p, "opted_in", "dashboard", "test")
    wc.record(1, OUT, "opted_in", "dashboard", "test")
    wc.record(1, OUT, "opted_out", "customer_message", "stop", "customer")
    sent_templates = []

    def fake_send(business, payload):
        sent_templates.append(payload)
        return {"ok": True, "id": "wamid.T"}
    monkeypatch.setattr(wt, "_default_send", fake_send)
    return d, sent_templates


def _approve(purpose, spec=()):
    row = wt.register(1, purpose, purpose + "_v1", "en", list(spec))
    wt.set_status(1, row["id"], "approved")
    return row


# ── campaigns ──────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def campaign(monkeypatch, env):
    import crud
    import main
    import services.campaign_service as cs
    texts = []
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop", "whatsapp_phone_id": "PID", "currency_symbol": "$"})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    monkeypatch.setattr(cs, "resolve_audience", lambda b, a, p=None: [{"phone": x, "name": "Ann Lee"} for x in (OPEN, NOCONS, OUT, CLOSED, CLOSED2)])
    monkeypatch.setattr(cs, "_on_cooldown", lambda b, p: False)
    monkeypatch.setattr(cs, "_mark_sent", lambda b, p: None)
    monkeypatch.setattr(main, "send_whatsapp", lambda pid, tok, to, msg: texts.append(to) or {"messages": [{"id": "w"}]})
    return cs, texts, env[1]


def test_campaign_sends_free_text_only_to_consenting_customers_inside_the_window(campaign):
    cs, texts, templates = campaign
    out = cs.CampaignService.run(1, "all", "Big sale!", dry_run=False)
    assert texts == [OPEN] and templates == []
    assert out["sent"] == 1 and out["failed"] == 0
    assert out["skipped_no_consent"] == 1 and out["skipped_opted_out"] == 1 and out["skipped_window"] == 2
    assert "template" in out["window_note"] and "opted in" in out["consent_note"]


def test_nobody_without_consent_ever_receives_a_marketing_template(campaign):
    cs, texts, templates = campaign
    _approve("campaign", ["customer_name", "business_name"])
    cs.CampaignService.run(1, "all", "Big sale!", dry_run=False)
    assert {t["to"] for t in templates} == {CLOSED, CLOSED2}
    assert NOCONS not in {t["to"] for t in templates} and OUT not in {t["to"] for t in templates}
    assert texts == [OPEN]


def test_customers_outside_the_window_get_the_approved_marketing_template(campaign):
    cs, texts, templates = campaign
    _approve("campaign", ["customer_name", "business_name"])
    out = cs.CampaignService.run(1, "all", "Big sale!", dry_run=False)
    assert out["sent"] == 3 and out["sent_template"] == 2 and out["skipped_window"] == 0 and out["failed"] == 0
    p = templates[0]
    assert p["type"] == "template" and p["template"]["name"] == "campaign_v1"
    assert [x["text"] for x in p["template"]["components"][0]["parameters"]] == ["Ann", "Shop"]    # first name only
    assert "Big sale" not in repr(templates)                                                      # free text never leaks into a template


def test_a_campaign_is_never_duplicated_by_a_second_run(campaign):
    cs, texts, templates = campaign
    _approve("campaign")
    cs.CampaignService.run(1, "all", "Big sale!", dry_run=False)
    first = len(templates)
    out = cs.CampaignService.run(1, "all", "Big sale!", dry_run=False)
    assert len(templates) == first                                                                # same text, same day: no repeat
    assert out["sent_template"] == 0


def test_the_per_run_template_cap_is_respected(campaign, monkeypatch):
    cs, texts, templates = campaign
    _approve("campaign")
    monkeypatch.setenv("WA_TEMPLATE_MAX_PER_RUN", "1")
    out = cs.CampaignService.run(1, "all", "Big sale!", dry_run=False)
    assert len(templates) == 1 and out["skipped_cap"] == 1 and out["sent_template"] == 1


def test_an_opt_out_stops_the_next_campaign_immediately(campaign):
    cs, texts, templates = campaign
    wc.handle_inbound(1, OPEN, "STOP", "Shop")
    out = cs.CampaignService.run(1, "all", "Another sale", dry_run=False)
    assert OPEN not in texts and out["skipped_opted_out"] == 2


def test_opting_out_of_another_business_changes_nothing_here(campaign):
    cs, texts, templates = campaign
    wc.record(2, OPEN, "opted_out", "customer_message")
    cs.CampaignService.run(1, "all", "Sale", dry_run=False)
    assert texts == [OPEN]


def test_a_meta_template_error_is_a_failure_with_a_safe_reason(campaign, monkeypatch):
    cs, texts, templates = campaign
    _approve("campaign")
    monkeypatch.setattr(wt, "_default_send", lambda b, p: {"ok": False, "error": {"code": 132001, "message": "Template name does not exist"}})
    out = cs.CampaignService.run(1, "all", "Sale", dry_run=False)
    assert out["failed"] == 2 and out["sent"] == 1 and CLOSED in out["failed_numbers"]


def test_a_dry_run_sends_nothing_and_consults_nothing(campaign):
    cs, texts, templates = campaign
    out = cs.CampaignService.run(1, "all", "Sale", dry_run=True)
    assert out["dry_run"] is True and texts == [] and templates == []


def test_without_the_migration_campaigns_behave_as_before_the_consent_feature(campaign, env):
    cs, texts, templates = campaign
    env[0].missing.add("whatsapp_consent_events")
    wc._reset_for_tests()
    out = cs.CampaignService.run(1, "all", "Sale", dry_run=False)
    assert set(texts) == {OPEN, NOCONS, OUT}                       # no consent gating: only the 24-hour window applies
    assert out["skipped_window"] == 2 and out["skipped_no_consent"] == 0


# ── broadcast ──────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def bcast(monkeypatch, env):
    import crud
    import routes.business_routes as br
    texts = []
    monkeypatch.setattr(br, "_rate_check", lambda *a, **k: None)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop", "whatsapp_phone_id": "PID"})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    monkeypatch.setattr(crud, "get_all_customer_phones", lambda b: [OPEN, NOCONS, OUT, CLOSED, CLOSED2])
    monkeypatch.setattr(crud, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(br, "send_whatsapp", lambda pid, tok, to, msg: texts.append(to) or {"messages": [1]})
    return br, texts, env[1]


def _bc(br):
    return br.broadcast(br.BroadcastRequest(message="hello there everyone"), request=None, user={"business_id": 1})


def test_broadcast_applies_consent_then_window(bcast):
    br, texts, templates = bcast
    out = _bc(br)
    assert texts == [OPEN] and templates == []
    assert out["sent"] == 1 and out["skipped_no_consent"] == 2 and out["skipped_window"] == 2 and out["total"] == 5
    assert sorted(out["skipped_no_consent_numbers"]) == sorted([NOCONS, OUT]) and "opted in" in out["consent_note"]


def test_broadcast_uses_the_approved_template_outside_the_window(bcast):
    br, texts, templates = bcast
    _approve("campaign")
    out = _bc(br)
    assert texts == [OPEN] and {t["to"] for t in templates} == {CLOSED, CLOSED2}
    assert out["sent"] == 3 and out["sent_template"] == 2 and "skipped_window" not in out


def test_broadcast_to_nobody_eligible_sends_nothing_and_says_why(bcast, env):
    br, texts, templates = bcast
    d = env[0]
    d.tables["whatsapp_consent_events"].clear()
    out = _bc(br)
    assert texts == [] and templates == [] and out["sent"] == 0 and out["failed"] == 0 and out["skipped_no_consent"] == 5


# ── cart recovery and re-engagement (marketing) ────────────────────────────────────────────────────
def _carts(env):
    d = env[0]
    items = [{"name": "Burger", "qty": 1, "price": 5}]
    d.tables["carts"] = [{"business_id": 1, "phone": p, "items": items, "state_data": {"state": "browsing"}, "updated_at": _ago(3)}
                         for p in (OPEN, NOCONS, OUT, CLOSED)]
    d.tables["businesses"] = [{"id": 1, "name": "Shop", "currency_symbol": "$", "whatsapp_phone_id": "PID", "features_json": {}}]


def test_cart_recovery_honours_consent_window_and_templates(env, monkeypatch):
    import growth.cart_recovery as cr
    _carts(env)
    free = []
    monkeypatch.setattr(cr, "_is_recovery_enabled", lambda b: True)
    monkeypatch.setattr(cr, "_send_recovery_message", lambda b, biz, phone, msg: free.append(phone) or True)
    _approve("cart_recovery", ["business_name"])
    out = cr.run_cart_recovery(1)
    assert free == [OPEN]                                           # in the window and opted in: free text as before
    assert out["sent"] == 2 and out["skipped_no_consent"] == 2     # CLOSED went by template; NOCONS and OUT were skipped
    templates = env[1]
    assert [t["to"] for t in templates] == [CLOSED] and templates[0]["template"]["name"] == "cart_recovery_v1"


def test_cart_recovery_without_a_template_skips_the_closed_window_customer(env, monkeypatch):
    import growth.cart_recovery as cr
    _carts(env)
    free = []
    monkeypatch.setattr(cr, "_is_recovery_enabled", lambda b: True)
    monkeypatch.setattr(cr, "_send_recovery_message", lambda b, biz, phone, msg: free.append(phone) or True)
    out = cr.run_cart_recovery(1)
    assert free == [OPEN] and out["sent"] == 1 and out["skipped_window"] == 1 and env[1] == []


def _memory(env):
    d = env[0]
    old = _ago(24 * 20)
    d.tables["user_memory"] = [{"business_id": 1, "phone": p, "customer_name": "Ann Lee", "order_count": 2, "last_seen": old, "last_product": "Burger"}
                               for p in (CLOSED, NOCONS, OUT)]
    d.tables["carts"] = []
    d.tables["businesses"] = [{"id": 1, "name": "Shop", "currency_symbol": "$", "whatsapp_phone_id": "PID", "features_json": {}}]


def test_reengagement_only_reaches_consenting_customers_through_a_template(env, monkeypatch):
    import growth.reengagement as rg
    _memory(env)
    free = []
    monkeypatch.setattr(rg, "_is_reengagement_enabled", lambda b: True)
    monkeypatch.setattr(rg, "_send_message", lambda biz, phone, msg: free.append(phone) or True)
    _approve("reengagement", ["customer_name", "business_name"])
    out = rg.run_reengagement(1)
    assert free == [] and out["sent"] == 1 and out["skipped_no_consent"] == 2
    assert [t["to"] for t in env[1]] == [CLOSED]
    assert [x["text"] for x in env[1][0]["template"]["components"][0]["parameters"]] == ["Ann", "Shop"]


def test_reengagement_without_a_template_sends_nothing(env, monkeypatch):
    import growth.reengagement as rg
    _memory(env)
    free = []
    monkeypatch.setattr(rg, "_is_reengagement_enabled", lambda b: True)
    monkeypatch.setattr(rg, "_send_message", lambda biz, phone, msg: free.append(phone) or True)
    out = rg.run_reengagement(1)
    assert free == [] and out["sent"] == 0 and env[1] == []


# ── utility templates (no marketing consent needed) ────────────────────────────────────────────────
def test_payment_reminder_uses_the_utility_template_even_without_marketing_consent(env, monkeypatch):
    import workflows.payment_reminder as pr
    import main
    free = []
    monkeypatch.setattr(pr, "_is_on_cooldown", lambda oid: False)
    monkeypatch.setattr(pr, "_mark_reminded", lambda oid: None)
    monkeypatch.setattr(pr, "_resolve_wa_credentials", lambda b: ("PID", "TOKEN"))
    monkeypatch.setattr(pr, "build_reminder_message", lambda *a, **k: "please pay")
    monkeypatch.setattr(main, "send_whatsapp", lambda *a: free.append(a) or {"messages": [1]})
    _approve("payment_reminder", ["business_name", "order_id", "amount"])
    order = {"id": 9, "customer_phone": CLOSED, "total_price": 12.5, "currency_symbol": "$"}
    wc.record(1, CLOSED, "opted_out", "customer_message")             # opted out of MARKETING: reminders are transactional
    out = pr.send_reminder(order, {"id": 1, "name": "Shop"}, 1)
    assert out["ok"] is True and out.get("sent_as_template") is True and free == []
    assert [x["text"] for x in env[1][0]["template"]["components"][0]["parameters"]] == ["Shop", "ORDER-9", "$12.50"]
    again = pr.send_reminder(order, {"id": 1, "name": "Shop"}, 1)      # same order and tier: never duplicated
    assert again["ok"] is False and len(env[1]) == 1


def test_payment_reminder_without_a_template_is_skipped_outside_the_window(env, monkeypatch):
    import workflows.payment_reminder as pr
    import main
    free = []
    monkeypatch.setattr(pr, "_is_on_cooldown", lambda oid: False)
    monkeypatch.setattr(pr, "_resolve_wa_credentials", lambda b: ("PID", "TOKEN"))
    monkeypatch.setattr(pr, "build_reminder_message", lambda *a, **k: "please pay")
    monkeypatch.setattr(main, "send_whatsapp", lambda *a: free.append(a) or {"messages": [1]})
    out = pr.send_reminder({"id": 9, "customer_phone": CLOSED}, {"id": 1, "name": "Shop"}, 1)
    assert out["ok"] is False and out["skipped_window"] is True and free == [] and env[1] == []


def test_booking_reminder_uses_the_utility_template_outside_the_window(env, monkeypatch):
    import services.booking_service as bs
    import routes.webhook_routes as wh
    import crud
    d = env[0]
    d.tables["bookings"] = [{"id": 5, "reminder_24h_sent": False}]
    free = []
    monkeypatch.setattr(bs, "get_upcoming_reminders", lambda bid, window_hours=24.5: [
        {"id": 5, "customer_phone": CLOSED, "customer_name": "Ann Lee", "service_name": "Haircut",
         "booking_date": "2026-12-01", "start_time": "10:00", "reminder_24h_sent": False}])
    monkeypatch.setattr(wh, "send_whatsapp", lambda *a: free.append(a) or {"messages": [1]})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    monkeypatch.setattr("services._ai_state._set_awaiting_reminder_response", lambda *a, **k: None)
    _approve("booking_reminder", ["customer_name", "service", "date", "time", "business_name"])
    out = bs.send_reminders_for_business(1, {"id": 1, "name": "Shop", "whatsapp_phone_id": "PID"})
    assert out["ok"] is True and out["sent"] == 1 and free == []
    assert [x["text"] for x in env[1][0]["template"]["components"][0]["parameters"]] == ["Ann", "Haircut", "2026-12-01", "10:00", "Shop"]
    assert d.tables["bookings"][0]["reminder_24h_sent"] is True


def test_booking_reminder_without_a_template_is_skipped_and_stays_unsent(env, monkeypatch):
    import services.booking_service as bs
    import routes.webhook_routes as wh
    import crud
    d = env[0]
    d.tables["bookings"] = [{"id": 5, "reminder_24h_sent": False}]
    free = []
    monkeypatch.setattr(bs, "get_upcoming_reminders", lambda bid, window_hours=24.5: [
        {"id": 5, "customer_phone": CLOSED, "reminder_24h_sent": False}])
    monkeypatch.setattr(wh, "send_whatsapp", lambda *a: free.append(a) or {"messages": [1]})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    out = bs.send_reminders_for_business(1, {"id": 1, "name": "Shop", "whatsapp_phone_id": "PID"})
    assert out["sent"] == 0 and out["skipped_window"] == 1 and free == [] and d.tables["bookings"][0]["reminder_24h_sent"] is False


# ── the webhook: keywords never reach the AI, other messages always do ─────────────────────────────
from test_webhook_media_and_voice import client, _envelope, _capture_generate_reply, CUSTOMER_PHONE   # noqa: E402,F401
import routes.webhook_routes as webhook_routes                                                       # noqa: E402


def _text(i, body):
    return {"type": "text", "from": CUSTOMER_PHONE, "id": i, "text": {"body": body}}


@pytest.fixture
def consent_log(monkeypatch):
    log = []
    monkeypatch.setattr(wc, "available", lambda: True)
    monkeypatch.setattr(wc, "record", lambda b, p, s, src, ev="", by="": log.append((b, p, s, src)) or True)
    monkeypatch.setattr(wc, "statuses", lambda b, p: {})
    return log


def test_stop_on_whatsapp_opts_out_confirms_and_never_reaches_the_ai(client, monkeypatch, consent_log):
    calls = _capture_generate_reply(monkeypatch)
    assert client.post("/webhook", json=_envelope(_text("wamid.s1", "STOP"))).status_code == 200
    assert calls == [] and [(s) for (_, _, s, _) in consent_log] == ["opted_out"]
    assert consent_log[0][3] == "customer_message" and consent_log[0][1] == CUSTOMER_PHONE
    sent = webhook_routes.send_whatsapp.call_args[0][3]
    assert "unsubscribed" in sent and "START" in sent


def test_subscribe_on_whatsapp_opts_in(client, monkeypatch, consent_log):
    calls = _capture_generate_reply(monkeypatch)
    client.post("/webhook", json=_envelope(_text("wamid.s2", "SUBSCRIBE")))
    assert calls == [] and [(s) for (_, _, s, _) in consent_log] == ["opted_in"]
    assert "subscribed" in webhook_routes.send_whatsapp.call_args[0][3]


@pytest.mark.parametrize("body", ["hi", "I want 2 burgers", "stop the order", "cancel my order", "yes", "where is my order"])
def test_every_other_message_goes_to_the_ai_untouched(client, monkeypatch, consent_log, body):
    calls = _capture_generate_reply(monkeypatch)
    client.post("/webhook", json=_envelope(_text("wamid.o-" + body[:4].replace(" ", ""), body)))
    assert len(calls) == 1 and consent_log == []


def test_without_the_migration_stop_is_just_a_normal_message(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    monkeypatch.setattr(wc, "available", lambda: False)
    client.post("/webhook", json=_envelope(_text("wamid.s3", "STOP")))
    assert len(calls) == 1


def test_a_consent_error_never_blocks_the_conversation(client, monkeypatch):
    calls = _capture_generate_reply(monkeypatch)
    monkeypatch.setattr(wc, "handle_inbound", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.post("/webhook", json=_envelope(_text("wamid.s4", "STOP"))).status_code == 200
    assert len(calls) == 1
