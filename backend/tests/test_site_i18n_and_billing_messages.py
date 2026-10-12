"""
tests/test_site_i18n_and_billing_messages.py

Regression tests for the site-generator / translation / cancellation-message work:

  A. translation_service  — masking of prices/URLs/emails/{placeholders}, explicit
     source+target, output validation (unchanged / wrong script / corrupted spans),
     unavailable pairs, failures never cached, bounded cache, language detection
  B. site_generator       — <html lang>/dir, per-text source language, coverage of
     previously hardcoded strings, honest translation status, owner edits,
     JSON-LD validity, script-injection hardening
  C. booking page         — localized, escaped, safe constants
  D. settings validation  — Website settings sanitised server-side
  E. billing messages     — all languages complete, dates localized, and every
     cancellation outcome (A–F) matches what Stripe actually reported

Translation ENGINE output is faked deterministically (Argos is optional and not
installed here); live translation QUALITY still needs manual verification — these
tests prove the plumbing: languages passed, values preserved, failures reported.
"""

import json
import re
import sys
import types
from html.parser import HTMLParser

import pytest

from test_billing_lifecycle import env, activate, make_sub, deliver, NOW, DAY  # noqa: F401  (fixtures)

import billing.stripe_service as svc
from billing import messages as bm


# ═══ A. translation_service ═════════════════════════════════════════════════

@pytest.fixture
def ts():
    sys.modules.pop("services.translation_service", None)
    import services.translation_service as t
    t._argos_checked, t._argos_available = True, True
    t._argos_installed_pairs = {("en", "pl"), ("en", "fr"), ("en", "ar"), ("pl", "en"), ("en", "de")}
    yield t
    sys.modules.pop("services.translation_service", None)


def _engine(ts, monkeypatch, fn):
    calls = []
    monkeypatch.setattr(ts, "_argos_translate_raw", lambda text, s, t: calls.append((text, s, t)) or fn(text, s, t))
    return calls


def test_explicit_source_and_target_are_passed_to_the_engine(ts, monkeypatch):
    calls = _engine(ts, monkeypatch, lambda t, s, tg: "Świeży chleb codziennie dla wszystkich klientów")
    out, status = ts.translate_text_checked("Fresh bread daily for all our customers", "pl", source_lang="en", business_id=1)
    assert status == "translated" and out.startswith("Świeży")
    assert [(c[1], c[2]) for c in calls] == [("en", "pl")]


def test_prices_urls_emails_phones_and_placeholders_survive(ts, monkeypatch):
    seen = {}

    def fake(text, s, t):
        seen["masked"] = text
        return text.replace("Call", "Zadzwoń").replace("or email", "lub napisz").replace("price", "cena")   # keeps ZQ tokens

    _engine(ts, monkeypatch, fake)
    src = "Call +263 771 234 567 or email hi@shop.com, price $12.50 at https://shop.com/menu for {customer_name}"
    out, status = ts.translate_text_checked(src, "pl", business_id=1)
    assert status == "translated"
    for keep in ("+263 771 234 567", "hi@shop.com", "$12.50", "https://shop.com/menu", "{customer_name}"):
        assert keep in out                                   # restored byte-for-byte
        assert keep not in seen["masked"]                    # and never exposed to the engine
    assert "ZQ" not in out


def test_corrupted_protected_span_rejects_the_whole_translation(ts, monkeypatch):
    _engine(ts, monkeypatch, lambda t, s, tg: "Cena wynosi tylko dwanaście dolarów")      # tokens dropped
    out, status = ts.translate_text_checked("The price is $12.50 for everyone today", "pl", business_id=1)
    assert status == "failed_validation" and out == "The price is $12.50 for everyone today"


def test_engine_echoing_the_source_is_a_failure_not_a_success(ts, monkeypatch):
    _engine(ts, monkeypatch, lambda t, s, tg: t)
    out, status = ts.translate_text_checked("We bake fresh bread every single morning", "fr", business_id=1)
    assert status == "failed_validation"


def test_arabic_output_must_actually_be_arabic_script(ts, monkeypatch):
    _engine(ts, monkeypatch, lambda t, s, tg: "We bake fresh bread every morning (not really Arabic)")
    assert ts.translate_text_checked("We bake fresh bread every single morning", "ar", business_id=1)[1] == "failed_validation"
    _engine(ts, monkeypatch, lambda t, s, tg: "نخبز الخبز الطازج كل صباح")
    out, status = ts.translate_text_checked("We bake fresh bread every single morning", "ar", business_id=2)
    assert status == "translated" and out == "نخبز الخبز الطازج كل صباح"


def test_arabic_script_where_latin_language_requested_is_rejected(ts, monkeypatch):
    _engine(ts, monkeypatch, lambda t, s, tg: "نخبز الخبز الطازج كل صباح")
    assert ts.translate_text_checked("We bake fresh bread every single morning", "fr", business_id=1)[1] == "failed_validation"


