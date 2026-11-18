"""Phase 4, stages 2-3: O1 config propagation, C1 voice gate, N1 non-blocking calls, P1x log hygiene.

C1/N1 are structural (source-level) checks: the webhook handler has no end-to-end harness yet
(listed as a test gap in docs/ai-development/phase4-plan.md)."""
import logging
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf8")


def test_phone_numbers_masked_in_ai_logs():
    from services import ai
    rec = logging.LogRecord("x", logging.INFO, "", 0, "phone=%s biz=%s", ("263771234567", 1), None)
    assert ai._MaskPhones().filter(rec) is True
    out = rec.getMessage()
    assert "263771234" not in out
    assert out == "phone=*********567 biz=1"


def test_masking_leaves_ordinary_numbers_alone():
    from services import ai
    rec = logging.LogRecord("x", logging.INFO, "", 0, "order=%s total=%s", (12345, "10.50"), None)
    ai._MaskPhones().filter(rec)
    assert rec.getMessage() == "order=12345 total=10.50"


def test_ai_logs_no_longer_contain_message_text():
    src = _src("services/ai.py")
    assert 'transcript=%r' not in src
    assert not re.search(r'log\.(info|warning)\([^)]*\btext=%r', src)


def test_recursive_core_calls_pass_business_config():
    src = _src("services/ai.py")
    for anchor in ('message="image"', 'message=product["name"]'):
        i = src.index(anchor)
        call = src[i:i + re.search(r"\n\s*\)\n", src[i:]).start()]
        assert "business_config=business_config" in call, anchor


def test_voice_transcription_is_budget_gated_and_recorded():
    src = _src("routes/webhook_routes.py")
    gate = src.index("should_call_llm(business[\"id\"], _v_session)")
    call = src.index("await transcribe_whatsapp_voice_note(")
    rec = src.index('intent="voice_transcription"')
    assert gate < call < rec


def test_webhook_blocking_calls_run_in_threadpool():
    src = _src("routes/webhook_routes.py")
    for needle in (
        "run_in_threadpool(crud.message_exists",
        "run_in_threadpool(crud.get_business_by_phone_id",
        "run_in_threadpool(crud.get_or_create_customer",
        "run_in_threadpool(crud.log_message",
        "run_in_threadpool(crud.get_products",
        "run_in_threadpool(maybe_translate",
        "run_in_threadpool(detect_and_set_language",
    ):
        assert needle in src, needle
    assert src.count("crud.get_products(") == 0          # fetched once, then reused


def test_crash_fallback_stays_silent_unless_confirmed_not_in_handoff():
    src = _src("routes/webhook_routes.py")
    i = src.index('log.exception("📦 STEP 6 FAIL')
    window = src[i:i + 1600]
    assert "_in_handoff is not False" in window
