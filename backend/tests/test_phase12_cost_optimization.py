"""
tests/test_phase12_cost_optimization.py — Phase 12 (AI Cost Optimization)
tests.

Three layers:
  1. Pure unit tests for services/model_routing.py's classify_route() —
     the spec's own worked routing-table examples.
  2. Pure unit tests for services/ai_usage_tracker.py — cost estimation,
     the per-customer sliding-window cap, and should_call_llm()'s
     combined gate.
  3. Pure unit tests for core/plan_guard.py's new
     get_ai_daily_limit()/check_ai_usage_limit() — fail-open behavior and
     the plan-tiered cap itself.
  4. Light end-to-end tests through services.ai.generate_reply() proving
     the P7 call site's safeguard wiring: a rate-limited/quota-capped
     customer still gets the correct deterministic reply (cart still
     works), just without the LLM rephrase attempt, and never leaks
     an API key anywhere in any reply.
"""

import time

import pytest

import services.ai as ai
import services.model_routing as routing
import services.ai_usage_tracker as tracker


# ═════════════════════════════════════════════════════════════════════════
# model_routing.classify_route() — spec's own worked examples
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,intent", [
    ("hi", "help"),
    ("hello there", "help"),
    ("menu", "browse"),
    ("show me the menu", "browse"),
    ("my cart", "cart"),
    ("checkout", "checkout"),
    ("remove burger", "remove"),
])
def test_simple_cases_route_to_none(text, intent):
    assert routing.classify_route(text, intent) == routing.ROUTE_NONE


def test_known_order_status_routes_to_none():
    # _is_status_query recognises this regardless of the coarse _intent()
    # bucket it happens to fall into.
    assert routing.classify_route("what's my order status", "order") == routing.ROUTE_NONE


@pytest.mark.parametrize("text", [
    "this is unacceptable, I want a refund and I'm furious",
    "i've had enough, this keeps happening every single time",
])
def test_customer_complaint_routes_to_full(text):
    assert routing.classify_route(text, "order") == routing.ROUTE_FULL


@pytest.mark.parametrize("text", [
    "that's too expensive for me",
    "what goes well with this?",
    "I'm buying a birthday gift for my sister",
])
def test_complex_recommendation_routes_to_full(text):
    assert routing.classify_route(text, "order") == routing.ROUTE_FULL


def test_complex_multilingual_conversation_routes_to_full():
    long_non_english = "Bonjour, je voudrais savoir si vous livrez dans mon quartier ce soir"
    assert routing.classify_route(long_non_english, "order", customer_language="French") == routing.ROUTE_FULL


def test_short_non_english_greeting_does_not_need_full_llm():
    assert routing.classify_route("hola", "help", customer_language="Spanish") == routing.ROUTE_NONE


@pytest.mark.parametrize("text", [
    "what's your delivery fee for the northern suburbs?",
    "can you tell me if this is gluten free",
    "do you have anything cheaper than this",
])
def test_ambiguous_question_routes_to_cheap(text):
    assert routing.classify_route(text, "order") == routing.ROUTE_CHEAP


@pytest.mark.parametrize("text", [
    "2 burgers please",
    "1x fries",
    "3 chicken wraps",
])
def test_plain_item_orders_never_routed_as_questions(text):
    assert routing.classify_route(text, "order") == routing.ROUTE_NONE


def test_classify_route_never_raises_on_empty_input():
    assert routing.classify_route("", "order") == routing.ROUTE_NONE
    assert routing.classify_route(None, "order") == routing.ROUTE_NONE


def test_model_for_tier_defaults_match_phase10_defaults(monkeypatch):
    monkeypatch.delenv("AI_ROUTING_MODEL_CHEAP", raising=False)
    monkeypatch.delenv("AI_ROUTING_MODEL_FULL", raising=False)
    from services.llm_response import _DEFAULT_MODELS
    assert routing.model_for_tier("cheap", "openai") == _DEFAULT_MODELS["openai"]
    assert routing.model_for_tier("full", "anthropic") == _DEFAULT_MODELS["anthropic"]
    assert routing.model_for_tier("none", "openai") == ""