def test_missing_pair_is_reported_unavailable_not_translated(ts, monkeypatch):
    out, status = ts.translate_text_checked("Fresh bread daily for everyone", "pt", business_id=1)   # no en->pt model installed
    assert out == "Fresh bread daily for everyone" and status == "unavailable"


def test_engine_error_is_reported_failed_and_never_cached(ts, monkeypatch):
    state = {"n": 0}

    def fake(t, s, tg):
        state["n"] += 1
        return None if state["n"] == 1 else "Świeży chleb codziennie dla wszystkich klientów"
    monkeypatch.setattr(ts, "_argos_translate_raw", lambda t, s, tg: fake(t, s, tg))
    assert ts.translate_text_checked("Fresh bread daily for all our customers", "pl", business_id=1)[1] == "failed"
    assert ts.translate_text_checked("Fresh bread daily for all our customers", "pl", business_id=1)[1] == "translated"
    assert ts.translate_text_checked("Fresh bread daily for all our customers", "pl", business_id=1)[1] == "cached"


@pytest.mark.parametrize("args,status", [
    (("hello there my friend", "en"), "same_language"),
    (("hello there my friend", "zz"), "unsupported_target"),
    (("", "pl"), "empty"),
    (("+263771234567", "pl"), "protected"),
    (("$49.99", "pl"), "protected"),
])
def test_noop_statuses(ts, args, status):
    assert ts.translate_text_checked(args[0], args[1], source_lang="en", business_id=1)[1] == status


def test_unsupported_source_language_is_reported(ts):
    assert ts.translate_text_checked("coisas boas para todos vocês", "pl", source_lang="xx")[1] == "unsupported_source"


def test_cache_is_bounded(ts, monkeypatch):
    monkeypatch.setattr(ts, "_CACHE_MAX_ENTRIES", 20)
    _engine(ts, monkeypatch, lambda t, s, tg: "Świeży chleb codziennie dla wszystkich klientów numer " + t[-3:])
    for i in range(60):
        ts.translate_text_checked(f"Fresh bread daily for customer number {i:03d}", "pl", business_id=1)
    assert len(ts._cache) <= 20


def test_translate_text_wrapper_still_returns_original_on_failure(ts):
    ts._argos_installed_pairs = set()
    assert ts.translate_text("Fresh bread daily for everyone", "pl") == "Fresh bread daily for everyone"


def test_detect_language_signals(ts):
    assert ts.detect_language("مرحبا بكم في متجرنا الجميل") == "ar"
    assert ts.detect_language("Zapraszamy do naszego salonu, czekamy na Państwa") == "pl"
    assert ts.detect_language("Hi") is None
    assert ts.detect_language("Hello and welcome to our little shop here") is None      # unsure → no guess (langdetect absent)


def test_detect_language_uses_langdetect_when_confident(ts, monkeypatch):
    fake = types.ModuleType("langdetect")
    fake.DetectorFactory = types.SimpleNamespace(seed=0)
    fake.detect_langs = lambda t: [types.SimpleNamespace(lang="fr", prob=0.97)]
    monkeypatch.setitem(sys.modules, "langdetect", fake)
    assert ts.detect_language("Bienvenue dans notre salon de coiffure, ouvert tous les jours") == "fr"
    fake.detect_langs = lambda t: [types.SimpleNamespace(lang="fr", prob=0.6)]
    assert ts.detect_language("Bienvenue dans notre salon de coiffure, ouvert tous les jours") is None
    fake.detect_langs = lambda t: [types.SimpleNamespace(lang="sw", prob=0.99)]            # not a supported site language
    assert ts.detect_language("Karibu sana kwenye duka letu kubwa la mjini") is None


def test_wrong_language_output_is_rejected_when_detector_is_confident(ts, monkeypatch):
    fake = types.ModuleType("langdetect")
    fake.DetectorFactory = types.SimpleNamespace(seed=0)
    fake.detect_langs = lambda t: [types.SimpleNamespace(lang="de", prob=0.99)]
    monkeypatch.setitem(sys.modules, "langdetect", fake)
    long_out = "Dies ist eindeutig ein deutscher Satz, der niemals als Französisch durchgehen sollte, wirklich nicht."
    _engine(ts, monkeypatch, lambda t, s, tg: long_out)
    assert ts.translate_text_checked("This is clearly an English sentence and it is fairly long indeed", "fr", business_id=1)[1] == "failed_validation"


def test_engine_status_shape(ts):
    st = ts.engine_status()
    assert st["engine"] == "argos" and st["available"] is True
    assert ["en", "pl"] in st["pairs"] and set(st["supported_languages"]) == set(ts.SUPPORTED_SITE_LANGS)


# ═══ B. site_generator ═════════════════════════════════════════════════════

@pytest.fixture
def sg(monkeypatch):
    import services.site_generator as g
    return g


