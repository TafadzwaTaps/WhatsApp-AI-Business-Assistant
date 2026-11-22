"""
routes/webhook_routes.py — WhatsApp webhook (GET verify + POST receive),
payment webhook, invoice download, and WebSocket.

Routes: GET /webhook, POST /webhook, POST /payment/webhook,
        GET /invoice/{order_id}, WS /ws/chat/{business_id}
"""

import hmac
import json
import os
import logging

from fastapi import APIRouter, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

import crud
from core.auth import decode_token, require_business
from core.crypto import TokenDecryptionError
from services.ai import generate_reply
from services.security import verify_meta_signature
from workflows.order_lifecycle import update_order_status_supabase, get_order

log = logging.getLogger("wazibot")
router = APIRouter()

# Runtime config — injected by main.py
VERIFY_TOKEN        = os.getenv("VERIFY_TOKEN", "myverifytoken123")
WHATSAPP_APP_SECRET = ""   # set by main.py
SHARED_PHONE_NUMBER_ID = ""
SHARED_WA_TOKEN        = ""
manager              = None  # ConnectionManager — set by main.py
send_whatsapp        = None  # set by main.py
_send_direct         = None  # set by main.py
_log_event           = None  # set by main.py
INVOICES_DIR         = ""    # set by main.py


def _mask_phone(phone) -> str:
    """Log-safe phone: keep only the last 4 digits (e.g. '***1234')."""
    s = str(phone or "")
    return ("***" + s[-4:]) if len(s) > 4 else "***"