def test_model_for_tier_honors_env_override(monkeypatch):
    monkeypatch.setenv("AI_ROUTING_MODEL_FULL", "gpt-4o")
    assert routing.model_for_tier("full", "openai") == "gpt-4o"


# ═════════════════════════════════════════════════════════════════════════
# ai_usage_tracker — cost estimation
# ═════════════════════════════════════════════════════════════════════════

def test_estimate_cost_known_model():
    cost = tracker.estimate_cost("gpt-4o-mini", prompt_tokens=1000, completion_tokens=1000)
    assert cost == round(0.00015 + 0.0006, 6)


def test_estimate_cost_unknown_model_never_invents_a_price():
    assert tracker.estimate_cost("some-future-model-nobody-priced-yet", 1000, 1000) == 0.0


def test_estimate_cost_handles_bad_input_gracefully():
    assert tracker.estimate_cost("gpt-4o-mini", "not-a-number", 10) == 0.0


# ═════════════════════════════════════════════════════════════════════════
# ai_usage_tracker — per-customer sliding-window cap
# ═════════════════════════════════════════════════════════════════════════

def test_customer_quota_ok_when_no_history():
    assert tracker.customer_llm_quota_ok({}) is True


def test_customer_quota_blocks_after_limit_reached():
    session = {}
    for _ in range(tracker._CUSTOMER_LLM_MAX_PER_WINDOW):
        assert tracker.customer_llm_quota_ok(session) is True
        session = tracker.record_customer_llm_call(session)
    assert tracker.customer_llm_quota_ok(session) is False


def test_customer_quota_history_is_pruned_outside_window():
    old_ts = time.time() - (tracker._CUSTOMER_LLM_WINDOW_SECONDS + 10)
    session = {"ai_llm_calls": [old_ts] * tracker._CUSTOMER_LLM_MAX_PER_WINDOW}
    # All stale — should read as no history at all.
    assert tracker.customer_llm_quota_ok(session) is True


def test_record_customer_llm_call_never_grows_unbounded():
    session = {}
    for _ in range(tracker._CUSTOMER_LLM_MAX_PER_WINDOW + 20):
        session = tracker.record_customer_llm_call(session)
    assert len(session["ai_llm_calls"]) <= tracker._CUSTOMER_LLM_MAX_PER_WINDOW


def test_customer_quota_ok_handles_non_dict_session():
    assert tracker.customer_llm_quota_ok(None) is True
    assert tracker.customer_llm_quota_ok("not a dict") is True


# ═════════════════════════════════════════════════════════════════════════
# ai_usage_tracker — should_call_llm() combined gate
# ═════════════════════════════════════════════════════════════════════════

def test_should_call_llm_allows_when_both_checks_pass(monkeypatch):
    monkeypatch.setattr(tracker, "business_llm_quota_ok", lambda biz: True)
    allowed, reason = tracker.should_call_llm(1, {})
    assert allowed is True
    assert reason == "ok"


def test_should_call_llm_blocks_on_customer_rate_limit(monkeypatch):
    monkeypatch.setattr(tracker, "business_llm_quota_ok", lambda biz: True)
    session = {"ai_llm_calls": [time.time()] * tracker._CUSTOMER_LLM_MAX_PER_WINDOW}
    allowed, reason = tracker.should_call_llm(1, session)
    assert allowed is False
    assert reason == "customer_rate_limited"


def test_should_call_llm_blocks_on_business_quota(monkeypatch):
    monkeypatch.setattr(tracker, "business_llm_quota_ok", lambda biz: False)
    allowed, reason = tracker.should_call_llm(1, {})
    assert allowed is False
    assert reason == "business_quota_reached"


def test_business_llm_quota_ok_fails_open_on_error(monkeypatch):
    import core.plan_guard as plan_guard

    def _boom(business_id):
        raise RuntimeError("db is down")

    monkeypatch.setattr(plan_guard, "check_ai_usage_limit", _boom)
    assert tracker.business_llm_quota_ok(1) is True


def test_record_llm_usage_never_raises_even_if_db_write_fails(monkeypatch):
    import crud.ai_usage as ai_usage_crud

    def _boom(row):
        raise RuntimeError("no such table")

    monkeypatch.setattr(ai_usage_crud, "insert_ai_usage_log", _boom)
    # Must not raise.
    tracker.record_llm_usage(
        business_id=1, phone="+1555", conversation_id="1:+1555",
        intent="add_to_cart", tier="cheap", provider="openai", model="gpt-4o-mini",
        prompt_tokens=10, completion_tokens=5, latency_ms=120, status="ok",
    )