def _mk(sg, monkeypatch, cfg=None, tagline=None, products=None, fj_extra=None, name="Salon Ola", category="Hair", **biz_extra):
    fj = {"site_generator": cfg or {}}
    fj.update(fj_extra or {})
    biz = {"id": 7, "name": name, "category": category, "currency_symbol": "$", "contact_phone": "263771",
           "use_shared_number": False, "tagline": tagline, "features_json": fj, "is_service_business": False,
           "logo_url": "https://cdn.example.com/logo.png", **biz_extra}
    prods = products if products is not None else [
        {"id": 1, "name": "Cut", "price": 5, "description": "Świetne strzyżenie dla każdego klienta salonu.",
         "image_url": "https://x.example/y.png", "category": "Hair", "stock": 2}]
    monkeypatch.setattr(sg, "_get_business_and_products", lambda s: (biz, prods))
    monkeypatch.setattr(sg, "_get_reviews", lambda i, limit=6: [{"customer_name": "", "last_rating": "5", "order_count": 2}])
    return biz


def _fake_translate(calls=None, fail_langs=(), unchanged_langs=()):
    def fn(text, source_lang="en", business_id=None, target_langs=None):
        if calls is not None:
            calls.append((text, source_lang))
        out = {source_lang: text}
        for l in ("en", "pl", "fr", "pt", "es", "de", "ar"):
            if l == source_lang or l in fail_langs:
                continue
            out[l] = text if l in unchanged_langs else f"[{l}]{text}"
        return out
    return fn


def test_html_lang_and_dir_follow_the_site_language(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={"language": "pl"})
    assert re.search(r'<html lang="pl" dir="ltr">', sg.generate_site_html("salon-ola"))
    _mk(sg, monkeypatch, cfg={"language": "ar"})
    h = sg.generate_site_html("salon-ola")
    assert '<html lang="ar" dir="rtl">' in h and "WZ_I18N_DEFAULT = \"ar\"" in h
    assert '<meta property="og:locale" content="ar">' in h and '"inLanguage": "ar"' in h


def test_language_resolution_order(sg, monkeypatch):
    s = sg._get_site_settings({"site_generator": {"language": "de"}})
    assert sg._site_language({"features_json": {"language": "fr"}}, s) == "de"              # explicit site setting wins
    s = sg._get_site_settings({})
    assert sg._site_language({"features_json": {"language": "fr"}}, s) == "fr"              # explicit profile language
    assert sg._site_language({"features_json": {"language": "en"}}, s) == "en"
    assert sg._site_language({"features_json": {"language": "sn"}}, s) == "en"              # unsupported → not pretended
    s = sg._get_site_settings({"site_generator": {"language": "xx"}})
    assert s["language"] == ""                                                               # invalid code ignored
    s = sg._get_site_settings({"site_generator": {"description": "مرحبا بكم في متجرنا الجميل جدا"}})
    assert sg._site_language({"features_json": {}}, s) == "ar"                              # detected from owner text


def test_owner_text_uses_site_language_as_source_and_generated_copy_uses_english(sg, monkeypatch):
    import services.translation_service as ts
    calls = []
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(calls))
    _mk(sg, monkeypatch, cfg={"language": "pl", "description": "Witamy w naszym salonie.", "hero_tagline": "",
                              "cta_text": "Zamów teraz", "contact_title": "Odwiedź nas", "hero_title": "Najlepszy salon"},
        tagline=None)
    sg.generate_site_html("salon-ola")
    by_text = {t: src for t, src in calls}
    assert by_text["Witamy w naszym salonie."] == "pl"                    # owner text → Polish source (was always "en")
    assert by_text["Zamów teraz"] == "pl" and by_text["Odwiedź nas"] == "pl" and by_text["Najlepszy salon"] == "pl"
    assert by_text["Świetne strzyżenie dla każdego klienta salonu."] == "pl"
    # generated English fallback copy (no owner description/tagline) is NOT mislabeled as Polish
    del calls[:]
    _mk(sg, monkeypatch, cfg={"language": "pl"}, tagline=None)
    sg.generate_site_html("salon-ola")
    assert {src for t, src in calls if t.startswith("Order ")} == {"en"}


def test_generated_fallback_about_copy_is_english_source(sg, monkeypatch):
    import services.translation_service as ts
    calls = []
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(calls))
    _mk(sg, monkeypatch, cfg={"language": "fr"})
    sg.generate_site_html("salon-ola")
    about = [src for t, src in calls if "is dedicated to delivering" in t]
    assert about == ["en"]


def test_custom_owner_text_follows_the_language_switcher(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={"cta_text": "Order now", "contact_title": "Visit us", "hero_title": "Best salon"})
    h = sg.generate_site_html("salon-ola")
    for key in ("cta_text_7", "contact_title_7", "hero_title_7", "meta_desc"):
        assert f'data-i18n-dyn="{key}"' in h or f'data-i18n-dyn-content="{key}"' in h
    blob = json.loads(re.search(r"const WZ_I18N_DYNAMIC = (\{.*?\});\nconst", h, re.S).group(1))
    assert blob["fr"]["cta_text_7"] == "[fr]Order now" and blob["ar"]["contact_title_7"] == "[ar]Visit us"


def test_business_name_stays_untranslated_when_no_custom_headline(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={})
    h = sg.generate_site_html("salon-ola")
    assert "hero_title_7" not in h and '<h1 class="hero-title">Salon Ola</h1>' in h


