"""
tests/test_translation_service.py

Covers services/translation_service.py (the new Argos-Translate-backed
service for the Site Generator's dynamic business content) and the
site_generator.py integration points that feed it.

Argos Translate itself (and its downloaded language models) is an OPTIONAL
dependency not installed in this test environment by design — see
translation_service.py's module docstring and requirements-translation.txt
for why. These tests therefore exercise:
  1. The service's behavior with Argos genuinely unavailable (the real
     state of this CI/test environment) — fallback-to-original-text,
     protected-content skipping, caching, tenant isolation.
  2. The service's behavior with Argos mocked as available, to verify the
     translation call is actually wired up correctly and cached — without
     requiring a real multi-hundred-MB model download in CI.
  3. site_generator.py's dynamic-content registry (_register_dynamic_text /
     _dyn_i18n_collect_and_translate) and its HTML output.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_translation_service_state():
    """Each test gets a clean module (fresh cache, fresh "have we checked
    for Argos yet" state) — these are process-level globals by design
    (see the module docstring on caching), so tests must not leak into
    each other."""
    sys.modules.pop("services.translation_service", None)
    import services.translation_service as ts
    yield ts
    sys.modules.pop("services.translation_service", None)


# ── Fallback behavior (Argos genuinely not installed in this environment) ──

def test_translate_text_falls_back_to_original_when_argos_unavailable(_reset_translation_service_state):
    ts = _reset_translation_service_state
    out = ts.translate_text("We are open every day from 9am to 6pm.", target_lang="fr", business_id=1)
    # Never blanks, never raises, never returns None/undefined — falls back
    # to the original text exactly (per the spec's fallback requirement).
    assert out == "We are open every day from 9am to 6pm."


def test_translate_text_same_language_is_a_no_op(_reset_translation_service_state):
    ts = _reset_translation_service_state
    assert ts.translate_text("Hello", target_lang="en", source_lang="en", business_id=1) == "Hello"


def test_translate_text_unsupported_language_is_a_no_op(_reset_translation_service_state):
    ts = _reset_translation_service_state
    assert ts.translate_text("Hello", target_lang="zz", business_id=1) == "Hello"


def test_translate_text_never_raises_on_empty_input(_reset_translation_service_state):
    ts = _reset_translation_service_state
    assert ts.translate_text("", target_lang="fr", business_id=1) == ""
    assert ts.translate_text("   ", target_lang="fr", business_id=1) == "   "


# ── Protected content (never corrupt these — spec items 6/16) ──────────────

@pytest.mark.parametrize("protected", [
    "https://wazibothq.com/site/kudzai-kitchen",
    "orders@kudzai-kitchen.com",
    "+263771234567",
    "$49.99",
    "12345",
])
def test_translate_text_never_translates_protected_content(_reset_translation_service_state, protected):
    ts = _reset_translation_service_state
    # Even if Argos WERE available, this must short-circuit before ever
    # calling it — proven by forcing a translated-looking stand-in and
    # checking it's never invoked.
    ts._argos_checked = True
    ts._argos_available = True
    ts._argos_installed_pairs = {("en", "fr")}
    called = {"n": 0}

    def _boom(text, s, t):
        called["n"] += 1
        return "SHOULD NOT BE CALLED"

    ts._argos_translate_raw = _boom
    out = ts.translate_text(protected, target_lang="fr", business_id=1)
    assert out == protected
    assert called["n"] == 0


# ── Argos mocked as available — verifies the wiring + caching ──────────────

def test_translate_text_uses_argos_when_available_and_caches_result(_reset_translation_service_state, monkeypatch):
    ts = _reset_translation_service_state
    ts._argos_checked = True
    ts._argos_available = True
    ts._argos_installed_pairs = {("en", "fr")}

    calls = []

    def _fake_translate_raw(text, source_lang, target_lang):
        calls.append((text, source_lang, target_lang))
        return f"[FR]{text}"

    monkeypatch.setattr(ts, "_argos_translate_raw", _fake_translate_raw)

    out1 = ts.translate_text("Fresh bread daily.", target_lang="fr", business_id=1)
    out2 = ts.translate_text("Fresh bread daily.", target_lang="fr", business_id=1)

    assert out1 == "[FR]Fresh bread daily."
    assert out2 == "[FR]Fresh bread daily."
    # Second call must be served from cache, not re-invoke the translator —
    # "must be cached/reused, never re-downloaded/re-translated per request".
    assert len(calls) == 1


def test_translate_text_cache_is_isolated_per_business(_reset_translation_service_state, monkeypatch):
    ts = _reset_translation_service_state
    ts._argos_checked = True
    ts._argos_available = True
    ts._argos_installed_pairs = {("en", "fr")}

    calls = []
    monkeypatch.setattr(ts, "_argos_translate_raw", lambda t, s, tg: calls.append(1) or f"[FR]{t}")

    ts.translate_text("Welcome to our shop.", target_lang="fr", business_id=1)
    ts.translate_text("Welcome to our shop.", target_lang="fr", business_id=2)
    # Same text, same language pair, DIFFERENT business — must not share a
    # cache entry (spec: "one business's custom content must not leak into
    # another's translations"), so the translator is invoked twice.
    assert len(calls) == 2


def test_translate_for_all_languages_includes_source_unchanged(_reset_translation_service_state, monkeypatch):
    ts = _reset_translation_service_state
    monkeypatch.setattr(ts, "translate_text", lambda text, target_lang, source_lang="en", business_id=None: f"[{target_lang}]{text}")
    out = ts.translate_for_all_languages("Hello", source_lang="en", business_id=1)
    assert out["en"] == "Hello"
    assert out["fr"] == "[fr]Hello"
    assert out["ar"] == "[ar]Hello"
    assert set(out.keys()) == set(ts.SUPPORTED_SITE_LANGS)


def test_clear_cache_for_business_only_drops_that_businesss_entries(_reset_translation_service_state, monkeypatch):
    ts = _reset_translation_service_state
    ts._argos_checked = True
    ts._argos_available = True
    ts._argos_installed_pairs = {("en", "fr")}
    monkeypatch.setattr(ts, "_argos_translate_raw", lambda t, s, tg: f"[FR]{t}")

    ts.translate_text("A description.", target_lang="fr", business_id=1)
    ts.translate_text("A description.", target_lang="fr", business_id=2)
    dropped = ts.clear_cache_for_business(1)
    assert dropped == 1
    assert any(k.startswith("2:") for k in ts._cache)
    assert not any(k.startswith("1:") for k in ts._cache)


def test_argos_unavailable_is_detected_gracefully_without_the_package_installed(_reset_translation_service_state):
    """The real, honest state of this test environment: argostranslate is
    NOT installed by default (it's an optional dependency — see
    requirements-translation.txt). This must not raise at import time, at
    app startup, or anywhere else — whether that's because the package
    itself is missing, or because it's installed but has zero language
    models downloaded (also a valid, must-not-crash state: an operator who
    installed the optional dependency but hasn't run the provisioning
    script yet)."""
    ts = _reset_translation_service_state
    result = ts._ensure_argos_loaded()
    assert result in (True, False)  # must not raise, whichever state this box is in
    # Either way, with no models actually installed, there is no usable
    # (source, target) pair — translate_text() must still behave correctly,
    # falling back to the original text rather than raising or blanking it.
    assert ts._argos_installed_pairs == set()
    assert ts.translate_text("Something.", target_lang="de", business_id=1) == "Something."


# ── site_generator.py integration ───────────────────────────────────────

def test_site_generator_dynamic_registry_noop_outside_bracket():
    import services.site_generator as sg
    # Calling _register_dynamic_text without _dyn_i18n_start() first (e.g.
    # a stray call from code that isn't part of a real page render) must
    # never raise and must not silently accumulate state forever.
    sg._dyn_i18n_ctx.set(None)
    sg._register_dynamic_text("some_key", "Some text", business_id=1)  # no-op, no raise
    assert sg._dyn_i18n_collect_and_translate() == {}


def test_site_generator_dynamic_registry_collects_and_translates(monkeypatch):
    import services.site_generator as sg
    import services.translation_service as ts

    monkeypatch.setattr(
        ts, "translate_for_all_languages",
        lambda text, source_lang="en", business_id=None, target_langs=None: {"en": text, "fr": f"[FR]{text}"},
    )

    sg._dyn_i18n_start()
    sg._register_dynamic_text("about_desc_1", "We sell great coffee.", business_id=1)
    sg._register_dynamic_text("prod_desc_9", "Locally roasted beans.", business_id=1)
    out = sg._dyn_i18n_collect_and_translate()

    assert out["fr"]["about_desc_1"] == "[FR]We sell great coffee."
    assert out["fr"]["prod_desc_9"] == "[FR]Locally roasted beans."
    # The registry resets after collection — a second collect on the same
    # (now-cleared) context must come back empty, not repeat stale entries.
    assert sg._dyn_i18n_collect_and_translate() == {}


def test_site_generator_dynamic_registry_failure_does_not_break_page_render(monkeypatch):
    """If the translation layer throws for any reason, site generation must
    still complete — the page renders correctly in its original language
    rather than 500ing (spec: never blank the site on translation failure)."""
    import services.site_generator as sg

    def _boom(*a, **kw):
        raise RuntimeError("simulated translation failure")

    monkeypatch.setattr("services.translation_service.translate_for_all_languages", _boom)
    sg._dyn_i18n_start()
    sg._register_dynamic_text("k", "text", business_id=1)
    out = sg._dyn_i18n_collect_and_translate()
    assert out == {}


def test_generate_site_html_embeds_dynamic_translations_and_new_languages(monkeypatch):
    import services.site_generator as sg

    fake_biz = {
        "id": 77, "name": "Test Cafe", "category": "Cafe",
        "tagline": "Great coffee", "currency_symbol": "$",
        "use_shared_number": True, "contact_phone": "", "features_json": {},
        "is_service_business": False,
    }
    fake_products = [
        {"id": 5, "name": "Latte", "price": 3.5,
         "description": "Smooth espresso with steamed milk.",
         "image_url": "", "category": "Drinks", "stock": 10},
    ]
    monkeypatch.setattr(sg, "_get_business_and_products", lambda slug: (fake_biz, fake_products))
    monkeypatch.setattr(sg, "_get_reviews", lambda biz_id: [])

    html = sg.generate_site_html("test-cafe")

    # Static UI chrome: German + Arabic are now offered (spec test matrix +
    # non-Latin-script requirement).
    assert '"de"' in html or "value=\"de\"" in html
    assert '"ar"' in html or "value=\"ar\"" in html

    # Dynamic business content is wired to the translation registry, not
    # left as plain untranslated text with no hook for the switcher.
    assert 'data-i18n-dyn="about_desc_77"' in html
    assert 'data-i18n-dyn="prod_desc_5"' in html

    # Accessibility labels are now translatable, not hardcoded.
    assert 'data-i18n-aria="chat_whatsapp"' in html
    assert 'data-i18n-title="order_on_whatsapp"' in html

    # Dynamic JS strings (checkout flow) go through the shared dictionary
    # rather than being hardcoded — "Redirecting to Stripe…" may still
    # appear as _wzT()'s English FALLBACK argument (used only if a
    # translation is missing), but the actual assignment must read from
    # the dictionary, not the literal string, at the point of use.
    assert "_wzT(" in html
    assert "btn.textContent = _wzT('redirecting_stripe'" in html
    assert "btn.textContent = 'Redirecting to Stripe" not in html

    # Product NAME must never be registered for translation (protected
    # content) — only its description.
    assert "prod_desc_5" in html
    assert "prod_name_5" not in html

    # RTL support wired up for Arabic.
    assert "WZ_I18N_RTL" in html