def test_make_conversation_id_is_stable_and_readable():
    assert tracker.make_conversation_id(1, "+1555") == "1:+1555"


# ═════════════════════════════════════════════════════════════════════════
# core.plan_guard — AI request cap
# ═════════════════════════════════════════════════════════════════════════

def test_get_ai_daily_limit_fails_open_on_error(monkeypatch):
    import core.plan_guard as plan_guard

    def _boom(business_id):
        raise RuntimeError("plan lookup failed")

    monkeypatch.setattr(plan_guard, "_get_business_plan", _boom)
    assert plan_guard.get_ai_daily_limit(1) is None


def test_check_ai_usage_limit_none_when_under_cap(monkeypatch):
    import core.plan_guard as plan_guard

    monkeypatch.setattr(plan_guard, "get_ai_daily_limit", lambda biz: 50)
    monkeypatch.setattr("crud.ai_usage.count_recent_ai_requests", lambda biz, hours=24.0: 10)
    assert plan_guard.check_ai_usage_limit(1) is None


def test_check_ai_usage_limit_returns_error_dict_when_cap_reached(monkeypatch):
    import core.plan_guard as plan_guard

    monkeypatch.setattr(plan_guard, "get_ai_daily_limit", lambda biz: 50)
    monkeypatch.setattr("crud.ai_usage.count_recent_ai_requests", lambda biz, hours=24.0: 50)
    result = plan_guard.check_ai_usage_limit(1)
    assert result is not None
    assert result["error"] == "ai_quota"
    assert result["limit"] == 50
    assert result["current"] == 50


def test_check_ai_usage_limit_unlimited_plan_skips_the_count_query(monkeypatch):
    import core.plan_guard as plan_guard

    monkeypatch.setattr(plan_guard, "get_ai_daily_limit", lambda biz: None)
    assert plan_guard.check_ai_usage_limit(1) is None


def test_check_ai_usage_limit_fails_open_if_usage_count_errors(monkeypatch):
    import core.plan_guard as plan_guard

    monkeypatch.setattr(plan_guard, "get_ai_daily_limit", lambda biz: 50)

    def _boom(biz, hours=24.0):
        raise RuntimeError("table missing")

    monkeypatch.setattr("crud.ai_usage.count_recent_ai_requests", _boom)
    assert plan_guard.check_ai_usage_limit(1) is None


# ═════════════════════════════════════════════════════════════════════════
# services.llm_response — usage/latency metadata capture (Phase 12)
# ═════════════════════════════════════════════════════════════════════════

def test_get_last_reply_meta_reports_disabled_when_feature_off(monkeypatch):
    import services.llm_response as llm

    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "false")
    llm.generate_natural_reply(facts={"product": "Burger"}, fallback_text="fallback")
    meta = llm.get_last_reply_meta()
    assert meta["status"] == "disabled"


def test_get_last_reply_meta_reports_ok_with_tokens_on_success(monkeypatch):
    import services.llm_response as llm

    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("AI_RESPONSE_LLM_PROVIDER", "openai")

    def _fake_call_openai(model, user_content, timeout):
        llm._last_call_meta = {"prompt_tokens": 42, "completion_tokens": 8, "latency_ms": 250}
        return "Nice! Added to your cart."

    monkeypatch.setattr(llm, "_call_openai", _fake_call_openai)
    reply = llm.generate_natural_reply(
        facts={"product": "Burger", "price": 5.0, "quantity": 1, "action": "Added 1 to cart"},
        fallback_text="fallback text",
    )
    assert reply == "Nice! Added to your cart."
    meta = llm.get_last_reply_meta()
    assert meta["status"] == "ok"
    assert meta["prompt_tokens"] == 42
    assert meta["completion_tokens"] == 8
    assert meta["latency_ms"] == 250
    assert meta["provider"] == "openai"


