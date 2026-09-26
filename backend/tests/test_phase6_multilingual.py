"""
tests/test_phase6_multilingual.py — Phase 6 (Multilingual Conversation) tests.

Covers services/translation_layer.py's Phase 6 additions:
  - translate_incoming_to_english() — offline word/phrase substitution so
    the existing English-only pipeline can understand common non-English
    ordering phrases (the spec's own Shona worked example).
  - detect_and_set_language() — now tries a real statistical detector
    (langdetect) before falling back to the keyword-signal list; Shona/
    Ndebele always use the keyword path since no statistical library
    covers them.
  - _mask_structured_values()/_unmask_structured_values() — the structured-
    value protection wired into maybe_translate() so order IDs, prices,
    dates, times, URLs, and real product names survive translation intact.
"""

import sys
import types

import pytest

import services.translation_layer as tl


# ═════════════════════════════════════════════════════════════════════════
# translate_incoming_to_english()
# ═════════════════════════════════════════════════════════════════════════

def test_spec_worked_example_shona_two_chicken():
    """The spec's own example: "Ndingada huku mbiri." must become
    understandable as "2 x chicken" to the existing English pipeline."""
    result = tl.translate_incoming_to_english("Ndingada huku mbiri.", "sn")
    lowered = result.lower()
    assert "i want" in lowered
    assert "chicken" in lowered
    assert "2" in lowered


@pytest.mark.parametrize("text,lang,expected_substr", [
    ("ngifuna inyama kubili", "nd", "2"),
    ("nataka kuku mbili", "sw", "chicken"),
    ("je veux deux poulet", "fr", "2"),
    ("eu quero dois frango", "pt", "2"),
    ("quiero dos pollo", "es", "2"),
])
def test_incoming_translation_across_supported_languages(text, lang, expected_substr):
    result = tl.translate_incoming_to_english(text, lang).lower()
    assert expected_substr in result


def test_incoming_translation_returns_unchanged_for_english():
    text = "2 chicken burgers please"
    assert tl.translate_incoming_to_english(text, "en") == text


def test_incoming_translation_returns_unchanged_for_unknown_language():
    text = "some text"
    assert tl.translate_incoming_to_english(text, "xx") == text


def test_incoming_translation_leaves_unmatched_words_untouched():
    """A product name typed in Shona that ISN'T in the phrase table must
    pass through unchanged — never invented or dropped."""
    result = tl.translate_incoming_to_english("ndinoda Sadza", "sn")
    assert "Sadza" in result  # untouched, unmatched word preserved
    assert "i want" in result.lower()


def test_incoming_translation_matches_multiword_phrases_before_single_words():
    # French "je veux" must translate as one phrase, not leave a stray "veux".
    result = tl.translate_incoming_to_english("je veux du poulet", "fr").lower()
    assert "veux" not in result
    assert "i want" in result


def test_incoming_translation_reorders_trailing_quantity_to_match_existing_matcher():
    """Shona/Swahili naturally place the quantity AFTER the noun ("chicken
    two"), but utils/fuzzy_matcher.extract_quantity() only recognizes
    "<number> <noun>" order. This must come out reordered, not verbatim."""
    assert tl.translate_incoming_to_english("Ndingada huku mbiri.", "sn") == "i want 2 chicken."
    assert tl.translate_incoming_to_english("nataka kuku mbili", "sw") == "i want 2 chicken"


def test_incoming_translation_does_not_corrupt_already_correct_order():
    """Regression: an earlier version of the trailing-quantity reorder
    matched ANY "<word> <digit>" pair anywhere in the sentence, which
    wrongly turned the already-correct "i want 2 chicken" (French "je veux
    deux poulet") into "i 2 want chicken" by reordering "want 2" instead
    of leaving the sentence alone. Must stay anchored to the true trailing
    pair only."""
    assert tl.translate_incoming_to_english("je veux deux poulet", "fr") == "i want 2 chicken"
    assert tl.translate_incoming_to_english("eu quero dois frango", "pt") == "i want 2 chicken"


def test_incoming_translation_uses_word_boundaries():
    """"un" (French for "1") must not match inside an unrelated longer
    word — word-boundary matching prevents partial-word corruption."""
    result = tl.translate_incoming_to_english("understand", "fr")
    assert result == "understand"  # "un" inside "understand" must not fire


# ═════════════════════════════════════════════════════════════════════════
# detect_and_set_language() — statistical + keyword fallback
# ═════════════════════════════════════════════════════════════════════════

def test_shona_keyword_detection_still_works_without_langdetect(monkeypatch):
    """langdetect has no Shona model at all — this must always go through
    the keyword table, which the spec's own example word ("ndingada") is
    now part of."""
    monkeypatch.setattr(tl, "set_customer_language", lambda *a, **k: True)
    assert tl.detect_and_set_language("Ndingada huku mbiri", "+1", 1) == "sn"


def test_ndebele_keyword_detection():
    result_holder = {}
    def _fake_set(phone, biz, lang):
        result_holder["lang"] = lang
        return True
    tl.set_customer_language = _fake_set
    try:
        assert tl.detect_and_set_language("sawubona, ngifuna ukudla", "+1", 1) == "nd"
        assert result_holder["lang"] == "nd"
    finally:
        import importlib
        importlib.reload(tl)


def test_detect_and_set_language_returns_none_for_plain_english(monkeypatch):
    monkeypatch.setattr(tl, "set_customer_language", lambda *a, **k: True)
    assert tl.detect_and_set_language("hello, can I get a chicken burger", "+1", 1) is None


