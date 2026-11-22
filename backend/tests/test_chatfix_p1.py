"""Customer-chat fixes, priority 1: #1 START greeting, #2/#13 money formatting and currency, #4/#5 progress and
separate payment, #9 business time zone. All faked; nothing is sent."""
import asyncio
import types

import pytest

from services.money import format_money, resolve_order_currency, order_money
from services.tz_display import format_order_datetime, business_tz
from services._ai_payments import build_order_progress
from services._ai_intent import is_business_start
from test_stage3_cart_lifecycle import world, PHONE, TEA2, state   # noqa: F401  (world = fixture)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═════════ #13 money formatting ═════════
@pytest.mark.parametrize("kw,expected", [
    ({"amount": 7.53, "currency": "PLN"}, "7,53 zł"),
    ({"amount": 7.53, "symbol": "zł"}, "7,53 zł"),
    ({"amount": 7.53, "currency": "USD"}, "$7.53"),
    ({"amount": 7.53, "currency": "GBP"}, "£7.53"),
    ({"amount": 1234.5, "currency": "PLN"}, "1 234,50 zł"),
    ({"amount": 1234.5, "currency": "USD"}, "$1,234.50"),
    ({"amount": 1500, "currency": "JPY"}, "¥1,500"),
    ({"amount": 7.53}, "$7.53"),
    ({"amount": 7.53, "symbol": "Ksh"}, "Ksh7.53"),
    ({"amount": 7.53, "currency": "EUR", "country": "DE"}, "7,53 €"),
    ({"amount": 7.53, "currency": "EUR"}, "€7.53"),
    ({"amount": -7.5, "currency": "USD"}, "-$7.50"),
    ({"amount": "12.5", "currency": "PLN"}, "12,50 zł"),
    ({"amount": 0, "currency": "PLN"}, "0,00 zł"),
])
def test_format_money(kw, expected):
    assert format_money(**kw) == expected


@pytest.mark.parametrize("bad", [None, "x", float("nan"), object()])
def test_format_money_never_raises(bad):
    assert isinstance(format_money(bad, currency="PLN"), str)


def test_rounding_is_half_up_and_display_only():
    assert format_money(0.005, currency="USD") == "$0.01"
    assert format_money(2.675, currency="USD") == "$2.68"


def test_currency_resolution_order_is_order_then_business_then_default():
    assert resolve_order_currency({"currency": "EUR"}, {"currency": "PLN"})[0] == "EUR"
    assert resolve_order_currency({}, {"currency": "PLN", "currency_symbol": "zł"}) == ("PLN", "zł")
    assert resolve_order_currency({"currency_symbol": "zł"}, {}) == ("PLN", "zł")
    assert resolve_order_currency({}, {}) == ("USD", "$")


def test_order_money_uses_the_business_when_the_order_has_no_currency():
    assert order_money({"total_price": 7.53}, {"currency": "PLN", "currency_symbol": "zł"}) == "7,53 zł"


# ═════════ #2 no hard-coded dollar in payment messages ═════════
def test_manual_confirm_message_uses_the_order_currency(monkeypatch):
    import crud
    import routes.chat_routes as cr
    import workflows.order_lifecycle as ol
    sent = []
    order = {"id": 48, "business_id": 1, "customer_phone": "2637", "total_price": 7.53, "status": "pending",
             "payment_status": "pending_cash", "currency_symbol": "zł"}
    monkeypatch.setattr(ol, "get_order", lambda oid: order)
    monkeypatch.setattr(crud, "claim_order_paid", lambda *a: True)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": 1, "currency": "PLN", "currency_symbol": "zł"})
    monkeypatch.setattr(ol, "update_order_status_supabase", lambda *a, **k: {})
    monkeypatch.setattr(ol, "_sync_user_memory_after_order", lambda *a: None)

    async def notify(o, m):
        sent.append(m)
    monkeypatch.setattr(cr, "_notify_customer_payment", notify)

    class Req:
        async def json(self):
            return {"order_id": 48, "reference": "ORDER-48", "amount": 7.53}
    _run(cr.manual_payment_confirm(Req(), user={"business_id": 1}))
    assert "7,53 zł" in sent[0] and "$" not in sent[0]