def test_get_last_reply_meta_never_exposes_api_key(monkeypatch):
    import services.llm_response as llm

    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value-should-never-leak")
    monkeypatch.setattr(llm, "_call_openai", lambda model, content, timeout: "hi there")
    llm.generate_natural_reply(facts={"product": "Burger"}, fallback_text="fallback")
    meta = llm.get_last_reply_meta()
    assert "sk-super-secret-value-should-never-leak" not in str(meta)


# ═════════════════════════════════════════════════════════════════════════
# End-to-end through services.ai.generate_reply() — the P7 call site
# ═════════════════════════════════════════════════════════════════════════

RETAIL_BIZ = {"id": 1, "is_service_business": False}
PRODUCTS = [{"id": 1, "name": "Burger", "price": 5.0, "stock": 10}]


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
    monkeypatch.setattr(crud, "get_or_create_customer", lambda phone, biz: {"id": 7})

    from core import plan_guard
    monkeypatch.setattr(plan_guard, "feature_access", lambda feature_key, business_id: {"allowed": True})

    return store


def _reply(text, ai_state):
    return ai.generate_reply(
        message=text, phone="+1555", business_id=1, business_name="Test Shop",
        products=PRODUCTS, business_config=RETAIL_BIZ,
    )


def test_add_to_cart_unaffected_when_llm_response_feature_off(ai_state, monkeypatch):
    monkeypatch.delenv("AI_RESPONSE_LLM_ENABLED", raising=False)
    reply = _reply("2 burgers please", ai_state)
    assert "Burger" in reply
    assert len(ai_state["cart"]) == 1
    assert ai_state["cart"][0]["qty"] == 2


def test_add_to_cart_skips_llm_when_customer_already_rate_limited(ai_state, monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setattr(tracker, "business_llm_quota_ok", lambda biz: True)
    # Pre-fill this customer's session with a maxed-out call history.
    ai_state["state_data"]["session"] = {
        "ai_llm_calls": [time.time()] * tracker._CUSTOMER_LLM_MAX_PER_WINDOW
    }

    called = {"hit": False}

    def _should_not_be_called(*a, **k):
        called["hit"] = True
        return "should never be used"

    monkeypatch.setattr("services.llm_response.generate_natural_reply", _should_not_be_called)

    reply = _reply("1 burger please", ai_state)
    assert called["hit"] is False
    assert "Burger" in reply
    assert len(ai_state["cart"]) == 1


def test_add_to_cart_skips_llm_when_business_over_daily_quota(ai_state, monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setattr(tracker, "business_llm_quota_ok", lambda biz: False)

    called = {"hit": False}

    def _should_not_be_called(*a, **k):
        called["hit"] = True
        return "should never be used"

    monkeypatch.setattr("services.llm_response.generate_natural_reply", _should_not_be_called)

    reply = _reply("1 burger please", ai_state)
    assert called["hit"] is False
    assert "Burger" in reply


def test_add_to_cart_records_usage_and_customer_call_on_success(ai_state, monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setattr(tracker, "business_llm_quota_ok", lambda biz: True)
    monkeypatch.setattr("services.llm_response.generate_natural_reply",
                         lambda **k: "Great pick! Burger is on its way.")
    monkeypatch.setattr("services.llm_response.get_last_reply_meta",
                         lambda: {"status": "ok", "provider": "openai", "model": "gpt-4o-mini",
                                   "prompt_tokens": 30, "completion_tokens": 10, "latency_ms": 90})

    recorded = {}

    def _fake_record(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(tracker, "record_llm_usage", _fake_record)

    reply = _reply("1 burger please", ai_state)
    assert "Burger" in reply
    assert recorded.get("business_id") == 1
    assert recorded.get("tier") == "cheap"
    assert recorded.get("status") == "ok"
    assert recorded.get("prompt_tokens") == 30
    # The customer's own call history should now have exactly one entry.
    assert len(ai_state["state_data"]["session"].get("ai_llm_calls", [])) == 1


def test_add_to_cart_reply_never_contains_an_api_key(ai_state, monkeypatch):
    monkeypatch.setenv("AI_RESPONSE_LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-appear-in-any-reply-abc123")
    monkeypatch.setattr(tracker, "business_llm_quota_ok", lambda biz: True)
    reply = _reply("1 burger please", ai_state)
    assert "sk-should-never-appear-in-any-reply-abc123" not in reply
