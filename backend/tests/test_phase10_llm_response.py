"""
tests/test_phase10_llm_response.py — Phase 10 (AI-Generated Response Layer) tests.

Two layers, same pattern as Phases 4-9's tests:
  1. Pure unit tests for services/llm_response.py — the safety-validation
     logic, the facts-to-prompt formatting, and generate_natural_reply()'s
     off/unconfigured/error fallback behavior — with the actual network
     call monkeypatched (no real API key or network access needed/used).
  2. End-to-end tests through services.ai.generate_reply() confirming:
       - with the feature OFF (the shipped default), the add-to-cart
         confirmation is BYTE-IDENTICAL to pre-Phase-10 behavior;
       - with it ON and a mocked LLM, the confirmation is replaced with
         the (validated) LLM phrasing;
       - a mocked LLM that invents an unlisted price is rejected and the
         deterministic fallback is used instead (the safety-validation
         step actually doing its job);
       - the cart listing itself is NEVER touched by the LLM, regardless
         of what the mocked LLM returns.
"""

import pytest

import services.ai as ai
import services.llm_response as llm


# ═════════════════════════════════════════════════════════════════════════
# is_enabled() / generate_natural_reply() gating
# ═════════════════════════════════════════════════════════════════════════

def test_disabled_by_default_returns_fallback_unchanged(monkeypatch):
    monkeypatch.delenv("AI_RESPONSE_LLM_ENABLED", raising=False)
    result = llm.generate_natural_reply({"product": "Burger"}, "FALLBACK TEXT")
    assert result == "FALLBACK TEXT"


def test_explicitly_disabled_returns_fallback_unchanged(monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "false")
    result = llm.generate_natural_reply({"product": "Burger"}, "FALLBACK TEXT")
    assert result == "FALLBACK TEXT"


def test_enabled_but_unknown_provider_returns_fallback(monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "not_a_real_provider")
    result = llm.generate_natural_reply({"product": "Burger"}, "FALLBACK TEXT")
    assert result == "FALLBACK TEXT"