def _get_customer_state_for_log(phone: str, business_id: int) -> str:
    try:
        from core.db import supabase as _sb
        res = (
            _sb.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        if res.data:
            return (res.data[0].get("state_data") or {}).get("state", "unknown")
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
    return "unknown"


# ── Webhook verify ────────────────────────────────────────────────────────────

@router.get("/webhook")
async def verify_webhook(request: Request):
    p         = request.query_params
    mode      = p.get("hub.mode", "")
    token     = p.get("hub.verify_token", "")
    challenge = p.get("hub.challenge", "")
    log.info("🔔 Webhook verify request  mode=%r", mode)
    # The documented placeholder value is public knowledge, so it is never
    # accepted: set VERIFY_TOKEN to your own random value (and the same value
    # in Meta's webhook settings).
    _token_ok = hmac.compare_digest(
        (token or "").encode("utf-8"), (VERIFY_TOKEN or "").encode("utf-8")
    ) if VERIFY_TOKEN else False
    if mode == "subscribe" and _token_ok and VERIFY_TOKEN != "myverifytoken123":
        return PlainTextResponse(content=challenge, status_code=200)
    raise HTTPException(403, "Webhook verification failed — token mismatch")


# ── Webhook receive ───────────────────────────────────────────────────────────

import asyncio
import weakref

# One lock per sender so two quick messages from the same customer are processed
# in order (conversation/cart state is a read-modify-write with no DB-level lock).
# Weak values: an entry disappears once nobody holds or waits on the lock.
_CUSTOMER_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _customer_lock(key: str) -> asyncio.Lock:
    lock = _CUSTOMER_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CUSTOMER_LOCKS[key] = lock
    return lock


def _split_webhook_payload(data: dict):
    """Yield one single-message payload per message in entry[] x changes[] x messages[]
    (envelope fields such as contacts/metadata are preserved). Payloads with no messages
    (status-only, malformed) yield nothing."""
    entries = data.get("entry")
    if not isinstance(entries, list):
        return
    for e in entries:
        if not isinstance(e, dict):
            continue
        for c in (e.get("changes") or []):
            if not isinstance(c, dict):
                continue
            value = c.get("value")
            if not isinstance(value, dict):
                continue
            for m in (value.get("messages") or []):
                if isinstance(m, dict):
                    yield {**data, "entry": [{**e, "changes": [{**c, "value": {**value, "messages": [m]}}]}]}


def _payload_sender(single: dict) -> str:
    try:
        return str(single["entry"][0]["changes"][0]["value"]["messages"][0].get("from") or "?")
    except Exception:
        return "?"


def _apply_status_events(data: dict) -> None:
    """Delivery receipts: update messages.status (sent/delivered/read/failed) by wa_message_id."""
    from crud.messages import apply_delivery_status
    for e in (data.get("entry") or []):
        for c in ((e or {}).get("changes") or []):
            for s in (((c or {}).get("value") or {}).get("statuses") or []):
                if not isinstance(s, dict):
                    continue
                wamid, status = s.get("id"), s.get("status")
                if not wamid or not status:
                    continue
                errs = s.get("errors") or []
                detail = None
                if status == "failed" and errs and isinstance(errs[0], dict):
                    detail = f"{errs[0].get('code')}: {errs[0].get('title') or ''}"[:200]
                    log.warning("WhatsApp delivery failed  wa_id=%s  error=%s", wamid, detail)
                try:
                    apply_delivery_status(wamid, status, detail)
                except Exception as exc:
                    log.warning("delivery status update failed  wa_id=%s: %s", wamid, exc)


def _is_duplicate_key_error(exc: Exception) -> bool:
    """True for a unique-violation (Postgres SQLSTATE 23505), tolerating the text forms
    PostgREST/supabase-py put in the exception."""
    code = getattr(exc, "code", None)
    if code is None and exc.args and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    s = str(exc).lower()
    return (str(code) == "23505" or "23505" in s or "duplicate key" in s
            or ("unique" in s and ("constraint" in s or "violat" in s)))


class WebhookRetry(Exception):
    """Raised for a transient failure BEFORE a customer message was durably handled, so the
    webhook can answer 503 and let Meta redeliver it (the unique wamid guard makes a
    redelivery of an already-handled message a harmless no-op)."""


def _is_transient_error(exc: Exception) -> bool:
    """True for network/timeout/5xx/connection-class failures worth a redelivery. Constraint,
    validation and other permanent errors return False (retrying would never succeed and
    sustained non-2xx answers can get a webhook disabled by Meta)."""
    if _is_duplicate_key_error(exc):
        return False
    # Structured signals first: network-level exception types are transient...
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    mod = (type(exc).__module__ or "").split(".")[0]
    if mod in ("httpx", "httpcore", "requests", "urllib3", "socket", "ssl") or \
            any(k in name for k in ("timeout", "connecterror", "networkerror", "transporterror",
                                    "remoteprotocol", "readerror")):
        return True
    # ...and when the database driver gave a structured error code, that code alone decides.
    # Never fall back to the message text then: Postgres can echo the offending row (including
    # the customer's own text) in its message, so a message containing the word "connection"
    # would otherwise be misread as an outage and retried forever.
    code = getattr(exc, "code", None)
    if code is None and exc.args and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    if code is not None and str(code) != "":
        c = str(code)
        return c.startswith(("08", "53", "57")) or c in ("502", "503", "504")
    # No structured code at all: a short message match for generic gateway/network text.
    s = str(exc).lower()[:120]
    return any(k in s for k in ("timed out", "timeout", "connection", "temporarily unavailable",
                                "bad gateway", "service unavailable", "gateway timeout",
                                "server disconnected", "reset by peer", "too many connections"))


def _wa_error_code(result) -> int | None:
    """Meta error code from a send_whatsapp() failure dict, or None."""
    try:
        err = result.get("error") if isinstance(result, dict) else None
        return int(err.get("code")) if isinstance(err, dict) and err.get("code") is not None else None
    except (TypeError, ValueError):
        return None


@router.post("/webhook")
async def receive_message(request: Request):
    raw_body   = await request.body()
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    if not verify_meta_signature(raw_body, sig_header, WHATSAPP_APP_SECRET):
        log.error("webhook: INVALID signature  ip=%s",
                  request.headers.get("x-forwarded-for", "?"))
        raise HTTPException(403, "Invalid webhook signature")

    try:
        data = json.loads(raw_body)
    except Exception:
        return {"status": "ok"}
    if not isinstance(data, dict):
        return {"status": "ok"}
    # "_replay" is an internal flag set only by /webhook/recover-unanswered. It must never be
    # accepted from a request body (it skips the duplicate check and message storage).
    data.pop("_replay", None)

    # Meta may batch several messages (and several entries/changes) in ONE
    # delivery and is told 200 afterwards, so every message must be handled
    # now - anything skipped here is lost for good. Each message runs through
    # the unchanged single-message pipeline below, serialised per customer.
    retry_needed = False
    for single in _split_webhook_payload(data):
        key = _payload_sender(single)
        async with _customer_lock(key):
            try:
                await _handle_one_message(single)
            except WebhookRetry as exc:
                # Transient failure before the message was stored: finish the rest of the
                # batch, then ask Meta to redeliver (handled messages dedupe on redelivery).
                retry_needed = True
                log.error("webhook: transient failure, requesting redelivery: %s", exc)
            except Exception as exc:   # one bad message must not drop the rest of the batch
                log.exception("webhook: message handler crashed: %s", exc)
    try:
        await run_in_threadpool(_apply_status_events, data)
    except Exception as exc:
        log.warning("webhook: status handling failed (ignored): %s", exc)
    if retry_needed:
        raise HTTPException(503, "Temporary processing error; please retry")
    return {"status": "ok"}


def _escalation_keywords_of(business: dict):
    """Optional business-specific escalation words: a column, or features_json.escalation_keywords."""
    kw = business.get("escalation_keywords")
    if kw:
        return kw
    fj = business.get("features_json")
    return (fj.get("escalation_keywords") if isinstance(fj, dict) else None) or ""


async def _handle_one_message(data: dict) -> dict:
    """The original single-message pipeline (STEP 1 - STEP 8), unchanged except for
    the handoff-safety checks marked 'Phase 2'. `data` holds exactly one message."""
    # STEP 1: Parse Meta payload
    try:
        entry = data.get("entry", [])
        if not entry: return {"status": "ok"}
        changes = entry[0].get("changes", [])
        if not changes: return {"status": "ok"}
        value = changes[0].get("value", {})
        if "statuses" in value and "messages" not in value: return {"status": "ok"}
        if "messages" not in value: return {"status": "ok"}

        msg_obj  = value["messages"][0]
        msg_type = msg_obj.get("type", "")
        SUPPORTED_TYPES = ("text", "image", "document", "sticker", "audio",
                            "video", "location", "contacts", "interactive", "button")
        if msg_type not in SUPPORTED_TYPES:
            log.info("Webhook: skipping unsupported message type=%s", msg_type)
            return {"status": "ok"}

        # Build `text` per message type. IMPORTANT: this must be the ONLY
        # place `text` gets assigned below — a prior version of this code
        # set a type-specific placeholder here (e.g. "[voice_note]") and
        # then unconditionally overwrote it a few lines later with
        # msg_obj.get("text", {}).get("body", ""), which is always empty
        # for non-text payloads. That silently dropped every voice note,
        # image, document and sticker (the missing-fields check below would
        # then trip on the now-empty `text` and return before STEP 6 ever
        # ran). Fixed 2026-09-24 — see Phase 0 audit.
        # Phase 13 (Image Understanding): default — only ever set for a
        # genuine "image" message below.
        image_media_id = ""

        if msg_type == "text":
            text = msg_obj.get("text", {}).get("body", "").strip()
        elif msg_type in ("image", "document", "sticker"):
            media_obj = msg_obj.get(msg_type, {})
            text = media_obj.get("caption", "").strip() or "[image]"
            # Phase 13 (Image Understanding): only a genuine "image" message
            # (not a document or decorative sticker) is a candidate for
            # visual analysis — captured here, alongside the caption, so it
            # can be threaded down to generate_reply() exactly like
            # audio's media_id already is for voice notes.
            image_media_id = media_obj.get("id", "") if msg_type == "image" else ""
        elif msg_type == "audio":
            text = "[voice_note]"
        elif msg_type == "video":
            media_obj = msg_obj.get("video", {})
            text = media_obj.get("caption", "").strip() or "[video]"
        elif msg_type == "location":
            text = "[location]"
        elif msg_type == "contacts":
            text = "[contact_card]"
        elif msg_type in ("interactive", "button"):
            # Button/list replies carry their selected text here instead of
            # the top-level "text" field.
            interactive_obj = msg_obj.get("interactive", {}) or msg_obj.get("button", {})
            text = (
                interactive_obj.get("button_reply", {}).get("title")
                or interactive_obj.get("list_reply", {}).get("title")
                or interactive_obj.get("text")
                or msg_obj.get("button", {}).get("text", "")
            ).strip() or "[unsupported]"
        else:
            text = ""

        metadata        = value.get("metadata", {})
        phone_number_id = metadata.get("phone_number_id", "")
        customer_phone  = msg_obj.get("from", "")
        wa_message_id   = msg_obj.get("id", "")

        if not phone_number_id or not customer_phone or not text:
            log.warning("Webhook: missing required fields")
            return {"status": "ok"}

        log.info("📩 STEP 1 OK  wa_id=%s  from=%s  type=%s  len=%d",
                 wa_message_id, _mask_phone(customer_phone), msg_type, len(text))
    except Exception as exc:
        log.error("📥 STEP 1 FAIL: %s", exc)
        return {"status": "ok"}

    # STEP 1b: Deduplication
    if wa_message_id and not data.get("_replay"):
        try:
            if await run_in_threadpool(crud.message_exists, wa_message_id):
                return {"status": "ok"}
        except Exception as exc:
            log.error("⚠️  Dedup check failed: %s", exc)

    # STEP 2: Find business
    try:
        from services.tenant_router import (
            is_shared_number, resolve_business_for_shared_number, is_switch_request,
        )
        # NOTE (Phase 1 audit): tenant_router currently does NOT define
        # is_businesses_help_request or _category_icon, and build_business_picker
        # has a different signature (no current_name/title). The import below
        # therefore always raises ImportError and the stubs are used:
        # is_businesses_help_request() always returns False, so the
        # "Business Directory" branch further down is currently INERT (never
        # runs, cannot crash, never swallows a reply). Do not rely on it.
        try:
            from services.tenant_router import (
                is_businesses_help_request, build_business_picker, _category_icon,
            )
        except ImportError:
            def is_businesses_help_request(t): return False
            def build_business_picker(b, p="WaziBot", current_name=""): return ""
            def _category_icon(c): return "🏪"
        if is_shared_number(phone_number_id):
            log.info("📋 STEP 2 — shared number  phone=%s", _mask_phone(customer_phone))
            active_businesses = crud.get_active_businesses()

            # Phase 7: Directory command — show full business directory
            # Works whether or not customer already has a selection
            # Does NOT clear selection — customer must say "switch" to actually switch
            from services.tenant_router import (
                get_selected_business_id, get_selected_business_name,
            )
            if is_businesses_help_request(text):
                from services.tenant_router import get_shared_wa_phone as _get_pname
                platform_name = _get_pname() or "WaziBot"
                current_name  = get_selected_business_name(customer_phone)
                picker_title  = (
                    f"📍 *Business Directory — {platform_name}*"
                    if not current_name else
                    f"📍 *Business Directory*\n\n🏪 Currently: *{current_name}*"
                )
                picker = build_business_picker(
                    active_businesses, platform_name,
                    current_name=current_name,
                    title=picker_title,
                )
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, picker)
                try:
                    _cust = crud.get_or_create_customer(customer_phone,
                                get_selected_business_id(customer_phone) or 0)
                    crud.create_message(_cust["id"],
                                get_selected_business_id(customer_phone) or 0,
                                text, "incoming", wa_message_id=wa_message_id)
                except Exception as _swallowed_exc:
                    from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
                return {"status": "ok"}

            business, direct_reply = resolve_business_for_shared_number(
                phone=customer_phone, text=text, active_businesses=active_businesses,
            )
            if direct_reply and not business:
                # Picker shown (no business selected yet) — send and stop
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, direct_reply)
                try:
                    cust_any = crud.get_or_create_customer(customer_phone, 0)
                    crud.create_message(cust_any["id"], 0, text, "incoming",
                                        wa_message_id=wa_message_id)
                    crud.create_message(cust_any["id"], 0, direct_reply, "outgoing")
                except Exception as _swallowed_exc:
                    from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
                return {"status": "ok"}

            if direct_reply and business:
                # Business just selected — send confirmation immediately,
                # then fall through to generate_reply() for the welcome greeting
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, direct_reply)
                try:
                    cust_confirmed = crud.get_or_create_customer(customer_phone, business["id"])
                    crud.create_message(cust_confirmed["id"], business["id"],
                                        direct_reply, "outgoing", sender_type="ai")
                except Exception as _swallowed_exc:
                    from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
                # Replace the customer's text with "hi" so generate_reply sends
                # a proper welcome rather than treating "2" as an order attempt
                text = "hi"

            if not business:
                log.error("📋 STEP 2 — no business resolved for shared number")
                return {"status": "ok"}
            token = SHARED_WA_TOKEN
        else:
            business = await run_in_threadpool(crud.get_business_by_phone_id, phone_number_id)
            if not business:
                log.error("📋 STEP 2 FAIL — No business for phone_number_id=%s", phone_number_id)
                return {"status": "ok"}
            if not business.get("is_active", True):
                return {"status": "ok"}
            token = ""
    except Exception as exc:
        log.exception("📋 STEP 2 FAIL: %s", exc)
        if _is_transient_error(exc):      # nothing stored yet: let Meta redeliver
            raise WebhookRetry("business lookup failed") from exc
        return {"status": "ok"}

    # STEP 3: Decrypt token
    if not is_shared_number(phone_number_id):
        try:
            token = crud.get_decrypted_token(business)
        except TokenDecryptionError as exc:
            log.error("🔑 STEP 3 FAIL: %s", exc)
            token = ""

    # STEP 4: Get or create customer
    try:
        customer = await run_in_threadpool(crud.get_or_create_customer, customer_phone, business["id"])
    except Exception as exc:
        log.exception("👤 STEP 4 FAIL: %s", exc)
        if _is_transient_error(exc):      # nothing stored yet: let Meta redeliver
            raise WebhookRetry("customer lookup failed") from exc
        return {"status": "ok"}

    # ACQUISITION TRACKING — record conversation_started on first ever message.
    # Runs after customer creation so we know the customer exists.
    # Fire-and-forget — never block the webhook response.
    # The acquisition_service deduplicates internally (one event per phone per biz).
    try:
        from services.acquisition_service import record_conversation_started
        record_conversation_started(business["id"], customer_phone)
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

    # STEP 5: Save incoming message
    in_msg: dict = {}
    if data.get("_replay"):
        pass      # recovery replay: the incoming row already exists, do not store it again
    else:
        try:
            # The message row is the dedupe guard, so it is written first; the legacy
            # chat_messages log comes after so a duplicate delivery leaves no stray log row.
            in_msg = await run_in_threadpool(
                crud.create_message,
                customer["id"], business["id"], text, "incoming",
                wa_message_id=wa_message_id,
            )
        except Exception as exc:
            if _is_duplicate_key_error(exc):
                log.warning("💾 STEP 5 — Duplicate at INSERT  wa_id=%s", wa_message_id)
                return {"status": "ok"}
            log.exception("💾 STEP 5 FAIL: %s", exc)
            if _is_transient_error(exc):      # not stored, nothing replied: let Meta redeliver
                raise WebhookRetry("could not store incoming message") from exc
        try:
            await run_in_threadpool(crud.log_message, business["id"], customer_phone, "in", text)
        except Exception as exc:
            log.warning("💾 STEP 5 — legacy chat log failed (ignored): %s", exc)

    try:
        if not data.get("_replay"):
            await manager.broadcast(business["id"], {
                "event": "new_message", "customer_id": customer["id"],
                "phone": customer_phone, "message": in_msg,
            })
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

    # Phase 2 handoff safety: never speak over a human agent. The strict probe returns
    # None when the state cannot be read - then stay silent rather than assume
    # "browsing" (the message is already stored for the agent to see).
    from services._ai_state import is_in_human_handoff
    _in_handoff = await run_in_threadpool(is_in_human_handoff, customer_phone, business["id"])
    if _in_handoff is None:      # one retry for a transient database error
        _in_handoff = await run_in_threadpool(is_in_human_handoff, customer_phone, business["id"])
    if _in_handoff is None:
        log.error("STEP 6 SKIP — conversation state unreadable, not replying  phone=%s", _mask_phone(customer_phone))
        return {"status": "ok"}

    # STEP 6: Generate AI reply
    try:
        products = await run_in_threadpool(crud.get_products, business["id"])
        message_has_image = (msg_type in ("image", "document", "sticker"))
        is_voice_note = (msg_type == "audio")

        voice_transcript = None
        if is_voice_note and text == "[voice_note]":
            # Reuses the same Whisper call already proven out on the
            # dashboard's POST /voice/transcribe endpoint — added here so
            # voice notes sent on WhatsApp actually get answered instead of
            # always hitting the "can't process audio yet" fallback below.
            # Fails closed to that same friendly fallback on any error
            # (missing OPENAI_API_KEY, network failure, empty transcript).
            audio_obj = msg_obj.get("audio", {})
            media_id  = audio_obj.get("id", "")
            if media_id and token:
                # C1: transcription costs money, so it passes the same per-business /
                # per-conversation LLM budget gate as image analysis and is recorded in usage.
                _v_ok, _v_session, _v_cost = True, {}, None
                try:
                    from services import ai_usage_tracker as _v_cost
                    from services._ai_state import _read_state_data_strict as _v_read
                    _v_session = (await run_in_threadpool(_v_read, customer_phone, business["id"])).get("session") or {}
                    _v_ok, _v_reason = _v_cost.should_call_llm(business["id"], _v_session)
                    if not _v_ok:
                        log.info("voice transcription skipped (budget gate) biz=%s reason=%s",
                                 business["id"], _v_reason)
                except Exception as exc:
                    log.debug("voice budget gate unavailable (allowing): %s", exc)
                if _v_ok:
                    try:
                        from services.whatsapp_service import transcribe_whatsapp_voice_note
                        voice_transcript = await transcribe_whatsapp_voice_note(
                            media_id=media_id, wa_token=token,
                            language=(business.get("preferred_language") or "en"),
                        )
                    except Exception as exc:
                        log.warning("voice transcription call failed: %s", exc)
                        voice_transcript = None
                    try:
                        if _v_cost is not None:
                            from services._ai_state import _write_state_data as _v_write
                            if voice_transcript:
                                # Re-read AFTER the slow transcription (strict: a read error skips the
                                # write) so a concurrent change to the session is never reverted.
                                _v_fresh = (await run_in_threadpool(_v_read, customer_phone, business["id"])).get("session") or {}
                                _v_cost.record_customer_llm_call(_v_fresh)
                                await run_in_threadpool(_v_write, customer_phone, business["id"], {"session": _v_fresh})
                            _v_cost.record_llm_usage(
                                business_id=business["id"], phone=customer_phone,
                                conversation_id=_v_cost.make_conversation_id(business["id"], customer_phone),
                                intent="voice_transcription", tier="full",
                                provider="openai", model="whisper-1",
                                prompt_tokens=0, completion_tokens=0, latency_ms=0,
                                status="ok" if voice_transcript else "no_result",
                            )
                    except Exception as exc:
                        log.debug("voice usage record failed (ignored): %s", exc)

            if not voice_transcript and _in_handoff:
                return {"status": "ok"}      # a human owns this chat: no automatic fallback reply
            if not voice_transcript:
                reply = (
                    "🎤 Sorry, I couldn't understand that voice message. "
                    "Could you try again or type your order instead? "
                    "Type *menu* to get started! 😊"
                )
                try:
                    out_msg = crud.create_message(
                        customer["id"], business["id"], reply, "outgoing", sender_type="ai",
                    )
                except Exception as exc:
                    log.warning("STEP 7 voice-note-reply failed: %s", exc)
                if token:
                    await run_in_threadpool(send_whatsapp, phone_number_id, token, customer_phone, reply)
                return {"status": "ok"}

            # Transcription succeeded — treat the transcript as this
            # message's text and fall through into the normal pipeline
            # below (language detection, generate_reply, etc.) exactly
            # like any typed message.
            text = voice_transcript
            log.info("🎤 voice note transcribed  phone=%s  len=%d", _mask_phone(customer_phone), len(text))

        # ── Multi-language: explicit customer language-switch request ──────
        # New module, additive only — never touches generate_reply() or the
        # AI conversation state. If the customer explicitly asks to switch
        # reply language ("translate to french", "switch to shona", etc.),
        # acknowledge immediately and skip the normal AI turn for this
        # message, exactly like the voice-note short-circuit above.
        try:
            from services.language_commands import detect_language_switch_command
            switch_reply = detect_language_switch_command(text, customer_phone, business["id"])
        except Exception as exc:
            log.debug("language switch check failed (ignored): %s", exc)
            switch_reply = None

        if switch_reply and not _in_handoff:
            try:
                out_msg = crud.create_message(
                    customer["id"], business["id"], switch_reply, "outgoing", sender_type="ai",
                )
            except Exception as exc:
                log.warning("STEP 7 language-switch-reply failed: %s", exc)
            if token:
                await run_in_threadpool(send_whatsapp, phone_number_id, token, customer_phone, switch_reply)
            return {"status": "ok"}

        # ── Multi-language: passive detection from natural phrasing ────────
        # Already-built helper in translation_layer.py, previously never
        # called anywhere. Fire-and-forget — only writes preferred_language
        # if a recognised greeting/phrase is detected; never blocks or
        # alters this turn's AI reply.
        try:
            from services.translation_layer import detect_and_set_language
            await run_in_threadpool(detect_and_set_language, text, customer_phone, business["id"])
        except Exception as exc:
            log.debug("detect_and_set_language failed (ignored): %s", exc)

        # ── Phase 6: understand a non-English message ───────────────────────
        # Mirrors the outgoing maybe_translate() wrapper below, in reverse:
        # an offline, best-effort word/phrase substitution turns known
        # foreign words into English BEFORE the message reaches the existing
        # deterministic pipeline (fuzzy_matcher, ai.py, nl_commerce.py,
        # business_knowledge.py) — none of which need to change, since they
        # already understand English. Uses the customer's STORED preferred_
        # language (set just above, or by an earlier turn / explicit
        # /translate command), not just this message's own signal words, so
        # a later bare "mbiri" ("2") still resolves correctly even though it
        # carries no language signal on its own. Only ever narrows what the
        # existing matcher sees as unmatched text into matched text — never
        # removes information the matcher already used, so this cannot make
        # an existing English conversation behave any differently.
        try:
            from services.translation_layer import get_customer_language, translate_incoming_to_english
            _customer_lang = get_customer_language(customer_phone, business["id"])
            if _customer_lang and _customer_lang != "en":
                _translated_text = translate_incoming_to_english(text, _customer_lang)
                if _translated_text != text:
                    log.info("incoming translation applied  phone=%s  lang=%s",
                              _mask_phone(customer_phone), _customer_lang)
                    text = _translated_text
        except Exception as exc:
            log.debug("translate_incoming_to_english failed (ignored): %s", exc)

        business_phone_id = business.get("whatsapp_phone_id", "")
        msg_from          = msg_obj.get("from", "")
        is_from_agent     = bool(business_phone_id and msg_from and msg_from == business_phone_id)

        # ── Phase 2: Conversational Intent Engine ───────────────────────────
        # New, read-only classification layer that runs BEFORE the existing
        # deterministic AI (generate_reply() / the carts.state_data state
        # machine below) — see services/intent_engine.py's module docstring
        # for the full design rationale. It does NOT replace anything below.
        # Classification+logging always runs (cheap, deterministic, never
        # blocks or alters the reply below) so its real-world accuracy can
        # be judged from the logs before anything is allowed to act on it.
        #
        # The low-confidence clarification intercept described in the Phase
        # 2 spec is implemented below but gated behind
        # INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT (default OFF). A keyword
        # classifier's coverage of real customer phrasing can't be fully
        # proven ahead of time — testing this found plausible real messages
        # ("please order two burgers", "hello there") it doesn't yet
        # recognise, which would otherwise hijack a message the existing
        # state machine already handles fine via its own P11/P12 fallback.
        # Shipping this OFF by default is the "smallest safe change" for
        # this phase; turn it on (Render env var, no redeploy needed) once
        # a few days of intent.classified logs show the classifier's
        # "unknown" rate on real traffic is low enough to trust.
        if not is_from_agent and not message_has_image and not text.startswith("["):
            try:
                # Phase 3: resolve elliptical replies ("two." after "which
                # one?" -> "chicken.") against the customer's own last few
                # messages before logging/acting on the classification —
                # see services/conversation_context.py's module docstring.
                # Reads only (crud.get_recent_messages, bounded to a small
                # limit) from the messages already kept for the dashboard
                # inbox — no new storage, no LLM, same $0-cost design as
                # Phase 2. Falls back to the plain Phase 2 classification
                # (still fully correct on its own) if this fails for any
                # reason or the customer record isn't available yet.
                from services.conversation_context import classify_with_context
                _intent_result = classify_with_context(
                    text, customer_id=customer.get("id"),
                    products=products,
                    language=(business.get("preferred_language") or "en"),
                )
                _log_event(
                    "intent.classified", phone=customer_phone, biz=business["id"],
                    intent=_intent_result.intent, confidence=_intent_result.confidence,
                    tier=_intent_result.tier, entities=_intent_result.entities,
                )

                _intercept_enabled = os.getenv(
                    "INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT", "false"
                ).strip().lower() in ("1", "true", "yes", "on")

                if _intercept_enabled and _intent_result.tier == "low":
                    from services._ai_state import _get_state
                    _current_state = _get_state(customer_phone, business["id"])
                    if _current_state == "browsing":
                        clarify_reply = (
                            "I'd be happy to help 😊 Are you looking for a product, "
                            "checking an order, making a booking, or something else?"
                        )
                        try:
                            crud.create_message(
                                customer["id"], business["id"], clarify_reply,
                                "outgoing", sender_type="ai",
                            )
                        except Exception as exc:
                            log.warning("STEP 6b intent-clarification-reply failed: %s", exc)
                        if token:
                            await run_in_threadpool(send_whatsapp, phone_number_id, token, customer_phone, clarify_reply)
                        return {"status": "ok"}
            except Exception as exc:
                # Classification is purely additive — any failure here must
                # never block or alter the existing reply pipeline below.
                log.debug("intent_engine classification failed (ignored): %s", exc)

        # Build per-business config so AI can tailor its copy
        # ── Self-healing business-config lookup ─────────────────────────────────
        # If the `business` dict came from a SELECT that's missing columns
        # biz_config actually needs (e.g. get_active_businesses on the
        # shared-number path using a stale/incomplete column list — this has
        # happened more than once with different fields: currency,
        # is_service_business, pickup_enabled...), a plain business.get(key,
        # default) silently and incorrectly falls back to the default — not
        # because that's the real value, but because the column was never
        # fetched in the first place.
        #
        # Rather than keep chasing individual fields one at a time whenever
        # this recurs, this checks for ANY of the fields biz_config below
        # actually reads, and if even one is missing, re-fetches the full
        # set directly by ID in a single query. This makes biz_config
        # correct regardless of which SELECT statement — current or a
        # future one that regresses again — originally found the business.
        _CONFIG_FIELDS = (
            "currency", "currency_symbol", "welcome_message", "menu_header",
            "is_service_business", "default_slot_mins", "pickup_enabled",
            "cash_enabled", "delivery_fee", "category",
        )
        if any(f not in business for f in _CONFIG_FIELDS):
            try:
                from core.db import supabase as _sb
                _cfg_row = (
                    _sb.table("businesses")
                    .select(",".join(_CONFIG_FIELDS))
                    .eq("id", business["id"])
                    .limit(1)
                    .execute()
                )
                if _cfg_row.data:
                    fetched = _cfg_row.data[0]
                    for _f in _CONFIG_FIELDS:
                        if _f not in business and _f in fetched:
                            business[_f] = fetched[_f]
                    log.info(
                        "🩹 business-config self-healed  biz=%s  currency=%r  symbol=%r  "
                        "is_service_business=%r  pickup_enabled=%r",
                        business["id"], business.get("currency"), business.get("currency_symbol"),
                        business.get("is_service_business"), business.get("pickup_enabled"),
                    )
            except Exception as _cfg_exc:
                log.warning("business-config self-heal lookup failed: %s", _cfg_exc)

        biz_config = {
            "welcome_message":       business.get("welcome_message", "")     or "",
            "currency":              business.get("currency", "USD")          or "USD",
            "currency_symbol":       business.get("currency_symbol", "$")     or "$",
            "category":              business.get("category", "")             or "",
            "menu_header":           business.get("menu_header", "")          or "",
            # Service business mode
            "is_service_business":   bool(business.get("is_service_business", False)),
            "default_slot_mins":     int(business.get("default_slot_mins", 60) or 60),
            # Fulfillment options — used to decide whether to ask delivery vs pickup
            "pickup_enabled":        bool(business.get("pickup_enabled", True)),
            "cash_enabled":          bool(business.get("cash_enabled", True)),
            # Visual catalog: pass sending credentials so ai.py can send images directly
            "phone_number_id":       phone_number_id or "",
            "wa_token":              token or "",
            # Business-specific escalation words (T1). Optional: column or features_json key.
            "escalation_keywords":   _escalation_keywords_of(business),
        }

        # ── Phase 13: Image Understanding ────────────────────────────────────
        # Mirrors the voice-note block above: the async I/O (media
        # validation + download + the vision-LLM call) happens HERE, in the
        # webhook's own async context, and only the already-resolved result
        # is handed to the deterministic engine — generate_reply() itself
        # stays fully synchronous, exactly like it already receives
        # voice_transcript as plain resolved text rather than a media id.
        # Entirely additive: image_analysis defaults to None, and ai.py
        # only acts on it when it's genuinely present — every other image
        # message (proof-of-payment, feature off, wrong plan, over quota,
        # download/analysis failure) behaves exactly as it did before this
        # phase.
        image_analysis = None
        if image_media_id and not is_from_agent and not _in_handoff:
            try:
                from core import plan_guard as _pg_img
                _img_allowed_plan = _pg_img.feature_access(
                    "image_understanding", business["id"]
                ).get("allowed", False)
            except Exception:
                _img_allowed_plan = False

            if _img_allowed_plan:
                try:
                    from services.image_understanding import (
                        is_enabled as _img_enabled,
                        analyze_customer_image,
                        get_last_call_meta as _img_get_meta,
                    )
                    from services import ai_usage_tracker as _img_cost
                    from services._ai_state import (
                        _get_session as _img_get_session,
                        _write_state_data as _img_write_state_data,
                    )

                    if _img_enabled():
                        _img_session = _img_get_session(customer_phone, business["id"]) or {}
                        _img_ok, _img_reason = _img_cost.should_call_llm(business["id"], _img_session)
                    else:
                        _img_session, _img_ok, _img_reason = {}, False, "disabled"

                    if _img_ok:
                        from services.whatsapp_service import download_whatsapp_media
                        img_bytes, img_mime = await download_whatsapp_media(image_media_id, token)

                        if img_bytes:
                            _img_caption = "" if text == "[image]" else text
                            image_analysis = analyze_customer_image(img_bytes, img_mime, _img_caption)
                            _img_meta = _img_get_meta()

                            if image_analysis is not None:
                                _img_cost.record_customer_llm_call(_img_session)
                                _img_write_state_data(customer_phone, business["id"], {"session": _img_session})

                            _img_cost.record_llm_usage(
                                business_id=business["id"], phone=customer_phone,
                                conversation_id=_img_cost.make_conversation_id(business["id"], customer_phone),
                                intent="image_understanding", tier="full",
                                provider=os.getenv("IMAGE_UNDERSTANDING_PROVIDER", "openai").strip().lower(),
                                model=os.getenv("IMAGE_UNDERSTANDING_MODEL", "") or "default",
                                prompt_tokens=_img_meta.get("prompt_tokens", 0),
                                completion_tokens=_img_meta.get("completion_tokens", 0),
                                latency_ms=_img_meta.get("latency_ms", 0),
                                status="ok" if image_analysis is not None else "no_result",
                            )
                    else:
                        log.debug("image understanding skipped — %s", _img_reason)
                except Exception as exc:
                    log.debug("image understanding pipeline failed (ignored): %s", exc)
                    image_analysis = None

        # WhatsApp marketing consent keywords (STOP, UNSUBSCRIBE, SUBSCRIBE, START after an opt-out) are
        # handled here, before the AI: the customer is opted out/in immediately and gets a confirmation.
        # Inactive until migrations/whatsapp_consent_and_templates.sql is applied. Any other message is
        # untouched and goes to the AI exactly as before.
        _consent_reply = None
        if msg_type == "text" and not is_from_agent:
            try:
                from services import wa_consent as _wa_consent
                _consent_reply = await run_in_threadpool(
                    _wa_consent.handle_inbound, business["id"], customer_phone, text, business.get("name", ""))
            except Exception as exc:
                log.warning("consent keyword handling failed (ignored): %s", type(exc).__name__)
        _reply_fn = (lambda **_kw: _consent_reply) if _consent_reply else generate_reply

        reply = await run_in_threadpool(_reply_fn,
            message=text,
            phone=customer_phone,
            business_id=business["id"],
            business_name=business["name"],
            products=products,
            message_has_image=message_has_image,
            message_is_from_agent=is_from_agent,
            voice_transcript=voice_transcript,
            business_config=biz_config,
            image_analysis=image_analysis,
        )

        # ── Multi-language: passive translation wrapper ────────────────────
        # Applied AFTER generate_reply() returns, exactly per
        # services/translation_layer.py's documented usage. No-op unless the
        # business has opted in (features_json.translation_enabled) and the
        # customer has a non-English preferred_language on file — which is
        # set either by detect_and_set_language() picking up on greeting
        # phrases, or explicitly via the language-switch command above.
        try:
            from services.translation_layer import maybe_translate
            reply = await run_in_threadpool(maybe_translate, reply, customer_phone, business["id"], products=products)
        except Exception as exc:
            log.debug("maybe_translate failed (using original reply): %s", exc)

        _log_event(
            "ai.request" if reply else "ai.suppressed",
            phone=customer_phone, biz=business["id"],
            msg_len=len(text), reply_len=len(reply),
        )
    except Exception as exc:
        log.exception("📦 STEP 6 FAIL: %s", exc)
        # Bug fix: this generic "thanks for contacting us" message reads as
        # a first-contact auto-reply — confusing and misleading if the
        # customer was actually mid-booking or mid-checkout when something
        # failed, since it gives no indication anything they were doing was
        # even acknowledged. Check their current state so the apology at
        # least reflects what they were actually in the middle of, rather
        # than sounding like their message was never processed at all.
        # A human owns this chat (or we cannot tell): never send the generic apology over them.
        if _in_handoff is not False:
            return {"status": "ok"}
        try:
            from services._ai_state import _get_state
            _crash_state = _get_state(customer_phone, business["id"]) or ""
        except Exception:
            _crash_state = ""

        if "booking" in _crash_state:
            reply = (
                f"😔 Sorry — something went wrong confirming your booking with "
                f"*{business['name']}*. Please try again, or type *book* to start over."
            )
        elif _crash_state in ("checkout", "awaiting_payment"):
            reply = (
                f"😔 Sorry — something went wrong processing your order with "
                f"*{business['name']}*. Please try again, or contact us directly."
            )
        else:
            reply = (
                f"Hi! 👋 Thanks for contacting *{business['name']}*. "
                f"We received your message and will get back to you shortly."
            )

    # STEP 7: Save outgoing
    out_msg: dict = {}
    try:
        await run_in_threadpool(crud.log_message, business["id"], customer_phone, "out", reply)
        out_msg = await run_in_threadpool(
            crud.create_message,
            customer["id"], business["id"], reply, "outgoing", sender_type="ai",
        )
    except Exception as exc:
        log.exception("💾 STEP 7 FAIL: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": out_msg,
        })
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

    # STEP 8: Send via WhatsApp API
    if not reply:
        log.info("📤 STEP 8 SKIP — empty reply  phone=%s  state=%s",
                 _mask_phone(customer_phone), _get_customer_state_for_log(customer_phone, business["id"]))
        return {"status": "ok"}

    if token:
        result = await run_in_threadpool(send_whatsapp, phone_number_id, token, customer_phone, reply)
        if "error" in result:
            _log_event("wa.failed", phone=customer_phone, biz=business["id"],
                       error=result["error"])
            if _wa_error_code(result) == 131047:
                # Meta: more than 24h since the customer's last message. Not retryable and not
                # a bug: a template message would be needed. Recorded so it is visible.
                log.warning("📤 STEP 8 — 24h customer-service window closed (131047)  biz=%s", business["id"])
                _log_event("wa.window_closed", phone=customer_phone, biz=business["id"])
            # Record the failure on the stored reply so the inbox shows it as undelivered.
            if isinstance(out_msg, dict) and out_msg.get("id"):
                await run_in_threadpool(crud.messages.mark_message_failed, out_msg["id"])
        else:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            _log_event("wa.sent", phone=customer_phone, biz=business["id"],
                       msg_id=msg_id, reply_len=len(reply))
            # Keep Meta's id on the stored message so delivery receipts can update it.
            if isinstance(out_msg, dict) and out_msg.get("id"):
                await run_in_threadpool(crud.messages.set_message_wa_id, out_msg["id"], msg_id)
    else:
        log.error("📤 STEP 8 FAIL — No token for '%s'", business["name"])
        if isinstance(out_msg, dict) and out_msg.get("id"):
            await run_in_threadpool(crud.messages.mark_message_failed, out_msg["id"])

    return {"status": "ok"}