class _TextNodes(HTMLParser):
    def __init__(self):
        super().__init__(); self.stack, self.out = [], []

    def handle_starttag(self, t, a):
        a = dict(a)
        self.stack.append((t, any(k.startswith("data-i18n") for k in a), t in ("script", "style")))

    def handle_endtag(self, t):
        while self.stack:
            if self.stack.pop()[0] == t:
                break

    def handle_data(self, d):
        d = d.strip()
        if d and not any(x[2] or x[1] for x in self.stack) and not re.fullmatch(r"[\W\d_]+", d):
            self.out.append(d)


def test_no_hardcoded_english_chrome_is_left_outside_the_translation_system(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={"show_reviews": True, "location": "12 Main St", "business_hours": "9-5", "show_map": True})
    p = _TextNodes(); p.feed(sg.generate_site_html("salon-ola"))
    allowed = {"Salon Ola | Hair", "Salon Ola", "S", "Cut", "Hair", "WaziBot", "📍 12 Main St", "12 Main St", "🕐 9-5", "9-5"}
    allowed |= {f"{f} {n}" for _, f, n in sg.SITE_I18N_LANGS}                # language names stay in their own language
    leftovers = [t for t in p.out if t not in allowed]
    assert leftovers == [], f"untranslated visible text: {leftovers}"


def test_every_language_has_every_key_and_placeholders_match(sg):
    en = sg.SITE_I18N["en"]
    for lang, d in sg.SITE_I18N.items():
        assert set(d) == set(en), (lang, set(en) ^ set(d))
        for k, v in d.items():
            assert v.strip(), (lang, k)
            assert sorted(re.findall(r"\{\w+\}", v)) == sorted(re.findall(r"\{\w+\}", en[k])), (lang, k)
    for k in ("secure_payments", "footer_tagline", "footer_secure", "filter_all", "menu_label", "map_title",
              "orders_label", "customer_fallback", "translation_partial"):
        assert k in en
    assert re.search(r"[؀-ۿ]", sg.SITE_I18N["ar"]["checkout"])


def test_translation_status_complete_partial_unavailable(sg, monkeypatch):
    import services.translation_service as ts
    _mk(sg, monkeypatch, cfg={"language": "pl"})
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    h = sg.generate_site_html("salon-ola")
    assert '<meta name="wz-translation-status" content="complete">' in h and "const WZ_I18N_LANG_OK = " in h
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(fail_langs=("de",)))
    h = sg.generate_site_html("salon-ola")
    assert 'content="partial"' in h and '"de": false' in h and '"fr": true' in h
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(unchanged_langs=("fr", "en", "pt", "es", "de", "ar", "pl")))
    assert 'content="unavailable"' in sg.generate_site_html("salon-ola")
    monkeypatch.setattr(ts, "translate_for_all_languages", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    h = sg.generate_site_html("salon-ola")
    assert 'content="unavailable"' in h and "Salon Ola" in h                        # still renders


def test_failed_translations_are_omitted_so_the_page_falls_back_per_element(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(fail_langs=("fr",)))
    _mk(sg, monkeypatch, cfg={"language": "pl", "description": "Witamy w naszym salonie fryzjerskim."})
    h = sg.generate_site_html("salon-ola")
    blob = json.loads(re.search(r"const WZ_I18N_DYNAMIC = (\{.*?\});\nconst", h, re.S).group(1))
    assert "fr" not in blob and blob["de"]["about_desc_7"].startswith("[de]")
    assert blob["pl"]["about_desc_7"] == "Witamy w naszym salonie fryzjerskim."          # source language entry kept


def test_unchanged_multiword_result_is_treated_as_untranslated(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(unchanged_langs=("de",)))
    _mk(sg, monkeypatch, cfg={"language": "pl", "description": "Witamy w naszym salonie fryzjerskim dzisiaj."})
    h = sg.generate_site_html("salon-ola")
    assert '"de": false' in h


def test_work_per_page_is_bounded(sg, monkeypatch):
    import services.translation_service as ts
    calls = []
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(calls))
    prods = [{"id": i, "name": f"P{i}", "price": 1, "description": f"Product description number {i} here"} for i in range(200)]
    _mk(sg, monkeypatch, cfg={"language": "pl"}, products=prods)
    h = sg.generate_site_html("salon-ola")
    assert len(calls) <= sg._MAX_DYNAMIC_STRINGS and 'content="partial"' in h


def test_owner_edit_republish_retranslates_only_current_text(sg, monkeypatch):
    import services.translation_service as ts
    calls = []
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(calls))
    _mk(sg, monkeypatch, cfg={"language": "pl", "description": "Stary opis naszego salonu fryzjerskiego."})
    h1 = sg.generate_site_html("salon-ola")
    _mk(sg, monkeypatch, cfg={"language": "pl", "description": "Nowy opis naszego salonu fryzjerskiego."})
    h2 = sg.generate_site_html("salon-ola")
    assert "Stary opis" in h1 and "Stary opis" not in h2 and "Nowy opis" in h2
    assert '"fr": "[fr]Nowy opis' in h2 or "[fr]Nowy opis" in h2                              # new text translated
    assert sg._dyn_i18n_ctx.get() is None                                                    # registry reset between renders