def test_enabled_but_no_api_key_returns_fallback(monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = llm.generate_natural_reply({"product": "Burger"}, "FALLBACK TEXT")
    assert result == "FALLBACK TEXT"


def test_enabled_with_mocked_provider_returns_llm_text(monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm, "_call_openai", lambda model, content, timeout: "Done 😊 Added to your cart.")
    result = llm.generate_natural_reply({"product": "Burger", "price": 5.0}, "FALLBACK TEXT")
    assert result == "Done 😊 Added to your cart."


def test_provider_call_exception_falls_back(monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")

    def _boom(model, content, timeout):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(llm, "_call_openai", _boom)
    result = llm.generate_natural_reply({"product": "Burger"}, "FALLBACK TEXT")
    assert result == "FALLBACK TEXT"


def test_empty_facts_returns_fallback_without_calling_provider(monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")

    def _boom(*a, **k):
        raise AssertionError("provider must not be called for empty facts")

    monkeypatch.setattr(llm, "_call_openai", _boom)
    result = llm.generate_natural_reply({}, "FALLBACK TEXT")
    assert result == "FALLBACK TEXT"


# ═════════════════════════════════════════════════════════════════════════
# _validate_llm_output() — the safety-validation step
# ═════════════════════════════════════════════════════════════════════════

def test_validate_accepts_reply_using_only_given_price():
    facts = {"product": "Chicken Burger", "price": 8.0, "quantity": 2}
    out = llm._validate_llm_output(
        "Done 😊 I've added 2 Chicken Burgers to your cart. That's $16.", facts,
    )
    assert out is not None and "$16" in out


def test_validate_rejects_invented_price():
    facts = {"product": "Chicken Burger", "price": 8.0, "quantity": 1}
    out = llm._validate_llm_output(
        "Added! That'll be $99, a real steal.", facts,
    )
    assert out is None


def test_validate_rejects_empty_reply():
    assert llm._validate_llm_output("", {"price": 8.0}) is None
    assert llm._validate_llm_output("   ", {"price": 8.0}) is None


def test_validate_rejects_too_long_reply():
    facts = {"price": 8.0}
    long_text = "This is a very long reply. " * 40
    assert llm._validate_llm_output(long_text, facts) is None


def test_validate_rejects_prompt_leak_language():
    facts = {"price": 8.0}
    assert llm._validate_llm_output("As an AI language model, I added your item.", facts) is None
    assert llm._validate_llm_output("My instructions are to phrase this nicely.", facts) is None


def test_validate_accepts_price_times_quantity_as_allowed_total():
    facts = {"price": 5.0, "quantity": 3}
    out = llm._validate_llm_output("Added! Your total for this item is $15.", facts)
    assert out is not None


def test_validate_strips_surrounding_quotes():
    out = llm._validate_llm_output('"Added to your cart 😊"', {})
    assert out == "Added to your cart 😊"


# ═════════════════════════════════════════════════════════════════════════
# _facts_to_prompt_block()
# ═════════════════════════════════════════════════════════════════════════

def test_facts_to_prompt_block_matches_spec_shape():
    block = llm._facts_to_prompt_block({
        "product": "Chicken Burger", "price": 8.0, "stock": 12,
        "action": "Added 2 to cart", "customer_language": "English",
    })
    assert "PRODUCT:\nChicken Burger" in block
    assert "PRICE:\n8.0" in block
    assert "STOCK:\n12" in block
    assert "ACTION:\nAdded 2 to cart" in block
    assert "CUSTOMER LANGUAGE:\nEnglish" in block


def test_facts_to_prompt_block_omits_none_values():
    block = llm._facts_to_prompt_block({"product": "Haircut", "stock": None})
    assert "STOCK" not in block


# ═════════════════════════════════════════════════════════════════════════
# End-to-end through services.ai.generate_reply()
# ═════════════════════════════════════════════════════════════════════════

RETAIL_BIZ = {"id": 1, "is_service_business": False}
PRODUCTS = [{"id": 1, "name": "Chicken Burger", "price": 8.0, "stock": 12}]


@pytest.fixture
def ai_state(monkeypatch):
    store = {"cart": [], "state_data": {"state": "browsing", "session": {}}}

    monkeypatch.setattr(ai, "_get_state", lambda phone, biz: store["state_data"].get("state", "browsing"))
    monkeypatch.setattr(ai, "_get_session", lambda phone, biz: store["state_data"].get("session") or {})
    monkeypatch.setattr(ai, "_read_state_data", lambda phone, biz: store["state_data"])
    monkeypatch.setattr(ai, "_write_state_data", lambda phone, biz, patch: store["state_data"].update(patch))
    monkeypatch.setattr(ai, "_reset_state", lambda phone, biz: store["state_data"].update({"state": "browsing", "session": {}}))
    monkeypatch.setattr(ai, "_load_cart", lambda phone, biz: store["cart"])
    monkeypatch.setattr(ai, "_save_cart", lambda phone, biz, cart: store.update(cart=cart))

    import crud
    monkeypatch.setattr(crud, "get_business_by_id", lambda biz_id: RETAIL_BIZ)
    monkeypatch.setattr(crud, "get_user_memory", lambda phone, biz: None)
    monkeypatch.setattr(crud, "save_user_memory", lambda *a, **k: None)
    monkeypatch.setattr(crud, "get_product_by_name",
                         lambda biz, name: next((p for p in PRODUCTS if p["name"] == name), None))

    from core import plan_guard
    monkeypatch.setattr(plan_guard, "feature_access", lambda feature_key, business_id: {"allowed": True})

    from services import translation_layer
    monkeypatch.setattr(translation_layer, "get_customer_language", lambda phone, biz: "en")

    monkeypatch.delenv("AI_RESPONSE_LLM_ENABLED", raising=False)
    return store


def _reply(text, ai_state):
    return ai.generate_reply(
        message=text, phone="+1555", business_id=1, business_name="Test Shop",
        products=PRODUCTS, business_config=RETAIL_BIZ,
    )


def test_off_by_default_add_to_cart_confirmation_unchanged(ai_state):
    reply = _reply("2 chicken burger", ai_state)
    assert reply.startswith("👍 Nice choice! Added *Chicken Burger*")
    assert "🛒" in reply  # the deterministic cart block is still present


def test_enabled_with_mocked_llm_replaces_confirmation_line(ai_state, monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")

    import services.llm_response as llm_mod
    monkeypatch.setattr(
        llm_mod, "_call_openai",
        lambda model, content, timeout: "Done 😊 I've added 2 Chicken Burgers to your cart. That's $16.",
    )

    reply = _reply("2 chicken burger", ai_state)
    assert "Done 😊 I've added 2 Chicken Burgers to your cart. That's $16." in reply
    # the cart listing block underneath is still the real, deterministic one
    assert "🛒" in reply and "Chicken Burger" in reply.split("🛒")[1]


def test_llm_inventing_a_price_is_rejected_and_falls_back(ai_state, monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")

    import services.llm_response as llm_mod
    monkeypatch.setattr(
        llm_mod, "_call_openai",
        lambda model, content, timeout: "Added! Today only, it's just $1 — amazing deal!",
    )

    reply = _reply("2 chicken burger", ai_state)
    # the invented $1 price must never reach the customer
    assert "$1 —" not in reply
    assert reply.startswith("👍 Nice choice! Added *Chicken Burger*")


def test_llm_never_asked_to_phrase_the_cart_listing_itself(ai_state, monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")

    captured = {}

    def _capture(model, content, timeout):
        captured["content"] = content
        return "Done 😊"

    import services.llm_response as llm_mod
    monkeypatch.setattr(llm_mod, "_call_openai", _capture)

    _reply("2 chicken burger", ai_state)
    # the prompt given to the LLM carries only the facts, never a
    # pre-rendered cart listing with per-item lines/emoji formatting
    assert "🛒" not in captured["content"]
    assert "PRODUCT" in captured["content"]
    assert "PRICE" in captured["content"]