# ── Recovery of lost replies ──────────────────────────────────────────────────

@router.post("/webhook/recover-unanswered")
async def recover_unanswered(request: Request, dry_run: bool = True,
                             max_age_minutes: int = 30, limit: int = 20):
    """Re-run the reply for customer messages that were stored but never answered (crash,
    restart or failure between storing the message and replying).

    Safety: protected by CRON_SECRET and fails closed; DRY RUN BY DEFAULT (nothing is sent
    unless dry_run=false is passed explicitly); text messages only; at most 120 minutes old;
    skips customers in human handoff, shared-number businesses and inactive businesses;
    re-checks "still unanswered" under the per-customer lock right before replaying, so a
    reply that arrived in the meantime is never duplicated."""
    import hmac as _hmac
    _secret = os.getenv("CRON_SECRET", "")
    if not _secret:
        log.error("CRON_SECRET not configured - refusing /webhook/recover-unanswered")
        raise HTTPException(503, "Cron endpoint not configured")
    if not _hmac.compare_digest(request.headers.get("x-cron-secret", "").encode(), _secret.encode()):
        raise HTTPException(403, "Forbidden")

    max_age = min(max(int(max_age_minutes), 5), 120)
    limit = min(max(int(limit), 1), 50)
    from core.db import supabase
    from services._ai_state import is_in_human_handoff

    candidates = await run_in_threadpool(crud.messages.get_unanswered_inbound, max_age, 3, limit * 3)
    report = {"dry_run": bool(dry_run), "candidates": len(candidates), "replayed": 0, "failed": 0,
              "skipped": {"non_text": 0, "business": 0, "shared_number": 0, "handoff": 0,
                          "answered_meanwhile": 0, "no_customer": 0, "state_changed": 0}}
    handled = 0
    for c in candidates:
        if handled >= limit:
            break
        text = (c.get("text") or "").strip()
        if not text or text.startswith("["):
            report["skipped"]["non_text"] += 1      # placeholders such as [image] cannot be replayed
            continue
        bid, cid = c.get("business_id"), c.get("customer_id")
        if not c.get("created_at"):
            report["skipped"]["no_customer"] += 1     # cannot prove it is unanswered without a timestamp
            continue
        biz = await run_in_threadpool(crud.get_business_by_id, bid)
        if not biz or not biz.get("is_active", True):
            report["skipped"]["business"] += 1
            continue
        if biz.get("use_shared_number") or not biz.get("whatsapp_phone_id"):
            report["skipped"]["shared_number"] += 1
            continue
        cust = await run_in_threadpool(
            lambda: supabase.table("customers").select("phone").eq("id", cid).eq("business_id", bid)
            .limit(1).execute().data)
        phone = (cust or [{}])[0].get("phone") if cust else None
        if not phone:
            report["skipped"]["no_customer"] += 1
            continue
        if await run_in_threadpool(is_in_human_handoff, phone, bid) is not False:
            report["skipped"]["handoff"] += 1       # a human owns it, or state unreadable
            continue
        handled += 1
        if dry_run:
            report["replayed"] += 1                 # "would replay"
            continue
        async with _customer_lock(phone):
            if await run_in_threadpool(crud.messages.has_reply_since, cid, bid, c.get("created_at") or ""):
                report["skipped"]["answered_meanwhile"] += 1
                handled -= 1
                continue
            # Re-check handoff now that we hold the lock (it may have started while we waited).
            if await run_in_threadpool(is_in_human_handoff, phone, bid) is not False:
                report["skipped"]["handoff"] += 1
                handled -= 1
                continue
            # If the conversation state was written AFTER this message arrived, the original run
            # got part-way (e.g. an order or cart change) before it was lost. Replaying a
            # "yes"/"confirm" into that changed state could repeat the action, so leave it to a human.
            try:
                st = await run_in_threadpool(
                    lambda: supabase.table("carts").select("updated_at").eq("phone", phone)
                    .eq("business_id", bid).limit(1).execute().data)
            except Exception:
                st = None
            if st is None or (st and crud.messages._ts(st[0].get("updated_at")) > crud.messages._ts(c.get("created_at"))):
                report["skipped"]["state_changed"] += 1
                handled -= 1
                continue
            payload = {"_replay": True, "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": biz["whatsapp_phone_id"]},
                "messages": [{"type": "text", "from": phone,
                              "id": c.get("wa_message_id") or f"replay-{c.get('id')}",
                              "text": {"body": text}}]}}]}]}
            try:
                await _handle_one_message(payload)
                report["replayed"] += 1
            except Exception as exc:
                report["failed"] += 1
                log.error("recover_unanswered: replay failed biz=%s customer=%s: %s", bid, cid, type(exc).__name__)
    log.info("recover_unanswered: %s", report)
    return report