def test_payment_webhook_message_uses_the_order_currency(monkeypatch):
    import crud
    import routes.webhook_routes as wr
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "s")
    order = {"id": 48, "business_id": 1, "customer_phone": "2637", "total_price": 7.53, "status": "pending",
             "payment_status": "pending", "currency_symbol": "zł"}
    sent = []
    monkeypatch.setattr(wr, "get_order", lambda oid: order)
    monkeypatch.setattr(crud, "claim_order_paid", lambda *a: True)
    monkeypatch.setattr(wr, "update_order_status_supabase", lambda *a, **k: {})
    monkeypatch.setattr(wr, "send_whatsapp", lambda *a, **k: sent.append(a[3]) or {})
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": 1, "whatsapp_phone_id": "P", "currency": "PLN", "currency_symbol": "zł"})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "T")
    req = types.SimpleNamespace(headers={"X-Payment-Webhook-Secret": "s"})
    _run(wr.payment_webhook(types.SimpleNamespace(reference="ORDER-48", amount=7.53), req))
    assert "7,53 zł" in sent[0] and "$" not in sent[0]


def test_no_hard_coded_dollar_left_in_customer_payment_code():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("routes/chat_routes.py", "routes/webhook_routes.py", "workflows/payment_reminder.py",
                "services/pdf_invoice.py", "services/invoice_service.py"):
        assert "${" not in (root / rel).read_text(encoding="utf8"), rel


def test_reminder_message_uses_the_order_currency():
    import workflows.payment_reminder as pr
    order = {"id": 7, "business_id": None, "total_price": 12.5, "currency_symbol": "zł", "payment_method": "cash"}
    msg = pr.build_reminder_message(order, "Shop", 1)
    assert "12,50 zł" in msg and "$" not in msg


# ═════════ #9 business time zone ═════════
WAW = {"features_json": {"timezone": "Europe/Warsaw"}}


@pytest.mark.parametrize("utc,expected", [
    ("2026-11-22T16:58:00+00:00", "2026-11-22 17:58"),      # winter, +1
    ("2026-07-01T10:00:00Z", "2026-07-01 12:00"),           # summer, +2
    ("2026-03-29T00:30:00Z", "2026-03-29 01:30"),           # just before the spring change
    ("2026-03-29T01:30:00Z", "2026-03-29 03:30"),           # just after
    ("2026-10-25T00:30:00Z", "2026-10-25 02:30"),           # before the autumn change (CEST)
    ("2026-10-25T01:30:00Z", "2026-10-25 02:30"),           # after (CET)
])
def test_utc_is_shown_in_the_business_zone_dst_safe(utc, expected):
    assert format_order_datetime(utc, WAW) == expected


def test_timezone_fallbacks_never_raise():
    assert format_order_datetime("2026-11-22T16:58:00", {}) == "2026-11-22 16:58"                       # naive = UTC
    assert format_order_datetime("2026-11-22T16:58:00Z", {"features_json": {"timezone": "Nope/Zone"}}) == "2026-11-22 16:58"
    assert format_order_datetime("garbage", WAW) == "garbage"
    assert format_order_datetime(None, WAW) == ""
    assert business_tz(None) is not None


# ═════════ #4/#5 progress: fulfilment vs payment ═════════
def test_pickup_has_its_own_stages_and_never_says_on_the_way():
    p = build_order_progress("ready", "pending_cash", "pickup")
    assert p["labels"] == ["Received", "Confirmed", "Preparing", "Ready for pickup", "Collected"]
    assert p["header"][1] == "Ready for pickup!" and "On the way" not in p["progress"] and "on the way" not in p["header"][1].lower()
    assert p["eta"] == "" and "Ready now" not in str(p)


def test_delivery_has_its_own_stages():
    p = build_order_progress("out_for_delivery", "paid", "delivery")
    assert p["labels"][3:] == ["Out for delivery", "Delivered"] and "on the way" in p["header"][1].lower()
    assert p["progress"].endswith("Out for delivery")


def test_payment_never_replaces_the_fulfilment_header():
    for pay in ("paid", "pending_cash", "awaiting_payment"):
        assert build_order_progress("ready", pay, "pickup")["header"][1] == "Ready for pickup!"
    assert build_order_progress("preparing", "paid", "pickup")["header"][1] == "Your order is being prepared"


def test_paid_pulls_a_pending_order_to_confirmed_but_no_further():
    p = build_order_progress("pending", "paid", "pickup")
    assert p["stage"] == 1 and p["header"][1] == "Order confirmed"


