"""Phase 3d: follow-ups from the application-wide DOM / server-HTML sink audit.

Covers: HTML injection in outgoing emails, social-link host bypass, currency-symbol handling
(validated on save, escaped / sanitised on render, safe inside the generated site's script),
and the remaining frontend escapes."""
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
STATIC = BACKEND.parent / "static"
PAYLOAD = '<img src=x onerror=alert(1)>'
LINK = '"><a href="https://evil.example">verify</a>'


def _read(p):
    return pathlib.Path(p).read_text(encoding="utf8")


# ── emails ────────────────────────────────────────────────────────────────────
@pytest.fixture
def sent(monkeypatch):
    from services import email_service as es
    box = []
    monkeypatch.setattr(es, "_send", lambda to, subject, html, *a, **k: box.append((to, subject, html)) or True)
    monkeypatch.setattr(es, "_is_opted_out", lambda *_a, **_k: False)
    return box


def _assert_escaped(html):
    assert "<img src=x" not in html
    assert 'href="https://evil.example"' not in html
    assert "&lt;img" in html or "&lt;a href" in html or "&quot;&gt;&lt;a" in html


def test_welcome_email_escapes_business_and_username(sent):
    from services import email_service as es
    es.send_welcome_email("v@example.com", PAYLOAD, LINK, owner_name=PAYLOAD)
    _assert_escaped(sent[0][2])


def test_receipt_escapes_item_customer_business_and_symbol(sent):
    from services import email_service as es
    es.send_purchase_receipt("v@example.com", PAYLOAD, LINK,
                             [{"name": PAYLOAD, "qty": 1, "price": 2.0}], 2.0, currency_symbol=PAYLOAD)
    html = sent[0][2]
    _assert_escaped(html)
    assert "<img" not in html


def test_other_emails_escape_business_name(sent):
    from services import email_service as es
    es.send_trial_expiry_warning("o@example.com", PAYLOAD, 3)
    es.send_trial_expired("o@example.com", PAYLOAD)
    es.send_payment_failed("o@example.com", PAYLOAD, "Starter", "$5.00", "1 Jan")
    es.send_subscription_confirmed("o@example.com", PAYLOAD, "Starter", "$5.00", "1 Feb")
    assert len(sent) >= 4
    for _to, _subj, html in sent:
        assert "<img src=x" not in html


def test_email_subjects_stay_plain_text(sent):
    from services import email_service as es
    es.send_purchase_receipt("v@example.com", "A", "Tom & Jerry's <Shop>\nBcc: x@y",
                             [{"name": "t", "qty": 1, "price": 1}], 1.0)
    subject = sent[0][1]
    assert "&amp;" not in subject and "&#x27;" not in subject and "\n" not in subject
    assert "Tom & Jerry's <Shop>" in subject


def test_weekly_report_escapes_business_name():
    from services import weekly_report_service as w
    html = w._build_report_html(PAYLOAD, {"orders_7d": 1, "revenue_7d": 1.0, "orders_30d": 1, "revenue_30d": 1.0, "new_customers_30d": 1, "new_customers_7d": 1, "repeat_customers": 1, "top_product": "<img src=x onerror=1>"}, "https://x", "month", "https://u")
    assert "<img src=x" not in html


# ── site generator ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [
    "https://evil.example\\.instagram.com/x",
    "https://evil.example@instagram.com/x",
    "https://instagram.com:80@evil.example/x",
    "https://instagram.com.evil.example/x",
])
def test_social_url_rejects_host_tricks(value):
    from services.site_generator import _social_url
    assert _social_url("instagram", value) == ""


def test_social_url_still_accepts_real_links_and_handles():
    from services.site_generator import _social_url
    assert _social_url("instagram", "https://www.instagram.com/shop").startswith("https://")
    assert _social_url("instagram", "@shop") == "https://instagram.com/shop"


def test_currency_symbol_cannot_break_out_of_site_script():
    from services import site_generator as sg
    import inspect
    src = inspect.getsource(sg)
    i = src.index("sym_js = (")
    expr = src[i:i + 300]
    assert '\\\\u003c' in expr and "[:8]" in expr
    # behavioural: replay the same transformation on hostile input
    import json
    hostile = "<!--<script>'\"</script>"
    out = (json.dumps(str(hostile)[:8])[1:-1].replace("'", "\\'")
           .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    assert "<" not in out and ">" not in out


# ── currency symbol validation ────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["<img src=x onerror=1>", 'a"b', "a'b", "a&b", "a\\b", "a`b", "123456789", "a\nb"])
def test_clean_currency_symbol_rejects(bad):
    from crud._helpers import clean_currency_symbol
    with pytest.raises(ValueError):
        clean_currency_symbol(bad)


@pytest.mark.parametrize("good", ["$", "zł", "R", "ZWL$", "€", "KSh", "د.إ", ""])
def test_clean_currency_symbol_accepts_real_symbols(good):
    from crud._helpers import clean_currency_symbol
    assert clean_currency_symbol(good) == good


def test_business_update_and_onboarding_reject_bad_symbol():
    biz = _read(BACKEND / "routes" / "business_routes.py")
    onb = _read(BACKEND / "routes" / "onboarding_routes.py")
    assert "clean_currency_symbol" in biz and "clean_currency_symbol" in onb
    assert "except HTTPException:\n        raise" in onb.replace("\r\n", "\n")


# ── frontend escapes ──────────────────────────────────────────────────────────
def test_public_store_escapes_currency_symbol():
    assert "const curr = esc(sym(biz));" in _read(STATIC / "store.html") + _read(STATIC / "store.js")


def test_dashboard_currency_helper_strips_markup_chars():
    js = _read(STATIC / "dashboard.js")
    i = js.index("function getCurrencySymbol")
    body = js[i:i + 260]
    assert "replace(/[<>\"'&" in body and "slice(0, 8)" in body


def test_dashboard_currency_converter_and_inbox_escape_remaining_values():
    js = _read(STATIC / "dashboard.js")
    assert "${escHtml(fromCurrency)}" in js and "${escHtml(toCurrency)}" in js
    assert "${escHtml(res.to_currency)}" in js
    inbox = _read(STATIC / "inbox.js")
    assert "msg-status ${escHtml(msg.status || 'sent')}" in inbox
    assert "escHtml((n.timestamp||'').slice(0,16)" in inbox


# ── review follow-ups ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["https://nottwitter.com/x", "https://eviltwitter.com/a", "https://instagram.com:443/a"])
def test_x_platform_and_ports_rejected(value):
    from services.site_generator import _social_url
    assert _social_url("x", value) == "" if "twitter" in value else _social_url("instagram", value) == ""


@pytest.mark.parametrize("platform,value", [("x", "https://twitter.com/shop"), ("x", "https://x.com/shop"),
                                            ("x", "https://mobile.twitter.com/shop"),
                                            ("instagram", "https://INSTAGRAM.com/a"), ("telegram", "https://t.me/a")])
def test_legitimate_social_links_still_pass(platform, value):
    from services.site_generator import _social_url
    assert _social_url(platform, value) != ""


def test_weekly_report_title_is_escaped():
    src = _read(BACKEND / "services" / "weekly_report_service.py")
    assert 'Monthly Report — {_html_escape(biz_name)}' in src


def test_cancelled_email_escapes_plan_and_date(sent):
    from services import email_service as es
    es.send_subscription_cancelled("o@example.com", "Biz", PAYLOAD, PAYLOAD)
    html = sent[0][2]
    assert "<img src=x" not in html
    assert "\n" not in sent[0][1]            # subject is plain text: no HTML escaping, no newlines