# ── Payment webhook ───────────────────────────────────────────────────────────

class PaymentConfirmRequest(BaseModel):
    reference: str
    amount: float


@router.post("/payment/webhook")
async def payment_webhook(data: PaymentConfirmRequest, request: Request):
    # Authentication: this endpoint can mark an order as paid, so it must only
    # accept calls from a trusted payment provider/integration that knows the
    # shared secret. Fails closed when PAYMENT_WEBHOOK_SECRET is not configured.
    import hmac as _hmac
    _expected = os.getenv("PAYMENT_WEBHOOK_SECRET", "").strip()
    if not _expected:
        log.error("payment_webhook: PAYMENT_WEBHOOK_SECRET not configured — refusing request")
        raise HTTPException(503, "Payment webhook is not configured")
    _given = request.headers.get("X-Payment-Webhook-Secret", "")
    if not _hmac.compare_digest(_given.encode(), _expected.encode()):
        log.warning("payment_webhook: bad secret  ip=%s", request.headers.get("x-forwarded-for", "?"))
        raise HTTPException(403, "Forbidden")
    reference = (data.reference or "").strip().upper()
    if not reference.startswith("ORDER-"):
        raise HTTPException(400, f"Invalid reference. Expected ORDER-{{id}}, got: {reference}")
    try:
        order_id = int(reference.split("-")[1])
    except (IndexError, ValueError):
        raise HTTPException(400, f"Cannot parse order ID: {reference}")

    order = get_order(order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")

    order_total = float(order.get("total_price") or 0)
    if round(float(data.amount), 2) != round(order_total, 2):
        raise HTTPException(400,
            f"Amount mismatch: expected ${order_total:.2f}, received ${data.amount:.2f}")

    if order.get("payment_status") == "paid":
        return {"success": True, "message": f"Order {order_id} already paid.", "order_id": order_id}

    biz_id = order.get("business_id")
    if crud.payment_blocked(order):
        # Cancelled/refunded: never marked paid and never "confirmed" to the customer. The owner is told so
        # the money can be returned. 200 so the provider does not keep retrying.
        crud.alert_owner_payment_on_closed_order(order, "payment_webhook")
        return {"success": False, "order_id": order_id, "message": f"Order {order_id} is closed; manual review needed."}
    try:
        _claimed = crud.claim_order_paid(order_id, biz_id, {"payment_reference": reference})
    except Exception:
        raise HTTPException(503, "Temporary error - please retry")   # provider re-delivers
    if not _claimed:
        _now_order = get_order(order_id) or order
        if crud.payment_blocked(_now_order):
            crud.alert_owner_payment_on_closed_order(_now_order, "payment_webhook")
            return {"success": False, "order_id": order_id, "message": f"Order {order_id} is closed; manual review needed."}
        return {"success": True, "message": f"Order {order_id} already paid.", "order_id": order_id}
    try:
        update_order_status_supabase(order_id, "paid", business_id=biz_id)
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

    phone = order.get("customer_phone", "")
    if phone and biz_id:
        try:
            business = crud.get_business_by_id(biz_id)
            if business:
                token    = crud.get_decrypted_token(business)
                phone_id = business.get("whatsapp_phone_id")
                if token and phone_id:
                    _sent = send_whatsapp(phone_id, token, phone,
                        f"✅ *Payment Confirmed!*\n\n"
                        f"Thank you! Your payment for *{reference}* has been verified.\n\n"
                        f"💰 Amount: ${order_total:.2f}\n"
                        f"📦 Your order is now being prepared. Thank you! 🙏"
                    )
                    if isinstance(_sent, dict) and "error" in _sent:
                        # The payment is recorded either way; a failed notice must be visible
                        # (131047 = outside Meta's 24h customer-service window).
                        log.warning("payment_webhook: confirmation not delivered  order=%s  meta_code=%s",
                                    order_id, _wa_error_code(_sent))
        except Exception as exc:
            log.exception("payment_webhook notify error: %s", exc)

    return {"success": True, "order_id": order_id, "reference": reference,
            "message": f"Payment confirmed for {reference}"}


# ── Invoice download ──────────────────────────────────────────────────────────

@router.get("/invoice/{order_id}")
def download_invoice(order_id: int, user=Depends(require_business)):
    order = crud.get_order_by_id(order_id, user["business_id"])
    if not order:
        raise HTTPException(404, "Order not found")

    pdf_path = os.path.join(INVOICES_DIR, f"invoice_{order_id}.pdf")
    if not os.path.exists(pdf_path):
        try:
            business = crud.get_business_by_id(user["business_id"])
            order["business_name"] = business.get("name", "") if business else ""
            from services.pdf_invoice import generate_pdf_invoice
            pdf_path = generate_pdf_invoice(order)
        except Exception as exc:
            log.exception("download_invoice: PDF generation failed — %s", exc)
            raise HTTPException(500, "Failed to generate invoice PDF")

    return FileResponse(path=pdf_path, media_type="application/pdf",
                        filename=f"invoice_{order_id}.pdf")


# ── WebSocket ─────────────────────────────────────────────────────────────────

_WS_MAX_MESSAGE = 4096
_WS_MAX_PER_BUSINESS = 20


def _ws_origin_allowed(origin: str, host: str) -> bool:
    from urllib.parse import urlparse
    try:
        o = urlparse(origin)
    except Exception:
        return False
    if o.netloc and o.netloc.lower() == (host or "").lower():
        return True
    import os as _os
    allowed = {"https://wazibothq.com", "https://www.wazibothq.com", "http://localhost:3000",
               "http://localhost:5173", "http://127.0.0.1:3000"}
    allowed |= {x.strip().rstrip("/") for x in _os.getenv("ALLOWED_ORIGINS", "").split(",") if x.strip()}
    return origin.rstrip("/") in allowed


@router.websocket("/ws/chat/{business_id}")
async def websocket_chat(websocket: WebSocket, business_id: int):
    # Browsers always send Origin on a WebSocket handshake: refuse other sites (cross-site
    # WebSocket hijacking). Non-browser clients send none and still need a valid token.
    origin = (getattr(websocket, "headers", None) or {}).get("origin", "")
    if origin and not _ws_origin_allowed(origin, (getattr(websocket, "headers", None) or {}).get("host", "")):
        await websocket.close(code=4003)
        return
    if getattr(manager, "count", lambda _b: 0)(business_id) >= _WS_MAX_PER_BUSINESS:
        await websocket.close(code=4008)
        return

    token_param = websocket.query_params.get("token", "")
    # Cookie sessions (flag OFF by default): a browser's handshake carries the HttpOnly access cookie.
    # Only trusted when the browser also sent an Origin (already checked above): a non-browser has no
    # cookie, and a cross-site page is refused by the Origin check.
    _from_cookie = False
    if not token_param and origin:
        try:
            from core import cookie_auth as _ck
            if _ck.enabled():
                token_param = (getattr(websocket, "cookies", None) or {}).get(_ck.ACCESS_COOKIE, "") or ""
                _from_cookie = bool(token_param)
        except Exception as _wce:
            log.debug("ws cookie lookup skipped: %s", type(_wce).__name__)
    accepted = False
    if not token_param and hasattr(websocket, "accept"):
        # Preferred: token as the FIRST message ({"type":"auth","token":...}) so it never
        # appears in a URL, proxy log or browser history.
        try:
            await websocket.accept()
            accepted = True
            first = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=5))
            if len(str(first)) > _WS_MAX_MESSAGE or first.get("type") != "auth":
                raise ValueError("bad auth frame")
            token_param = str(first.get("token") or "")
        except Exception:
            await websocket.close(code=4001)
            return
    try:
        payload = decode_token(token_param)
        from core.auth import is_token_revoked as _revoked
        if _revoked(payload.get("jti")):
            await websocket.close(code=4001)
            return
        if payload.get("type") == "refresh":          # refresh tokens never authenticate
            await websocket.close(code=4001)
            return
        if _from_cookie and (payload.get("impersonated_by") or payload.get("role") == "superadmin"):
            await websocket.close(code=4001)          # support and admin sessions are Bearer-only
            return
        if payload.get("business_id") != business_id and payload.get("role") != "superadmin":
            await websocket.close(code=4001)
            return
        # Phase 4: same session checks as get_current_user: a token issued before a password
        # change, or for a suspended business, must not open a live socket.
        if payload.get("role") == "business" and not payload.get("impersonated_by"):
            from core.auth import _get_current_password_changed_at, _lookup_auth_state
            _cur = _get_current_password_changed_at(business_id)
            if _cur and _cur != payload.get("pwd_ts"):
                await websocket.close(code=4001)
                return
            _state = _lookup_auth_state(business_id)
            if _state and _state[1] is False:
                await websocket.close(code=4001)
                return
    except Exception:
        await websocket.close(code=4001)
        return

    await manager.connect(websocket, business_id, accepted=True) if accepted else await manager.connect(websocket, business_id)
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > _WS_MAX_MESSAGE:               # the socket only carries pings
                await websocket.close(code=1009)
                manager.disconnect(websocket, business_id)
                return
            try:
                msg = json.loads(raw) if raw else {}
                if msg.get("type") == "ping":
                    from core.auth import is_token_revoked as _rv
                    if _rv(payload.get("jti")):          # logged out while the socket was open
                        await websocket.close(code=4001)
                        return
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception as _swallowed_exc:
                from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, business_id)      # always free the slot
