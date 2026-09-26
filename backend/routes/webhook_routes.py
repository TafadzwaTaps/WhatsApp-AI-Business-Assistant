"""
routes/webhook_routes.py — WhatsApp webhook (GET verify + POST receive),
payment webhook, invoice download, and WebSocket.

Routes: GET /webhook, POST /webhook, POST /payment/webhook,
        GET /invoice/{order_id}, WS /ws/chat/{business_id}
"""

import json
import os
import logging

from fastapi import APIRouter, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
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
    except Exception:
        pass
    return "unknown"


# ── Webhook verify ────────────────────────────────────────────────────────────

@router.get("/webhook")
async def verify_webhook(request: Request):
    p         = request.query_params
    mode      = p.get("hub.mode", "")
    token     = p.get("hub.verify_token", "")
    challenge = p.get("hub.challenge", "")
    log.info("🔔 Webhook verify  mode=%r  token_match=%s  challenge=%r",
             mode, token == VERIFY_TOKEN, challenge)
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    raise HTTPException(403, "Webhook verification failed — token mismatch")


# ── Webhook receive ───────────────────────────────────────────────────────────

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
        if msg_type == "text":
            text = msg_obj.get("text", {}).get("body", "").strip()
        elif msg_type in ("image", "document", "sticker"):
            media_obj = msg_obj.get(msg_type, {})
            text = media_obj.get("caption", "").strip() or "[image]"
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

        log.info("📩 STEP 1 OK  wa_id=%s  from=%s  text=%r",
                 wa_message_id, customer_phone, text)
    except Exception as exc:
        log.error("📥 STEP 1 FAIL: %s", exc)
        return {"status": "ok"}

    # STEP 1b: Deduplication
    if wa_message_id:
        try:
            if crud.message_exists(wa_message_id):
                return {"status": "ok"}
        except Exception as exc:
            log.error("⚠️  Dedup check failed: %s", exc)

    # STEP 2: Find business
    try:
        from services.tenant_router import (
            is_shared_number, resolve_business_for_shared_number, is_switch_request,
        )
        # These were added in a later deploy — safe fallback if not yet present
        try:
            from services.tenant_router import (
                is_businesses_help_request, build_business_picker, _category_icon,
            )
        except ImportError:
            def is_businesses_help_request(t): return False
            def build_business_picker(b, p="WaziBot", current_name=""): return ""
            def _category_icon(c): return "🏪"
        if is_shared_number(phone_number_id):
            log.info("📋 STEP 2 — shared number  phone=%s", customer_phone)
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
                except Exception:
                    pass
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
                except Exception:
                    pass
                return {"status": "ok"}

            if direct_reply and business:
                # Business just selected — send confirmation immediately,
                # then fall through to generate_reply() for the welcome greeting
                _send_direct(phone_number_id, SHARED_WA_TOKEN, customer_phone, direct_reply)
                try:
                    cust_confirmed = crud.get_or_create_customer(customer_phone, business["id"])
                    crud.create_message(cust_confirmed["id"], business["id"],
                                        direct_reply, "outgoing", sender_type="ai")
                except Exception:
                    pass
                # Replace the customer's text with "hi" so generate_reply sends
                # a proper welcome rather than treating "2" as an order attempt
                text = "hi"

            if not business:
                log.error("📋 STEP 2 — no business resolved for shared number")
                return {"status": "ok"}
            token = SHARED_WA_TOKEN
        else:
            business = crud.get_business_by_phone_id(phone_number_id)
            if not business:
                log.error("📋 STEP 2 FAIL — No business for phone_number_id=%s", phone_number_id)
                return {"status": "ok"}
            if not business.get("is_active", True):
                return {"status": "ok"}
            token = ""
    except Exception as exc:
        log.exception("📋 STEP 2 FAIL: %s", exc)
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
        customer = crud.get_or_create_customer(customer_phone, business["id"])
    except Exception as exc:
        log.exception("👤 STEP 4 FAIL: %s", exc)
        return {"status": "ok"}

    # ACQUISITION TRACKING — record conversation_started on first ever message.
    # Runs after customer creation so we know the customer exists.
    # Fire-and-forget — never block the webhook response.
    # The acquisition_service deduplicates internally (one event per phone per biz).
    try:
        from services.acquisition_service import record_conversation_started
        record_conversation_started(business["id"], customer_phone)
    except Exception:
        pass  # analytics must never crash the webhook

    # STEP 5: Save incoming message
    in_msg: dict = {}
    try:
        crud.log_message(business["id"], customer_phone, "in", text)
        in_msg = crud.create_message(
            customer["id"], business["id"], text, "incoming",
            wa_message_id=wa_message_id,
        )
    except Exception as exc:
        err_str = str(exc)
        if "wa_message_id" in err_str or "unique" in err_str.lower():
            log.warning("💾 STEP 5 — Duplicate at INSERT  wa_id=%s", wa_message_id)
            return {"status": "ok"}
        log.exception("💾 STEP 5 FAIL: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": in_msg,
        })
    except Exception:
        pass

    # STEP 6: Generate AI reply
    try:
        products = crud.get_products(business["id"])
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
                try:
                    from services.whatsapp_service import transcribe_whatsapp_voice_note
                    voice_transcript = await transcribe_whatsapp_voice_note(
                        media_id=media_id, wa_token=token,
                        language=(business.get("preferred_language") or "en"),
                    )
                except Exception as exc:
                    log.warning("voice transcription call failed: %s", exc)
                    voice_transcript = None

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
                    send_whatsapp(phone_number_id, token, customer_phone, reply)
                return {"status": "ok"}

            # Transcription succeeded — treat the transcript as this
            # message's text and fall through into the normal pipeline
            # below (language detection, generate_reply, etc.) exactly
            # like any typed message.
            text = voice_transcript
            log.info("🎤 voice note transcribed  phone=%s  text=%r", customer_phone, text)

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

        if switch_reply:
            try:
                out_msg = crud.create_message(
                    customer["id"], business["id"], switch_reply, "outgoing", sender_type="ai",
                )
            except Exception as exc:
                log.warning("STEP 7 language-switch-reply failed: %s", exc)
            if token:
                send_whatsapp(phone_number_id, token, customer_phone, switch_reply)
            return {"status": "ok"}

        # ── Multi-language: passive detection from natural phrasing ────────
        # Already-built helper in translation_layer.py, previously never
        # called anywhere. Fire-and-forget — only writes preferred_language
        # if a recognised greeting/phrase is detected; never blocks or
        # alters this turn's AI reply.
        try:
            from services.translation_layer import detect_and_set_language
            detect_and_set_language(text, customer_phone, business["id"])
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
                              customer_phone, _customer_lang)
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
                    products=crud.get_products(business["id"]),
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
                            send_whatsapp(phone_number_id, token, customer_phone, clarify_reply)
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
        }

        reply = generate_reply(
            message=text,
            phone=customer_phone,
            business_id=business["id"],
            business_name=business["name"],
            products=products,
            message_has_image=message_has_image,
            message_is_from_agent=is_from_agent,
            voice_transcript=voice_transcript,
            business_config=biz_config,
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
            reply = maybe_translate(reply, customer_phone, business["id"], products=products)
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
        crud.log_message(business["id"], customer_phone, "out", reply)
        out_msg = crud.create_message(
            customer["id"], business["id"], reply, "outgoing", sender_type="ai",
        )
    except Exception as exc:
        log.exception("💾 STEP 7 FAIL: %s", exc)

    try:
        await manager.broadcast(business["id"], {
            "event": "new_message", "customer_id": customer["id"],
            "phone": customer_phone, "message": out_msg,
        })
    except Exception:
        pass

    # STEP 8: Send via WhatsApp API
    if not reply:
        log.info("📤 STEP 8 SKIP — empty reply  phone=%s  state=%s",
                 customer_phone, _get_customer_state_for_log(customer_phone, business["id"]))
        return {"status": "ok"}

    if token:
        result = send_whatsapp(phone_number_id, token, customer_phone, reply)
        if "error" in result:
            _log_event("wa.failed", phone=customer_phone, biz=business["id"],
                       error=result["error"])
        else:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            _log_event("wa.sent", phone=customer_phone, biz=business["id"],
                       msg_id=msg_id, reply_len=len(reply))
    else:
        log.error("📤 STEP 8 FAIL — No token for '%s'", business["name"])

    return {"status": "ok"}


# ── Payment webhook ───────────────────────────────────────────────────────────

class PaymentConfirmRequest(BaseModel):
    reference: str
    amount: float


@router.post("/payment/webhook")
async def payment_webhook(data: PaymentConfirmRequest):
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
    crud.update_order_payment(order_id, biz_id, {
        "payment_status":    "paid",
        "payment_reference": reference,
    })
    try:
        update_order_status_supabase(order_id, "paid")
    except Exception:
        pass

    phone = order.get("customer_phone", "")
    if phone and biz_id:
        try:
            business = crud.get_business_by_id(biz_id)
            if business:
                token    = crud.get_decrypted_token(business)
                phone_id = business.get("whatsapp_phone_id")
                if token and phone_id:
                    send_whatsapp(phone_id, token, phone,
                        f"✅ *Payment Confirmed!*\n\n"
                        f"Thank you! Your payment for *{reference}* has been verified.\n\n"
                        f"💰 Amount: ${order_total:.2f}\n"
                        f"📦 Your order is now being prepared. Thank you! 🙏"
                    )
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

@router.websocket("/ws/chat/{business_id}")
async def websocket_chat(websocket: WebSocket, business_id: int):
    token_param = websocket.query_params.get("token", "")
    try:
        payload = decode_token(token_param)
        if payload.get("business_id") != business_id and payload.get("role") != "superadmin":
            await websocket.close(code=4001)
            return
    except Exception:
        await websocket.close(code=4001)
        return

    await manager.connect(websocket, business_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw) if raw else {}
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket, business_id)
