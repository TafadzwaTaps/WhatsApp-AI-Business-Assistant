"""
tests/test_phase1_regression_existing_flows.py — Phase 1 regression tests.

Phase 1 touched routes/webhook_routes.py, integrations/whatsapp.py,
services/whatsapp_service.py, services/ai_new.py, and
routes/password_reset_routes.py. None of these edits were meant to change
behavior of the pre-existing order flow, booking flow, or human-handoff
flow — this file is a smoke-test net for those three, asserting the parts
of each that are pure/deterministic still behave exactly as before, plus
targeted tests for the two other Phase 1 items (duplicate AI engines, the
broken integrations/whatsapp.py import).
"""

import time
from datetime import date, timedelta

import crud
from services import booking_service
from workflows import human_handoff
from workflows.order_lifecycle import format_order_status, next_order_stage


# ── Existing order flow ─────────────────────────────────────────────────────

def test_order_status_progression_unchanged():
    """The status pipeline a real order walks through must be untouched."""
    assert next_order_stage("pending") == "awaiting_payment"
    assert next_order_stage("confirmed") == "preparing"
    assert format_order_status("pending") and format_order_status("confirmed")


def test_order_status_display_labels_present_for_known_states():
    for status in ("pending", "confirmed", "preparing", "delivered", "cancelled"):
        label = format_order_status(status)
        assert isinstance(label, str) and label.strip(), f"missing/blank label for {status!r}"


# ── Existing booking flow ───────────────────────────────────────────────────

def test_booking_request_parsing_still_detects_intent_and_time():
    parsed = booking_service.parse_booking_request("Book me for tomorrow at 2pm")
    assert parsed.has_booking_intent is True
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert parsed.date_str == tomorrow
    assert parsed.time_str == "14:00"


def test_booking_request_parsing_ignores_ordinary_chat():
    parsed = booking_service.parse_booking_request("hi, how much is the burger?")
    assert parsed.has_booking_intent is False


def test_check_availability_rejects_slot_outside_working_hours(fake_supabase):
    fake_supabase.results["bookings"] = []
    result = booking_service.check_availability(
        business_id=1,
        booking_date=(date.today() + timedelta(days=2)).isoformat(),
        start_time="22:00",  # outside the 08:00-17:00 default working hours
        duration_hrs=1.0,
        _biz_config={"working_hours_start": "08:00", "working_hours_end": "17:00",
                     "booking_lead_hrs": 1, "features_json": {}},
        _existing_bookings=[],
    )
    assert result["available"] is False


def test_check_availability_accepts_open_slot_with_no_conflicts(fake_supabase):
    fake_supabase.results["bookings"] = []
    far_future = (date.today() + timedelta(days=10)).isoformat()
    result = booking_service.check_availability(
        business_id=1,
        booking_date=far_future,
        start_time="10:00",
        duration_hrs=1.0,
        _biz_config={"working_hours_start": "08:00", "working_hours_end": "17:00",
                     "booking_lead_hrs": 1, "features_json": {}},
        _existing_bookings=[],
    )
    assert result["available"] is True


# ── Existing human handoff flow ─────────────────────────────────────────────

def test_handoff_request_detection_unchanged():
    assert human_handoff.is_handoff_request("I want to speak to a human") is True
    assert human_handoff.is_handoff_request("agent") is True
    assert human_handoff.is_handoff_request("connect me to a human agent") is True
    assert human_handoff.is_handoff_request("what's on the menu today?") is False


def test_handoff_acknowledgement_mentions_business_name():
    msg = human_handoff.handoff_acknowledgement("Test Biz")
    assert "Test Biz" in msg
    assert "human agent" in msg.lower() or "support team" in msg.lower()


def test_handoff_auto_resume_triggers_on_resume_keyword():
    should_resume, _reason = human_handoff.should_auto_resume(
        "resume", {"state": "human_handoff"}
    )
    assert should_resume is True


def test_handoff_auto_resume_does_not_trigger_on_ordinary_message():
    should_resume, _reason = human_handoff.should_auto_resume(
        "is my order ready yet?", {"state": "human_handoff", "handoff_started_at": time.time()}
    )
    assert should_resume is False


# ── Duplicate AI implementations (Phase 1 item #3) ──────────────────────────

def test_ai_new_module_has_no_importers_in_the_live_app():
    """
    services/ai_new.py is a confirmed-dead older snapshot of the AI engine
    (Phase 0 audit: zero importers anywhere in backend/). It was kept
    on disk (not deleted) but must never be silently wired back in. This
    greps the actual source tree so it fails loudly if a future change
    reintroduces an import of it.
    """
    import pathlib
    import re

    backend_root = pathlib.Path(__file__).resolve().parents[1]
    pattern = re.compile(r"\bimport\s+services\.ai_new\b|\bfrom\s+services\.ai_new\s+import\b")

    offenders = []
    for py_file in backend_root.rglob("*.py"):
        if py_file.name == "ai_new.py" or "tests" in py_file.parts:
            continue
        try:
            text = py_file.read_text(errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            offenders.append(str(py_file))

    assert offenders == [], f"services.ai_new must stay unimported, but found: {offenders}"


def test_ai_new_module_is_clearly_documented_as_non_production():
    import services.ai_new as ai_new
    assert ai_new.__doc__ is not None
    assert "NOT THE PRODUCTION AI ENGINE" in ai_new.__doc__


def test_live_ai_engine_is_services_ai():
    """The one production entry point, per Phase 1's requirement — confirms
    services/ai.py still defines generate_reply and is importable cleanly."""
    from services.ai import generate_reply
    assert callable(generate_reply)


# ── integrations/whatsapp.py broken import (Phase 1 item #3, escalated) ────

def test_integrations_whatsapp_module_imports_without_error():
    """
    Regression test for a live crash bug found during Phase 1: this module
    had a module-level `from services.ai_service import generate_reply`
    where services/ai_service.py doesn't exist anywhere in the repo. Since
    Python runs all module-level imports the moment a module is imported
    for ANY reason, this silently broke send_whatsapp_document() — used
    for real PDF invoice delivery (services/_ai_payments.py) — every time
    it was invoked, via a surrounding try/except that swallowed the
    ModuleNotFoundError. This just needs to import cleanly.
    """
    import importlib
    import integrations.whatsapp as whatsapp_module
    importlib.reload(whatsapp_module)
    assert hasattr(whatsapp_module, "send_whatsapp_document")
    assert hasattr(whatsapp_module, "send_whatsapp_message")


def test_send_document_via_whatsapp_service_no_longer_crashes_on_import(monkeypatch):
    """
    services/whatsapp_service.py's send_document() lazily imports
    integrations.whatsapp — confirms that path (the actual one
    _ai_payments.py exercises for PDF invoices) no longer raises
    ModuleNotFoundError before it even gets to send the request.
    """
    from services.whatsapp_service import WhatsAppService

    def _fake_send_whatsapp_document(**kwargs):
        return {"messages": [{"id": "wamid_doc"}]}

    monkeypatch.setattr(
        "integrations.whatsapp.send_whatsapp_document", _fake_send_whatsapp_document
    )
    result = WhatsAppService.send_document(
        phone="263771234567", file_path="/tmp/invoice.pdf",
        access_token="tok", phone_number_id="123",
    )
    assert result == {"messages": [{"id": "wamid_doc"}]}
