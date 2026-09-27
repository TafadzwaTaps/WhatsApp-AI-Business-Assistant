"""
tests/test_phase14_conversation_analytics.py — Phase 14 (Conversation
Analytics) tests.

Four layers:
  1. Pure unit tests for crud/handoff_log.py — best-effort insert/read,
     never raises.
  2. Pure unit tests for services/conversation_analytics.py's individual
     sub-computations (intent stats, top products, response time pairing,
     abandoned carts, handoff metrics, AI-generated sales), each with
     dependencies monkeypatched so no real Supabase call is made.
  3. A test proving _escalate_to_human() (services/ai.py) now writes a
     handoff_log row for every trigger, without changing its existing
     customer-facing behavior.
  4. An endpoint-level test for GET /analytics/conversation-insights —
     the plan-gate branch and the allowed branch.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

import services.conversation_analytics as analytics
import crud.handoff_log as handoff_log


# ═════════════════════════════════════════════════════════════════════════
# crud/handoff_log.py — best-effort, never raises
# ═════════════════════════════════════════════════════════════════════════

def test_insert_handoff_event_never_raises_on_db_error(monkeypatch):
    import core.db as db

    class _BoomTable:
        def insert(self, row):
            raise RuntimeError("table missing")

    monkeypatch.setattr(db.supabase, "table", lambda name: _BoomTable())
    handoff_log.insert_handoff_event(1, "+1555", "Complex request")  # must not raise


def test_get_handoff_events_returns_empty_list_on_error(monkeypatch):
    import core.db as db

    class _BoomTable:
        def select(self, *a, **k):
            raise RuntimeError("table missing")

    monkeypatch.setattr(db.supabase, "table", lambda name: _BoomTable())
    assert handoff_log.get_handoff_events(1) == []


# ═════════════════════════════════════════════════════════════════════════
# _lazy_intent_stats() — reuses Phase 2's classify_intent, no persistence
# ═════════════════════════════════════════════════════════════════════════

def test_lazy_intent_stats_computes_rates_and_top_questions(monkeypatch):
    texts = [
        "asdkjhaskjdh",          # -> unknown, low confidence
        "asdkjhaskjdh",          # duplicate, no "?" -> not counted as a question
        "do you deliver?",       # real question
        "do you deliver?",       # same question again -> counted twice
        "hi",                    # too short to count as a "question" anyway
    ]
    monkeypatch.setattr("crud.messages.get_incoming_texts_since", lambda biz, hours=720.0, limit=1000: texts)

    result = analytics._lazy_intent_stats(1, [], hours=720.0)
    assert result["classified_count"] == 5
    assert result["unknown_intent_rate"] > 0
    assert any(q["text"] == "do you deliver?" and q["count"] == 2 for q in result["top_customer_questions"])


def test_lazy_intent_stats_empty_input(monkeypatch):
    monkeypatch.setattr("crud.messages.get_incoming_texts_since", lambda biz, hours=720.0, limit=1000: [])
    result = analytics._lazy_intent_stats(1, [], hours=720.0)
    assert result["classified_count"] == 0
    assert result["unknown_intent_rate"] == 0.0
    assert result["top_customer_questions"] == []


def test_lazy_intent_stats_never_returns_phone_or_customer_id(monkeypatch):
    texts = ["do you deliver to my area?"]
    monkeypatch.setattr("crud.messages.get_incoming_texts_since", lambda biz, hours=720.0, limit=1000: texts)
    result = analytics._lazy_intent_stats(1, [], hours=720.0)
    dumped = str(result)
    assert "phone" not in dumped.lower()
    assert "customer_id" not in dumped.lower()


# ═════════════════════════════════════════════════════════════════════════
# _top_products_requested() — reuses the existing deterministic matcher
# ═════════════════════════════════════════════════════════════════════════

def test_top_products_requested_tallies_matches(monkeypatch):
    # Two distinct products so an unrelated phrase can't spuriously match
    # the only catalogue entry (the fuzzy matcher's single-candidate
    # behavior with just one product is pre-existing, out-of-scope
    # behavior — not something Phase 14 changes).
    products = [
        {"id": 1, "name": "Chicken Burger", "price": 8.0, "stock": 5},
        {"id": 2, "name": "Veggie Wrap", "price": 6.0, "stock": 5},
    ]
    texts = ["I want a chicken burger", "chicken burger please", "veggie wrap for me"]
    monkeypatch.setattr("crud.messages.get_incoming_texts_since", lambda biz, hours=720.0, limit=1000: texts)

    result = analytics._top_products_requested(1, products, hours=720.0)
    top = {r["name"]: r["count"] for r in result}
    assert top.get("Chicken Burger") == 2
    assert top.get("Veggie Wrap") == 1


def test_top_products_requested_empty_when_no_products(monkeypatch):
    monkeypatch.setattr("crud.messages.get_incoming_texts_since", lambda biz, hours=720.0, limit=1000: ["hi"])
    assert analytics._top_products_requested(1, [], hours=720.0) == []


# ═════════════════════════════════════════════════════════════════════════
# _conversation_count_and_response_time() — pairing logic
# ═════════════════════════════════════════════════════════════════════════

def _iso(dt):
    return dt.isoformat()


def test_response_time_pairs_incoming_to_next_outgoing(monkeypatch):
    t0 = datetime.now(timezone.utc)
    rows = [
        {"customer_id": 1, "direction": "incoming", "created_at": _iso(t0)},
        {"customer_id": 1, "direction": "outgoing", "created_at": _iso(t0 + timedelta(seconds=30))},
        {"customer_id": 2, "direction": "incoming", "created_at": _iso(t0)},
        {"customer_id": 2, "direction": "outgoing", "created_at": _iso(t0 + timedelta(seconds=90))},
    ]
    monkeypatch.setattr("crud.messages.get_messages_since", lambda biz, hours=720.0, limit=4000: rows)

    result = analytics._conversation_count_and_response_time(1, hours=720.0)
    assert result["conversation_count"] == 2
    assert result["average_response_seconds"] == 60.0


def test_response_time_ignores_replies_more_than_a_day_later(monkeypatch):
    t0 = datetime.now(timezone.utc)
    rows = [
        {"customer_id": 1, "direction": "incoming", "created_at": _iso(t0)},
        {"customer_id": 1, "direction": "outgoing", "created_at": _iso(t0 + timedelta(days=3))},
    ]
    monkeypatch.setattr("crud.messages.get_messages_since", lambda biz, hours=720.0, limit=4000: rows)

    result = analytics._conversation_count_and_response_time(1, hours=720.0)
    assert result["average_response_seconds"] is None


def test_response_time_no_data_returns_none_not_error(monkeypatch):
    monkeypatch.setattr("crud.messages.get_messages_since", lambda biz, hours=720.0, limit=4000: [])
    result = analytics._conversation_count_and_response_time(1, hours=720.0)
    assert result["conversation_count"] == 0
    assert result["average_response_seconds"] is None


# ═════════════════════════════════════════════════════════════════════════
# _abandoned_cart_count() — reuses growth.cart_recovery's own definition
# ═════════════════════════════════════════════════════════════════════════

def test_abandoned_cart_count_counts_only_idle_browsing_carts_with_items(monkeypatch):
    from growth.cart_recovery import CART_IDLE_SECONDS
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=CART_IDLE_SECONDS + 100)
    recent = now - timedelta(seconds=10)

    carts = [
        {"items": [{"name": "Burger", "qty": 1, "price": 5}], "updated_at": _iso(old), "state_data": {"state": "browsing"}},
        {"items": [{"name": "Burger", "qty": 1, "price": 5}], "updated_at": _iso(recent), "state_data": {"state": "browsing"}},  # too recent
        {"items": [], "updated_at": _iso(old), "state_data": {"state": "browsing"}},  # empty cart
        {"items": [{"name": "Burger", "qty": 1, "price": 5}], "updated_at": _iso(old), "state_data": {"state": "checkout"}},  # skip state
    ]
    monkeypatch.setattr("crud.customers.get_carts_for_business", lambda biz: carts)

    assert analytics._abandoned_cart_count(1) == 1


def test_abandoned_cart_count_returns_zero_on_error(monkeypatch):
    def _boom(biz):
        raise RuntimeError("db down")
    monkeypatch.setattr("crud.customers.get_carts_for_business", _boom)
    assert analytics._abandoned_cart_count(1) == 0


# ═════════════════════════════════════════════════════════════════════════
# _handoff_metrics() — rate + failure-reason classification
# ═════════════════════════════════════════════════════════════════════════

def test_handoff_metrics_computes_rate_and_failed_conversations(monkeypatch):
    events = [
        {"reason": "Complex request"},
        {"reason": "Repeated misunderstanding"},
        {"reason": "Refund/dispute request"},
    ]
    monkeypatch.setattr("crud.handoff_log.get_handoff_events", lambda biz, hours=720.0: events)

    result = analytics._handoff_metrics(1, hours=720.0, conversation_count=10)
    assert result["human_handoff_count"] == 3
    assert result["human_handoff_rate"] == 0.3
    assert result["failed_conversations"] == 2  # only the two AI-failure reasons
    assert result["handoff_reason_breakdown"]["Refund/dispute request"] == 1


def test_handoff_metrics_zero_conversations_avoids_division_by_zero(monkeypatch):
    monkeypatch.setattr("crud.handoff_log.get_handoff_events", lambda biz, hours=720.0: [])
    result = analytics._handoff_metrics(1, hours=720.0, conversation_count=0)
    assert result["human_handoff_rate"] == 0.0


# ═════════════════════════════════════════════════════════════════════════
# get_conversation_analytics() — full assembly, cache behavior
# ═════════════════════════════════════════════════════════════════════════

def test_get_conversation_analytics_assembles_all_fields_and_never_raises(monkeypatch):
    analytics._cache.clear()
    monkeypatch.setattr("crud.get_products", lambda biz: [], raising=False)
    import crud
    monkeypatch.setattr(crud, "get_products", lambda biz: [])
    monkeypatch.setattr("crud.messages.get_incoming_texts_since", lambda biz, hours=720.0, limit=1000: [])
    monkeypatch.setattr("crud.messages.get_messages_since", lambda biz, hours=720.0, limit=4000: [])
    monkeypatch.setattr("crud.customers.get_carts_for_business", lambda biz: [])
    monkeypatch.setattr("crud.handoff_log.get_handoff_events", lambda biz, hours=720.0: [])
    monkeypatch.setattr("crud.ai_usage.get_ai_usage_summary",
                         lambda biz, hours=720.0: {"requests": 0, "total_tokens": 0, "estimated_cost": 0.0})
    monkeypatch.setattr("crud.analytics.get_business_stats",
                         lambda biz: {"ai_handled": 0, "human_handled": 0})

    result = analytics.get_conversation_analytics(1, hours=720.0)
    for key in (
        "conversation_count", "human_handoff_rate", "unknown_intent_rate",
        "low_confidence_rate", "top_customer_questions", "top_products_requested",
        "abandoned_carts", "ai_generated_sales_count", "booking_conversations",
        "failed_conversations", "average_response_seconds", "ai_cost", "insights",
    ):
        assert key in result


def test_get_conversation_analytics_uses_cache_on_second_call(monkeypatch):
    analytics._cache.clear()
    import crud
    monkeypatch.setattr(crud, "get_products", lambda biz: [])
    call_count = {"n": 0}

    def _counting_get_messages_since(biz, hours=720.0, limit=4000):
        call_count["n"] += 1
        return []

    monkeypatch.setattr("crud.messages.get_incoming_texts_since", lambda biz, hours=720.0, limit=1000: [])
    monkeypatch.setattr("crud.messages.get_messages_since", _counting_get_messages_since)
    monkeypatch.setattr("crud.customers.get_carts_for_business", lambda biz: [])
    monkeypatch.setattr("crud.handoff_log.get_handoff_events", lambda biz, hours=720.0: [])
    monkeypatch.setattr("crud.ai_usage.get_ai_usage_summary",
                         lambda biz, hours=720.0: {"requests": 0, "total_tokens": 0, "estimated_cost": 0.0})
    monkeypatch.setattr("crud.analytics.get_business_stats",
                         lambda biz: {"ai_handled": 0, "human_handled": 0})

    analytics.get_conversation_analytics(1, hours=720.0)
    analytics.get_conversation_analytics(1, hours=720.0)
    assert call_count["n"] == 1  # second call served from cache


def test_build_insights_matches_spec_wording_shape():
    intent_stats = {"top_customer_questions": [{"text": "do you deliver?", "count": 5}]}
    top_products = [{"name": "Chicken Burger", "count": 3}]
    handoff_stats = {"failed_conversations": 8, "human_handoff_rate": 0.042}

    lines = analytics._build_insights(intent_stats, top_products, handoff_stats)
    joined = "\n".join(lines)
    assert 'Customers frequently ask:\n"do you deliver?"' in joined
    assert 'Customers frequently request:\n"Chicken Burger"' in joined
    assert "AI failed to understand:\n8 conversations" in joined
    assert "Human handoff:\n4.2%" in joined


# ═════════════════════════════════════════════════════════════════════════
# services.ai._escalate_to_human() now writes a durable handoff_log row
# ═════════════════════════════════════════════════════════════════════════

def test_escalate_to_human_writes_handoff_log_without_changing_behavior(monkeypatch):
    import services.ai as ai

    store = {"state_data": {"state": "browsing", "session": {}}}
    monkeypatch.setattr(ai, "_set_human_handoff", lambda phone, biz: store["state_data"].update({"state": "human_handoff"}))
    monkeypatch.setattr(ai, "_write_state_data", lambda phone, biz, patch: store["state_data"].update(patch))
    monkeypatch.setattr(ai, "_get_memory", lambda phone, biz: {"customer_name": "Test"})
    monkeypatch.setattr(ai, "_get_active_order", lambda phone, biz: None)

    import crud
    monkeypatch.setattr(crud, "get_or_create_customer", lambda phone, biz: {"id": 1})

    recorded = {}

    def _fake_insert(business_id, phone, reason):
        recorded["business_id"] = business_id
        recorded["phone"] = phone
        recorded["reason"] = reason

    monkeypatch.setattr("crud.handoff_log.insert_handoff_event", _fake_insert)

    ticket = ai._escalate_to_human(
        phone="+1555", business_id=1, business_name="Test Shop", text="help",
        trigger_reason="Complex request", issue="test issue", ai_summary="test summary",
    )
    assert isinstance(ticket, str)
    assert recorded.get("business_id") == 1
    assert recorded.get("reason") == "Complex request"
    assert store["state_data"]["state"] == "human_handoff"


def test_escalate_to_human_unaffected_by_handoff_log_failure(monkeypatch):
    import services.ai as ai

    store = {"state_data": {"state": "browsing", "session": {}}}
    monkeypatch.setattr(ai, "_set_human_handoff", lambda phone, biz: store["state_data"].update({"state": "human_handoff"}))
    monkeypatch.setattr(ai, "_write_state_data", lambda phone, biz, patch: store["state_data"].update(patch))
    monkeypatch.setattr(ai, "_get_memory", lambda phone, biz: {"customer_name": "Test"})
    monkeypatch.setattr(ai, "_get_active_order", lambda phone, biz: None)

    import crud
    monkeypatch.setattr(crud, "get_or_create_customer", lambda phone, biz: {"id": 1})

    def _boom(business_id, phone, reason):
        raise RuntimeError("table missing")

    monkeypatch.setattr("crud.handoff_log.insert_handoff_event", _boom)

    # Must not raise, and the customer-facing side (ticket + state) still works.
    ticket = ai._escalate_to_human(
        phone="+1555", business_id=1, business_name="Test Shop", text="help",
        trigger_reason="Complex request", issue="test issue", ai_summary="test summary",
    )
    assert isinstance(ticket, str)
    assert store["state_data"]["state"] == "human_handoff"


# ═════════════════════════════════════════════════════════════════════════
# Endpoint: GET /analytics/conversation-insights
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def _auth_header(monkeypatch, business_id=1):
    import core.auth as auth

    def _fake_require_business():
        return {"business_id": business_id, "role": "owner"}

    import routes.business_routes as br
    monkeypatch.setattr(br, "require_business", _fake_require_business)


def test_endpoint_returns_not_allowed_below_plan(monkeypatch, client):
    import routes.business_routes as br
    import core.plan_guard as plan_guard

    def _fake_dep():
        return {"business_id": 1}

    # feature_access is imported locally inside the route body
    # (`from core.plan_guard import feature_access`), so the source
    # module must be patched, not routes.business_routes's own namespace
    # — the same local-import gotcha documented elsewhere in this project.
    monkeypatch.setattr(plan_guard, "feature_access", lambda feature, biz: {"allowed": False, "required_tier": "GROWTH", "upgrade_url": "/pricing"})
    main_app = client.app
    main_app.dependency_overrides[br.require_business] = _fake_dep

    resp = client.get("/analytics/conversation-insights")
    main_app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is False
    assert body["required_tier"] == "GROWTH"


def test_endpoint_returns_data_when_allowed(monkeypatch, client):
    import routes.business_routes as br
    import core.plan_guard as plan_guard

    def _fake_dep():
        return {"business_id": 1}

    monkeypatch.setattr(plan_guard, "feature_access", lambda feature, biz: {"allowed": True})
    monkeypatch.setattr(
        "services.conversation_analytics.get_conversation_analytics",
        lambda biz, hours=720.0: {"conversation_count": 5, "insights": []},
    )
    main_app = client.app
    main_app.dependency_overrides[br.require_business] = _fake_dep

    resp = client.get("/analytics/conversation-insights?days=30")
    main_app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is True
    assert body["conversation_count"] == 5