def test_langdetect_trusted_set_excludes_shona_and_ndebele():
    """Documents the real limitation: even if langdetect somehow returned
    "sn" or "nd" (it never trains on them, so it wouldn't), this codebase
    would not act on it — only the keyword table may claim those two."""
    assert "sn" not in tl._LANGDETECT_TRUSTED
    assert "nd" not in tl._LANGDETECT_TRUSTED


def test_detect_via_langdetect_gracefully_handles_missing_library(monkeypatch):
    """In this environment langdetect isn't installed — confirms the
    ImportError fallback path (the real, currently-exercised path) never
    raises and simply returns None."""
    assert tl._detect_via_langdetect("Bonjour, je voudrais un café") is None


def test_detect_via_langdetect_short_text_skipped():
    assert tl._detect_via_langdetect("hi") is None


def test_detect_via_langdetect_uses_installed_library_when_present(monkeypatch):
    """Simulates langdetect being installed by injecting a fake module,
    without requiring the real (unavailable in this sandbox) package."""
    fake_module = types.ModuleType("langdetect")

    class _FakeFactory:
        seed = 1

    class _FakeException(Exception):
        pass

    def _fake_detect(text):
        return "fr"

    fake_module.detect = _fake_detect
    fake_module.DetectorFactory = _FakeFactory
    fake_module.LangDetectException = _FakeException
    monkeypatch.setitem(sys.modules, "langdetect", fake_module)

    assert tl._detect_via_langdetect("Bonjour, je voudrais un café au lait") == "fr"


def test_detect_via_langdetect_untrusted_result_ignored(monkeypatch):
    """A language langdetect knows about but WaziBot has no reply
    infrastructure for (e.g. German) must not be acted on."""
    fake_module = types.ModuleType("langdetect")

    class _FakeFactory:
        seed = 1

    class _FakeException(Exception):
        pass

    fake_module.detect = lambda text: "de"
    fake_module.DetectorFactory = _FakeFactory
    fake_module.LangDetectException = _FakeException
    monkeypatch.setitem(sys.modules, "langdetect", fake_module)

    assert tl._detect_via_langdetect("Guten Tag, ich möchte bestellen") is None


# ═════════════════════════════════════════════════════════════════════════
# Structured-value protection
# ═════════════════════════════════════════════════════════════════════════

def test_mask_and_unmask_round_trip_preserves_structured_values():
    reply = (
        "Order *ORDER-182* confirmed!\n"
        "Total: $16.00\n"
        "Pickup at 14:30 on 2026-09-25.\n"
        "Details: https://wazibothq.com/order/182\n"
        "Contact: hello@testbiz.com or +263771234567"
    )
    masked, tokens = tl._mask_structured_values(reply)
    assert "ORDER-182" not in masked
    assert "16.00" not in masked
    assert "wazibothq.com" not in masked
    assert "hello@testbiz.com" not in masked

    restored = tl._unmask_structured_values(masked, tokens)
    assert restored == reply


def test_mask_protects_real_product_names():
    reply = "Added *Chicken Burger* x2 to your cart. Total: $16.00"
    masked, tokens = tl._mask_structured_values(reply, product_names=("Chicken Burger",))
    assert "Chicken Burger" not in masked
    restored = tl._unmask_structured_values(masked, tokens)
    assert restored == reply


def test_mask_handles_no_structured_values_gracefully():
    reply = "Hello! How can I help you today?"
    masked, tokens = tl._mask_structured_values(reply)
    assert masked == reply
    assert tokens == {}


def test_maybe_translate_never_corrupts_order_id_or_price(monkeypatch):
    """End-to-end: even with a phrase-table language active, a masked
    order ID / price must come back completely intact."""
    monkeypatch.setattr(tl, "_is_translation_enabled", lambda biz_id: True)
    monkeypatch.setattr(tl, "_get_customer_language", lambda phone, biz: "sn")
    monkeypatch.setattr(tl, "_libretranslate", lambda text, lang: None)  # force phrase-table path

    reply = "Order *ORDER-99* — Total: $24.00\n\n_Type *checkout* when ready._"
    result = tl.maybe_translate(reply, "+1", 1, products=[])
    assert "ORDER-99" in result
    assert "$24.00" in result
    # the phrase table still did its normal word-swap job elsewhere
    assert "bhadhara" in result  # "checkout" -> Shona


def test_maybe_translate_disabled_returns_original(monkeypatch):
    monkeypatch.setattr(tl, "_is_translation_enabled", lambda biz_id: False)
    assert tl.maybe_translate("hello", "+1", 1) == "hello"


def test_maybe_translate_english_customer_returns_original(monkeypatch):
    monkeypatch.setattr(tl, "_is_translation_enabled", lambda biz_id: True)
    monkeypatch.setattr(tl, "_get_customer_language", lambda phone, biz: "en")
    assert tl.maybe_translate("hello", "+1", 1) == "hello"


def test_maybe_translate_never_raises_on_error(monkeypatch):
    monkeypatch.setattr(tl, "_is_translation_enabled", lambda biz_id: (_ for _ in ()).throw(RuntimeError("boom")))
    assert tl.maybe_translate("hello", "+1", 1) == "hello"


# ═════════════════════════════════════════════════════════════════════════
# Spanish phrase-table gap closed
# ═════════════════════════════════════════════════════════════════════════

def test_spanish_now_has_outgoing_phrase_table_entries():
    assert "es" in tl._PHRASE_TABLE
    assert tl._PHRASE_TABLE["es"]["checkout"] == "pagar"


def test_get_customer_language_public_wrapper(monkeypatch):
    monkeypatch.setattr(tl, "_get_customer_language", lambda phone, biz: "fr")
    assert tl.get_customer_language("+1", 1) == "fr"