def test_completed_stages_are_all_checked_with_no_waiting_marker():
    done = build_order_progress("completed", "paid", "pickup")["progress"]
    assert done.count("✅") == 5 and "⏳" not in done and "🔵" not in done
    mid = build_order_progress("preparing", "paid", "delivery")["progress"]
    assert mid.startswith("✅ ✅ 🔵 ⬜ ⬜")


def test_payment_line_state_is_independent():
    assert build_order_progress("ready", "paid", "pickup")["payment"][0] == "✅"
    assert build_order_progress("ready", "pending_cash", "pickup")["payment"][0] == "⏳"
    assert build_order_progress("cancelled", "refunded", "pickup")["closed"] == "cancelled"
    assert build_order_progress("refunded", "refunded", None)["header"][1] == "Order refunded"


def test_unknown_fulfilment_uses_neutral_wording():
    p = build_order_progress("ready", "paid", None)
    assert p["kind"] == "generic" and p["labels"][3] == "Ready"


def test_status_message_shows_payment_on_its_own_line_and_local_date(monkeypatch):
    import crud
    import services._ai_payments as ap
    import workflows.order_lifecycle as ol
    order = {"id": 48, "business_id": 1, "customer_phone": PHONE, "total_price": 7.53, "status": "ready",
             "payment_status": "paid", "fulfillment_method": "pickup", "currency_symbol": "zł",
             "created_at": "2026-11-22T16:58:00+00:00"}
    monkeypatch.setattr(ol, "get_order", lambda oid: order)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": 1, "currency": "PLN", "currency_symbol": "zł",
                                                                "features_json": {"timezone": "Europe/Warsaw"}})
    out = ap._order_status_message(48, PHONE, 1, "zł")
    assert "Ready for pickup!" in out and "Payment : *Paid" in out
    assert "2026-11-22 17:58" in out and "7,53 zł" in out
    assert "On the way" not in out and "Ready now" not in out and "Estimated" not in out
    assert out.index("Ready for pickup!") < out.index("Payment :")


# ═════════ #1 START greeting ═════════
@pytest.mark.parametrize("text,biz,expected", [
    ("START FLAVOURY-FOODS", "Flavoury Foods", True),
    ("start flavoury-foods", "Flavoury Foods", True),
    ("  Start   FLAVOURY FOODS ", "Flavoury Foods", True),
    ("START OTHER-SHOP", "Flavoury Foods", False),
    ("start", "Flavoury Foods", False),
    ("I want to start ordering", "Flavoury Foods", False),
    ("START", "", False), ("", "Flavoury Foods", False),
])
def test_is_business_start(text, biz, expected):
    assert is_business_start(text, biz) is expected


def _say(world, text, name="Flavoury Foods"):
    import services.ai as ai
    return ai.generate_reply(message=text, phone=PHONE, business_id=1, business_name=name,
                             products=world.db.tables["products"], business_config={"id": 1, "is_service_business": False})


def test_start_keyword_welcomes_instead_of_the_not_sure_fallback(world):
    out = _say(world, "START FLAVOURY-FOODS")
    assert "not sure" not in out.lower() and "Flavoury Foods" in out
    assert "*menu*" in out and "where is my order" in out and "talk to a person" in out


def test_start_keyword_mentions_an_old_cart_in_one_line_and_does_not_print_it(world):
    world.put_cart([{"name": "Grilled Salmon", "qty": 1, "price": 9.39}], state="browsing")
    out = _say(world, "START FLAVOURY-FOODS")
    assert "You still have 1 item in your cart" in out and "Grilled Salmon" not in out and "9.39" not in out
    world.put_cart(TEA2, state="browsing")
    assert "You still have 2 items in your cart" in _say(world, "START FLAVOURY-FOODS")


def test_start_keyword_without_a_cart_has_no_cart_line(world):
    world.put_cart([], state="browsing")
    assert "cart" not in _say(world, "START FLAVOURY-FOODS").lower().split("what would you like")[1].split("menu")[0]
    assert "still have" not in _say(world, "START FLAVOURY-FOODS")


def test_another_businesss_slug_is_not_treated_as_this_businesss_greeting(world):
    out = _say(world, "START SOMEONE-ELSE")
    assert "You're chatting with *Flavoury Foods*" not in out


def test_start_keyword_does_not_change_the_cart(world):
    world.put_cart(TEA2, state="browsing")
    _say(world, "START FLAVOURY-FOODS")
    assert [i["name"] for i in world.db.cart_row()["items"]] == ["Tea"]


def test_bare_start_still_works_as_before(world):
    assert "not sure" not in _say(world, "start").lower()