def test_registry_is_isolated_between_renders(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    sg._dyn_i18n_start("pl")
    sg._register_dynamic_text("k1", "Pierwszy tekst do tłumaczenia tutaj", 1)
    sg._dyn_i18n_collect_and_translate()
    sg._dyn_i18n_start("en")
    out = sg._dyn_i18n_collect_and_translate()
    assert out == {} and sg._dyn_i18n_status()["status"] == "none"


def test_json_ld_is_valid_single_typed_and_script_safe(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    nasty = 'Joe\'s "Best" \\ Café </script><script>alert(1)</script>'
    _mk(sg, monkeypatch, cfg={}, name=nasty, category="Hair & Beauty")
    h = sg.generate_site_html("joes")
    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', h, re.S).group(1)
    data = json.loads(ld)                                                                  # valid JSON
    assert data["@type"] == "LocalBusiness" and data["name"] == nasty and data["additionalType"] == "Hair & Beauty"
    assert ld.count('"@type"') == 1 and "&#x27;" not in ld and "&quot;" not in ld
    assert "</script><script>alert(1)" not in h and "<script>alert(1)" not in h
    # the WhatsApp links built from the business name must not contain any raw markup/quotes
    for href in re.findall(r'href="(https://wa\.me/[^"]*)"', h):
        assert re.fullmatch(r"https://wa\.me/\d*\?text=[A-Za-z0-9%._~-]*", href), href


def test_json_ld_only_contains_known_facts(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={}, logo_url="javascript:alert(1)")
    h = sg.generate_site_html("salon-ola")
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', h, re.S).group(1))
    assert "image" not in ld and "og:image" not in h                                         # unsafe logo URL never emitted
    assert not ({"telephone", "address", "openingHours", "aggregateRating", "review", "priceRange"} & set(ld))


def test_category_filter_cannot_break_out_of_the_handler(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    evil = "x');alert(1);('"
    html = sg._category_filter_html([{"category": evil}, {"category": "Hair"}])

    class P(HTMLParser):
        calls = []
        def handle_starttag(self, t, a):
            for k, v in a:
                if k == "onclick": P.calls.append(v)
    P().feed(html)
    assert f'_wzFilter(this,{json.dumps(evil)})' in P.calls
    assert not any(c.count("alert(1)") and c.endswith("alert(1);('")  for c in P.calls)


def test_embedded_language_data_cannot_close_the_script_tag(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={"language": "en", "description": "Nice place </script><script>alert(1)</script> truly"})
    h = sg.generate_site_html("salon-ola")
    assert "</script><script>alert(1)" not in h and "<script>alert(1)" not in h


def test_sanitised_html_output_is_well_formed(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={"language": "ar", "location": "شارع <b>الرئيسي</b>", "show_map": True})
    h = sg.generate_site_html("salon-ola")
    assert "<b>الرئيسي</b>" not in h and "&lt;b&gt;" in h
    p = _TextNodes(); p.feed(h)                                                             # parses without error


def test_partial_translation_notice_is_wired(sg, monkeypatch):
    import services.translation_service as ts
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate())
    _mk(sg, monkeypatch, cfg={})
    h = sg.generate_site_html("salon-ola")
    assert 'id="wz-translation-note" hidden' in h and "WZ_I18N_LANG_OK[lang] !== false" in h
    assert "(browserLang === WZ_I18N_DEFAULT || WZ_I18N_LANG_OK[browserLang] !== false)" in h   # no auto-switch into a partial page


def test_prices_currency_and_ids_are_not_translated_by_the_site(sg, monkeypatch):
    import services.translation_service as ts
    calls = []
    monkeypatch.setattr(ts, "translate_for_all_languages", _fake_translate(calls))
    _mk(sg, monkeypatch, cfg={"language": "pl"})
    h = sg.generate_site_html("salon-ola")
    texts = [t for t, _ in calls]
    assert "Cut" not in texts and "Salon Ola" not in texts                                   # names never sent to MT
    assert "$5.00" in h and 'data-price="5.0"' in h and 'data-id="1"' in h


# ═══ C. booking page ═══════════════════════════════════════════════════════

def test_booking_catalogue_complete_for_all_site_languages():
    from routes.booking_routes import BOOKING_I18N
    import services.site_generator as g
    assert set(BOOKING_I18N) == {c for c, _, _ in g.SITE_I18N_LANGS}
    en = BOOKING_I18N["en"]
    for lang, d in BOOKING_I18N.items():
        assert set(d) == set(en), lang
        assert all(v.strip() for v in d.values())
    assert re.search(r"[؀-ۿ]", BOOKING_I18N["ar"]["confirm"])


def test_booking_page_uses_only_defined_keys_and_site_language():
    from routes.booking_routes import BOOKING_I18N, _render_booking_page_html
    biz = {"id": 3, "name": "Salon <b>Ola</b>", "features_json": {"site_generator": {"language": "ar"}}}
    h = _render_booking_page_html("salon-ola", biz)
    assert '<html lang="ar" dir="rtl">' in h
    used = set(re.findall(r'data-i18n(?:-ph)?="([a-z_]+)"', h)) | set(re.findall(r"_bt\('([a-z_]+)'\)", h))
    assert used and used <= set(BOOKING_I18N["en"])
    assert "Salon &lt;b&gt;Ola&lt;/b&gt;" in h


def test_booking_page_escapes_business_data_and_script_constants():
    from routes.booking_routes import _render_booking_page_html
    h = _render_booking_page_html("x</script><script>alert(1)", {"id": 3, "name": 'a"</script>b', "features_json": {}})
    assert "</script><script>alert(1)" not in h
    assert "esc(s.name)" in h and "esc(data.currency_symbol)" in h and "esc(data.reason" in h
    assert "${s.name}" not in h and "${data.reason" not in h
    assert "onclick=\"pickTime('${t}'" not in h                                              # slot handler no longer string-built


# ═══ D. settings validation ════════════════════════════════════════════════

def test_site_settings_are_sanitised_server_side():
    from routes.business_routes import _sanitize_site_settings as clean
    out = clean({"theme_style": "dark_modern", "font": "<script>", "layout": "wide", "accent_color": "red; background:url(x)",
                 "hero_title": "T" * 500, "description": "line1\nline2\x00\x07", "location": "A\tB\nC", "language": "xx",
                 "show_map": "yes", "show_hours": 0, "custom_key": {"keep": 1}, "cta_text": ["no"]})
    assert out["theme_style"] == "dark_modern" and "font" not in out and out["layout"] == "wide"
    assert out["accent_color"] == "" and len(out["hero_title"]) == 80
    assert out["description"] == "line1\nline2" and out["location"] == "A B C"
    assert out["language"] == "" and out["show_map"] is True and out["show_hours"] is False
    assert out["custom_key"] == {"keep": 1} and out["cta_text"] == ""
    assert clean("not a dict") == {} and clean({"accent_color": "#1a2B3c"})["accent_color"] == "#1a2B3c"
    assert clean({"language": "ar"})["language"] == "ar"


def test_update_me_sanitises_site_settings_and_caps_size(monkeypatch):
    import crud
    from fastapi import HTTPException
    from routes import business_routes as br
    saved = {}
    monkeypatch.setattr(crud, "get_business_by_id", lambda bid: {"id": bid, "features_json": {}})
    monkeypatch.setattr(crud, "update_business", lambda bid, d: saved.update(d.dict()) or {"id": bid})
    br.update_me(br.BusinessUpdate(features_json={"site_generator": {"hero_title": "H" * 999, "language": "fr", "font": "../x"}}),
                 user={"business_id": 1})
    sgc = saved["features_json"]["site_generator"]
    assert len(sgc["hero_title"]) == 80 and sgc["language"] == "fr" and "font" not in sgc
    with pytest.raises(HTTPException) as e:
        br.update_me(br.BusinessUpdate(features_json={"junk": "x" * 70_000}), user={"business_id": 1})
    assert e.value.status_code == 413


def test_translation_status_endpoint_reports_engine_truthfully(monkeypatch):
    from routes import business_routes as br
    import services.translation_service as ts
    monkeypatch.setattr(ts, "_ensure_argos_loaded", lambda: False)
    monkeypatch.setattr(ts, "_argos_installed_pairs", set())
    st = br.translation_status(user={"business_id": 1})
    assert st["available"] is False and st["pairs"] == []


def test_site_route_renders_off_the_event_loop(monkeypatch):
    import asyncio
    import main
    import services.site_generator as g
    from fastapi.testclient import TestClient
    seen = {}

    def fake(slug):
        try:
            asyncio.get_running_loop(); seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return "<html>ok</html>"
    monkeypatch.setattr(g, "generate_site_html", fake)
    r = TestClient(main.app).get("/site/some-shop")
    assert r.status_code == 200 and seen["on_loop"] is False


# ═══ E. billing messages ═══════════════════════════════════════════════════

KEYS = {"cancel_scheduled", "cancel_completed", "cancel_failed", "already_scheduled", "already_canceled",
        "cancel_reversed", "not_cancelling", "no_subscription"}


def test_every_language_has_every_billing_message_with_matching_placeholders():
    assert set(bm.MESSAGES) == {"en", "pl", "fr", "pt", "es", "de", "ar", "sw"}
    for lang, d in bm.MESSAGES.items():
        assert set(d) == KEYS, lang
        for k, v in d.items():
            assert v.strip()
            assert sorted(re.findall(r"\{\w+\}", v)) == sorted(re.findall(r"\{\w+\}", bm.MESSAGES["en"][k])), (lang, k)
        assert len(set(d.values())) == len(d)                                              # no accidental duplicates
    for k in ("cancel_scheduled", "already_scheduled"):
        assert all("{date}" in bm.MESSAGES[l][k] for l in bm.MESSAGES)
    assert re.search(r"[؀-ۿ]", bm.MESSAGES["ar"]["cancel_failed"])
    assert not any(re.search(r"[؀-ۿ]", bm.MESSAGES[l]["cancel_failed"]) for l in bm.MESSAGES if l != "ar")


def test_required_english_wording_is_exact():
    m = bm.MESSAGES["en"]
    assert m["cancel_scheduled"] == ("Your subscription has been set to cancel at the end of your current billing period. "
                                     "You'll continue to enjoy your paid WaziBot features until {date}. You won't be charged "
                                     "for another billing period. You can manage your subscription from your billing settings.")
    assert m["cancel_completed"] == ("Your WaziBot subscription has been canceled. Your paid access has ended. Your business data "
                                     "has not been deleted, and you can choose a plan again whenever you're ready.")
    assert m["cancel_failed"] == ("We couldn't cancel your subscription right now. Your current subscription status has not been "
                                  "changed. Please try again or contact support if the problem continues.")
    assert m["already_scheduled"] == ("Your subscription is already scheduled to end on {date}. Your paid features remain "
                                      "available until then.")
    assert m["already_canceled"] == ("Your subscription is already canceled. You can review available plans if you'd like to "
                                     "reactivate WaziBot.")
    assert m["cancel_reversed"] == ("Your cancellation has been reversed, and your subscription is active according to your "
                                    "current billing schedule.")


@pytest.mark.parametrize("lang,expected", [("en", "March 5, 2027"), ("pl", "5 marca 2027"), ("fr", "5 mars 2027"),
                                           ("pt", "5 de março de 2027"), ("es", "5 de marzo de 2027"),
                                           ("de", "5. März 2027"), ("ar", "5 مارس 2027"), ("sw", "5 Machi 2027")])
def test_dates_are_localized(lang, expected):
    assert bm.format_date("2027-03-05T10:00:00+00:00", lang) == expected
    assert expected in bm.billing_message("cancel_scheduled", lang, "2027-03-05T10:00:00+00:00")
    assert "{date}" not in bm.billing_message("already_scheduled", lang, "2027-03-05T10:00:00+00:00")


def test_unknown_dates_are_never_invented_and_unreviewed_languages_fall_back():
    assert bm.format_date("garbage") == "" and bm.format_date(None) == ""
    assert "—" in bm.billing_message("cancel_scheduled", "en", None)
    assert bm.normalize_lang("sn") == "en" and bm.normalize_lang("nd") == "en" and bm.normalize_lang(None) == "en"
    assert bm.normalize_lang("FR-ca") == "fr"


def _set_lang(env, lang):
    env.db.biz(1)["features_json"] = {"language": lang}


def test_cancel_scheduled_message_is_localized_and_dated_from_stripe(env):
    activate(env); _set_lang(env, "fr")
    r = svc.cancel_subscription(1)
    assert r["ok"] and r["state"] == "scheduled"
    assert r["message"] == bm.billing_message("cancel_scheduled", "fr", r["access_until"])
    assert bm.format_date(r["access_until"], "fr") in r["message"] and "Vous continuerez" in r["message"]


def test_cancel_in_arabic_and_english_default(env):
    activate(env)
    assert svc.cancel_subscription(1)["message"].startswith("Your subscription has been set to cancel")
    svc.resume_subscription(1); _set_lang(env, "ar")
    assert re.search(r"[؀-ۿ]", svc.cancel_subscription(1)["message"])


def test_repeated_cancellation_reports_already_scheduled_without_calling_stripe_again(env):
    activate(env); _set_lang(env, "de")
    first = svc.cancel_subscription(1)
    mods = len(env.fs.modifies)
    again = svc.cancel_subscription(1)
    assert again["ok"] and again["state"] == "already_scheduled" and len(env.fs.modifies) == mods
    assert again["message"] == bm.billing_message("already_scheduled", "de", first["access_until"])


def test_cancelling_an_already_canceled_subscription(env):
    activate(env)
    env.fs.subs["sub_1"]["status"] = "canceled"
    r = svc.cancel_subscription(1)
    assert r["ok"] and r["state"] == "already_canceled" and r["message"] == bm.MESSAGES["en"]["already_canceled"]
    assert env.fs.modifies == []


def test_cancel_failure_message_when_stripe_errors_and_nothing_changed(env, monkeypatch):
    activate(env); _set_lang(env, "pl")

    def boom(*a, **k): raise RuntimeError("stripe down")
    monkeypatch.setattr(env.fs.Subscription, "modify", staticmethod(boom))
    r = svc.cancel_subscription(1)
    assert r["code"] == "cancel_failed" and r["error"] == r["message"] == bm.MESSAGES["pl"]["cancel_failed"]
    assert env.db.biz(1)["cancel_at_period_end"] in (False, None) and env.db.biz(1)["billing_status"] == "active"


def test_unconfirmed_cancellation_is_never_reported_as_success(env, monkeypatch):
    activate(env)
    monkeypatch.setattr(env.fs.Subscription, "modify",
                        staticmethod(lambda sid, **kw: {**env.fs.subs[sid], "cancel_at_period_end": False}))
    r = svc.cancel_subscription(1)
    assert "ok" not in r and r["message"] == bm.MESSAGES["en"]["cancel_failed"]
    assert not env.db.biz(1).get("cancel_at_period_end")


def test_cancellation_reversal_message_only_after_stripe_confirms(env, monkeypatch):
    activate(env); _set_lang(env, "es")
    svc.cancel_subscription(1)
    r = svc.resume_subscription(1)
    assert r["ok"] and r["state"] == "resumed" and r["message"] == bm.MESSAGES["es"]["cancel_reversed"]
    svc.cancel_subscription(1)
    monkeypatch.setattr(env.fs.Subscription, "modify",
                        staticmethod(lambda sid, **kw: {**env.fs.subs[sid], "cancel_at_period_end": True}))
    r = svc.resume_subscription(1)
    assert "ok" not in r and r["code"] == "resume_failed" and r["message"] == bm.MESSAGES["es"]["cancel_failed"]


def test_resume_when_nothing_scheduled_and_no_subscription(env):
    activate(env)
    r = svc.resume_subscription(1)
    assert r["code"] == "not_cancelling" and r["message"] == bm.MESSAGES["en"]["not_cancelling"]
    env.db.biz(1)["stripe_subscription_id"] = None
    assert svc.cancel_subscription(1)["message"] == bm.MESSAGES["en"]["no_subscription"]


def test_status_message_for_scheduled_cancel_and_webhook_driven_completion(env):
    activate(env); _set_lang(env, "pt")
    svc.cancel_subscription(1)
    s = svc.get_subscription_status(1)
    assert s["state"] == "cancel_scheduled" and s["billing_status"] == "active" and s["tier"] == "starter"
    assert s["status_message"] == bm.billing_message("already_scheduled", "pt", s["current_period_end"])
    # Cancellation completes (Stripe deleted event) → paid access ended, localized completed notice
    env.fs.subs["sub_1"]["status"] = "canceled"
    env.fs.subs["sub_1"]["ended_at"] = NOW
    deliver(env, "evt_done", "customer.subscription.deleted", env.fs.subs["sub_1"])
    s = svc.get_subscription_status(1)
    assert s["state"] == "cancelled" and s["tier"] == "free" and s["status_message"] == bm.MESSAGES["pt"]["cancel_completed"]
    # …and an older cancellation shows the "already canceled" notice instead
    env.db.biz(1)["canceled_at"] = "2020-01-01T00:00:00+00:00"
    assert svc.get_subscription_status(1)["status_message"] == bm.MESSAGES["pt"]["already_canceled"]


def test_messages_use_the_requesting_businesses_language_only(env):
    activate(env); activate(env, bid=2, sub_id="sub_x", cust="cus_2", price="price_gm", tier="growth") if False else None
    _set_lang(env, "de")
    env.db.biz(2)["features_json"] = {"language": "fr"}
    assert bm.business_language(1) == "de" and bm.business_language(2) == "fr"
    assert bm.business_language(999) == "en"


def test_cancel_endpoint_returns_message_and_failure_status_codes(env, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from routes_saas import billing_routes as br
    from core.auth import require_business
    app = fastapi.FastAPI(); app.include_router(br.router)
    app.dependency_overrides[require_business] = lambda: {"business_id": 1}
    c = TestClient(app)
    activate(env); _set_lang(env, "fr")
    ok = c.post("/billing/cancel", json={"confirm": True})
    assert ok.status_code == 200 and ok.json()["message"].startswith("Votre abonnement est configuré")
    again = c.post("/billing/cancel", json={"confirm": True}).json()
    assert again["state"] == "already_scheduled"
    assert c.post("/billing/resume").json()["message"] == bm.MESSAGES["fr"]["cancel_reversed"]
    assert c.post("/billing/resume").status_code == 409
    monkeypatch.setattr(env.fs.Subscription, "modify", staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))))
    bad = c.post("/billing/cancel", json={"confirm": True})
    assert bad.status_code == 502 and bad.json()["detail"] == bm.MESSAGES["fr"]["cancel_failed"]
    assert c.get("/billing/status").json()["status_message"] == ""


def test_cross_tenant_cancel_cannot_touch_another_business(env):
    activate(env)
    env.db.biz(1)["stripe_subscription_id"] = "sub_2"                # tampered mapping to business 2's subscription
    r = svc.cancel_subscription(1)
    assert r["code"] == "mismatch" and env.fs.modifies == [] and r["message"] == bm.MESSAGES["en"]["cancel_failed"]


# ═══ F. public static pages ═══════════════════════════════════════════════

def test_store_page_escape_helper_escapes_quotes_for_attribute_contexts():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parents[2] / "static" / "store.html").read_text(encoding="utf-8")
    m = re.search(r"function esc\(s\)\{(.*?)\}\n", html)
    assert m, "esc() helper missing"
    body = m.group(1)
    for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert ch in body, f"esc() no longer escapes {ch}"
